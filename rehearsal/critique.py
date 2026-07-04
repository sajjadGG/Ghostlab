"""`rehearsal critique` — turn a run into MCP tool-usability feedback.

Where `evaluate` answers "did this scenario pass?", `critique` answers "how do I
improve this MCP server?". It collects the tools the agent-under-test actually
exercised, pairs each with its real definition (description + input schema from
an inspect.json), and asks a codex judge to grade naming, description quality,
parameter clarity, and error-message quality — with concrete suggestions.

This mirrors the dual-purpose tool evaluation in anthropics/claude-cookbooks,
where the agent both solves the task and critiques the tools it used. Output is a
`critique.json` + `critique.md` artifact alongside the run's `verdict.*`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .codex_backend import CodexBackend
from .evaluate import read_run
from .inspect import _schema_summary

DESCRIPTION_QUALITIES = ("good", "adequate", "unclear", "missing")
ERROR_QUALITIES = ("good", "unclear", "missing", "n/a")

_CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tools": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "name_clarity": {"type": "integer", "minimum": 0, "maximum": 5},
                    "description_quality": {"type": "string", "enum": list(DESCRIPTION_QUALITIES)},
                    "param_issues": {"type": "array", "items": {"type": "string"}},
                    "error_quality": {"type": "string", "enum": list(ERROR_QUALITIES)},
                    "suggestions": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "name",
                    "name_clarity",
                    "description_quality",
                    "param_issues",
                    "error_quality",
                    "suggestions",
                ],
                "additionalProperties": False,
            },
        },
        "overall_score": {"type": "integer", "minimum": 0, "maximum": 5},
        "overall_notes": {"type": "string"},
        "top_recommendations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tools", "overall_score", "overall_notes", "top_recommendations"],
    "additionalProperties": False,
}


def _tool_defs(inspect: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Index inspect.json tool definitions by name."""
    if not inspect:
        return {}
    return {t.get("name", ""): t for t in inspect.get("tools", []) if t.get("name")}


