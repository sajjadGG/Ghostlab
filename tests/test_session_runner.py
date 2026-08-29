"""Tests for the codex session runner command building (no codex needed)."""
from __future__ import annotations

import unittest

from rehearsal.config import RunnerConfig
from rehearsal.runners import CodexSessionRunner, create_runner
from rehearsal.session_provenance import (
    CODEX_ORIGINATOR_ENV,
    GHOSTLAB_ORIGINATOR,
)

BASE_COMMAND = ["codex", "-c", "x=1", "exec", "--json", "--skip-git-repo-check", "-"]


def _runner() -> CodexSessionRunner:
    return CodexSessionRunner(RunnerConfig(kind="codex-session", command=list(BASE_COMMAND)))


class SessionCommandTest(unittest.TestCase):
    def test_first_turn_uses_base_command(self) -> None:
        runner = _runner()
        self.assertEqual(runner._command_for_turn(), BASE_COMMAND)

    def test_resume_inserted_after_exec(self) -> None:
        runner = _runner()
        runner.thread_id = "abc-123"
        command = runner._command_for_turn()
        self.assertEqual(
            command,
            ["codex", "-c", "x=1", "exec", "resume", "abc-123", "--json", "--skip-git-repo-check", "-"],
        )

    def test_extract_thread_id(self) -> None:
        jsonl = '{"type":"thread.started","thread_id":"t-9"}\n{"type":"turn.started"}'
        self.assertEqual(CodexSessionRunner._extract_thread_id(jsonl), "t-9")

    def test_extract_thread_id_missing(self) -> None:
        self.assertIsNone(CodexSessionRunner._extract_thread_id('{"type":"turn.started"}'))

    def test_is_stateful(self) -> None:
        self.assertTrue(create_runner(RunnerConfig(kind="codex-session", command=BASE_COMMAND), "aut").stateful)

    def test_tags_codex_sessions_with_ghostlab_originator(self) -> None:
        runner = CodexSessionRunner(
            RunnerConfig(
                kind="codex-session",
                command=list(BASE_COMMAND),
                env={"EXISTING": "value", CODEX_ORIGINATOR_ENV: "spoofed"},
            )
        )
        self.assertEqual(runner.config.env["EXISTING"], "value")
        self.assertEqual(
            runner.config.env[CODEX_ORIGINATOR_ENV],
            GHOSTLAB_ORIGINATOR,
        )

    def test_tags_fresh_codex_processes_with_ghostlab_originator(self) -> None:
        runner = create_runner(
            RunnerConfig(kind="process", command=["codex", "exec", "-"]),
            "user",
        )
        self.assertEqual(
            runner.config.env[CODEX_ORIGINATOR_ENV],
            GHOSTLAB_ORIGINATOR,
        )

    def test_requires_exec_in_command(self) -> None:
        with self.assertRaises(ValueError):
            CodexSessionRunner(RunnerConfig(kind="codex-session", command=["codex", "-"]))


if __name__ == "__main__":
    unittest.main()
