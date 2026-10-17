"""OpenShell-compatible Copilot sessions and resolver-built job runners."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from rehearsal.config import RunnerConfig, TargetConfig
from rehearsal.jobs import create_job, default_job_spec, materialize_job_runners
from rehearsal.resolved_config import _describe_runner
from rehearsal.runners import (
    OpenShellCopilotProcessRunner,
    OpenShellCopilotSessionRunner,
    _copilot_sandbox_command,
)


class FakeSandbox:
    def __init__(self, returncode: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.returncode = returncode

    def exec(self, command, *, input_text, env, timeout):
        self.commands.append(list(command))
        self.inputs.append(input_text)
        return subprocess.CompletedProcess(command, self.returncode, stdout="", stderr="")

    def close(self) -> None:
        pass


COMMAND = ["copilot", "--output-format", "json", "--no-remote",
           "--session-id", "67686f73-746c-4000-8000-000000000000", "--prompt"]


def _config(kind: str) -> RunnerConfig:
    return RunnerConfig(
        kind=kind, command=list(COMMAND), prompt_mode="append-arg",
        parser="copilot-json", sandbox={"backend": "openshell"},
    )


class CopilotSandboxCommandTest(unittest.TestCase):
    def test_drops_flags_the_image_cli_rejects(self) -> None:
        cleaned = _copilot_sandbox_command(list(COMMAND), resume=False)
        self.assertNotIn("--session-id", cleaned)
        self.assertNotIn("--no-remote", cleaned)
        self.assertNotIn("--continue", cleaned)
        self.assertEqual(cleaned[-1], "--prompt")

    def test_resume_continues_before_the_prompt(self) -> None:
        cleaned = _copilot_sandbox_command(["copilot", "--session-id=x", "--prompt"], resume=True)
        self.assertEqual(cleaned, ["copilot", "--continue", "--prompt"])


class OpenShellCopilotRunnerTest(unittest.TestCase):
    def test_session_continues_only_after_a_successful_turn(self) -> None:
        runner = OpenShellCopilotSessionRunner(_config("copilot-session"), "aut")
        runner.sandbox = FakeSandbox(returncode=1)
        runner.run_turn("first")
        runner.sandbox.returncode = 0
        runner.run_turn("first again")
        runner.run_turn("second\nwith a newline")
        first, retried, second = runner.sandbox.commands
        self.assertNotIn("--continue", first)
        self.assertNotIn("--continue", retried)
        self.assertIn("--continue", second)
        for command in (first, retried, second):
            self.assertNotIn("--session-id", command)
            self.assertNotIn("--no-remote", command)
        # OpenShell rejects argv containing newlines: the prompt rides on stdin
        # and is re-attached as the last argument inside the sandbox.
        self.assertEqual(second[:4], ["/bin/sh", "-c", 'exec "$@" "$(cat)"', "sh"])
        self.assertEqual(second[-1], "--prompt")
        self.assertEqual(runner.sandbox.inputs[-1], "second\nwith a newline")
        self.assertFalse(any("\n" in part for part in second))

    def test_process_runner_never_resumes(self) -> None:
        runner = OpenShellCopilotProcessRunner(_config("process"), "aut")
        runner.sandbox = FakeSandbox()
        runner.run_turn("a")
        runner.run_turn("b")
        for command in runner.sandbox.commands:
            self.assertNotIn("--continue", command)
            self.assertNotIn("--session-id", command)


class ClaudeJobRunnerTest(unittest.TestCase):
    def test_claude_aut_is_materialized_by_the_shared_resolver(self) -> None:
        target = TargetConfig(
            id="notes", transport="streamable-http",
            connection={"url": "https://example.test/mcp"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            spec = default_job_spec("Notes", target=target)
            spec.agent["runtime"] = {"backend": "claude", "model": "sonnet"}
            spec_path = create_job("Notes", spec, jobs_root=Path(tmp) / "jobs")
            written = materialize_job_runners(spec_path)
            aut = json.loads(written["aut"].read_text())
        self.assertEqual(aut["parser"], "claude-json")
        self.assertEqual(aut["command"][:2], ["claude", "-p"])
        config = json.loads(aut["command"][aut["command"].index("--mcp-config") + 1])
        self.assertEqual(config["mcpServers"]["notes"]["url"], "https://example.test/mcp")
        self.assertIn("mcp__notes", aut["command"])
        self.assertEqual(aut["sandbox"]["backend"], "openshell")

    def test_config_view_names_the_claude_backend(self) -> None:
        view = _describe_runner(
            {"command": ["claude", "-p"], "parser": "claude-json"}, {}, "runners/aut.json"
        )
        self.assertEqual((view["backend"], view["kind"]), ("claude", "process"))


if __name__ == "__main__":
    unittest.main()
