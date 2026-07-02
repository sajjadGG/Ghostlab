"""Unit tests for safe tool sampling: arg generation, planning gates, execution."""
from __future__ import annotations

import unittest

from rehearsal.contract import build_contract
from rehearsal.sampling import (
    ArgGenerationError,
    generate_arguments,
    generate_example_value,
    plan_samples,
    run_samples,
)


def _contract_for(tools: list[dict]) -> dict:
    return build_contract({
        "target_id": "t", "transport": "stdio",
        "server_info": {"name": "t", "version": "0"},
        "tools": tools, "resources": [], "prompts": [], "lint": [],
    })


READ_TOOL = {
    "name": "notes_list",
    "description": "List notes, most recent first, for the current user.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "max"},
            "lang": {"type": "string", "description": "language", "enum": ["en", "fr"]},
        },
        "required": ["lang"],
    },
    "annotations": {"readOnlyHint": True},
}
WRITE_TOOL = {
    "name": "notes_create",
    "description": "Create a new note with the given body text.",
    "inputSchema": {
        "type": "object",
        "properties": {"body": {"type": "string", "description": "note body"}},
        "required": ["body"],
    },
}
DESTRUCTIVE_TOOL = {
    "name": "notes_purge",
    "description": "Permanently delete every note for this user.",
    "inputSchema": {"type": "object", "properties": {}},
}


class ArgGenerationTest(unittest.TestCase):
    def test_prefers_default_examples_enum(self) -> None:
        self.assertEqual(generate_example_value({"type": "integer", "default": 5}, "n"), 5)
        self.assertEqual(generate_example_value({"type": "string", "examples": ["hi"]}, "s"), "hi")
        self.assertEqual(generate_example_value({"type": "string", "enum": ["a", "b"]}, "s"), "a")

    def test_type_zero_values(self) -> None:
        self.assertEqual(generate_example_value({"type": "boolean"}, "flag"), False)
        self.assertEqual(generate_example_value({"type": "array"}, "items"), [])
        value = generate_example_value(
            {"type": "array", "minItems": 2, "items": {"type": "integer"}}, "nums"
        )
        self.assertEqual(value, [1, 1])

    def test_format_and_name_aware_strings(self) -> None:
        self.assertIn("@", generate_example_value({"type": "string", "format": "email"}, "to"))
        self.assertEqual(generate_example_value({"type": "string"}, "target_lang"), "en")

    def test_nested_object_requires_only_required(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            "required": ["a"],
        }
        self.assertEqual(generate_example_value(schema, "obj"), {"a": "ghostlab sample"})

    def test_untypeable_property_raises(self) -> None:
        with self.assertRaises(ArgGenerationError):
            generate_example_value({}, "mystery")

    def test_generate_arguments_covers_required_only(self) -> None:
        self.assertEqual(generate_arguments(READ_TOOL), {"lang": "en"})


class PlanSamplesTest(unittest.TestCase):
    def test_safe_mode_calls_only_read_only(self) -> None:
        tools = [READ_TOOL, WRITE_TOOL, DESTRUCTIVE_TOOL]
        plan = plan_samples(_contract_for(tools), tools, mode="safe")
        by_tool = {entry["tool"]: entry for entry in plan}
        self.assertEqual(by_tool["notes_list"]["source"], "generated")
        self.assertIn("skipped", by_tool["notes_create"])
        self.assertIn("skipped", by_tool["notes_purge"])

    def test_fixture_mode_gates_mutations_and_destructive(self) -> None:
        tools = [WRITE_TOOL, DESTRUCTIVE_TOOL]
        fixtures = [
            {"tool": "notes_create", "arguments": {"body": "hi"}},
            {"tool": "notes_purge", "arguments": {}},
        ]
        contract = _contract_for(tools)

        plan = plan_samples(contract, tools, mode="fixture", fixtures=fixtures)
        by_tool = {entry["tool"]: entry for entry in plan}
        self.assertIn("approve-mutations", by_tool["notes_create"]["skipped"])
        self.assertIn("approve-destructive", by_tool["notes_purge"]["skipped"])

        plan = plan_samples(
            contract, tools, mode="fixture", fixtures=fixtures,
            approve_mutations=True,
        )
        by_tool = {entry["tool"]: entry for entry in plan}
        self.assertEqual(by_tool["notes_create"]["arguments"], {"body": "hi"})
        self.assertIn("skipped", by_tool["notes_purge"])  # still gated

        plan = plan_samples(
            contract, tools, mode="fixture", fixtures=fixtures,
            approve_mutations=True, approve_destructive=True,
        )
        by_tool = {entry["tool"]: entry for entry in plan}
        self.assertEqual(by_tool["notes_purge"]["source"], "fixture")

    def test_off_mode_plans_nothing(self) -> None:
        self.assertEqual(plan_samples(_contract_for([READ_TOOL]), [READ_TOOL], mode="off"), [])

    def test_ungenerable_read_only_tool_is_skipped_with_reason(self) -> None:
        tool = {
            "name": "opaque_get",
            "description": "Fetch a record using an opaque untyped key.",
            "inputSchema": {"type": "object", "properties": {"key": {}}, "required": ["key"]},
            "annotations": {"readOnlyHint": True},
        }
        plan = plan_samples(_contract_for([tool]), [tool], mode="safe")
        self.assertIn("cannot generate arguments", plan[0]["skipped"])


class RunSamplesTest(unittest.TestCase):
    def test_records_results_and_shape_findings(self) -> None:
        ui_tool = {
            "name": "widget_show",
            "description": "Render the widget for the current exercise state.",
            "inputSchema": {"type": "object", "properties": {}},
            "outputSchema": {"type": "object", "properties": {}},
            "annotations": {"readOnlyHint": True},
            "_meta": {"ui": {"resourceUri": "ui://w/v1.html"}},
        }

        class FakeClient:
            def call_tool(self, name, arguments):
                if name == "notes_list":
                    return {"content": [{"type": "text", "text": "3 notes"}]}
                # declares outputSchema + UI but returns nothing model-visible
                return {"content": []}

        tools = [READ_TOOL, ui_tool]
        plan = plan_samples(_contract_for(tools), tools, mode="safe")
        report = run_samples(FakeClient(), plan, tools)
        self.assertEqual(report["summary"]["called"], 2)
        self.assertEqual(report["summary"]["failed"], 0)
        kinds = {finding["kind"] for finding in report["findings"]}
        self.assertIn("missing_structured_content", kinds)
        self.assertIn("ui_tool_no_model_content", kinds)

    def test_transport_error_becomes_finding(self) -> None:
        class ExplodingClient:
            def call_tool(self, name, arguments):
                raise RuntimeError("boom")

        plan = plan_samples(_contract_for([READ_TOOL]), [READ_TOOL], mode="safe")
        report = run_samples(ExplodingClient(), plan, [READ_TOOL])
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["findings"][0]["kind"], "sample_call_failed")

    def test_is_error_result_becomes_warning(self) -> None:
        class ErrClient:
            def call_tool(self, name, arguments):
                return {"isError": True, "content": [{"type": "text", "text": "denied"}]}

        plan = plan_samples(_contract_for([READ_TOOL]), [READ_TOOL], mode="safe")
        report = run_samples(ErrClient(), plan, [READ_TOOL])
        self.assertEqual(report["samples"][0]["status"], "tool_error")
        self.assertEqual(report["findings"][0]["kind"], "sample_tool_error")


if __name__ == "__main__":
    unittest.main()
