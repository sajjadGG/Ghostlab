"""Unit tests for RunnerHost: scenario execution, progress, judge/critique wiring."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rehearsal.config import RunnerConfig
from rehearsal.hosts.runner import RunnerHost

FAKE_USER_RUNNER = RunnerConfig(kind="mock")


def _write_spec(tmp: Path) -> Path:
    spec_path = tmp / "ghostlab.yaml"
    spec_path.write_text(
        "schema_version: 1\n"
        "id: fake-notes\n"
        "target:\n"
        "  transport: stdio\n"
        "  connection:\n"
        "    command:\n"
        "      - echo\n",
        encoding="utf-8",
    )
    return spec_path


def _write_scenario(tmp: Path) -> Path:
    path = tmp / "scenario.json"
    path.write_text(json.dumps({
        "id": "s1", "title": "t", "persona": "alice", "goal": "learn something",
        "max_turns": 2, "success_criteria": [], "failure_signals": [],
        "opening_message": "hi",
    }), encoding="utf-8")
    return path


def _write_persona(tmp: Path) -> Path:
    path = tmp / "persona.json"
    path.write_text(json.dumps({"id": "alice", "name": "Alice", "summary": "s"}), encoding="utf-8")
    return path


def _write_spec_with_capabilities(tmp: Path, tool_names: list[str]) -> Path:
    """A spec with discovered capabilities + a sibling inspect.json (post-discover shape)."""
    discover_dir = tmp / ".ghostlab" / "discover" / "20260101-x"
    discover_dir.mkdir(parents=True)
    (discover_dir / "contract.json").write_text("{}", encoding="utf-8")
    (discover_dir / "inspect.json").write_text(
        json.dumps({"tools": [{"name": name, "description": "d"} for name in tool_names]}),
        encoding="utf-8",
    )
    spec_path = tmp / "ghostlab.yaml"
    spec_path.write_text(
        "schema_version: 1\n"
        "id: fake-notes\n"
        "target:\n"
        "  transport: stdio\n"
        "  connection:\n"
        "    command:\n"
        "      - echo\n"
        "capabilities:\n"
        "  generated_from: .ghostlab/discover/20260101-x/contract.json\n"
        "  tools:\n"
        + "".join(f"    - name: {name}\n" for name in tool_names),
        encoding="utf-8",
    )
    return spec_path


class RunnerHostExecuteTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.spec_path = _write_spec(self.tmp)
        self.scenario_path = _write_scenario(self.tmp)
        self.persona_path = _write_persona(self.tmp)
        self.out_dir = self.tmp / "out"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _case(self, execution: dict) -> dict:
        return {"id": "semantic-gen-x", "suite": "semantic", "kind": "conversational",
                "title": "t", "reason": "r", "tools": [], "status": "proposed",
                "execution": execution}

    def test_seed_case_skips_with_instructions(self) -> None:
        host = RunnerHost("h", "codex-session", {}, self.spec_path)
        result = host.execute(self._case({"needs_generation": True}), self.out_dir)
        self.assertEqual(result.status, "skip")
        self.assertIn("ghostlab plan --generate", result.detail)

    def test_missing_scenario_ref_skips(self) -> None:
        host = RunnerHost("h", "codex-session", {}, self.spec_path)
        result = host.execute(self._case({}), self.out_dir)
        self.assertEqual(result.status, "skip")

    @patch("rehearsal.orchestrator.run_scenario")
    def test_missing_user_runner_config_errors_instead_of_reusing_aut_runner(
        self, run_scenario_mock: MagicMock
    ) -> None:
        host = RunnerHost("h", "codex-session", {}, self.spec_path, show_progress=False)
        result = host.execute(self._case({"scenario": str(self.scenario_path)}), self.out_dir)
        self.assertEqual(result.status, "error")
        self.assertIn("user-emulator runner", result.detail)
        run_scenario_mock.assert_not_called()

    @patch("rehearsal.orchestrator.run_scenario")
    def test_aut_and_user_runner_configs_are_distinct(self, run_scenario_mock: MagicMock) -> None:
        run_scenario_mock.return_value = MagicMock(
            status="completed", turns=1, run_dir=self.out_dir / "run1",
            report_path=self.out_dir / "run1" / "report.md",
        )
        aut_config_path = self.tmp / "aut-runner.json"
        aut_config_path.write_text(json.dumps({
            "kind": "process", "command": ["codex", "-c", "mcp_servers.x.url=1", "exec", "-"],
        }), encoding="utf-8")
        host = RunnerHost(
            "h", "process", {"config_ref": str(aut_config_path)}, self.spec_path,
            show_progress=False, user_runner_config=FAKE_USER_RUNNER,
        )
        host.execute(self._case({"scenario": str(self.scenario_path)}), self.out_dir)
        kwargs = run_scenario_mock.call_args.kwargs
        self.assertIsNot(kwargs["aut_runner_config"], kwargs["user_runner_config"])
        self.assertEqual(kwargs["user_runner_config"], FAKE_USER_RUNNER)
        self.assertIn("mcp_servers", " ".join(kwargs["aut_runner_config"].command))
        self.assertNotIn("mcp_servers", " ".join(kwargs["user_runner_config"].command))

    @patch("rehearsal.orchestrator.run_scenario")
    def test_no_backend_uses_run_status(self, run_scenario_mock: MagicMock) -> None:
        run_scenario_mock.return_value = MagicMock(
            status="completed", turns=3, run_dir=self.out_dir / "run1",
            report_path=self.out_dir / "run1" / "report.md",
        )
        host = RunnerHost("h", "codex-session", {}, self.spec_path, backend=None,
                          show_progress=False, user_runner_config=FAKE_USER_RUNNER)
        result = host.execute(
            self._case({"scenario": str(self.scenario_path), "persona": str(self.persona_path)}),
            self.out_dir,
        )
        self.assertEqual(result.status, "pass")
        self.assertIn("no judge configured", result.detail)
        run_scenario_mock.assert_called_once()
        self.assertEqual(run_scenario_mock.call_args.kwargs["persona"].id, "alice")

    @patch("rehearsal.orchestrator.run_scenario")
    def test_no_backend_failed_run_is_fail(self, run_scenario_mock: MagicMock) -> None:
        run_scenario_mock.return_value = MagicMock(
            status="max_turns_reached", turns=2, run_dir=self.out_dir / "run1",
            report_path=self.out_dir / "run1" / "report.md",
        )
        host = RunnerHost("h", "codex-session", {}, self.spec_path, show_progress=False,
                          user_runner_config=FAKE_USER_RUNNER)
        result = host.execute(
            self._case({"scenario": str(self.scenario_path)}), self.out_dir
        )
        self.assertEqual(result.status, "fail")

    @patch("rehearsal.critique.write_critique_artifacts")
    @patch("rehearsal.critique.critique_run")
    @patch("rehearsal.evaluate.write_verdict_artifacts")
    @patch("rehearsal.evaluate.evaluate_run")
    @patch("rehearsal.orchestrator.run_scenario")
    def test_judge_and_critique_receive_discovered_tools_as_ground_truth(
        self, run_scenario_mock, evaluate_mock, write_verdict_mock, critique_mock, write_critique_mock
    ) -> None:
        # Regression test: without the discovered tool list, the judge has no
        # ground truth and can (and did, live against Cortex) flag a real,
        # successfully-called tool as "hallucinated". evaluate_run/critique_run
        # must receive the spec's discovered tools, not None.
        spec_path = _write_spec_with_capabilities(self.tmp, ["session_get_plan", "lesson_start"])
        run_dir = self.out_dir / "run1"
        run_scenario_mock.return_value = MagicMock(
            status="completed", turns=2, run_dir=run_dir, report_path=run_dir / "report.md",
        )
        evaluate_mock.return_value = {"verdict": "pass", "gates": [], "judge": {"summary": "ok"}}
        critique_mock.return_value = {"critique": {"overall_score": 4}}

        host = RunnerHost("h", "codex-session", {}, spec_path, backend=MagicMock(),
                          show_progress=False, user_runner_config=FAKE_USER_RUNNER)
        host.execute(self._case({"scenario": str(self.scenario_path)}), self.out_dir)

        eval_kwargs = evaluate_mock.call_args.kwargs
        self.assertIsNotNone(eval_kwargs["capabilities"])
        self.assertIn(
            "session_get_plan", eval_kwargs["capabilities"]["taxonomy"]["discovered"],
        )
        critique_kwargs = critique_mock.call_args.kwargs
        self.assertIsNotNone(critique_kwargs["inspect"])
        self.assertIn("session_get_plan", {t["name"] for t in critique_kwargs["inspect"]["tools"]})

    @patch("rehearsal.evaluate.evaluate_run")
    @patch("rehearsal.orchestrator.run_scenario")
    def test_gates_are_surfaced_alongside_a_generous_judge_summary(
        self, run_scenario_mock, evaluate_mock
    ) -> None:
        # Reproduces the live bug: judge.verdict="pass" with an all-clear
        # summary, but combine_verdict's gates (e.g. a hallucinated-tools hit)
        # downgraded the overall verdict to "fail". The detail must show the
        # gate, not just echo the now-contradictory summary text.
        run_dir = self.out_dir / "run1"
        run_scenario_mock.return_value = MagicMock(
            status="completed", turns=2, run_dir=run_dir, report_path=run_dir / "report.md",
        )
        evaluate_mock.return_value = {
            "verdict": "fail",
            "gates": ["hallucinated_tools:cortex/session_get_plan"],
            "judge": {"summary": "All success criteria were met and no failure signals were triggered."},
        }
        with patch("rehearsal.evaluate.write_verdict_artifacts"), \
             patch("rehearsal.critique.critique_run", side_effect=Exception("skip")):
            host = RunnerHost("h", "codex-session", {}, self.spec_path, backend=MagicMock(),
                              show_progress=False, user_runner_config=FAKE_USER_RUNNER)
            result = host.execute(self._case({"scenario": str(self.scenario_path)}), self.out_dir)
        self.assertEqual(result.status, "fail")
        self.assertIn("gates:", result.detail)
        self.assertIn("hallucinated_tools", result.detail)

    @patch("rehearsal.critique.write_critique_artifacts")
    @patch("rehearsal.critique.critique_run")
    @patch("rehearsal.evaluate.write_verdict_artifacts")
    @patch("rehearsal.evaluate.evaluate_run")
    @patch("rehearsal.orchestrator.run_scenario")
    def test_judge_verdict_decides_pass_fail_over_run_status(
        self, run_scenario_mock, evaluate_mock, write_verdict_mock, critique_mock, write_critique_mock
    ) -> None:
        run_dir = self.out_dir / "run1"
        run_scenario_mock.return_value = MagicMock(
            status="completed", turns=3, run_dir=run_dir, report_path=run_dir / "report.md",
        )
        # The conversation "completed" but the judge says the goal wasn't met.
        evaluate_mock.return_value = {"verdict": "fail", "judge": {"summary": "goal not met"}}
        critique_mock.return_value = {"critique": {"overall_score": 3}}

        backend = MagicMock()
        host = RunnerHost("h", "codex-session", {}, self.spec_path, backend=backend,
                          show_progress=False, user_runner_config=FAKE_USER_RUNNER)
        result = host.execute(
            self._case({"scenario": str(self.scenario_path)}), self.out_dir
        )
        self.assertEqual(result.status, "fail")
        self.assertIn("goal not met", result.detail)
        self.assertIn("verdict", result.artifacts)
        self.assertIn("critique", result.artifacts)
        write_verdict_mock.assert_called_once()
        write_critique_mock.assert_called_once()

    @patch("rehearsal.evaluate.evaluate_run")
    @patch("rehearsal.orchestrator.run_scenario")
    def test_partial_verdict_counts_as_pass(self, run_scenario_mock, evaluate_mock) -> None:
        run_dir = self.out_dir / "run1"
        run_scenario_mock.return_value = MagicMock(
            status="completed", turns=3, run_dir=run_dir, report_path=run_dir / "report.md",
        )
        evaluate_mock.return_value = {"verdict": "partial", "judge": {"summary": "mostly there"}}
        with patch("rehearsal.evaluate.write_verdict_artifacts"), \
             patch("rehearsal.critique.critique_run", side_effect=Exception("boom")):
            host = RunnerHost("h", "codex-session", {}, self.spec_path, backend=MagicMock(),
                              show_progress=False, user_runner_config=FAKE_USER_RUNNER)
            result = host.execute(self._case({"scenario": str(self.scenario_path)}), self.out_dir)
        self.assertEqual(result.status, "pass")

    @patch("rehearsal.evaluate.evaluate_run")
    @patch("rehearsal.orchestrator.run_scenario")
    def test_judge_codex_error_falls_back_to_run_status(self, run_scenario_mock, evaluate_mock) -> None:
        from rehearsal.codex_backend import CodexError

        run_dir = self.out_dir / "run1"
        run_scenario_mock.return_value = MagicMock(
            status="completed", turns=1, run_dir=run_dir, report_path=run_dir / "report.md",
        )
        evaluate_mock.side_effect = CodexError("codex not found")
        host = RunnerHost("h", "codex-session", {}, self.spec_path, backend=MagicMock(),
                          show_progress=False, user_runner_config=FAKE_USER_RUNNER)
        result = host.execute(self._case({"scenario": str(self.scenario_path)}), self.out_dir)
        self.assertEqual(result.status, "pass")
        self.assertIn("judge unavailable", result.detail)


class TurnProgressCallbackTest(unittest.TestCase):
    def test_prints_user_and_assistant_turns(self) -> None:
        from rehearsal.hosts.runner import _print_turn_progress
        from rehearsal.types import Event

        lines = []
        callback = _print_turn_progress("  ")
        with patch("builtins.print", side_effect=lambda *a: lines.append(" ".join(map(str, a)))):
            callback(Event.create("user_message", turn=1, content="hello there"))
            callback(Event.create("aut_result", turn=1, output="hi back",
                                  tool_calls=[{"server": "cortex", "tool": "notes_list"}]))
            callback(Event.create("run_finished", status="completed"))
        self.assertTrue(any("user: hello there" in line for line in lines))
        self.assertTrue(any("assistant: hi back" in line and "notes_list" in line for line in lines))
        self.assertTrue(any("completed" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
