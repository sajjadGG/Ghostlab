from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from .config import RunnerConfig

# Known host noise that should never be treated as conversational content.
# Matched line-by-line and stripped from the message passed to the other agent.
_NOISE_PATTERNS = [
    re.compile(r"^\s*mcp:\s+\S+/\S+\s+(started|\(completed\)|\(failed\))\s*$"),
    re.compile(r"reconnecting\.\.\.", re.IGNORECASE),
    re.compile(r"failed to connect to websocket", re.IGNORECASE),
    re.compile(r"exceeded retry limit", re.IGNORECASE),
    re.compile(r"^\s*\[\d{4}-\d{2}-\d{2}T.*\]\s", ),  # timestamped log lines
    re.compile(r"cf-ray:", re.IGNORECASE),
    re.compile(r"^\s*tokens used", re.IGNORECASE),
]


def redact_host_noise(text: str) -> str:
    """Drop known agent-host noise lines so they aren't seen as conversation."""
    kept = [
        line
        for line in text.splitlines()
        if not any(pattern.search(line) for pattern in _NOISE_PATTERNS)
    ]
    return "\n".join(kept).strip()


@dataclass(frozen=True)
class RunnerResult:
    output: str
    exit_code: int
    timed_out: bool = False
    stderr: str = ""


class AgentRunner:
    def run_turn(self, prompt: str) -> RunnerResult:
        raise NotImplementedError


class MockRunner(AgentRunner):
    def __init__(self, name: str) -> None:
        self.name = name
        self.turn_count = 0

    def run_turn(self, prompt: str) -> RunnerResult:
        self.turn_count += 1
        if self.name == "user" and self.turn_count > 2:
            return RunnerResult(output="REHEARSAL_DONE", exit_code=0)
        return RunnerResult(
            output=f"[mock:{self.name}:turn-{self.turn_count}] Received prompt with {len(prompt)} chars.",
            exit_code=0,
        )


class ProcessRunner(AgentRunner):
    """Runs one fresh process per turn and sends the prompt on stdin."""

    def __init__(self, config: RunnerConfig) -> None:
        if not config.command:
            raise ValueError("Process runner requires a non-empty command")
        self.config = config

    def run_turn(self, prompt: str) -> RunnerResult:
        env = os.environ.copy()
        env.update(self.config.env)
        command = list(self.config.command)
        input_text = prompt

        if self.config.prompt_mode == "append-arg":
            command.append(prompt)
            input_text = None
        elif self.config.prompt_mode == "replace-placeholder":
            command = [part.replace("{prompt}", prompt) for part in command]
            input_text = None
        elif self.config.prompt_mode != "stdin":
            return RunnerResult(
                output=f"Unsupported prompt_mode: {self.config.prompt_mode}",
                exit_code=2,
            )

        try:
            completed = subprocess.run(
                command,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Keep streams separate even on timeout so stderr never pollutes the
            # conversational message handed to the other agent.
            return RunnerResult(
                output=(exc.stdout or "").strip(),
                exit_code=124,
                timed_out=True,
                stderr=(exc.stderr or "").strip(),
            )

        return RunnerResult(
            output=completed.stdout.strip(),
            exit_code=completed.returncode,
            stderr=completed.stderr.strip(),
        )


def create_runner(config: RunnerConfig, name: str) -> AgentRunner:
    if config.kind == "mock":
        return MockRunner(name)
    if config.kind == "process":
        return ProcessRunner(config)
    raise ValueError(f"Unsupported runner kind: {config.kind}")
