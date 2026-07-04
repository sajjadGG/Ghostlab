"""Unit tests for the deterministic contract lint / risk classification."""
from __future__ import annotations

import unittest

from rehearsal.contract import (
    build_contract,
    classify_tool_risk,
    lint_tool_schema,
    lint_ui_metadata,
    render_contract_md,
)


def _kinds(findings: list[dict]) -> set[str]:
    return {f["kind"] for f in findings}


class SchemaLintTest(unittest.TestCase):
    def test_flags_missing_description_and_schema(self) -> None:
        findings = lint_tool_schema({"name": "notes_list"})
        self.assertIn("missing_tool_description", _kinds(findings))
        self.assertIn("missing_input_schema", _kinds(findings))

    def test_flags_required_param_not_in_properties(self) -> None:
        tool = {
            "name": "notes_delete",
            "description": "Delete a note permanently by its identifier.",
            "inputSchema": {
                "type": "object",
                "properties": {"note_id": {"type": "string", "description": "id"}},
                "required": ["note_id", "confirm"],
            },
        }
        findings = lint_tool_schema(tool)
        self.assertIn("required_param_undefined", _kinds(findings))

    def test_flags_untyped_and_undocumented_params(self) -> None:
        tool = {
            "name": "notes_update",
            "description": "Update the text of an existing note in place.",
            "inputSchema": {
                "type": "object",
                "properties": {"note_id": {}, "body": {"type": "string"}},
                "required": ["note_id"],
            },
        }
        kinds = _kinds(lint_tool_schema(tool))
        self.assertIn("untyped_param", kinds)
        self.assertIn("undocumented_param", kinds)

    def test_flags_host_unfriendly_schema_features(self) -> None:
        tool = {
            "name": "notes_query",
            "description": "Query notes with a structured filter expression.",
            "inputSchema": {
                "type": "object",
                "properties": {"filter": {"$ref": "#/$defs/filter"}},
                "$defs": {"filter": {"type": "object"}},
            },
        }
        self.assertIn("host_unfriendly_schema", _kinds(lint_tool_schema(tool)))

    def test_clean_tool_produces_no_findings(self) -> None:
        tool = {
            "name": "notes_list",
            "description": "List the user's saved notes, most recent first.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Maximum notes to return."}
                },
            },
        }
        self.assertEqual(lint_tool_schema(tool), [])


class RiskClassificationTest(unittest.TestCase):
    def test_annotations_win_over_heuristics(self) -> None:
        tool = {
            "name": "notes_delete",  # heuristics would say mutating+destructive
            "annotations": {"readOnlyHint": True},
            "inputSchema": {"type": "object", "properties": {}},
        }
        risk = classify_tool_risk(tool)
        self.assertTrue(risk["read_only"])
        self.assertFalse(risk["destructive"])
        self.assertIn("annotations", risk["source"])

    def test_heuristic_classification(self) -> None:
        risk = classify_tool_risk({"name": "student_complete_onboarding"})
        self.assertFalse(risk["read_only"])
        self.assertIn("mutates-state", risk["labels"])
        self.assertEqual(risk["source"], "heuristic")

        risk = classify_tool_risk({"name": "memory_get"})
        self.assertTrue(risk["read_only"])

        risk = classify_tool_risk({"name": "state_reset"})
        self.assertTrue(risk["destructive"])

    def test_credential_and_ui_labels(self) -> None:
        tool = {
            "name": "widget_show",
            "inputSchema": {
                "type": "object",
                "properties": {"api_token": {"type": "string"}},
            },
            "_meta": {"ui": {"resourceUri": "ui://w/v1.html"}},
        }
        risk = classify_tool_risk(tool)
        self.assertEqual(risk["credential_params"], ["api_token"])
        self.assertTrue(risk["produces_ui"])
        self.assertIn("credential-bearing", risk["labels"])
        self.assertIn("ui-producing", risk["labels"])

    def test_unknown_mutation_stays_none(self) -> None:
        risk = classify_tool_risk({"name": "frobnicate_zork"})
        self.assertIsNone(risk["read_only"])
        self.assertIn("unknown-mutation", risk["labels"])


