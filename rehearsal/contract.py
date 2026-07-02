"""Contract model: turn an `inspect.json` into a linted `contract.json`.

The inspect stage captures *what* a server exposes; this module judges *how
well* it exposes it (roadmap section 2, "Stronger MCP Contract Inspection"):

- **Schema lint** — missing/weak descriptions, untyped or undocumented
  parameters, `required` names that aren't defined, schema features real hosts
  handle poorly (`$ref`, top-level `oneOf`/`allOf`), oversized input surfaces.
- **Risk classification** — read-only vs mutating, destructive, open-world and
  idempotency, credential-bearing parameters, UI-producing. MCP tool
  `annotations` (`readOnlyHint`, `destructiveHint`, ...) are trusted when
  present; name/description heuristics fill the gaps and the `source` field
  records which one spoke.
- **UI metadata compatibility** — standard `_meta.ui.resourceUri` vs the
  OpenAI `openai/outputTemplate` alias, dangling `ui://` references, unknown
  `_meta.ui.visibility` values.

Everything is deterministic — no model calls — so the contract is cheap enough
to regenerate on every `ghostlab discover` and stable enough to gate releases.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .mcp_apps import ui_resource_uri
from .types import utc_now

SEVERITIES = ("error", "warning", "info")

# Verb stems that imply a tool mutates server state / is destructive. Matched
# against `_`-separated name tokens so `views_create_reading_practice` hits
# `create` but `regenerate_summary` does not hit `generate` twice.
_MUTATING_STEMS = (
    "create", "update", "delete", "set", "write", "add", "remove", "insert",
    "complete", "record", "reset", "send", "post", "submit", "upload", "move",
    "rename", "assign", "cancel", "start", "stop", "restart", "apply", "save",
    "mark", "register", "sync", "import", "generate", "make", "build",
)
_READONLY_STEMS = (
    "get", "list", "read", "search", "find", "fetch", "status", "show",
    "describe", "query", "lookup", "check", "view", "preview", "count",
    "inspect", "probe", "export", "download",
)
_DESTRUCTIVE_STEMS = ("delete", "remove", "reset", "drop", "purge", "wipe", "destroy", "clear")
_CREDENTIAL_TOKENS = ("token", "api_key", "apikey", "password", "secret", "credential", "auth")

# Names so generic a host model can't pick between servers exposing them.
_GENERIC_NAMES = {"run", "execute", "do", "call", "invoke", "process", "handle", "action", "main", "tool"}

_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

# JSON Schema keywords that many MCP hosts translate poorly into native
# tool-calling schemas.
_HOST_UNFRIENDLY_KEYWORDS = ("$ref", "$defs", "definitions", "patternProperties", "if", "then", "else")

_KNOWN_VISIBILITY = {"visible", "hidden"}

_MIN_TOOL_DESCRIPTION = 20  # chars; below this a model has little to go on


def _finding(kind: str, severity: str, where: str, message: str, **extra: Any) -> dict[str, Any]:
    assert severity in SEVERITIES
    return {"kind": kind, "severity": severity, "in": where, "message": message, **extra}


def _name_tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[._\-]", name.lower()) if token]


# --------------------------------------------------------------------------- #
# Schema lint
# --------------------------------------------------------------------------- #
def lint_tool_schema(tool: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic quality checks for one tool's contract surface."""
    name = tool.get("name", "?")
    where = f"tool:{name}"
    findings: list[dict[str, Any]] = []

    description = (tool.get("description") or "").strip()
    if not description:
        findings.append(_finding(
            "missing_tool_description", "error", where,
            "tool has no description; hosts select tools by description",
        ))
    elif len(description) < _MIN_TOOL_DESCRIPTION:
        findings.append(_finding(
            "weak_tool_description", "warning", where,
            f"description is only {len(description)} chars; models need intent, "
            "constraints, and when-to-use guidance",
        ))

    if name != "?":
        if name.lower() in _GENERIC_NAMES:
            findings.append(_finding(
                "generic_tool_name", "warning", where,
                f"name '{name}' is too generic for reliable tool selection",
            ))
        elif not _SNAKE_CASE_RE.match(name):
            findings.append(_finding(
                "unconventional_tool_name", "info", where,
                f"name '{name}' is not snake_case; some hosts normalize names "
                "inconsistently",
            ))

    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        findings.append(_finding(
            "missing_input_schema", "error", where,
            "tool declares no inputSchema; hosts cannot construct arguments",
        ))
        return findings

    for keyword in _HOST_UNFRIENDLY_KEYWORDS:
        if _schema_uses_keyword(schema, keyword):
            findings.append(_finding(
                "host_unfriendly_schema", "warning", where,
                f"inputSchema uses '{keyword}', which several hosts translate "
                "poorly; prefer inlined, flat schemas",
                keyword=keyword,
            ))

    for combinator in ("oneOf", "anyOf", "allOf"):
        if combinator in schema:
            findings.append(_finding(
                "top_level_combinator", "warning", where,
                f"inputSchema is a top-level '{combinator}'; hosts that flatten "
                "to a single object schema will mangle it",
            ))

    properties = schema.get("properties")
    required = schema.get("required") or []
    if not isinstance(properties, dict):
        properties = {}
    if isinstance(required, list):
        for req in required:
            if req not in properties:
                findings.append(_finding(
                    "required_param_undefined", "error", where,
                    f"required parameter '{req}' is not defined in properties",
                    param=str(req),
                ))

    if len(properties) > 12:
        findings.append(_finding(
            "large_input_surface", "warning", where,
            f"{len(properties)} parameters; large flat surfaces raise "
            "wrong-argument rates — consider grouping or splitting the tool",
        ))

    for prop_name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        prop_where = f"{where}#{prop_name}"
        has_type = any(key in prop for key in ("type", "enum", "const", "anyOf", "oneOf", "$ref"))
        if not has_type:
            findings.append(_finding(
                "untyped_param", "warning", prop_where,
                f"parameter '{prop_name}' declares no type/enum; hosts will "
                "guess and often guess strings",
                param=prop_name,
            ))
        if not (prop.get("description") or "").strip():
            severity = "warning" if prop_name in required else "info"
            findings.append(_finding(
                "undocumented_param", severity, prop_where,
                f"parameter '{prop_name}' has no description"
                + (" (and is required)" if prop_name in required else ""),
                param=prop_name,
            ))
        enum = prop.get("enum")
        if isinstance(enum, list) and len(enum) > 6 and not (prop.get("description") or "").strip():
            findings.append(_finding(
                "undocumented_enum", "warning", prop_where,
                f"parameter '{prop_name}' offers {len(enum)} enum values with no "
                "description explaining how to choose",
                param=prop_name,
            ))
    return findings


