from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

from .config import RunnerConfig, ScenarioConfig, TargetConfig
from .logging import JsonlLogger
from .mcp_config import write_mcp_servers_config
from .prompts import build_aut_prompt, build_user_emulator_prompt
from .report import write_markdown_report
from .runners import create_runner
from .types import Event, TranscriptTurn, utc_now


def build_run_id(target_id: str, scenario_id: str) -> str:
    timestamp = utc_now().replace("+00:00", "Z").replace(":", "")
    return f"{timestamp}-{target_id}-{scenario_id}"


def run_scenario(
    *,
    target: TargetConfig,
    scenario: ScenarioConfig,
    aut_runner_config: RunnerConfig,
    user_runner_config: RunnerConfig,
    output_dir: Path,
) -> Path:
    run_id = build_run_id(target.id, scenario.id)
    run_dir = output_dir / run_id
    event_log_path = run_dir / "events.jsonl"
    report_path = run_dir / "report.md"
    mcp_config_path = run_dir / "target.mcp.json"
    logger = JsonlLogger(event_log_path)
    write_mcp_servers_config(mcp_config_path, target)

    aut_runner_config = replace(
        aut_runner_config,
        env={
            **aut_runner_config.env,
            "REHEARSAL_TARGET_ID": target.id,
            "REHEARSAL_MCP_CONFIG": str(mcp_config_path.resolve()),
        },
    )

    aut_runner = create_runner(aut_runner_config, "aut")
    user_runner = create_runner(user_runner_config, "user")

    transcript: list[TranscriptTurn] = []
    status = "completed"

    logger.write(
        Event.create(
            "run_started",
            run_id=run_id,
            target=asdict(target),
            scenario=asdict(scenario),
            mcp_config_path=str(mcp_config_path),
            aut_runner=asdict(aut_runner_config),
            user_runner=asdict(user_runner_config),
        )
    )

    user_message = scenario.opening_message

    for turn_index in range(1, scenario.max_turns + 1):
        transcript.append(TranscriptTurn(role="user", content=user_message))
        logger.write(Event.create("user_message", turn=turn_index, content=user_message))

        aut_prompt = build_aut_prompt(
            target,
            scenario,
            transcript[:-1],
            user_message,
            str(mcp_config_path.resolve()),
        )
        aut_result = aut_runner.run_turn(aut_prompt)
        logger.write(
            Event.create(
                "aut_result",
                turn=turn_index,
                exit_code=aut_result.exit_code,
                timed_out=aut_result.timed_out,
                output=aut_result.output,
            )
        )

        if aut_result.timed_out or aut_result.exit_code != 0:
            status = "aut_failed"
            transcript.append(TranscriptTurn(role="assistant", content=aut_result.output))
            break

        transcript.append(TranscriptTurn(role="assistant", content=aut_result.output))

        user_prompt = build_user_emulator_prompt(scenario, transcript, aut_result.output)
        user_result = user_runner.run_turn(user_prompt)
        logger.write(
            Event.create(
                "user_emulator_result",
                turn=turn_index,
                exit_code=user_result.exit_code,
                timed_out=user_result.timed_out,
                output=user_result.output,
            )
        )

        if user_result.timed_out or user_result.exit_code != 0:
            status = "user_emulator_failed"
            break

        next_message = user_result.output.strip()
        if next_message == "REHEARSAL_DONE":
            status = "completed"
            break

        user_message = next_message
    else:
        status = "max_turns_reached"

    logger.write(Event.create("run_finished", status=status, transcript=[asdict(t) for t in transcript]))
    write_markdown_report(report_path, target, scenario, transcript, status, event_log_path)
    return report_path
