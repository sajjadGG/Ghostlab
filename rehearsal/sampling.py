"""Safe tool sampling: actually call tools during discovery (roadmap Phase A2).

Contract lint judges what a server *declares*; sampling checks what it *does*
when called once with plausible arguments. The safety model is explicit:

- ``safe`` mode calls only tools the risk classifier marked **read-only**,
  with arguments generated from each parameter's schema (default → example →
  enum head → type zero-value).
- ``fixture`` mode additionally calls tools listed in the spec's
  ``setup.fixtures`` with their user-provided arguments — but a fixture for a
  state-mutating tool still requires ``approve_mutations`` and a destructive
  tool requires ``approve_destructive``. Nothing destructive ever runs
  implicitly.
- Tools whose required arguments cannot be generated are *skipped with a
  reason*, never guessed at.

Each sample records the arguments, outcome, latency, and a shape summary of
the result (content kinds, structuredContent keys, UI references), plus
contract-style findings for failures and result-shape problems.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .mcp_apps import tool_result_ui_ref

SAMPLE_MODES = ("off", "safe", "fixture")


# --------------------------------------------------------------------------- #
# Argument generation
# --------------------------------------------------------------------------- #
class ArgGenerationError(ValueError):
    """Raised when a required argument cannot be generated safely."""


def generate_example_value(schema: Any, name: str = "value", depth: int = 0) -> Any:
    """A plausible, safe value for one JSON-schema property."""
    if depth > 4:
        raise ArgGenerationError(f"{name}: schema nests too deeply to generate")
    if not isinstance(schema, dict):
        raise ArgGenerationError(f"{name}: property has no schema")
    if "default" in schema:
        return schema["default"]
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    if "const" in schema:
        return schema["const"]
    for combinator in ("anyOf", "oneOf"):
        options = schema.get(combinator)
        if isinstance(options, list) and options:
            return generate_example_value(options[0], name, depth + 1)

    schema_type = schema.get("type")
    if isinstance(schema_type, list) and schema_type:
        schema_type = schema_type[0]
    if schema_type == "string":
        return _example_string(schema, name)
    if schema_type == "integer":
        return int(schema.get("minimum", 1))
    if schema_type == "number":
        return float(schema.get("minimum", 1))
    if schema_type == "boolean":
        return False
    if schema_type == "array":
        min_items = int(schema.get("minItems", 0))
        if min_items <= 0:
            return []
        item = generate_example_value(schema.get("items", {}), f"{name}[]", depth + 1)
        return [item] * min_items
    if schema_type == "object":
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        return {
            key: generate_example_value(properties.get(key), f"{name}.{key}", depth + 1)
            for key in required
        }
    raise ArgGenerationError(f"{name}: cannot generate a value for schema type {schema_type!r}")


def _example_string(schema: dict[str, Any], name: str) -> str:
    fmt = schema.get("format", "")
    by_format = {
        "date": "2026-01-15",
        "date-time": "2026-01-15T12:00:00Z",
        "time": "12:00:00",
        "email": "ghostlab-sample@example.com",
        "uri": "https://example.com/ghostlab-sample",
        "url": "https://example.com/ghostlab-sample",
        "uuid": "00000000-0000-4000-8000-000000000000",
    }
    if fmt in by_format:
        return by_format[fmt]
    lowered = name.lower()
    if "lang" in lowered:
        return "en"
    if "id" == lowered or lowered.endswith("_id"):
        return "ghostlab-sample-id"
    value = "ghostlab sample"
    max_length = schema.get("maxLength")
    if isinstance(max_length, int):
        value = value[: max(1, max_length)]
    return value


def generate_arguments(tool: dict[str, Any]) -> dict[str, Any]:
    """Arguments covering every *required* parameter of a tool.

    Optional parameters are left out — the sample should exercise the minimal
    contract, and optional values are where generation guesses go wrong.
    """
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        raise ArgGenerationError("tool has no inputSchema")
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    return {
        name: generate_example_value(properties.get(name), name)
        for name in required
    }


# --------------------------------------------------------------------------- #
# Sampling plan + execution
# --------------------------------------------------------------------------- #
def plan_samples(
    contract: dict[str, Any],
    tools: list[dict[str, Any]],
    *,
    mode: str,
    fixtures: Optional[list[dict[str, Any]]] = None,
    approve_mutations: bool = False,
    approve_destructive: bool = False,
) -> list[dict[str, Any]]:
    """Decide which tools get called, with what arguments, and which are skipped.

    Returns entries: ``{tool, source, arguments}`` for planned calls and
    ``{tool, skipped: reason}`` for the rest, so the report shows *why* a tool
    went unsampled.
    """
    if mode not in SAMPLE_MODES:
        raise ValueError(f"unknown sample mode {mode!r}; expected one of {SAMPLE_MODES}")
    if mode == "off":
        return []

    risk_by_name = {entry["name"]: entry["risk"] for entry in contract.get("tools", [])}
    tools_by_name = {tool.get("name"): tool for tool in tools if tool.get("name")}
    fixture_by_tool: dict[str, dict[str, Any]] = {}
    for fixture in fixtures or []:
        if isinstance(fixture, dict) and fixture.get("tool"):
            fixture_by_tool[str(fixture["tool"])] = dict(fixture.get("arguments") or {})

    plan: list[dict[str, Any]] = []
    for name, tool in tools_by_name.items():
        risk = risk_by_name.get(name, {})
        read_only = risk.get("read_only")
        destructive = bool(risk.get("destructive"))
        fixture_args = fixture_by_tool.get(name)

        if mode == "fixture" and fixture_args is not None:
            if destructive and not approve_destructive:
                plan.append({"tool": name, "skipped": "destructive fixture requires --approve-destructive"})
            elif read_only is not True and not destructive and not approve_mutations:
                plan.append({"tool": name, "skipped": "mutating fixture requires --approve-mutations"})
            else:
                plan.append({"tool": name, "source": "fixture", "arguments": fixture_args})
            continue

        if read_only is not True:
            reason = "destructive" if destructive else (
                "mutates state" if read_only is False else "mutation unknown"
            )
            plan.append({"tool": name, "skipped": f"not sampled: {reason} (add a fixture to test it)"})
            continue
        try:
            plan.append({"tool": name, "source": "generated", "arguments": generate_arguments(tool)})
        except ArgGenerationError as exc:
            plan.append({"tool": name, "skipped": f"cannot generate arguments: {exc}"})
    return plan


def run_samples(
    client: Any,
    plan: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Execute a sampling plan against a connected MCP client."""
    tools_by_name = {tool.get("name"): tool for tool in tools if tool.get("name")}
    samples: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    called = failed = 0

    for entry in plan:
        name = entry["tool"]
        if "skipped" in entry:
            samples.append({"tool": name, "status": "skipped", "reason": entry["skipped"]})
            continue
        called += 1
        started = time.monotonic()
        record: dict[str, Any] = {
            "tool": name,
            "status": "ok",
            "source": entry["source"],
            "arguments": entry["arguments"],
        }
        try:
            result = client.call_tool(name, entry["arguments"])
        except Exception as exc:  # noqa: BLE001 — a failing call is a finding, not a crash
            failed += 1
            record.update({"status": "error", "error": str(exc)})
            findings.append({
                "kind": "sample_call_failed", "severity": "error", "in": f"tool:{name}",
                "message": f"call with {entry['source']} arguments failed: {exc}",
            })
            samples.append(record)
            continue
        record["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        record["result"] = summarize_result(result)
        if record["result"]["is_error"]:
            failed += 1
            record["status"] = "tool_error"
            findings.append({
                "kind": "sample_tool_error", "severity": "warning", "in": f"tool:{name}",
                "message": "tool returned isError=true for minimal valid arguments: "
                           + (record["result"]["first_text"][:200] or "(no message)"),
            })
        findings.extend(_shape_findings(name, tools_by_name.get(name), result))
        samples.append(record)

    return {
        "samples": samples,
        "findings": findings,
        "summary": {
            "planned": len(plan),
            "called": called,
            "failed": failed,
            "skipped": sum(1 for sample in samples if sample["status"] == "skipped"),
        },
    }


def summarize_result(result: Any) -> dict[str, Any]:
    """Shape summary of a tools/call result — never the full payload."""
    result = result if isinstance(result, dict) else {}
    content = result.get("content") or []
    kinds = [
        entry.get("type", "?") for entry in content if isinstance(entry, dict)
    ]
    first_text = ""
    for entry in content:
        if isinstance(entry, dict) and entry.get("type") == "text":
            first_text = str(entry.get("text", ""))[:300]
            break
    structured = result.get("structuredContent")
    return {
        "is_error": bool(result.get("isError")),
        "content_kinds": kinds,
        "first_text": first_text,
        "structured_keys": sorted(structured) if isinstance(structured, dict) else [],
        "ui_resource": tool_result_ui_ref(result),
    }


def _shape_findings(
    name: str, tool: Optional[dict[str, Any]], result: Any
) -> list[dict[str, Any]]:
    """Result-shape checks that need a live call to observe."""
    findings: list[dict[str, Any]] = []
    if not isinstance(result, dict) or not isinstance(tool, dict):
        return findings
    where = f"tool:{name}"
    if isinstance(tool.get("outputSchema"), dict) and not isinstance(
        result.get("structuredContent"), dict
    ):
        findings.append({
            "kind": "missing_structured_content", "severity": "warning", "in": where,
            "message": "tool declares an outputSchema but returned no "
                       "structuredContent; hosts that rely on it see nothing",
        })
    from .mcp_apps import ui_resource_uri

    declares_ui = ui_resource_uri(tool.get("_meta")) is not None
    if declares_ui and not result.get("isError"):
        content = result.get("content") or []
        has_model_visible = any(
            isinstance(entry, dict) and entry.get("type") == "text" and entry.get("text")
            for entry in content
        ) or isinstance(result.get("structuredContent"), dict)
        if not has_model_visible:
            findings.append({
                "kind": "ui_tool_no_model_content", "severity": "warning", "in": where,
                "message": "UI-producing tool returned neither text content nor "
                           "structuredContent; the host model cannot narrate the widget",
            })
    return findings
