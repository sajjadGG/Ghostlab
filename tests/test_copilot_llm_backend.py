"""Tests for the GitHub Copilot LLM backend and its registration."""
from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from rehearsal.copilot_llm_backend import (
    CopilotLlmBackend,
    CopilotLlmError,
    CopilotUsage,
    collect_text,
    collect_usage,
    extract_json,
    resolve_copilot_llm_bin,
)
from rehearsal.llm_backend import (
    BACKENDS,
    BackendError,
    LlmBackendError,
    backend_error_types,
    backend_label,
    create_backend,
    resolve_backend_kind,
)


def _msg(content: str) -> str:
    return json.dumps({"type": "assistant.message", "data": {"content": content}})


def _completed(stdout: str = "", stderr: str = "", code: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


class CollectTextTest(unittest.TestCase):
    def test_returns_assistant_message_content(self) -> None:
        stream = "\n".join([
            "GitHub Copilot CLI 1.0.0",  # non-JSON banner
            json.dumps({"type": "session.tools_updated", "data": {}}),
            _msg("42"),
            json.dumps({"type": "assistant.idle", "data": {}}),
        ])
        self.assertEqual(collect_text(stream), "42")

    def test_last_message_wins_across_turns(self) -> None:
        stream = "\n".join([_msg("thinking out loud"), _msg("final answer")])
        self.assertEqual(collect_text(stream), "final answer")

    def test_ignores_malformed_lines(self) -> None:
        self.assertEqual(collect_text("{not json\n" + _msg("ok")), "ok")

    def test_empty_stream_yields_empty_string(self) -> None:
        self.assertEqual(collect_text(""), "")


class ExtractJsonTest(unittest.TestCase):
    def test_parses_bare_json(self) -> None:
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_parses_fenced_json(self) -> None:
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_parses_prose_wrapped_json(self) -> None:
        self.assertEqual(extract_json('Here you go:\n{"a": 1}\nHope that helps.'), {"a": 1})

    def test_parses_array(self) -> None:
        self.assertEqual(extract_json("Result: [1, 2]"), [1, 2])

    def test_rejects_empty(self) -> None:
        with self.assertRaises(CopilotLlmError):
            extract_json("   ")

    def test_rejects_unparseable(self) -> None:
        with self.assertRaises(CopilotLlmError):
            extract_json("no json here at all")


class BuildCommandTest(unittest.TestCase):
    def test_seals_tools_and_ends_with_prompt_flag(self) -> None:
        command = CopilotLlmBackend(bin_path="/bin/copilot", model="gpt-5.4-mini").build_command()
        self.assertEqual(command[0], "/bin/copilot")
        self.assertEqual(command[-1], "--prompt")
        for flag in ("--no-ask-user", "--no-custom-instructions", "--disable-builtin-mcps"):
            self.assertIn(flag, command)
        self.assertTrue(any(a.startswith("--excluded-tools=") for a in command))
        self.assertIn("--model", command)
        self.assertIn("gpt-5.4-mini", command)

    def test_omits_model_flag_when_unset(self) -> None:
        self.assertNotIn("--model", CopilotLlmBackend(bin_path="/bin/copilot").build_command())

    def test_disables_named_mcp_servers(self) -> None:
        command = CopilotLlmBackend(
            bin_path="/bin/copilot", disabled_mcp_servers=("paperclip",)
        ).build_command()
        self.assertIn("--disable-mcp-server", command)
        self.assertIn("paperclip", command)


class GenerateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = CopilotLlmBackend(bin_path="/bin/copilot", model="gpt-5.4-mini")

    def test_generate_text_returns_reply(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout=_msg("hello"))):
            self.assertEqual(self.backend.generate_text("hi"), "hello")

    def test_generate_text_passes_prompt_as_final_arg(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout=_msg("ok"))) as run:
            self.backend.generate_text("what is 2+2?")
        self.assertEqual(run.call_args.args[0][-1], "what is 2+2?")
        self.assertEqual(run.call_args.args[0][-2], "--prompt")

    def test_generate_json_embeds_schema_and_parses(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout=_msg('{"a": 1}'))) as run:
            self.assertEqual(self.backend.generate_json("go", {"type": "object"}), {"a": 1})
        prompt = run.call_args.args[0][-1]
        self.assertIn("JSON Schema", prompt)
        self.assertIn("go", prompt)

    def test_unavailable_model_is_reported_clearly(self) -> None:
        stderr = 'Model "gpt-9" from --model flag is not available.'
        with patch("subprocess.run", return_value=_completed(stderr=stderr, code=1)):
            with self.assertRaises(CopilotLlmError) as ctx:
                self.backend.generate_text("hi")
        self.assertIn("not available on this account", str(ctx.exception))

    def test_unavailable_model_detected_on_stdout_too(self) -> None:
        stdout = 'Model "gpt-9" from --model flag is not available.'
        with patch("subprocess.run", return_value=_completed(stdout=stdout)):
            with self.assertRaises(CopilotLlmError):
                self.backend.generate_text("hi")

    def test_phrase_inside_model_output_is_not_a_model_error(self) -> None:
        # Copilot echoes its system prompt back in snapshot events, so a naive
        # substring check on "is not available" produces false positives.
        stream = json.dumps({
            "type": "model.messages_snapshot",
            "data": {"messages": [{"role": "system", "content": "the tool is not available"}]},
        }) + "\n" + _msg("fine")
        with patch("subprocess.run", return_value=_completed(stdout=stream)):
            self.assertEqual(self.backend.generate_text("hi"), "fine")

    def test_nonzero_exit_raises(self) -> None:
        with patch("subprocess.run", return_value=_completed(stderr="boom", code=2)):
            with self.assertRaises(CopilotLlmError):
                self.backend.generate_text("hi")

    def test_no_assistant_message_raises(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout="{}")):
            with self.assertRaises(CopilotLlmError):
                self.backend.generate_text("hi")

    def test_timeout_raises(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("copilot", 1)):
            with self.assertRaises(CopilotLlmError):
                self.backend.generate_text("hi")

    def test_runs_in_a_scratch_directory(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout=_msg("ok"))) as run:
            self.backend.generate_text("hi")
        self.assertIsNotNone(run.call_args.kwargs.get("cwd"))