def _schema_uses_keyword(schema: Any, keyword: str) -> bool:
    if isinstance(schema, dict):
        if keyword in schema:
            return True
        return any(_schema_uses_keyword(value, keyword) for value in schema.values())
    if isinstance(schema, list):
        return any(_schema_uses_keyword(item, keyword) for item in schema)
    return False


# --------------------------------------------------------------------------- #
# Risk classification
# --------------------------------------------------------------------------- #
def classify_tool_risk(tool: dict[str, Any]) -> dict[str, Any]:
    """Risk labels for one tool. MCP `annotations` win; heuristics fill gaps."""
    name = tool.get("name", "") or ""
    description = (tool.get("description") or "").lower()
    tokens = set(_name_tokens(name))
    annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}

    sources: set[str] = set()

    def annotated(key: str) -> Optional[bool]:
        value = annotations.get(key)
        if isinstance(value, bool):
            sources.add("annotations")
            return value
        return None

    read_only = annotated("readOnlyHint")
    if read_only is None:
        if tokens & set(_MUTATING_STEMS):
            read_only = False
            sources.add("heuristic")
        elif tokens & set(_READONLY_STEMS):
            read_only = True
            sources.add("heuristic")

    destructive = annotated("destructiveHint")
    if destructive is None:
        if read_only is True:
            destructive = False  # a declared read-only tool cannot destroy
        else:
            destructive = bool(tokens & set(_DESTRUCTIVE_STEMS))
            if destructive:
                sources.add("heuristic")
    elif destructive and read_only is True:
        destructive = False

    idempotent = annotated("idempotentHint")
    open_world = annotated("openWorldHint")

    properties = ((tool.get("inputSchema") or {}).get("properties") or {})
    credential_params = [
        prop for prop in properties
        if any(token in prop.lower() for token in _CREDENTIAL_TOKENS)
    ]
    url_params = [prop for prop in properties if "url" in prop.lower()]
    external_network = bool(url_params) or any(
        marker in description for marker in ("http://", "https://", "external api", "web request")
    )

    produces_ui = ui_resource_uri(tool.get("_meta")) is not None

    labels: list[str] = []
    if read_only is True:
        labels.append("read-only")
    elif read_only is False:
        labels.append("mutates-state")
    else:
        labels.append("unknown-mutation")
    if destructive:
        labels.append("destructive")
    if external_network:
        labels.append("external-network")
    if credential_params:
        labels.append("credential-bearing")
    if produces_ui:
        labels.append("ui-producing")
    if open_world:
        labels.append("open-world")

    return {
        "read_only": read_only,
        "destructive": bool(destructive),
        "idempotent": idempotent,
        "open_world": open_world,
        "external_network": external_network,
        "credential_params": credential_params,
        "url_params": url_params,
        "produces_ui": produces_ui,
        "labels": labels,
        "source": "annotations" if sources == {"annotations"} else
                  ("mixed" if "annotations" in sources else "heuristic"),
    }


