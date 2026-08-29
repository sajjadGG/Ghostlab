from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rehearsal.config import RunnerConfig, TargetConfig
from rehearsal.runners import OpenShellProcessRunner, create_runner
from rehearsal.sandbox import (
    OpenShellSandbox, SandboxError, normalize_sandbox, sandbox_stdio_target,
)
from rehearsal.session_provenance import (
    CODEX_ORIGINATOR_ENV,
    GHOSTLAB_ORIGINATOR,
)


class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, command, *, input=None, **kwargs):
        self.calls.append((list(command), input))
        stdout = "agent reply" if command[1:3] == ["sandbox", "exec"] else ""
        if command[1] == "logs":
            stdout = "action=deny dst_host=example.com"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


class OpenShellSandboxTest(unittest.TestCase):
    def test_normalizes_uploads_and_requires_explicit_network_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "agent").mkdir()
            config = normalize_sandbox(
                {"uploads": [{"source": "agent", "target": "/sandbox/workspace"}]}, root
            )
            self.assertEqual(config["backend"], "openshell")
            self.assertEqual(config["uploads"][0]["source"], str((root / "agent").resolve()))
        with self.assertRaises(SandboxError):
            normalize_sandbox({"network": "enabled"})
        with self.assertRaises(SandboxError):
            normalize_sandbox({"network": "policy"})

    def test_lifecycle_exec_env_allowlist_logs_and_cleanup(self) -> None:
        fake = FakeCommands()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"ALLOWED_TOKEN": "secret", "BLOCKED_TOKEN": "nope"}
        ):
            sandbox = OpenShellSandbox(
                {
                    "bin": "openshell", "image": "base", "env_allowlist": ["ALLOWED_TOKEN"],
                    "artifact_dir": tmp, "keep": False, "workdir": "/sandbox/workspace",
                },
                role="aut", run=fake,
            )
            result = sandbox.exec(
                ["codex", "exec", "-"], input_text="hello",
                env={
                    "BLOCKED_TOKEN": "nope",
                    "REHEARSAL_TARGET_ID": "target",
                    CODEX_ORIGINATOR_ENV: GHOSTLAB_ORIGINATOR,
                },
                timeout=10,
            )
            self.assertEqual(result.stdout, "agent reply")
            create_command = next(call[0] for call in fake.calls if call[0][1:3] == ["sandbox", "create"])
            self.assertNotIn("--keep", create_command)
            self.assertNotIn("--no-keep", create_command)
            exec_command = next(call[0] for call in fake.calls if call[0][1:3] == ["sandbox", "exec"])
            rendered = " ".join(exec_command)
            self.assertIn("ALLOWED_TOKEN=secret", rendered)
            self.assertIn("REHEARSAL_TARGET_ID=target", rendered)
            self.assertIn(
                f"{CODEX_ORIGINATOR_ENV}={GHOSTLAB_ORIGINATOR}",
                rendered,
            )
            self.assertNotIn("BLOCKED_TOKEN", rendered)
            sandbox.close()
            self.assertTrue((Path(tmp) / "openshell-aut.log").exists())
            self.assertTrue(any(call[0][1:3] == ["sandbox", "delete"] for call in fake.calls))

    def test_runner_routes_process_turn_through_openshell(self) -> None:
        fake = FakeCommands()
        config = RunnerConfig(
            kind="process", command=["agent", "--prompt"], sandbox={"backend": "openshell", "bin": "openshell"}
        )
        runner = create_runner(config, "aut")
        self.assertIsInstance(runner, OpenShellProcessRunner)
        runner.sandbox.run = fake
        result = runner.run_turn("hello")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "agent reply")
        self.assertEqual(next(value for command, value in fake.calls if command[1:3] == ["sandbox", "exec"]), "hello")

    def test_missing_runtime_has_actionable_error(self) -> None:
        with patch("rehearsal.sandbox.shutil.which", return_value=None):
            sandbox = OpenShellSandbox({}, role="aut")
            with self.assertRaises(SandboxError) as ctx:
                sandbox.create()
        self.assertEqual(ctx.exception.kind, "sandbox_runtime_missing")
        self.assertIn("sandbox.backend: local", str(ctx.exception))

    def test_gateway_setup_failure_is_classified_and_cleaned_up(self) -> None:
        calls = []

        def failing(command, **kwargs):
            calls.append(list(command))
            if command[1:3] == ["sandbox", "create"]:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="gateway connection refused")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        sandbox = OpenShellSandbox({"bin": "openshell"}, run=failing)
        with self.assertRaises(SandboxError) as ctx:
            sandbox.create()
        self.assertEqual(ctx.exception.kind, "sandbox_gateway_unavailable")
        self.assertTrue(any(command[1:3] == ["sandbox", "delete"] for command in calls))

    def test_stdio_target_command_is_rewritten_into_uploaded_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            server = root / "server.py"
            server.write_text("print('x')", encoding="utf-8")
            target = TargetConfig(
                id="m", transport="stdio",
                connection={"command": ["python", str(server)], "args": [], "env": {}},
            )
            config = {
                "backend": "openshell", "bin": "openshell", "workdir": "/sandbox/project",
                "uploads": [{"source": str(root), "target": "/sandbox"}],
            }
            # `sandbox_stdio_target` builds its own sandbox and shells out to
            # openshell (pre-flight, then `ssh-config`), so the run function is
            # stubbed too — otherwise this unit test would need the real CLI
            # installed, which CI does not have.
            with patch.object(OpenShellSandbox, "create", return_value=None), \
                    patch("rehearsal.sandbox._default_run", FakeCommands()):
                rewritten, _sandbox = sandbox_stdio_target(target, config, role="direct")
        rendered = " ".join(rewritten.connection["command"])
        self.assertIn("/sandbox/project/server.py", rendered)
        self.assertNotIn(str(server), rendered)
