"""`rehearsal inspect` — connect to a target MCP and capture what it exposes.

Produces a raw `inspect.json` plus a readable `inspect.md`, and lints tool and
resource descriptions for references to tools that the server does not expose
(e.g. Cortex descriptions mention `kb_find` / `kb_read_skill` which are absent).
This artifact is the input to capability profiling and scenario generation.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import TargetConfig
from .mcp_client import McpClient, create_client

# Backtick-quoted spans, e.g. `kb_find` or `kb_read_skill({ skill: 'reading' })`.
_BACKTICK_SPAN_RE = re.compile(r"`([^`]+)`")
# Leading identifier inside a span, optionally immediately followed by "(" (call).
_SPAN_IDENT_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)*)\s*(\()?")
# Verbs that, when they precede a span, signal the identifier is invoked as a
# tool (e.g. "call `kb_read_skill`", "via `kb_find`").
_CALL_VERB_RE = re.compile(r"(?:call|calls|calling|invoke|invokes|via|through)\s*$", re.IGNORECASE)


def _prefix(name: str) -> str:
    return re.split(r"[._]", name)[0]


@dataclass
class InspectResult:
    target_id: str
    transport: str
    server_info: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    resources: list[dict[str, Any]] = field(default_factory=list)
    resource_templates: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    lint: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Candidate:
    token: str
    source: str
    strong: bool  # called like `tok(...)` or preceded by a call verb


def _collect_candidates(
    sources: list[tuple[str, str]], tool_names: set[str]
) -> list[_Candidate]:
    """Pull identifier candidates out of backtick spans across all descriptions."""
    candidates: list[_Candidate] = []
    for source, text in sources:
        if not text:
            continue
        for span in _BACKTICK_SPAN_RE.finditer(text):
            ident_match = _SPAN_IDENT_RE.match(span.group(1))
            if not ident_match:
                continue
            token = ident_match.group(1)
            normalized = token.replace(".", "_")
            if token in tool_names or normalized in tool_names:
                continue
            if "_" not in token and "." not in token:
                continue  # bare words (langs, single fields) are too noisy
            is_call = bool(ident_match.group(2))
            preceding = text[max(0, span.start() - 16):span.start()]
            strong = is_call or bool(_CALL_VERB_RE.search(preceding))
            candidates.append(_Candidate(token=token, source=source, strong=strong))
    return candidates


def lint_missing_tool_refs(
    tools: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    instructions: str = "",
) -> list[dict[str, Any]]:
    """Flag backticked identifiers in descriptions that aren't exposed tools.

    Two passes keep false positives down. A candidate is reported if it is
    *strongly* referenced (called as ``tok(...)`` or preceded by a call verb),
    or if it belongs to a "suspect family" — a name prefix (e.g. ``kb``) that has
    at least one strong reference but is not the prefix of any real tool. This
    catches coordinated mentions like "via `kb_find` and `kb_read`" without
    flagging schema fields such as `expected_version` or `correct_index`.

    Server ``instructions`` are linted too — they steer the host model just as
    hard as tool descriptions do.
    """
    tool_names = {t.get("name", "") for t in tools if t.get("name")}
    real_prefixes = {_prefix(name) for name in tool_names}

    sources: list[tuple[str, str]] = []
    for tool in tools:
        sources.append((f"tool:{tool.get('name', '?')}", tool.get("description", "") or ""))
    for resource in resources:
        sources.append((f"resource:{resource.get('uri', '?')}", resource.get("description", "") or ""))
    if instructions:
        sources.append(("instructions", instructions))

    candidates = _collect_candidates(sources, tool_names)
    suspect_families = {
        _prefix(c.token)
        for c in candidates
        if c.strong and _prefix(c.token) not in real_prefixes
    }

    findings: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate.strong or _prefix(candidate.token) in suspect_families:
            findings.append(
                {
                    "kind": "missing_tool_reference",
                    "referenced": candidate.token,
                    "in": candidate.source,
                }
            )

    # De-duplicate (referenced, in) pairs while preserving order.
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for finding in findings:
        key = (finding["referenced"], finding["in"])
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def inspect_target(target: TargetConfig, timeout: float = 30.0) -> InspectResult:
    client: McpClient = create_client(target, timeout=timeout)
    try:
        client.initialize()
        tools = client.list_collection("tools/list", "tools")
        resources = client.list_collection("resources/list", "resources")
        resource_templates = client.list_collection(
            "resources/templates/list", "resourceTemplates"
        )
        prompts = client.list_collection("prompts/list", "prompts")
    finally:
        client.close()

    lint = lint_missing_tool_refs(tools, resources, instructions=client.instructions)
    return InspectResult(
        target_id=target.id,
        transport=target.transport,
        server_info=client.server_info,
        capabilities=client.capabilities,
        instructions=client.instructions,
        tools=tools,
        resources=resources,
        resource_templates=resource_templates,
        prompts=prompts,
        lint=lint,
    )


def _schema_summary(schema: dict[str, Any]) -> str:
    """One-line summary of an input schema: required + optional property names."""
    if not isinstance(schema, dict):
        return ""
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    if not props:
        return "(no parameters)"
    parts = []
    for name in props:
        parts.append(name if name in required else f"{name}?")
    return ", ".join(parts)


def write_inspect_artifacts(result: InspectResult, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "inspect.json"
    md_path = out_dir / "inspect.md"

    json_path.write_text(
        json.dumps(asdict(result), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    server = result.server_info or {}
    noun = result.transport.title() if result.transport in ("skill", "agent") else "MCP"
    lines = [
        f"# {noun} Inspect: {result.target_id}",
        "",
        f"- Server: `{server.get('name', '?')}@{server.get('version', '?')}`",
        f"- Transport: `{result.transport}`",
        f"- Tools: {len(result.tools)} | Resources: {len(result.resources)} "
        f"| Templates: {len(result.resource_templates)} | Prompts: {len(result.prompts)}",
        f"- Lint findings: {len(result.lint)}",
        "",
    ]
    if result.instructions:
        lines += [f"## {noun} instructions", "", result.instructions.strip(), ""]

    lines += ["## Tools", ""]
    for tool in result.tools:
        name = tool.get("name", "?")
        title = tool.get("title", "")
        desc = (tool.get("description", "") or "").strip().replace("\n", " ")
        if len(desc) > 240:
            desc = desc[:237] + "..."
        params = _schema_summary(tool.get("inputSchema", {}))
        ui = tool.get("_meta", {}).get("ui", {}).get("resourceUri")
        header = f"### `{name}`" + (f" — {title}" if title else "")
        lines.append(header)
        if desc:
            lines.append(desc)
        lines.append(f"- params: {params}")
        if ui:
            lines.append(f"- ui resource: `{ui}`")
        lines.append("")

    if result.resources:
        lines += ["## Resources", ""]
        for resource in result.resources:
            lines.append(f"- `{resource.get('uri', '?')}` — {resource.get('name', '')}")
        lines.append("")

    if result.prompts:
        lines += ["## Prompts", ""]
        for prompt in result.prompts:
            lines.append(f"- `{prompt.get('name', '?')}` — {prompt.get('description', '')}")
        lines.append("")

    lines += ["## Lint findings", ""]
    if not result.lint:
        lines.append("None.")
    else:
        lines.append("Descriptions reference these identifiers that are **not** exposed as tools:")
        lines.append("")
        for finding in result.lint:
            lines.append(f"- `{finding['referenced']}` referenced in {finding['in']}")
    lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
