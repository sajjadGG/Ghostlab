"""`rehearsal scorecard` — roll a dataset run up into one MCP validation report.

Where `compare` diffs two runs and `evaluate`/`critique` score a single case, the
scorecard answers the headline question for a whole dataset run: *how healthy is
this MCP server?* It aggregates per-case artifacts (verdicts, tool calls, and
critiques when present) into server-level signals — pass rate, per-tool
reliability, hallucination and golden-mismatch counts, efficiency, and the
recurring tool-design recommendations — and writes `scorecard.json` + `.md`.

This mirrors the suite-level summary table in anthropics/claude-cookbooks'
tool_evaluation, but rolled up across personas and scenarios rather than tasks.
The `aggregate` core is pure; `load_cases` reads the artifacts off disk.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .evaluate import read_run
from .tool_capture import efficiency_metrics


def load_summary(path: Path) -> dict[str, Any]:
    """Load a run-dataset results.json from a summary dir or a direct file path."""
    if path.is_dir():
        path = path / "results.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_cases(summary: dict[str, Any], base_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Gather per-case artifacts from each row's run directory.

    Returns (cases, missing) where each case carries its status, verdict and
    critique dicts (when those artifacts exist), and the raw tool calls recovered
    from the run's events. `missing` lists rows whose run directory was not found.
    """
    cases: list[dict[str, Any]] = []
    missing: list[str] = []
    for row in summary.get("results", []):
        run_dir = Path(row.get("run_dir", ""))
        if not run_dir.is_absolute() and not run_dir.exists():
            # Stored relative to the original cwd; try alongside the summary too.
            candidate = base_dir / run_dir
            if candidate.exists():
                run_dir = candidate
        tool_calls: list[dict[str, Any]] = []
        if (run_dir / "events.jsonl").exists():
            tool_calls = read_run(run_dir)["tool_calls"]
        elif not run_dir.exists():
            missing.append(row.get("case", "?"))
        cases.append(
            {
                "case": row.get("case", "?"),
                "intent": row.get("intent", ""),
                "status": row.get("status", "unknown"),
                "verdict": _read_json(run_dir / "verdict.json"),
                "critique": _read_json(run_dir / "critique.json"),
                "tool_calls": tool_calls,
            }
        )
    return cases, missing


def _parse_coverage(coverage: Any) -> float | None:
    if not isinstance(coverage, str) or "/" not in coverage:
        return None
    got, _, total = coverage.partition("/")
    try:
        total_n = float(total)
        return float(got) / total_n if total_n else None
    except ValueError:
        return None


def _increment(counter: dict[str, int], key: str, by: int = 1) -> None:
    counter[key] = counter.get(key, 0) + by


def aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-case artifacts into server-level signals (pure)."""
    by_status: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    tool_stats: dict[str, dict[str, int]] = {}
    hallucinated: dict[str, int] = {}
    recommendations: dict[str, int] = {}
    weak_tools: dict[str, int] = {}
    golden_mismatches = 0
    coverage_ratios: list[float] = []
    critique_scores: list[float] = []
    total_calls = 0
    redundant_calls = 0
    n_verdicts = 0

    for case in cases:
        _increment(by_status, case.get("status", "unknown"))

        verdict = case.get("verdict")
        if verdict:
            n_verdicts += 1
            _increment(by_verdict, verdict.get("verdict", "?"))
            if "golden_mismatch" in verdict.get("gates", []):
                golden_mismatches += 1
            for tool in verdict.get("judge", {}).get("hallucinated_tools", []):
                _increment(hallucinated, tool)
            ratio = _parse_coverage(verdict.get("deterministic", {}).get("coverage"))
            if ratio is not None:
                coverage_ratios.append(ratio)

        for call in case.get("tool_calls", []):
            name = f"{call.get('server', '?')}/{call.get('tool', '?')}"
            stats = tool_stats.setdefault(name, {"calls": 0, "failures": 0})
            stats["calls"] += 1
            if call.get("status") == "failed":
                stats["failures"] += 1

        eff = efficiency_metrics(case.get("tool_calls", []))
        total_calls += eff["total_calls"]
        redundant_calls += eff["redundant_calls"]

        critique = case.get("critique")
        if critique:
            judged = critique.get("critique", critique)
            score = judged.get("overall_score")
            if isinstance(score, (int, float)):
                critique_scores.append(float(score))
            for rec in judged.get("top_recommendations", []):
                _increment(recommendations, rec)
            for tool in judged.get("tools", []):
                weak = tool.get("name_clarity", 5) <= 2 or tool.get("description_quality") in (
                    "unclear",
                    "missing",
                )
                if weak and tool.get("name"):
                    _increment(weak_tools, tool["name"])

    per_tool = [
        {
            "tool": name,
            "calls": stats["calls"],
            "failures": stats["failures"],
            "failure_rate": round(stats["failures"] / stats["calls"], 3) if stats["calls"] else 0.0,
        }
        for name, stats in tool_stats.items()
    ]
    per_tool.sort(key=lambda t: (-t["failure_rate"], -t["calls"], t["tool"]))

    n = len(cases)
    return {
        "totals": {"cases": n, "by_status": by_status, "by_verdict": by_verdict},
        "pass_rate": round(by_verdict.get("pass", 0) / n_verdicts, 3) if n_verdicts else None,
        "avg_coverage": round(sum(coverage_ratios) / len(coverage_ratios), 3)
        if coverage_ratios
        else None,
        "avg_tool_ergonomics": round(sum(critique_scores) / len(critique_scores), 2)
        if critique_scores
        else None,
        "hallucinated_tools": dict(sorted(hallucinated.items(), key=lambda kv: -kv[1])),
        "golden_mismatches": golden_mismatches,
        "efficiency": {
            "total_calls": total_calls,
            "redundant_calls": redundant_calls,
            "avg_calls_per_case": round(total_calls / n, 2) if n else 0.0,
        },
        "per_tool": per_tool,
        "weak_tools": dict(sorted(weak_tools.items(), key=lambda kv: -kv[1])),
        "recommendations": dict(sorted(recommendations.items(), key=lambda kv: -kv[1])),
    }


def build_scorecard(summary: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    cases, missing = load_cases(summary, base_dir)
    return {
        "dataset": summary.get("dataset", "?"),
        "version": summary.get("version"),
        "target": summary.get("target"),
        "missing_runs": missing,
        **aggregate(cases),
    }


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def render_scorecard_md(scorecard: dict[str, Any]) -> str:
    totals = scorecard["totals"]
    eff = scorecard["efficiency"]
    lines = [
        f"# MCP Scorecard: {scorecard.get('dataset', '?')}",
        "",
        f"- Cases: {totals['cases']}",
        f"- Pass rate: {_fmt_pct(scorecard.get('pass_rate'))}"
        + (
            "  (" + ", ".join(f"{k}={v}" for k, v in sorted(totals["by_verdict"].items())) + ")"
            if totals["by_verdict"]
            else ""
        ),
        f"- Avg tool coverage: {_fmt_pct(scorecard.get('avg_coverage'))}",
        "- Avg tool-ergonomics score: "
        + ("n/a" if scorecard.get("avg_tool_ergonomics") is None else f"{scorecard['avg_tool_ergonomics']}/5"),
        f"- Tool calls: {eff['total_calls']} total, {eff['redundant_calls']} redundant, "
        f"{eff['avg_calls_per_case']} avg/case",
        f"- Golden-assertion mismatches: {scorecard.get('golden_mismatches', 0)}",
        "",
    ]

    halluc = scorecard.get("hallucinated_tools", {})
    if halluc:
        lines += ["## Hallucinated tools", ""]
        lines += [f"- `{tool}` × {count}" for tool, count in halluc.items()]
        lines.append("")

    per_tool = scorecard.get("per_tool", [])
    if per_tool:
        lines += [
            "## Tool reliability",
            "",
            "| tool | calls | failures | failure rate |",
            "| --- | --- | --- | --- |",
        ]
        for tool in per_tool:
            lines.append(
                f"| `{tool['tool']}` | {tool['calls']} | {tool['failures']} | "
                f"{tool['failure_rate'] * 100:.0f}% |"
            )
        lines.append("")

    weak = scorecard.get("weak_tools", {})
    if weak:
        lines += ["## Tools flagged for poor design", ""]
        lines += [f"- `{tool}` (flagged in {count} run(s))" for tool, count in weak.items()]
        lines.append("")

    recs = scorecard.get("recommendations", {})
    if recs:
        lines += ["## Recurring recommendations", ""]
        lines += [f"- ({count}×) {rec}" for rec, count in recs.items()]
        lines.append("")

    if scorecard.get("missing_runs"):
        lines += [
            "## Missing runs",
            "",
            "Run directories not found for: " + ", ".join(scorecard["missing_runs"]),
            "",
        ]
    return "\n".join(lines)


def write_scorecard_artifacts(scorecard: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "scorecard.json"
    md_path = out_dir / "scorecard.md"
    json_path.write_text(json.dumps(scorecard, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_scorecard_md(scorecard), encoding="utf-8")
    return json_path, md_path


# --------------------------------------------------------------------------- #
# Benchmark aggregation
# --------------------------------------------------------------------------- #
# Source-normalized aggregation over scored benchmark attempts.
#
# The MCP scorecard above rolls conversational verdicts up into server health.
# This section answers a different question with different arithmetic: how well
# did an agent do on a set of Git-backed benchmark tasks, where several tasks
# may have been extracted from the same rollout.
#
# Two rules do the work:
#
# * a rollout that yielded three tasks does not get three times the weight, so
#   tasks are averaged within their source before sources are averaged;
# * an attempt that never produced a number is never counted as zero. Scorer
#   errors, timeouts, judge outages, and invalid candidate artifacts are
#   reported as coverage loss, because folding them into the mean would let a
#   broken harness look like a bad agent.

BENCHMARK_SCHEMA_VERSION = "ghostlab-benchmark-scorecard-v1"

# Statuses a scorer can report; only the first one carries a number.
SCORED_STATUS = "scored"


def load_attempts(paths: list[Path] | Path) -> list[dict[str, Any]]:
    """Read attempt records from files, directories, or a JSON array file."""
    if isinstance(paths, Path):
        paths = [paths]
    attempts: list[dict[str, Any]] = []
    for path in paths:
        path = Path(path)
        if path.is_dir():
            for candidate in sorted(path.rglob("attempt.json")):
                attempts.append(json.loads(candidate.read_text(encoding="utf-8")))
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(document, list):
            attempts += [item for item in document if isinstance(item, dict)]
        elif isinstance(document, dict):
            attempts.append(document)
    return attempts


def _attach_report(attempt: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Resolve an attempt's score report so component detail is available."""
    if isinstance(attempt.get("report"), dict):
        return attempt
    reference = attempt.get("score_report")
    if not isinstance(reference, str) or not reference:
        return attempt
    path = Path(reference)
    if not path.is_absolute():
        path = base_dir / path
    if not path.is_file():
        return attempt
    try:
        return {**attempt, "report": json.loads(path.read_text(encoding="utf-8"))}
    except (json.JSONDecodeError, OSError):
        return attempt


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _report(attempt: dict[str, Any]) -> dict[str, Any]:
    """The attached score report, or an empty one when it was not resolved."""
    report = attempt.get("report")
    return report if isinstance(report, dict) else {}


