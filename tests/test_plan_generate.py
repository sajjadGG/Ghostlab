"""Unit tests for real (mocked-Codex) persona/scenario generation into plan cases."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rehearsal.plan_generate import (
    generate_conversational_dataset,
    generated_dataset_to_cases,
    load_generated_cases,
    write_conversational_dataset,
)


class GenerateConversationalDatasetTest(unittest.TestCase):
    @patch("rehearsal.plan_generate.build_dataset")
    @patch("rehearsal.plan_generate.build_capability_profile")
    def test_profiles_then_builds_dataset(self, profile_mock, dataset_mock) -> None:
        profile_mock.return_value = {"mcp": "fake-notes@0.0.1"}
        dataset_mock.return_value = {"manifest": {"cases": []}, "personas": [], "scenarios": []}
        events = []

        result = generate_conversational_dataset(
            {"target_id": "fake-notes"}, MagicMock(), spec_id="fake-notes",
            n_personas=2, scenarios_per_persona=2, progress=events.append,
        )
        profile_mock.assert_called_once()
        dataset_mock.assert_called_once()
        self.assertEqual(dataset_mock.call_args.kwargs["n_personas"], 2)
        self.assertEqual(dataset_mock.call_args.kwargs["scenarios_per_persona"], 2)
        self.assertEqual(result["profile"]["mcp"], "fake-notes@0.0.1")
        self.assertTrue(any(e["phase"] == "profile" for e in events))


class DatasetToCasesTest(unittest.TestCase):
    def _manifest(self) -> dict:
        return {
            "cases": [
                {"id": "alice--happy", "persona": "alice", "scenario": "alice--happy",
                 "intent": "happy_path", "exercises": ["notes_list"]},
                {"id": "alice--edge", "persona": "alice", "scenario": "alice--edge",
                 "intent": "edge_case", "exercises": []},
                {"id": "bob--adv", "persona": "bob", "scenario": "bob--adv",
                 "intent": "adversarial", "exercises": ["notes_delete"]},
            ]
        }

    def test_routes_by_intent_to_suite(self) -> None:
        cases = generated_dataset_to_cases(Path("/tmp/ds"), self._manifest())
        by_id = {c["id"]: c for c in cases}
        self.assertEqual(by_id["semantic-gen-alice--happy"]["suite"], "semantic")
        self.assertEqual(by_id["semantic-gen-alice--edge"]["suite"], "semantic")
        self.assertEqual(by_id["security-gen-bob--adv"]["suite"], "security")
        for case in cases:
            self.assertEqual(case["kind"], "conversational")
            self.assertTrue(case["reason"])
            self.assertEqual(case["execution"]["type"], "scenario")
            self.assertTrue(case["execution"]["generated"])

    def test_execution_paths_point_at_dataset_dir(self) -> None:
        cases = generated_dataset_to_cases(Path("/tmp/ds"), self._manifest())
        case = next(c for c in cases if c["id"] == "semantic-gen-alice--happy")
        self.assertEqual(case["execution"]["scenario"], "/tmp/ds/scenarios/alice--happy.json")
        self.assertEqual(case["execution"]["persona"], "/tmp/ds/personas/alice.json")


class WriteAndReloadTest(unittest.TestCase):
    @patch("rehearsal.plan_generate.build_dataset")
    @patch("rehearsal.plan_generate.build_capability_profile")
    def test_write_then_reload_without_codex(self, profile_mock, dataset_mock) -> None:
        profile_mock.return_value = {"mcp": "t"}
        dataset_mock.return_value = {
            "manifest": {"name": "t", "cases": [
                {"id": "alice--happy", "persona": "alice", "scenario": "alice--happy",
                 "intent": "happy_path", "exercises": []},
            ]},
            "personas": [{"id": "alice", "name": "Alice", "summary": "s"}],
            "scenarios": [{"id": "alice--happy", "title": "t", "persona": "alice",
                          "goal": "g", "max_turns": 4, "opening_message": "hi",
                          "intent": "happy_path"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "generated"
            dataset = generate_conversational_dataset(
                {}, MagicMock(), spec_id="t", progress=None,
            )
            write_conversational_dataset(dataset, out_dir)
            self.assertTrue((out_dir / "profile.json").exists())
            self.assertTrue((out_dir / "dataset.json").exists())
            self.assertTrue((out_dir / "personas" / "alice.json").exists())
            self.assertTrue((out_dir / "scenarios" / "alice--happy.json").exists())

            # Reload: zero Codex calls, cases reconstructed from disk.
            cases = load_generated_cases(out_dir)
            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0]["id"], "semantic-gen-alice--happy")

    def test_load_generated_cases_missing_dir_returns_empty(self) -> None:
        self.assertEqual(load_generated_cases(Path("/nonexistent/path")), [])


if __name__ == "__main__":
    unittest.main()
