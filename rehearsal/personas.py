"""`rehearsal generate-personas` — propose a reusable user-profile library.

Uses the codex backend to generate personas relevant to an MCP's domain, derived
from its capability profile. Personas are decoupled from scenarios: the same
persona can be paired with many scenarios when building a dataset (#5).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .codex_backend import CodexBackend

_PERSONAS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "personas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "traits": {"type": "array", "items": {"type": "string"}},
                    "context": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["key", "value"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "name", "summary", "traits", "context"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["personas"],
    "additionalProperties": False,
}


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "persona"


# Placeholders: {mcp} {domain_summary} {categories} {n}
PERSONA_GEN_TEMPLATE = """You design a library of realistic user personas for testing an MCP (Model Context Protocol) server.
Each persona will later role-play a user in end-to-end conversations against the server's tools.

MCP: {mcp}
Domain: {domain_summary}
Tool categories: {categories}

Generate exactly {n} DISTINCT personas that represent the realistic range of users for this domain.
Vary their background, expertise/level, goals, constraints, and especially their behavior, so that
together they stress the server differently (e.g. a careful beginner, an impatient power user, a
confused or skeptical user, a non-native speaker, an edge-case user).

For each persona provide:
- id: short kebab-case identifier.
- name: a short human label (not necessarily a real name).
- summary: 1-3 sentences describing who they are and what they want.
- traits: 2-5 behavioral traits that shape how they talk (e.g. "terse", "impatient", "asks many questions", "easily confused", "adversarial", "non-native speaker").
- context: domain-relevant attributes as key/value string pairs (e.g. native_language: Persian, target_exam: IELTS, level: B1, daily_minutes: 30, tech_savviness: low). Choose keys that matter for THIS domain.

Output only the JSON object with a `personas` array."""


def _build_prompt(profile: dict[str, Any], n: int) -> str:
    from . import prompts

    categories = ", ".join(
        c.get("label", c.get("key", "")) for c in profile.get("categories", [])
    )
    return prompts.render(
        "persona_gen",
        PERSONA_GEN_TEMPLATE,
        mcp=profile.get("mcp", "?"),
        domain_summary=profile.get("domain_summary", ""),
        categories=categories,
        n=n,
    )


def _to_persona_dict(raw: dict[str, Any], index: int) -> dict[str, Any]:
    persona_id = _slug(str(raw.get("id") or raw.get("name") or f"persona-{index}"))
    context_pairs = raw.get("context", [])
    context: dict[str, str] = {}
    if isinstance(context_pairs, list):
        for pair in context_pairs:
            if isinstance(pair, dict) and "key" in pair:
                context[str(pair["key"])] = str(pair.get("value", ""))
    elif isinstance(context_pairs, dict):  # tolerate object form
        context = {str(k): str(v) for k, v in context_pairs.items()}
    return {
        "id": persona_id,
        "name": str(raw.get("name", persona_id)),
        "summary": str(raw.get("summary", "")),
        "traits": [str(t) for t in raw.get("traits", [])],
        "context": context,
    }


def persona_prompt(profile: dict[str, Any], n: int) -> str:
    """The exact prompt that `generate_personas` sends to codex."""
    return _build_prompt(profile, n)


def generate_personas(
    profile: dict[str, Any], backend: CodexBackend, n: int
) -> list[dict[str, Any]]:
    result = backend.generate_json(_build_prompt(profile, n), _PERSONAS_SCHEMA)
    raw_personas = result.get("personas", []) if isinstance(result, dict) else []

    personas: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_personas, start=1):
        persona = _to_persona_dict(raw, index)
        base_id = persona["id"]
        suffix = 2
        while persona["id"] in seen_ids:
            persona["id"] = f"{base_id}-{suffix}"
            suffix += 1
        seen_ids.add(persona["id"])
        personas.append(persona)
    return personas


def write_personas(personas: list[dict[str, Any]], out_dir: Path, prefix: str = "") -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for persona in personas:
        name = f"{prefix}{persona['id']}.json" if prefix else f"{persona['id']}.json"
        path = out_dir / name
        path.write_text(json.dumps(persona, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
