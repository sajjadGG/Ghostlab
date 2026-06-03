"""`rehearsal compare` — diff two dataset runs for regressions.

Compares two `run-dataset` result sets case-by-case and highlights what changed:
newly failing cases (regressions) first, then newly passing (fixes), then other
verdict/status/turn changes. Works on either a verdict (`pass`/`partial`/`fail`)
when both runs were evaluated, or on the run status otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Higher is better. Verdict takes precedence; status is the fallback signal.
_VERDICT_RANK = {"pass": 2, "partial": 1, "fail": 0}
_STATUS_RANK = {"completed": 1}  # everything else (failed/timeout/max_turns) = 0


def load_results(path: Path) -> dict[str, Any]:
    """Load a results.json from a summary dir or a direct file path."""
    if path.is_dir():
        path = path / "results.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _rank(row: dict[str, Any]) -> tuple[int, str]:
    """Return (score, basis). Prefer verdict; fall back to status."""
    if "verdict" in row:
        return _VERDICT_RANK.get(row["verdict"], 0), "verdict"
    return _STATUS_RANK.get(row.get("status"), 0), "status"


def _outcome(row: dict[str, Any]) -> str:
    return row.get("verdict") or row.get("status", "?")


def diff_results(base: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base_rows = {r["case"]: r for r in base.get("results", [])}
    cand_rows = {r["case"]: r for r in candidate.get("results", [])}

    regressions: list[dict[str, Any]] = []
    fixes: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    unchanged = 0
    added: list[str] = []
    removed: list[str] = []

    for case in sorted(cand_rows):
        if case not in base_rows:
            added.append(case)
            continue
        b, c = base_rows[case], cand_rows[case]
        b_score = _rank(b)[0]
        c_score = _rank(c)[0]
        entry = {
            "case": case,
            "base": _outcome(b),
            "candidate": _outcome(c),
            "base_turns": b.get("turns"),
            "candidate_turns": c.get("turns"),
        }
        if c_score < b_score:
            regressions.append(entry)
        elif c_score > b_score:
            fixes.append(entry)
        elif _outcome(b) != _outcome(c):
            changed.append(entry)
        else:
            unchanged += 1

    for case in sorted(base_rows):
        if case not in cand_rows:
            removed.append(case)

    return {
        "base": base.get("dataset"),
        "candidate": candidate.get("dataset"),
        "base_version": base.get("version"),
        "candidate_version": candidate.get("version"),
        "regressions": regressions,
        "fixes": fixes,
        "changed": changed,
        "unchanged": unchanged,
        "added": added,
        "removed": removed,
    }


def render_comparison_md(diff: dict[str, Any]) -> str:
    def table(rows: list[dict[str, Any]]) -> list[str]:
        out = ["| case | base | candidate | turns Δ |", "| --- | --- | --- | --- |"]
        for r in rows:
            bt, ct = r.get("base_turns"), r.get("candidate_turns")
            delta = "" if bt is None or ct is None else f"{bt}→{ct}"
            out.append(f"| {r['case']} | {r['base']} | {r['candidate']} | {delta} |")
        return out

    lines = [
        "# Dataset Comparison",
        "",
        f"- Base: `{diff['base']}` (version `{diff.get('base_version', '?')}`)",
        f"- Candidate: `{diff['candidate']}` (version `{diff.get('candidate_version', '?')}`)",
        f"- Regressions: {len(diff['regressions'])} | Fixes: {len(diff['fixes'])} | "
        f"Other changes: {len(diff['changed'])} | Unchanged: {diff['unchanged']}",
        "",
    ]
    if diff["regressions"]:
        lines += ["## ⚠️ Regressions", ""] + table(diff["regressions"]) + [""]
    if diff["fixes"]:
        lines += ["## ✅ Fixes", ""] + table(diff["fixes"]) + [""]
    if diff["changed"]:
        lines += ["## Other changes", ""] + table(diff["changed"]) + [""]
    if diff["added"]:
        lines += ["## Added cases", "", ", ".join(diff["added"]), ""]
    if diff["removed"]:
        lines += ["## Removed cases", "", ", ".join(diff["removed"]), ""]
    return "\n".join(lines)
