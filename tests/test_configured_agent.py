"""Tests for the configured-agent lab: config surface, purpose, sandbox, report."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rehearsal.agent_profile import (
    as_capability_profile, build_agent_profile, collect_evidence, profile_digest,
    profile_prompt,
)
from rehearsal.agent_sandbox import (
    AgentSandbox, ensure_agent_policy, opencode_auth_path, prepare_agent_sandbox,
    provider_endpoints, render_egress_policy,
)
from rehearsal.opencode_config import (
    OpencodeConfigError, build_project_config, runtime_input_paths, validate_runtime,
)
from rehearsal.rollout_report import collect_rollout, redact, render_html
from rehearsal.sandbox import SandboxError


class OpencodeConfigTest(unittest.TestCase):
    def test_full_surface_is_emitted_with_opencode_names(self) -> None:
        runtime = {
            "backend": "opencode",          # Ghostlab-only, must not be forwarded
            "timeout_seconds": 600,          # Ghostlab-only
            "model": "prov/m",
            "small_model": "prov/s",
            "default_agent": "build",
            "subagent_depth": 2,
            "tools": {"webfetch": False},
            "agents": {"build": {"prompt": "do the thing", "temperature": 0.2}},
            "commands": {"ship": {"template": "x"}},
            "plugins": ["p"],
        }
        config = build_project_config(runtime)
        self.assertEqual(config["model"], "prov/m")
        self.assertEqual(config["subagent_depth"], 2)
        # Ghostlab pluralizes for readability; OpenCode's own keys go out.
        self.assertIn("agent", config)
        self.assertIn("command", config)
        self.assertIn("plugin", config)
        for internal in ("backend", "timeout_seconds", "agents", "commands", "plugins"):
            self.assertNotIn(internal, config)

    def test_defaults_deny_side_effecting_tools_and_self_update(self) -> None:
        config = build_project_config({})
        self.assertEqual(config["permission"]["bash"], "deny")
        self.assertEqual(config["permission"]["edit"], "deny")
        self.assertIs(config["autoupdate"], False)

    def test_explicit_permission_merges_over_the_default(self) -> None:
        config = build_project_config({"permission": {"edit": "allow"}})
        self.assertEqual(config["permission"]["edit"], "allow")
        self.assertEqual(config["permission"]["bash"], "deny")

    def test_unknown_option_is_rejected_with_the_offending_key(self) -> None:
        with self.assertRaises(OpencodeConfigError) as ctx:
            validate_runtime({"modle": "typo/here"})
        self.assertIn("modle", str(ctx.exception))

    def test_unknown_agent_option_is_rejected(self) -> None:
        with self.assertRaises(OpencodeConfigError) as ctx:
            validate_runtime({"agents": {"build": {"promt": "typo"}}})
        self.assertIn("promt", str(ctx.exception))

    def test_paths_are_rewritten_for_the_sandbox(self) -> None:
        runtime = {
            "instructions": ["/host/AGENTS.md"],
            "skills": {"paths": ["/host/skills/x"]},
        }
        config = build_project_config(
            runtime, path_map=lambda value: str(value).replace("/host", "/sandbox/agent")
        )
        self.assertEqual(config["instructions"], ["/sandbox/agent/AGENTS.md"])
        self.assertEqual(config["skills"]["paths"], ["/sandbox/agent/skills/x"])

    def test_stdio_and_remote_mcps_both_render(self) -> None:
        config = build_project_config(None, [
            {"id": "local", "transport": "stdio",
             "connection": {"command": "node", "args": ["s.js"], "env": {"A": "1"}}},
            {"id": "remote", "transport": "streamable-http",
             "connection": {"url": "http://x/mcp", "headers": {"H": "v"}}},
        ])
        self.assertEqual(config["mcp"]["local"]["command"], ["node", "s.js"])
        self.assertEqual(config["mcp"]["local"]["environment"], {"A": "1"})
        self.assertEqual(config["mcp"]["remote"]["type"], "remote")
        self.assertEqual(config["mcp"]["remote"]["url"], "http://x/mcp")

    def test_input_paths_are_collected_for_upload(self) -> None:
        paths = runtime_input_paths(
            {"instructions": ["/a/AGENTS.md"], "skills": {"paths": ["/a/skills"]},
             "agents": {"b": {"prompt": "/a/prompt.md"}}},
            [{"path": "/a/other-skill"}],
        )
        self.assertEqual(
            sorted(paths), ["/a/AGENTS.md", "/a/other-skill", "/a/prompt.md", "/a/skills"]
        )


class AgentDefinitionTest(unittest.TestCase):
    """A file-driven agent config is what a coding harness can actually drive."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "AGENTS.md").write_text("Be careful.", encoding="utf-8")
        (self.tmp / "repo").mkdir()
        skill = self.tmp / "skills" / "notes"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("Draft notes.", encoding="utf-8")
        (self.tmp / "server.js").write_text("// mcp", encoding="utf-8")
        (self.tmp / "mcp.json").write_text(json.dumps(
            {"mcpServers": {"tiny": {"command": "node", "args": ["server.js"]}}}
        ), encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, payload: dict) -> Path:
        path = self.tmp / "agent.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_runtime_description_and_workspace_survive_loading(self) -> None:
        from rehearsal.agents import load_agent_definition

        path = self._write({
            "id": "a", "description": "Cuts releases.",
            "runtime": {
                "backend": "opencode", "model": "prov/m",
                "instructions": ["AGENTS.md"],
                "skills": {"paths": ["skills/notes"]},
            },
            "workspace": "repo",
            "inputs": {"mcps": [], "skills": []},
        })
        agent, _sandbox = load_agent_definition(path)
        self.assertEqual(agent["runtime"]["model"], "prov/m")
        self.assertEqual(agent["description"], "Cuts releases.")
        # Paths resolve against the agent file, not the caller's cwd.
        root = self.tmp.resolve()   # macOS resolves /var -> /private/var
        self.assertEqual(agent["runtime"]["instructions"], [str(root / "AGENTS.md")])
        self.assertEqual(
            agent["runtime"]["skills"]["paths"], [str(root / "skills" / "notes")]
        )
        self.assertTrue(agent["workspace"].endswith("repo"))

    def test_referenced_mcp_program_resolves_beside_its_config(self) -> None:
        from rehearsal.agents import load_agent_definition

        path = self._write({
            "id": "a", "runtime": {"backend": "opencode"},
            "inputs": {"mcps": [{"config_ref": "mcp.json", "server": "tiny"}]},
        })
        agent, _sandbox = load_agent_definition(path)
        args = agent["inputs"]["mcps"][0]["connection"]["args"]
        self.assertEqual(args, [str((self.tmp.resolve() / "server.js"))])

    def test_flags_and_package_names_are_left_alone(self) -> None:
        from rehearsal.agents import load_agent_definition

        (self.tmp / "mcp.json").write_text(json.dumps(
            {"mcpServers": {"pkg": {"command": "npx", "args": ["-y", "safari-mcp"]}}}
        ), encoding="utf-8")
        path = self._write({
            "id": "a", "runtime": {"backend": "opencode"},
            "inputs": {"mcps": [{"config_ref": "mcp.json", "server": "pkg"}]},
        })
        agent, _sandbox = load_agent_definition(path)
        self.assertEqual(agent["inputs"]["mcps"][0]["connection"]["args"], ["-y", "safari-mcp"])


class AgentProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "AGENTS.md").write_text("Never push without asking.", encoding="utf-8")
        skill = self.tmp / "skills" / "notes"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("Draft release notes.", encoding="utf-8")
        self.agent = {
            "id": "rel", "name": "Release Bot",
            "description": "Cuts releases.",
            "runtime": {
                "backend": "opencode",
                "instructions": [str(self.tmp / "AGENTS.md")],
                "skills": {"paths": [str(skill)]},
                "agents": {"main": {"description": "does it", "prompt": "inline text"}},
                "permission": {"bash": "deny"},
            },
            "inputs": {"mcps": [], "skills": []},
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_evidence_reads_the_referenced_files(self) -> None:
        evidence = collect_evidence(self.agent, {"tools": [{"name": "t", "description": "d"}]})
        self.assertEqual(evidence["description"], "Cuts releases.")
        self.assertIn("Never push", evidence["instructions"][0]["content"])
        self.assertIn("release notes", evidence["skills"][0]["content"])
        self.assertEqual(evidence["subagents"][0]["name"], "main")
        self.assertEqual(evidence["capabilities"][0]["name"], "t")

    def test_prompt_marks_the_owner_description_authoritative(self) -> None:
        prompt = profile_prompt(collect_evidence(self.agent))
        self.assertIn("authoritative", prompt)
        self.assertIn("Cuts releases.", prompt)
        self.assertIn("Never push", prompt)

    def test_missing_description_is_called_out_rather_than_faked(self) -> None:
        agent = {**self.agent, "description": ""}
        self.assertIn("none given", profile_prompt(collect_evidence(agent)))

    def test_build_records_its_evidence(self) -> None:
        class Backend:
            def generate_json(self, prompt, schema):
                return {"purpose": "p", "audience": "a", "workflows": [], "risk_surface": []}

        profile = build_agent_profile(self.agent, Backend())
        self.assertEqual(profile["agent_id"], "rel")
        self.assertTrue(profile["evidence"]["from_description"])
        self.assertEqual(len(profile["evidence"]["skills"]), 1)

    def test_adapter_feeds_the_existing_generators(self) -> None:
        profile = {
            "purpose": "Cuts releases", "audience": "maintainers",
            "workflows": [{"name": "cut", "steps": ["a", "b"]}],
            "risk_surface": [{"risk": "pushes", "why": "has git"}],
            "agent_id": "rel",
        }
        adapted = as_capability_profile(profile, {"tools": []})
        self.assertEqual(adapted["target_type"], "agent")
        self.assertIn("Cuts releases", adapted["domain_summary"])
        # Scenario generation reads `instructions` verbatim for agent targets.
        self.assertIn("Risk 'pushes'", adapted["instructions"])
        self.assertIs(adapted["agent_profile"], profile)

    def test_digest_includes_workflows_and_risks(self) -> None:
        digest = profile_digest({
            "purpose": "p", "workflows": [{"name": "w", "steps": ["s"]}],
            "risk_surface": [{"risk": "r", "why": "y"}], "out_of_scope": ["o"],
        })
        self.assertIn("Workflow 'w'", digest)
        self.assertIn("Risk 'r'", digest)
        self.assertIn("Out of scope: o", digest)


class AgentSandboxTest(unittest.TestCase):
    def test_provider_endpoints_include_the_model_catalog(self) -> None:
        hosts = provider_endpoints("github-copilot/claude-sonnet-4.5")
        # Without the catalog OpenCode reports "Model not found" before it ever
        # reaches the provider.
        self.assertIn("models.dev", hosts)
        self.assertIn("api.githubcopilot.com", hosts)

    def test_policy_grants_the_agent_data_dir(self) -> None:
        policy = render_egress_policy(["example.com"])
        self.assertIn("/opt/agent", policy)          # default policy denies /opt
        self.assertIn("host: example.com", policy)
        self.assertIn("version: 1", policy)

    def test_policy_is_generated_when_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = ensure_agent_policy({}, "github-copilot/x", Path(tmp))
            self.assertTrue(Path(config["policy"]).is_file())
            self.assertEqual(config["network"], "policy")

    def test_supplied_policy_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = ensure_agent_policy({"policy": "/mine.yaml"}, "x/y", Path(tmp))
            self.assertEqual(config["policy"], "/mine.yaml")

    def test_unknown_provider_without_policy_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SandboxError) as ctx:
                ensure_agent_policy({}, "mystery/model", Path(tmp))
        self.assertEqual(ctx.exception.kind, "sandbox_policy_missing")

    def test_local_backend_is_refused_for_a_configured_agent(self) -> None:
        with self.assertRaises(SandboxError) as ctx:
            prepare_agent_sandbox({"id": "a"}, {"backend": "local"})
        self.assertIn("must run under OpenShell", ctx.exception.detail)

    def test_missing_credentials_fail_before_the_sandbox_is_built(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                patch("rehearsal.agent_sandbox.opencode_auth_path",
                      return_value=Path(tmp) / "nope.json"):
            with self.assertRaises(SandboxError) as ctx:
                prepare_agent_sandbox(
                    {"id": "a", "runtime": {"model": "github-copilot/x"}},
                    {"backend": "openshell", "credentials": {"opencode_auth": True}},
                    artifact_dir=Path(tmp),
                )
        self.assertEqual(ctx.exception.kind, "sandbox_credentials_missing")

    def test_remote_path_maps_uploaded_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / "src").mkdir(parents=True)
            handle = AgentSandbox(
                sandbox=None, project_remote="/sandbox/agent",
                workspace_remote="/sandbox/workspace/repo",
                uploads=[{"source": str(root), "target": "/sandbox/workspace"}],
            )
            self.assertEqual(
                handle.remote_path(root / "src" / "app.py"),
                "/sandbox/workspace/repo/src/app.py",
            )
            # A path that was never uploaded is left alone rather than guessed at.
            self.assertEqual(handle.remote_path("/elsewhere/x"), "/elsewhere/x")

    def test_auth_path_honours_the_override(self) -> None:
        with patch.dict("os.environ", {"GHOSTLAB_OPENCODE_AUTH": "/tmp/auth.json"}):
            self.assertEqual(opencode_auth_path(), Path("/tmp/auth.json"))


