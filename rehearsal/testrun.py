"""`ghostlab test` executor: run a test plan across host adapters.

Routes each plan case to the hosts capable of executing it (one result per
(case, host) pair), records a results bundle with host/version fingerprints,
and evaluates the spec's review gates. Cases with `status: rejected` never
run; `--approved-only` narrows to curated cases. Skips are first-class
results with reasons — a suite the current hosts can't execute shows up as
explicit uncovered work, not silence.
"""
from __future__ import annotations

from pathlib import Path
import json
from typing import Any, Callable, Optional

from .hosts import CaseResult, HostAdapter
from .setup_runtime import environment_fingerprint
from .types import utc_now

ProgressFn = Callable[[str], None]


def select_cases(
    plan: dict[str, Any],
    *,
    suites: Optional[list[str]] = None,
    approved_only: bool = False,
) -> list[dict[str, Any]]:
    selected = []
    for case in plan.get("cases", []):
        if case.get("status") == "rejected":
            continue
        if approved_only and case.get("status") != "approved":
            continue
        if suites and case.get("suite") not in suites:
            continue
        selected.append(case)
    return selected


def _hosts_for_case(
    case: dict[str, Any], hosts: list[HostAdapter]
) -> tuple[list[HostAdapter], str]:
    """Hosts that will run this case, or (empty, reason-to-skip)."""
    execution = case.get("execution", {}) or {}
    if execution.get("type") == "host_smoke":
        wanted = str(execution.get("host", ""))
        for host in hosts:
            if host.id == wanted:
                return [host], ""
        return [], f"host '{wanted}' is not configured/selected"
    capable = [host for host in hosts if host.can_execute(case) is None]
    if capable:
        return capable, ""
    reasons = {host.can_execute(case) for host in hosts}
    return [], "; ".join(sorted(reason for reason in reasons if reason)) or "no capable host"


