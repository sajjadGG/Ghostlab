from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rehearsal.config import RunnerConfig, TargetConfig
from rehearsal.runners import OpenShellProcessRunner, create_runner
from rehearsal.sandbox import (
    WORKSPACE_RUNTIME_SUMMARY_PREFIX,
    OpenShellSandbox,
    SandboxError,
    normalize_sandbox,
    sandbox_stdio_target,
    verify_workspace_export_runtime,
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


class LocalRuntimeProbe:
    def __init__(self) -> None:
        self.config = {}

    def exec(self, command, *, input_text, **kwargs):
        return subprocess.run(
            ["/usr/bin/python3", *command[1:]],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )


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

    def test_post_create_upload_disables_gitignore_filtering(self) -> None:
        fake = FakeCommands()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "ignored.txt"
            source.write_text("secret", encoding="utf-8")
            sandbox = OpenShellSandbox({"bin": "openshell"}, run=fake)
            sandbox.created = True
            sandbox.upload_file(source, "/sandbox/private/renamed.txt", mode="600")

        upload = next(call for call, _ in fake.calls if call[1:3] == ["sandbox", "upload"])
        self.assertIn("--no-git-ignore", upload)
        self.assertIn("/sandbox/private/renamed.txt", upload)
        commands = [call for call, _ in fake.calls if call[1:3] == ["sandbox", "exec"]]
        self.assertTrue(any("/bin/mkdir" in call for call in commands))
        self.assertTrue(any("/usr/bin/test" in call for call in commands))
        self.assertTrue(any("chmod" in " ".join(call) for call in commands))

    def test_post_create_upload_surfaces_missing_destination(self) -> None:
        def missing_destination(command, **kwargs):
            if command[1:3] == ["sandbox", "exec"] and "/usr/bin/test" in command:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="denied")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            source.write_text("value", encoding="utf-8")
            sandbox = OpenShellSandbox({"bin": "openshell"}, run=missing_destination)
            sandbox.created = True
            with self.assertRaises(SandboxError) as ctx:
                sandbox.upload_file(source, "/sandbox/private/renamed.txt")
        self.assertEqual(ctx.exception.kind, "sandbox_upload_failed")

    def test_workspace_runtime_rejects_writable_files_and_policy(self) -> None:
        class RuntimeProbe:
            def __init__(self, config, result):
                self.config = config
                self.result = result

            def exec(self, command, **kwargs):
                return self.result

        with tempfile.TemporaryDirectory() as tmp:
            writable_path = Path(tmp) / "python3"
            writable_path.write_text("not trusted", encoding="utf-8")

            with self.assertRaises(SandboxError) as ctx:
                verify_workspace_export_runtime(
                    LocalRuntimeProbe(), python=str(writable_path)
                )
            self.assertEqual(ctx.exception.kind, "sandbox_runtime_untrusted")
            self.assertIn(str(writable_path), ctx.exception.detail)

            policy = Path(tmp) / "writable-runtime.yaml"
            policy.write_text(
                "version: 1\n"
                "filesystem_policy:\n"
                "  include_workdir: true\n"
                "  read_only: [/lib]\n"
                "  read_write: [/sandbox, /usr]\n",
                encoding="utf-8",
            )
            reported = WORKSPACE_RUNTIME_SUMMARY_PREFIX + json.dumps(
                {"paths": ["/usr/bin/python3", "/usr/lib/python3.11"], "uid": 998}
            )
            insecure_policy = RuntimeProbe(
                {"policy": str(policy)},
                subprocess.CompletedProcess([], 0, stdout=reported, stderr=""),
            )
            with self.assertRaises(SandboxError) as policy_ctx:
                verify_workspace_export_runtime(insecure_policy)
        self.assertEqual(policy_ctx.exception.kind, "sandbox_runtime_untrusted")
        self.assertIn("read-only", policy_ctx.exception.detail)

    def test_workspace_runtime_inventory_covers_all_exporter_dependencies(self) -> None:
        payload = verify_workspace_export_runtime(LocalRuntimeProbe())
        module_names = {Path(path).name.split(".", 1)[0] for path in payload["module_paths"]}

        for dependency in ("argparse", "subprocess", "threading", "typing"):
            self.assertIn(dependency, module_names)
        for module_path in payload["module_paths"]:
            self.assertTrue(
                any(
                    module_path == root or module_path.startswith(root.rstrip("/") + "/")
                    for root in payload["module_roots"]
                ),
                module_path,
            )

    def test_workspace_runtime_policy_rejects_writable_dependency_root(self) -> None:
        class RuntimeProbe:
            def __init__(self, policy: Path) -> None:
                self.config = {"policy": str(policy)}

            def exec(self, command, **kwargs):
                payload = {
                    "module_paths": ["/opt/python/argparse.py"],
                    "module_roots": ["/opt/python"],
                    "paths": [
                        "/usr/bin/python3",
                        "/opt/python",
                        "/opt/python/argparse.py",
                    ],
                    "uid": 998,
                    "zstd": "",
                }
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=WORKSPACE_RUNTIME_SUMMARY_PREFIX + json.dumps(payload),
                    stderr="",
                )

        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "writable-dependency.yaml"
            policy.write_text(
                "version: 1\n"
                "filesystem_policy:\n"
                "  include_workdir: true\n"
                "  read_only: [/usr]\n"
                "  read_write: [/sandbox, /opt/python]\n",
                encoding="utf-8",
            )
            with self.assertRaises(SandboxError) as ctx:
                verify_workspace_export_runtime(RuntimeProbe(policy))

        self.assertEqual(ctx.exception.kind, "sandbox_runtime_untrusted")
        self.assertIn("/opt/python", ctx.exception.detail)

    def test_workspace_runtime_rejects_writable_zstd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary_dir = Path(tmp) / "bin"
            binary_dir.mkdir()
            zstd = binary_dir / "zstd"
            zstd.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            zstd.chmod(0o755)

            with self.assertRaises(SandboxError) as ctx:
                verify_workspace_export_runtime(
                    LocalRuntimeProbe(),
                    search_path=str(binary_dir),
                )

        self.assertEqual(ctx.exception.kind, "sandbox_runtime_untrusted")
        self.assertIn("zstd", ctx.exception.detail)

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