def _is_valid(attempt: dict[str, Any]) -> bool:
    report = _report(attempt)
    status = str(attempt.get("status") or report.get("status") or "")
    if status != SCORED_STATUS:
        return False
    if report.get("valid") is False or attempt.get("valid") is False:
        return False
    components = report.get("components")
    if not isinstance(components, list):
        components = attempt.get("components")
    if isinstance(components, list):
        unscored_weight = sum(
            float(component.get("weight"))
            for component in components
            if isinstance(component, dict)
            and component.get("value") is None
            and _numeric(component.get("weight")) is not None
        )
        if unscored_weight > 0.20 + 1e-9:
            return False
    score = _numeric(attempt.get("score"))
    if score is None:
        score = _numeric(report.get("score_total"))
    return score is not None and 0.0 <= score <= 1.0


def _score_of(attempt: dict[str, Any]) -> float:
    report = _report(attempt)
    score = _numeric(attempt.get("score"))
    if score is None:
        score = _numeric(report.get("score_total"))
    return float(score or 0.0)


def _passed(attempt: dict[str, Any]) -> bool:
    report = _report(attempt)
    for holder in (attempt, report):
        if isinstance(holder.get("passed"), bool):
            return bool(holder["passed"])
    threshold = _numeric(report.get("pass_threshold"))
    return threshold is not None and _score_of(attempt) + 1e-9 >= threshold


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return 0.0 if values else None
    mean = sum(values) / len(values)
    return round((sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5, 6)


def _usage(attempt: dict[str, Any]) -> dict[str, float]:
    tokens = dict(attempt.get("tokens") or {})
    total = sum(
        float(tokens.get(key) or 0.0) for key in ("input", "output", "cached")
    )
    return {
        "tokens": total,
        "wall_time_ms": float(attempt.get("wall_time_ms") or 0.0),
        "cost_usd": float(attempt.get("cost_usd") or 0.0),
    }


def _source_normalized(per_task: dict[str, dict[str, Any]]) -> tuple[float | None, dict]:
    """Macro-average task means within each source, then across sources."""
    sources: dict[str, list[float]] = {}
    for entry in per_task.values():
        if entry["score"] is None:
            continue
        sources.setdefault(entry["source_id"], []).append(float(entry["score"]))
    per_source = {
        source_id: {"score": _mean(scores), "tasks": len(scores)}
        for source_id, scores in sorted(sources.items())
    }
    means = [entry["score"] for entry in per_source.values() if entry["score"] is not None]
    return _mean(means), per_source


def aggregate_attempts(
    attempts: list[dict[str, Any]],
    *,
    token_budgets: list[float] | None = None,
    wall_time_budgets_ms: list[float] | None = None,
) -> dict[str, Any]:
    """Aggregate benchmark attempts per agent (pure)."""
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        by_agent.setdefault(str(attempt.get("agent_id") or "unknown"), []).append(attempt)

    agents: dict[str, Any] = {}
    for agent_id, rows in sorted(by_agent.items()):
        valid = [row for row in rows if _is_valid(row)]
        invalid = [row for row in rows if not _is_valid(row)]

        seeds: dict[str, list[float]] = {}
        task_meta: dict[str, dict[str, Any]] = {}
        components: dict[str, list[float]] = {}
        passes: list[bool] = []
        for row in valid:
            task_id = str(row.get("task_id") or "unknown")
            seeds.setdefault(task_id, []).append(_score_of(row))
            task_meta.setdefault(
                task_id,
                {"source_id": str(row.get("source_id") or task_id), "attempts": 0},
            )
            task_meta[task_id]["attempts"] += 1
            passes.append(_passed(row))
            for component in _report(row).get("components", []) or []:
                value = _numeric(component.get("value"))
                if value is not None:
                    components.setdefault(str(component.get("id")), []).append(value)

        per_task = {
            task_id: {
                "score": _mean(values),
                "seed_std": _stdev(values),
                "seeds": len(values),
                "source_id": task_meta[task_id]["source_id"],
            }
            for task_id, values in sorted(seeds.items())
        }
        benchmark_score, per_source = _source_normalized(per_task)

        errors: dict[str, int] = {}
        for row in invalid:
            status = str(row.get("status") or _report(row).get("status") or "unknown")
            if status == SCORED_STATUS:
                status = "invalid_unscored_weight"
            errors[status] = errors.get(status, 0) + 1

        usages = [_usage(row) for row in valid]
        agents[agent_id] = {
            "benchmark_score": benchmark_score,
            "pass_rate": round(sum(1 for value in passes if value) / len(passes), 6)
            if passes
            else None,
            "per_source": per_source,
            "per_task": per_task,
            "per_component": {
                name: {"mean": _mean(values), "observations": len(values)}
                for name, values in sorted(components.items())
            },
            "seed_std_mean": _mean(
                [entry["seed_std"] for entry in per_task.values() if entry["seed_std"] is not None]
            ),
            "coverage": {
                "requested": len(rows),
                "scored": len(valid),
                "invalid": len(invalid),
                "ratio": round(len(valid) / len(rows), 6) if rows else None,
            },
            # Never folded into benchmark_score: a broken scorer is not a bad agent.
            "errors": errors,
            "usage": {
                "tokens_per_scored_attempt": _mean([usage["tokens"] for usage in usages]),
                "wall_time_ms_per_scored_attempt": _mean(
                    [usage["wall_time_ms"] for usage in usages]
                ),
                "cost_usd_per_scored_attempt": _mean([usage["cost_usd"] for usage in usages]),
            },
            "budgeted_scores": _budgeted_scores(
                valid, token_budgets or [], wall_time_budgets_ms or []
            ),
        }
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "attempts": len(attempts),
        "agents": agents,
    }


