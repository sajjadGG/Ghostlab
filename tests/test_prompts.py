from __future__ import annotations

import unittest

from rehearsal.config import PersonaConfig, ScenarioConfig
from rehearsal.prompts import build_user_emulator_prompt, normalize_user_emulator_message


class UserEmulatorRealismTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = ScenarioConfig(
            id="s", title="Do it", persona="in a hurry", goal="save a note",
            max_turns=3, opening_message="save this", success_criteria=[], failure_signals=[],
        )
        self.persona = PersonaConfig(
            id="p", name="Pat", summary="A hurried beginner",
            traits=["terse", "non-native speaker"], context={},
        )

    def test_prompt_defines_in_persona_permission_contract(self) -> None:
        prompt = build_user_emulator_prompt(
            self.scenario, [], "Can I overwrite the old note?", self.persona,
        )
        self.assertIn("If the assistant asks for permission", prompt)
        self.assertIn("destructive", prompt)
        self.assertIn("impatient: \"yeah, go ahead\"", prompt)

    def test_output_envelope_removes_wrappers_and_caps_length(self) -> None:
        value = normalize_user_emulator_message("USER: " + "word " * 200)
        self.assertLessEqual(len(value), 500)
        self.assertFalse(value.startswith("USER:"))
        self.assertEqual(normalize_user_emulator_message(" REHEARSAL_DONE "), "REHEARSAL_DONE")