def risk_findings(tool: dict[str, Any], risk: dict[str, Any]) -> list[dict[str, Any]]:
    """Findings derived from the risk map itself."""
    where = f"tool:{tool.get('name', '?')}"
    findings: list[dict[str, Any]] = []
    if risk["read_only"] is None:
        findings.append(_finding(
            "unclassifiable_mutation", "info", where,
            "cannot tell whether this tool mutates state; add MCP tool "
            "annotations (readOnlyHint) or a clearer verb to its name",
        ))
    if risk["destructive"] and not (tool.get("annotations") or {}).get("destructiveHint"):
        findings.append(_finding(
            "undeclared_destructive", "warning", where,
            "name suggests a destructive action but the tool does not declare "
            "annotations.destructiveHint; hosts cannot gate it behind approval",
        ))
    if risk["credential_params"]:
        findings.append(_finding(
            "credential_in_arguments", "warning", where,
            "parameters %s look credential-bearing; secrets in tool arguments "
            "end up in transcripts and logs" % ", ".join(risk["credential_params"]),
            params=risk["credential_params"],
        ))
    return findings


# --------------------------------------------------------------------------- #
# UI metadata compatibility
# --------------------------------------------------------------------------- #
OPENAI_OUTPUT_TEMPLATE_KEY = "openai/outputTemplate"


def lint_ui_metadata(
    tool: dict[str, Any], resource_uris: set[str]
) -> list[dict[str, Any]]:
    """Standard vs OpenAI-alias `_meta` checks plus dangling `ui://` references."""
    name = tool.get("name", "?")
    where = f"tool:{name}"
    meta = tool.get("_meta") if isinstance(tool.get("_meta"), dict) else {}
    findings: list[dict[str, Any]] = []

    standard_uri = ui_resource_uri(meta)
    openai_uri = meta.get(OPENAI_OUTPUT_TEMPLATE_KEY)
    openai_uri = openai_uri if isinstance(openai_uri, str) and openai_uri else None

    if openai_uri and not standard_uri:
        findings.append(_finding(
            "openai_alias_only", "warning", where,
            f"declares '{OPENAI_OUTPUT_TEMPLATE_KEY}' but not the standard "
            "_meta.ui.resourceUri; non-ChatGPT hosts will not render the widget",
        ))
    if standard_uri and not openai_uri:
        findings.append(_finding(
            "missing_openai_alias", "info", where,
            f"declares _meta.ui.resourceUri but not '{OPENAI_OUTPUT_TEMPLATE_KEY}'; "
            "add the alias for ChatGPT compatibility",
        ))
    if standard_uri and openai_uri and standard_uri != openai_uri:
        findings.append(_finding(
            "ui_alias_mismatch", "error", where,
            f"standard ({standard_uri}) and OpenAI ({openai_uri}) UI URIs differ; "
            "hosts will render different widgets",
        ))

    for uri in {standard_uri, openai_uri} - {None}:
        if resource_uris and uri not in resource_uris:
            findings.append(_finding(
                "dangling_ui_resource", "error", where,
                f"references {uri} but the server does not list that resource",
                uri=uri,
            ))

    ui_meta = meta.get("ui") if isinstance(meta.get("ui"), dict) else {}
    visibility = ui_meta.get("visibility")
    if visibility is not None and visibility not in _KNOWN_VISIBILITY:
        findings.append(_finding(
            "unknown_ui_visibility", "warning", where,
            f"_meta.ui.visibility is {visibility!r}; expected one of "
            f"{sorted(_KNOWN_VISIBILITY)}",
        ))
    return findings


