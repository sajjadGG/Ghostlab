from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rehearsal.agents import load_agent_definition
from rehearsal.jobs import build_codex_aut_runner, create_job, default_agent_job_spec


class AgentDefinitionTest(unittest.TestCase):
    def test_resolves_composed_agent_inputs_and_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "workspace").mkdir()
            skill = root / "skill" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("# Skill\nDo the work.\n", encoding="utf-8")
            mcp = root / "mcp.json"
            mcp.write_text(json.dumps({"mcpServers": {"notes": {"url": "https://example.com/mcp"}}}))
            agent_path = root / "agent.json"
            agent_path.write_text(json.dumps({
                "id": "research-agent",
                "instructions": "Use the configured capabilities.",
                "runner": {"kind": "process", "command": ["codex", "exec", "-"]},
                "workspace": "workspace",
                "inputs": {
                    "mcps": [{"config_ref": "mcp.json", "server": "notes"}],
                    "skills": ["skill"],
                },
                "sandbox": {"env_allowlist": ["OPENAI_API_KEY"]},
            }), encoding="utf-8")

            agent, sandbox = load_agent_definition(agent_path)
            self.assertEqual(agent["inputs"]["mcps"][0]["id"], "notes")
            self.assertEqual(agent["inputs"]["skills"][0]["path"], str(skill.resolve()))
            self.assertEqual(sandbox["uploads"][0]["target"], "/sandbox")
            self.assertEqual(sandbox["workdir"], "/sandbox/workspace")
            spec = default_agent_job_spec("Research", agent=agent, sandbox=sandbox)
            self.assertEqual(spec.agent["id"], "research-agent")
            self.assertEqual(spec.sandbox["backend"], "openshell")
            self.assertEqual(spec.target_type, "mcp")

    def test_codex_runner_wires_multiple_mcps(self) -> None:
        agent = {
            "id": "combo", "runner": {}, "inputs": {"skills": [], "mcps": [
                {"id": "one", "transport": "streamable-http", "connection": {"url": "https://one/mcp"}},
                {"id": "two", "transport": "stdio", "connection": {"command": "node", "args": ["server.js"]}},
            ]},
        }
        spec = default_agent_job_spec("Combo", agent=agent)
        runner = build_codex_aut_runner(spec)
        rendered = " ".join(runner["command"])
        self.assertIn("mcp_servers.one.url", rendered)
        self.assertIn("mcp_servers.two.command", rendered)
        self.assertEqual(runner["sandbox"]["backend"], "openshell")

    def test_agent_without_mcp_or_skill_is_still_evaluable(self) -> None:
        agent = {
            "id": "plain", "instructions": "Answer tersely.",
            "runner": {"kind": "process", "command": ["my-agent"]},
            "inputs": {"mcps": [], "skills": []},
        }
        spec = default_agent_job_spec("Plain", agent=agent)
        self.assertEqual(spec.target_type, "agent")
        self.assertEqual(spec.target_config().transport, "agent")

    def test_inline_agent_tests_seed_scenarios_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent = {
                "id": "plain", "runner": {"kind": "process", "command": ["my-agent"]},
                "inputs": {"mcps": [], "skills": []},
                "tests": [{
                    "id": "hello", "goal": "get a greeting", "opening_message": "hello",
                    "success_criteria": ["greets the user"],
                }],
            }
            spec = default_agent_job_spec("Plain", agent=agent)
            spec_path = create_job("Plain", spec, jobs_root=Path(tmp))
            self.assertTrue((spec_path.parent / "scenarios" / "hello.json").exists())
            plan = (spec_path.parent / "test-plan.yaml").read_text(encoding="utf-8")
            self.assertIn("semantic-agent-hello", plan)
