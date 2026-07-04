"""Browser-free tests for the live MCP Apps host (relay, bridge extraction, planning).

The Playwright render path is exercised live against a server elsewhere; here we
lock in the pure logic: the relay only forwards server methods, the bridge page
routes those methods, the follow-up/context extractors read the trace, wire
normalization aliases camelCase, and intent planning does the exercise.
"""
from __future__ import annotations

import unittest

from rehearsal.apps_host import protocol
from rehearsal.apps_host.live import (
    build_relay,
    plan_widget_intents,
    resolve_ui_map,
    to_wire_tool_result,
    widgets_in_turn,
)


class FakeClient:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": f"called {name}"}], "isError": False}

    def read_resource(self, uri):
        return {"contents": [{"uri": uri, "text": "<html></html>"}]}

    def _call(self, method, params):
        class R:
            def unwrap(self, _ctx):
                return {"tools": [
                    {"name": "views_create_writing_practice",
                     "_meta": {"ui": {"resourceUri": "ui://writing-task/v1.html"}}},
                    {"name": "memory_get"},
                ]}
        return R()


class RelayTest(unittest.TestCase):
    def test_relays_tools_call_to_client(self):
        client = FakeClient()
        relay = build_relay(client)
        out = relay("tools/call", {"name": "session_record_result", "arguments": {"x": 1}})
        self.assertEqual(client.calls, [("session_record_result", {"x": 1})])
        self.assertFalse(out["isError"])

    def test_relays_resources_read(self):
        relay = build_relay(FakeClient())
        out = relay("resources/read", {"uri": "ui://writing-task/v1.html"})
        self.assertIn("contents", out)

    def test_rejects_non_relayable_method(self):
        relay = build_relay(FakeClient())
        with self.assertRaises(ValueError):
            relay("ui/request-teardown", {})

    def test_tools_call_needs_name(self):
        relay = build_relay(FakeClient())
        with self.assertRaises(ValueError):
            relay("tools/call", {})


class BridgePageTest(unittest.TestCase):
    def test_host_page_advertises_relay_methods(self):
        page = protocol.build_host_page()
        self.assertIn("__ghostlabRelay", page)
        self.assertIn("tools/call", page)
        self.assertIn("resources/read", page)


class TraceExtractionTest(unittest.TestCase):
    def _trace(self):
        return [
            {"direction": "widget->host", "msg": {"jsonrpc": "2.0", "method": "ui/initialize", "id": 1}},
            {"direction": "widget->host", "msg": {
                "jsonrpc": "2.0", "method": "ui/message",
                "params": {"role": "user", "content": [{"type": "text", "text": "Please evaluate my essay."}]}}},
            {"direction": "widget->host", "msg": {
                "jsonrpc": "2.0", "method": "ui/update-model-context",
                "params": {"context": {"wordCount": 320}}}},
        ]

    def test_extracts_follow_up_message(self):
        msgs = protocol.extract_widget_messages(self._trace())
        self.assertEqual(len(msgs), 1)
        self.assertEqual(protocol.widget_message_text(msgs[0]), "Please evaluate my essay.")

    def test_extracts_model_context_updates(self):
        updates = protocol.extract_model_context_updates(self._trace())
        self.assertEqual(updates, [{"context": {"wordCount": 320}}])


class WireAndPlanningTest(unittest.TestCase):
    def test_wire_normalization_adds_camelcase(self):
        wire = to_wire_tool_result({"structured_content": {"a": 1}, "content": []})
        self.assertEqual(wire["structuredContent"], {"a": 1})
        self.assertIn("structured_content", wire)  # original kept

    def test_resolve_ui_map_from_tools_list(self):
        ui_map = resolve_ui_map(FakeClient())
        self.assertEqual(ui_map["views_create_writing_practice"], "ui://writing-task/v1.html")

    def test_widgets_in_turn_fills_resource_uri_from_map(self):
        call = {
            "server": "cortex", "tool": "views_create_writing_practice", "status": "completed",
            "arguments": {"question_prompt": "Q"}, "result": {"structured_content": {"question_prompt": "Q"}},
        }
        widgets = widgets_in_turn([call], {"views_create_writing_practice": "ui://writing-task/v1.html"})
        self.assertEqual(widgets[0]["resource_uri"], "ui://writing-task/v1.html")
        self.assertEqual(widgets[0]["_tool_input"], {"question_prompt": "Q"})

    def test_heuristic_plan_writes_and_submits(self):
        widget = {"tool": "views_create_writing_practice",
                  "fields": {"question_prompt": "Discuss X.", "word_limit": 320}, "text": ""}
        intents = plan_widget_intents(widget, goal="write essay", backend=None)
        self.assertEqual([i.type for i in intents], ["type", "submit"])
        self.assertGreater(len(str(intents[0].value)), 200)  # a real response, not a stub


if __name__ == "__main__":
    unittest.main()
