"""Tests for evaluation deterministic checks and verdict combination (no codex)."""
from __future__ import annotations

import unittest

from rehearsal.evaluate import combine_verdict, deterministic_checks

SCENARIO = {"exercises": ["memory_get", "student_get_status", "views_create_writing_task"]}
TOOL_CALLS = [
    {"server": "cortex", "tool": "memory_get", "status": "completed"},
    {"server": "cortex", "tool": "student_get_status", "status": "failed"},
]


class DeterministicTest(unittest.TestCase):
    def test_coverage_and_missing(self) -> None:
        det = deterministic_checks(SCENARIO, TOOL_CALLS)
        self.assertEqual(det["coverage"], "2/3")
        self.assertEqual(det["exercises_missing"], ["views_create_writing_task"])

    def test_failed_calls_collected(self) -> None:
        det = deterministic_checks(SCENARIO, TOOL_CALLS)
        self.assertEqual(det["tool_failures"], ["cortex/student_get_status"])

    def test_no_tool_calls_flag(self) -> None:
        det = deterministic_checks(SCENARIO, [])
        self.assertTrue(det["no_tool_calls"])
        self.assertEqual(det["coverage"], "0/3")


class CombineVerdictTest(unittest.TestCase):
    def test_pass_passes_through(self) -> None:
        judge = {"verdict": "pass", "failure_signals": [], "hallucinated_tools": []}
        verdict, gates = combine_verdict("completed", {}, judge)
        self.assertEqual(verdict, "pass")
        self.assertEqual(gates, [])

    def test_triggered_failure_signal_forces_fail(self) -> None:
        judge = {
            "verdict": "partial",
            "failure_signals": [{"index": 0, "triggered": True}],
            "hallucinated_tools": [],
        }
        verdict, gates = combine_verdict("completed", {}, judge)
        self.assertEqual(verdict, "fail")
        self.assertTrue(any("failure_signals_triggered" in g for g in gates))

    def test_hallucinated_tools_force_fail(self) -> None:
        judge = {"verdict": "pass", "failure_signals": [], "hallucinated_tools": ["kb_find"]}
        verdict, gates = combine_verdict("completed", {}, judge)
        self.assertEqual(verdict, "fail")
        self.assertTrue(any("hallucinated_tools" in g for g in gates))

    def test_run_crash_forces_fail(self) -> None:
        judge = {"verdict": "partial", "failure_signals": [], "hallucinated_tools": []}
        verdict, gates = combine_verdict("aut_failed", {}, judge)
        self.assertEqual(verdict, "fail")
        self.assertIn("run_status:aut_failed", gates)

    def test_invalid_verdict_defaults_fail(self) -> None:
        judge = {"verdict": "weird", "failure_signals": [], "hallucinated_tools": []}
        verdict, _ = combine_verdict("completed", {}, judge)
        self.assertEqual(verdict, "fail")


if __name__ == "__main__":
    unittest.main()