def _budgeted_scores(
    valid: list[dict[str, Any]],
    token_budgets: list[float],
    wall_time_budgets_ms: list[float],
) -> list[dict[str, Any]]:
    """Score under a fixed budget: over-budget attempts score zero, not excluded.

    Dropping them would reward an agent for spending more, which is exactly the
    behavior the budgeted view exists to expose.
    """
    rows: list[dict[str, Any]] = []
    for kind, key, budgets in (
        ("tokens", "tokens", token_budgets),
        ("wall_time_ms", "wall_time_ms", wall_time_budgets_ms),
    ):
        for budget in budgets:
            per_task: dict[str, dict[str, Any]] = {}
            grouped: dict[str, list[float]] = {}
            for attempt in valid:
                task_id = str(attempt.get("task_id") or "unknown")
                usage = _usage(attempt)[key]
                grouped.setdefault(task_id, []).append(
                    _score_of(attempt) if usage <= float(budget) else 0.0
                )
                per_task.setdefault(
                    task_id,
                    {"source_id": str(attempt.get("source_id") or task_id), "score": None},
                )
            for task_id, values in grouped.items():
                per_task[task_id]["score"] = _mean(values)
            score, _sources = _source_normalized(per_task)
            rows.append({"budget": kind, "limit": float(budget), "benchmark_score": score})
    return rows


