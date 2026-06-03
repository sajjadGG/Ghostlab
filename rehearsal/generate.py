"""`rehearsal generate-scenarios` — propose grounded scenarios from a profile.

Consumes a capability profile (`capabilities.json`) and uses the codex backend
to propose use-case scenarios the MCP actually supports. Each scenario matches
`ScenarioConfig` and declares which tools it should exercise, so coverage can be
measured later. All tool references are filtered to real tool names so scenarios
never depend on hallucinated or non-exposed tools.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .codex_backend import CodexBackend

INTENTS = ("happy_path", "edge_case", "adversarial")

_SCENARIOS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "intent": {"type": "string", "enum": list(INTENTS)},
                    "persona": {"type": "string"},
                    "goal": {"type": "string"},
                    "max_turns": {"type": "integer"},
                    "opening_message": {"type": "string"},
                    "success_criteria": {"type": "array", "items": {"type": "string"}},
                    "failure_signals": {"type": "array", "items": {"type": "string"}},
                    "exercises": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "id",
                    "title",
                    "intent",
                    "persona",
                    "goal",
                    "max_turns",
                    "opening_message",
                    "success_criteria",
                    "failure_signals",
                    "exercises",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["scenarios"],
    "additionalProperties": False,
}


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "scenario"


def profile_tool_names(profile: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tool_list in profile.get("taxonomy", {}).values():
        names.update(tool_list)
    return names


def _profile_digest(profile: dict[str, Any]) -> str:
    lines = [
        f"MCP: {profile.get('mcp', '?')}",
        f"Domain: {profile.get('domain_summary', '')}",
        "",
        "Tool categories:",
    ]
    cat_desc = {c.get("key"): c for c in profile.get("categories", [])}
    for family, names in profile.get("taxonomy", {}).items():
        cat = cat_desc.get(family, {})
        label = cat.get("label", family)
        lines.append(f"- {label} ({family}): {', '.join(names)}")
    lines += ["", "Known workflows:"]
    for wf in profile.get("workflows", []):
        lines.append(f"- {wf.get('name', '?')}: {' -> '.join(wf.get('steps', []))}")
    surfaces = profile.get("state_surfaces", {})
    lines += [
        "",
        f"Read-only tools: {', '.join(surfaces.get('read', [])) or 'none'}",
        f"Mutating tools: {', '.join(surfaces.get('write', [])) or 'none'}",
    ]
    missing = profile.get("gaps", {}).get("missing_referenced_tools", [])
    if missing:
        lines.append(f"Non-exposed tools mentioned in docs (do NOT rely on these): {', '.join(missing)}")
    return "\n".join(lines)


def _build_prompt(profile: dict[str, Any], n: int) -> str:
    return f"""You design end-to-end test scenarios for an MCP (Model Context Protocol) server.
In each test, one agent role-plays a user and another agent uses the MCP tools to help them.

Capability profile:

{_profile_digest(profile)}

Generate exactly {n} diverse, realistic scenarios that this MCP can support. Spread them across intents:
- happy_path: a primary, well-supported use case.
- edge_case: ambiguous requests, missing prerequisites, or conflicting/stale state.
- adversarial: the user asks for something the MCP can't do, or pushes on the known gaps/failure modes.

For each scenario provide:
- id: short kebab-case identifier.
- title: one short line.
- intent: one of happy_path | edge_case | adversarial.
- persona: 1-3 sentences describing the user (background, constraints, attitude).
- goal: what the user wants to achieve in this conversation.
- max_turns: an integer between 3 and 6.
- opening_message: the user's first message, in their voice.
- success_criteria: 2-4 observable things the assistant should do (reference real behavior/tools).
- failure_signals: 2-4 things that would indicate a bug or bad behavior to probe for.
- exercises: the tool names (from the lists above) this scenario should cause the assistant to use. Use ONLY real tool names; never the non-exposed ones.

Output only the JSON object with a `scenarios` array."""


def _to_scenario_dict(raw: dict[str, Any], tool_names: set[str], index: int) -> dict[str, Any]:
    """Normalize one generated scenario into a ScenarioConfig-shaped dict."""
    exercises = [t for t in raw.get("exercises", []) if t in tool_names]
    scenario_id = _slug(str(raw.get("id") or raw.get("title") or f"scenario-{index}"))
    max_turns = raw.get("max_turns", 4)
    try:
        max_turns = max(2, min(8, int(max_turns)))
    except (TypeError, ValueError):
        max_turns = 4
    return {
        "id": scenario_id,
        "title": str(raw.get("title", scenario_id)),
        "intent": str(raw.get("intent", "")),
        "persona": str(raw.get("persona", "")),
        "goal": str(raw.get("goal", "")),
        "max_turns": max_turns,
        "opening_message": str(raw.get("opening_message", "")),
        "success_criteria": [str(s) for s in raw.get("success_criteria", [])],
        "failure_signals": [str(s) for s in raw.get("failure_signals", [])],
        "exercises": exercises,
    }


def generate_scenarios(
    profile: dict[str, Any], backend: CodexBackend, n: int
) -> list[dict[str, Any]]:
    tool_names = profile_tool_names(profile)
    result = backend.generate_json(_build_prompt(profile, n), _SCENARIOS_SCHEMA)
    raw_scenarios = result.get("scenarios", []) if isinstance(result, dict) else []

    scenarios: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_scenarios, start=1):
        scenario = _to_scenario_dict(raw, tool_names, index)
        # De-duplicate ids.
        base_id = scenario["id"]
        suffix = 2
        while scenario["id"] in seen_ids:
            scenario["id"] = f"{base_id}-{suffix}"
            suffix += 1
        seen_ids.add(scenario["id"])
        scenarios.append(scenario)
    return scenarios


def write_scenarios(scenarios: list[dict[str, Any]], out_dir: Path, prefix: str = "") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for scenario in scenarios:
        name = f"{prefix}{scenario['id']}.json" if prefix else f"{scenario['id']}.json"
        path = out_dir / name
        path.write_text(json.dumps(scenario, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
