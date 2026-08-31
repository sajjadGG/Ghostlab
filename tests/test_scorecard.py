"""Tests for the dataset scorecard aggregation + rendering (no codex, no disk)."""
from __future__ import annotations

import unittest

from rehearsal.scorecard import (
    aggregate,
    aggregate_attempts,
    build_benchmark_scorecard,
    load_attempts,
    render_benchmark_scorecard_md,
    render_scorecard_md,
)

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


def attempt(
    task_id: str,
    source_id: str,
    *,
    agent: str = "a1",
    seed: int = 0,
    score: float | None = 1.0,
    status: str = "scored",
    passed: bool | None = None,
    tokens: int = 100,
    wall_time_ms: int = 1000,
    components: list | None = None,
    valid: bool | None = None,
) -> dict:
    report = {
        "schema_version": "retro-score-report-v1",
        "task_id": task_id,
        "status": status,
        "score_total": score,
        "pass_threshold": 0.8,
        "unscored_weight": 0.0,
        "valid": status == "scored" if valid is None else valid,
        "components": components or [],
    }
    record = {
        "schema_version": "retro-benchmark-attempt-v1",
        "attempt_id": f"{task_id}-{agent}-{seed}",
        "task_id": task_id,
        "source_id": source_id,
        "agent_id": agent,
        "seed": seed,
        "status": status,
        "score": score,
        "tokens": {"input": tokens, "output": 0, "cached": 0},
        "wall_time_ms": wall_time_ms,
        "report": report,
    }
    if passed is not None:
        record["passed"] = passed
    return record


