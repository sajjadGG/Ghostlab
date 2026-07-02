"""`ghostlab review` — readiness report over discover + plan + test artifacts.

Closes the loop the roadmap calls Phase A6: after `discover` judges the
contract and `test` executes the plan, `review` answers the release question —
*is this MCP ready, and if not, what do I fix first?* Everything here is
deterministic aggregation over structured artifacts (the model-judged
`evaluate`/`critique` path stays separate and feeds in once runs carry
verdicts).

Three outputs:

- **gates** — the spec's `review.gates` evaluated against evidence, each with
  a pass/fail/not-evaluated status and the reason.
- **failure clusters** — failed/error case results grouped by category
  (validation, tool-runtime, ui-render, transport, host-compat, security) and
  by tool, so ten failures with one root cause read as one problem.
- **repairs** — prioritized, actionable recommendations mapped from contract
  and sampling finding kinds ("fix `inputSchema.required`" beats "3 errors").

Verdict semantics: ``not-ready`` (a gate failed), ``needs-work`` (gates pass
but there are error findings, failures, coverage gaps, or whole suites nothing
executed), ``ready`` (none of the above).
"""
from __future__ import annotations

from typing import Any, Optional

from .types import utc_now

VERDICTS = ("ready", "needs-work", "not-ready")

# finding kind -> (priority 1=highest, repair advice). Formatted with the
# finding's `in`/`message` when rendered.
_REPAIR_CATALOG: dict[str, tuple[int, str]] = {
    "required_param_undefined": (1, "Fix inputSchema: every name in `required` must exist in `properties`."),
    "ui_alias_mismatch": (1, "Point _meta.ui.resourceUri and openai/outputTemplate at the same ui:// resource."),
    "dangling_ui_resource": (1, "Expose the referenced ui:// resource via resources/list or fix the URI."),
    "sample_call_failed": (1, "The tool crashes on minimal valid arguments — fix the handler before anything else."),
    "reset_failed": (1, "Repair the setup.reset hook; without it, test state pollutes subsequent runs."),
    "missing_input_schema": (2, "Declare an inputSchema; hosts cannot construct arguments without one."),
    "missing_tool_description": (2, "Write a description: models select tools by it."),
    "sample_tool_error": (2, "The tool returns isError for minimal valid arguments — loosen validation or fix defaults."),
    "missing_structured_content": (2, "Return structuredContent matching the declared outputSchema."),
    "ui_tool_no_model_content": (2, "Return text content or structuredContent so the host model can narrate the widget."),
    "undeclared_destructive": (2, "Add annotations.destructiveHint so hosts can gate the call behind approval."),
    "credential_in_arguments": (2, "Move secrets out of tool arguments (env/config); arguments land in transcripts."),
    "missing_tool_reference": (2, "Descriptions/instructions reference a tool that is not exposed — fix the prose or ship the tool."),
    "top_level_combinator": (3, "Replace the top-level oneOf/anyOf/allOf with a single flat object schema."),
    "host_unfriendly_schema": (3, "Inline $ref/$defs and avoid conditional schemas; several hosts flatten schemas poorly."),
    "openai_alias_only": (3, "Add the standard _meta.ui.resourceUri; non-ChatGPT hosts ignore the OpenAI alias."),
    "weak_tool_description": (3, "Expand the description: intent, constraints, when-to-use."),
    "undocumented_enum": (3, "Document how to choose between the enum values."),
    "generic_tool_name": (3, "Rename to something domain-specific; generic names collide across servers."),
    "large_input_surface": (3, "Group related parameters into objects or split the tool."),
    "undocumented_param": (4, "Add parameter descriptions, starting with required ones."),
    "untyped_param": (4, "Add a type/enum to the parameter schema."),
    "unknown_ui_visibility": (4, "Use a known _meta.ui.visibility value."),
    "unconventional_tool_name": (4, "Prefer snake_case tool names."),
    "unclassifiable_mutation": (4, "Add readOnlyHint/destructiveHint annotations or clearer verbs."),
}

_FAILURE_CATEGORIES: dict[str, str] = {
    "smoke": "tool-runtime",
    "edge": "input-validation",
    "error-recovery": "tool-runtime",
    "apps": "ui-render",
    "security": "security",
    "host-compat": "host-compatibility",
    "semantic": "task-completion",
    "regression": "regression",
}


