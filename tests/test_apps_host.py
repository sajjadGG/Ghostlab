"""Browser-free tests for the MCP Apps host layer (increment 2).

The protocol/executor/assertion/report logic is pure Python; only the renderer
needs a browser, so it is exercised separately (live) and not imported here.
"""
from __future__ import annotations

import json
import unittest

from rehearsal.apps_host import protocol
from rehearsal.apps_host.assertions import (
    assertions_for,
    evaluate_assertions,
    widget_slug,
)
from rehearsal.apps_host.executor import plan_intent, plan_intents
from rehearsal.apps_host.report import build_render_report, first_ui_tool, render_report_md
from rehearsal.apps_host.renderer import RenderResult
from rehearsal.mcp_apps import UiIntent


class ProtocolTests(unittest.TestCase):
    def test_initialize_result_shape(self):
        result = protocol.build_initialize_result()
        self.assertEqual(result["protocolVersion"], protocol.PROTOCOL_VERSION)
        self.assertIn("hostInfo", result)
        self.assertIn("hostCapabilities", result)
        self.assertIn("hostContext", result)

    def test_host_page_is_valid_and_wires_methods(self):
        page = protocol.build_host_page(width=320, height=240)
        self.assertIn("__ghostlabMount", page)
        self.assertIn(protocol.UI_INITIALIZE, page)
        self.assertIn(protocol.NOTIF_TOOL_INPUT, page)
        self.assertIn(protocol.NOTIF_TOOL_RESULT, page)
        self.assertIn("320", page)
        # The embedded INIT_RESULT must be valid JSON-bearing.
        self.assertIn(protocol.PROTOCOL_VERSION, page)

    def test_classify_transcript(self):
        raw = [
            {"direction": "widget->host", "msg": {"jsonrpc": "2.0", "id": 0, "method": "ui/initialize"}},
            {"direction": "host->widget", "msg": {"jsonrpc": "2.0", "id": 0, "result": {}}},
            {"direction": "widget->host", "msg": {"jsonrpc": "2.0", "method": "ui/notifications/initialized"}},
            {"direction": "host->widget", "msg": {"jsonrpc": "2.0", "method": "ui/notifications/tool-result", "params": {}}},
            {"direction": "widget->host", "msg": {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601}}},
            {"not": "a message"},
        ]
        msgs = protocol.classify_transcript(raw)
        kinds = [(m.direction, m.kind, m.method) for m in msgs]
        self.assertEqual(kinds[0], ("widget->host", "request", "ui/initialize"))
        self.assertEqual(kinds[1][1], "response")
        self.assertEqual(kinds[2][1], "notification")
        self.assertEqual(kinds[4][1], "error")
        self.assertEqual(len(msgs), 5)  # the malformed entry is dropped

    def test_handshake_completed(self):
        msgs = protocol.classify_transcript([
            {"direction": "widget->host", "msg": {"jsonrpc": "2.0", "method": "ui/notifications/initialized"}},
            {"direction": "host->widget", "msg": {"jsonrpc": "2.0", "method": "ui/notifications/tool-result", "params": {}}},
        ])
        self.assertTrue(protocol.handshake_completed(msgs))

    def test_handshake_incomplete_without_data(self):
        msgs = protocol.classify_transcript([
            {"direction": "widget->host", "msg": {"jsonrpc": "2.0", "method": "ui/notifications/initialized"}},
        ])
        self.assertFalse(protocol.handshake_completed(msgs))


class ExecutorTests(unittest.TestCase):
    def test_choose_clicks_target(self):
        plan = plan_intent(UiIntent(type="choose", target="Option B"))
        self.assertEqual(plan.actions[0].op, "click_text")
        self.assertEqual(plan.actions[0].text, "Option B")
        self.assertEqual(plan.error, "")

    def test_reorder_clicks_each_in_order(self):
        plan = plan_intent(UiIntent(type="reorder", value=["The", "cat", "sat"]))
        self.assertEqual([a.text for a in plan.actions], ["The", "cat", "sat"])

    def test_reorder_requires_list(self):
        plan = plan_intent(UiIntent(type="reorder", value="The cat"))
        self.assertTrue(plan.error)
        self.assertEqual(plan.actions, [])

    def test_type_fills_default_selector(self):
        plan = plan_intent(UiIntent(type="type", value="hello"))
        self.assertEqual(plan.actions[0].op, "fill")
        self.assertEqual(plan.actions[0].text, "hello")

    def test_reveal_and_submit_have_fallbacks(self):
        reveal = plan_intent(UiIntent(type="reveal"))
        self.assertEqual(reveal.actions[0].text, "Reveal answer")
        submit = plan_intent(UiIntent(type="submit"))
        self.assertEqual(submit.actions[0].text, "Check")
        self.assertIn("fallbacks", submit.actions[0].note)

    def test_submit_override(self):
        plan = plan_intent(UiIntent(type="submit", target="Finish"))
        self.assertEqual(plan.actions[0].text, "Finish")

    def test_plan_intents_sequence(self):
        plans = plan_intents([UiIntent(type="reveal"), UiIntent(type="submit")])
        self.assertEqual(len(plans), 2)


