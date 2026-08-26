from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch

from rehearsal.cli import main
from rehearsal.config import RunnerConfig, ScenarioConfig, TargetConfig
from rehearsal.copilot_backend import (
    build_copilot_command,
    build_copilot_mcp_config,
    copilot_runner,
)
from rehearsal.jobs import (
    build_copilot_aut_runner,
    build_copilot_user_runner,
    create_job,
    default_job_spec,
    materialize_job_runners,
)
from rehearsal.orchestrator import run_scenario
from rehearsal.runners import (
    CopilotProcessRunner,
    CopilotSessionRunner,
    OpenShellCopilotSessionRunner,
    RunnerResult,
    create_runner,
)
from rehearsal.spec import load_spec
from rehearsal.tool_capture import parse_copilot_output


def _target() -> TargetConfig:
    return TargetConfig(
        id="notes",
        transport="streamable-http",
        connection={
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer ${COPILOT_MCP_TOKEN}"},
        },
    )


class CopilotCommandTest(unittest.TestCase):
    def test_builds_complete_declarative_command(self) -> None:
        runtime = {
            "copilot_bin": "/opt/copilot",
            "model": "claude-sonnet-4.6",
            "agent": "release-reviewer",
            "reasoning_effort": "high",
            "context": "long_context",
            "working_directory": "/workspace",
            "allow_all_tools": True,
            "allow_tools": ["notes(search)", "shell(git status)"],
            "deny_tools": ["write(.env)"],
            "available_tools": ["shell", "notes"],
            "excluded_tools": ["web"],
            "allow_urls": ["https://example.test"],
            "deny_urls": ["https://bad.test"],
            "add_dirs": ["/fixtures"],
            "disable_mcp_servers": ["global-server"],
            "plugin_dirs": ["/plugins/review"],
            "secret_env_vars": ["TOKEN", "API_KEY"],
            "max_ai_credits": 3,
            "no_custom_instructions": True,
            "extra_args": ["--log-level", "warning"],
        }
        command = build_copilot_command(runtime, mcps=[{
            "id": "notes",
            "transport": "streamable-http",
            "connection": {"url": "https://example.test/mcp"},
        }])
        self.assertEqual(command[0], "/opt/copilot")
        self.assertEqual(command[-1], "--prompt")
        self.assertIn("--output-format", command)
        self.assertIn("json", command)
        self.assertIn("--agent", command)
        self.assertEqual(command[command.index("--agent") + 1], "release-reviewer")
        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.assertEqual(command[command.index("--context") + 1], "long_context")
        self.assertIn("--available-tools=shell,notes", command)
        self.assertIn("--secret-env-vars=TOKEN,API_KEY", command)
        config_arg = command[command.index("--additional-mcp-config") + 1]
        config = json.loads(config_arg)
        self.assertEqual(config["mcpServers"]["notes"]["type"], "http")

    def test_rejects_args_that_break_ghostlab_protocol(self) -> None:
        with self.assertRaisesRegex(ValueError, "protocol flag"):
            build_copilot_command({"extra_args": ["--output-format=text"]})

    def test_normalizes_remote_and_stdio_mcp_servers(self) -> None:
        config = build_copilot_mcp_config([
            {
                "id": "remote",
                "transport": "sse",
                "connection": {
                    "url": "https://example.test/sse",
                    "headers": {"X-Test": "yes"},
                },
            },
            {
                "id": "local",
                "transport": "stdio",
                "connection": {
                    "command": ["python"],
                    "args": ["server.py"],
                    "env": {"MODE": "test"},
                },
            },
        ])
        self.assertEqual(config["mcpServers"]["remote"]["type"], "sse")
        self.assertEqual(config["mcpServers"]["local"]["command"], "python")
        self.assertEqual(config["mcpServers"]["local"]["args"], ["server.py"])
        self.assertEqual(config["mcpServers"]["local"]["env"], {"MODE": "test"})

    def test_runner_preserves_environment_and_role_config(self) -> None:
        runner = copilot_runner({
            "kind": "copilot-session",
            "model": "gpt-5.4",
            "env": {"COPILOT_GITHUB_TOKEN": "from-environment"},
            "timeout_seconds": 777,
        })
        self.assertEqual(runner.kind, "copilot-session")
        self.assertEqual(runner.parser, "copilot-json")
        self.assertEqual(runner.prompt_mode, "append-arg")
        self.assertEqual(runner.timeout_seconds, 777)
        self.assertEqual(
            runner.env["COPILOT_GITHUB_TOKEN"], "from-environment"
        )

    def test_runner_environment_expands_references_at_execution_time(self) -> None:
        config = RunnerConfig(
            kind="process",
            command=[
                sys.executable,
                "-c",
                "import os; print(os.environ['COPILOT_TEST_TOKEN'])",
            ],
            env={"COPILOT_TEST_TOKEN": "$GHOSTLAB_TEST_SOURCE"},
        )
        with patch.dict(os.environ, {"GHOSTLAB_TEST_SOURCE": "resolved-value"}):
            result = create_runner(config, "aut").run_turn("")
        self.assertEqual(result.output, "resolved-value")

    def test_mcp_secret_placeholder_is_persisted_but_expanded_for_execution(self) -> None:
        runner_config = copilot_runner(
            {},
            mcps=[{
                "id": "secure",
                "transport": "streamable-http",
                "connection": {
                    "url": "https://example.test/mcp",
                    "headers": {"Authorization": "Bearer ${COPILOT_MCP_TOKEN}"},
                },
            }],
        )
        serialized = json.dumps(runner_config.command)
        self.assertIn("${COPILOT_MCP_TOKEN}", serialized)
        self.assertNotIn("secret-at-runtime", serialized)
        runner = create_runner(runner_config, "aut")
        with patch.dict(
            os.environ, {"COPILOT_MCP_TOKEN": "secret-at-runtime"}
        ), patch(
            "rehearsal.runners._exec",
            return_value=RunnerResult(output="", exit_code=0),
        ) as execute:
            runner.run_turn("hello")
        executed = json.dumps(execute.call_args.args[0])
        self.assertIn("secret-at-runtime", executed)
        self.assertNotIn("${COPILOT_MCP_TOKEN}", executed)


