"""Scorer packages: manifest strictness, isolation, statuses, and composition.

The OpenShell CLI is faked at the subprocess boundary (see `openshell_fake`),
so the scorer entrypoint, the mount layout, the generated isolation policy, and
the host-side composition all run for real.
"""
from __future__ import annotations

import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from openshell_fake import FakeOpenShell, opencode_error_stream, opencode_stream

from rehearsal.config import ConfigError, load_scorer
from rehearsal.sandbox import OpenShellSandbox
from rehearsal.scorers import (
    CANDIDATE_ROOT,
    external_symlinks,
    redact_trace,
    FIXTURES_ROOT,
    INPUT_ROOT,
    OUTPUT_ROOT,
    SCORER_ROOT,
    STATUS_INVALID_CANDIDATE,
    STATUS_JUDGE_UNAVAILABLE,
    STATUS_SCORED,
    STATUS_SCORER_ERROR,
    STATUS_SCORER_TIMEOUT,
    ScorerRunConfig,
    package_hash,
    run_scorer,
)

SCORE_SCRIPT = """\
import argparse, json, os, pathlib, sys

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()

root = os.environ.get("GHOSTLAB_FAKE_SANDBOX_ROOT", "").rstrip("/")
resolve = lambda path: pathlib.Path(root + path if root else path)

payload = json.loads(pathlib.Path(args.input).read_text(encoding="utf-8"))
repo = resolve(payload["repo_path"])
expected = json.loads(
    (resolve("/fixtures") / "expected.json").read_text(encoding="utf-8")
)
feature = repo / "feature.py"
behavior = 1.0 if feature.exists() and expected["symbol"] in feature.read_text() else 0.0
regression = 0.0 if (repo / "BROKEN").exists() else 1.0

components = [
    {"id": "requested_behavior", "value": behavior, "weight": 0.7, "hard_gate": True,
     "gate_passed": behavior >= 1.0,
     "evidence": [{"kind": "command", "ref": "grep feature", "summary": str(behavior)}]},
    {"id": "regression_suite", "value": regression, "weight": 0.2, "hard_gate": True,
     "gate_passed": regression >= 1.0, "evidence": []},
]
if os.environ.get("SCORER_OMIT") != "residual":
    components += json.loads(os.environ.get("SCORER_EXTRA_COMPONENTS", "[]"))

report = {
    "schema_version": "retro-score-report-v1",
    "task_id": payload["task_id"],
    "attempt_id": payload["attempt_id"],
    "status": "scored",
    "components": components,
    "commands": [{"argv": ["python3", "score.py"], "exit_code": 0, "duration_ms": 3}],
    "warnings": [],
}
pathlib.Path(args.output).write_text(json.dumps(report), encoding="utf-8")
print("scored")
"""

JUDGE_SCHEMA = {
    "type": "object",
    "required": ["criteria"],
    "properties": {
        "criteria": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "verdict"],
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"enum": ["MET", "UNMET", "CANNOT_ASSESS"]},
                    "value": {"type": ["number", "null"], "minimum": 0.0, "maximum": 1.0},
                    "evidence": {"type": "array"},
                },
            },
        }
    },
}

JUDGE_AGENT = {
    "id": "retro-residual-judge-v1",
    "runtime": {
        "backend": "opencode",
        "model": "github-copilot/claude-sonnet-4.5",
        "tools": {"bash": False, "webfetch": False},
        "permission": {"bash": "deny", "edit": "deny", "external_directory": "deny"},
    },
    "inputs": {"skills": [], "mcps": [], "assets": []},
}


def deterministic_components() -> list[dict]:
    return [
        {"id": "requested_behavior", "kind": "deterministic", "weight": 0.7,
         "hard_gate": True, "range": [0.0, 1.0]},
        {"id": "regression_suite", "kind": "deterministic", "weight": 0.3,
         "hard_gate": True, "range": [0.0, 1.0]},
    ]


def hybrid_components() -> list[dict]:
    return [
        {"id": "requested_behavior", "kind": "deterministic", "weight": 0.7,
         "hard_gate": True, "range": [0.0, 1.0]},
        {"id": "regression_suite", "kind": "deterministic", "weight": 0.2,
         "hard_gate": True, "range": [0.0, 1.0]},
        {"id": "project_fit", "kind": "judge", "weight": 0.1,
         "hard_gate": False, "range": [0.0, 1.0]},
    ]


