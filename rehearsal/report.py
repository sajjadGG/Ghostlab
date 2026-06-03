from __future__ import annotations

from pathlib import Path

from .config import ScenarioConfig, TargetConfig
from .types import TranscriptTurn


def write_markdown_report(
    path: Path,
    target: TargetConfig,
    scenario: ScenarioConfig,
    transcript: list[TranscriptTurn],
    status: str,
    event_log_path: Path,
    tool_calls_by_turn: dict[int, list[dict]] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tool_calls_by_turn = tool_calls_by_turn or {}

    lines = [
        f"# Rehearsal Run: {scenario.id}",
        "",
        f"- Status: `{status}`",
        f"- Target: `{target.id}`",
        f"- Transport: `{target.transport}`",
        f"- Scenario: `{scenario.title}`",
        f"- Event log: `{event_log_path}`",
        "",
        "## Goal",
        "",
        scenario.goal,
        "",
    ]

    all_calls = [call for turn in sorted(tool_calls_by_turn) for call in tool_calls_by_turn[turn]]
    if all_calls:
        if scenario.exercises:
            called = {f"{c['server']}/{c['tool']}".split("/")[-1] for c in all_calls}
            covered = [t for t in scenario.exercises if t in called]
            missing = [t for t in scenario.exercises if t not in called]
            lines += [
                "## Tool coverage",
                "",
                f"- expected (exercises): {', '.join(f'`{t}`' for t in scenario.exercises)}",
                f"- called: {', '.join(f'`{t}`' for t in sorted(called)) or '(none)'}",
                f"- missing: {', '.join(f'`{t}`' for t in missing) or '(none)'}"
                + (f"  — covered {len(covered)}/{len(scenario.exercises)}"),
                "",
            ]
        lines += ["## Tool calls", "", "| turn | # | tool | status |", "| --- | --- | --- | --- |"]
        for turn in sorted(tool_calls_by_turn):
            for call in tool_calls_by_turn[turn]:
                lines.append(
                    f"| {turn} | {call['index']} | `{call['server']}/{call['tool']}` | {call['status']} |"
                )
        lines.append("")

    lines += ["## Transcript", ""]
    for index, turn in enumerate(transcript, start=1):
        lines.extend([f"### {index}. {turn.role}", "", turn.content, ""])

    path.write_text("\n".join(lines), encoding="utf-8")