def build_benchmark_scorecard(
    attempts: list[dict[str, Any]],
    base_dir: Path,
    *,
    token_budgets: list[float] | None = None,
    wall_time_budgets_ms: list[float] | None = None,
) -> dict[str, Any]:
    resolved = [_attach_report(attempt, base_dir) for attempt in attempts]
    return aggregate_attempts(
        resolved,
        token_budgets=token_budgets,
        wall_time_budgets_ms=wall_time_budgets_ms,
    )


def render_benchmark_scorecard_md(scorecard: dict[str, Any]) -> str:
    lines = ["# Benchmark scorecard", "", f"- Attempts: {scorecard.get('attempts', 0)}", ""]
    for agent_id, agent in scorecard.get("agents", {}).items():
        coverage = agent["coverage"]
        lines += [
            f"## {agent_id}",
            "",
            "- Benchmark score (source-normalized): "
            + ("n/a" if agent["benchmark_score"] is None else f"{agent['benchmark_score']:.3f}"),
            f"- Pass rate: {_fmt_pct(agent['pass_rate'])}",
            f"- Valid coverage: {coverage['scored']}/{coverage['requested']} "
            f"({_fmt_pct(coverage['ratio'])})",
            "- Mean seed std: "
            + ("n/a" if agent["seed_std_mean"] is None else f"{agent['seed_std_mean']:.3f}"),
            "",
        ]
        if agent["errors"]:
            lines += [
                "Excluded from the score (never counted as zero): "
                + ", ".join(f"{name}={count}" for name, count in sorted(agent["errors"].items())),
                "",
            ]
        if agent["per_component"]:
            lines += ["| component | mean | observations |", "| --- | --- | --- |"]
            for name, entry in agent["per_component"].items():
                mean = "n/a" if entry["mean"] is None else f"{entry['mean']:.3f}"
                lines.append(f"| `{name}` | {mean} | {entry['observations']} |")
            lines.append("")
        if agent["per_source"]:
            lines += ["| source | mean | tasks |", "| --- | --- | --- |"]
            for source_id, entry in agent["per_source"].items():
                mean = "n/a" if entry["score"] is None else f"{entry['score']:.3f}"
                lines.append(f"| `{source_id}` | {mean} | {entry['tasks']} |")
            lines.append("")
        for row in agent["budgeted_scores"]:
            score = "n/a" if row["benchmark_score"] is None else f"{row['benchmark_score']:.3f}"
            lines.append(f"- Score at {row['budget']} <= {row['limit']:g}: {score}")
        if agent["budgeted_scores"]:
            lines.append("")
    return "\n".join(lines)


def write_benchmark_scorecard(scorecard: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "benchmark-scorecard.json"
    md_path = out_dir / "benchmark-scorecard.md"
    json_path.write_text(
        json.dumps(scorecard, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_benchmark_scorecard_md(scorecard), encoding="utf-8")
    return json_path, md_path
