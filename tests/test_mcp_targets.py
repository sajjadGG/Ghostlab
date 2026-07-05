"""Tests for the MCP target adapter layer (standard mcpServers + GhostLab native)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rehearsal.config import ConfigError
from rehearsal.mcp_targets import load_target, normalize_target


class NormalizeMcpServersTest(unittest.TestCase):
    def test_single_stdio_server_auto_selected(self) -> None:
        data = {
            "mcpServers": {
                "obsidian": {
                    "command": "npx",
                    "args": ["-y", "obsidian-mcp"],
                    "env": {"VAULT": "/notes"},
                }
            }
        }
        target = normalize_target(data)
        self.assertEqual(target.id, "obsidian")
        self.assertEqual(target.transport, "stdio")
        self.assertEqual(target.connection["command"], "npx")
        self.assertEqual(target.connection["args"], ["-y", "obsidian-mcp"])
        self.assertEqual(target.connection["env"], {"VAULT": "/notes"})

    def test_http_server_with_headers_preserves_placeholder(self) -> None:
        data = {
            "mcpServers": {
                "github": {
                    "url": "https://api.githubcopilot.com/mcp/",
                    "headers": {"Authorization": "Bearer ${GH_TOKEN}"},
                }
            }
        }
        target = normalize_target(data)
        self.assertEqual(target.transport, "streamable-http")
        self.assertEqual(target.connection["url"], "https://api.githubcopilot.com/mcp/")
        # secret stays a literal placeholder; expansion happens at connect time
        self.assertEqual(
            target.connection["headers"]["Authorization"], "Bearer ${GH_TOKEN}"
        )

    def test_type_field_maps_transport(self) -> None:
        sse = normalize_target({"mcpServers": {"s": {"type": "sse", "url": "http://x/sse"}}})
        self.assertEqual(sse.transport, "sse")
        http = normalize_target({"mcpServers": {"s": {"type": "http", "url": "http://x/mcp"}}})
        self.assertEqual(http.transport, "streamable-http")

    def test_multi_server_requires_selector(self) -> None:
        data = {"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}
        with self.assertRaises(ConfigError) as ctx:
            normalize_target(data)
        self.assertIn("--server", str(ctx.exception))
        self.assertIn("a", str(ctx.exception))
        self.assertIn("b", str(ctx.exception))

    def test_server_selector_picks_and_validates(self) -> None:
        data = {"mcpServers": {"a": {"command": "x"}, "b": {"url": "http://y/mcp"}}}
        self.assertEqual(normalize_target(data, server="b").transport, "streamable-http")
        with self.assertRaises(ConfigError) as ctx:
            normalize_target(data, server="nope")
        self.assertIn("no server 'nope'", str(ctx.exception))

    def test_entry_without_command_or_url_errors(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            normalize_target({"mcpServers": {"broken": {"foo": 1}}})
        self.assertIn("command", str(ctx.exception))
        self.assertIn("url", str(ctx.exception))


class NormalizeGhostlabNativeTest(unittest.TestCase):
    def test_native_target_still_supported(self) -> None:
        data = {
            "id": "cortex",
            "transport": "streamable-http",
            "connection": {"url": "http://localhost:8000/mcp", "headers": {}},
            "capabilities": {"tools": ["memory_get"]},
        }
        target = normalize_target(data)
        self.assertEqual(target.id, "cortex")
        self.assertEqual(target.capabilities, {"tools": ["memory_get"]})

    def test_unrecognized_config_names_both_formats(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            normalize_target({"random": "shape"})
        msg = str(ctx.exception)
        self.assertIn("mcpServers", msg)
        self.assertIn("GhostLab target", msg)


class LoadTargetFromDiskTest(unittest.TestCase):
    def test_load_standard_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp.json"
            path.write_text(
                json.dumps({"mcpServers": {"notes": {"command": "notes-mcp"}}}),
                encoding="utf-8",
            )
            target = load_target(path)
            self.assertEqual(target.id, "notes")
            self.assertEqual(target.transport, "stdio")


if __name__ == "__main__":
    unittest.main()
