"""Unit tests for the readiness report (`ghostlab review`)."""
from __future__ import annotations

import unittest

from rehearsal.readiness import build_readiness, render_readiness_md

GATES = {
    "min_pass_rate": 0.9,
    "no_tool_schema_errors": True,
    "no_ui_console_errors": True,
    "no_high_security_findings": True,
}


def _results(entries: list[dict], executed: int | None = None, pass_rate: float | None = None) -> dict:
    if executed is None:
        executed = sum(1 for entry in entries if entry["status"] != "skip")
    if pass_rate is None:
        passed = sum(1 for entry in entries if entry["status"] == "pass")
        pass_rate = passed / executed if executed else None
    return {"executed": executed, "pass_rate": pass_rate, "results": entries}


def _entry(case: str, suite: str, status: str, kind: str = "protocol", detail: str = "") -> dict:
    return {"case": case, "suite": suite, "kind": kind, "host": "direct-mcp",
            "status": status, "detail": detail}


class GatesTest(unittest.TestCase):
    def test_all_gates_pass_yields_ready(self) -> None:
        contract = {"findings": []}
        plan = {"suites": {"smoke": {"cases": 1}}, "coverage": {"gaps": []}}
        results = _results([
            _entry("smoke-discovery", "smoke", "pass"),
            _entry("apps-render-x", "apps", "pass", kind="ui"),
            _entry("security-x", "security", "pass", kind="conversational"),
        ])
        readiness = build_readiness("t", GATES, contract=contract, plan=plan, results=results)
        self.assertEqual(readiness["verdict"], "ready")
        self.assertTrue(all(gate["status"] == "pass" for gate in readiness["gates"]))

    def test_gate_failure_yields_not_ready(self) -> None:
        contract = {"findings": [{"kind": "required_param_undefined", "severity": "error",
                                  "in": "tool:x", "message": "m"}]}
        readiness = build_readiness("t", GATES, contract=contract,
                                    results=_results([_entry("a", "smoke", "pass")]))
        self.assertEqual(readiness["verdict"], "not-ready")
        by_gate = {gate["gate"]: gate["status"] for gate in readiness["gates"]}
        self.assertEqual(by_gate["no_tool_schema_errors"], "fail")

    def test_missing_evidence_is_not_evaluated_and_needs_work(self) -> None:
        readiness = build_readiness("t", GATES)
        self.assertEqual(readiness["verdict"], "needs-work")
        statuses = {gate["status"] for gate in readiness["gates"]}
        self.assertEqual(statuses, {"not-evaluated"})

    def test_low_pass_rate_fails_gate(self) -> None:
        results = _results([
            _entry("a", "smoke", "pass"),
            _entry("b", "smoke", "fail", detail="isError"),
        ])
        readiness = build_readiness("t", {"min_pass_rate": 0.9}, results=results)
        self.assertEqual(readiness["verdict"], "not-ready")


class ClusteringTest(unittest.TestCase):
    def test_same_signature_clusters_across_suites(self) -> None:
        detail = "handshake_completed: widget never initialized the bridge"
        results = _results([
            _entry("smoke-render-first-widget", "smoke", "fail", kind="ui", detail=detail),
            _entry("apps-render-x", "apps", "fail", kind="ui", detail=detail),
            _entry("edge-y", "edge", "fail", detail="accepted invalid input"),
        ])
        readiness = build_readiness("t", {}, results=results)
        clusters = readiness["failures"]
        self.assertEqual(len(clusters), 2)
        top = clusters[0]  # largest first
        self.assertEqual(top["category"], "ui-render")
        self.assertEqual(top["count"], 2)
        self.assertEqual(clusters[1]["category"], "input-validation")

    def test_error_status_classifies_as_transport(self) -> None:
        readiness = build_readiness("t", {}, results=_results([
            _entry("a", "smoke", "error", detail="connection refused"),
        ]))
        self.assertEqual(readiness["failures"][0]["category"], "transport-protocol")


class RepairsTest(unittest.TestCase):
    def test_repairs_are_prioritized_and_deduplicated(self) -> None:
        contract = {"findings": [
            {"kind": "undocumented_param", "severity": "info", "in": "tool:a#x", "message": "m"},
            {"kind": "required_param_undefined", "severity": "error", "in": "tool:b", "message": "m"},
            {"kind": "required_param_undefined", "severity": "error", "in": "tool:c", "message": "m"},
        ]}
        readiness = build_readiness("t", {}, contract=contract)
        repairs = readiness["repairs"]
        self.assertEqual(repairs[0]["kind"], "required_param_undefined")
        self.assertEqual(repairs[0]["priority"], 1)
        self.assertEqual(repairs[0]["where"], ["tool:b", "tool:c"])
        self.assertEqual(repairs[-1]["kind"], "undocumented_param")

    def test_unexecuted_planned_suites_are_noted(self) -> None:
        plan = {"suites": {"smoke": {"cases": 1}, "security": {"cases": 2}},
                "coverage": {"gaps": []}}
        results = _results([_entry("smoke-discovery", "smoke", "pass")])
        readiness = build_readiness("t", {}, plan=plan, results=results)
        self.assertTrue(any("security" in note for note in readiness["coverage_notes"]))
        self.assertEqual(readiness["verdict"], "needs-work")


class RenderTest(unittest.TestCase):
    def test_markdown_renders_all_sections(self) -> None:
        contract = {"findings": [
            {"kind": "required_param_undefined", "severity": "error", "in": "tool:b", "message": "m"},
        ]}
        results = _results([_entry("a", "smoke", "fail", detail="isError for valid args")])
        md = render_readiness_md(build_readiness("t", GATES, contract=contract, results=results))
        self.assertIn("NOT-READY", md)
        self.assertIn("## Gates", md)
        self.assertIn("## Failure clusters", md)
        self.assertIn("required_param_undefined", md)


if __name__ == "__main__":
    unittest.main()
