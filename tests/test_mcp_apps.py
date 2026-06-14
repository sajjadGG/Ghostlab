"""Tests for the MCP Apps host-layer foundation (no network needed)."""
from __future__ import annotations

import unittest

from rehearsal.mcp_apps import (
    AppResource,
    UiIntentError,
    build_app_report,
    diagnose_resource,
    parse_app_resource,
    parse_ui_intent,
    parse_ui_intents,
    probe_ui_tools,
    render_app_report_md,
    tool_result_ui_ref,
    ui_resource_uri,
    ui_tools,
)

# Real shapes observed from the cortex server.
FLASHCARDS_TOOL = {
    "name": "views_create_flash_cards",
    "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
    "_meta": {
        "ui": {"resourceUri": "ui://flashcards-set/v1.html"},
        "ui/resourceUri": "ui://flashcards-set/v1.html",
    },
}
LISTENING_TOOL = {
    "name": "views_create_listening_practice",
    "inputSchema": {"type": "object", "properties": {"audio_url": {"type": "string"}}},
    "_meta": {"ui": {"resourceUri": "ui://listening-practice/v1.html"}},
}
PLAIN_TOOL = {"name": "student_get_status", "inputSchema": {"type": "object"}}

SCRAMBLE_READ = {
    "contents": [
        {
            "uri": "ui://sentence-scramble/v1.html",
            "mimeType": "text/html;profile=mcp-app",
            "text": "<html><body>scramble</body></html>",
            "_meta": {"ui": {"prefersBorder": True, "csp": {"connectDomains": [], "resourceDomains": []}}},
        }
    ]
}


class UiReferenceTests(unittest.TestCase):
    def test_ui_resource_uri_nested_and_flat(self):
        self.assertEqual(ui_resource_uri({"ui": {"resourceUri": "ui://x"}}), "ui://x")
        self.assertEqual(ui_resource_uri({"ui/resourceUri": "ui://y"}), "ui://y")
        self.assertIsNone(ui_resource_uri({"ui": {}}))
        self.assertIsNone(ui_resource_uri(None))

    def test_ui_tools_filters_non_ui(self):
        refs = ui_tools([FLASHCARDS_TOOL, PLAIN_TOOL, LISTENING_TOOL])
        self.assertEqual(
            refs,
            [
                {"tool": "views_create_flash_cards", "resource_uri": "ui://flashcards-set/v1.html"},
                {"tool": "views_create_listening_practice", "resource_uri": "ui://listening-practice/v1.html"},
            ],
        )

    def test_tool_result_ui_ref(self):
        self.assertEqual(
            tool_result_ui_ref({"_meta": {"ui": {"resourceUri": "ui://z"}}}), "ui://z"
        )
        self.assertIsNone(tool_result_ui_ref({"content": []}))


class ResourceParseTests(unittest.TestCase):
    def test_parse_app_resource(self):
        res = parse_app_resource("ui://sentence-scramble/v1.html", SCRAMBLE_READ)
        self.assertTrue(res.is_mcp_app)
        self.assertTrue(res.renderable)
        self.assertEqual(res.html_length, len("<html><body>scramble</body></html>"))
        self.assertTrue(res.prefers_border)
        self.assertEqual(res.csp_connect_domains, [])

    def test_parse_app_resource_empty_contents(self):
        res = parse_app_resource("ui://missing", {"contents": []})
        self.assertFalse(res.renderable)
        self.assertIsNotNone(res.fetch_error)

    def test_parse_picks_matching_uri(self):
        read = {"contents": [{"uri": "ui://other", "text": "x"}, SCRAMBLE_READ["contents"][0]]}
        res = parse_app_resource("ui://sentence-scramble/v1.html", read)
        self.assertEqual(res.mime_type, "text/html;profile=mcp-app")


