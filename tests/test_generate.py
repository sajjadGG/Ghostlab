"""Tests for scenario normalization/grounding (no codex needed)."""
from __future__ import annotations

import unittest

from rehearsal.generate import _to_scenario_dict, profile_tool_names

PROFILE = {
    "taxonomy": {
        "memory": ["memory_get", "memory_put"],
        "student": ["student_get_status"],
    }
}


class GroundingTest(unittest.TestCase):
    def test_profile_tool_names(self) -> None:
        self.assertEqual(
            profile_tool_names(PROFILE),
            {"memory_get", "memory_put", "student_get_status"},
        )

    def test_filters_hallucinated_exercises(self) -> None:
        raw = {
            "id": "Some Title!",
            "title": "x",
            "intent": "happy_path",
            "exercises": ["memory_get", "kb_find", "student_get_status", "made_up"],
            "max_turns": 5,
        }
        scenario = _to_scenario_dict(raw, profile_tool_names(PROFILE), 1)
        self.assertEqual(scenario["exercises"], ["memory_get", "student_get_status"])

    def test_slugifies_id(self) -> None:
        raw = {"title": "Daily Practice!!", "exercises": []}
        scenario = _to_scenario_dict(raw, set(), 1)
        self.assertEqual(scenario["id"], "daily-practice")

    def test_clamps_and_defaults_max_turns(self) -> None:
        self.assertEqual(_to_scenario_dict({"max_turns": 99}, set(), 1)["max_turns"], 8)
        self.assertEqual(_to_scenario_dict({"max_turns": 1}, set(), 1)["max_turns"], 2)
        self.assertEqual(_to_scenario_dict({"max_turns": "x"}, set(), 1)["max_turns"], 4)

    def test_id_falls_back_to_index(self) -> None:
        scenario = _to_scenario_dict({"exercises": []}, set(), 7)
        self.assertEqual(scenario["id"], "scenario-7")

    def test_skill_scenarios_keep_workflow_exercises(self) -> None:
        raw = {"id": "build", "exercises": ["scripts/init-artifact.sh", "bundle"]}
        scenario = _to_scenario_dict(raw, set(), 1, keep_ungrounded_exercises=True)
        self.assertEqual(scenario["exercises"], ["scripts/init-artifact.sh", "bundle"])


class GenerateCountTest(unittest.TestCase):
    def test_caps_overproduced_scenarios_to_n(self) -> None:
        from rehearsal.generate import generate_scenarios

        class Fake:
            def generate_json(self, prompt, schema):
                return {"scenarios": [
                    {"id": "a", "title": "a", "intent": "happy_path", "persona": "",
                     "goal": "", "max_turns": 3, "opening_message": "hi",
                     "success_criteria": [], "failure_signals": [], "exercises": []},
                    {"id": "b", "title": "b", "intent": "edge_case", "persona": "",
                     "goal": "", "max_turns": 3, "opening_message": "hi",
                     "success_criteria": [], "failure_signals": [], "exercises": []},
                    {"id": "c", "title": "c", "intent": "adversarial", "persona": "",
                     "goal": "", "max_turns": 3, "opening_message": "hi",
                     "success_criteria": [], "failure_signals": [], "exercises": []},
                ]}

        self.assertEqual(len(generate_scenarios({}, Fake(), 1)), 1)


if __name__ == "__main__":
    unittest.main()