class RolloutReportTest(unittest.TestCase):
    def test_secrets_are_redacted_before_rendering(self) -> None:
        cleaned = redact({
            "headers": {"Authorization": "Bearer abc"},
            "credentials": {"access": "tok", "opencode_auth": True},
            "model": "prov/m",
        })
        self.assertEqual(cleaned["headers"]["Authorization"], "«redacted»")
        self.assertEqual(cleaned["credentials"]["access"], "«redacted»")
        self.assertEqual(cleaned["model"], "prov/m")

    def test_collect_reassembles_turns_and_tool_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            events = [
                {"type": "user_message", "data": {"turn": 1, "message": "hi"}},
                {"type": "aut_result", "data": {
                    "turn": 1, "output": "hello",
                    "tool_calls": [{"server": "s", "tool": "t", "status": "completed"}]}},
            ]
            (run / "events.jsonl").write_text(
                "\n".join(json.dumps(item) for item in events), encoding="utf-8"
            )
            (run / "verdict.json").write_text(
                json.dumps({"verdict": "pass", "judge": {"summary": "good"}}), encoding="utf-8"
            )
            rollout = collect_rollout(run)
        self.assertEqual(len(rollout["turns"]), 1)
        self.assertEqual(rollout["turns"][0]["assistant"], "hello")
        self.assertEqual(rollout["verdict"]["verdict"], "pass")

    def test_html_contains_the_conversation_and_verdict(self) -> None:
        rollout = {
            "run_id": "r1",
            "turns": [{"turn": 1, "user": "do it", "assistant": "done",
                       "tool_calls": [{"server": "s", "tool": "t", "status": "completed",
                                       "duration_ms": 12.0, "arguments": {"a": 1}}]}],
            "verdict": {"verdict": "pass", "judge": {"summary": "all good", "criteria": [
                {"met": True, "evidence": "because"}]}},
            "critique": {"overall_score": 4, "top_recommendations": ["document x"]},
        }
        page = render_html(
            rollout, title="T", agent={"runtime": {"model": "prov/m"}},
            sandbox={"credentials": {"access": "secret-token"}},
        )
        self.assertIn("do it", page)
        self.assertIn("s/t", page)
        self.assertIn("all good", page)
        self.assertIn("document x", page)
        # The secret must never reach the document.
        self.assertNotIn("secret-token", page)

    def test_html_escapes_untrusted_transcript_text(self) -> None:
        page = render_html({"run_id": "r", "turns": [
            {"turn": 1, "user": "<script>alert(1)</script>", "tool_calls": []}
        ]})
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


if __name__ == "__main__":
    unittest.main()