def execute_plan(
    plan: dict[str, Any],
    hosts: list[HostAdapter],
    out_dir: Path,
    *,
    suites: Optional[list[str]] = None,
    approved_only: bool = False,
    progress: ProgressFn = print,
    resume_results: Optional[dict[str, Any]] = None,
    checkpoint_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Run every selected case, printing progress as each one starts/finishes.

    Protocol cases finish in milliseconds so the line-per-case is mostly a
    log; conversational cases can take real wall-clock minutes (a live
    multi-turn LLM conversation), where visible progress is the difference
    between "it's working" and "did it hang".
    """
    cases = select_cases(plan, suites=suites, approved_only=approved_only)
    out_dir.mkdir(parents=True, exist_ok=True)
    total = len(cases)

    prior_entries = list((resume_results or {}).get("results", []))
    selected_ids = {case.get("id") for case in cases}
    selected_hosts = {host.id for host in hosts} | {"-"}
    prior_entries = [
        entry for entry in prior_entries
        if entry.get("case") in selected_ids and entry.get("host") in selected_hosts
    ]
    terminal = {"pass", "fail", "skip"}
    completed_keys = {
        (entry.get("case"), entry.get("host"))
        for entry in prior_entries
        if entry.get("status") in terminal
    }
    results: list[CaseResult] = [
        CaseResult(
            case_id=str(entry.get("case", "?")), suite=str(entry.get("suite", "?")),
            host=str(entry.get("host", "-")), status=str(entry.get("status", "error")),
            kind=str(entry.get("kind", "")), detail=str(entry.get("detail", "")),
            duration_ms=float(entry.get("duration_ms", 0) or 0),
            artifacts=dict(entry.get("artifacts") or {}),
        )
        for entry in prior_entries if entry.get("status") in terminal
    ]

    def checkpoint() -> None:
        if checkpoint_path is None:
            return
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = _result_bundle(plan, hosts, results, suites, approved_only, partial=True)
        checkpoint_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    try:
        for index, case in enumerate(cases, start=1):
            capable, skip_reason = _hosts_for_case(case, hosts)
            label = f"[{index}/{total}] {case['id']} [{case.get('suite', '?')}/{case.get('kind', '?')}]"
            if not capable:
                if (case["id"], "-") in completed_keys:
                    progress(f"{label}: resume skip (already completed)")
                    continue
                progress(f"{label}: skip ({skip_reason})")
                results.append(CaseResult(
                    case_id=case["id"], suite=case.get("suite", "?"),
                    host="-", status="skip", kind=case.get("kind", ""),
                    detail=skip_reason,
                ))
                checkpoint()
                continue
            for host in capable:
                if (case["id"], host.id) in completed_keys:
                    progress(f"{label} on {host.id}: resume skip (already completed)")
                    continue
                progress(f"{label} on {host.id}...")
                try:
                    result = host.execute(case, out_dir)
                    detail = f": {result.detail}" if result.detail else ""
                    progress(f"  -> {result.status}{detail}")
                    results.append(result)
                    checkpoint()
                except Exception as exc:  # noqa: BLE001 — isolate case crashes
                    progress(f"  -> error: {exc}")
                    results.append(CaseResult(
                        case_id=case["id"], suite=case.get("suite", "?"),
                        host=host.id, status="error", kind=case.get("kind", ""),
                        detail=str(exc),
                    ))
                    checkpoint()
    finally:
        for host in hosts:
            try:
                host.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    return _result_bundle(plan, hosts, results, suites, approved_only)


def _result_bundle(
    plan: dict[str, Any], hosts: list[HostAdapter], results: list[CaseResult],
    suites: Optional[list[str]], approved_only: bool, *, partial: bool = False,
) -> dict[str, Any]:
    totals = {"pass": 0, "fail": 0, "skip": 0, "error": 0}
    for result in results:
        totals[result.status] = totals.get(result.status, 0) + 1
    # Harness outages are retryable infrastructure failures, not product
    # failures, and therefore never dilute the target's readiness pass rate.
    executed = totals["pass"] + totals["fail"] + totals["error"]
    pass_rate = round(totals["pass"] / executed, 4) if executed else None

    bundle = {
        "id": plan.get("id", "?"),
        "generated_at": utc_now(),
        "plan_generated_at": plan.get("generated_at", "?"),
        "filters": {"suites": suites or [], "approved_only": approved_only},
        "hosts": [host.version_info() for host in hosts],
        "totals": totals,
        "executed": executed,
        "pass_rate": pass_rate,
        "results": [result.to_json() for result in results],
        "fingerprint": environment_fingerprint(),
    }
    if partial:
        bundle["partial"] = True
    return bundle


def execute_plan_repeated(
    plan: dict[str, Any],
    hosts: list[HostAdapter],
    out_dir: Path,
    *,
    repeat: int = 1,
    suites: Optional[list[str]] = None,
    approved_only: bool = False,
    progress: ProgressFn = print,
    resume_results: Optional[dict[str, Any]] = None,
    checkpoint_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Run the plan ``repeat`` times and aggregate variance (roadmap A7).

    Model-backed hosts are nondeterministic; repeating the same plan is how a
    "failure" separates into *broken* (fails every attempt) vs *flaky* (mixed
    outcomes). With ``repeat=1`` this is exactly :func:`execute_plan`. Hosts
    re-open lazily between attempts, so each attempt gets a fresh session.
    """
    if repeat <= 1:
        return execute_plan(
            plan, hosts, out_dir, suites=suites, approved_only=approved_only, progress=progress,
            resume_results=resume_results, checkpoint_path=checkpoint_path,
        )
    if resume_results:
        raise ValueError("--resume currently supports --repeat 1 only")

    attempts: list[dict[str, Any]] = []
    for attempt in range(1, repeat + 1):
        progress(f"=== attempt {attempt}/{repeat} ===")
        result = execute_plan(
            plan, hosts, out_dir, suites=suites, approved_only=approved_only, progress=progress
        )
        for entry in result["results"]:
            entry["attempt"] = attempt
        attempts.append(result)

    merged: list[dict[str, Any]] = [entry for result in attempts for entry in result["results"]]
    totals = {"pass": 0, "fail": 0, "skip": 0, "error": 0}
    for entry in merged:
        totals[entry["status"]] = totals.get(entry["status"], 0) + 1
    executed = totals["pass"] + totals["fail"] + totals["error"]

    per_case: dict[tuple, dict[str, Any]] = {}
    for entry in merged:
        key = (entry["case"], entry["host"])
        stats = per_case.setdefault(
            key,
            {"case": entry["case"], "host": entry["host"], "suite": entry["suite"],
             "runs": 0, "pass": 0, "fail": 0, "error": 0, "skip": 0,
             "harness_error": 0},
        )
        stats["runs"] += 1
        stats[entry["status"]] += 1
    for stats in per_case.values():
        stats["flaky"] = stats["pass"] > 0 and (stats["fail"] + stats["error"]) > 0
    variance_cases = sorted(per_case.values(), key=lambda s: (not s["flaky"], s["case"]))
    flaky = [f"{stats['case']}@{stats['host']}" for stats in variance_cases if stats["flaky"]]

    aggregate = dict(attempts[-1])
    aggregate.update({
        "generated_at": utc_now(),
        "attempts": repeat,
        "totals": totals,
        "executed": executed,
        "pass_rate": round(totals["pass"] / executed, 4) if executed else None,
        "results": merged,
        "variance": {
            "attempt_pass_rates": [result["pass_rate"] for result in attempts],
            "per_case": variance_cases,
            "flaky_cases": flaky,
        },
    })
    return aggregate


def evaluate_gates(results: dict[str, Any], gates: dict[str, Any]) -> list[str]:
    """Return gate-failure messages (empty = all gates pass)."""
    failures = []
    min_rate = gates.get("min_pass_rate")
    if isinstance(min_rate, (int, float)) and results.get("executed"):
        rate = results.get("pass_rate") or 0.0
        if rate < float(min_rate):
            failures.append(
                f"min_pass_rate: {rate:.0%} < {float(min_rate):.0%} "
                f"({results['totals']['fail']} fail, {results['totals']['error']} error)"
            )
    return failures


def render_results_md(results: dict[str, Any]) -> str:
    totals = results["totals"]
    rate = results.get("pass_rate")
    lines = [
        f"# Test Results: {results.get('id', '?')}",
        "",
        f"- Executed: {results.get('executed', 0)} "
        f"(pass {totals['pass']}, fail {totals['fail']}, error {totals['error']}, "
        f"skip {totals['skip']}, harness error {totals.get('harness_error', 0)})",
        f"- Pass rate: {'n/a' if rate is None else f'{rate:.0%}'}",
        f"- Hosts: {', '.join(host['id'] for host in results.get('hosts', []))}",
        "",
        "| case | suite | host | status | detail |",
        "|---|---|---|---|---|",
    ]
    for entry in results.get("results", []):
        detail = str(entry.get("detail", "")).replace("|", "\\|")[:140]
        lines.append(
            f"| `{entry['case']}` | {entry['suite']} | {entry['host']} "
            f"| {entry['status']} | {detail} |"
        )
    return "\n".join(lines) + "\n"