class CopilotSessionRunnerTest(unittest.TestCase):
    def test_reuses_one_session_id_and_appends_each_prompt(self) -> None:
        config = RunnerConfig(
            kind="copilot-session",
            command=["copilot", "--output-format", "json", "--prompt"],
            prompt_mode="append-arg",
            parser="copilot-json",
        )
        runner = CopilotSessionRunner(config)
        with patch(
            "rehearsal.runners._exec",
            return_value=RunnerResult(output="", exit_code=0),
        ) as execute:
            runner.run_turn("first")
            runner.run_turn("second")

        first = execute.call_args_list[0].args[0]
        second = execute.call_args_list[1].args[0]
        first_id = first[first.index("--session-id") + 1]
        second_id = second[second.index("--session-id") + 1]
        self.assertEqual(first_id, second_id)
        self.assertEqual(first[-1], "first")
        self.assertEqual(second[-1], "second")
        self.assertEqual(runner.thread_id, first_id)
        self.assertTrue(create_runner(config, "aut").stateful)

    def test_requires_prompt_flag(self) -> None:
        with self.assertRaisesRegex(ValueError, "--prompt"):
            CopilotSessionRunner(
                RunnerConfig(kind="copilot-session", command=["copilot"])
            )

    def test_factory_supports_openshell_sessions(self) -> None:
        runner = create_runner(
            RunnerConfig(
                kind="copilot-session",
                command=["copilot", "--prompt"],
                prompt_mode="append-arg",
                parser="copilot-json",
                sandbox={"backend": "openshell"},
            ),
            "aut",
        )
        self.assertIsInstance(runner, OpenShellCopilotSessionRunner)

    def test_copilot_json_errors_fail_even_when_process_exits_zero(self) -> None:
        config = RunnerConfig(
            kind="process",
            command=["copilot", "--prompt"],
            prompt_mode="append-arg",
            parser="copilot-json",
        )
        runner = create_runner(config, "aut")
        self.assertIsInstance(runner, CopilotProcessRunner)
        output = json.dumps({
            "type": "session.error",
            "data": {"message": "model unavailable"},
        })
        with patch(
            "rehearsal.runners._exec",
            return_value=RunnerResult(output=output, exit_code=0),
        ):
            result = runner.run_turn("hello")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("model unavailable", result.stderr)