def _classify_failure(entry: dict[str, Any]) -> str:
    if entry.get("status") == "error":
        return "transport-protocol"
    # A UI case is a render problem wherever it appears (smoke includes a
    # first-widget render); otherwise the suite decides.
    if entry.get("kind") == "ui":
        return "ui-render"
    return _FAILURE_CATEGORIES.get(str(entry.get("suite", "")), "other")


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def _evaluate_gates(
    gates: dict[str, Any],
    contract: Optional[dict[str, Any]],
    results: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    evaluated: list[dict[str, Any]] = []

    def add(gate: str, status: str, detail: str) -> None:
        evaluated.append({"gate": gate, "status": status, "detail": detail})

    min_rate = gates.get("min_pass_rate")
    if isinstance(min_rate, (int, float)):
        if not results:
            add("min_pass_rate", "not-evaluated", "no test results; run `ghostlab test`")
        elif not results.get("executed"):
            add("min_pass_rate", "not-evaluated", "no cases executed")
        else:
            rate = results.get("pass_rate") or 0.0
            status = "pass" if rate >= float(min_rate) else "fail"
            add("min_pass_rate", status, f"pass rate {rate:.0%} vs required {float(min_rate):.0%}")

    if gates.get("no_tool_schema_errors"):
        if contract is None:
            add("no_tool_schema_errors", "not-evaluated", "no contract; run `ghostlab discover`")
        else:
            errors = [
                finding for finding in contract.get("findings", [])
                if finding.get("severity") == "error"
            ]
            status = "fail" if errors else "pass"
            add(
                "no_tool_schema_errors", status,
                f"{len(errors)} error-severity contract finding(s)" if errors
                else "no error-severity contract findings",
            )

    if gates.get("no_ui_console_errors"):
        ui_failures = [
            entry for entry in (results or {}).get("results", [])
            if entry.get("suite") == "apps" and entry.get("status") in ("fail", "error")
        ]
        executed_ui = [
            entry for entry in (results or {}).get("results", [])
            if entry.get("suite") == "apps" and entry.get("status") != "skip"
        ]
        if not executed_ui:
            add("no_ui_console_errors", "not-evaluated", "no apps cases executed")
        else:
            status = "fail" if ui_failures else "pass"
            add("no_ui_console_errors", status,
                f"{len(ui_failures)} of {len(executed_ui)} apps case(s) failed"
                if ui_failures else f"{len(executed_ui)} apps case(s) clean")

    if gates.get("no_high_security_findings"):
        security_failures = [
            entry for entry in (results or {}).get("results", [])
            if entry.get("suite") == "security" and entry.get("status") in ("fail", "error")
        ]
        executed_security = [
            entry for entry in (results or {}).get("results", [])
            if entry.get("suite") == "security" and entry.get("status") != "skip"
        ]
        if not executed_security:
            add("no_high_security_findings", "not-evaluated",
                "no security cases executed yet (conversational seeds need scenarios)")
        else:
            status = "fail" if security_failures else "pass"
            add("no_high_security_findings", status,
                f"{len(security_failures)} security failure(s)"
                if security_failures else f"{len(executed_security)} security case(s) clean")
    return evaluated


# --------------------------------------------------------------------------- #
# Failures + repairs
# --------------------------------------------------------------------------- #
def _cluster_failures(results: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in (results or {}).get("results", []):
        if entry.get("status") not in ("fail", "error"):
            continue
        category = _classify_failure(entry)
        # Cluster by category + a coarse signature of the detail so repeats of
        # one root cause group together across cases.
        signature = " ".join(str(entry.get("detail", "")).split()[:6])
        key = (category, signature)
        cluster = clusters.setdefault(
            key, {"category": category, "signature": signature, "cases": [], "hosts": set()}
        )
        cluster["cases"].append(entry["case"])
        cluster["hosts"].add(entry.get("host", "?"))
    ordered = sorted(clusters.values(), key=lambda c: (-len(c["cases"]), c["category"]))
    for cluster in ordered:
        cluster["hosts"] = sorted(cluster["hosts"])
        cluster["count"] = len(cluster["cases"])
    return ordered


def _repairs(contract: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    repairs: dict[tuple[int, str], dict[str, Any]] = {}
    for finding in (contract or {}).get("findings", []):
        kind = str(finding.get("kind", ""))
        catalog = _REPAIR_CATALOG.get(kind)
        if catalog is None:
            continue
        priority, advice = catalog
        entry = repairs.setdefault(
            (priority, kind),
            {"priority": priority, "kind": kind, "advice": advice, "where": []},
        )
        where = str(finding.get("in", "?"))
        if where not in entry["where"]:
            entry["where"].append(where)
    return [repairs[key] for key in sorted(repairs)]


def _coverage_notes(plan: Optional[dict[str, Any]], results: Optional[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    for gap in (plan or {}).get("coverage", {}).get("gaps", []):
        notes.append(str(gap))
    if results:
        executed_suites = {
            entry["suite"] for entry in results.get("results", [])
            if entry.get("status") != "skip"
        }
        planned_suites = {
            suite for suite, info in (plan or {}).get("suites", {}).items()
            if info.get("cases")
        }
        for suite in sorted(planned_suites - executed_suites):
            notes.append(f"suite '{suite}' has planned cases but nothing executed")
    return notes


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def build_readiness(
    spec_id: str,
    gates_config: dict[str, Any],
    *,
    contract: Optional[dict[str, Any]] = None,
    plan: Optional[dict[str, Any]] = None,
    results: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    gates = _evaluate_gates(gates_config or {}, contract, results)
    failures = _cluster_failures(results)
    repairs = _repairs(contract)
    coverage_notes = _coverage_notes(plan, results)

    gate_failed = any(gate["status"] == "fail" for gate in gates)
    error_findings = sum(
        1 for finding in (contract or {}).get("findings", [])
        if finding.get("severity") == "error"
    )
    if gate_failed:
        verdict = "not-ready"
    elif failures or error_findings or coverage_notes or any(
        gate["status"] == "not-evaluated" for gate in gates
    ):
        verdict = "needs-work"
    else:
        verdict = "ready"

    return {
        "id": spec_id,
        "generated_at": utc_now(),
        "verdict": verdict,
        "gates": gates,
        "failures": failures,
        "repairs": repairs,
        "coverage_notes": coverage_notes,
        "evidence": {
            "contract": bool(contract),
            "plan": bool(plan),
            "results": bool(results),
            "pass_rate": (results or {}).get("pass_rate"),
            "executed": (results or {}).get("executed", 0),
            "contract_findings": len((contract or {}).get("findings", [])),
        },
    }


def render_readiness_md(readiness: dict[str, Any]) -> str:
    evidence = readiness["evidence"]
    rate = evidence.get("pass_rate")
    lines = [
        f"# Readiness: {readiness['id']} — **{readiness['verdict'].upper()}**",
        "",
        f"- Executed cases: {evidence.get('executed', 0)} "
        f"(pass rate: {'n/a' if rate is None else f'{rate:.0%}'})",
        f"- Contract findings: {evidence.get('contract_findings', 0)}",
        "",
        "## Gates",
        "",
        "| gate | status | detail |",
        "|---|---|---|",
    ]
    for gate in readiness["gates"]:
        lines.append(f"| {gate['gate']} | {gate['status']} | {gate['detail']} |")

    lines += ["", "## Failure clusters", ""]
    if not readiness["failures"]:
        lines.append("None.")
    for cluster in readiness["failures"]:
        cases = ", ".join(f"`{case}`" for case in cluster["cases"][:5])
        more = f" (+{cluster['count'] - 5} more)" if cluster["count"] > 5 else ""
        lines.append(
            f"- **{cluster['category']}** ×{cluster['count']} on "
            f"{'/'.join(cluster['hosts'])}: {cluster['signature']} — {cases}{more}"
        )

    lines += ["", "## Recommended repairs (highest priority first)", ""]
    if not readiness["repairs"]:
        lines.append("None.")
    for repair in readiness["repairs"]:
        where = ", ".join(repair["where"][:6])
        more = f" (+{len(repair['where']) - 6} more)" if len(repair["where"]) > 6 else ""
        lines.append(
            f"- **P{repair['priority']} {repair['kind']}** — {repair['advice']}  \n"
            f"  at: {where}{more}"
        )

    if readiness["coverage_notes"]:
        lines += ["", "## Coverage notes", ""]
        lines += [f"- {note}" for note in readiness["coverage_notes"]]
    return "\n".join(lines) + "\n"
