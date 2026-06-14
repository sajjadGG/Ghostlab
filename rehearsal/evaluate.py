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
from .tool_capture import efficiency_metrics

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
    """Reconstruct scenario, transcript, and tool calls from a run's events."""
    scenario: dict[str, Any] = {}
    persona: dict[str, Any] | None = None
    transcript: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    status = "unknown"

    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        data = event.get("data", {})
        if event["type"] == "run_started":
            scenario = data.get("scenario", {})
            persona = data.get("persona")
        elif event["type"] == "aut_result":
            tool_calls.extend(data.get("tool_calls", []))
        elif event["type"] == "run_finished":
            transcript = data.get("transcript", [])
            status = data.get("status", "unknown")

    return {
        "scenario": scenario,
        "persona": persona,
        "transcript": transcript,
        "tool_calls": tool_calls,
        "status": status,
    }


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


def _build_judge_prompt(run: dict[str, Any], tool_names: list[str] | None) -> str:
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
    return f"""You are evaluating one end-to-end test conversation against an MCP server.
A user (emulated) talked to an assistant that has access to MCP tools.

Scenario goal:
{scenario.get('goal', '')}

Success criteria (judge each by index):
{criteria_block}

Failure signals to check for (judge each by index):
{signals_block}
{tools_line}
Tool calls the assistant actually made:
{_format_tool_calls(run['tool_calls'])}

Conversation transcript:
{_format_transcript(run['transcript'])}

Judge strictly from evidence in the transcript and tool calls. For each success
criterion return met (true/false) + short evidence. For each failure signal
return triggered (true/false) + short evidence. Give an overall verdict:
- "pass": all criteria met and no failure signal triggered.
- "partial": some but not all criteria met, no failure signal triggered.
- "fail": no criteria met, or any failure signal triggered.
Output only the JSON object."""


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
    run_dir: Path, backend: CodexBackend, capabilities: dict[str, Any] | None = None
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

    judge = backend.generate_json(_build_judge_prompt(run, tool_names), _JUDGE_SCHEMA)
    verdict, gates = combine_verdict(run["status"], deterministic, judge)

    return {
        "run_dir": str(run_dir),
        "scenario": run["scenario"].get("id", "?"),
        "run_status": run["status"],
        "verdict": verdict,
        "gates": gates,
        "deterministic": deterministic,
        "judge": judge,
    }


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
