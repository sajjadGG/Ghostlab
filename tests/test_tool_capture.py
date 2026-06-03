"""Tests for tool-call capture and host-noise redaction (no codex needed)."""
from __future__ import annotations

import unittest

from rehearsal.runners import redact_host_noise
from rehearsal.tool_capture import parse_tool_calls, summarize_tool_calls

# Real codex stderr shape observed in prior cortex runs.
STDERR = """\
mcp: cortex/memory_get started
mcp: cortex/memory_get (completed)
mcp: cortex/student_complete_onboarding started
mcp: cortex/student_complete_onboarding (failed)
mcp: cortex/student_complete_onboarding started
mcp: cortex/student_complete_onboarding (completed)
mcp: cortex/views_generate_sentence_scramble started
mcp: cortex/views_generate_sentence_scramble (completed)
"""


class ParseToolCallsTest(unittest.TestCase):
    def test_pairs_started_with_end_state(self) -> None:
        calls = parse_tool_calls(STDERR)
        self.assertEqual(len(calls), 4)
        statuses = [(c["tool"], c["status"]) for c in calls]
        self.assertEqual(
            statuses,
            [
                ("memory_get", "completed"),
                ("student_complete_onboarding", "failed"),
                ("student_complete_onboarding", "completed"),
                ("views_generate_sentence_scramble", "completed"),
            ],
        )
        self.assertTrue(all(c["server"] == "cortex" for c in calls))

    def test_unmatched_start_is_unknown(self) -> None:
        calls = parse_tool_calls("mcp: cortex/memory_get started\n")
        self.assertEqual(calls[0]["status"], "unknown")

    def test_scans_multiple_streams(self) -> None:
        calls = parse_tool_calls("mcp: s/a started", "mcp: s/a (completed)")
        self.assertEqual(calls[0]["status"], "completed")

    def test_no_calls_on_plain_text(self) -> None:
        self.assertEqual(parse_tool_calls("just a normal answer"), [])

    def test_summary_counts(self) -> None:
        summary = summarize_tool_calls(parse_tool_calls(STDERR))
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["by_status"], {"completed": 3, "failed": 1})
        self.assertEqual(summary["by_tool"]["cortex/student_complete_onboarding"], 2)


class RedactionTest(unittest.TestCase):
    def test_strips_mcp_and_reconnect_noise(self) -> None:
        text = (
            "Here is your answer.\n"
            "mcp: cortex/memory_get started\n"
            "Reconnecting... 2/5 (unexpected status 403)\n"
            "Second line of the answer."
        )
        cleaned = redact_host_noise(text)
        self.assertIn("Here is your answer.", cleaned)
        self.assertIn("Second line of the answer.", cleaned)
        self.assertNotIn("mcp: cortex/memory_get", cleaned)
        self.assertNotIn("Reconnecting", cleaned)

    def test_keeps_clean_text_unchanged(self) -> None:
        self.assertEqual(redact_host_noise("just an answer"), "just an answer")


if __name__ == "__main__":
    unittest.main()
