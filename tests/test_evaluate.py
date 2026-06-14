"""Tests for evaluation deterministic checks and verdict combination (no codex)."""
from __future__ import annotations

import unittest

from rehearsal.evaluate import (
    check_expected_outcome,
    combine_verdict,
    deterministic_checks,
)

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

    def test_golden_mismatch_forces_fail(self) -> None:
        judge = {"verdict": "pass", "failure_signals": [], "hallucinated_tools": []}
        det = {"expected_outcome": {"defined": True, "passed": False}}
        verdict, gates = combine_verdict("completed", det, judge)
        self.assertEqual(verdict, "fail")
        self.assertIn("golden_mismatch", gates)

    def test_golden_pass_does_not_gate(self) -> None:
        judge = {"verdict": "pass", "failure_signals": [], "hallucinated_tools": []}
        det = {"expected_outcome": {"defined": True, "passed": True}}
        verdict, gates = combine_verdict("completed", det, judge)
        self.assertEqual(verdict, "pass")
        self.assertEqual(gates, [])


class ExpectedOutcomeTest(unittest.TestCase):
    TRANSCRIPT = [
        {"role": "user", "content": "what's my band score?"},
        {"role": "assistant", "content": "Your IELTS band score is 7.5 overall."},
    ]
    CALLS = [
        {"server": "cortex", "tool": "student_get_status",
         "status": "completed", "arguments": {"id": "u1", "verbose": True}},
    ]

    def test_undefined_when_no_expectations(self) -> None:
        result = check_expected_outcome({}, self.TRANSCRIPT, self.CALLS)
        self.assertEqual(result, {"defined": False})

    def test_must_include_passes_case_insensitively(self) -> None:
        scenario = {"expected_outcome": {"must_include": ["BAND SCORE", "7.5"]}}
        result = check_expected_outcome(scenario, self.TRANSCRIPT, self.CALLS)
        self.assertTrue(result["passed"])
        self.assertEqual(result["missing_substrings"], [])

    def test_must_include_missing_fails(self) -> None:
        scenario = {"expected_outcome": {"must_include": ["band 9"]}}
        result = check_expected_outcome(scenario, self.TRANSCRIPT, self.CALLS)
        self.assertFalse(result["passed"])
        self.assertEqual(result["missing_substrings"], ["band 9"])

    def test_must_not_include_present_fails(self) -> None:
        scenario = {"expected_outcome": {"must_not_include": ["error"]}}
        transcript = [{"role": "assistant", "content": "An error occurred."}]
        result = check_expected_outcome(scenario, transcript, [])
        self.assertFalse(result["passed"])
        self.assertEqual(result["forbidden_present"], ["error"])

    def test_expected_tool_args_subset_match(self) -> None:
        scenario = {"expected_outcome": {
            "expected_tool_args": [{"tool": "student_get_status", "arguments": {"id": "u1"}}]
        }}
        result = check_expected_outcome(scenario, self.TRANSCRIPT, self.CALLS)
        self.assertTrue(result["passed"])

    def test_expected_tool_args_mismatch_fails(self) -> None:
        scenario = {"expected_outcome": {
            "expected_tool_args": [{"tool": "student_get_status", "arguments": {"id": "WRONG"}}]
        }}
        result = check_expected_outcome(scenario, self.TRANSCRIPT, self.CALLS)
        self.assertFalse(result["passed"])
        self.assertEqual(len(result["tool_arg_mismatches"]), 1)


if __name__ == "__main__":
    unittest.main()