class UiMetadataLintTest(unittest.TestCase):
    def test_openai_alias_only_is_flagged(self) -> None:
        tool = {"name": "w", "_meta": {"openai/outputTemplate": "ui://w/v1.html"}}
        kinds = _kinds(lint_ui_metadata(tool, {"ui://w/v1.html"}))
        self.assertIn("openai_alias_only", kinds)

    def test_alias_mismatch_is_an_error(self) -> None:
        tool = {
            "name": "w",
            "_meta": {
                "ui": {"resourceUri": "ui://w/v1.html"},
                "openai/outputTemplate": "ui://w/v2.html",
            },
        }
        findings = lint_ui_metadata(tool, {"ui://w/v1.html", "ui://w/v2.html"})
        mismatch = [f for f in findings if f["kind"] == "ui_alias_mismatch"]
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]["severity"], "error")

    def test_dangling_ui_resource(self) -> None:
        tool = {"name": "w", "_meta": {"ui": {"resourceUri": "ui://missing/v1.html"}}}
        kinds = _kinds(lint_ui_metadata(tool, {"ui://other/v1.html"}))
        self.assertIn("dangling_ui_resource", kinds)

    def test_unknown_visibility(self) -> None:
        tool = {
            "name": "w",
            "_meta": {"ui": {"resourceUri": "ui://w/v1.html", "visibility": "sometimes"}},
        }
        kinds = _kinds(lint_ui_metadata(tool, {"ui://w/v1.html"}))
        self.assertIn("unknown_ui_visibility", kinds)

    def test_visibility_audience_list_is_accepted(self) -> None:
        # Cortex-style: visibility is an array of audiences, e.g. ["app"].
        tool = {
            "name": "w",
            "_meta": {"ui": {"resourceUri": "ui://w/v1.html", "visibility": ["app"]}},
        }
        kinds = _kinds(lint_ui_metadata(tool, {"ui://w/v1.html"}))
        self.assertNotIn("unknown_ui_visibility", kinds)
        tool["_meta"]["ui"]["visibility"] = ["app", "sideways"]
        kinds = _kinds(lint_ui_metadata(tool, {"ui://w/v1.html"}))
        self.assertIn("unknown_ui_visibility", kinds)


class BuildContractTest(unittest.TestCase):
    def _inspect_data(self) -> dict:
        return {
            "target_id": "fake-notes",
            "transport": "stdio",
            "server_info": {"name": "fake-notes", "version": "0.0.1"},
            "tools": [
                {
                    "name": "notes_list",
                    "description": "List the user's saved notes, most recent first.",
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {"readOnlyHint": True},
                },
                {
                    "name": "widget_show",
                    "description": "Render the notes widget for browsing notes.",
                    "inputSchema": {"type": "object", "properties": {}},
                    "_meta": {"ui": {"resourceUri": "ui://notes/v1.html"}},
                },
            ],
            "resources": [{"uri": "ui://notes/v1.html", "name": "widget"}],
            "prompts": [],
            "lint": [
                {"kind": "missing_tool_reference", "referenced": "notes_archive", "in": "instructions"}
            ],
        }

    def test_builds_summary_and_folds_inspect_lint(self) -> None:
        contract = build_contract(self._inspect_data())
        self.assertEqual(contract["counts"]["tools"], 2)
        self.assertEqual(contract["counts"]["ui_tools"], 1)
        self.assertEqual(contract["mcp"], "fake-notes@0.0.1")
        kinds = _kinds(contract["findings"])
        self.assertIn("missing_tool_reference", kinds)
        by_severity = contract["summary"]["findings_by_severity"]
        self.assertEqual(
            by_severity["error"] + by_severity["warning"] + by_severity["info"],
            len(contract["findings"]),
        )

    def test_markdown_renders(self) -> None:
        md = render_contract_md(build_contract(self._inspect_data()))
        self.assertIn("# MCP Contract: fake-notes", md)
        self.assertIn("`notes_list`", md)
        self.assertIn("read-only", md)


if __name__ == "__main__":
    unittest.main()
