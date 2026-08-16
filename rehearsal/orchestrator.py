from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .config import PersonaConfig, RunnerConfig, ScenarioConfig, TargetConfig
from .logging import JsonlLogger
from .mcp_apps import widgets_from_tool_calls
from .mcp_config import write_mcp_servers_config
from .prompts import (
    build_aut_prompt,
    build_user_emulator_prompt,
    normalize_user_emulator_message,
)
from .report import write_markdown_report
from .runners import create_runner, redact_host_noise
from .tool_capture import (
    annotate_tool_failures,
    parse_codex_output,
    parse_opencode_output,
    parse_tool_calls,
    summarize_tool_calls,
)
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


def _runner_model(config: RunnerConfig) -> str:
    """Return the model selected in a runner command, or the codex default."""
    for flag in ("-m", "--model"):
        if flag in config.command:
            index = config.command.index(flag)
            if index + 1 < len(config.command):
                return config.command[index + 1]
    return "codex default"


def classify_runner_failure(*, exit_code: int, timed_out: bool, stderr: str, output: str) -> str:
    """Separate retryable agent-backend outages from failures of the target."""
    text = f"{stderr}\n{output}".lower()
    unavailable = (
        "out of credits", "insufficient_quota", "quota exceeded", "rate limit",
        "too many requests", "authentication", "unauthorized", "model unavailable",
        "service unavailable", "connection refused", "failed to connect",
        "sandbox_runtime_missing", "sandbox_setup_failed", "sandbox_policy_missing",
        "sandbox_upload_missing", "sandbox_timeout",
        "sandbox_gateway_unavailable", "sandbox_policy_invalid", "sandbox_image_unavailable",
    )
    if timed_out or exit_code == 124 or any(marker in text for marker in unavailable):
        return "backend_unavailable"
    return "runner_failed"


