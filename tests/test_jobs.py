"""Unit tests for the job model (rehearsal.jobs) and prompt overrides."""
from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from rehearsal import jobs, prompts
from rehearsal.config import ConfigError, TargetConfig
from rehearsal.spec import DEFAULT_GENERATION, DEFAULT_PROMPTS, DEFAULT_TEST, load_spec


def _target() -> TargetConfig:
    return jobs.target_from_url("http://localhost:8000/mcp")


class SlugifyTest(unittest.TestCase):
    def test_kebab_cases_and_strips(self) -> None:
        self.assertEqual(jobs.slugify("Cortex Eval"), "cortex-eval")
        self.assertEqual(jobs.slugify("  Weird__Name!! "), "weird-name")
        self.assertEqual(jobs.slugify("already-good"), "already-good")

    def test_empty_falls_back(self) -> None:
        self.assertEqual(jobs.slugify("!!!"), "job")


class DefaultJobSpecTest(unittest.TestCase):
    def test_populates_all_sections_with_defaults(self) -> None:
        spec = jobs.default_job_spec("Cortex Eval", target=_target())
        self.assertEqual(spec.id, "cortex-eval")
        self.assertEqual(spec.name, "Cortex Eval")
        self.assertEqual(spec.workspace, "workspace")
        self.assertEqual(spec.generation, DEFAULT_GENERATION)
        self.assertEqual(spec.test, DEFAULT_TEST)
        self.assertEqual(spec.prompts, DEFAULT_PROMPTS)
        # target id follows the slug so target_config() is self-consistent
        self.assertEqual(spec.target_config().id, "cortex-eval")

    def test_overrides_merge_over_defaults(self) -> None:
        spec = jobs.default_job_spec(
            "j",
            target=_target(),
            generation={"personas": 5, "model": "gpt-x"},
            review_gates={"min_pass_rate": 0.75},
            aut_runner="runners/codex-cortex-local-aut.json",
        )
        self.assertEqual(spec.generation["personas"], 5)
        self.assertEqual(spec.generation["model"], "gpt-x")
        # untouched keys keep their default
        self.assertEqual(spec.generation["scenarios_per_persona"], 2)
        self.assertEqual(spec.review["gates"]["min_pass_rate"], 0.75)
        self.assertTrue(spec.review["gates"]["no_tool_schema_errors"])
        aut = [h for h in spec.hosts if h["id"] == "aut"][0]
        self.assertEqual(aut["kind"], "process")
        self.assertEqual(aut["config_ref"], "runners/codex-cortex-local-aut.json")

    def test_none_override_values_are_ignored(self) -> None:
        spec = jobs.default_job_spec(
            "j", target=_target(), generation={"personas": None}
        )
        self.assertEqual(spec.generation["personas"], DEFAULT_GENERATION["personas"])


class CreateAndResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "jobs"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create(self, name: str = "Cortex Eval", **kw) -> Path:
        spec = jobs.default_job_spec(name, target=_target(), **kw)
        return jobs.create_job(name, spec, jobs_root=self.root)

    def test_creates_self_contained_tree(self) -> None:
        spec_path = self._create()
        job_dir = spec_path.parent
        self.assertTrue(spec_path.is_file())
        self.assertTrue((job_dir / "workspace").is_dir())
        self.assertTrue((job_dir / "runs").is_dir())
        self.assertEqual(job_dir.name, "cortex-eval")

    def test_job_yaml_round_trips(self) -> None:
        spec_path = self._create(generation={"personas": 3})
        loaded = load_spec(spec_path)
        self.assertEqual(loaded.id, "cortex-eval")
        self.assertEqual(loaded.generation["personas"], 3)
        self.assertEqual(sorted(loaded.prompts), sorted(DEFAULT_PROMPTS))
        self.assertEqual(loaded.test["judge"], True)

    def test_header_documents_prompt_placeholders(self) -> None:
        spec_path = self._create()
        text = spec_path.read_text(encoding="utf-8")
        self.assertIn("user_emulator: persona, goal", text)
        self.assertIn("Available {placeholders}", text)

    def test_refuses_overwrite_without_force(self) -> None:
        self._create()
        with self.assertRaises(ConfigError):
            self._create()
        # force overwrites cleanly
        spec = jobs.default_job_spec("Cortex Eval", target=_target())
        jobs.create_job("Cortex Eval", spec, jobs_root=self.root, force=True)

    def test_resolve_job_by_name_dir_and_file(self) -> None:
        spec_path = self._create()
        job_dir = spec_path.parent
        # by explicit dir
        self.assertEqual(jobs.resolve_job(str(job_dir)), spec_path)
        # by direct file
        self.assertEqual(jobs.resolve_job(str(spec_path)), spec_path)

    def test_resolve_dir_without_job_yaml_errors(self) -> None:
        empty = self.root / "empty"
        empty.mkdir(parents=True)
        with self.assertRaises(ConfigError) as ctx:
            jobs.resolve_job(str(empty))
        self.assertIn("No job.yaml", str(ctx.exception))

    def test_resolve_unknown_name_errors_with_create_hint(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            jobs.resolve_job("definitely-not-a-job")
        self.assertIn("ghostlab create", str(ctx.exception))

    def test_runtime_configuration_updates_inline_and_materialized_runner(self) -> None:
        spec_path = self._create()
        spec = load_spec(spec_path)
        runner_path = jobs.add_aut_host(spec, spec_path, jobs.build_codex_aut_runner(spec))
        spec = load_spec(spec_path)
        jobs.update_agent_runtime(
            spec, spec_path, model="gpt-aut", kind="codex-session",
            timeout_seconds=901, approval_mode="on-request",
            codex_sandbox="workspace-write", codex_bin="/opt/codex",
            user_model="gpt-user", generation_model="gpt-generation",
            judge_model="gpt-judge",
        )
        loaded = load_spec(spec_path)
        command = loaded.agent["runner"]["command"]
        self.assertEqual(command[0], "/opt/codex")
        self.assertEqual(command[command.index("-m") + 1], "gpt-aut")
        self.assertEqual(command[command.index("-a") + 1], "on-request")
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(loaded.agent["runner"]["kind"], "codex-session")
        aut_host = next(host for host in loaded.hosts if host["id"] == "aut")
        self.assertEqual(aut_host["kind"], "codex-session")
        self.assertEqual(loaded.test["user_model"], "gpt-user")
        self.assertEqual(loaded.test["judge_model"], "gpt-judge")
        self.assertEqual(loaded.generation["model"], "gpt-generation")
        self.assertEqual(json.loads(runner_path.read_text()), loaded.agent["runner"])

    def test_resolved_config_reports_exact_codex_settings(self) -> None:
        from rehearsal.resolved_config import resolved_job_config

        spec_path = self._create()
        spec = load_spec(spec_path)
        runner = jobs.build_codex_aut_runner(
            spec, model="gpt-aut", approval_mode="never",
            codex_sandbox="read-only", timeout_seconds=777,
        )
        jobs.add_aut_host(spec, spec_path, runner)
        loaded = load_spec(spec_path)
        loaded.test["user_model"] = "gpt-user"
        loaded.test["judge_model"] = "gpt-judge"
        loaded.generation["model"] = "gpt-generation"
        resolved = resolved_job_config(loaded, spec_path)
        self.assertEqual(resolved["agent"]["runner"]["command"], runner["command"])
        self.assertEqual(resolved["agent"]["runner"]["model"], "gpt-aut")
        self.assertEqual(resolved["agent"]["runner"]["timeout_seconds"], 777)
        self.assertEqual(resolved["models"]["user_emulator"], "gpt-user")
        self.assertEqual(resolved["models"]["generation"], "gpt-generation")
        self.assertEqual(resolved["models"]["judge"], "gpt-judge")


class ConnectionEnvExpansionTest(unittest.TestCase):
    def test_expands_env_in_headers_at_connection_time(self) -> None:
        import os
        from unittest.mock import patch

        from rehearsal.config import expand_env
        from rehearsal.mcp_config import build_mcp_servers_config

        target = jobs.target_from_url(
            "http://localhost:8000/mcp",
            headers={"Authorization": "Bearer ${GH_TOKEN}"},
        )
        # The stored spec keeps the placeholder literal (no secret written).
        self.assertEqual(
            target.connection["headers"]["Authorization"], "Bearer ${GH_TOKEN}"
        )
        with patch.dict(os.environ, {"GH_TOKEN": "s3cret"}):
            expanded = expand_env(target.connection)
            self.assertEqual(expanded["headers"]["Authorization"], "Bearer s3cret")
            cfg = build_mcp_servers_config(target)
        server = next(iter(cfg["mcpServers"].values()))
        self.assertEqual(server["headers"]["Authorization"], "Bearer s3cret")

    def test_undefined_env_left_literal(self) -> None:
        from rehearsal.config import expand_env

        out = expand_env({"h": "Bearer ${DEFINITELY_UNSET_VAR_XYZ}"})
        self.assertEqual(out["h"], "Bearer ${DEFINITELY_UNSET_VAR_XYZ}")


class PromptOverrideTest(unittest.TestCase):
    def tearDown(self) -> None:
        prompts.set_overrides({})  # never leak overrides into other tests

    def test_override_replaces_builtin(self) -> None:
        prompts.set_overrides({"profile": "MY PROMPT {digest} :: {families}"})
        from rehearsal.profile import _build_prompt

        out = _build_prompt("DIG", ["a", "b"])
        self.assertEqual(out, "MY PROMPT DIG :: a, b")

    def test_blank_override_uses_builtin(self) -> None:
        prompts.set_overrides({"profile": "   "})
        from rehearsal.profile import _build_prompt

        out = _build_prompt("DIG", ["a"])
        self.assertIn("capability profile", out)

    def test_broken_override_falls_back(self) -> None:
        # attribute access on a missing name would raise; render must not
        prompts.set_overrides({"profile": "bad {digest.nope}"})
        from rehearsal.profile import _build_prompt

        out = _build_prompt("DIG", ["a"])
        self.assertIn("capability profile", out)

    def test_unknown_placeholder_left_literal(self) -> None:
        prompts.set_overrides({"profile": "{digest} and {mystery}"})
        from rehearsal.profile import _build_prompt

        out = _build_prompt("DIG", ["a"])
        self.assertEqual(out, "DIG and {mystery}")


class OpencodeSkillRunnerTest(unittest.TestCase):
    def test_skill_job_does_not_emit_a_fake_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "demo-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo.\n---\n# Demo\n",
                encoding="utf-8",
            )
            spec = jobs.default_skill_job_spec("demo-skill", skill_path=skill)
            spec.agent["runtime"] = {
                "backend": "opencode",
                "model": "github-copilot/mai-code-1.1-flash",
                "permission": {"bash": "allow", "edit": "allow"},
            }
            spec_path = jobs.create_job("demo-skill", spec, jobs_root=root / "jobs")
            runner = jobs.build_opencode_aut_runner(
                spec, spec_path, model="github-copilot/mai-code-1.1-flash",
            )
            project = spec_path.parent / "runners" / "opencode-aut" / "opencode.json"
            config = json.loads(project.read_text(encoding="utf-8"))
            self.assertNotIn("mcp", config)
            self.assertIn(str(skill.resolve()), config.get("skills", {}).get("paths", []))
            self.assertEqual(config["permission"]["bash"], "allow")
            self.assertIn("mai-code-1.1-flash", " ".join(runner["command"]))


if __name__ == "__main__":
    unittest.main()