class DiagnosticsTests(unittest.TestCase):
    def test_clean_resource_no_findings(self):
        res = parse_app_resource("ui://flashcards-set/v1.html", {
            "contents": [{
                "uri": "ui://flashcards-set/v1.html",
                "mimeType": "text/html;profile=mcp-app",
                "text": "<html></html>",
                "_meta": {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}}},
            }]
        })
        self.assertEqual(diagnose_resource(res, FLASHCARDS_TOOL), [])

    def test_remote_media_with_empty_csp_flags(self):
        res = parse_app_resource("ui://listening-practice/v1.html", {
            "contents": [{
                "uri": "ui://listening-practice/v1.html",
                "mimeType": "text/html;profile=mcp-app",
                "text": "<html></html>",
                "_meta": {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}}},
            }]
        })
        kinds = [f["kind"] for f in diagnose_resource(res, LISTENING_TOOL)]
        self.assertIn("csp_blocks_remote_media", kinds)

    def test_remote_media_matches_url_variants(self):
        reading_tool = {
            "name": "views_create_reading_practice",
            "inputSchema": {"type": "object", "properties": {"passage_image_url": {"type": "string"}}},
            "_meta": {"ui": {"resourceUri": "ui://reading-practice/v1.html"}},
        }
        res = AppResource(
            uri="ui://reading-practice/v1.html",
            mime_type="text/html;profile=mcp-app",
            html="<html></html>",
            html_length=13,
        )
        findings = diagnose_resource(res, reading_tool)
        media = [f for f in findings if f["kind"] == "csp_blocks_remote_media"]
        self.assertEqual(media[0]["media_params"], ["passage_image_url"])

    def test_remote_media_with_open_csp_ok(self):
        res = AppResource(
            uri="ui://listening-practice/v1.html",
            mime_type="text/html;profile=mcp-app",
            html="<html></html>",
            html_length=13,
            csp_resource_domains=["cdn.example.com"],
        )
        kinds = [f["kind"] for f in diagnose_resource(res, LISTENING_TOOL)]
        self.assertNotIn("csp_blocks_remote_media", kinds)

    def test_non_mcp_app_and_empty_flagged(self):
        res = AppResource(uri="ui://x", mime_type="text/html", html_length=0)
        kinds = [f["kind"] for f in diagnose_resource(res)]
        self.assertIn("resource_empty", kinds)
        self.assertIn("resource_not_mcp_app", kinds)

    def test_unfetchable_short_circuits(self):
        res = AppResource(uri="ui://x", fetch_error="HTTP 500")
        findings = diagnose_resource(res)
        self.assertEqual([f["kind"] for f in findings], ["resource_unfetchable"])


class UiIntentTests(unittest.TestCase):
    def test_parse_valid_intent(self):
        intent = parse_ui_intent({"type": "choose", "target": "option-2", "note": "second answer"})
        self.assertEqual(intent.type, "choose")
        self.assertEqual(intent.to_json(), {"type": "choose", "target": "option-2", "note": "second answer"})

    def test_unknown_type_rejected(self):
        with self.assertRaises(UiIntentError):
            parse_ui_intent({"type": "explode"})

    def test_non_object_rejected(self):
        with self.assertRaises(UiIntentError):
            parse_ui_intent(["submit"])

    def test_parse_intent_list(self):
        intents = parse_ui_intents([
            {"type": "reorder", "value": [3, 1, 2]},
            {"type": "submit"},
        ])
        self.assertEqual([i.type for i in intents], ["reorder", "submit"])

    def test_parse_intents_requires_list(self):
        with self.assertRaises(UiIntentError):
            parse_ui_intents({"type": "submit"})


class FakeClient:
    """Minimal client exposing read_resource for probe_ui_tools."""

    def __init__(self, by_uri, errors=None):
        self.by_uri = by_uri
        self.errors = errors or {}
        self.calls = []

    def read_resource(self, uri):
        self.calls.append(uri)
        if uri in self.errors:
            raise RuntimeError(self.errors[uri])
        return self.by_uri[uri]


class ProbeAndReportTests(unittest.TestCase):
    def test_probe_ui_tools_caches_and_diagnoses(self):
        client = FakeClient({"ui://flashcards-set/v1.html": SCRAMBLE_READ})
        # Two tools share one resource → fetched once.
        tools = [FLASHCARDS_TOOL, dict(FLASHCARDS_TOOL, name="views_create_flash_cards_alt")]
        probes = probe_ui_tools(client, tools)
        self.assertEqual(len(probes), 2)
        self.assertEqual(client.calls, ["ui://flashcards-set/v1.html"])  # cached
        self.assertTrue(probes[0].resource.renderable)

    def test_probe_only_filter(self):
        client = FakeClient({"ui://flashcards-set/v1.html": SCRAMBLE_READ})
        probes = probe_ui_tools(client, [FLASHCARDS_TOOL, LISTENING_TOOL], only={"views_create_flash_cards"})
        self.assertEqual([p.tool for p in probes], ["views_create_flash_cards"])

    def test_probe_records_fetch_error(self):
        client = FakeClient({}, errors={"ui://flashcards-set/v1.html": "boom"})
        probes = probe_ui_tools(client, [FLASHCARDS_TOOL])
        self.assertEqual(probes[0].resource.fetch_error, "boom")
        self.assertEqual(probes[0].diagnostics[0]["kind"], "resource_unfetchable")

    def test_build_and_render_report(self):
        client = FakeClient({"ui://flashcards-set/v1.html": SCRAMBLE_READ})
        probes = probe_ui_tools(client, [FLASHCARDS_TOOL])
        report = build_app_report("cortex", probes)
        self.assertEqual(report["summary"]["ui_tools"], 1)
        self.assertEqual(report["summary"]["renderable_resources"], 1)
        self.assertEqual(report["host_bridge_transcript"]["status"], "pending")
        md = render_app_report_md(report)
        self.assertIn("MCP Apps Report: cortex", md)
        self.assertIn("views_create_flash_cards", md)


if __name__ == "__main__":
    unittest.main()
