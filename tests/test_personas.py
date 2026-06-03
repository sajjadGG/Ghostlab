"""Tests for persona normalization and prompt composition (no codex needed)."""
from __future__ import annotations

import unittest

from rehearsal.config import PersonaConfig, ScenarioConfig
from rehearsal.personas import _to_persona_dict
from rehearsal.prompts import compose_persona


def _scenario(persona: str = "") -> ScenarioConfig:
    return ScenarioConfig(
        id="s",
        title="t",
        persona=persona,
        goal="g",
        max_turns=3,
        success_criteria=[],
        failure_signals=[],
        opening_message="hi",
    )


class PersonaNormalizeTest(unittest.TestCase):
    def test_context_pairs_to_dict(self) -> None:
        raw = {
            "id": "Power User",
            "name": "PU",
            "summary": "s",
            "traits": ["terse"],
            "context": [{"key": "level", "value": "C1"}, {"key": "exam", "value": "IELTS"}],
        }
        persona = _to_persona_dict(raw, 1)
        self.assertEqual(persona["id"], "power-user")
        self.assertEqual(persona["context"], {"level": "C1", "exam": "IELTS"})

    def test_tolerates_object_context(self) -> None:
        raw = {"id": "x", "context": {"level": "B1"}}
        self.assertEqual(_to_persona_dict(raw, 1)["context"], {"level": "B1"})

    def test_id_fallback_to_index(self) -> None:
        self.assertEqual(_to_persona_dict({}, 3)["id"], "persona-3")


class ComposePersonaTest(unittest.TestCase):
    def test_falls_back_to_inline_persona(self) -> None:
        result = compose_persona(_scenario("an inline persona"), None)
        self.assertEqual(result, "an inline persona")

    def test_composes_summary_traits_context(self) -> None:
        persona = PersonaConfig(
            id="p",
            name="P",
            summary="A careful beginner.",
            traits=["polite", "easily confused"],
            context={"level": "A2", "native_language": "Persian"},
        )
        result = compose_persona(_scenario(""), persona)
        self.assertIn("A careful beginner.", result)
        self.assertIn("Behavioral traits: polite, easily confused", result)
        self.assertIn("level: A2", result)
        self.assertIn("native_language: Persian", result)

    def test_scenario_persona_refines_reusable_persona(self) -> None:
        persona = PersonaConfig(id="p", name="P", summary="Base.", traits=[], context={})
        result = compose_persona(_scenario("Today they are in a hurry."), persona)
        self.assertIn("Base.", result)
        self.assertIn("In this scenario specifically: Today they are in a hurry.", result)


if __name__ == "__main__":
    unittest.main()
