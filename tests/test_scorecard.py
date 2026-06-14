"""Tests for the dataset scorecard aggregation + rendering (no codex, no disk)."""
from __future__ import annotations

import unittest

from rehearsal.scorecard import aggregate, render_scorecard_md

CASES = [
    {
        "case": "c1",
        "intent": "happy",
        "status": "completed",
        "verdict": {
            "verdict": "pass",
            "gates": [],
            "deterministic": {"coverage": "2/2"},
            "judge": {"hallucinated_tools": []},
        },
        "critique": {
            "critique": {
                "overall_score": 4,
                "top_recommendations": ["Document id format"],
                "tools": [{"name": "a", "name_clarity": 4, "description_quality": "good"}],
            }
        },
        "tool_calls": [
            {"server": "s", "tool": "a", "status": "completed", "arguments": {"x": 1}},
            {"server": "s", "tool": "b", "status": "completed", "arguments": {}},
        ],
    },
    {
        "case": "c2",
        "intent": "edge",
        "status": "completed",
        "verdict": {
            "verdict": "fail",
            "gates": ["golden_mismatch", "hallucinated_tools:kb_find"],
            "deterministic": {"coverage": "1/2"},
            "judge": {"hallucinated_tools": ["kb_find"]},
        },
        "critique": {
            "critique": {
                "overall_score": 2,
                "top_recommendations": ["Document id format", "Add units"],
                "tools": [{"name": "a", "name_clarity": 1, "description_quality": "unclear"}],
            }
        },
        "tool_calls": [
            {"server": "s", "tool": "a", "status": "failed", "arguments": {"x": 1}},
            {"server": "s", "tool": "a", "status": "completed", "arguments": {"x": 1}},
        ],
    },
]


class AggregateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sc = aggregate(CASES)

    def test_totals_and_pass_rate(self) -> None:
        self.assertEqual(self.sc["totals"]["cases"], 2)
        self.assertEqual(self.sc["totals"]["by_verdict"], {"pass": 1, "fail": 1})
        self.assertEqual(self.sc["pass_rate"], 0.5)

    def test_avg_coverage(self) -> None:
        # (1.0 + 0.5) / 2
        self.assertEqual(self.sc["avg_coverage"], 0.75)

    def test_avg_tool_ergonomics(self) -> None:
        self.assertEqual(self.sc["avg_tool_ergonomics"], 3.0)

    def test_hallucination_and_golden(self) -> None:
        self.assertEqual(self.sc["hallucinated_tools"], {"kb_find": 1})
        self.assertEqual(self.sc["golden_mismatches"], 1)

    def test_per_tool_failure_rates_sorted(self) -> None:
        per_tool = {t["tool"]: t for t in self.sc["per_tool"]}
        # tool a: 3 calls (1 fail in c2), tool b: 1 call
        self.assertEqual(per_tool["s/a"]["calls"], 3)
        self.assertEqual(per_tool["s/a"]["failures"], 1)
        self.assertAlmostEqual(per_tool["s/a"]["failure_rate"], 0.333, places=3)
        # highest failure rate sorts first
        self.assertEqual(self.sc["per_tool"][0]["tool"], "s/a")

    def test_efficiency_rollup(self) -> None:
        # c2 calls a twice with identical args -> 1 redundant
        self.assertEqual(self.sc["efficiency"]["redundant_calls"], 1)
        self.assertEqual(self.sc["efficiency"]["total_calls"], 4)
        self.assertEqual(self.sc["efficiency"]["avg_calls_per_case"], 2.0)

    def test_weak_tools_and_recommendations(self) -> None:
        self.assertEqual(self.sc["weak_tools"], {"a": 1})  # only c2's a is weak
        self.assertEqual(self.sc["recommendations"]["Document id format"], 2)

    def test_no_verdicts_yields_none_pass_rate(self) -> None:
        sc = aggregate([{"case": "x", "status": "completed", "tool_calls": []}])
        self.assertIsNone(sc["pass_rate"])
        self.assertIsNone(sc["avg_coverage"])


class RenderTest(unittest.TestCase):
    def test_renders_headline_and_tables(self) -> None:
        md = render_scorecard_md({"dataset": "d", "missing_runs": [], **aggregate(CASES)})
        self.assertIn("# MCP Scorecard: d", md)
        self.assertIn("Pass rate: 50%", md)
        self.assertIn("`kb_find` × 1", md)
        self.assertIn("Tool reliability", md)
        self.assertIn("(2×) Document id format", md)


if __name__ == "__main__":
    unittest.main()
