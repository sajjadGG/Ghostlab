"""`rehearsal evaluate` — score a run into a pass/fail verdict.

Combines deterministic checks over the captured tool calls (failed calls,
expected-tool coverage) with a codex LLM-judge over the scenario's
success_criteria / failure_signals, and emits a structured `verdict.json` plus a
`verdict.md` section. Hard gates (the run crashed, a failure signal triggered, or
the assistant claimed a non-exposed tool) force an overall `fail`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .codex_backend import CodexBackend
from .logging import JsonlLogger
from .tool_capture import efficiency_metrics
from .types import Event, utc_now

VERDICTS = ("pass", "partial", "fail")

_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "met": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "required": ["index", "met", "evidence"],
                "additionalProperties": False,
            },
        },
        "failure_signals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "triggered": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
                "required": ["index", "triggered", "evidence"],
                "additionalProperties": False,
            },
        },
        "hallucinated_tools": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "summary": {"type": "string"},
    },
    "required": ["criteria", "failure_signals", "hallucinated_tools", "verdict", "summary"],
    "additionalProperties": False,
}


def read_run(run_dir: Path) -> dict[str, Any]:
    """Reconstruct a run from its ``events.jsonl`` file."""
    events: list[dict[str, Any]] = []
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return reconstruct_run(events)


def reconstruct_run(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Reconstruct run setup, chronological trace, transcript, and tool calls.

    Pure over an ordered list of ``{type,timestamp,data}`` events, so it works
    the same whether events come from ``events.jsonl`` or the SQLite ledger.
    """
    scenario: dict[str, Any] = {}
    persona: dict[str, Any] | None = None
    target: dict[str, Any] = {}
    transcript: list[dict[str, Any]] = []
    trace: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    prompts: list[dict[str, Any]] = []
    models: dict[str, str] = {}
    started_at: str | None = None
    finished_at: str | None = None
    evaluation: dict[str, Any] = {}
    status = "unknown"

    for event in events:
        data = event.get("data", {})
        if event["type"] == "run_started":
            scenario = data.get("scenario", {})
            persona = data.get("persona")
            target = data.get("target", {})
            models = data.get("models", {})
            if not models:
                models = {
                    "agent_under_test": _model_from_runner(data.get("aut_runner", {})),
                    "user_emulator": _model_from_runner(data.get("user_runner", {})),
                }
            started_at = event.get("timestamp")
        elif event["type"] in ("aut_prompt", "user_emulator_prompt"):
            prompts.append(
                {
                    "type": event["type"],
                    "turn": data.get("turn"),
                    "prompt": data.get("prompt", ""),
                    "stateful_resume": data.get("stateful_resume", False),
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event["type"] == "user_message":
            trace.append(
                {
                    "type": "message",
                    "role": "user",
                    "turn": data.get("turn"),
                    "content": data.get("content", ""),
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event["type"] == "aut_result":
            turn_calls = data.get("tool_calls", [])
            tool_calls.extend(turn_calls)
            trace.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "turn": data.get("turn"),
                    "content": data.get("output", ""),
                    "tool_calls": turn_calls,
                    "exit_code": data.get("exit_code"),
                    "timed_out": data.get("timed_out", False),
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event["type"] == "run_finished":
            transcript = data.get("transcript", [])
            status = data.get("status", "unknown")
            finished_at = event.get("timestamp")
        elif event["type"] == "evaluation_started":
            evaluation["started_at"] = event.get("timestamp")
            evaluation["model"] = data.get("model")
            evaluation["prompt"] = data.get("prompt", "")
        elif event["type"] == "evaluation_finished":
            evaluation["finished_at"] = event.get("timestamp")
            evaluation["verdict"] = data.get("verdict")

    return {
        "target": target,
        "scenario": scenario,
        "persona": persona,
        "transcript": transcript,
        "trace": trace,
        "tool_calls": tool_calls,
        "prompts": prompts,
        "models": models,
        "started_at": started_at,
        "finished_at": finished_at,
        "evaluation": evaluation,
        "status": status,
    }


def _model_from_runner(runner: dict[str, Any]) -> str:
    command = runner.get("command", [])
    for flag in ("-m", "--model"):
        if flag in command:
            index = command.index(flag)
            if index + 1 < len(command):
                return str(command[index + 1])
    return "codex default"


def deterministic_checks(scenario: dict[str, Any], tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    called = {f"{c.get('server', '?')}/{c.get('tool', '?')}".split("/")[-1] for c in tool_calls}
    expected = list(scenario.get("exercises", []))
    missing = [t for t in expected if t not in called]
    failures = [
        f"{c.get('server', '?')}/{c.get('tool', '?')}"
        for c in tool_calls
        if c.get("status") == "failed"
    ]
    return {
        "tool_failures": failures,
        "exercises_expected": expected,
        "exercises_called": sorted(called),
        "exercises_missing": missing,
        "coverage": f"{len(expected) - len(missing)}/{len(expected)}" if expected else "n/a",
        "no_tool_calls": len(tool_calls) == 0,
        "efficiency": efficiency_metrics(tool_calls),
    }


def _final_assistant_turn(transcript: list[dict[str, Any]]) -> str:
    for turn in reversed(transcript):
        if turn.get("role") == "assistant":
            return turn.get("content", "") or ""
    return ""


def _args_match(expected: dict[str, Any], actual: Any) -> bool:
    """True if every expected key/value is present and equal in the actual args."""
    if not isinstance(actual, dict):
        return not expected  # no captured args can only satisfy an empty expectation
    return all(actual.get(key) == value for key, value in expected.items())


def check_expected_outcome(
    scenario: dict[str, Any],
    transcript: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate a scenario's optional deterministic golden assertions.

    Returns ``{"defined": False}`` when the scenario declares no expectations.
    Otherwise reports each unmet assertion and an overall ``passed`` flag, giving
    objective, judge-independent grading for scenarios with a known-correct answer.
    """
    outcome = scenario.get("expected_outcome") or {}
    if not outcome:
        return {"defined": False}

    final = _final_assistant_turn(transcript).lower()
    missing = [s for s in outcome.get("must_include", []) if s.lower() not in final]
    forbidden = [s for s in outcome.get("must_not_include", []) if s.lower() in final]

    arg_mismatches: list[str] = []
    for expected in outcome.get("expected_tool_args", []):
        tool = expected.get("tool")
        want = expected.get("arguments", {})
        ok = any(
            call.get("tool") == tool and _args_match(want, call.get("arguments"))
            for call in tool_calls
        )
        if not ok:
            arg_mismatches.append(f"{tool}({json.dumps(want)})")

    passed = not (missing or forbidden or arg_mismatches)
    return {
        "defined": True,
        "passed": passed,
        "missing_substrings": missing,
        "forbidden_present": forbidden,
        "tool_arg_mismatches": arg_mismatches,
    }


def _format_transcript(transcript: list[dict[str, Any]]) -> str:
    return "\n".join(f"{t.get('role', '?').upper()}: {t.get('content', '')}" for t in transcript)


def _format_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    if not tool_calls:
        return "(no tool calls captured)"
    lines = []
    for call in tool_calls:
        name = f"{call.get('server', '?')}/{call.get('tool', '?')}"
        args = call.get("arguments")
        arg_str = json.dumps(args)[:200] if args is not None else ""
        lines.append(f"- {name} [{call.get('status', '?')}] {arg_str}")
    return "\n".join(lines)


# Placeholders: {goal} {criteria_block} {signals_block} {tools_line}
# {tool_calls} {transcript}
JUDGE_TEMPLATE = """You are evaluating one end-to-end test conversation against an MCP server.
A user (emulated) talked to an assistant that has access to MCP tools.

Scenario goal:
{goal}

Success criteria (judge each by index):
{criteria_block}

Failure signals to check for (judge each by index):
{signals_block}
{tools_line}
Tool calls the assistant actually made:
{tool_calls}

Conversation transcript:
{transcript}

Judge strictly from evidence in the transcript and tool calls. For each success
criterion return met (true/false) + short evidence. For each failure signal
return triggered (true/false) + short evidence. Give an overall verdict:
- "pass": all criteria met and no failure signal triggered.
- "partial": some but not all criteria met, no failure signal triggered.
- "fail": no criteria met, or any failure signal triggered.
Output only the JSON object."""


def _build_judge_prompt(run: dict[str, Any], tool_names: list[str] | None) -> str:
    from . import prompts

    scenario = run["scenario"]
    criteria = scenario.get("success_criteria", [])
    signals = scenario.get("failure_signals", [])
    criteria_block = "\n".join(f"{i}. {c}" for i, c in enumerate(criteria)) or "(none)"
    signals_block = "\n".join(f"{i}. {s}" for i, s in enumerate(signals)) or "(none)"
    tools_line = (
        f"\nThe ONLY real tools this server exposes are: {', '.join(tool_names)}.\n"
        "If the assistant claims to use or have used any tool NOT in that list, list it in "
        "hallucinated_tools.\n"
        if tool_names
        else ""
    )
    return prompts.render(
        "judge",
        JUDGE_TEMPLATE,
        goal=scenario.get("goal", ""),
        criteria_block=criteria_block,
        signals_block=signals_block,
        tools_line=tools_line,
        tool_calls=_format_tool_calls(run["tool_calls"]),
        transcript=_format_transcript(run["transcript"]),
    )


def combine_verdict(
    status: str, deterministic: dict[str, Any], judge: dict[str, Any]
) -> tuple[str, list[str]]:
    """Fold judge result + hard gates into a final verdict + list of gate hits."""
    gates: list[str] = []
    if status in ("aut_failed", "user_emulator_failed"):
        gates.append(f"run_status:{status}")
    outcome = deterministic.get("expected_outcome", {})
    if outcome.get("defined") and not outcome.get("passed"):
        gates.append("golden_mismatch")
    triggered = [s for s in judge.get("failure_signals", []) if s.get("triggered")]
    if triggered:
        gates.append(f"failure_signals_triggered:{len(triggered)}")
    if judge.get("hallucinated_tools"):
        gates.append("hallucinated_tools:" + ",".join(judge["hallucinated_tools"]))

    verdict = judge.get("verdict", "fail")
    if verdict not in VERDICTS:
        verdict = "fail"
    if gates:
        verdict = "fail"
    return verdict, gates


def judge_prompt(run: dict[str, Any], capabilities: dict[str, Any] | None = None) -> str:
    """The exact judge prompt for a run dict (from read_run), for UI preview."""
    tool_names = None
    if capabilities:
        names: list[str] = []
        for group in capabilities.get("taxonomy", {}).values():
            names.extend(group)
        tool_names = names
    return _build_judge_prompt(run, tool_names)


def evaluate_run(
    run_dir: Path,
    backend: CodexBackend,
    capabilities: dict[str, Any] | None = None,
    store: "Any | None" = None,
) -> dict[str, Any]:
    run = read_run(run_dir)
    deterministic = deterministic_checks(run["scenario"], run["tool_calls"])
    deterministic["expected_outcome"] = check_expected_outcome(
        run["scenario"], run["transcript"], run["tool_calls"]
    )

    tool_names: list[str] | None = None
    if capabilities:
        names: list[str] = []
        for group in capabilities.get("taxonomy", {}).values():
            names.extend(group)
        tool_names = names

    prompt = _build_judge_prompt(run, tool_names)
    model = backend.model or "codex default"
    logger = JsonlLogger(run_dir / "events.jsonl")
    started_at = utc_now()
    started_event = Event.create("evaluation_started", model=model, prompt=prompt)
    logger.write(started_event)

    # SQLite persistence is best-effort: the verdict artifacts are always written.
    run_db_id = None
    if store is not None:
        try:
            run_db_id = store.run_id_by_public(run_dir.name)
            if run_db_id is not None:
                store.append_event(run_db_id, started_event)
        except Exception:  # noqa: BLE001
            run_db_id = None

    judge = backend.generate_json(prompt, _JUDGE_SCHEMA)
    verdict, gates = combine_verdict(run["status"], deterministic, judge)
    result = {
        "run_dir": str(run_dir),
        "scenario": run["scenario"].get("id", "?"),
        "run_status": run["status"],
        "verdict": verdict,
        "gates": gates,
        "deterministic": deterministic,
        "judge": judge,
    }
    finished_event = Event.create("evaluation_finished", verdict=verdict, gates=gates)
    logger.write(finished_event)
    if run_db_id is not None:
        try:
            store.append_event(run_db_id, finished_event)
            store.record_judgment(
                run_dir.name, result, model=model, prompt_text=prompt,
                started_at=started_at, finished_at=utc_now(),
            )
        except Exception:  # noqa: BLE001
            pass
    return result


def evidence_references(run: dict[str, Any], evidence: str) -> list[str]:
    """Find likely trace turns and tool calls referenced by judge evidence."""
    evidence_lower = evidence.lower()
    references: list[str] = []
    for item in run.get("trace", []):
        turn = item.get("turn", "?")
        content = str(item.get("content", "")).lower()
        if content and any(token in content for token in evidence_lower.split() if len(token) > 7):
            label = f"{item.get('role', '?')} turn {turn}"
            if label not in references:
                references.append(label)
        for call in item.get("tool_calls", []):
            tool = str(call.get("tool", ""))
            full_name = f"{call.get('server', '?')}/{tool}"
            if tool and tool.lower() in evidence_lower and f"{full_name} · turn {turn}" not in references:
                references.append(f"{full_name} · turn {turn}")
    return references[:5]


def write_verdict_artifacts(verdict: dict[str, Any], run_dir: Path) -> tuple[Path, Path]:
    json_path = run_dir / "verdict.json"
    md_path = run_dir / "verdict.md"
    json_path.write_text(json.dumps(verdict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_verdict_md(verdict), encoding="utf-8")
    return json_path, md_path


def _efficiency_line(eff: dict[str, Any]) -> str:
    parts = [
        f"{eff.get('total_calls', 0)} calls",
        f"{eff.get('unique_tools', 0)} unique",
        f"{eff.get('redundant_calls', 0)} redundant",
    ]
    if "avg_duration_ms" in eff:
        parts.append(f"avg {eff['avg_duration_ms']}ms")
    return "- Tool efficiency: " + ", ".join(parts)


def render_verdict_md(verdict: dict[str, Any]) -> str:
    judge = verdict.get("judge", {})
    det = verdict.get("deterministic", {})
    lines = [
        f"# Verdict: {verdict['verdict'].upper()}  ({verdict['scenario']})",
        "",
        f"- Run status: `{verdict['run_status']}`",
        f"- Gates: {', '.join(verdict['gates']) if verdict['gates'] else 'none'}",
        f"- Tool coverage: {det.get('coverage', 'n/a')}"
        + (f" (missing: {', '.join(det['exercises_missing'])})" if det.get("exercises_missing") else ""),
        f"- Failed tool calls: {', '.join(det['tool_failures']) if det.get('tool_failures') else 'none'}",
        _efficiency_line(det.get("efficiency", {})),
        "",
        f"**{judge.get('summary', '')}**",
        "",
        "## Success criteria",
        "",
    ]
    for item in judge.get("criteria", []):
        mark = "x" if item.get("met") else " "
        lines.append(f"- [{mark}] ({item.get('index')}) {item.get('evidence', '')}")
    lines += ["", "## Failure signals", ""]
    for item in judge.get("failure_signals", []):
        mark = "!" if item.get("triggered") else " "
        lines.append(f"- [{mark}] ({item.get('index')}) {item.get('evidence', '')}")
    if judge.get("hallucinated_tools"):
        lines += ["", "## Hallucinated tools", "", ", ".join(judge["hallucinated_tools"])]

    outcome = det.get("expected_outcome", {})
    if outcome.get("defined"):
        lines += ["", "## Golden assertions", ""]
        lines.append(f"- Result: {'PASS' if outcome.get('passed') else 'FAIL'}")
        if outcome.get("missing_substrings"):
            lines.append(f"- Missing required text: {', '.join(outcome['missing_substrings'])}")
        if outcome.get("forbidden_present"):
            lines.append(f"- Forbidden text present: {', '.join(outcome['forbidden_present'])}")
        if outcome.get("tool_arg_mismatches"):
            lines.append(f"- Unmet tool-arg expectations: {', '.join(outcome['tool_arg_mismatches'])}")
    lines.append("")
    return "\n".join(lines)
