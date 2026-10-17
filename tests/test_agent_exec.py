"""`ghostlab exec`: one agent definition, any harness, one answer."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from rehearsal import agent_exec
from rehearsal.agent_exec import (
    ExecConfig,
    ExecError,
    build_invocation,
    claude_mcp_config,
    compose_prompt,
    execute,
    resolve_backend,
)
from rehearsal.runner_presets import codex_mcp_overrides
from rehearsal.runners import RunnerResult
from rehearsal.tool_capture import parse_claude_output, parse_codex_output

STDIO = {
    "id": "tiny", "transport": "stdio",
    "connection": {"command": "node", "args": ["/srv/tiny-mcp.js"], "env": {"LEVEL": "1"}},
}
HTTP = {
    "id": "hub", "transport": "streamable-http",
    "connection": {
        "url": "https://hub.example/mcp",
        "headers": {"Authorization": "Bearer ${HUB_TOKEN}", "X-Team": "qa"},
    },
}


def agent_with(backend: str, **extra) -> dict:
    return {
        "id": "helper",
        "instructions": "Be concise.",
        "runtime": {"backend": backend, "model": "m-1", **extra},
        "inputs": {"mcps": [STDIO, HTTP], "skills": []},
    }


class ResolveBackendTest(unittest.TestCase):
    def test_runtime_backend_wins_and_flag_overrides(self) -> None:
        self.assertEqual(resolve_backend(agent_with("claude")), "claude")
        self.assertEqual(resolve_backend(agent_with("claude"), "codex"), "codex")

    def test_raw_runner_command_is_its_own_backend(self) -> None:
        self.assertEqual(resolve_backend({"runner": {"command": ["my-agent"]}}), "runner")

    def test_no_harness_is_a_clear_error(self) -> None:
        with self.assertRaisesRegex(ExecError, "names no harness"):
            resolve_backend({"id": "x"})
        with self.assertRaisesRegex(ExecError, "unsupported backend"):
            resolve_backend(agent_with("gemini"))


class CodexMcpOverridesTest(unittest.TestCase):
    def test_stdio_command_is_a_string_with_separate_args(self) -> None:
        # A list-valued `command` makes codex refuse to load its config at all.
        args = codex_mcp_overrides([STDIO])
        self.assertIn('mcp_servers.tiny.command="node"', args)
        self.assertIn('mcp_servers.tiny.args=["/srv/tiny-mcp.js"]', args)
        self.assertIn('mcp_servers.tiny.env.LEVEL="1"', args)

    def test_http_headers_map_to_bearer_and_static_headers(self) -> None:
        args = codex_mcp_overrides([HTTP])
        self.assertIn('mcp_servers.hub.url="https://hub.example/mcp"', args)
        self.assertIn('mcp_servers.hub.bearer_token_env_var="HUB_TOKEN"', args)
        self.assertIn('mcp_servers.hub.http_headers.X-Team="qa"', args)

    def test_env_only_header_is_read_from_the_environment(self) -> None:
        mcp = {"id": "k", "transport": "http",
               "connection": {"url": "u", "headers": {"X-Key": "${API_KEY}"}}}
        self.assertIn('mcp_servers.k.env_http_headers.X-Key="API_KEY"', codex_mcp_overrides([mcp]))


class BuildInvocationTest(unittest.TestCase):
    def test_codex_attaches_every_mcp_and_runs_in_workdir(self) -> None:
        inv = build_invocation(agent_with("codex"), "codex", workdir="/w")
        self.assertEqual(inv.parser, "codex-json")
        self.assertEqual(inv.prompt_mode, "stdin")
        self.assertIn("-m", inv.argv)
        self.assertIn('mcp_servers.tiny.command="node"', inv.argv)
        self.assertIn('mcp_servers.hub.url="https://hub.example/mcp"', inv.argv)
        self.assertEqual(inv.argv[-4:], ["exec", "--json", "--skip-git-repo-check", "-"])
        self.assertEqual(inv.argv[inv.argv.index("-C") + 1], "/w")

    def test_claude_gets_strict_mcp_config_and_preapproved_mcp_tools(self) -> None:
        inv = build_invocation(
            agent_with("claude", permission_mode="acceptEdits", allow_tools=["Read"]),
            "claude", workdir="/w",
        )
        self.assertEqual(inv.parser, "claude-json")
        self.assertEqual(inv.argv[:5], ["claude", "-p", "--output-format", "stream-json", "--verbose"])
        config = json.loads(inv.argv[inv.argv.index("--mcp-config") + 1])
        self.assertEqual(config["mcpServers"]["tiny"]["command"], "node")
        self.assertEqual(config["mcpServers"]["hub"]["type"], "http")
        self.assertIn("--strict-mcp-config", inv.argv)
        allowed = inv.argv[inv.argv.index("--allowedTools") + 1].split(",")
        self.assertEqual(allowed, ["mcp__tiny", "mcp__hub", "Read"])
        self.assertIn("acceptEdits", inv.argv)

    def test_copilot_on_host_is_tagged_and_in_sandbox_is_image_compatible(self) -> None:
        host = build_invocation(agent_with("copilot"), "copilot", workdir="/w")
        self.assertEqual(host.prompt_mode, "append-arg")
        self.assertIn("--session-id", host.argv)
        self.assertEqual(host.argv[-1], "--prompt")
        self.assertNotIn("-C", host.argv)
        boxed = build_invocation(
            agent_with("copilot", copilot_bin="/opt/homebrew/bin/copilot"),
            "copilot", workdir="/w", sandboxed=True,
        )
        self.assertEqual(boxed.argv[0], "copilot")
        self.assertNotIn("--session-id", boxed.argv)
        self.assertNotIn("--no-remote", boxed.argv)

    def test_opencode_writes_a_project_config_with_mcps_and_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "notes" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("---\nname: notes\n---\nWrite notes.", encoding="utf-8")
            agent = agent_with("opencode")
            agent["inputs"]["skills"] = [{"path": str(skill)}]
            inv = build_invocation(agent, "opencode", workdir="/w")
        config = json.loads(inv.files["opencode.json"])
        self.assertEqual(set(config["mcp"]), {"tiny", "hub"})
        self.assertEqual(config["skills"]["paths"], [str(skill)])
        self.assertEqual(inv.argv[inv.argv.index("--dir") + 1], "/w")
        self.assertEqual(inv.argv[inv.argv.index("--model") + 1], "m-1")

    def test_sandbox_path_map_rewrites_stdio_programs(self) -> None:
        inv = build_invocation(
            agent_with("claude"), "claude", workdir="/w", sandboxed=True,
            path_map=lambda value: str(value).replace("/srv", "/sandbox/mcp/srv"),
        )
        config = json.loads(inv.argv[inv.argv.index("--mcp-config") + 1])
        self.assertEqual(config["mcpServers"]["tiny"]["args"], ["/sandbox/mcp/srv/tiny-mcp.js"])

    def test_raw_runner_is_passed_through(self) -> None:
        agent = {"runner": {"command": ["my-agent", "--json"], "parser": "codex-json",
                            "prompt_mode": "append-arg", "env": {"A": "1"}}}
        inv = build_invocation(agent, "runner", workdir="/w")
        self.assertEqual(inv.argv, ["my-agent", "--json"])
        self.assertEqual((inv.prompt_mode, inv.parser, inv.env), ("append-arg", "codex-json", {"A": "1"}))


class ComposePromptTest(unittest.TestCase):
    def test_skills_travel_in_the_prompt_except_for_opencode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "release-notes" / "SKILL.md"
            skill.parent.mkdir()
            skill.write_text("Group by Added and Fixed.", encoding="utf-8")
            agent = {"instructions": "Be concise.", "inputs": {"skills": [{"path": str(skill)}]}}
            codex = compose_prompt(agent, "Draft notes", "codex")
            opencode = compose_prompt(agent, "Draft notes", "opencode")
        self.assertIn("## Skill: release-notes", codex)
        self.assertIn("Group by Added and Fixed.", codex)
        self.assertTrue(codex.endswith("Task:\nDraft notes"))
        self.assertIn("Be concise.", opencode)
        self.assertNotIn("Group by Added", opencode)

    def test_bare_agent_sends_the_task_unchanged(self) -> None:
        self.assertEqual(compose_prompt({}, "hello", "claude"), "hello")


class ClaudeParserTest(unittest.TestCase):
    STREAM = "\n".join(json.dumps(event) for event in [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__tiny__host_info", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "linux 6.12"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "denied", "is_error": True},
        ]}},
        {"type": "user", "message": "plain string payload"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "draft"}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "Kernel: linux 6.12"},
    ])

    def test_pairs_tool_use_with_results_and_takes_the_final_result(self) -> None:
        parsed = parse_claude_output(self.STREAM)
        self.assertEqual(parsed["message"], "Kernel: linux 6.12")
        self.assertEqual(
            [(c["server"], c["tool"], c["status"], c["result"]) for c in parsed["tool_calls"]],
            [("tiny", "host_info", "completed", "linux 6.12")],
        )
        self.assertEqual(parsed["builtin_calls"][0]["status"], "failed")
        self.assertEqual(parsed["errors"], [])

    def test_error_result_is_reported(self) -> None:
        stream = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True})
        self.assertEqual(parse_claude_output(stream)["errors"], ["error_max_turns"])


class CodexParserErrorsTest(unittest.TestCase):
    def test_turn_failed_is_surfaced(self) -> None:
        stream = json.dumps({"type": "turn.failed", "error": {"message": "model is not supported"}})
        self.assertEqual(parse_codex_output(stream)["errors"], ["model is not supported"])


ECHO_AGENT = """\
import sys
task = sys.stdin.read()
print("ANSWER: " + task.strip().splitlines()[-1])
"""


class ExecuteLocalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "agent.py").write_text(ECHO_AGENT, encoding="utf-8")
        (self.root / "work").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_agent(self, command: list[str], **extra) -> Path:
        path = self.root / "agent.json"
        path.write_text(json.dumps({
            "id": "echo", "runner": {"kind": "process", "command": command},
            "workspace": "work", "sandbox": {"backend": "local"}, **extra,
        }), encoding="utf-8")
        return path

    def test_runs_the_agent_and_keeps_answer_and_trace(self) -> None:
        agent = self.write_agent([sys.executable, str(self.root / "agent.py")])
        result = execute(ExecConfig(agent_path=agent, prompt="add two numbers", run_dir=self.root / "run"))
        self.assertTrue(result.ok, result.manifest)
        self.assertEqual(result.answer, "ANSWER: add two numbers")
        manifest = json.loads((self.root / "run" / "exec.json").read_text())
        self.assertEqual((manifest["backend"], manifest["sandbox"]), ("runner", "local"))
        self.assertEqual(manifest["workspace"], str((self.root / "work").resolve()))
        self.assertEqual((self.root / "run" / "answer.md").read_text(), "ANSWER: add two numbers\n")

    def test_failing_agent_is_an_agent_error(self) -> None:
        agent = self.write_agent([sys.executable, "-c", "import sys; sys.exit(3)"])
        result = execute(ExecConfig(agent_path=agent, prompt="x", run_dir=self.root / "run"))
        self.assertEqual(result.status, "agent_error")

    def test_missing_binary_is_not_a_crash(self) -> None:
        agent = self.write_agent(["/nonexistent/agent-cli"])
        result = execute(ExecConfig(agent_path=agent, prompt="x", run_dir=self.root / "run"))
        self.assertFalse(result.ok)
        self.assertIn("not found", result.manifest["error"])

    def test_empty_task_and_unknown_sandbox_are_rejected(self) -> None:
        agent = self.write_agent([sys.executable, str(self.root / "agent.py")])
        with self.assertRaisesRegex(ExecError, "task is empty"):
            execute(ExecConfig(agent_path=agent, prompt="  ", run_dir=self.root / "run"))
        with self.assertRaisesRegex(ExecError, "unsupported sandbox"):
            execute(ExecConfig(agent_path=agent, prompt="x", run_dir=self.root / "r", sandbox="vm"))

    def test_export_names_cannot_escape_the_run_dir(self) -> None:
        agent = self.write_agent([sys.executable, str(self.root / "agent.py")])
        for exports in ((("/sandbox/a", "../a"),), (("/sandbox/a", "/tmp/a"),), (("/etc/passwd", "p"),)):
            with self.assertRaises(ExecError):
                execute(ExecConfig(agent_path=agent, prompt="x", run_dir=self.root / "r",
                                   exports=exports))

    def test_cli_prints_only_the_answer_on_stdout(self) -> None:
        from rehearsal.cli import main

        agent = self.write_agent([sys.executable, str(self.root / "agent.py")])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["exec", "--agent", str(agent), "--run-dir", str(self.root / "run"), "say hi"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "ANSWER: say hi")
        self.assertIn("exec completed", err.getvalue())


class FakeSandbox:
    name = "ghostlab-agent-test"

    def __init__(self) -> None:
        self.uploaded: list[tuple[str, str]] = []
        self.downloaded: list[str] = []

    def upload_file(self, source, destination, mode=""):
        self.uploaded.append((Path(source).name, destination))

    def ssh_command(self, argv, *, env, workdir="", base_env=None):
        return ["ssh", f"cd {workdir}", *argv]

    def download(self, source, destination):
        self.downloaded.append(source)
        Path(destination).write_text("exported", encoding="utf-8")


class FakeHandle:
    def __init__(self) -> None:
        self.sandbox = FakeSandbox()
        self.closed = False

    def remote_path(self, value):
        return str(value)

    def close(self) -> None:
        self.closed = True


class ExecuteOpenShellTest(unittest.TestCase):
    def test_runs_inside_the_sandbox_and_tears_it_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "repo").mkdir()
            path = root / "agent.json"
            path.write_text(json.dumps({
                "id": "boxed", "workspace": "repo",
                "runtime": {"backend": "opencode", "model": "github-copilot/gpt-5-mini"},
                "inputs": {"mcps": [STDIO]},
            }), encoding="utf-8")
            handle = FakeHandle()
            seen: dict = {}
            stream = json.dumps({"type": "text", "part": {"type": "text", "text": "done"}})

            def fake_prepare(agent, sandbox, **kwargs):
                seen["sandbox"], seen["kwargs"] = sandbox, kwargs
                return handle

            def fake_run(argv, **kwargs):
                seen["argv"] = argv
                return RunnerResult(stream, 0)

            with mock.patch("rehearsal.agent_sandbox.prepare_agent_sandbox", fake_prepare), \
                    mock.patch.object(agent_exec, "_run", fake_run):
                result = execute(ExecConfig(
                    agent_path=path, prompt="go", run_dir=root / "run",
                    providers=("copilot",), exports=(("/sandbox/repo/out.md", "out.md"),),
                ))
            self.assertTrue(result.ok, result.manifest)
            self.assertEqual(result.answer, "done")
            self.assertTrue(handle.closed)
            self.assertEqual(seen["argv"][:2], ["ssh", "cd /sandbox/repo"])
            self.assertIn(("opencode.json", "/sandbox/repo/opencode.json"), handle.sandbox.uploaded)
            self.assertEqual(seen["sandbox"]["providers"], ["copilot"])
            self.assertEqual(
                [u["target"] for u in seen["sandbox"]["uploads"]], ["/sandbox"],
            )
            self.assertTrue(seen["kwargs"]["egress_policy"])
            self.assertEqual(result.manifest["sandbox_name"], "ghostlab-agent-test")
            self.assertEqual(result.manifest["exports"], [{"source": "/sandbox/repo/out.md", "path": "out.md"}])
            self.assertEqual((root / "run" / "out.md").read_text(), "exported")


class ClaudeMcpConfigTest(unittest.TestCase):
    def test_sse_and_env(self) -> None:
        config = claude_mcp_config([
            STDIO, {"id": "s", "transport": "sse", "connection": {"url": "https://x/sse"}},
        ])
        self.assertEqual(config["mcpServers"]["tiny"]["env"], {"LEVEL": "1"})
        self.assertEqual(config["mcpServers"]["s"], {"type": "sse", "url": "https://x/sse"})


if __name__ == "__main__":
    unittest.main()
