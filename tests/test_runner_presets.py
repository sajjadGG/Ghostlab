"""Tests for codex runner presets built from a target (no codex needed)."""
from __future__ import annotations

import unittest

from rehearsal.config import TargetConfig
from rehearsal.runner_presets import codex_aut_runner, codex_user_runner


def _http_target() -> TargetConfig:
    return TargetConfig(
        id="cortex", transport="streamable-http", connection={"url": "http://localhost:8000/mcp"}
    )


class AutRunnerTest(unittest.TestCase):
    def test_injects_http_url_and_json(self) -> None:
        runner = codex_aut_runner(_http_target(), session=True)
        self.assertEqual(runner.kind, "codex-session")
        self.assertEqual(runner.parser, "codex-json")
        self.assertIn('mcp_servers.cortex.url="http://localhost:8000/mcp"', runner.command)
        self.assertIn("--json", runner.command)
        self.assertIn("exec", runner.command)

    def test_non_session_is_process(self) -> None:
        self.assertEqual(codex_aut_runner(_http_target(), session=False).kind, "process")

    def test_stdio_injects_command_and_args(self) -> None:
        target = TargetConfig(
            id="fs", transport="stdio", connection={"command": "python", "args": ["-m", "srv"]}
        )
        runner = codex_aut_runner(target)
        joined = " ".join(runner.command)
        self.assertIn('mcp_servers.fs.command="python"', joined)
        self.assertIn("mcp_servers.fs.args=", joined)


class UserRunnerTest(unittest.TestCase):
    def test_user_runner_has_no_mcp_and_text_parser(self) -> None:
        runner = codex_user_runner()
        self.assertEqual(runner.parser, "text")
        self.assertNotIn("--json", runner.command)
        self.assertFalse(any("mcp_servers" in part for part in runner.command))


if __name__ == "__main__":
    unittest.main()