def run_scenario(
    *,
    target: TargetConfig,
    scenario: ScenarioConfig,
    aut_runner_config: RunnerConfig,
    user_runner_config: RunnerConfig,
    output_dir: Path,
    persona: PersonaConfig | None = None,
    event_callback: Callable[[Event], None] | None = None,
    store: "Any | None" = None,
    batch_id: int | None = None,
    case_public_id: str | None = None,
    inspection_public_id: str | None = None,
    profile_public_id: str | None = None,
    apps_mode: bool = False,
    apps_backend: "Any | None" = None,
) -> RunResult:
    run_id = build_run_id(target.id, scenario.id)
    run_dir = output_dir / run_id
    event_log_path = run_dir / "events.jsonl"
    report_path = run_dir / "report.md"
    mcp_config_path = run_dir / "target.mcp.json"
    logger = JsonlLogger(event_log_path)
    if target.transport == "skill":
        skill_path = Path(str(target.connection.get("path", ""))).expanduser().resolve()
        mcp_config_path = skill_path
    elif target.transport == "agent":
        mcp_config_path = run_dir / "agent.json"
        mcp_config_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_config_path.write_text(
            json.dumps(target.capabilities.get("agent_definition", {}), indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        write_mcp_servers_config(mcp_config_path, target)

    def sandbox_for(config: RunnerConfig, role: str, include_target: bool) -> dict[str, Any]:
        sandbox = {**dict(config.sandbox or {}), "artifact_dir": str(run_dir), "name": run_id}
        uploads = list(sandbox.get("uploads", []) or [])
        if include_target and mcp_config_path.exists():
            uploads.append({
                "source": str(mcp_config_path),
                "target": f"/sandbox/workspace/{mcp_config_path.name}",
            })
        sandbox["uploads"] = uploads
        sandbox["role"] = role
        return sandbox

    aut_model = _runner_model(aut_runner_config)
    user_model = _runner_model(user_runner_config)
    # SQLite is the system of record, but a persistence hiccup must never abort a
    # live run — the JSONL log and report are always written regardless.
    run_db_id: int | None = None
    if store is not None:
        try:
            run_db_id = store.start_run(
                run_id,
                target=target,
                scenario=scenario,
                persona=persona,
                aut_runner=aut_runner_config,
                user_runner=user_runner_config,
                aut_model=aut_model,
                user_model=user_model,
                max_turns=scenario.max_turns,
                batch_id=batch_id,
                case_public_id=case_public_id,
                inspection_public_id=inspection_public_id,
                profile_public_id=profile_public_id,
            )
        except Exception:  # noqa: BLE001 — persistence is best-effort
            run_db_id = None

    aut_runner_config = replace(
        aut_runner_config,
        env={
            **aut_runner_config.env,
            "REHEARSAL_TARGET_ID": target.id,
            "REHEARSAL_MCP_CONFIG": (
                f"/sandbox/workspace/{mcp_config_path.name}"
                if (aut_runner_config.sandbox or {}).get("backend") == "openshell"
                else str(mcp_config_path.resolve())
            ),
        },
        sandbox=sandbox_for(aut_runner_config, "aut", True),
    )
    user_runner_config = replace(
        user_runner_config,
        sandbox=sandbox_for(user_runner_config, "user", False),
    )

    aut_runner = create_runner(aut_runner_config, "aut")
    user_runner = create_runner(user_runner_config, "user")

    transcript: list[TranscriptTurn] = []
    tool_calls_by_turn: dict[int, list] = {}
    status = "completed"

    def emit(event: Event) -> None:
        logger.write(event)
        if run_db_id is not None:
            try:
                store.append_event(run_db_id, event)
            except Exception:  # noqa: BLE001 — persistence is best-effort
                pass
        if event_callback is not None:
            event_callback(event)

    emit(
        Event.create(
            "run_started",
            run_id=run_id,
            target=asdict(target),
            scenario=asdict(scenario),
            mcp_config_path=str(mcp_config_path),
            aut_runner=asdict(aut_runner_config),
            user_runner=asdict(user_runner_config),
            models={
                "agent_under_test": _runner_model(aut_runner_config),
                "user_emulator": _runner_model(user_runner_config),
            },
            persona=asdict(persona) if persona else None,
        )
    )

    user_message = scenario.opening_message
    aut_stateful = getattr(aut_runner, "stateful", False)

    # MCP Apps mode: a live host that renders the widgets the agent opens and
    # lets the user operate them for real (DOM actions -> backend tools/calls,
    # Submit -> a follow-up message back into the conversation). Opt-in; a
    # connection failure degrades to the text-only widget flow.
    apps_session = None
    persona_note = (persona.summary if persona else "") or scenario.persona
    if apps_mode:
        try:
            from .apps_host.live import AppsHostSession

            apps_session = AppsHostSession.connect(target, backend=apps_backend, out_dir=run_dir)
            emit(Event.create("apps_mode_started", ui_tools=len(apps_session.ui_map)))
        except Exception as exc:  # noqa: BLE001 — never abort the run over apps setup
            emit(Event.create("apps_mode_unavailable", reason=str(exc)))
            apps_session = None

    for turn_index in range(1, scenario.max_turns + 1):
        transcript.append(TranscriptTurn(role="user", content=user_message))
        emit(Event.create("user_message", turn=turn_index, content=user_message))

        if aut_stateful and turn_index > 1:
            # The session already holds prior context; send only the new message.
            aut_prompt = user_message
        else:
            aut_prompt = build_aut_prompt(
                target,
                scenario,
                transcript[:-1],
                user_message,
                str(mcp_config_path.resolve()),
            )
        emit(
            Event.create(
                "aut_prompt",
                turn=turn_index,
                prompt=aut_prompt,
                stateful_resume=aut_stateful and turn_index > 1,
            )
        )
        aut_result = aut_runner.run_turn(aut_prompt)
        # The conversational message is stdout only, with known host noise
        # stripped; stderr is logged separately and never shown to the emulator.
        # With the codex-json parser we recover the message and rich tool calls
        # (arguments/result/error) from the JSONL stream instead.
        builtin_calls: list = []
        if aut_runner_config.parser == "codex-json":
            parsed = parse_codex_output(aut_result.output)
            aut_message = parsed["message"] or redact_host_noise(aut_result.output)
            tool_calls = parsed["tool_calls"]
        elif aut_runner_config.parser == "opencode-json":
            # opencode namespaces MCP tools as `<server>_<tool>`; pass the target
            # id so its own built-in tools are not mistaken for MCP calls.
            servers = [target.id] + [
                str(mcp.get("id")) for mcp in
                ((target.capabilities or {}).get("agent_definition", {})
                 .get("inputs", {}) or {}).get("mcps", []) or []
            ]
            parsed = parse_opencode_output(aut_result.output, servers=servers)
            aut_message = parsed["message"] or redact_host_noise(aut_result.output)
            tool_calls = parsed["tool_calls"]
            builtin_calls = parsed["builtin_calls"]
        else:
            aut_message = redact_host_noise(aut_result.output)
            tool_calls = parse_tool_calls(aut_result.output, aut_result.stderr)
        annotate_tool_failures(tool_calls, aut_result.stderr)
        tool_calls_by_turn[turn_index] = tool_calls
        emit(
            Event.create(
                "aut_result",
                turn=turn_index,
                exit_code=aut_result.exit_code,
                timed_out=aut_result.timed_out,
                output=aut_message,
                stderr=aut_result.stderr,
                tool_calls=tool_calls,
                builtin_calls=builtin_calls,
                session_id=getattr(aut_runner, "thread_id", None),
            )
        )

        if aut_result.timed_out or aut_result.exit_code != 0:
            cause = classify_runner_failure(
                exit_code=aut_result.exit_code, timed_out=aut_result.timed_out,
                stderr=aut_result.stderr, output=aut_result.output,
            )
            status = "backend_unavailable" if cause == "backend_unavailable" else "aut_failed"
            emit(Event.create("harness_failure", actor="agent_under_test", cause=cause,
                              exit_code=aut_result.exit_code, timed_out=aut_result.timed_out))
            transcript.append(TranscriptTurn(role="assistant", content=aut_message))
            break

        transcript.append(TranscriptTurn(role="assistant", content=aut_message))

        widgets = widgets_from_tool_calls(tool_calls)
        if widgets:
            emit(Event.create("widgets_shown", turn=turn_index, widgets=widgets))

        # If the agent opened a widget and apps mode is on, the user operates it
        # for real. A Submit that emits a follow-up message IS the user's next
        # turn — they "spoke" through the widget — so we skip the text emulator.
        widget_follow_up = ""
        if apps_session is not None and tool_calls:
            try:
                outcomes = apps_session.drive_turn(tool_calls, scenario.goal, persona_note)
            except Exception as exc:  # noqa: BLE001 — widget failure must not kill the run
                outcomes = []
                emit(Event.create("widget_interaction_error", turn=turn_index, reason=str(exc)))
            follow_ups: list[str] = []
            for outcome in outcomes:
                emit(Event.create("widget_interaction", turn=turn_index, outcome=outcome.to_json()))
                text = outcome.follow_up_text()
                if text:
                    follow_ups.append(text)
            widget_follow_up = "\n\n".join(follow_ups)

        if widget_follow_up:
            # The next loop iteration emits the user_message (with this content);
            # mark that its source was the widget, not the text emulator.
            emit(Event.create("widget_follow_up", turn=turn_index, content=widget_follow_up))
            user_message = widget_follow_up
            continue

        user_prompt = build_user_emulator_prompt(
            scenario, transcript, aut_message, persona, widgets=widgets
        )
        emit(Event.create("user_emulator_prompt", turn=turn_index, prompt=user_prompt))
        user_result = user_runner.run_turn(user_prompt)
        if user_runner_config.parser in ("opencode-json", "opencode-text"):
            # The emulator speaks over an opencode JSON event stream; recover the
            # human-visible reply so the AUT never sees raw protocol frames.
            user_message_out = (
                parse_opencode_output(user_result.output)["message"]
                or redact_host_noise(user_result.output)
            )
        else:
            user_message_out = redact_host_noise(user_result.output)
        emit(
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
            cause = classify_runner_failure(
                exit_code=user_result.exit_code, timed_out=user_result.timed_out,
                stderr=user_result.stderr, output=user_result.output,
            )
            status = "backend_unavailable"
            emit(Event.create("harness_failure", actor="user_emulator", cause=cause,
                              exit_code=user_result.exit_code, timed_out=user_result.timed_out))
            break

        # A text-only widget can legitimately ask for a long essay/form value;
        # ordinary chat turns keep the tighter realism budget.
        next_message = normalize_user_emulator_message(
            user_message_out, max_chars=4000 if widgets else 500
        )
        if next_message == "REHEARSAL_DONE":
            status = "completed"
            break

        user_message = next_message
    else:
        status = "max_turns_reached"

    if apps_session is not None:
        apps_session.close()
    aut_runner.close()
    user_runner.close()

    all_tool_calls = [call for turn in sorted(tool_calls_by_turn) for call in tool_calls_by_turn[turn]]
    emit(
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
    if run_db_id is not None:
        try:
            store.finish_run(run_db_id, status=status, turns_completed=turns)
        except Exception:  # noqa: BLE001 — persistence is best-effort
            pass
    return RunResult(report_path=report_path, run_dir=run_dir, status=status, turns=turns)
