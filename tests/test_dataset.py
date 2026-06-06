"""Tests for deterministic dataset assembly (no codex needed)."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from rehearsal.dataset import assemble_cases, build_dataset

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

    @patch("rehearsal.dataset.generate_scenarios")
    @patch("rehearsal.dataset.generate_personas")
    def test_build_dataset_reports_generation_progress(
        self, generate_personas: MagicMock, generate_scenarios: MagicMock
    ) -> None:
        generate_personas.return_value = [{"id": "alice", "name": "Alice"}]
        generate_scenarios.return_value = [{"id": "happy", "intent": "happy_path"}]
        events = []

        dataset = build_dataset(
            {"mcp": "test"},
            MagicMock(),
            n_personas=1,
            scenarios_per_persona=1,
            seed=0,
            name="test",
            progress=events.append,
        )

        self.assertEqual(len(dataset["manifest"]["cases"]), 1)
        self.assertEqual(
            [(event["phase"], event["completed"]) for event in events],
            [("personas", 0), ("personas", 1), ("scenarios", 0), ("scenarios", 1), ("cases", 0), ("cases", 1)],
        )


if __name__ == "__main__":
    unittest.main()
