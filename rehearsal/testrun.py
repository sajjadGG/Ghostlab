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
from typing import Any, Optional

from .hosts import CaseResult, HostAdapter
from .setup_runtime import environment_fingerprint
from .types import utc_now


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
) -> dict[str, Any]:
    cases = select_cases(plan, suites=suites, approved_only=approved_only)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: list[CaseResult] = []
    try:
        for case in cases:
            capable, skip_reason = _hosts_for_case(case, hosts)
            if not capable:
                results.append(CaseResult(
                    case_id=case["id"], suite=case.get("suite", "?"),
                    host="-", status="skip", detail=skip_reason,
                ))
                continue
            for host in capable:
                try:
                    results.append(host.execute(case, out_dir))
                except Exception as exc:  # noqa: BLE001 — isolate case crashes
                    results.append(CaseResult(
                        case_id=case["id"], suite=case.get("suite", "?"),
                        host=host.id, status="error", detail=str(exc),
                    ))
    finally:
        for host in hosts:
            try:
                host.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    totals = {"pass": 0, "fail": 0, "skip": 0, "error": 0}
    for result in results:
        totals[result.status] = totals.get(result.status, 0) + 1
    executed = totals["pass"] + totals["fail"] + totals["error"]
    pass_rate = round(totals["pass"] / executed, 4) if executed else None

    return {
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
        f"skip {totals['skip']})",
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
