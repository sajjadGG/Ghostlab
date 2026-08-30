"""`ghostlab artifact-run`: one agent, one turn, exported state.

The sandbox runtime is faked at the `openshell` subprocess boundary (see
`openshell_fake`), so the runner, the pre-close export, the canonical archive,
and the manifest all execute for real.
"""
from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openshell_fake import FakeOpenShell

from rehearsal.artifact_run import (
    STATUS_AGENT_ERROR,
    STATUS_COMPLETED,
    STATUS_EXPORT_FAILED,
    STATUS_HARNESS_ERROR,
    STATUS_MODEL_UNAVAILABLE,
    STATUS_OUTPUT_CONTRACT_FAILED,
    STATUS_SANDBOX_ERROR,
    STATUS_TIMED_OUT,
    ArtifactRunError,
    run_artifact,
)
from rehearsal.config import ConfigError, artifact_run_config, parse_export
from rehearsal.runners import AgentRunner, OpenShellProcessRunner, create_runner
from rehearsal.sandbox import SandboxError
from rehearsal.workspace_export import workspace_state_hash

WRITE_SCRIPT = """\
import json, os, pathlib, sys
prompt = sys.stdin.read()
root = pathlib.Path(os.environ.get("GHOSTLAB_FAKE_SANDBOX_ROOT", "/"))
workspace = pathlib.Path.cwd()
(workspace / "feature.py").write_text("def feature():\\n    return 1\\n", encoding="utf-8")
(workspace / "README.md").write_text("edited by the agent\\n", encoding="utf-8")
out = root / "sandbox" / "output"
out.mkdir(parents=True, exist_ok=True)
(out / "task-definitions.json").write_text(
    json.dumps({"schema_version": "v1", "tasks": [{"id": "t1", "prompt": prompt.strip()}]}),
    encoding="utf-8",
)
print("done")
"""

CONTRACT = {
    "type": "object",
    "required": ["schema_version", "tasks"],
    "properties": {
        "schema_version": {"const": "v1"},
        "tasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "prompt"],
                "properties": {"id": {"type": "string"}, "prompt": {"type": "string"}},
            },
        },
    },
}


class ArtifactRunTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.fake = FakeOpenShell(self.tmp / "sandboxes")

        self.workspace = self.tmp / "repo"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("base\n", encoding="utf-8")
        (self.workspace / "pkg").mkdir()
        (self.workspace / "pkg" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
        cache = self.workspace / "pkg" / "__pycache__"
        cache.mkdir()
        (cache / "core.pyc").write_bytes(b"\x00cached")
        subprocess.run(["git", "init", "-q", str(self.workspace)], check=True)
        subprocess.run(
            ["git", "-C", str(self.workspace), "add", "README.md", "pkg/core.py"], check=True
        )
        subprocess.run(
            [
                "git", "-C", str(self.workspace),
                "-c", "user.email=t@example.com", "-c", "user.name=t",
                "commit", "-qm", "base",
            ],
            check=True,
        )

        self.script = self.tmp / "agent.py"
        self.script.write_text(WRITE_SCRIPT, encoding="utf-8")
        self.agent_path = self.tmp / "agent.json"
        self.write_agent()
        self.run_dir = self.tmp / "run"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_agent(self, **overrides) -> None:
        agent = {
            "id": "candidate",
            "name": "candidate",
            "runner": {
                "kind": "process",
                "command": ["python3", "/sandbox/agent/agent.py"],
                "prompt_mode": "stdin",
                "parser": "text",
                "timeout_seconds": 60,
            },
            "sandbox": {
                "backend": "openshell",
                "bin": "openshell",
                "image": "base",
                "uploads": [{"source": str(self.tmp / "agent"), "target": "/sandbox"}],
            },
        }
        agent_dir = self.tmp / "agent"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "agent.py").write_text(self.script.read_text(encoding="utf-8"), "utf-8")
        agent.update(overrides)
        self.agent_path.write_text(json.dumps(agent, indent=2), encoding="utf-8")

    def config(self, **overrides):
        options = {
            "agent": self.agent_path,
            "run_dir": self.run_dir,
            "prompt": "Implement the feature.",
            "workspace": self.workspace,
            "export_workspace": "candidate-state.tar.zst",
        }
        options.update(overrides)
        return artifact_run_config(**options)

    def run_with_fake(self, config=None, **overrides):
        with patch("rehearsal.sandbox._default_run", self.fake):
            return run_artifact(config or self.config(**overrides))


