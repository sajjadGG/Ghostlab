"""`ghostlab artifact-run`: one agent, one turn, exported state.

The sandbox runtime is faked at the `openshell` subprocess boundary (see
`openshell_fake`), so the runner, the pre-close export, the canonical archive,
and the manifest all execute for real.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
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
    path_bytes,
    run_artifact,
    sha256_path,
)
from rehearsal.config import (
    ConfigError,
    artifact_run_config,
    parse_command,
    parse_export,
)
from rehearsal.runners import AgentRunner, OpenShellProcessRunner, create_runner
from rehearsal.sandbox import SandboxError
from rehearsal.workspace_export import (
    SCHEMA_VERSION,
    state_hash,
    verify_export,
    workspace_state_hash,
)


def archive_members(path: Path) -> list[str]:
    if path.name.endswith(".tar.zst"):
        completed = subprocess.run(
            ["zstd", "-q", "-d", "-c", str(path)],
            capture_output=True,
            check=True,
        )
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as handle:
            return handle.getnames()
    with tarfile.open(path, mode="r:*") as handle:
        return handle.getnames()


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

    def empty_workspace_status(self, archive: Path) -> Path:
        excludes = ["__test_exclude__"]
        status = self.tmp / f"{archive.name}.status.json"
        status.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "root": "repo",
                    "excludes": excludes,
                    "retain": [],
                    "file_count": 0,
                    "total_bytes": 0,
                    "state_sha256": state_hash([], excludes, []),
                    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        return status

    def workspace_verification_fixture(self) -> tuple[Path, Path, Path]:
        content = b"streamed workspace\n"
        entry = {
            "path": "payload.txt",
            "kind": "file",
            "mode": "0644",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        raw_tar = self.tmp / "workspace.tar"
        with tarfile.open(raw_tar, "w") as handle:
            member = tarfile.TarInfo(entry["path"])
            member.mode = 0o644
            member.size = len(content)
            handle.addfile(member, io.BytesIO(content))

        archive = self.tmp / "workspace.tar.zst"
        archive.write_bytes(b"zstd process fixture")
        status = self.tmp / "status.json"
        status.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "root": "repo",
                    "excludes": [],
                    "retain": [],
                    "file_count": 1,
                    "total_bytes": len(content),
                    "state_sha256": state_hash([entry], [], []),
                    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "files": [entry],
                }
            ),
            encoding="utf-8",
        )
        return status, archive, raw_tar


class ArtifactRunTest(ArtifactRunTestCase):
    def test_directory_export_hash_is_stable_and_content_sensitive(self) -> None:
        directory = self.tmp / "directory-export"
        (directory / "nested").mkdir(parents=True)
        (directory / "nested" / "a.txt").write_text("a", encoding="utf-8")
        first = sha256_path(directory)

        self.assertEqual(path_bytes(directory), 1)
        self.assertEqual(sha256_path(directory), first)
        (directory / "nested" / "a.txt").write_text("changed", encoding="utf-8")
        self.assertNotEqual(sha256_path(directory), first)

    def test_missing_declared_export_does_not_discard_other_outputs(self) -> None:
        stale_optional = self.run_dir / "not-produced"
        stale_optional.parent.mkdir(parents=True)
        stale_optional.write_text("stale", encoding="utf-8")
        config = artifact_run_config(
            agent=self.agent_path,
            run_dir=self.run_dir,
            prompt="do the work",
            workspace=self.workspace,
            exports=["/sandbox/output/task-definitions.json=task-definitions.json"],
            optional_exports=["/sandbox/output/not-produced=not-produced"],
        )
        result = self.run_with_fake(config=config)

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertTrue((self.run_dir / "task-definitions.json").is_file())
        self.assertFalse(stale_optional.exists())

    def test_startup_cleanup_precedes_config_failure_and_never_follows_symlinks(self) -> None:
        config = self.config(
            exports=["/sandbox/output/task-definitions.json=nested/result.json"],
            optional_exports=["/sandbox/output/not-produced=optional.json"],
        )
        outside = self.tmp / "outside"
        outside.mkdir()
        targets = {
            "prompt": outside / "prompt.txt",
            "required": outside / "result.json",
            "optional": outside / "optional.json",
            "requested": outside / "candidate-state.tar.zst",
            "fallback": outside / "candidate-state.tar.gz",
        }
        for name, target in targets.items():
            target.write_text(f"keep {name}", encoding="utf-8")
        exported_workspace = outside / "workspace-export"
        exported_workspace.mkdir()
        (exported_workspace / "keep.txt").write_text("keep workspace", encoding="utf-8")

        self.run_dir.mkdir()
        (self.run_dir / "prompt.txt").symlink_to(targets["prompt"])
        (self.run_dir / "nested").symlink_to(outside, target_is_directory=True)
        (self.run_dir / "optional.json").symlink_to(targets["optional"])
        (self.run_dir / "candidate-state.tar.zst").symlink_to(targets["requested"])
        (self.run_dir / "candidate-state.tar.gz").symlink_to(targets["fallback"])
        (self.run_dir / "workspace-export").symlink_to(
            exported_workspace, target_is_directory=True
        )
        self.agent_path.write_text(json.dumps({"id": "missing-runner"}), encoding="utf-8")

        with self.assertRaises(ArtifactRunError):
            self.run_with_fake(config=config)

        for relative in (
            "prompt.txt",
            "nested",
            "optional.json",
            "candidate-state.tar.zst",
            "candidate-state.tar.gz",
            "workspace-export",
        ):
            self.assertFalse(os.path.lexists(self.run_dir / relative))
        for name, target in targets.items():
            self.assertEqual(target.read_text(encoding="utf-8"), f"keep {name}")
        self.assertEqual(
            (exported_workspace / "keep.txt").read_text(encoding="utf-8"),
            "keep workspace",
        )

    def test_startup_cleanup_removes_renamed_and_disabled_prior_outputs(self) -> None:
        config = self.config(
            exports=["/sandbox/output/task-definitions.json=new-result.json"],
            export_workspace="",
        )
        self.run_dir.mkdir()
        prior_paths = (
            "old-result.json",
            "old-optional.json",
            "failed-before-recording.json",
            "old-state.tar.zst",
            "old-state.tar.gz",
        )
        for relative in prior_paths:
            (self.run_dir / relative).write_text("stale", encoding="utf-8")
        (self.run_dir / "artifact-run.json").write_text(
            json.dumps(
                {
                    "exports": [{"path": "old-result.json"}],
                    "configured_exports": [
                        "old-result.json",
                        "old-optional.json",
                        "failed-before-recording.json",
                    ],
                    "workspace_archive_candidates": [
                        "old-state.tar.zst",
                        "old-state.tar.gz",
                    ],
                    "workspace_export": {"archive": "old-state.tar.gz"},
                }
            ),
            encoding="utf-8",
        )
        self.agent_path.write_text(json.dumps({"id": "missing-runner"}), encoding="utf-8")

        with self.assertRaises(ArtifactRunError):
            self.run_with_fake(config=config)

        for relative in prior_paths:
            self.assertFalse((self.run_dir / relative).exists())
        self.assertFalse((self.run_dir / "artifact-run.json").exists())

    def test_startup_rejects_unsafe_paths_in_the_previous_manifest(self) -> None:
        outside = self.tmp / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        self.run_dir.mkdir()
        previous = self.run_dir / "artifact-run.json"
        previous.write_text(
            json.dumps({"exports": [{"path": "../outside.txt"}]}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ArtifactRunError, "unsafe prior manifest path"):
            self.run_with_fake()

        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        self.assertTrue(previous.is_file())

    def test_startup_never_reads_a_symlinked_previous_manifest(self) -> None:
        outside = self.tmp / "outside-manifest.json"
        outside.write_text(
            json.dumps({"exports": [{"path": "outside-output.json"}]}),
            encoding="utf-8",
        )
        self.run_dir.mkdir()
        previous = self.run_dir / "artifact-run.json"
        previous.symlink_to(outside)

        with self.assertRaisesRegex(ArtifactRunError, "expected a regular file"):
            self.run_with_fake()

        self.assertTrue(previous.is_symlink())
        self.assertTrue(outside.is_file())

    def test_outputs_replace_stale_symlinks_without_writing_outside_run_dir(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        outside_result = outside / "task-definitions.json"
        outside_result.write_text("do not replace", encoding="utf-8")
        outside_prompt = outside / "prompt.txt"
        outside_prompt.write_text("old prompt", encoding="utf-8")
        outside_requested = outside / "candidate-state.tar.zst"
        outside_requested.write_text("old requested archive", encoding="utf-8")
        outside_fallback = outside / "candidate-state.tar.gz"
        outside_fallback.write_text("old fallback archive", encoding="utf-8")
        outside_workspace = outside / "workspace-export"
        outside_workspace.mkdir()
        (outside_workspace / "keep.txt").write_text("keep", encoding="utf-8")

        self.run_dir.mkdir()
        (self.run_dir / "nested").symlink_to(outside, target_is_directory=True)
        (self.run_dir / "prompt.txt").symlink_to(outside_prompt)
        (self.run_dir / "candidate-state.tar.zst").symlink_to(outside_requested)
        (self.run_dir / "candidate-state.tar.gz").symlink_to(outside_fallback)
        (self.run_dir / "workspace-export").symlink_to(
            outside_workspace, target_is_directory=True
        )

        result = self.run_with_fake(
            exports=["/sandbox/output/task-definitions.json=nested/task-definitions.json"]
        )

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertFalse((self.run_dir / "nested").is_symlink())
        self.assertTrue((self.run_dir / "nested" / "task-definitions.json").is_file())
        self.assertEqual(outside_result.read_text(encoding="utf-8"), "do not replace")
        self.assertEqual(outside_prompt.read_text(encoding="utf-8"), "old prompt")
        self.assertEqual(
            outside_requested.read_text(encoding="utf-8"), "old requested archive"
        )
        self.assertEqual(
            outside_fallback.read_text(encoding="utf-8"), "old fallback archive"
        )
        self.assertEqual(
            (outside_workspace / "keep.txt").read_text(encoding="utf-8"), "keep"
        )

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
        self.assertEqual(
            len(self.fake.execs),
            3,
        )  # exporter preflight, agent turn, then export

    def test_agent_can_modify_only_the_sandbox_copy(self) -> None:
        before = workspace_state_hash(self.workspace)
        result = self.run_with_fake()

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertFalse((self.workspace / "feature.py").exists())
        self.assertEqual((self.workspace / "README.md").read_text(encoding="utf-8"), "base\n")
        self.assertEqual(workspace_state_hash(self.workspace), before)
        self.assertEqual(result.manifest["workspace_input_sha256"], before)
        self.assertNotEqual(result.manifest["workspace_output_sha256"], before)

    def test_task_environment_image_and_setup_run_before_agent(self) -> None:
        marker = self.workspace / "setup.txt"
        config = self.config(
            sandbox_image="project@sha256:" + "a" * 64,
            setup_commands=[
                json.dumps(
                    [
                        "python3",
                        "-c",
                        "from pathlib import Path; Path('setup.txt').write_text('ready')",
                    ]
                )
            ],
        )

        result = self.run_with_fake(config=config)

        self.assertEqual(result.status, STATUS_COMPLETED)
        create = next(call for call in self.fake.calls if call[1:3] == ["sandbox", "create"])
        self.assertEqual(create[create.index("--from") + 1], "project@sha256:" + "a" * 64)
        self.assertEqual(result.manifest["setup_results"][0]["exit_code"], 0)
        self.assertEqual(result.manifest["setup_commands"][0][0], "python3")
        self.assertFalse(marker.exists())
        sandbox = self.fake.created[0]
        self.assertEqual(
            self.fake.read(sandbox, f"/sandbox/{self.workspace.name}/setup.txt"),
            "ready",
        )

    def test_failed_setup_is_a_harness_error_and_skips_agent(self) -> None:
        self.fake.exec_hook = lambda name, argv, text, root: (
            subprocess.CompletedProcess(argv, 7, stdout="", stderr="setup failed")
            if argv == ["false"]
            else None
        )

        result = self.run_with_fake(
            config=self.config(setup_commands=[json.dumps(["false"])])
        )

        self.assertEqual(result.status, STATUS_HARNESS_ERROR)
        self.assertIn("setup command exited 7", result.manifest["error"])
        self.assertFalse(
            any(command[-1].endswith("agent.py") for _name, command in self.fake.execs)
        )

    def test_runtime_trust_is_checked_after_setup_and_before_the_agent(self) -> None:
        setup_finished = False

        def writable_runtime(name, argv, text, root):
            nonlocal setup_finished
            if argv == ["tamper-runtime"]:
                setup_finished = True
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if (
                argv[:4] == ["/usr/bin/python3", "-I", "-S", "-c"]
                and argv[-1] == "/usr/bin/python3"
                and setup_finished
            ):
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr=(
                        "workspace exporter runtime is untrusted: "
                        "/usr/bin/python3 is writable"
                    ),
                )
            return None

        self.fake.exec_hook = writable_runtime
        result = self.run_with_fake(
            config=self.config(setup_commands=[json.dumps(["tamper-runtime"])])
        )

        self.assertEqual(result.status, STATUS_HARNESS_ERROR)
        self.assertIn("sandbox_runtime_untrusted", result.manifest["error"])
        self.assertIn("writable", result.manifest["error"])
        self.assertFalse(
            any(command[-1].endswith("agent.py") for _name, command in self.fake.execs)
        )
        self.assertEqual(self.fake.deleted, self.fake.created)

    def test_declared_export_only_run_still_hashes_the_final_workspace(self) -> None:
        result = self.run_with_fake(
            export_workspace="",
            exports=["/sandbox/output/task-definitions.json=task-definitions.json"],
        )

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertTrue(result.manifest["workspace_output_sha256"])
        self.assertNotEqual(
            result.manifest["workspace_input_sha256"],
            result.manifest["workspace_output_sha256"],
        )

    def test_workspace_export_ignores_candidate_python_module_shadowing(self) -> None:
        (self.workspace / "hashlib.py").write_text(
            "raise RuntimeError('candidate module was imported')\n",
            encoding="utf-8",
        )

        result = self.run_with_fake()

        self.assertEqual(result.status, STATUS_COMPLETED)
        self.assertTrue(result.manifest["workspace_output_sha256"])

    def test_downloaded_workspace_archive_is_verified_against_status(self) -> None:
        result = self.run_with_fake()
        export = result.manifest["workspace_export"]
        status = self.run_dir / export["status"]
        archive = self.run_dir / export["archive"]

        verified = verify_export(status, archive)
        self.assertEqual(verified["state_sha256"], export["state_sha256"])

        payload = json.loads(status.read_text(encoding="utf-8"))
        payload["files"][0]["sha256"] = "0" * 64
        status.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "archive contents"):
            verify_export(status, archive)

    def test_workspace_archive_verification_enforces_expansion_limits(self) -> None:
        result = self.run_with_fake()
        export = result.manifest["workspace_export"]
        status = self.run_dir / export["status"]
        archive = self.run_dir / export["archive"]
        payload = json.loads(status.read_text(encoding="utf-8"))
        payload["total_bytes"] = 1
        status.write_text(json.dumps(payload), encoding="utf-8")

        with (
            patch("rehearsal.workspace_export.MAX_VERIFIED_TOTAL_BYTES", 1),
            patch("rehearsal.workspace_export.MAX_VERIFIED_MEMBER_BYTES", 1),
            self.assertRaisesRegex(ValueError, "too large|expanded-size"),
        ):
            verify_export(status, archive)

    def test_workspace_verification_bounds_pax_and_gnu_metadata_before_parsing(
        self,
    ) -> None:
        archives = []
        for suffix, mode in ((".tar", "w"), (".tar.gz", "w:gz")):
            pax_archive = self.tmp / f"oversized-workspace-pax{suffix}"
            with tarfile.open(pax_archive, mode, format=tarfile.PAX_FORMAT) as handle:
                member = tarfile.TarInfo("feature.py")
                member.pax_headers = {"comment": "x" * 512}
                handle.addfile(member, io.BytesIO())
            archives.append(pax_archive)

        gnu_archive = self.tmp / "oversized-workspace-gnu.tar"
        with tarfile.open(gnu_archive, "w", format=tarfile.GNU_FORMAT) as handle:
            member = tarfile.TarInfo("n" * 512)
            handle.addfile(member, io.BytesIO())
        archives.append(gnu_archive)

        for archive in archives:
            with self.subTest(archive=archive.name):
                status = self.empty_workspace_status(archive)
                with (
                    patch(
                        "rehearsal.workspace_export.MAX_VERIFIED_METADATA_BYTES",
                        128,
                    ),
                    self.assertRaisesRegex(ValueError, "metadata"),
                ):
                    verify_export(status, archive)

        with (
            patch("rehearsal.workspace_export.MAX_VERIFIED_FILES", 1),
            self.assertRaisesRegex(ValueError, "raw member-count"),
        ):
            verify_export(self.empty_workspace_status(archives[0]), archives[0])

    def test_workspace_verification_bounds_cumulative_metadata(self) -> None:
        archive = self.tmp / "cumulative-workspace-metadata.tar"
        with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as handle:
            for name in ("one.txt", "two.txt"):
                member = tarfile.TarInfo(name)
                member.pax_headers = {"comment": "x" * 300}
                handle.addfile(member, io.BytesIO())

        with (
            patch(
                "rehearsal.workspace_export.MAX_VERIFIED_METADATA_BYTES",
                1024,
            ),
            patch(
                "rehearsal.workspace_export.MAX_VERIFIED_TOTAL_METADATA_BYTES",
                512,
            ),
            self.assertRaisesRegex(ValueError, "total size"),
        ):
            verify_export(self.empty_workspace_status(archive), archive)

    def test_workspace_verification_rejects_all_gnu_sparse_forms(self) -> None:
        archives = []
        old_sparse = self.tmp / "old-gnu-sparse.tar"
        with tarfile.open(old_sparse, "w", format=tarfile.GNU_FORMAT) as handle:
            member = tarfile.TarInfo("sparse")
            member.type = tarfile.GNUTYPE_SPARSE
            handle.addfile(member)
        archives.append(old_sparse)

        sparse_headers = (
            {"GNU.sparse.map": "0,1", "GNU.sparse.size": "1"},
            {"GNU.sparse.size": "1"},
            {
                "GNU.sparse.major": "1",
                "GNU.sparse.minor": "0",
                "GNU.sparse.realsize": "1",
            },
        )
        for index, headers in enumerate(sparse_headers):
            archive = self.tmp / f"pax-gnu-sparse-{index}.tar"
            with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as handle:
                member = tarfile.TarInfo("sparse")
                member.pax_headers = headers
                handle.addfile(member, io.BytesIO())
            archives.append(archive)

        for archive in archives:
            with (
                self.subTest(archive=archive.name),
                self.assertRaisesRegex(ValueError, "GNU sparse"),
            ):
                verify_export(self.empty_workspace_status(archive), archive)

    def test_workspace_zstd_metadata_rejection_reaps_process(self) -> None:
        raw_tar = self.tmp / "oversized-workspace-metadata.tar"
        with tarfile.open(raw_tar, "w", format=tarfile.PAX_FORMAT) as handle:
            member = tarfile.TarInfo("feature.py")
            member.pax_headers = {"comment": "x" * 512}
            handle.addfile(member, io.BytesIO())
        archive = self.tmp / "oversized-workspace-metadata.tar.zst"
        archive.write_bytes(b"zstd process fixture")
        status = self.empty_workspace_status(archive)

        real_popen = subprocess.Popen
        processes: list[subprocess.Popen[bytes]] = []
        script = (
            "import os, pathlib, sys, time\n"
            "os.write(1, pathlib.Path(sys.argv[1]).read_bytes())\n"
            "os.close(1)\n"
            "time.sleep(60)\n"
        )

        def spawn(*_args, **kwargs):
            process = real_popen(
                [sys.executable, "-c", script, str(raw_tar)],
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
            )
            processes.append(process)
            return process

        try:
            with (
                patch(
                    "rehearsal.workspace_export.subprocess.Popen", side_effect=spawn
                ),
                patch(
                    "rehearsal.workspace_export.MAX_VERIFIED_METADATA_BYTES",
                    128,
                ),
                self.assertRaisesRegex(ValueError, "metadata"),
            ):
                verify_export(status, archive)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()

        self.assertIsNotNone(processes[0].returncode)

    def test_workspace_zstd_verification_drains_stderr_while_streaming(self) -> None:
        status, archive, raw_tar = self.workspace_verification_fixture()
        real_popen = subprocess.Popen
        processes: list[subprocess.Popen[bytes]] = []
        script = (
            "import pathlib, sys\n"
            "sys.stderr.buffer.write(b'e' * (2 * 1024 * 1024))\n"
            "sys.stderr.buffer.flush()\n"
            "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())\n"
            "sys.stdout.buffer.flush()\n"
        )

        def spawn(*_args, **kwargs):
            process = real_popen(
                [sys.executable, "-c", script, str(raw_tar)],
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
            )
            processes.append(process)
            return process

        try:
            with patch(
                "rehearsal.workspace_export.subprocess.Popen", side_effect=spawn
            ):
                verified = verify_export(status, archive)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()

        self.assertEqual(verified["file_count"], 1)
        self.assertIsNotNone(processes[0].returncode)

    def test_workspace_zstd_verification_reaps_process_on_tar_error(self) -> None:
        status, archive, _raw_tar = self.workspace_verification_fixture()
        real_popen = subprocess.Popen
        processes: list[subprocess.Popen[bytes]] = []
        script = (
            "import os, time\n"
            "os.write(2, b'corrupt zstd stream')\n"
            "os.write(1, b'not a tar')\n"
            "os.close(1)\n"
            "time.sleep(60)\n"
        )

        def spawn(*_args, **kwargs):
            process = real_popen(
                [sys.executable, "-c", script],
                stdout=kwargs["stdout"],
                stderr=kwargs["stderr"],
            )
            processes.append(process)
            return process

        try:
            with (
                patch(
                    "rehearsal.workspace_export.subprocess.Popen", side_effect=spawn
                ),
                self.assertRaisesRegex(ValueError, "invalid workspace archive"),
            ):
                verify_export(status, archive)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()

        self.assertIsNotNone(processes[0].returncode)

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
        members = archive_members(archive)
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
        with self.assertRaises(ConfigError):
            parse_export("/")
        with self.assertRaises(ConfigError):
            parse_export("/sandbox/x.json=.")

    def test_setup_command_is_a_nonempty_json_argv(self) -> None:
        self.assertEqual(parse_command('["python3", "-m", "venv", ".venv"]'), (
            "python3",
            "-m",
            "venv",
            ".venv",
        ))
        for invalid in ("not-json", "[]", '{"command": "python3"}', '[""]'):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                parse_command(invalid)

    def test_export_destinations_must_be_unique_and_not_reserved(self) -> None:
        with self.assertRaisesRegex(ConfigError, "overlap"):
            self.config(
                exports=["/sandbox/output/required.json=result.json"],
                optional_exports=["/sandbox/output/optional.json=result.json"],
            )
        with self.assertRaisesRegex(ConfigError, "reserved"):
            self.config(exports=["/sandbox/output/value.json=artifact-run.json"])
        with self.assertRaisesRegex(ConfigError, "filename"):
            self.config(export_workspace="../candidate.tar.gz")
        for extension_only in (".tar.zst", ".tar.gz", ".tgz", ".tar"):
            with self.subTest(extension_only=extension_only), self.assertRaisesRegex(
                ConfigError,
                "filename",
            ):
                self.config(export_workspace=extension_only)
        with self.assertRaisesRegex(ConfigError, "overlap"):
            self.config(
                exports=["/sandbox/output/value=candidate.tar.gz"],
                export_workspace="candidate.tar.zst",
            )
        with self.assertRaisesRegex(ConfigError, "overlap"):
            self.config(
                exports=["/sandbox/output/value=candidate.tar.gz"],
                export_workspace="candidate.tgz",
            )
        with self.assertRaisesRegex(ConfigError, "overlap"):
            self.config(
                exports=["/sandbox/output/value=candidate.tar.gz"],
                export_workspace="candidate",
            )
        with self.assertRaisesRegex(ConfigError, "contract-target"):
            self.config(contract_target="../result.json")
        contract = self.tmp / "export-contract.json"
        contract.write_text(json.dumps(CONTRACT), encoding="utf-8")
        with self.assertRaisesRegex(ConfigError, "contract-target"):
            self.config(
                exports=["/sandbox/output/value=result.json"],
                output_contract=contract,
                contract_target="../result.json",
            )

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
            "--sandbox-image", "project@sha256:" + "b" * 64,
            "--setup-command", '["python3", "-c", "print(1)"]',
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
        from rehearsal.runners import (
            OpenShellOpencodeProcessRunner,
            create_sandboxed_runner,
        )

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