# --------------------------------------------------------------------------- #
# Contract assembly
# --------------------------------------------------------------------------- #
def build_contract(inspect_data: dict[str, Any]) -> dict[str, Any]:
    """Assemble `contract.json` from an `inspect.json` payload."""
    tools = inspect_data.get("tools", []) or []
    resources = inspect_data.get("resources", []) or []
    resource_uris = {
        res.get("uri") for res in resources if isinstance(res, dict) and res.get("uri")
    }
    server = inspect_data.get("server_info", {}) or {}

    tool_entries: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        risk = classify_tool_risk(tool)
        tool_findings = (
            lint_tool_schema(tool)
            + risk_findings(tool, risk)
            + lint_ui_metadata(tool, resource_uris)
        )
        findings.extend(tool_findings)
        schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        tool_entries.append({
            "name": tool.get("name", "?"),
            "title": tool.get("title", ""),
            "description_chars": len((tool.get("description") or "").strip()),
            "params": {
                "required": sorted(name for name in properties if name in required),
                "optional": sorted(name for name in properties if name not in required),
            },
            "risk": risk,
            "ui_resource": ui_resource_uri(tool.get("_meta")),
            "findings": len(tool_findings),
        })

    # Fold in the inspect-stage description lint (missing tool references).
    for entry in inspect_data.get("lint", []) or []:
        if isinstance(entry, dict) and entry.get("kind") == "missing_tool_reference":
            findings.append(_finding(
                "missing_tool_reference", "warning", str(entry.get("in", "?")),
                f"description references `{entry.get('referenced')}`, which is "
                "not an exposed tool",
                referenced=entry.get("referenced"),
            ))

    by_severity = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        by_severity[finding["severity"]] += 1

    risk_counts: dict[str, int] = {}
    for entry in tool_entries:
        for label in entry["risk"]["labels"]:
            risk_counts[label] = risk_counts.get(label, 0) + 1

    return {
        "generated_at": utc_now(),
        "target_id": inspect_data.get("target_id", "?"),
        "mcp": f"{server.get('name', '?')}@{server.get('version', '?')}",
        "transport": inspect_data.get("transport", "?"),
        "counts": {
            "tools": len(tool_entries),
            "resources": len(resources),
            "prompts": len(inspect_data.get("prompts", []) or []),
            "ui_tools": sum(1 for entry in tool_entries if entry["risk"]["produces_ui"]),
        },
        "tools": tool_entries,
        "findings": findings,
        "summary": {
            "findings_by_severity": by_severity,
            "risk_labels": dict(sorted(risk_counts.items())),
            "tools_with_findings": sum(1 for entry in tool_entries if entry["findings"]),
        },
    }


def merge_findings(contract: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """Fold extra findings (e.g. from live tool sampling) into a contract.

    Appends them and recomputes the severity summary so gates and reports see
    one consistent findings list.
    """
    if not findings:
        return
    contract["findings"] = list(contract.get("findings", [])) + list(findings)
    by_severity = {severity: 0 for severity in SEVERITIES}
    for finding in contract["findings"]:
        by_severity[finding.get("severity", "info")] += 1
    contract.setdefault("summary", {})["findings_by_severity"] = by_severity


def render_contract_md(contract: dict[str, Any]) -> str:
    """Readable Markdown view of a contract."""
    counts = contract.get("counts", {})
    summary = contract.get("summary", {})
    severities = summary.get("findings_by_severity", {})
    lines = [
        f"# MCP Contract: {contract.get('target_id', '?')}",
        "",
        f"- Server: `{contract.get('mcp', '?')}` over `{contract.get('transport', '?')}`",
        f"- Tools: {counts.get('tools', 0)} ({counts.get('ui_tools', 0)} UI-producing)"
        f" | Resources: {counts.get('resources', 0)} | Prompts: {counts.get('prompts', 0)}",
        f"- Findings: {severities.get('error', 0)} error(s), "
        f"{severities.get('warning', 0)} warning(s), {severities.get('info', 0)} info",
        "",
        "## Tools",
        "",
        "| tool | risk | required params | findings |",
        "|---|---|---|---|",
    ]
    for entry in contract.get("tools", []):
        labels = ", ".join(entry["risk"]["labels"]) or "-"
        required = ", ".join(entry["params"]["required"]) or "-"
        lines.append(
            f"| `{entry['name']}` | {labels} | {required} | {entry['findings']} |"
        )
    lines += ["", "## Findings", ""]
    findings = contract.get("findings", [])
    if not findings:
        lines.append("None.")
    for severity in SEVERITIES:
        group = [f for f in findings if f["severity"] == severity]
        if not group:
            continue
        lines.append(f"### {severity.capitalize()}s")
        lines.append("")
        for finding in group:
            lines.append(f"- **{finding['kind']}** ({finding['in']}): {finding['message']}")
        lines.append("")
    return "\n".join(lines) + "\n"
