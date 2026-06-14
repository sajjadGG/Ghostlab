"""Tests for persisted run events and prompt transparency."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rehearsal.config import PersonaConfig, RunnerConfig, ScenarioConfig, TargetConfig
from rehearsal.orchestrator import run_scenario


class OrchestratorEventsTest(unittest.TestCase):
    def test_run_persists_exact_agent_and_user_emulator_prompts(self) -> None:
        target = TargetConfig(id="demo", transport="streamable-http", connection={"url": "http://mcp"})
        scenario = ScenarioConfig(
            id="case-1",
            title="Test case",
            persona="Needs concise help",
            goal="Complete the task",
            max_turns=3,
            success_criteria=["Task completed"],
            failure_signals=[],
            opening_message="Please help.",
        )
        persona = PersonaConfig(id="p1", name="Pat", summary="A careful user")

        with tempfile.TemporaryDirectory() as tmp:
            result = run_scenario(
                target=target,
                scenario=scenario,
                aut_runner_config=RunnerConfig(kind="mock"),
                user_runner_config=RunnerConfig(kind="mock"),
                output_dir=Path(tmp),
                persona=persona,
            )
            events = [
                json.loads(line)
                for line in (result.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        types = [event["type"] for event in events]
        self.assertIn("aut_prompt", types)
        self.assertIn("user_emulator_prompt", types)
        started = next(event for event in events if event["type"] == "run_started")
        self.assertEqual(started["data"]["models"]["agent_under_test"], "codex default")
        user_prompt = next(event for event in events if event["type"] == "user_emulator_prompt")
        self.assertIn("A careful user", user_prompt["data"]["prompt"])
        self.assertIn("Complete the task", user_prompt["data"]["prompt"])


if __name__ == "__main__":
    unittest.main()