class CopilotOutputTest(unittest.TestCase):
    def test_extracts_final_message_and_mcp_calls(self) -> None:
        events = [
            {
                "type": "assistant.message",
                "data": {
                    "content": "I will look that up.",
                    "toolRequests": [{
                        "toolCallId": "mcp-1",
                        "name": "notes_search",
                        "arguments": {"query": "release"},
                        "mcpServerName": "notes",
                        "mcpToolName": "search",
                    }],
                },
            },
            {
                "type": "tool.execution_start",
                "data": {
                    "toolCallId": "mcp-1",
                    "toolName": "notes_search",
                    "arguments": {"query": "release"},
                    "mcpServerName": "notes",
                    "mcpToolName": "search",
                },
            },
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "mcp-1",
                    "success": True,
                    "result": {"content": [{"type": "text", "text": "found"}]},
                },
            },
            {
                "type": "tool.execution_start",
                "data": {
                    "toolCallId": "builtin-1",
                    "toolName": "shell",
                    "arguments": {"command": "pwd"},
                },
            },
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "builtin-1",
                    "success": False,
                    "error": {"message": "permission denied"},
                },
            },
            {
                "type": "assistant.message",
                "data": {"content": "The release note was found.", "toolRequests": []},
            },
        ]
        parsed = parse_copilot_output(
            "\n".join(json.dumps(event) for event in events)
        )
        self.assertEqual(parsed["message"], "The release note was found.")
        self.assertEqual(len(parsed["tool_calls"]), 1)
        self.assertEqual(parsed["tool_calls"][0]["server"], "notes")
        self.assertEqual(parsed["tool_calls"][0]["tool"], "search")
        self.assertEqual(parsed["tool_calls"][0]["status"], "completed")
        self.assertEqual(len(parsed["builtin_calls"]), 1)
        self.assertEqual(
            parsed["builtin_calls"][0]["failure_cause"], "permission_denied"
        )

    def test_dual_copilot_run_uses_parsed_conversation_messages(self) -> None:
        scenario = ScenarioConfig(
            id="copilot-flow",
            title="Copilot flow",
            persona="A user",
            goal="Get an answer",
            max_turns=3,
            success_criteria=["The assistant answers"],
            failure_signals=[],
            opening_message="Hello",
        )
        aut_config = copilot_runner(
            {"model": "gpt-5.4"}, sandbox={"backend": "local"}
        )
        user_config = copilot_runner(
            {"model": "gpt-5-mini"}, sandbox={"backend": "local"}
        )
        first_aut_output = "\n".join([
            json.dumps({
                "type": "assistant.message",
                "data": {"content": "What should I look up?", "toolRequests": []},
            }),
            json.dumps({"type": "result", "exitCode": 0}),
        ])
        first_user_output = "\n".join([
            json.dumps({
                "type": "assistant.message",
                "data": {"content": "Look up the release.", "toolRequests": []},
            }),
            json.dumps({"type": "result", "exitCode": 0}),
        ])
        second_aut_output = "\n".join([
            json.dumps({
                "type": "assistant.message",
                "data": {"content": "The release is ready.", "toolRequests": []},
            }),
            json.dumps({"type": "result", "exitCode": 0}),
        ])
        second_user_output = "\n".join([
            json.dumps({
                "type": "assistant.message",
                "data": {"content": "REHEARSAL_DONE", "toolRequests": []},
            }),
            json.dumps({"type": "result", "exitCode": 0}),
        ])
        with tempfile.TemporaryDirectory() as tmp, patch(
            "rehearsal.runners._exec",
            side_effect=[
                RunnerResult(output=first_aut_output, exit_code=0),
                RunnerResult(output=first_user_output, exit_code=0),
                RunnerResult(output=second_aut_output, exit_code=0),
                RunnerResult(output=second_user_output, exit_code=0),
            ],
        ):
            result = run_scenario(
                target=_target(),
                scenario=scenario,
                aut_runner_config=aut_config,
                user_runner_config=user_config,
                output_dir=Path(tmp),
            )
            events = [
                json.loads(line)
                for line in (result.run_dir / "events.jsonl").read_text().splitlines()
            ]

        self.assertEqual(result.status, "completed")
        aut_results = [event for event in events if event["type"] == "aut_result"]
        user_results = [
            event for event in events if event["type"] == "user_emulator_result"
        ]
        user_prompts = [
            event for event in events if event["type"] == "user_emulator_prompt"
        ]
        self.assertEqual(aut_results[0]["data"]["output"], "What should I look up?")
        self.assertEqual(user_results[-1]["data"]["output"], "REHEARSAL_DONE")
        self.assertIn("The conversation so far", user_prompts[0]["data"]["prompt"])
        self.assertNotIn("The conversation so far", user_prompts[1]["data"]["prompt"])
        self.assertIn("The release is ready.", user_prompts[1]["data"]["prompt"])


