"""Unit tests for the inspect lint and MCP SSE parsing.

Run with: python3 -m unittest discover -s tests
"""
from __future__ import annotations

import unittest

from rehearsal.inspect import lint_missing_tool_refs
from rehearsal.mcp_client import _parse_sse


# A trimmed slice of the real Cortex tool set: the `views_*` tools reference
# `kb_find` / `kb_read` / `kb_read_skill`, which are NOT exposed.
CORTEX_TOOLS = [
    {
        "name": "views_create_flash_cards",
        "description": "Create a flashcards view from course material selected from "
        "the knowledge base (for example via `kb_find` and `kb_read`). Supply 10 "
        "cards with `front`, `back`.",
    },
    {
        "name": "views_create_reading_practice",
        "description": "Typical flow: call `kb_read_skill({ skill: 'reading', n })` to "
        "fetch a passage, then pass it through this tool.",
    },
    {
        "name": "memory_put",
        "description": "Replace the document. Pass `expected_version` (the version you "
        "last saw from `memory_get`) so the server can reject stale writes.",
    },
    {
        "name": "memory_get",
        "description": "Fetch user memory.",
    },
]


class LintTest(unittest.TestCase):
    def test_flags_missing_kb_tools(self) -> None:
        findings = lint_missing_tool_refs(CORTEX_TOOLS, resources=[])
        referenced = {f["referenced"] for f in findings}
        self.assertIn("kb_find", referenced)
        self.assertIn("kb_read", referenced)
        self.assertIn("kb_read_skill", referenced)

    def test_does_not_flag_schema_fields(self) -> None:
        findings = lint_missing_tool_refs(CORTEX_TOOLS, resources=[])
        referenced = {f["referenced"] for f in findings}
        # `expected_version`, `front`, `back` are prose field mentions, not tools.
        self.assertNotIn("expected_version", referenced)
        self.assertNotIn("front", referenced)
        self.assertNotIn("back", referenced)

    def test_does_not_flag_exposed_tools(self) -> None:
        findings = lint_missing_tool_refs(CORTEX_TOOLS, resources=[])
        referenced = {f["referenced"] for f in findings}
        # `memory_get` is referenced in prose but IS exposed -> not a finding.
        self.assertNotIn("memory_get", referenced)

    def test_clean_server_has_no_findings(self) -> None:
        tools = [
            {"name": "do_thing", "description": "Calls `do_thing` and reads `field_a`."},
        ]
        self.assertEqual(lint_missing_tool_refs(tools, resources=[]), [])


class SseParseTest(unittest.TestCase):
    def test_parses_data_frame(self) -> None:
        body = 'event: message\ndata: {"result": {"ok": true}, "id": 1}\n\n'
        message = _parse_sse(body)
        self.assertEqual(message["result"], {"ok": True})

    def test_parses_multiline_data(self) -> None:
        body = 'data: {"a":\ndata: 1}\n\n'
        self.assertEqual(_parse_sse(body), {"a": 1})


if __name__ == "__main__":
    unittest.main()
