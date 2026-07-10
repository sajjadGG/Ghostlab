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
    if profile.get("target_type") in ("skill", "agent") and profile.get("instructions"):
        lines += ["", "Capability instructions:", str(profile["instructions"])[:12000]]
    return "\n".join(lines)


def _persona_block(persona: dict[str, Any]) -> str:
    traits = ", ".join(persona.get("traits", []))
    context = "; ".join(f"{k}: {v}" for k, v in persona.get("context", {}).items())
    lines = [
        f"These scenarios are for ONE specific, fixed user persona:",
        f"- name: {persona.get('name', persona.get('id', '?'))}",
        f"- summary: {persona.get('summary', '')}",
    ]
    if traits:
        lines.append(f"- traits: {traits}")
    if context:
        lines.append(f"- context: {context}")
    lines.append(
        "The persona's IDENTITY is fixed and supplied separately at run time. Do NOT restate "
        "their identity in the `persona` field. Instead, put only a short SITUATIONAL note there "
        "(what is happening for them in THIS conversation, e.g. 'in a hurry on mobile', 'just "
        "failed a mock test'). Tailor goals, opening messages, and difficulty to this persona."
    )
    return "\n".join(lines)


# Placeholders: {profile_digest} {persona_section} {n} {persona_field_help}
SCENARIO_GEN_TEMPLATE = """You design end-to-end test scenarios for an agent capability (an MCP server or a skill).
In each test, one agent role-plays a user and another agent uses the target capability to help them.

Capability profile:

{profile_digest}{persona_section}

Generate exactly {n} diverse, realistic scenarios that this capability can support. Spread them across intents:
- happy_path: a primary, well-supported use case.
- edge_case: ambiguous requests, missing prerequisites, or conflicting/stale state.
- adversarial: the user asks for something the MCP can't do, or pushes on the known gaps/failure modes.

For each scenario provide:
- id: short kebab-case identifier.
- title: one short line.
- intent: one of happy_path | edge_case | adversarial.
- persona: {persona_field_help}
- goal: what the user wants to achieve in this conversation.
- max_turns: an integer between 3 and 6.
- opening_message: the user's first message, in their voice.
- success_criteria: 2-4 observable things the assistant should do (reference real behavior/tools).
- failure_signals: 2-4 things that would indicate a bug or bad behavior to probe for.
- exercises: for MCP targets, the real tool names this should exercise; for skill targets with no tools, use an empty array.

Output only the JSON object with a `scenarios` array."""


def _build_prompt(profile: dict[str, Any], n: int, persona: dict[str, Any] | None = None) -> str:
    from . import prompts

    persona_section = f"\n\n{_persona_block(persona)}\n" if persona else ""
    persona_field_help = (
        "a SHORT situational note for this conversation (not the persona's identity)."
        if persona
        else "1-3 sentences describing the user (background, constraints, attitude)."
    )
    return prompts.render(
        "scenario_gen",
        SCENARIO_GEN_TEMPLATE,
        profile_digest=_profile_digest(profile),
        persona_section=persona_section,
        n=n,
        persona_field_help=persona_field_help,
    )


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


def scenario_prompt(profile: dict[str, Any], n: int, persona: dict[str, Any] | None = None) -> str:
    """The exact prompt that `generate_scenarios` sends to codex."""
    return _build_prompt(profile, n, persona)


def generate_scenarios(
    profile: dict[str, Any],
    backend: CodexBackend,
    n: int,
    persona: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    tool_names = profile_tool_names(profile)
    result = backend.generate_json(_build_prompt(profile, n, persona), _SCENARIOS_SCHEMA)
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
