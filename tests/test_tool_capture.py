"""Tests for tool-call capture and host-noise redaction (no codex needed)."""
from __future__ import annotations

import unittest

from rehearsal.runners import redact_host_noise
from rehearsal.tool_capture import (
    efficiency_metrics,
    parse_codex_output,
    parse_tool_calls,
    summarize_tool_calls,
)

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


# Real codex `exec --json` lines (thread/turn/item schema) observed live.
CODEX_JSONL = "\n".join(
    [
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"turn.started"}',
        '{"type":"item.started","item":{"id":"item_0","type":"mcp_tool_call","server":"cortex","tool":"student_get_status","arguments":{},"status":"in_progress"}}',
        '{"type":"item.completed","item":{"id":"item_0","type":"mcp_tool_call","server":"cortex","tool":"student_get_status","arguments":{},"result":{"content":[{"type":"text","text":"ok"}]},"error":null,"status":"completed"}}',
        '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"You are set up for IELTS."}}',
        '{"type":"turn.completed"}',
        "stray non-json log line",
    ]
)


class CodexJsonParseTest(unittest.TestCase):
    def test_extracts_message_and_rich_tool_call(self) -> None:
        parsed = parse_codex_output(CODEX_JSONL)
        self.assertEqual(parsed["message"], "You are set up for IELTS.")
        self.assertEqual(len(parsed["tool_calls"]), 1)
        call = parsed["tool_calls"][0]
        self.assertEqual(call["tool"], "student_get_status")
        self.assertEqual(call["status"], "completed")
        self.assertEqual(call["arguments"], {})
        self.assertIn("content", call["result"])

    def test_only_counts_completed_items(self) -> None:
        # The in_progress item.started must not create a duplicate record.
        parsed = parse_codex_output(CODEX_JSONL)
        self.assertEqual(len(parsed["tool_calls"]), 1)

    def test_failed_tool_call_status(self) -> None:
        jsonl = '{"type":"item.completed","item":{"type":"mcp_tool_call","server":"s","tool":"t","arguments":{},"result":null,"error":{"message":"boom"},"status":"failed"}}'
        call = parse_codex_output(jsonl)["tool_calls"][0]
        self.assertEqual(call["status"], "failed")
        self.assertEqual(call["error"], {"message": "boom"})

    def test_summary_works_on_codex_calls(self) -> None:
        summary = summarize_tool_calls(parse_codex_output(CODEX_JSONL)["tool_calls"])
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["by_status"], {"completed": 1})


class EfficiencyMetricsTest(unittest.TestCase):
    def test_counts_and_uniques(self) -> None:
        calls = [
            {"server": "s", "tool": "a", "arguments": {"x": 1}},
            {"server": "s", "tool": "b", "arguments": {"x": 1}},
            {"server": "s", "tool": "a", "arguments": {"x": 2}},
        ]
        eff = efficiency_metrics(calls)
        self.assertEqual(eff["total_calls"], 3)
        self.assertEqual(eff["unique_tools"], 2)
        self.assertEqual(eff["redundant_calls"], 0)
        self.assertEqual(eff["max_calls_to_one_tool"], 2)

    def test_redundant_identical_args(self) -> None:
        calls = [
            {"server": "s", "tool": "a", "arguments": {"x": 1, "y": 2}},
            {"server": "s", "tool": "a", "arguments": {"y": 2, "x": 1}},  # same, key order differs
            {"server": "s", "tool": "a", "arguments": {"x": 9}},
        ]
        eff = efficiency_metrics(calls)
        self.assertEqual(eff["redundant_calls"], 1)

    def test_missing_args_not_counted_redundant(self) -> None:
        # Text-parser calls carry no arguments; repeats can't be judged.
        calls = [{"server": "s", "tool": "a"}, {"server": "s", "tool": "a"}]
        eff = efficiency_metrics(calls)
        self.assertEqual(eff["redundant_calls"], 0)

    def test_duration_aggregated_when_present(self) -> None:
        calls = [
            {"server": "s", "tool": "a", "arguments": {}, "duration_ms": 100},
            {"server": "s", "tool": "b", "arguments": {}, "duration_ms": 300},
        ]
        eff = efficiency_metrics(calls)
        self.assertEqual(eff["total_duration_ms"], 400)
        self.assertEqual(eff["avg_duration_ms"], 200)

    def test_no_duration_keys_when_absent(self) -> None:
        eff = efficiency_metrics([{"server": "s", "tool": "a", "arguments": {}}])
        self.assertNotIn("avg_duration_ms", eff)

    def test_empty(self) -> None:
        eff = efficiency_metrics([])
        self.assertEqual(eff["total_calls"], 0)
        self.assertEqual(eff["max_calls_to_one_tool"], 0)

    def test_parse_captures_duration_when_provided(self) -> None:
        jsonl = (
            '{"type":"item.completed","item":{"type":"mcp_tool_call","server":"s",'
            '"tool":"t","arguments":{},"status":"completed","duration_ms":42}}'
        )
        call = parse_codex_output(jsonl)["tool_calls"][0]
        self.assertEqual(call["duration_ms"], 42)


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
