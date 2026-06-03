from __future__ import annotations

import json
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


def _exec(command: list[str], input_text: str | None, env: dict[str, str], timeout: int) -> RunnerResult:
    """Run a command once, keeping stdout and stderr separate."""
    full_env = os.environ.copy()
    full_env.update(env)
    try:
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
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


class AgentRunner:
    # Stateful runners keep one agent session alive across turns, so the
    # orchestrator should send only the new user message after the first turn.
    stateful = False

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
        command = list(self.config.command)
        input_text: str | None = prompt

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

        return _exec(command, input_text, self.config.env, self.config.timeout_seconds)


class CodexSessionRunner(AgentRunner):
    """Keeps one codex session alive across turns via `codex exec resume`.

    Turn 1 runs the base command and records the `thread_id` from the JSONL
    `thread.started` event. Later turns insert `resume <thread_id>` after `exec`
    so codex retains conversation context — the orchestrator then only needs to
    send the new user message instead of replaying the whole transcript.
    """

    stateful = True

    def __init__(self, config: RunnerConfig) -> None:
        if not config.command:
            raise ValueError("Session runner requires a non-empty command")
        if "exec" not in config.command:
            raise ValueError("codex-session command must contain 'exec'")
        self.config = config
        self.thread_id: str | None = None

    def _command_for_turn(self) -> list[str]:
        command = list(self.config.command)
        if self.thread_id:
            insert_at = command.index("exec") + 1
            command[insert_at:insert_at] = ["resume", self.thread_id]
        return command

    @staticmethod
    def _extract_thread_id(jsonl_text: str) -> str | None:
        for line in jsonl_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return str(event["thread_id"])
        return None

    def run_turn(self, prompt: str) -> RunnerResult:
        result = _exec(
            self._command_for_turn(), prompt, self.config.env, self.config.timeout_seconds
        )
        if self.thread_id is None:
            self.thread_id = self._extract_thread_id(result.output)
        return result


def create_runner(config: RunnerConfig, name: str) -> AgentRunner:
    if config.kind == "mock":
        return MockRunner(name)
    if config.kind == "process":
        return ProcessRunner(config)
    if config.kind == "codex-session":
        return CodexSessionRunner(config)
    raise ValueError(f"Unsupported runner kind: {config.kind}")