class ArtifactRunTest(ArtifactRunTestCase):
    def test_runs_exactly_one_agent_runner_and_no_user_emulator(self) -> None:
        created: list[str] = []

        def factory(runner_config, name):
            created.append(name)
            return create_runner(runner_config, name)

        with patch("rehearsal.sandbox._default_run", self.fake):
            result = run_artifact(self.config(), runner_factory=factory)

        self.assertEqual(created, ["aut"])
        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertIs(result.manifest["runner"]["user_emulator"], False)
        # One sandbox, one turn: nothing emulates a second participant.
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(len(self.fake.execs), 2)  # the agent turn, then the export

    def test_agent_can_modify_only_the_sandbox_copy(self) -> None:
        before = workspace_state_hash(self.workspace)
        result = self.run_with_fake()

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertFalse((self.workspace / "feature.py").exists())
        self.assertEqual((self.workspace / "README.md").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(workspace_state_hash(self.workspace), before)
        self.assertEqual(result.manifest["workspace_input_sha256"], before)
        self.assertNotEqual(result.manifest["workspace_output_sha256"], before)

    def test_workspace_export_keeps_changed_and_untracked_and_drops_git_and_caches(self) -> None:
        result = self.run_with_fake()
        status = json.loads(
            (self.run_dir / "workspace-export" / "status.json").read_text(encoding="utf-8")
        )
        paths = [entry["path"] for entry in status["files"]]

        self.assertIn("feature.py", paths)  # untracked, created by the agent
        self.assertIn("README.md", paths)  # tracked and changed
        self.assertIn("pkg/core.py", paths)  # tracked and unchanged
        self.assertFalse([path for path in paths if path.startswith(".git/")])
        self.assertFalse([path for path in paths if "__pycache__" in path])

        untracked = json.loads(
            (self.run_dir / "workspace-export" / "untracked.json").read_text(encoding="utf-8")
        )
        self.assertIn("feature.py", untracked["untracked"])
        self.assertIn("README.md", untracked["changed"])
        diff = (self.run_dir / "workspace-export" / "diff.patch").read_text(encoding="utf-8")
        self.assertIn("edited by the agent", diff)

        archive = self.run_dir / str(result.manifest["workspace_export"]["archive"])
        self.assertTrue(archive.exists())
        with tarfile.open(archive) as handle:
            members = handle.getnames()
        self.assertIn("feature.py", members)
        self.assertFalse([name for name in members if name.startswith(".git/")])
        self.assertEqual(
            sorted(members), sorted(paths), "archive and status.json must describe one state"
        )

    def test_archive_is_deterministic_across_runs(self) -> None:
        first = self.run_with_fake()
        second = self.run_with_fake(run_dir=self.tmp / "run2")
        self.assertEqual(
            first.manifest["workspace_output_sha256"],
            second.manifest["workspace_output_sha256"],
        )
        name = str(first.manifest["workspace_export"]["archive"])
        self.assertEqual(
            (self.run_dir / name).read_bytes(), (self.tmp / "run2" / name).read_bytes()
        )

    def test_declared_json_output_is_schema_validated(self) -> None:
        contract = self.tmp / "contract.json"
        contract.write_text(json.dumps(CONTRACT), encoding="utf-8")
        result = self.run_with_fake(
            exports=["/sandbox/output/task-definitions.json=task-definitions.json"],
            output_contract=contract,
        )
        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertEqual(result.manifest["output_contract"], str(contract))
        exported = json.loads(
            (self.run_dir / "task-definitions.json").read_text(encoding="utf-8")
        )
        self.assertEqual(exported["tasks"][0]["prompt"], "Implement the feature.")

    def test_schema_violation_is_its_own_status(self) -> None:
        contract = self.tmp / "contract.json"
        contract.write_text(
            json.dumps({**CONTRACT, "required": ["schema_version", "tasks", "absent"]}),
            encoding="utf-8",
        )
        result = self.run_with_fake(
            exports=["/sandbox/output/task-definitions.json=task-definitions.json"],
            output_contract=contract,
        )
        self.assertEqual(result.status, STATUS_OUTPUT_CONTRACT_FAILED)
        self.assertTrue(result.manifest["contract_errors"])
        self.assertIn("absent", result.manifest["contract_errors"][0])

    def test_timeout_model_outage_and_export_failure_are_distinct(self) -> None:
        self.fake.timeout_on = "agent.py"
        timed_out = self.run_with_fake(run_dir=self.tmp / "timeout")
        self.assertEqual(timed_out.status, STATUS_TIMED_OUT)
        self.assertTrue(timed_out.manifest["timed_out"])
        self.assertIsNone(timed_out.manifest.get("score"))

        self.fake.timeout_on = ""
        self.fake.exec_hook = lambda name, argv, text, root: (
            subprocess.CompletedProcess(argv, 1, stdout="", stderr="opencode error: quota exceeded")
            if argv and argv[-1].endswith("agent.py")
            else None
        )
        outage = self.run_with_fake(run_dir=self.tmp / "outage")
        self.assertEqual(outage.status, STATUS_MODEL_UNAVAILABLE)

        self.fake.exec_hook = lambda name, argv, text, root: (
            subprocess.CompletedProcess(argv, 3, stdout="", stderr="traceback: boom")
            if argv and argv[-1].endswith("agent.py")
            else None
        )
        failed = self.run_with_fake(run_dir=self.tmp / "agent-error")
        self.assertEqual(failed.status, STATUS_AGENT_ERROR)

        self.fake.exec_hook = None
        self.fake.fail_download = "status.json"
        export_failed = self.run_with_fake(run_dir=self.tmp / "export")
        self.assertEqual(export_failed.status, STATUS_EXPORT_FAILED)
        self.assertIn("status.json", export_failed.manifest["export_error"])

    def test_sandbox_failure_is_never_reported_as_an_agent_failure(self) -> None:
        self.fake.fail_create = "gateway connection refused"
        result = self.run_with_fake(run_dir=self.tmp / "gateway")
        self.assertEqual(result.status, STATUS_SANDBOX_ERROR)
        self.assertEqual(result.manifest["exit_code"], 125)

    def test_agent_prose_about_quotas_is_not_a_model_outage(self) -> None:
        # The agent's own transcript legitimately mentions provider vocabulary
        # when it is editing code that talks to providers.
        self.fake.exec_hook = lambda name, argv, text_, root: (
            subprocess.CompletedProcess(
                argv, 1,
                stdout="I updated the retry path for HTTP 503 and quota errors, then failed.",
                stderr="AssertionError: expected 2 got 1",
            )
            if argv and argv[-1].endswith("agent.py")
            else None
        )
        result = self.run_with_fake(run_dir=self.tmp / "prose")
        self.assertEqual(result.status, STATUS_AGENT_ERROR)

    def test_export_io_failure_is_an_export_failure(self) -> None:
        with patch(
            "rehearsal.artifact_run.sha256_path", side_effect=[("a" * 64), OSError("disk full")]
        ):
            result = self.run_with_fake(run_dir=self.tmp / "export-io")
        self.assertEqual(result.status, STATUS_EXPORT_FAILED)
        self.assertIn("disk full", result.manifest["export_error"])
        self.assertEqual(self.fake.deleted, self.fake.created)

    def test_a_harness_failure_still_writes_the_manifest_and_closes_the_sandbox(self) -> None:
        with patch(
            "rehearsal.artifact_run._parse_calls", side_effect=RuntimeError("harness bug")
        ):
            result = self.run_with_fake(run_dir=self.tmp / "harness")
        self.assertEqual(result.status, STATUS_HARNESS_ERROR)
        self.assertIn("harness bug", result.manifest["error"])
        self.assertTrue(result.manifest_path.exists())
        # The manifest is the only record the attempt happened, and the sandbox
        # must not outlive it.
        self.assertEqual(self.fake.deleted, self.fake.created)

    def test_missing_declared_export_fails_the_run(self) -> None:
        result = self.run_with_fake(
            exports=["/sandbox/output/absent.json=absent.json"], export_workspace=""
        )
        self.assertEqual(result.status, STATUS_EXPORT_FAILED)
        self.assertFalse((self.run_dir / "absent.json").exists())

    def test_export_happens_before_the_sandbox_is_deleted(self) -> None:
        result = self.run_with_fake(
            exports=["/sandbox/output/task-definitions.json=task-definitions.json"]
        )
        self.assertEqual(result.status, STATUS_COMPLETED)
        verbs = [call[1:3] for call in self.fake.calls]
        last_download = max(index for index, verb in enumerate(verbs) if verb == ["sandbox", "download"])
        delete = next(index for index, verb in enumerate(verbs) if verb == ["sandbox", "delete"])
        self.assertLess(last_download, delete)
        self.assertEqual(self.fake.deleted, self.fake.created)

    def test_manifest_records_hashes_prompt_and_events(self) -> None:
        result = self.run_with_fake()
        manifest = result.manifest
        self.assertEqual(manifest["schema_version"], "ghostlab-artifact-run-v1")
        self.assertEqual(manifest["exit_code"], 0)
        self.assertEqual(len(manifest["prompt_sha256"]), 64)
        self.assertEqual(len(manifest["agent_config_sha256"]), 64)
        self.assertTrue(manifest["exports"])
        events = [
            json.loads(line)
            for line in (self.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [event["type"] for event in events][:2],
            ["artifact_run.started", "agent.prompt"],
        )
        self.assertEqual(events[-1]["data"]["status"], STATUS_COMPLETED)
        self.assertEqual(
            (self.run_dir / "prompt.txt").read_text(encoding="utf-8"), "Implement the feature."
        )


class ArtifactRunConfigTest(ArtifactRunTestCase):
    def test_export_specification_parsing(self) -> None:
        self.assertEqual(parse_export("/sandbox/output/x.json=x.json"), ("/sandbox/output/x.json", "x.json"))
        self.assertEqual(parse_export("/sandbox/output/x.json"), ("/sandbox/output/x.json", "x.json"))
        with self.assertRaises(ConfigError):
            parse_export("relative/x.json=x.json")
        with self.assertRaises(ConfigError):
            parse_export("/sandbox/x.json=../escape.json")

    def test_prompt_and_contract_are_required_to_be_usable(self) -> None:
        with self.assertRaises(ConfigError):
            artifact_run_config(agent=self.agent_path, run_dir=self.run_dir, prompt="   ")
        with self.assertRaises(ConfigError):
            artifact_run_config(
                agent=self.agent_path, run_dir=self.run_dir, prompt="x",
                output_contract=self.tmp / "absent.json",
            )
        contract = self.tmp / "contract.json"
        contract.write_text(json.dumps(CONTRACT), encoding="utf-8")
        with self.assertRaises(ConfigError):
            artifact_run_config(
                agent=self.agent_path, run_dir=self.run_dir, prompt="x", output_contract=contract
            )

    def test_local_sandbox_is_refused(self) -> None:
        self.write_agent(sandbox={"backend": "local"})
        with self.assertRaises(ArtifactRunError) as ctx:
            self.run_with_fake()
        self.assertIn("OpenShell", str(ctx.exception))

    def test_agent_without_a_runner_command_is_refused(self) -> None:
        self.agent_path.write_text(json.dumps({"id": "x"}), encoding="utf-8")
        with self.assertRaises(ArtifactRunError) as ctx:
            self.run_with_fake()
        self.assertIn("runner command", str(ctx.exception))

    def test_missing_workspace_is_refused(self) -> None:
        with self.assertRaises(ArtifactRunError):
            self.run_with_fake(workspace=None)


class ArtifactRunCliTest(ArtifactRunTestCase):
    def test_cli_wires_every_flag_and_reports_the_status_in_its_exit_code(self) -> None:
        from rehearsal.cli import main

        contract = self.tmp / "contract.json"
        contract.write_text(json.dumps(CONTRACT), encoding="utf-8")
        argv = [
            "artifact-run",
            "--agent", str(self.agent_path),
            "--workspace", str(self.workspace),
            "--prompt", "Implement the feature.",
            "--export", "/sandbox/output/task-definitions.json=task-definitions.json",
            "--export-workspace", "candidate-state.tar.zst",
            "--output-contract", str(contract),
            "--run-dir", str(self.run_dir),
        ]
        with patch("rehearsal.sandbox._default_run", self.fake):
            self.assertEqual(main(argv), 0)

        manifest = json.loads((self.run_dir / "artifact-run.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], STATUS_COMPLETED)
        self.assertEqual(
            sorted(export["path"] for export in manifest["exports"]),
            sorted([manifest["workspace_export"]["archive"], "task-definitions.json"]),
        )

        self.fake.exec_hook = lambda name, argv_, text_, root: (
            subprocess.CompletedProcess(argv_, 4, stdout="", stderr="boom")
            if argv_ and argv_[-1].endswith("agent.py")
            else None
        )
        with patch("rehearsal.sandbox._default_run", self.fake):
            self.assertEqual(main([*argv[:-1], str(self.tmp / "run-failing")]), 1)


class RunnerExportHookTest(unittest.TestCase):
    def test_non_sandboxed_runners_report_export_as_unsupported(self) -> None:
        runner = AgentRunner()
        with self.assertRaises(SandboxError) as ctx:
            runner.export_artifact("/sandbox/output/x.json", Path("x.json"))
        self.assertEqual(ctx.exception.kind, "export_unsupported")
        with self.assertRaises(SandboxError):
            runner.export_workspace(destination=Path("."))

    def test_legacy_dispatch_still_leaves_opencode_runners_on_the_host(self) -> None:
        # The job flows wrap opencode in OpenShell themselves, so `create_runner`
        # must not promote it a second time just because a backend is declared.
        from rehearsal.config import RunnerConfig
        from rehearsal.runners import OpencodeProcessRunner

        for parser in ("opencode-json", "opencode-text"):
            runner = create_runner(
                RunnerConfig(
                    kind="process",
                    command=["opencode", "run"],
                    parser=parser,
                    sandbox={"backend": "openshell", "bin": "openshell"},
                ),
                "aut",
            )
            self.assertIsInstance(runner, OpencodeProcessRunner)
            self.assertNotIsInstance(runner, OpenShellProcessRunner)
            self.assertIsNone(runner.sandbox_handle)

    def test_sandboxed_runner_is_an_explicit_opt_in(self) -> None:
        from rehearsal.config import RunnerConfig
        from rehearsal.runners import OpenShellOpencodeProcessRunner, create_sandboxed_runner

        runner = create_sandboxed_runner(
            RunnerConfig(
                kind="process",
                command=["opencode", "run"],
                parser="opencode-json",
                sandbox={"backend": "openshell", "bin": "openshell"},
            ),
            "aut",
        )
        self.assertIsInstance(runner, OpenShellOpencodeProcessRunner)
        self.assertIsNotNone(runner.sandbox_handle)

        with self.assertRaises(ValueError) as ctx:
            create_sandboxed_runner(
                RunnerConfig(kind="process", command=["x"], sandbox={"backend": "local"}), "aut"
            )
        self.assertIn("openshell", str(ctx.exception))

    def test_artifact_run_defaults_to_the_sandboxed_factory(self) -> None:
        import inspect

        from rehearsal.runners import create_sandboxed_runner

        default = inspect.signature(run_artifact).parameters["runner_factory"].default
        self.assertIs(default, create_sandboxed_runner)


if __name__ == "__main__":
    unittest.main()