class CopilotJobTest(unittest.TestCase):
    def test_builds_distinct_aut_and_user_runners(self) -> None:
        spec = default_job_spec("Notes", target=_target())
        spec.sandbox["backend"] = "local"
        aut = build_copilot_aut_runner(
            spec,
            runtime={"model": "gpt-5.4", "agent": "notes-agent"},
        )
        user = build_copilot_user_runner(
            spec,
            runtime={"model": "gpt-5-mini", "agent": "persona-agent"},
        )
        self.assertEqual(aut["kind"], "copilot-session")
        self.assertIn("--additional-mcp-config", aut["command"])
        self.assertNotIn("--additional-mcp-config", user["command"])
        disabled_index = user["command"].index("--disable-mcp-server")
        self.assertEqual(user["command"][disabled_index + 1], "notes")
        self.assertEqual(
            aut["command"][aut["command"].index("--agent") + 1], "notes-agent"
        )
        self.assertEqual(
            user["command"][user["command"].index("--agent") + 1],
            "persona-agent",
        )

    def test_materializes_both_role_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = default_job_spec("Notes", target=_target())
            spec.sandbox["backend"] = "local"
            spec.agent["runtime"] = {
                "backend": "copilot",
                "model": "gpt-5.4",
                "agent": "notes-agent",
            }
            spec.test["user_runtime"] = {
                "backend": "copilot",
                "model": "gpt-5-mini",
                "agent": "persona-agent",
            }
            spec_path = create_job("Notes", spec, jobs_root=root / "jobs")
            written = materialize_job_runners(spec_path)
            loaded = load_spec(spec_path)

            self.assertEqual(set(written), {"aut", "user"})
            self.assertTrue((spec_path.parent / "runners" / "aut.json").is_file())
            self.assertTrue((spec_path.parent / "runners" / "user.json").is_file())
            aut_host = next(host for host in loaded.hosts if host["id"] == "aut")
            self.assertEqual(aut_host["kind"], "copilot-session")
            self.assertEqual(loaded.test["user_runner"], "runners/user.json")
            self.assertEqual(loaded.agent["runtime"]["agent"], "notes-agent")
            self.assertEqual(
                loaded.test["user_runtime"]["agent"], "persona-agent"
            )

    def test_create_cli_wires_full_copilot_config_for_both_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jobs_root = Path(tmp) / "jobs"
            with patch.dict(
                "os.environ", {"GHOSTLAB_JOBS_DIR": str(jobs_root)}
            ):
                code = main([
                    "create",
                    "--name",
                    "Copilot Eval",
                    "--target",
                    "https://example.test/mcp",
                    "--sandbox",
                    "local",
                    "--aut-backend",
                    "copilot",
                    "--user-backend",
                    "copilot",
                    "--model",
                    "gpt-5.4",
                    "--user-model",
                    "gpt-5-mini",
                    "--aut-agent",
                    "release-agent",
                    "--user-agent",
                    "persona-agent",
                    "--aut-reasoning-effort",
                    "high",
                    "--user-context",
                    "long_context",
                    "--copilot-bin",
                    "/opt/copilot",
                    "--aut-copilot-arg=--no-custom-instructions",
                    "--aut-runner-env",
                    "AUT_SETTING=yes",
                    "--user-runner-env",
                    "USER_SETTING=yes",
                    "--no-discover",
                    "--yes",
                ])
            self.assertEqual(code, 0)
            spec_path = jobs_root / "copilot-eval" / "job.yaml"
            spec = load_spec(spec_path)
            aut = json.loads(
                (spec_path.parent / "runners" / "aut.json").read_text()
            )
            user = json.loads(
                (spec_path.parent / "runners" / "user.json").read_text()
            )
            self.assertEqual(spec.agent["runtime"]["backend"], "copilot")
            self.assertEqual(spec.agent["runtime"]["agent"], "release-agent")
            self.assertEqual(spec.test["user_runtime"]["agent"], "persona-agent")
            self.assertEqual(aut["env"], {"AUT_SETTING": "yes"})
            self.assertEqual(user["env"], {"USER_SETTING": "yes"})
            self.assertIn("--no-custom-instructions", aut["command"])
            self.assertIn("--disable-mcp-server", user["command"])

    def test_interactive_create_wizard_selects_copilot_agents_for_both_roles(self) -> None:
        answers = [
            "copilot",
            "gpt-5.4",
            "copilot-session",
            "600",
            "release-agent",
            "high",
            "long_context",
            "",
            "copilot",
            "gpt-5-mini",
            "persona-agent",
            "low",
            "default",
            "",
            "",
            "",
            "2",
            "2",
            "0.9",
            "y",
        ]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ", {"GHOSTLAB_JOBS_DIR": str(Path(tmp) / "jobs")}
        ), patch(
            "builtins.input", side_effect=answers
        ), patch(
            "sys.stdout", new_callable=io.StringIO
        ):
            code = main([
                "create",
                "--name",
                "Wizard Copilot",
                "--target",
                "https://example.test/mcp",
                "--sandbox",
                "local",
                "--no-discover",
            ])
            spec = load_spec(Path(tmp) / "jobs" / "wizard-copilot" / "job.yaml")

        self.assertEqual(code, 0)
        self.assertEqual(spec.agent["runtime"]["backend"], "copilot")
        self.assertEqual(spec.agent["runtime"]["agent"], "release-agent")
        self.assertEqual(spec.test["user_runtime"]["backend"], "copilot")
        self.assertEqual(spec.test["user_runtime"]["agent"], "persona-agent")


if __name__ == "__main__":
    unittest.main()