class CollectUsageTest(unittest.TestCase):
    def test_reads_the_usage_checkpoint(self) -> None:
        stream = json.dumps({
            "type": "session.usage_checkpoint",
            "data": {"totalNanoAiu": 9845375000, "totalPremiumRequests": 15},
        })
        usage = collect_usage(stream)
        self.assertTrue(usage.reported)
        self.assertEqual(usage.nano_aiu, 9845375000)
        self.assertEqual(usage.premium_requests, 15.0)
        self.assertAlmostEqual(usage.aiu, 9.845375)

    def test_captures_the_answering_model(self) -> None:
        stream = json.dumps({
            "type": "assistant.message",
            "data": {"content": "hi", "model": "claude-opus-5"},
        })
        self.assertEqual(collect_usage(stream).model, "claude-opus-5")

    def test_absent_checkpoint_is_reported_as_unreported(self) -> None:
        usage = collect_usage(_msg("hi"))
        self.assertFalse(usage.reported)
        self.assertEqual(usage.nano_aiu, 0)
        self.assertEqual(usage.aiu, 0.0)

    def test_last_checkpoint_wins(self) -> None:
        stream = "\n".join([
            json.dumps({"type": "session.usage_checkpoint",
                        "data": {"totalNanoAiu": 1, "totalPremiumRequests": 0}}),
            json.dumps({"type": "session.usage_checkpoint",
                        "data": {"totalNanoAiu": 500, "totalPremiumRequests": 2}}),
        ])
        self.assertEqual(collect_usage(stream).nano_aiu, 500)

    def test_ignores_noise(self) -> None:
        self.assertFalse(collect_usage("banner\n{bad json\n").reported)


class GenerateWithUsageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = CopilotLlmBackend(bin_path="/bin/copilot", model="gpt-5.4-mini")

    def test_returns_reply_and_usage(self) -> None:
        stream = "\n".join([
            _msg("391"),
            json.dumps({"type": "session.usage_checkpoint",
                        "data": {"totalNanoAiu": 387000000, "totalPremiumRequests": 0.33}}),
        ])
        with patch("subprocess.run", return_value=_completed(stdout=stream)):
            reply, usage = self.backend.generate_text_with_usage("hi")
        self.assertEqual(reply, "391")
        self.assertAlmostEqual(usage.premium_requests, 0.33)
        self.assertTrue(usage.reported)

    def test_generate_text_still_returns_a_plain_string(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout=_msg("391"))):
            self.assertEqual(self.backend.generate_text("hi"), "391")

    def test_usage_is_optional(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout=_msg("391"))):
            _, usage = self.backend.generate_text_with_usage("hi")
        self.assertEqual(usage, CopilotUsage())


class ResolveBinTest(unittest.TestCase):
    def test_env_override_is_used(self) -> None:
        with patch("shutil.which", return_value="/from/path/copilot"):
            with patch.dict("os.environ", {"GHOSTLAB_COPILOT_BIN": "copilot"}):
                self.assertEqual(resolve_copilot_llm_bin(), "/from/path/copilot")

    def test_missing_binary_raises(self) -> None:
        with patch("shutil.which", return_value=None):
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(CopilotLlmError):
                    resolve_copilot_llm_bin()


class RegistrationTest(unittest.TestCase):
    def test_copilot_is_a_known_backend(self) -> None:
        self.assertIn("copilot", BACKENDS)
        self.assertEqual(resolve_backend_kind("copilot"), "copilot")

    def test_env_var_selects_copilot(self) -> None:
        with patch.dict("os.environ", {"GHOSTLAB_LLM_BACKEND": "copilot"}):
            self.assertEqual(resolve_backend_kind(), "copilot")

    def test_unknown_backend_still_rejected(self) -> None:
        with self.assertRaises(BackendError):
            resolve_backend_kind("nope")

    def test_create_backend_builds_copilot(self) -> None:
        backend = create_backend("copilot", bin_path="/bin/copilot", model="claude-sonnet-5")
        self.assertIsInstance(backend, CopilotLlmBackend)
        self.assertEqual(backend.model, "claude-sonnet-5")

    def test_error_type_is_backend_agnostic(self) -> None:
        self.assertIn(CopilotLlmError, backend_error_types())
        self.assertTrue(issubclass(CopilotLlmError, LlmBackendError))

    def test_label_names_the_backend_and_model(self) -> None:
        backend = create_backend("copilot", bin_path="/bin/copilot", model="gpt-5.4-mini")
        label = backend_label(backend)
        self.assertIn("copilot", label)
        self.assertIn("gpt-5.4-mini", label)

    def test_label_survives_a_missing_binary(self) -> None:
        with patch("shutil.which", return_value=None):
            with patch.dict("os.environ", {}, clear=True):
                self.assertIn("copilot", backend_label(create_backend("copilot")))


if __name__ == "__main__":
    unittest.main()


class SessionTest(unittest.TestCase):
    """Multi-turn conversations via Copilot's --session-id."""

    SID = "0f8fad5b-d9cb-469f-a165-70867728950e"

    def setUp(self) -> None:
        self.backend = CopilotLlmBackend(bin_path="/bin/copilot", model="gpt-5.4-mini")

    def test_no_session_flag_by_default(self) -> None:
        self.assertNotIn("--session-id", self.backend.build_command())

    def test_session_flag_precedes_prompt(self) -> None:
        command = self.backend.build_command(self.SID)
        at = command.index("--session-id")
        self.assertEqual(command[at + 1], self.SID)
        self.assertEqual(command[-1], "--prompt")

    def test_generate_passes_session_and_keeps_prompt_last(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout=_msg("ok"))) as run:
            self.backend.generate_text_with_usage("turn two", session_id=self.SID)
        args = run.call_args.args[0]
        self.assertIn(self.SID, args)
        self.assertEqual(args[-2:], ["--prompt", "turn two"])

    def test_generate_text_forwards_session(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout=_msg("ok"))) as run:
            self.backend.generate_text("hi", session_id=self.SID)
        self.assertIn("--session-id", run.call_args.args[0])

    def test_rejects_non_uuid_session(self) -> None:
        with patch("subprocess.run") as run:
            with self.assertRaises(CopilotLlmError):
                self.backend.generate_text("hi", session_id="not-a-uuid")
        run.assert_not_called()