def collect_exercised_tools(
    run: dict[str, Any], inspect: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Group a run's tool calls by tool, paired with each tool's real definition.

    Only tools the agent actually called are returned — critique is evidence-based,
    grounded in observed usage rather than the full catalog. Each entry carries the
    definition (description + param summary) when an inspect.json is supplied, plus
    every observed call (arguments, status, error) so the judge can reason about
    real behavior.
    """
    defs = _tool_defs(inspect)
    by_tool: dict[str, dict[str, Any]] = {}
    for call in run.get("tool_calls", []):
        name = call.get("tool", "?")
        entry = by_tool.get(name)
        if entry is None:
            definition = defs.get(name, {})
            entry = {
                "name": name,
                "server": call.get("server", "?"),
                "description": (definition.get("description", "") or "").strip(),
                "params": _schema_summary(definition.get("inputSchema", {})) if definition else "",
                "known": name in defs,
                "calls": [],
            }
            by_tool[name] = entry
        entry["calls"].append(
            {
                "status": call.get("status", "?"),
                "arguments": call.get("arguments"),
                "error": call.get("error"),
            }
        )
    return list(by_tool.values())


def _format_call(call: dict[str, Any]) -> str:
    args = call.get("arguments")
    arg_str = json.dumps(args)[:200] if args is not None else "(args not captured)"
    line = f"  - [{call.get('status', '?')}] args={arg_str}"
    error = call.get("error")
    if error:
        line += f"\n    error: {json.dumps(error)[:200]}"
    return line


def _format_tools(tools: list[dict[str, Any]]) -> str:
    if not tools:
        return "(no tools were exercised in this run)"
    blocks: list[str] = []
    for tool in tools:
        header = f"### {tool['server']}/{tool['name']}"
        if not tool["known"]:
            header += "  (NOT in the inspected server definition)"
        lines = [header]
        lines.append(f"description: {tool['description'] or '(none provided)'}")
        if tool["known"]:
            lines.append(f"parameters: {tool['params'] or '(no parameters)'}")
        lines.append(f"observed calls ({len(tool['calls'])}):")
        lines.extend(_format_call(c) for c in tool["calls"])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _format_transcript(transcript: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{t.get('role', '?').upper()}: {t.get('content', '')}" for t in transcript
    )


# Placeholders: {goal} {tools} {transcript}
CRITIQUE_TEMPLATE = """You are a developer-experience reviewer for an MCP (Model Context Protocol) server.
An assistant used this server's tools to pursue a user goal. Critique the *tools*,
not the assistant: judge how well-designed each exercised tool is for an LLM client.

Scenario goal:
{goal}

Tools the assistant exercised (with their real definitions and observed calls):
{tools}

Conversation transcript (for context on how tools were chosen and whether results were usable):
{transcript}

For each exercised tool, judge from evidence:
- name_clarity (0-5): does the name make its purpose obvious to an LLM with no extra context?
- description_quality: "good" (clear + complete), "adequate" (usable but improvable),
  "unclear" (ambiguous/misleading), or "missing" (empty or absent).
- param_issues: list specific problems with the input schema — ambiguous names, missing
  units/formats, undocumented required fields, redundant params. Empty list if none.
- error_quality: judge from observed failures — "good", "unclear", "missing", or "n/a"
  if the tool never failed.
- suggestions: concrete, actionable edits to the tool definition. Empty list if none.

Then give an overall_score (0-5) for the server's tool ergonomics, overall_notes
(2-3 sentences), and top_recommendations (the highest-impact fixes across all tools).
Judge strictly from the evidence above. Output only the JSON object."""


def _build_critique_prompt(run: dict[str, Any], tools: list[dict[str, Any]]) -> str:
    from . import prompts

    scenario = run.get("scenario", {})
    return prompts.render(
        "critique",
        CRITIQUE_TEMPLATE,
        goal=scenario.get("goal", "(unspecified)"),
        tools=_format_tools(tools),
        transcript=_format_transcript(run.get("transcript", [])),
    )


def critique_prompt(run: dict[str, Any], inspect: dict[str, Any] | None = None) -> str:
    """The exact critique prompt for a run dict (from read_run), for UI preview."""
    return _build_critique_prompt(run, collect_exercised_tools(run, inspect))


def critique_run(
    run_dir: Path, backend: CodexBackend, inspect: dict[str, Any] | None = None
) -> dict[str, Any]:
    run = read_run(run_dir)
    tools = collect_exercised_tools(run, inspect)
    judged = backend.generate_json(_build_critique_prompt(run, tools), _CRITIQUE_SCHEMA)
    return {
        "run_dir": str(run_dir),
        "scenario": run["scenario"].get("id", "?"),
        "exercised_tools": [t["name"] for t in tools],
        "critique": judged,
    }


def write_critique_artifacts(critique: dict[str, Any], run_dir: Path) -> tuple[Path, Path]:
    json_path = run_dir / "critique.json"
    md_path = run_dir / "critique.md"
    json_path.write_text(json.dumps(critique, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_critique_md(critique), encoding="utf-8")
    return json_path, md_path


def render_critique_md(critique: dict[str, Any]) -> str:
    judged = critique.get("critique", {})
    score = judged.get("overall_score", "?")
    lines = [
        f"# Tool Usability Critique  ({critique.get('scenario', '?')})",
        "",
        f"- Overall tool-ergonomics score: **{score}/5**",
        f"- Exercised tools: {', '.join(f'`{t}`' for t in critique.get('exercised_tools', [])) or 'none'}",
        "",
        f"{judged.get('overall_notes', '')}",
        "",
    ]
    recs = judged.get("top_recommendations", [])
    if recs:
        lines += ["## Top recommendations", ""]
        lines += [f"- {r}" for r in recs]
        lines.append("")

    lines += ["## Per-tool findings", ""]
    for tool in judged.get("tools", []):
        lines.append(f"### `{tool.get('name', '?')}`")
        lines.append(
            f"- name clarity: {tool.get('name_clarity', '?')}/5 | "
            f"description: {tool.get('description_quality', '?')} | "
            f"errors: {tool.get('error_quality', '?')}"
        )
        issues = tool.get("param_issues", [])
        if issues:
            lines.append("- parameter issues:")
            lines += [f"  - {i}" for i in issues]
        suggestions = tool.get("suggestions", [])
        if suggestions:
            lines.append("- suggestions:")
            lines += [f"  - {s}" for s in suggestions]
        lines.append("")
    return "\n".join(lines)
