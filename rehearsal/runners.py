from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

from .config import RunnerConfig
from .sandbox import OpenShellSandbox, SandboxError

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

    def close(self) -> None:
        """Release runner-owned resources."""


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


def _prompt_command(
    command: list[str], prompt_mode: str, prompt: str
) -> tuple[list[str], str | None] | RunnerResult:
    input_text: str | None = prompt
    if prompt_mode == "append-arg":
        command.append(prompt)
        input_text = None
    elif prompt_mode == "replace-placeholder":
        command = [part.replace("{prompt}", prompt) for part in command]
        input_text = None
    elif prompt_mode != "stdin":
        return RunnerResult(
            output=f"Unsupported prompt_mode: {prompt_mode}", exit_code=2,
        )
    return command, input_text


class OpenShellProcessRunner(AgentRunner):
    """Run every agent turn inside one policy-enforced OpenShell sandbox."""

    def __init__(self, config: RunnerConfig, name: str) -> None:
        if not config.command:
            raise ValueError("OpenShell process runner requires a non-empty command")
        self.config = config
        self.sandbox = OpenShellSandbox(config.sandbox, role=name)

    def _run(self, command: list[str], input_text: str | None) -> RunnerResult:
        try:
            result = self.sandbox.exec(
                command, input_text=input_text, env=self.config.env,
                timeout=self.config.timeout_seconds,
            )
            return RunnerResult(
                output=result.stdout.strip(), exit_code=result.returncode,
                stderr=result.stderr.strip(),
            )
        except SandboxError as exc:
            return RunnerResult(output="", exit_code=125, stderr=str(exc))

    def run_turn(self, prompt: str) -> RunnerResult:
        prepared = _prompt_command(list(self.config.command), self.config.prompt_mode, prompt)
        if isinstance(prepared, RunnerResult):
            return prepared
        return self._run(*prepared)

    def close(self) -> None:
        self.sandbox.close()


class OpenShellCodexSessionRunner(OpenShellProcessRunner):
    """Codex session resume semantics, with every turn executed in OpenShell."""

    stateful = True

    def __init__(self, config: RunnerConfig, name: str) -> None:
        super().__init__(config, name)
        if "exec" not in config.command:
            raise ValueError("codex-session command must contain 'exec'")
        self.thread_id: str | None = None

    def _command_for_turn(self) -> list[str]:
        command = list(self.config.command)
        if self.thread_id:
            insert_at = command.index("exec") + 1
            command[insert_at:insert_at] = ["resume", self.thread_id]
        return command

    def run_turn(self, prompt: str) -> RunnerResult:
        prepared = _prompt_command(self._command_for_turn(), self.config.prompt_mode, prompt)
        if isinstance(prepared, RunnerResult):
            return prepared
        result = self._run(*prepared)
        if self.thread_id is None:
            self.thread_id = CodexSessionRunner._extract_thread_id(result.output)
        return result


class OpencodeProcessRunner(ProcessRunner):
    """ProcessRunner that treats an opencode `error` event as a failed turn.

    opencode exits 0 even when the provider rejects the request (bad model,
    revoked auth, quota), leaving only an `{"type":"error"}` frame in the
    stream. Without this the orchestrator would take that frame as the agent's
    reply and feed raw JSON to the other agent as if a human had typed it.
    """

    def run_turn(self, prompt: str) -> RunnerResult:
        result = super().run_turn(prompt)
        from .tool_capture import parse_opencode_output

        errors = parse_opencode_output(result.output).get("errors") or []
        if errors and result.exit_code == 0:
            detail = "; ".join(errors)[:500]
            return RunnerResult(
                output=result.output,
                exit_code=1,
                timed_out=result.timed_out,
                stderr=(f"opencode error: {detail}\n{result.stderr}").strip(),
            )
        return result


def create_runner(config: RunnerConfig, name: str) -> AgentRunner:
    if config.kind == "mock":
        return MockRunner(name)
    if config.parser in ("opencode-json", "opencode-text"):
        return OpencodeProcessRunner(config)
    backend = str((config.sandbox or {}).get("backend", "local"))
    if backend == "openshell" and config.kind == "process":
        return OpenShellProcessRunner(config, name)
    if backend == "openshell" and config.kind == "codex-session":
        return OpenShellCodexSessionRunner(config, name)
    if backend not in ("local", "openshell"):
        raise ValueError(f"Unsupported sandbox backend: {backend}")
    if config.kind == "process":
        return ProcessRunner(config)
    if config.kind == "codex-session":
        return CodexSessionRunner(config)
    raise ValueError(f"Unsupported runner kind: {config.kind}")
