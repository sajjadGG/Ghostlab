"""Tests for sandboxing a local stdio MCP: uploads, preflight, and cleanup."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rehearsal.config import TargetConfig
from rehearsal.sandbox import (
    OpenShellSandbox,
    SandboxError,
    auto_uploads_for_command,
    normalize_sandbox,
    preflight_stdio_command,
    sandbox_stdio_target,
)


class FakeRun:
    """Records openshell invocations and replays scripted results."""

    def __init__(self, missing: "list[str] | None" = None) -> None:
        self.calls: list[list[str]] = []
        self.missing = missing or []

    def __call__(self, command, **kwargs):
        self.calls.append([str(part) for part in command])
        stdout = ""
        if "-c" in command:  # the preflight shell probe
            stdout = "".join(f"MISSING:{name}\n" for name in self.missing)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def command_containing(self, token: str) -> "list[str] | None":
        for call in self.calls:
            if token in call:
                return call
        return None


class AutoUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.server = self.tmp / "server" / "index.js"
        self.server.parent.mkdir()
        self.server.write_text("// server", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_local_program_directory_is_uploaded(self) -> None:
        uploads = auto_uploads_for_command(["node", str(self.server)], [])
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]["source"], str(self.server.parent.resolve()))
        self.assertEqual(uploads[0]["target"], "/sandbox/mcp")

    def test_bare_binaries_and_missing_paths_are_left_alone(self) -> None:
        uploads = auto_uploads_for_command(["node", "--flag", "/nope/x.js"], [])
        self.assertEqual(uploads, [])

    def test_paths_already_covered_are_not_duplicated(self) -> None:
        existing = [{"source": str(self.tmp), "target": "/sandbox"}]
        self.assertEqual(auto_uploads_for_command(["node", str(self.server)], existing), [])

    def test_uploads_the_package_root_not_just_the_entry_directory(self) -> None:
        """`build/index.js` needs the `node_modules` beside `package.json`."""
        pkg = self.tmp / "chart-mcp"
        (pkg / "build").mkdir(parents=True)
        (pkg / "node_modules").mkdir()
        (pkg / "package.json").write_text("{}", encoding="utf-8")
        entry = pkg / "build" / "index.js"
        entry.write_text("// entry", encoding="utf-8")

        uploads = auto_uploads_for_command(["node", str(entry)], [])
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]["source"], str(pkg.resolve()))

    def test_interpreter_paths_are_never_uploaded(self) -> None:
        """A venv interpreter would otherwise drag in the whole enclosing repo."""
        venv_bin = self.tmp / "repo" / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (self.tmp / "repo" / "pyproject.toml").write_text("", encoding="utf-8")
        interpreter = venv_bin / "python3.13"
        interpreter.write_text("", encoding="utf-8")
        script = self.tmp / "repo" / "server.py"
        script.write_text("", encoding="utf-8")

        uploads = auto_uploads_for_command([str(interpreter), str(script)], [])
        sources = [item["source"] for item in uploads]
        self.assertNotIn(str(venv_bin.resolve()), sources)
        # The script still travels, rooted at its project.
        self.assertEqual(sources, [str((self.tmp / "repo").resolve())])


class PreflightTest(unittest.TestCase):
    def _sandbox(self, runner: FakeRun) -> OpenShellSandbox:
        sandbox = OpenShellSandbox({"bin": "/bin/openshell"}, role="t", run=runner)
        sandbox.created = True
        return sandbox

    def test_missing_program_raises_a_classified_error(self) -> None:
        runner = FakeRun(missing=["/srv/index.js"])
        with self.assertRaises(SandboxError) as ctx:
            preflight_stdio_command(self._sandbox(runner), ["node", "/srv/index.js"])
        self.assertEqual(ctx.exception.kind, "sandbox_command_missing")
        self.assertIn("/srv/index.js", ctx.exception.detail)
        self.assertIn("--sandbox local", ctx.exception.detail)

    def test_present_program_passes(self) -> None:
        preflight_stdio_command(self._sandbox(FakeRun()), ["node", "/srv/index.js"])

    def test_empty_command_is_a_no_op(self) -> None:
        runner = FakeRun()
        preflight_stdio_command(self._sandbox(runner), [])
        self.assertEqual(runner.calls, [])


class SandboxStdioTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.server = self.tmp / "server" / "index.js"
        self.server.parent.mkdir()
        self.server.write_text("// server", encoding="utf-8")
        self.target = TargetConfig(
            id="safari", transport="stdio",
            connection={"command": ["node", str(self.server)]},
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _wrap(self, runner: FakeRun):
        config = normalize_sandbox({"backend": "openshell", "bin": "/bin/openshell"}, None)
        with patch("rehearsal.sandbox._default_run", runner):
            return sandbox_stdio_target(self.target, config, role="test")

    def test_program_is_uploaded_and_command_rewritten(self) -> None:
        runner = FakeRun()
        rewritten, sandbox = self._wrap(runner)
        create = runner.command_containing("create")
        self.assertIsNotNone(create)
        self.assertIn(f"{self.server.parent.resolve()}:/sandbox/mcp", create)
        # Gitignore filtering would silently drop node_modules/.venv.
        self.assertIn("--no-git-ignore", create)
        # SSH, not `sandbox exec`: exec buffers stdin until EOF and would
        # deadlock a persistent stdio MCP session.
        wrapped = rewritten.connection["command"]
        self.assertEqual(wrapped[0], "ssh")
        self.assertIn("-T", wrapped)
        remote = wrapped[-1]
        # The command must point inside the sandbox, not at the host path.
        self.assertIn("/sandbox/mcp/server/index.js", remote)
        self.assertNotIn(str(self.server), remote)

    def test_env_and_workdir_travel_in_the_remote_command(self) -> None:
        self.target = TargetConfig(
            id="safari", transport="stdio",
            connection={"command": ["node", str(self.server)], "env": {"TOKEN": "s3cret"}},
        )
        config = normalize_sandbox(
            {
                "backend": "openshell", "bin": "/bin/openshell",
                "workdir": "/sandbox/app", "env_allowlist": ["TOKEN"],
            },
            None,
        )
        with patch("rehearsal.sandbox._default_run", FakeRun()):
            rewritten, _ = sandbox_stdio_target(self.target, config, role="test")
        remote = rewritten.connection["command"][-1]
        self.assertIn("cd /sandbox/app &&", remote)
        self.assertIn("TOKEN=s3cret", remote)

    def test_env_outside_the_allowlist_is_dropped(self) -> None:
        self.target = TargetConfig(
            id="safari", transport="stdio",
            connection={"command": ["node", str(self.server)], "env": {"SECRET": "nope"}},
        )
        config = normalize_sandbox({"backend": "openshell", "bin": "/bin/openshell"}, None)
        with patch("rehearsal.sandbox._default_run", FakeRun()):
            rewritten, _ = sandbox_stdio_target(self.target, config, role="test")
        self.assertNotIn("nope", rewritten.connection["command"][-1])

    def test_local_backend_is_untouched(self) -> None:
        target, sandbox = sandbox_stdio_target(
            self.target, {"backend": "local"}, role="test"
        )
        self.assertIs(target, self.target)
        self.assertIsNone(sandbox)

    def test_failed_preflight_deletes_the_sandbox(self) -> None:
        """A raised preflight hands back no handle, so it must clean up itself."""
        runner = FakeRun(missing=["/sandbox/mcp/server/index.js"])
        with self.assertRaises(SandboxError):
            self._wrap(runner)
        self.assertIsNotNone(runner.command_containing("delete"))


if __name__ == "__main__":
    unittest.main()
