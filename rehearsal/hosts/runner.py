"""Runner-backed hosts: Codex sessions / spawned agent processes.

Wraps the existing `RunnerConfig` machinery (`rehearsal/runners.py`) in the
host-adapter interface so model-backed hosts appear in the test matrix next to
the direct protocol host. This is the actual dual-agent role-play GhostLab is
for: a user-emulator session (driven by a generated persona + scenario goal)
and an agent-under-test session with the target MCP wired in, going back and
forth turn by turn (`rehearsal/orchestrator.py`).

Conversational plan cases without a concrete `execution.scenario` are inert
seeds (``needs_generation: true``) — this host reports why it skipped instead
of pretending coverage exists. `ghostlab plan --generate` turns seeds into
real cases (see `rehearsal/plan_generate.py`).

When a `CodexBackend` is supplied, each run is scored by the judge
(`evaluate_run`) and critiqued for tool ergonomics (`critique_run`); the
judge's verdict — not just "did the conversation finish" — decides pass/fail,
since a session can complete without the user's goal actually being met.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import load_runner
from .base import CaseResult, HostAdapter, HostCapabilities


def _print_turn_progress(prefix: str) -> Callable[[Any], None]:
    """Live per-turn progress callback for `orchestrator.run_scenario`.

    Turn prompts (`aut_prompt`/`user_emulator_prompt`) carry the full prompt
    text and are noisy; only the actual conversation turns are printed.
    """

    def callback(event: Any) -> None:
        turn = event.data.get("turn")
        if event.type == "user_message":
            content = str(event.data.get("content", "")).replace("\n", " ")[:160]
            print(f"{prefix}turn {turn} user: {content}")
        elif event.type == "aut_result":
            output = str(event.data.get("output", "")).replace("\n", " ")[:160]
            tool_calls = event.data.get("tool_calls") or []
            tools = ", ".join(call.get("tool", "?") for call in tool_calls) or "-"
            print(f"{prefix}turn {turn} assistant: {output}  [tools: {tools}]")
        elif event.type == "run_finished":
            print(f"{prefix}-> {event.data.get('status')}")

    return callback


class RunnerHost(HostAdapter):
    capabilities = HostCapabilities(
        model_backed=True,
        executes_conversational=True,
        exposes_tool_trace=True,
        supports_session_resume=True,
    )

    def __init__(
        self,
        host_id: str,
        kind: str,
        host_config: dict[str, Any],
        spec_path: Path,
        backend: Optional[Any] = None,
        show_progress: bool = True,
        user_runner_config: Optional[Any] = None,
    ) -> None:
        self.id = host_id
        self.kind = kind
        self.host_config = host_config
        self.spec_path = spec_path
        self.backend = backend
        self.show_progress = show_progress
        # The user-emulator config is deliberately *not* derived from
        # host_config: the host's config_ref wires the target MCP into the
        # agent-under-test session, and the user emulator must never have
        # that access — it plays a human, not another tool-using agent.
        self.user_runner_config = user_runner_config
        self._runner_config = None

    def _load_runner_config(self):
        if self._runner_config is None:
            config_ref = self.host_config.get("config_ref")
            path: Optional[Path] = None
            if config_ref:
                path = Path(config_ref)
                if not path.is_absolute():
                    path = self.spec_path.resolve().parent / path
            self._runner_config = load_runner(path, fallback_kind=self.kind)
        return self._runner_config

    def version_info(self) -> dict[str, Any]:
        info = super().version_info()
        config = self.host_config.get("config_ref")
        if config:
            info["config_ref"] = str(config)
        return info

    def execute(self, case: dict[str, Any], out_dir: Path) -> CaseResult:
        execution = case.get("execution", {}) or {}
        started = time.monotonic()

        def done(status: str, detail: str = "", **artifacts: str) -> CaseResult:
            return CaseResult(
                case_id=case["id"],
                suite=case.get("suite", "?"),
                host=self.id,
                status=status,
                kind=case.get("kind", ""),
                detail=detail,
                duration_ms=(time.monotonic() - started) * 1000,
                artifacts=dict(artifacts),
            )

        if execution.get("needs_generation"):
            return done(
                "skip",
                "conversational seed; run `ghostlab plan --generate` to turn it "
                "into a real scenario",
            )
        scenario_ref = execution.get("scenario")
        if not scenario_ref:
            return done("skip", "no scenario attached to this conversational case")
        return self._run_scenario_case(case, execution, out_dir, done)

    def _resolve(self, ref: str) -> Path:
        path = Path(ref)
        return path if path.is_absolute() else self.spec_path.resolve().parent / path

    def _run_scenario_case(
        self, case: dict[str, Any], execution: dict[str, Any], out_dir: Path, done
    ) -> CaseResult:
        from ..config import ConfigError, load_persona, load_scenario
        from ..orchestrator import run_scenario
        from ..spec import load_spec

        try:
            spec = load_spec(self.spec_path)
            target = spec.target_config()
            scenario = load_scenario(self._resolve(str(execution["scenario"])))
            persona = None
            if execution.get("persona"):
                persona = load_persona(self._resolve(str(execution["persona"])))
            aut_runner_config = self._load_runner_config()
        except ConfigError as exc:
            return done("error", str(exc))

        capabilities, inspect_data = self._load_discovered_tools(spec)

        if self.user_runner_config is None:
            return done(
                "error",
                "no user-emulator runner configured for this host "
                "(pass --user-runner to `ghostlab test`)",
            )

        prefix = f"    [{case['id']}] "
        callback = _print_turn_progress(prefix) if self.show_progress else None
        if self.show_progress:
            who = persona.name if persona else "unnamed user"
            print(f"{prefix}goal: {scenario.goal!r} (persona: {who})")

        result = run_scenario(
            target=target,
            scenario=scenario,
            aut_runner_config=aut_runner_config,
            user_runner_config=self.user_runner_config,
            output_dir=out_dir,
            persona=persona,
            event_callback=callback,
        )
        return self._judge_and_critique(
            case, scenario, result, prefix, done, capabilities, inspect_data
        )

    def _load_discovered_tools(self, spec) -> tuple[Optional[dict], Optional[dict]]:
        """Ground truth for the judge/critique: the spec's discovered tools.

        Without this, the judge has nothing to check tool calls against and
        can (and did, in practice) flag a real, successfully-called tool as
        "hallucinated" purely by guessing from the transcript. Returns
        ``(capabilities, inspect_data)`` — a taxonomy-shaped dict for
        `evaluate_run`'s hallucination check, and the raw inspect.json for
        `critique_run`'s tool descriptions. Either may be None if discovery
        hasn't produced them yet; the judge/critique degrade gracefully.
        """
        tools = (spec.capabilities or {}).get("tools", [])
        capabilities = {"taxonomy": {"discovered": [t["name"] for t in tools if t.get("name")]}} \
            if tools else None

        inspect_data = None
        generated_from = (spec.capabilities or {}).get("generated_from", "")
        if generated_from:
            inspect_path = (self.spec_path.resolve().parent / generated_from).parent / "inspect.json"
            if inspect_path.exists():
                try:
                    import json

                    inspect_data = json.loads(inspect_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
        return capabilities, inspect_data

    def _judge_and_critique(
        self, case, scenario, result, prefix: str, done, capabilities=None, inspect_data=None
    ) -> CaseResult:
        artifacts = {"run_dir": str(result.run_dir), "report": str(result.report_path)}
        if self.backend is None:
            # No judge available: the conversation finishing is the best signal
            # we have, but that's not the same as the goal being met.
            status = "pass" if result.status == "completed" else "fail"
            return done(
                status,
                f"run {result.status} in {result.turns} turn(s) (no judge configured)",
                **artifacts,
            )

        from ..codex_backend import CodexError
        from ..critique import critique_run, write_critique_artifacts
        from ..evaluate import evaluate_run, write_verdict_artifacts

        try:
            verdict = evaluate_run(result.run_dir, self.backend, capabilities=capabilities)
        except CodexError as exc:
            status = "pass" if result.status == "completed" else "fail"
            return done(
                status,
                f"run {result.status} in {result.turns} turn(s) (judge unavailable: {exc})",
                **artifacts,
            )
        write_verdict_artifacts(verdict, result.run_dir)
        artifacts["verdict"] = str(result.run_dir / "verdict.json")
        # `gates` are hard checks (hallucinated tools, golden mismatches, triggered
        # failure signals) that can override a generous judge summary; surface them
        # so a "fail" next to an all-clear-sounding summary is never a mystery.
        gate_note = f" [gates: {', '.join(verdict['gates'])}]" if verdict.get("gates") else ""
        if self.show_progress:
            print(f"{prefix}judge: {verdict['verdict']} — {verdict['judge'].get('summary', '')}{gate_note}")

        try:
            critique = critique_run(result.run_dir, self.backend, inspect=inspect_data)
            write_critique_artifacts(critique, result.run_dir)
            artifacts["critique"] = str(result.run_dir / "critique.json")
        except Exception:  # noqa: BLE001 — a bonus signal; never fails the case
            pass

        detail = f"verdict={verdict['verdict']}: {verdict['judge'].get('summary', '')}{gate_note}"
        status = "pass" if verdict["verdict"] in ("pass", "partial") else "fail"
        return done(status, detail, **artifacts)
