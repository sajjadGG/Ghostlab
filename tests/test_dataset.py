"""Tests for deterministic dataset assembly (no codex needed)."""
from __future__ import annotations

import unittest

from rehearsal.dataset import assemble_cases

PERSONAS = [{"id": "alice"}, {"id": "bob"}]
SCENARIOS_BY_PERSONA = {
    "alice": [
        {"id": "alice--happy", "intent": "happy_path", "exercises": ["t1"], "max_turns": 4},
        {"id": "alice--edge", "intent": "edge_case", "exercises": [], "max_turns": 5},
    ],
    "bob": [
        {"id": "bob--adv", "intent": "adversarial", "exercises": ["t2"], "max_turns": 3},
    ],
}


class AssembleCasesTest(unittest.TestCase):
    def test_one_case_per_persona_scenario(self) -> None:
        cases = assemble_cases(PERSONAS, SCENARIOS_BY_PERSONA, seed=0)
        self.assertEqual(len(cases), 3)
        case_ids = {c["id"] for c in cases}
        # Case id is the (already persona-namespaced) scenario id.
        self.assertEqual(case_ids, {"alice--happy", "alice--edge", "bob--adv"})

    def test_case_links_persona_and_scenario(self) -> None:
        cases = assemble_cases(PERSONAS, SCENARIOS_BY_PERSONA, seed=0)
        by_scenario = {c["scenario"]: c for c in cases}
        self.assertEqual(by_scenario["bob--adv"]["persona"], "bob")
        self.assertEqual(by_scenario["bob--adv"]["intent"], "adversarial")
        self.assertEqual(by_scenario["alice--happy"]["exercises"], ["t1"])

    def test_seed_is_deterministic(self) -> None:
        a = assemble_cases(PERSONAS, SCENARIOS_BY_PERSONA, seed=42)
        b = assemble_cases(PERSONAS, SCENARIOS_BY_PERSONA, seed=42)
        self.assertEqual([c["id"] for c in a], [c["id"] for c in b])

    def test_missing_scenarios_for_persona_is_skipped(self) -> None:
        cases = assemble_cases([{"id": "ghost"}], {}, seed=0)
        self.assertEqual(cases, [])


if __name__ == "__main__":
    unittest.main()