class ScorerTestCase(unittest.TestCase):
    task_id = "2d493d"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.fake = FakeOpenShell(self.tmp / "sandboxes")
        self.run_dir = self.tmp / "attempt"
        self.run_dir.mkdir()

        self.task = self.tmp / "task.json"
        self.task.write_text(
            json.dumps(
                {
                    "schema_version": "retro-benchmark-task-v1",
                    "task_id": self.task_id,
                    "kind": "replay",
                    "prompt": "Add the feature.",
                }
            ),
            encoding="utf-8",
        )
        # Oracle material lives beside the task on the host and must never be
        # visible to a scorer sandbox.
        (self.tmp / "oracle.patch").write_text("the answer\n", encoding="utf-8")
        (self.tmp / "source-rollout.jsonl").write_text('{"event": 1}\n', encoding="utf-8")

        self.candidate_dir = self.tmp / "candidate-repo"
        self.candidate_dir.mkdir()
        (self.candidate_dir / "feature.py").write_text("def feature():\n    return 1\n", "utf-8")
        self.candidate = self.make_archive(self.candidate_dir)
        self.scorer = self.write_scorer()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def make_archive(self, root: Path, name: str = "candidate-state.tar.gz") -> Path:
        archive = self.tmp / name
        with tarfile.open(archive, "w:gz") as handle:
            for path in sorted(root.rglob("*")):
                handle.add(path, arcname=str(path.relative_to(root)))
        return archive

    def write_scorer(self, **overrides) -> Path:
        package = Path(overrides.pop("package_dir", self.tmp / "scorer"))
        package.mkdir(parents=True, exist_ok=True)
        (package / "score.py").write_text(SCORE_SCRIPT, encoding="utf-8")
        (package / "fixtures").mkdir(exist_ok=True)
        (package / "fixtures" / "expected.json").write_text(
            json.dumps({"symbol": "def feature"}), encoding="utf-8"
        )
        (package / "judge.prompt.md").write_text(
            "Anchors: 1.0 idiomatic, 0.0 alien to the project.\n", encoding="utf-8"
        )
        (package / "judge.schema.json").write_text(json.dumps(JUDGE_SCHEMA), encoding="utf-8")
        (package / "judge-agent.json").write_text(
            json.dumps(overrides.pop("judge_agent", JUDGE_AGENT)), encoding="utf-8"
        )
        manifest = {
            "schema_version": "retro-scorer-v1",
            "task_id": self.task_id,
            "mode": "deterministic",
            "entrypoint": [
                "python3", f"{SCORER_ROOT}/score.py",
                "--input", f"{INPUT_ROOT}/score-input.json",
                "--output", f"{OUTPUT_ROOT}/score-report.json",
            ],
            "runtime": {
                "image": "base", "network": "disabled", "timeout_seconds": 900,
                "cpu": 2, "memory_mb": 4096, "candidate_mount": "read_only",
            },
            "components": deterministic_components(),
            "pass_threshold": 0.8,
            "required_artifacts": ["repo", "task"],
        }
        manifest.update(overrides)
        (package / "scorer.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return package / "scorer.json"

    def sandbox_factory(self, config, role="scorer"):
        return OpenShellSandbox({**config, "bin": "openshell"}, role=role, run=self.fake)

    def score(self, **overrides) -> dict:
        options = {
            "task": self.task,
            "scorer": self.scorer,
            "candidate": self.candidate,
            "output": self.run_dir / "score-report.json",
            "run_dir": self.run_dir,
            "attempt_id": "attempt-1",
        }
        options.update(overrides)
        return run_scorer(ScorerRunConfig(**options), sandbox_factory=self.sandbox_factory)

    def judge_replying(self, payload) -> None:
        def hook(name, argv, input_text, root):
            if argv and argv[0] == "opencode":
                stream = payload if isinstance(payload, str) else json.dumps(payload)
                return subprocess.CompletedProcess(
                    argv, 0, stdout=opencode_stream(stream), stderr=""
                )
            return None

        self.fake.exec_hook = hook


class ScorerManifestTest(ScorerTestCase):
    def test_valid_manifest_exposes_components_and_residuals(self) -> None:
        manifest = load_scorer(
            self.write_scorer(
                mode="hybrid",
                components=hybrid_components(),
                judge={
                    "enabled": True,
                    "agent_config": "judge-agent.json",
                    "prompt": "judge.prompt.md",
                    "output_schema": "judge.schema.json",
                    "criteria": ["project_fit"],
                },
            )
        )
        self.assertEqual(manifest.mode, "hybrid")
        self.assertEqual(manifest.judge_criteria(), ("project_fit",))
        self.assertEqual(
            manifest.deterministic_ids(), ("requested_behavior", "regression_suite")
        )
        self.assertEqual(manifest.timeout_seconds(), 900)

    def test_weights_must_sum_to_one(self) -> None:
        components = deterministic_components()
        components[1]["weight"] = 0.5
        with self.assertRaises(ConfigError) as ctx:
            load_scorer(self.write_scorer(components=components))
        self.assertIn("sum to 1.0", str(ctx.exception))

    def test_component_rules_are_enforced(self) -> None:
        duplicate = deterministic_components() + [
            {"id": "requested_behavior", "kind": "deterministic", "weight": 0.0,
             "hard_gate": False, "range": [0.0, 1.0]}
        ]
        with self.assertRaises(ConfigError) as ctx:
            load_scorer(self.write_scorer(components=duplicate))
        self.assertIn("duplicate component", str(ctx.exception))

        rescaled = deterministic_components()
        rescaled[0]["range"] = [0.0, 10.0]
        with self.assertRaises(ConfigError) as ctx:
            load_scorer(self.write_scorer(components=rescaled))
        self.assertIn("range", str(ctx.exception))

        unknown_kind = deterministic_components()
        unknown_kind[0]["kind"] = "vibes"
        with self.assertRaises(ConfigError):
            load_scorer(self.write_scorer(components=unknown_kind))

    def test_performance_components_must_declare_their_protocol(self) -> None:
        components = deterministic_components()
        components[1] = {
            "id": "speed", "kind": "performance", "weight": 0.3,
            "hard_gate": False, "range": [0.0, 1.0],
            "performance": {"metric": "median_runtime_ms"},
        }
        with self.assertRaises(ConfigError) as ctx:
            load_scorer(self.write_scorer(components=components))
        self.assertIn("measured_runs", str(ctx.exception))

    def test_schema_version_mode_and_mount_are_pinned(self) -> None:
        with self.assertRaises(ConfigError):
            load_scorer(self.write_scorer(schema_version="retro-scorer-v2"))
        with self.assertRaises(ConfigError):
            load_scorer(self.write_scorer(mode="whatever"))
        with self.assertRaises(ConfigError) as ctx:
            load_scorer(
                self.write_scorer(
                    runtime={
                        "image": "base", "network": "disabled", "timeout_seconds": 900,
                        "candidate_mount": "read_write",
                    }
                )
            )
        self.assertIn("candidate_mount", str(ctx.exception))

    def test_judge_block_must_cover_every_judge_component(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_scorer(self.write_scorer(mode="hybrid", components=hybrid_components()))
        self.assertIn("judge block", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            load_scorer(
                self.write_scorer(
                    mode="hybrid",
                    components=hybrid_components(),
                    judge={
                        "enabled": True, "agent_config": "judge-agent.json",
                        "prompt": "judge.prompt.md", "output_schema": "judge.schema.json",
                        "criteria": ["absent_component"],
                    },
                )
            )
        self.assertIn("unknown components", str(ctx.exception))

    def test_package_hash_covers_every_file_including_skills(self) -> None:
        package = self.scorer.parent
        before = package_hash(package)
        skills = package / "skills" / "audit"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("scorer-only skill\n", encoding="utf-8")
        self.assertNotEqual(before, package_hash(package))

    def test_manifest_paths_must_resolve_inside_the_package(self) -> None:
        outside = self.tmp / "outside-agent.json"
        outside.write_text(json.dumps(JUDGE_AGENT), encoding="utf-8")
        hybrid = {
            "mode": "hybrid",
            "components": hybrid_components(),
            "judge": {
                "enabled": True, "agent_config": "judge-agent.json",
                "prompt": "judge.prompt.md", "output_schema": "judge.schema.json",
                "criteria": ["project_fit"],
            },
        }
        for escape in ("../outside-agent.json", str(outside), "/etc/passwd"):
            manifest = json.loads(json.dumps(hybrid))
            manifest["judge"]["agent_config"] = escape
            with self.assertRaises(ConfigError) as ctx:
                load_scorer(self.write_scorer(**manifest))
            self.assertIn("inside the scorer package", str(ctx.exception))

    def test_package_hash_covers_symlink_targets(self) -> None:
        package = self.scorer.parent
        (package / "real-a.txt").write_text("A", encoding="utf-8")
        (package / "real-b.txt").write_text("B", encoding="utf-8")
        link = package / "chosen.txt"
        link.symlink_to("real-a.txt")
        before = package_hash(package)
        # The mount preserves the link, so retargeting it changes what the
        # scorer reads and must change the package identity.
        link.unlink()
        link.symlink_to("real-b.txt")
        self.assertNotEqual(before, package_hash(package))

    def test_symlinks_leaving_the_package_are_refused(self) -> None:
        oracle = self.tmp / "oracle.patch"
        package = self.scorer.parent
        (package / "fixtures" / "answer.patch").symlink_to(oracle)
        self.assertEqual(external_symlinks(package), ["fixtures/answer.patch"])
        report = self.score()
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertIn("outside the package", report["error"])
        self.assertIn("fixtures/answer.patch", report["error"])
        self.assertFalse((self.run_dir / "scorer-staging" / "fixtures" / "answer.patch").exists())

    def test_a_judge_block_without_an_active_judge_is_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_scorer(
                self.write_scorer(
                    judge={"enabled": False, "criteria": ["regression_suite"]}
                )
            )
        self.assertIn("judge.criteria", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            load_scorer(self.write_scorer(judge={"enabled": True}))
        self.assertIn("no judge", str(ctx.exception))

    def test_declared_package_hash_mismatch_is_a_scorer_error(self) -> None:
        report = self.score(scorer=self.write_scorer(package_sha256="deadbeef"))
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertIsNone(report["score_total"])
        self.assertIn("package hash mismatch", report["error"])


class DeterministicScorerTest(ScorerTestCase):
    def test_deterministic_run_produces_a_composed_report(self) -> None:
        report = self.score()
        self.assertEqual(report["status"], STATUS_SCORED)
        self.assertEqual(report["score_total"], 1.0)
        self.assertTrue(report["passed"])
        self.assertTrue(report["valid"])
        self.assertEqual(report["hard_gate_failures"], [])
        self.assertEqual(report["task_id"], self.task_id)
        self.assertEqual(report["attempt_id"], "attempt-1")
        self.assertEqual(len(report["scorer_package_sha256"]), 64)
        self.assertEqual(report["hashes"]["seed"], 0)
        self.assertEqual([c["id"] for c in report["components"]],
                         ["requested_behavior", "regression_suite"])
        self.assertTrue(report["commands"])
        self.assertTrue((self.run_dir / "score-report.json").exists())

    def test_observed_failure_is_a_real_zero_not_an_error(self) -> None:
        empty = self.tmp / "empty-repo"
        empty.mkdir()
        (empty / "README.md").write_text("nothing implemented\n", encoding="utf-8")
        report = self.score(candidate=self.make_archive(empty, "empty.tar.gz"))
        self.assertEqual(report["status"], STATUS_SCORED)
        self.assertEqual(report["score_total"], 0.0)
        self.assertFalse(report["passed"])
        self.assertTrue(report["valid"])
        self.assertEqual(report["hard_gate_failures"], ["requested_behavior"])

    def test_hard_gate_failure_forces_total_zero(self) -> None:
        broken = self.tmp / "regressed"
        broken.mkdir()
        (broken / "feature.py").write_text("def feature():\n    return 1\n", encoding="utf-8")
        (broken / "BROKEN").write_text("regression\n", encoding="utf-8")
        report = self.score(candidate=self.make_archive(broken, "regressed.tar.gz"))
        self.assertEqual(report["status"], STATUS_SCORED)
        self.assertEqual(report["hard_gate_failures"], ["regression_suite"])
        # requested_behavior scored 1.0 * 0.7, but a failed gate zeroes the total.
        self.assertEqual(report["score_total"], 0.0)
        self.assertFalse(report["passed"])

    def test_unscored_soft_weight_is_reported_and_bounded(self) -> None:
        components = [
            {"id": "requested_behavior", "kind": "deterministic", "weight": 0.7,
             "hard_gate": True, "range": [0.0, 1.0]},
            {"id": "regression_suite", "kind": "deterministic", "weight": 0.15,
             "hard_gate": True, "range": [0.0, 1.0]},
            {"id": "style", "kind": "deterministic", "weight": 0.15,
             "hard_gate": False, "range": [0.0, 1.0]},
        ]
        report = self.score(scorer=self.write_scorer(components=components))
        self.assertEqual(report["status"], STATUS_SCORED)
        style = next(item for item in report["components"] if item["id"] == "style")
        self.assertFalse(style["scored"])
        self.assertEqual(style["verdict"], "CANNOT_ASSESS")
        self.assertAlmostEqual(report["unscored_weight"], 0.15)
        # No silent renormalization: the missing weight is simply not earned.
        self.assertAlmostEqual(report["score_total"], 0.85)
        self.assertTrue(report["valid"])

    def test_more_than_twenty_percent_unscored_invalidates_the_result(self) -> None:
        components = [
            {"id": "requested_behavior", "kind": "deterministic", "weight": 0.7,
             "hard_gate": True, "range": [0.0, 1.0]},
            {"id": "regression_suite", "kind": "deterministic", "weight": 0.05,
             "hard_gate": True, "range": [0.0, 1.0]},
            {"id": "style", "kind": "deterministic", "weight": 0.25,
             "hard_gate": False, "range": [0.0, 1.0]},
        ]
        report = self.score(scorer=self.write_scorer(components=components))
        self.assertEqual(report["status"], STATUS_SCORED)
        self.assertFalse(report["valid"])
        self.assertFalse(report["passed"])
        self.assertTrue(any("invalid" in warning for warning in report["warnings"]))

    def test_scorer_crash_is_scorer_error_not_zero(self) -> None:
        package = self.tmp / "crashing"
        scorer = self.write_scorer(package_dir=package)
        (package / "score.py").write_text("import sys; sys.exit(9)", encoding="utf-8")
        report = self.score(scorer=scorer)
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertIsNone(report["score_total"])
        self.assertIsNone(report["passed"])
        self.assertFalse(report["valid"])

    def test_a_deterministic_scorer_never_starts_a_judge_sandbox(self) -> None:
        manifest = load_scorer(self.scorer)
        self.assertEqual(manifest.judge_criteria(), ())
        report = self.score()
        self.assertEqual(report["status"], STATUS_SCORED)
        self.assertIsNone(report["judge"])
        self.assertEqual(len(self.fake.created), 1)
        self.assertFalse((self.run_dir / "judge-policy.yaml").exists())

    def test_scorer_timeout_has_its_own_status(self) -> None:
        self.fake.timeout_on = "score.py"
        report = self.score()
        self.assertEqual(report["status"], STATUS_SCORER_TIMEOUT)
        self.assertIsNone(report["score_total"])

    def test_missing_report_is_a_scorer_error(self) -> None:
        package = self.tmp / "silent"
        scorer = self.write_scorer(package_dir=package)
        (package / "score.py").write_text("print('nothing written')", encoding="utf-8")
        report = self.score(scorer=scorer)
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertIn("score-report.json", report["error"])

    def test_report_declaring_unknown_components_is_rejected(self) -> None:
        package = self.tmp / "liar"
        scorer = self.write_scorer(package_dir=package)
        (package / "score.py").write_text(
            "import argparse, json, pathlib\n"
            "p = argparse.ArgumentParser(); p.add_argument('--input'); p.add_argument('--output')\n"
            "a = p.parse_args()\n"
            "payload = json.loads(pathlib.Path(a.input).read_text())\n"
            "pathlib.Path(a.output).write_text(json.dumps({\n"
            "  'schema_version': 'retro-score-report-v1', 'task_id': payload['task_id'],\n"
            "  'status': 'scored', 'components': [{'id': 'invented', 'value': 1.0}]}))\n",
            encoding="utf-8",
        )
        report = self.score(scorer=scorer)
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertIn("invented", report["error"])

    def test_scorer_cannot_claim_a_different_task(self) -> None:
        other = self.tmp / "other-task.json"
        other.write_text(
            json.dumps(
                {"schema_version": "retro-benchmark-task-v1", "task_id": "other", "prompt": "x"}
            ),
            encoding="utf-8",
        )
        report = self.score(task=other)
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertIn("does not match", report["error"])

    def test_broken_or_absent_candidate_artifact_is_its_own_status(self) -> None:
        corrupt = self.tmp / "corrupt.tar.gz"
        corrupt.write_bytes(b"not an archive at all")
        report = self.score(candidate=corrupt)
        self.assertEqual(report["status"], STATUS_INVALID_CANDIDATE)
        self.assertIsNone(report["score_total"])

        missing = self.score(candidate=self.tmp / "absent.tar.gz")
        self.assertEqual(missing["status"], STATUS_INVALID_CANDIDATE)

    def test_archive_escaping_the_extraction_root_is_refused(self) -> None:
        payload = self.tmp / "payload.txt"
        payload.write_text("x", encoding="utf-8")
        for index, arcname in enumerate(
            # The second name is a sibling that merely shares the root's prefix;
            # a string-prefix containment test would let it through.
            ["../escaped.txt", "../repo-evil/x", "../repo/../repo2/y"]
        ):
            archive = self.tmp / f"escape-{index}.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                handle.add(payload, arcname=arcname)
            report = self.score(candidate=archive)
            self.assertEqual(report["status"], STATUS_INVALID_CANDIDATE, arcname)
            self.assertIn("escapes", report["error"])
        self.assertFalse((self.run_dir / "scorer-staging" / "candidate" / "repo-evil").exists())

    def test_a_hardlink_cannot_pull_a_host_file_into_the_candidate_mount(self) -> None:
        secret = self.tmp / "host-secret.txt"
        secret.write_text("HOST SECRET\n", encoding="utf-8")
        payload = self.tmp / "normal.txt"
        payload.write_text("x", encoding="utf-8")

        archive = self.tmp / "hardlink.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            handle.add(payload, arcname="a/b/c/normal.txt")
            member = tarfile.TarInfo("a/b/c/stolen.txt")
            member.type = tarfile.LNKTYPE
            # A hard link's target is resolved against the extraction root, not
            # the member's own directory.
            member.linkname = "../../../host-secret.txt"
            handle.addfile(member)

        report = self.score(candidate=archive)
        self.assertEqual(report["status"], STATUS_INVALID_CANDIDATE)
        self.assertIn("escapes", report["error"])
        stolen = self.run_dir / "scorer-staging" / "candidate" / "repo" / "a/b/c/stolen.txt"
        self.assertFalse(stolen.exists())

    def test_a_status_the_scorer_declared_is_not_republished_as_scored(self) -> None:
        package = self.tmp / "self-reporting"
        scorer = self.write_scorer(package_dir=package)
        (package / "score.py").write_text(
            "import argparse, json, pathlib\n"
            "p = argparse.ArgumentParser(); p.add_argument('--input'); p.add_argument('--output')\n"
            "a = p.parse_args()\n"
            "payload = json.loads(pathlib.Path(a.input).read_text())\n"
            "pathlib.Path(a.output).write_text(json.dumps({\n"
            "  'schema_version': 'retro-score-report-v1', 'task_id': payload['task_id'],\n"
            "  'status': 'scorer_error', 'components': [],\n"
            "  'warnings': ['the project environment did not build']}))\n",
            encoding="utf-8",
        )
        report = self.score(scorer=scorer)
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertIsNone(report["score_total"])
        self.assertIn("did not build", report["error"])

    def test_a_malformed_manifest_number_still_produces_a_report(self) -> None:
        components = deterministic_components()
        components[0]["range"] = ["zero", 1]
        report = self.score(scorer=self.write_scorer(components=components))
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertIn("range", report["error"])
        self.assertTrue((self.run_dir / "score-report.json").exists())

    def test_an_unexpected_harness_failure_still_produces_a_report(self) -> None:
        from unittest.mock import patch

        with patch("rehearsal.scorers.stage_inputs", side_effect=RuntimeError("harness bug")):
            report = self.score()
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertIsNone(report["score_total"])
        self.assertIn("harness bug", report["error"])


class ScorerRepeatabilityTest(ScorerTestCase):
    def test_repeated_deterministic_runs_are_reported_as_stable(self) -> None:
        report = self.score(repeat=3)
        self.assertEqual(report["status"], STATUS_SCORED)
        self.assertEqual(
            report["repeatability"],
            {
                "runs": 3,
                "deterministic_stable": True,
                "unstable_components": [],
                "max_total_spread": 0.0,
                "totals": [1.0, 1.0, 1.0],
            },
        )
        self.assertEqual(len(self.fake.created), 3)
        self.assertTrue((self.run_dir / "deterministic-report-3.json").exists())

    def test_unstable_components_are_named_not_hidden(self) -> None:
        runs = {"n": 0}

        def hook(name, argv, input_text, root):
            if not (argv and argv[-1].endswith("score-report.json")):
                return None
            runs["n"] += 1
            report = {
                "schema_version": "retro-score-report-v1",
                "task_id": self.task_id,
                "status": "scored",
                "components": [
                    {"id": "requested_behavior", "value": 1.0, "gate_passed": True},
                    {"id": "regression_suite", "value": 1.0 if runs["n"] % 2 else 0.0,
                     "gate_passed": bool(runs["n"] % 2)},
                ],
            }
            destination = root / "output" / "score-report.json"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="scored", stderr="")

        self.fake.exec_hook = hook
        report = self.score(repeat=2)
        self.assertEqual(report["status"], STATUS_SCORED)
        self.assertFalse(report["repeatability"]["deterministic_stable"])
        self.assertEqual(report["repeatability"]["unstable_components"], ["regression_suite"])
        self.assertAlmostEqual(report["repeatability"]["max_total_spread"], 0.3)
        self.assertTrue(any("did not reproduce" in w for w in report["warnings"]))


# A real artifact-run trace: identity, host paths, hashes, model stderr, and the
# tool evidence a scorer is allowed to see.
ARTIFACT_TRACE = [
    {
        "type": "artifact_run.started",
        "timestamp": "2026-08-29T00:00:00Z",
        "data": {
            "agent": "acme-coder-v3",
            "workspace": "/Users/dev/private/repos/secret-project",
            "workspace_input_sha256": "f" * 64,
            "prompt_sha256": "e" * 64,
        },
    },
    {"type": "agent.prompt", "timestamp": "2026-08-29T00:00:01Z", "data": {"chars": 42}},
    {
        "type": "agent.tool_call",
        "timestamp": "2026-08-29T00:00:02Z",
        "data": {
            "server": "fs", "tool": "write", "status": "completed", "duration_ms": 12,
            "arguments": {"path": "/Users/dev/private/repos/secret-project/feature.py"},
        },
    },
    {
        "type": "agent.reply",
        "timestamp": "2026-08-29T00:00:03Z",
        "data": {
            "exit_code": 0, "timed_out": False, "chars": 900,
            "stderr": "warning: claude-opus-4 rate limit near for org acme",
        },
    },
    {
        "type": "artifact_run.finished",
        "timestamp": "2026-08-29T00:00:04Z",
        "data": {"status": "completed"},
    },
]

IDENTITY_MARKERS = (
    "acme-coder-v3", "claude-opus-4", "secret-project", "/Users/dev",
    "rate limit", "f" * 64, "e" * 64, "prompt_sha256", "artifact_run",
    "workspace", "exit_code", "stderr",
)


class TraceRedactionTest(unittest.TestCase):
    def test_only_allowlisted_tool_evidence_survives(self) -> None:
        events = redact_trace(json.dumps(event) for event in ARTIFACT_TRACE)
        self.assertEqual([event["type"] for event in events], ["agent.tool_call"])
        self.assertEqual(
            events[0],
            {
                "type": "agent.tool_call",
                "timestamp": "2026-08-29T00:00:02Z",
                "data": {"server": "fs", "tool": "write", "status": "completed",
                         "duration_ms": 12},
            },
        )
        # Tool arguments carry host paths, so they are not in the allowlist.
        self.assertNotIn("arguments", events[0]["data"])

    def test_malformed_and_unknown_lines_are_dropped_not_passed_through(self) -> None:
        events = redact_trace(
            [
                "",
                "not json at all",
                json.dumps(["a", "list"]),
                json.dumps({"type": "some.future.event", "data": {"secret": "x"}}),
                json.dumps({"type": "agent.tool_call", "data": {"tool": "read"}}),
            ]
        )
        self.assertEqual(events, [{"type": "agent.tool_call", "timestamp": "",
                                   "data": {"tool": "read"}}])


class ScorerIsolationTest(ScorerTestCase):
    def test_candidate_scorer_and_fixtures_are_mounted_read_only(self) -> None:
        self.score()
        policy = (self.run_dir / "scorer-policy.yaml").read_text(encoding="utf-8")
        read_only = policy.split("read_only: [")[1].split("]")[0]
        read_write = policy.split("read_write: [")[1].split("]")[0]
        for mount in (CANDIDATE_ROOT, SCORER_ROOT, FIXTURES_ROOT, INPUT_ROOT):
            self.assertIn(mount, read_only)
        self.assertEqual(
            sorted(part.strip() for part in read_write.split(",")),
            ["/dev/null", "/output", "/tmp"],
        )
        for mount in (CANDIDATE_ROOT, SCORER_ROOT, FIXTURES_ROOT):
            self.assertNotIn(mount, read_write)

    def test_deterministic_sandbox_has_no_network_and_no_credentials(self) -> None:
        self.score()
        policy = (self.run_dir / "scorer-policy.yaml").read_text(encoding="utf-8")
        self.assertNotIn("network_policies", policy)
        create = next(call for call in self.fake.calls if call[1:3] == ["sandbox", "create"])
        self.assertIn("--no-auto-providers", create)
        self.assertNotIn("--provider", create)

    def test_scorer_cannot_reach_the_oracle_or_the_source_rollout(self) -> None:
        self.score()
        name = self.fake.created[0]
        root = self.fake.inside(name, "/")
        present = {path.name for path in root.rglob("*")}
        self.assertIn("expected.json", present)  # the tree really was inspected
        self.assertNotIn("oracle.patch", present)
        self.assertNotIn("source-rollout.jsonl", present)
        self.assertEqual(
            sorted(target for _source, target in self.fake.uploads[name]), ["/"] * 5
        )
        self.assertEqual(
            sorted(Path(source).name for source, _target in self.fake.uploads[name]),
            ["candidate", "fixtures", "input", "output", "scorer"],
        )

    def test_hidden_fixtures_live_outside_the_candidate_mount(self) -> None:
        self.score()
        name = self.fake.created[0]
        self.assertTrue(self.fake.exists(name, f"{FIXTURES_ROOT}/expected.json"))
        self.assertFalse(self.fake.exists(name, f"{CANDIDATE_ROOT}/repo/fixtures"))
        self.assertFalse(self.fake.exists(name, f"{SCORER_ROOT}/fixtures/expected.json"))

    def test_the_trace_the_scorer_receives_carries_no_identity(self) -> None:
        trace = self.tmp / "events.jsonl"
        trace.write_text(
            "".join(json.dumps(event) + "\n" for event in ARTIFACT_TRACE), encoding="utf-8"
        )
        report = self.score(trace=trace)
        self.assertEqual(report["status"], STATUS_SCORED)

        staged = self.run_dir / "scorer-staging" / "input" / "aut-events.jsonl"
        body = staged.read_text(encoding="utf-8")
        for marker in IDENTITY_MARKERS:
            self.assertNotIn(marker, body, marker)
        events = [json.loads(line) for line in body.splitlines() if line.strip()]
        self.assertEqual([event["type"] for event in events], ["agent.tool_call"])
        self.assertEqual(sorted(events[0]["data"]),
                         ["duration_ms", "server", "status", "tool"])

        name = self.fake.created[0]
        self.assertEqual(self.fake.read(name, f"{INPUT_ROOT}/aut-events.jsonl"), body)
        staged_input = json.loads(
            (self.run_dir / "scorer-staging" / "input" / "score-input.json").read_text("utf-8")
        )
        self.assertEqual(staged_input["trace_path"], f"{INPUT_ROOT}/aut-events.jsonl")

    def test_a_declared_but_missing_trace_is_not_advertised_to_the_scorer(self) -> None:
        self.score(trace=self.tmp / "absent-events.jsonl")
        staged_input = json.loads(
            (self.run_dir / "scorer-staging" / "input" / "score-input.json").read_text("utf-8")
        )
        self.assertIsNone(staged_input["trace_path"])

    def test_scorer_input_is_the_declared_contract(self) -> None:
        self.score(seed=7, attempt_id="a-7")
        staged = json.loads(
            (self.run_dir / "scorer-staging" / "input" / "score-input.json").read_text("utf-8")
        )
        self.assertEqual(staged["schema_version"], "retro-score-input-v1")
        self.assertEqual(staged["repo_path"], f"{CANDIDATE_ROOT}/repo")
        self.assertEqual(staged["task_path"], f"{INPUT_ROOT}/task.json")
        self.assertEqual(staged["seed"], 7)
        self.assertEqual(staged["attempt_id"], "a-7")
        self.assertIsNone(staged["trace_path"])

    def test_every_sandbox_is_deleted(self) -> None:
        self.score()
        self.assertEqual(self.fake.deleted, self.fake.created)

    def test_the_sandbox_is_deleted_even_when_the_host_side_crashes(self) -> None:
        from unittest.mock import patch

        with patch(
            "rehearsal.scorers.validate_report_document", side_effect=RuntimeError("boom")
        ):
            report = self.score()
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertEqual(self.fake.deleted, self.fake.created)


class HybridScorerTest(ScorerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.scorer = self.write_scorer(
            mode="hybrid",
            components=hybrid_components(),
            judge={
                "enabled": True,
                "agent_config": "judge-agent.json",
                "prompt": "judge.prompt.md",
                "output_schema": "judge.schema.json",
                "criteria": ["project_fit"],
            },
        )

    def test_judge_scores_only_the_declared_residual(self) -> None:
        self.judge_replying(
            {"criteria": [{"id": "project_fit", "verdict": "MET", "value": 0.8,
                           "evidence": ["feature.py"]}]}
        )
        report = self.score()
        self.assertEqual(report["status"], STATUS_SCORED)
        self.assertAlmostEqual(report["score_total"], 0.7 + 0.2 + 0.08)
        fit = next(item for item in report["components"] if item["id"] == "project_fit")
        self.assertEqual(fit["verdict"], "MET")
        self.assertEqual(fit["evidence"], ["feature.py"])
        self.assertEqual(report["judge"]["criteria"], ["project_fit"])
        self.assertEqual(len(report["hashes"]["judge_prompt_sha256"]), 64)

    def test_deterministic_sandbox_is_gone_before_the_judge_starts(self) -> None:
        self.judge_replying(
            {"criteria": [{"id": "project_fit", "verdict": "MET", "value": 1.0}]}
        )
        self.score()
        self.assertEqual(len(self.fake.created), 2)
        scorer_sandbox, judge_sandbox = self.fake.created
        verbs = [(call[1:3], call[3:4]) for call in self.fake.calls]
        deleted_scorer = next(
            index for index, (verb, name) in enumerate(verbs)
            if verb == ["sandbox", "delete"] and name == [scorer_sandbox]
        )
        created_judge = next(
            index for index, call in enumerate(self.fake.calls)
            if call[1:3] == ["sandbox", "create"] and judge_sandbox in call
        )
        self.assertLess(deleted_scorer, created_judge)

    def test_judge_sandbox_sees_no_fixtures_no_scorer_code_and_has_a_provider(self) -> None:
        self.judge_replying(
            {"criteria": [{"id": "project_fit", "verdict": "MET", "value": 1.0}]}
        )
        self.score()
        judge_sandbox = self.fake.created[1]
        names = sorted(
            Path(source).name for source, _target in self.fake.uploads[judge_sandbox]
        )
        self.assertEqual(names, ["candidate", "input", "output"])
        self.assertTrue(self.fake.exists(judge_sandbox, f"{CANDIDATE_ROOT}/repo/feature.py"))
        self.assertFalse(self.fake.exists(judge_sandbox, FIXTURES_ROOT))
        self.assertFalse(self.fake.exists(judge_sandbox, SCORER_ROOT))
        self.assertTrue(self.fake.exists(judge_sandbox, f"{INPUT_ROOT}/rubric.md"))
        self.assertTrue(
            self.fake.exists(judge_sandbox, f"{INPUT_ROOT}/deterministic-report.json")
        )
        create = [
            call for call in self.fake.calls
            if call[1:3] == ["sandbox", "create"] and judge_sandbox in call
        ][0]
        self.assertIn("--provider", create)
        judge_policy = (self.run_dir / "judge-policy.yaml").read_text(encoding="utf-8")
        self.assertIn("network_policies", judge_policy)
        self.assertIn(CANDIDATE_ROOT, judge_policy.split("read_only: [")[1].split("]")[0])

    def test_judge_prompt_carries_the_criterion_and_hides_the_candidate(self) -> None:
        captured: dict[str, str] = {}

        def hook(name, argv, input_text, root):
            if argv and argv[0] == "opencode":
                captured["prompt"] = input_text or ""
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=opencode_stream(
                        json.dumps(
                            {"criteria": [{"id": "project_fit", "verdict": "MET", "value": 1.0}]}
                        )
                    ),
                    stderr="",
                )
            return None

        self.fake.exec_hook = hook
        self.score()
        prompt = captured["prompt"]
        self.assertIn("project_fit", prompt)
        self.assertIn("deterministic results are authoritative", prompt)
        self.assertIn("Anchors: 1.0 idiomatic", prompt)
        self.assertNotIn("oracle", prompt.lower())

    def test_cannot_assess_leaves_the_component_unscored(self) -> None:
        self.judge_replying(
            {"criteria": [{"id": "project_fit", "verdict": "CANNOT_ASSESS", "value": 0.9}]}
        )
        report = self.score()
        self.assertEqual(report["status"], STATUS_SCORED)
        fit = next(item for item in report["components"] if item["id"] == "project_fit")
        self.assertFalse(fit["scored"])
        self.assertIsNone(fit["value"])
        self.assertAlmostEqual(report["unscored_weight"], 0.1)
        self.assertAlmostEqual(report["score_total"], 0.9)
        self.assertTrue(report["valid"])

    def test_provider_outage_is_judge_unavailable_not_scorer_error(self) -> None:
        def hook(name, argv, input_text, root):
            if argv and argv[0] == "opencode":
                return subprocess.CompletedProcess(
                    argv, 0, stdout=opencode_error_stream("quota exceeded"), stderr=""
                )
            return None

        self.fake.exec_hook = hook
        report = self.score()
        self.assertEqual(report["status"], STATUS_JUDGE_UNAVAILABLE)
        self.assertIsNone(report["score_total"])
        self.assertIn("quota exceeded", report["error"])

    def test_judge_reply_violating_its_declared_schema_is_unavailable(self) -> None:
        self.judge_replying({"criteria": [{"id": "project_fit", "verdict": "MAYBE"}]})
        report = self.score()
        self.assertEqual(report["status"], STATUS_JUDGE_UNAVAILABLE)
        self.assertIn("output schema", report["error"])

    def test_unparseable_judge_reply_is_unavailable(self) -> None:
        self.judge_replying("the repository looks fine to me")
        report = self.score()
        self.assertEqual(report["status"], STATUS_JUDGE_UNAVAILABLE)

    def test_judge_agent_must_meet_the_permission_floor(self) -> None:
        agent = json.loads(json.dumps(JUDGE_AGENT))
        agent["runtime"]["permission"]["bash"] = "allow"
        self.write_scorer(judge_agent=agent, mode="hybrid",
                          components=hybrid_components(),
                          judge={
                              "enabled": True, "agent_config": "judge-agent.json",
                              "prompt": "judge.prompt.md",
                              "output_schema": "judge.schema.json",
                              "criteria": ["project_fit"],
                          })
        report = self.score()
        self.assertEqual(report["status"], STATUS_SCORER_ERROR)
        self.assertIn("permission floor", report["error"])
        self.assertIn("permission.bash", report["error"])

    def test_judge_model_must_be_pinned(self) -> None:
        agent = json.loads(json.dumps(JUDGE_AGENT))
        agent["runtime"]["model"] = "${SCORER_JUDGE_MODEL}"
        self.write_scorer(judge_agent=agent, mode="hybrid",
                          components=hybrid_components(),
                          judge={
                              "enabled": True, "agent_config": "judge-agent.json",
                              "prompt": "judge.prompt.md",
                              "output_schema": "judge.schema.json",
                              "criteria": ["project_fit"],
                          })
        report = self.score()
        self.assertEqual(report["status"], STATUS_JUDGE_UNAVAILABLE)
        self.assertIn("pinned model", report["error"])

    def test_deterministic_failure_short_circuits_before_the_judge_runs(self) -> None:
        self.fake.timeout_on = "score.py"
        report = self.score()
        self.assertEqual(report["status"], STATUS_SCORER_TIMEOUT)
        self.assertEqual(len(self.fake.created), 1)


class ScorerCliTest(ScorerTestCase):
    def test_cli_writes_the_report_and_exits_on_status(self) -> None:
        from unittest.mock import patch

        from rehearsal.cli import main

        output = self.run_dir / "score-report.json"
        argv = [
            "scorer-run",
            "--task", str(self.task),
            "--scorer", str(self.scorer),
            "--candidate", str(self.candidate),
            "--output", str(output),
            "--run-dir", str(self.run_dir),
            "--attempt-id", "cli-1",
        ]
        with patch("rehearsal.sandbox._default_run", self.fake), patch(
            "rehearsal.sandbox.shutil.which", return_value="openshell"
        ):
            self.assertEqual(main(argv), 0)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], STATUS_SCORED)
        self.assertEqual(report["attempt_id"], "cli-1")

        corrupt = self.tmp / "corrupt.tar.gz"
        corrupt.write_bytes(b"nope")
        with patch("rehearsal.sandbox._default_run", self.fake), patch(
            "rehearsal.sandbox.shutil.which", return_value="openshell"
        ):
            self.assertEqual(main([*argv[:6], str(corrupt), *argv[7:]]), 1)

    def test_scorecard_aggregates_attempts_from_the_cli(self) -> None:
        from rehearsal.cli import main

        report = self.score()
        self.assertEqual(report["status"], STATUS_SCORED)
        (self.run_dir / "attempt.json").write_text(
            json.dumps(
                {
                    "schema_version": "retro-benchmark-attempt-v1",
                    "attempt_id": "attempt-1",
                    "task_id": self.task_id,
                    "source_id": "rollout-1",
                    "agent_id": "candidate",
                    "seed": 0,
                    "status": report["status"],
                    "score": report["score_total"],
                    "passed": report["passed"],
                    "score_report": "score-report.json",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(main(["scorecard", "--attempts", str(self.run_dir)]), 0)
        scorecard = json.loads(
            (self.run_dir / "benchmark-scorecard.json").read_text(encoding="utf-8")
        )
        self.assertEqual(scorecard["agents"]["candidate"]["benchmark_score"], 1.0)
        self.assertEqual(
            scorecard["agents"]["candidate"]["per_source"]["rollout-1"]["tasks"], 1
        )


if __name__ == "__main__":
    unittest.main()
