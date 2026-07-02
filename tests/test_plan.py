"""Unit tests for the coverage-driven test plan generator."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rehearsal.contract import build_contract
from rehearsal.plan import (
    SUITES,
    build_test_plan,
    load_test_plan,
    render_plan_md,
    set_case_statuses,
    write_test_plan,
)

TOOLS = [
    {
        "name": "notes_list",
        "description": "List the user's saved notes, most recent first.",
        "inputSchema": {
            "type": "object",
            "properties": {"lang": {"type": "string", "enum": ["en", "fr"], "description": "x"}},
            "required": ["lang"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "notes_purge",
        "description": "Permanently delete every note for this user account.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "widget_show",
        "description": "Render the notes widget for browsing and editing notes.",
        "inputSchema": {
            "type": "object",
            "properties": {"api_token": {"type": "string", "description": "auth"}},
        },
        "annotations": {"readOnlyHint": True},
        "_meta": {"ui": {"resourceUri": "ui://notes/v1.html"}},
    },
]


def _contract() -> dict:
    return build_contract({
        "target_id": "fake-notes", "transport": "stdio",
        "server_info": {"name": "fake-notes", "version": "0.0.1"},
        "tools": TOOLS,
        "resources": [{"uri": "ui://notes/v1.html", "name": "widget"}],
        "prompts": [],
        "lint": [{"kind": "missing_tool_reference", "referenced": "notes_archive",
                  "in": "instructions"}],
    })


def _plan(**kwargs) -> dict:
    return build_test_plan("fake-notes", _contract(), TOOLS, **kwargs)


class BuildPlanTest(unittest.TestCase):
    def test_every_case_has_a_reason_and_valid_suite(self) -> None:
        plan = _plan()
        self.assertGreater(len(plan["cases"]), 5)
        for case in plan["cases"]:
            self.assertIn(case["suite"], SUITES)
            self.assertTrue(case["reason"], f"{case['id']} lacks a reason")
            self.assertEqual(case["status"], "proposed")

    def test_smoke_calls_only_read_only_tools_with_generated_args(self) -> None:
        plan = _plan()
        calls = {
            case["execution"]["tool"]: case
            for case in plan["cases"]
            if case["suite"] == "smoke" and case["execution"].get("type") == "tool_call"
        }
        self.assertIn("notes_list", calls)
        self.assertNotIn("notes_purge", calls)  # destructive, never in smoke
        self.assertEqual(calls["notes_list"]["execution"]["arguments"], {"lang": "en"})

    def test_edge_probes_derive_from_schema(self) -> None:
        plan = _plan()
        edge_ids = {case["id"] for case in plan["cases"] if case["suite"] == "edge"}
        self.assertIn("edge-notes-list-missing-required", edge_ids)
        self.assertIn("edge-notes-list-invalid-enum-lang", edge_ids)

    def test_security_cases_from_contract_signals(self) -> None:
        plan = _plan()
        ids = {case["id"] for case in plan["cases"] if case["suite"] == "security"}
        self.assertIn("security-hallucinated-notes-archive", ids)
        self.assertIn("security-destructive-notes-purge", ids)
        self.assertIn("security-credential-widget-show", ids)
        self.assertIn("security-resource-injection", ids)

    def test_apps_cases_per_ui_resource(self) -> None:
        plan = _plan()
        ids = {case["id"] for case in plan["cases"] if case["suite"] == "apps"}
        self.assertEqual(ids, {"apps-render-notes-v1-html", "apps-interact-notes-v1-html"})

    def test_error_recovery_seeded_by_sample_findings(self) -> None:
        samples = {"findings": [
            {"kind": "sample_call_failed", "severity": "error", "in": "tool:notes_list",
             "message": "boom"},
        ]}
        plan = _plan(samples=samples)
        ids = {case["id"] for case in plan["cases"] if case["suite"] == "error-recovery"}
        self.assertEqual(ids, {"error-recovery-notes-list"})

    def test_host_compat_needs_two_hosts(self) -> None:
        one = _plan(hosts=[{"id": "direct-mcp", "kind": "direct-mcp"}])
        self.assertEqual(one["suites"]["host-compat"]["cases"], 0)
        two = _plan(hosts=[
            {"id": "direct-mcp", "kind": "direct-mcp"},
            {"id": "codex-cli", "kind": "codex-session"},
        ])
        self.assertEqual(two["suites"]["host-compat"]["cases"], 2)

    def test_prior_statuses_survive_regeneration(self) -> None:
        prior = _plan()
        set_case_statuses(prior, {"smoke-discovery"}, "approved")
        set_case_statuses(prior, {"security-resource-injection"}, "rejected")
        regenerated = _plan(prior_plan=prior)
        statuses = {case["id"]: case["status"] for case in regenerated["cases"]}
        self.assertEqual(statuses["smoke-discovery"], "approved")
        self.assertEqual(statuses["security-resource-injection"], "rejected")
        self.assertEqual(statuses["semantic-notes-workflow"], "proposed")

    def test_coverage_reports_gaps_for_rejected_tools(self) -> None:
        prior = _plan()
        # Reject every case that touches notes_purge -> it becomes untested.
        purge_cases = {
            case["id"] for case in prior["cases"] if "notes_purge" in case.get("tools", [])
        }
        set_case_statuses(prior, purge_cases, "rejected")
        plan = _plan(prior_plan=prior)
        self.assertIn("notes_purge", plan["coverage"]["untested_tools"])
        self.assertTrue(any("notes_purge" in gap for gap in plan["coverage"]["gaps"]))


class PlanPersistenceTest(unittest.TestCase):
    def test_yaml_round_trip(self) -> None:
        plan = _plan()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test-plan.yaml"
            write_test_plan(plan, path)
            self.assertEqual(load_test_plan(path), plan)

    def test_set_statuses_updates_suite_summary(self) -> None:
        plan = _plan()
        updated = set_case_statuses(plan, set(), "approved")  # empty set = all
        self.assertEqual(len(updated), len(plan["cases"]))
        for suite in plan["suites"].values():
            self.assertEqual(suite["approved"], suite["cases"])
        with self.assertRaises(ValueError):
            set_case_statuses(plan, set(), "bogus")

    def test_markdown_renders(self) -> None:
        md = render_plan_md(_plan())
        self.assertIn("# Test Plan: fake-notes", md)
        self.assertIn("smoke-discovery", md)
        self.assertIn("reason:", md)


if __name__ == "__main__":
    unittest.main()
