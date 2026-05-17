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
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

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
        "## Transcript",
        "",
    ]

    for index, turn in enumerate(transcript, start=1):
        lines.extend([f"### {index}. {turn.role}", "", turn.content, ""])

    path.write_text("\n".join(lines), encoding="utf-8")