class BenchmarkAggregationTest(unittest.TestCase):
    """Section 16: source-normalized macro-average, with invalid runs excluded."""

    def test_sources_are_weighted_equally_regardless_of_task_count(self) -> None:
        # r1 yielded three tasks, r2 one. Without source normalization r1 would
        # carry three quarters of the benchmark.
        attempts = [
            attempt("t1", "r1", score=0.0),
            attempt("t2", "r1", score=0.0),
            attempt("t3", "r1", score=0.0),
            attempt("t4", "r2", score=1.0),
        ]
        agent = aggregate_attempts(attempts)["agents"]["a1"]
        self.assertEqual(agent["benchmark_score"], 0.5)
        self.assertEqual(agent["per_source"]["r1"]["tasks"], 3)
        self.assertEqual(agent["per_source"]["r2"]["score"], 1.0)

    def test_seeds_are_averaged_within_a_task_and_reported_as_spread(self) -> None:
        attempts = [
            attempt("t1", "r1", seed=0, score=1.0),
            attempt("t1", "r1", seed=1, score=0.0),
        ]
        agent = aggregate_attempts(attempts)["agents"]["a1"]
        self.assertEqual(agent["per_task"]["t1"]["score"], 0.5)
        self.assertEqual(agent["per_task"]["t1"]["seeds"], 2)
        self.assertEqual(agent["per_task"]["t1"]["seed_std"], 0.5)
        self.assertEqual(agent["benchmark_score"], 0.5)

    def test_scorer_and_harness_failures_never_become_zeros(self) -> None:
        attempts = [
            attempt("t1", "r1", score=1.0),
            attempt("t2", "r2", score=None, status="scorer_error"),
            attempt("t3", "r3", score=None, status="scorer_timeout"),
            attempt("t4", "r4", score=None, status="judge_unavailable"),
            attempt("t5", "r5", score=None, status="invalid_candidate_artifact"),
        ]
        agent = aggregate_attempts(attempts)["agents"]["a1"]
        self.assertEqual(agent["benchmark_score"], 1.0)
        self.assertEqual(agent["coverage"], {
            "requested": 5, "scored": 1, "invalid": 4, "ratio": 0.2,
        })
        self.assertEqual(
            agent["errors"],
            {
                "scorer_error": 1, "scorer_timeout": 1,
                "judge_unavailable": 1, "invalid_candidate_artifact": 1,
            },
        )

    def test_a_report_marked_invalid_is_excluded_and_accounted(self) -> None:
        attempts = [
            attempt("t1", "r1", score=0.9, valid=False),
            attempt("t2", "r2", score=0.5),
        ]
        agent = aggregate_attempts(attempts)["agents"]["a1"]
        self.assertEqual(agent["benchmark_score"], 0.5)
        self.assertEqual(agent["errors"], {"invalid_unscored_weight": 1})
        self.assertEqual(agent["coverage"]["scored"], 1)

    def test_strict_report_with_excess_unscored_weight_is_excluded(self) -> None:
        row = attempt("t1", "r1", score=0.7)
        row["report"].pop("valid", None)
        row["report"]["components"] = [
            {
                "id": "measured",
                "value": 1.0,
                "weight": 0.7,
                "hard_gate": False,
                "gate_passed": None,
                "evidence": [],
            },
            {
                "id": "missing",
                "value": None,
                "weight": 0.3,
                "hard_gate": False,
                "gate_passed": None,
                "evidence": [],
            },
        ]

        scorecard = aggregate_attempts([row])

        agent = scorecard["agents"]["a1"]
        self.assertEqual(agent["coverage"]["scored"], 0)
        self.assertEqual(agent["coverage"]["invalid"], 1)
        self.assertEqual(agent["errors"], {"invalid_unscored_weight": 1})

    def test_pass_rate_and_component_means_come_from_valid_attempts_only(self) -> None:
        attempts = [
            attempt(
                "t1", "r1", score=1.0, passed=True,
                components=[{"id": "behavior", "value": 1.0}],
            ),
            attempt(
                "t2", "r2", score=0.4, passed=False,
                components=[{"id": "behavior", "value": 0.4}],
            ),
            attempt("t3", "r3", score=None, status="scorer_error"),
        ]
        agent = aggregate_attempts(attempts)["agents"]["a1"]
        self.assertEqual(agent["pass_rate"], 0.5)
        self.assertEqual(agent["per_component"]["behavior"],
                         {"mean": 0.7, "observations": 2})
        self.assertEqual(agent["usage"]["tokens_per_scored_attempt"], 100.0)

    def test_report_pass_threshold_is_used_when_attempt_omits_passed(self) -> None:
        row = attempt("t1", "r1", score=0.75)
        row["report"]["pass_threshold"] = 0.7

        agent = aggregate_attempts([row])["agents"]["a1"]

        self.assertEqual(agent["pass_rate"], 1.0)

    def test_budgeted_score_zeroes_over_budget_attempts(self) -> None:
        attempts = [
            attempt("t1", "r1", score=1.0, tokens=50, wall_time_ms=100),
            attempt("t2", "r2", score=1.0, tokens=5000, wall_time_ms=90_000),
        ]
        agent = aggregate_attempts(
            attempts, token_budgets=[1000], wall_time_budgets_ms=[1000]
        )["agents"]["a1"]
        self.assertEqual(agent["benchmark_score"], 1.0)
        self.assertEqual(
            agent["budgeted_scores"],
            [
                {"budget": "tokens", "limit": 1000.0, "benchmark_score": 0.5},
                {"budget": "wall_time_ms", "limit": 1000.0, "benchmark_score": 0.5},
            ],
        )

    def test_agents_are_aggregated_independently(self) -> None:
        attempts = [
            attempt("t1", "r1", agent="a1", score=1.0),
            attempt("t1", "r1", agent="a2", score=0.0),
        ]
        agents = aggregate_attempts(attempts)["agents"]
        self.assertEqual(agents["a1"]["benchmark_score"], 1.0)
        self.assertEqual(agents["a2"]["benchmark_score"], 0.0)

    def test_report_is_resolved_from_disk_and_rendered(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "score-report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "retro-score-report-v1",
                        "task_id": "t1",
                        "status": "scored",
                        "score_total": 0.75,
                        "passed": False,
                        "valid": True,
                        "pass_threshold": 0.8,
                        "unscored_weight": 0.0,
                        "components": [{"id": "behavior", "value": 0.75}],
                    }
                ),
                encoding="utf-8",
            )
            record = {
                "attempt_id": "x", "task_id": "t1", "source_id": "r1",
                "agent_id": "a1", "seed": 0, "status": "scored", "score": 0.75,
                "score_report": "score-report.json",
            }
            (base / "attempt.json").write_text(json.dumps(record), encoding="utf-8")
            attempts = load_attempts([base])
            scorecard = build_benchmark_scorecard(attempts, base)

        agent = scorecard["agents"]["a1"]
        self.assertEqual(agent["benchmark_score"], 0.75)
        self.assertEqual(agent["per_component"]["behavior"]["mean"], 0.75)
        self.assertEqual(agent["pass_rate"], 0.0)
        md = render_benchmark_scorecard_md(scorecard)
        self.assertIn("# Benchmark scorecard", md)
        self.assertIn("0.750", md)
        self.assertIn("Valid coverage: 1/1", md)


if __name__ == "__main__":
    unittest.main()
