"""Tests for the opencode LLM backend, its capture parser, and runner presets."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rehearsal.config import RunnerConfig, TargetConfig
from rehearsal.llm_backend import (
    BackendError,
    LlmBackendError,
    create_backend,
    resolve_backend_kind,
)
from rehearsal.opencode_backend import (
    OpencodeBackend,
    OpencodeError,
    collect_text,
    extract_json,
    first_stream_error,
)
from rehearsal.runner_presets import (
    opencode_aut_runner,
    opencode_project_config,
    opencode_user_runner,
)
from rehearsal.runners import create_runner
from rehearsal.tool_capture import parse_opencode_output


def _event(kind: str, **part: object) -> str:
    return json.dumps({"type": kind, "part": part})


class CollectTextTest(unittest.TestCase):
    def test_joins_text_parts_and_ignores_noise(self) -> None:
        stream = "\n".join([
            "opencode starting up",  # non-JSON banner
            _event("step_start"),
            _event("text", text="Hello"),
            _event("tool_use", tool="x", state={}),
            _event("text", text="world"),
            "{not json",
        ])
        self.assertEqual(collect_text(stream), "Hello\nworld")

    def test_empty_stream_yields_empty_string(self) -> None:
        self.assertEqual(collect_text(""), "")

    def test_stream_error_is_extracted(self) -> None:
        stream = json.dumps({
            "type": "error",
            "error": {"data": {"message": "model \"mai-code-1.1-flash\" is not accessible"}},
        })
        self.assertIn("mai-code-1.1-flash", first_stream_error(stream))


class ExtractJsonTest(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self) -> None:
        self.assertEqual(extract_json('```json\n{"a": 2}\n```'), {"a": 2})

    def test_prose_wrapped_json(self) -> None:
        text = 'Sure! Here you go:\n{"a": 3}\nHope that helps.'
        self.assertEqual(extract_json(text), {"a": 3})

    def test_array_payload(self) -> None:
        self.assertEqual(extract_json("prefix [1, 2] suffix"), [1, 2])

    def test_unparseable_raises(self) -> None:
        with self.assertRaises(OpencodeError):
            extract_json("no json at all")

    def test_empty_raises(self) -> None:
        with self.assertRaises(OpencodeError):
            extract_json("   ")


class OpencodeBackendTest(unittest.TestCase):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}

    def test_generate_json_parses_stream(self) -> None:
        completed = unittest.mock.Mock(
            returncode=0, stdout=_event("text", text='{"ok": true}'), stderr=""
        )
        with patch("subprocess.run", return_value=completed) as run:
            backend = OpencodeBackend(bin_path="/bin/opencode", model="prov/model")
            self.assertEqual(backend.generate_json("prompt", self.schema), {"ok": True})
        command = run.call_args[0][0]
        self.assertIn("--model", command)
        self.assertIn("prov/model", command)
        self.assertIn("--format", command)
        # The schema has to travel in the prompt: opencode has no schema flag.
        self.assertIn("JSON Schema", run.call_args[1]["input"])

    def test_nonzero_exit_raises(self) -> None:
        completed = unittest.mock.Mock(returncode=1, stdout="", stderr="boom")
        with patch("subprocess.run", return_value=completed):
            backend = OpencodeBackend(bin_path="/bin/opencode")
            with self.assertRaises(OpencodeError) as ctx:
                backend.generate_json("prompt", self.schema)
        self.assertIn("boom", str(ctx.exception))

    def test_empty_reply_raises(self) -> None:
        completed = unittest.mock.Mock(returncode=0, stdout="", stderr="")
        with patch("subprocess.run", return_value=completed):
            backend = OpencodeBackend(bin_path="/bin/opencode")
            with self.assertRaises(OpencodeError):
                backend.generate_json("prompt", self.schema)

    def test_stream_error_event_is_a_model_error(self) -> None:
        completed = unittest.mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "type": "error",
                "error": {"data": {"message": "model is not accessible via /chat/completions"}},
            }),
            stderr="",
        )
        with patch("subprocess.run", return_value=completed):
            backend = OpencodeBackend(bin_path="/bin/opencode")
            with self.assertRaises(OpencodeError) as ctx:
                backend.generate_json("prompt", self.schema)
        self.assertIn("not accessible", str(ctx.exception))

    def test_defaults_to_a_pinned_model(self) -> None:
        completed = unittest.mock.Mock(
            returncode=0, stdout=_event("text", text="{}"), stderr=""
        )
        with patch("subprocess.run", return_value=completed) as run:
            OpencodeBackend(bin_path="/bin/opencode").generate_json("p", self.schema)
        command = run.call_args[0][0]
        self.assertIn("github-copilot/claude-sonnet-4.5", command)


class BackendFactoryTest(unittest.TestCase):
    def test_explicit_beats_spec_and_default(self) -> None:
        self.assertEqual(resolve_backend_kind("opencode", "codex"), "opencode")
        self.assertEqual(resolve_backend_kind("", "opencode"), "opencode")
        self.assertEqual(resolve_backend_kind("", ""), "codex")

    def test_unknown_backend_rejected(self) -> None:
        with self.assertRaises(BackendError):
            resolve_backend_kind("gemini")

    def test_create_backend_returns_requested_type(self) -> None:
        self.assertEqual(type(create_backend("opencode")).__name__, "OpencodeBackend")
        self.assertEqual(type(create_backend("codex")).__name__, "CodexBackend")

    def test_both_errors_share_a_base(self) -> None:
        from rehearsal.codex_backend import CodexError

        self.assertTrue(issubclass(CodexError, LlmBackendError))
        self.assertTrue(issubclass(OpencodeError, LlmBackendError))


class ParseOpencodeOutputTest(unittest.TestCase):
    def test_splits_mcp_tools_from_builtins(self) -> None:
        stream = "\n".join([
            _event("text", text="working on it"),
            _event(
                "tool_use", tool="safari_safari_list_tabs", callID="c1",
                state={
                    "status": "completed", "input": {"a": 1}, "output": "[]",
                    "time": {"start": 1000, "end": 1150},
                },
            ),
            _event(
                "tool_use", tool="read", callID="c2",
                state={"status": "completed", "input": {}, "output": "x"},
            ),
        ])
        parsed = parse_opencode_output(stream, servers=["safari"])
        self.assertEqual(parsed["message"], "working on it")
        self.assertEqual(len(parsed["tool_calls"]), 1)
        call = parsed["tool_calls"][0]
        self.assertEqual(call["server"], "safari")
        self.assertEqual(call["tool"], "safari_list_tabs")
        self.assertEqual(call["status"], "completed")
        self.assertEqual(call["duration_ms"], 150.0)
        # A host built-in must never be reported as an MCP call, or the judge's
        # hallucination check would flag it as an invented tool.
        self.assertEqual(len(parsed["builtin_calls"]), 1)
        self.assertEqual(parsed["builtin_calls"][0]["tool"], "read")

    def test_without_server_hint_nothing_is_claimed_as_mcp(self) -> None:
        stream = _event(
            "tool_use", tool="safari_safari_list_tabs", state={"status": "completed"}
        )
        parsed = parse_opencode_output(stream)
        self.assertEqual(parsed["tool_calls"], [])
        self.assertEqual(len(parsed["builtin_calls"]), 1)

    def test_failed_call_is_classified(self) -> None:
        stream = _event(
            "tool_use", tool="safari_safari_click",
            state={"status": "error", "error": "permission denied", "input": {}},
        )
        call = parse_opencode_output(stream, servers=["safari"])["tool_calls"][0]
        self.assertEqual(call["status"], "failed")
        self.assertEqual(call["failure_cause"], "permission_denied")

    def test_error_events_are_collected(self) -> None:
        stream = "\n".join([
            json.dumps({
                "type": "error",
                "error": {
                    "name": "APIError",
                    "data": {"message": "The requested model is not supported."},
                },
            }),
            _event("text", text="ignored"),
        ])
        parsed = parse_opencode_output(stream)
        self.assertEqual(parsed["errors"], ["The requested model is not supported."])


class OpencodeRunnerTest(unittest.TestCase):
    def test_error_event_becomes_a_failed_turn(self) -> None:
        """opencode exits 0 on provider errors; the runner must not accept that."""
        stream = json.dumps({
            "type": "error",
            "error": {"name": "APIError", "data": {"message": "model not supported"}},
        })
        config = RunnerConfig(
            kind="process", command=["opencode"], parser="opencode-json"
        )
        runner = create_runner(config, "aut")
        with patch(
            "rehearsal.runners._exec",
            return_value=unittest.mock.Mock(
                output=stream, exit_code=0, timed_out=False, stderr=""
            ),
        ):
            result = runner.run_turn("hi")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("model not supported", result.stderr)

    def test_clean_turn_passes_through(self) -> None:
        config = RunnerConfig(
            kind="process", command=["opencode"], parser="opencode-json"
        )
        runner = create_runner(config, "aut")
        with patch(
            "rehearsal.runners._exec",
            return_value=unittest.mock.Mock(
                output=_event("text", text="hello"), exit_code=0,
                timed_out=False, stderr="",
            ),
        ):
            result = runner.run_turn("hi")
        self.assertEqual(result.exit_code, 0)


class OpencodePresetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stdio_target_becomes_a_local_mcp_entry(self) -> None:
        target = TargetConfig(
            id="safari", transport="stdio",
            connection={"command": "node", "args": ["/srv/index.js"], "env": {"A": "1"}},
        )
        config = opencode_project_config(target)
        entry = config["mcp"]["safari"]
        self.assertEqual(entry["type"], "local")
        self.assertEqual(entry["command"], ["node", "/srv/index.js"])
        self.assertEqual(entry["environment"], {"A": "1"})

    def test_http_target_becomes_a_remote_mcp_entry(self) -> None:
        target = TargetConfig(
            id="cortex", transport="streamable-http",
            connection={"url": "http://localhost:8000/mcp", "headers": {"H": "v"}},
        )
        entry = opencode_project_config(target)["mcp"]["cortex"]
        self.assertEqual(entry["type"], "remote")
        self.assertEqual(entry["url"], "http://localhost:8000/mcp")
        self.assertEqual(entry["headers"], {"H": "v"})

    def test_user_emulator_gets_no_mcp_at_all(self) -> None:
        """The emulated human must never hold the agent-under-test's tools."""
        runner = opencode_user_runner(self.tmp / "user", model="prov/m")
        config = json.loads((self.tmp / "user" / "opencode.json").read_text())
        self.assertNotIn("mcp", config)
        self.assertEqual(runner.parser, "opencode-text")

    def test_aut_runner_writes_project_and_uses_json_parser(self) -> None:
        target = TargetConfig(
            id="safari", transport="stdio", connection={"command": ["node", "s.js"]},
        )
        runner = opencode_aut_runner(target, self.tmp / "aut", model="prov/m")
        self.assertEqual(runner.parser, "opencode-json")
        self.assertIn("--dir", runner.command)
        config = json.loads((self.tmp / "aut" / "opencode.json").read_text())
        self.assertIn("safari", config["mcp"])
        # Side-effecting host tools stay off so the agent cannot bypass the MCP.
        self.assertEqual(config["permission"]["bash"], "deny")


if __name__ == "__main__":
    unittest.main()
