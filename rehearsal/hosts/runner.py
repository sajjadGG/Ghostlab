"""Runner-backed hosts: Codex sessions / spawned agent processes.

Wraps the existing `RunnerConfig` machinery (`rehearsal/runners.py`) in the
host-adapter interface so model-backed hosts appear in the test matrix next to
the direct protocol host. Conversational plan cases are seeds
(``needs_generation: true``) until scenario generation is wired into the plan
(Phase A5); until then this host reports *why* it skipped instead of
pretending coverage exists. Cases that carry a concrete ``scenario`` file
reference are executed through the existing dual-agent orchestrator.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from ..config import load_runner
from .base import CaseResult, HostAdapter, HostCapabilities


class RunnerHost(HostAdapter):
    capabilities = HostCapabilities(
        model_backed=True,
        executes_conversational=True,
        exposes_tool_trace=True,
        supports_session_resume=True,
    )

    def __init__(
        self, host_id: str, kind: str, host_config: dict[str, Any], spec_path: Path
    ) -> None:
        self.id = host_id
        self.kind = kind
        self.host_config = host_config
        self.spec_path = spec_path
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

        def done(status: str, detail: str = "") -> CaseResult:
            return CaseResult(
                case_id=case["id"],
                suite=case.get("suite", "?"),
                host=self.id,
                status=status,
                detail=detail,
                duration_ms=(time.monotonic() - started) * 1000,
            )

        if execution.get("needs_generation"):
            return done(
                "skip",
                "conversational seed; generate a scenario for it first "
                "(generate-scenarios), then attach it as execution.scenario",
            )
        scenario_ref = execution.get("scenario")
        if not scenario_ref:
            return done("skip", "no scenario attached to this conversational case")
        return self._run_scenario_case(case, str(scenario_ref), out_dir, done)

    def _run_scenario_case(
        self, case: dict[str, Any], scenario_ref: str, out_dir: Path, done
    ) -> CaseResult:
        from ..config import ConfigError, load_scenario
        from ..orchestrator import run_scenario
        from ..spec import load_spec

        try:
            spec = load_spec(self.spec_path)
            target = spec.target_config()
            scenario_path = Path(scenario_ref)
            if not scenario_path.is_absolute():
                scenario_path = self.spec_path.resolve().parent / scenario_path
            scenario = load_scenario(scenario_path)
            runner_config = self._load_runner_config()
        except ConfigError as exc:
            return done("error", str(exc))

        result = run_scenario(
            target=target,
            scenario=scenario,
            aut_runner_config=runner_config,
            user_runner_config=runner_config,
            output_dir=out_dir,
        )
        if result.status == "completed":
            return done("pass", f"completed in {result.turns} turn(s)")
        return done("fail", f"run status: {result.status}")
