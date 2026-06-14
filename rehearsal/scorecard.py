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
            f"  (" + ", ".join(f"{k}={v}" for k, v in sorted(totals["by_verdict"].items())) + ")"
            if totals["by_verdict"]
            else ""
        ),
        f"- Avg tool coverage: {_fmt_pct(scorecard.get('avg_coverage'))}",
        f"- Avg tool-ergonomics score: "
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
