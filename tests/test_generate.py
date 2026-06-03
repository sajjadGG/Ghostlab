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


if __name__ == "__main__":
    unittest.main()