class AssertionTests(unittest.TestCase):
    def test_widget_slug(self):
        self.assertEqual(widget_slug("ui://sentence-scramble/v1.html"), "sentence-scramble")
        self.assertEqual(widget_slug("not-a-uri"), "")

    def test_assertions_for_includes_generic_and_specific(self):
        names = [a.name for a in assertions_for("ui://sentence-scramble/v1.html")]
        self.assertIn("handshake_completed", names)
        self.assertIn("scramble_prompt_visible", names)

    def test_evaluate_passes_on_good_render(self):
        summary = {
            "handshake_completed": True,
            "console_errors": [],
            "body_text": "Sentence Scramble — reorder the words. Reveal answer",
            "interactive_count": 6,
        }
        results = evaluate_assertions(assertions_for("ui://sentence-scramble/v1.html"), summary)
        self.assertTrue(all(r["passed"] for r in results), results)

    def test_evaluate_flags_failures(self):
        summary = {"handshake_completed": False, "console_errors": ["boom"], "body_text": "", "interactive_count": 0}
        results = evaluate_assertions(assertions_for("ui://flashcards-set/v1.html"), summary)
        failed = {r["name"] for r in results if not r["passed"]}
        self.assertIn("handshake_completed", failed)
        self.assertIn("body_rendered", failed)
        self.assertIn("no_console_errors", failed)
        self.assertIn("has_interactive_elements", failed)


class ReportTests(unittest.TestCase):
    def _result(self):
        return RenderResult(
            uri="ui://sentence-scramble/v1.html",
            handshake_completed=True,
            body_text="Sentence Scramble Reveal answer",
            final_body_text="Sentence Scramble Reveal answer",
            interactive_count=6,
            transcript=[{"direction": "widget->host", "kind": "request", "method": "ui/initialize", "id": 0}],
            screenshot_path="/tmp/widget.png",
            intent_log=[{"intent": {"type": "reveal"}, "ok": True, "steps": []}],
        )

    def test_build_render_report(self):
        result = self._result()
        assertions = evaluate_assertions(assertions_for(result.uri), result.summary())
        report = build_render_report("cortex", "views_generate_sentence_scramble", result, assertions)
        self.assertTrue(report["summary"]["rendered"])
        self.assertTrue(report["summary"]["handshake_completed"])
        self.assertEqual(report["host_bridge_transcript"]["status"], "captured")
        self.assertEqual(report["interaction_transcript"]["status"], "captured")
        self.assertEqual(report["render_artifacts"]["screenshot"], "/tmp/widget.png")
        # Round-trips as JSON.
        json.dumps(report)

    def test_render_report_md(self):
        result = self._result()
        assertions = evaluate_assertions(assertions_for(result.uri), result.summary())
        md = render_report_md(build_render_report("cortex", "t", result, assertions))
        self.assertIn("MCP Apps Render: cortex", md)
        self.assertIn("Host-bridge transcript", md)
        self.assertIn("Final app state", md)

    def test_report_reflects_render_error(self):
        result = RenderResult(uri="ui://x/v1.html", error="browser launch failed")
        assertions = evaluate_assertions(assertions_for(result.uri), result.summary())
        report = build_render_report("cortex", "t", result, assertions)
        self.assertFalse(report["summary"]["rendered"])
        self.assertEqual(report["render_artifacts"]["status"], "failed")


class FirstUiToolTests(unittest.TestCase):
    TOOLS = [
        {"name": "plain"},
        {"name": "ui_a", "_meta": {"ui": {"resourceUri": "ui://a/v1.html"}}},
        {"name": "ui_b", "_meta": {"ui": {"resourceUri": "ui://b/v1.html"}}},
    ]

    def test_first_ui_tool_default(self):
        self.assertEqual(first_ui_tool(self.TOOLS)["name"], "ui_a")

    def test_first_ui_tool_named(self):
        self.assertEqual(first_ui_tool(self.TOOLS, "ui_b")["name"], "ui_b")

    def test_first_ui_tool_none(self):
        self.assertIsNone(first_ui_tool([{"name": "plain"}]))


if __name__ == "__main__":
    unittest.main()
