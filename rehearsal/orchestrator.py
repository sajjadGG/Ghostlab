from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .config import PersonaConfig, RunnerConfig, ScenarioConfig, TargetConfig
from .logging import JsonlLogger
from .mcp_config import write_mcp_servers_config
from .prompts import build_aut_prompt, build_user_emulator_prompt
from .report import write_markdown_report
from .runners import create_runner, redact_host_noise
from .tool_capture import parse_tool_calls, summarize_tool_calls
from .types import Event, TranscriptTurn, utc_now


@dataclass(frozen=True)
class RunResult:
    report_path: Path
    run_dir: Path
    status: str
    turns: int


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
    persona: PersonaConfig | None = None,
) -> RunResult:
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
    tool_calls_by_turn: dict[int, list] = {}
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
            persona=asdict(persona) if persona else None,
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
        # The conversational message is stdout only, with known host noise
        # stripped; stderr is logged separately and never shown to the emulator.
        aut_message = redact_host_noise(aut_result.output)
        tool_calls = parse_tool_calls(aut_result.output, aut_result.stderr)
        tool_calls_by_turn[turn_index] = tool_calls
        logger.write(
            Event.create(
                "aut_result",
                turn=turn_index,
                exit_code=aut_result.exit_code,
                timed_out=aut_result.timed_out,
                output=aut_message,
                stderr=aut_result.stderr,
                tool_calls=tool_calls,
            )
        )

        if aut_result.timed_out or aut_result.exit_code != 0:
            status = "aut_failed"
            transcript.append(TranscriptTurn(role="assistant", content=aut_message))
            break

        transcript.append(TranscriptTurn(role="assistant", content=aut_message))

        user_prompt = build_user_emulator_prompt(scenario, transcript, aut_message, persona)
        user_result = user_runner.run_turn(user_prompt)
        user_message_out = redact_host_noise(user_result.output)
        logger.write(
            Event.create(
                "user_emulator_result",
                turn=turn_index,
                exit_code=user_result.exit_code,
                timed_out=user_result.timed_out,
                output=user_message_out,
                stderr=user_result.stderr,
            )
        )

        if user_result.timed_out or user_result.exit_code != 0:
            status = "user_emulator_failed"
            break

        next_message = user_message_out.strip()
        if next_message == "REHEARSAL_DONE":
            status = "completed"
            break

        user_message = next_message
    else:
        status = "max_turns_reached"

    all_tool_calls = [call for turn in sorted(tool_calls_by_turn) for call in tool_calls_by_turn[turn]]
    logger.write(
        Event.create(
            "run_finished",
            status=status,
            transcript=[asdict(t) for t in transcript],
            tool_call_summary=summarize_tool_calls(all_tool_calls),
        )
    )
    write_markdown_report(
        report_path, target, scenario, transcript, status, event_log_path, tool_calls_by_turn
    )
    turns = sum(1 for turn in transcript if turn.role == "assistant")
    return RunResult(report_path=report_path, run_dir=run_dir, status=status, turns=turns)
