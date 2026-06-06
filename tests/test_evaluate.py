"""Tests for evaluation deterministic checks and verdict combination (no codex)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rehearsal.evaluate import combine_verdict, deterministic_checks, evidence_references, read_run

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


class ReadRunTest(unittest.TestCase):
    def test_preserves_tool_calls_inside_assistant_turn(self) -> None:
        events = [
            {
                "type": "run_started",
                "timestamp": "2026-01-01T00:00:00Z",
                "data": {
                    "target": {"id": "mcp"},
                    "scenario": {"id": "s"},
                    "persona": {"id": "p"},
                    "models": {"agent_under_test": "gpt-test", "user_emulator": "gpt-user"},
                },
            },
            {
                "type": "aut_prompt",
                "timestamp": "2026-01-01T00:00:00Z",
                "data": {"turn": 1, "prompt": "agent prompt", "stateful_resume": False},
            },
            {
                "type": "user_message",
                "timestamp": "2026-01-01T00:00:01Z",
                "data": {"turn": 1, "content": "Hello"},
            },
            {
                "type": "aut_result",
                "timestamp": "2026-01-01T00:00:02Z",
                "data": {
                    "turn": 1,
                    "output": "Done",
                    "tool_calls": [{"server": "mcp", "tool": "lookup", "status": "completed"}],
                },
            },
            {
                "type": "run_finished",
                "timestamp": "2026-01-01T00:00:03Z",
                "data": {"status": "completed", "transcript": []},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "events.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events),
                encoding="utf-8",
            )
            run = read_run(run_dir)

        self.assertEqual(run["target"]["id"], "mcp")
        self.assertEqual(run["models"]["agent_under_test"], "gpt-test")
        self.assertEqual(run["prompts"][0]["prompt"], "agent prompt")
        self.assertEqual(run["started_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(run["finished_at"], "2026-01-01T00:00:03Z")
        self.assertEqual([item["role"] for item in run["trace"]], ["user", "assistant"])
        self.assertEqual(run["trace"][1]["tool_calls"][0]["tool"], "lookup")

    def test_evidence_references_trace_turns_and_tools(self) -> None:
        run = {
            "trace": [
                {
                    "role": "assistant",
                    "turn": 2,
                    "content": "I checked the learner profile before updating it.",
                    "tool_calls": [
                        {"server": "cortex", "tool": "student_get_status", "status": "completed"}
                    ],
                }
            ]
        }
        refs = evidence_references(run, "Called student_get_status before updating the learner profile.")
        self.assertIn("assistant turn 2", refs)
        self.assertIn("cortex/student_get_status · turn 2", refs)


if __name__ == "__main__":
    unittest.main()
