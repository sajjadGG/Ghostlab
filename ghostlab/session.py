"""Run an agent headlessly: one turn, or a conversation that resumes its session.

A :class:`Session` owns one box (local or OpenShell) for its lifetime, so a
multi-turn conversation keeps both the harness session and the workspace
edits between turns. Every turn leaves a trace on disk::

    <run_dir>/turn-1/prompt.txt  stdout.txt  stderr.txt  answer.md  result.json  files/

and returns a :class:`Result`. Failures are classified rather than raised —
``timed_out``, ``model_unavailable``, ``sandbox_error``, ``harness_error``,
``agent_error`` — so a caller can tell "the agent got it wrong" from "the
infrastructure broke".
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from ._util import extract_json, read_json, utc_now, write_json, write_text
from .harnesses import HARNESSES, Invocation
from .sandbox import Box, Completed, LocalBox, OpenShellBox, SandboxError

if TYPE_CHECKING:
    from .agent import Agent

COMPLETED = "completed"
STATUSES = (COMPLETED, "agent_error", "timed_out", "model_unavailable", "sandbox_error",
            "harness_error")

# Provider-side failures a harness reports on stderr or in-stream.
_MODEL_OUTAGE = (
    "model not found", "unauthorized", "quota", "rate limit", "usage limit",
    "authentication failed", "no auth", "provider error", "overloaded",
    "service unavailable", "invalid api key", "model is not supported",
)


class AgentError(RuntimeError):
    """An agent turn did not complete; ``.result`` holds the full trace."""

    def __init__(self, result: "Result") -> None:
        self.result = result
        super().__init__(f"agent {result.status}: {result.error or 'no detail'} ({result.dir})")


@dataclass
class Result:
    status: str
    answer: str
    dir: Path
    agent: str = ""
    harness: str = ""
    model: str = ""
    turn: int = 1
    session_id: str = ""
    exit_code: int = 0
    duration: float = 0.0
    error: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    builtin_calls: list[dict[str, Any]] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    started_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == COMPLETED

    def check(self) -> "Result":
        """Return self, or raise :class:`AgentError` if the turn did not complete."""
        if not self.ok:
            raise AgentError(self)
        return self

    def json(self) -> Any:
        """The JSON value in the answer (tolerates prose and code fences)."""
        return extract_json(self.answer)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "dir": str(self.dir)}

    @classmethod
    def load(cls, directory: Path) -> "Result":
        data = read_json(Path(directory) / "result.json")
        return cls(**{**data, "dir": Path(data["dir"])})


def _status(done: Completed, errors: Sequence[str]) -> str:
    if done.timed_out:
        return "timed_out"
    if done.exit_code == 127 and "not found" in done.stderr:
        return "harness_error"
    if done.exit_code == 0 and not errors:
        return COMPLETED
    text = " ".join([done.stderr, *errors]).lower()
    return "model_unavailable" if any(s in text for s in _MODEL_OUTAGE) else "agent_error"


def _error(status: str, done: Completed, errors: Sequence[str]) -> str:
    if status == COMPLETED:
        return ""
    if status == "timed_out":
        return "the harness did not finish before the timeout"
    if errors:
        return "; ".join(errors)[:500]
    lines = [line for line in done.stderr.strip().splitlines() if line.strip()]
    return lines[-1][:500] if lines else f"exit code {done.exit_code}"


class Session:
    """A live conversation with one agent. Use as a context manager."""

    def __init__(self, agent: "Agent", run_dir: Path) -> None:
        self.agent = agent
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.harness = HARNESSES[agent.harness]
        self.sandboxed = agent.sandbox.backend == "openshell"
        self.session_id = ""
        self.turns = 0
        self._base: Invocation | None = None
        self.box: Box = self._make_box()

    def _make_box(self) -> Box:
        agent = self.agent
        if not self.sandboxed:
            return LocalBox(agent.sandbox, agent.workspace, self.run_dir)
        egress = self.harness.egress(agent) if agent.sandbox.policy is None else None
        return OpenShellBox(
            agent.sandbox, agent.workspace, self.run_dir,
            mcp_commands=[m.command for m in agent.mcps if m.transport == "stdio"],
            agent_files=self.harness.agent_files(agent), egress=egress, name=agent.name,
        )

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.box.close()
        except SandboxError:
            pass  # teardown must never mask the turn's own outcome

    def send(self, prompt: str, *, collect: Sequence[str] = ()) -> Result:
        """Send one message; the first turn carries the agent's instructions and skills."""
        if not prompt.strip():
            raise ValueError("the prompt is empty")
        self.turns += 1
        turn_dir = self.run_dir / f"turn-{self.turns}"
        text = self.harness.prompt(self.agent, prompt) if self.turns == 1 else prompt
        write_text(turn_dir / "prompt.txt", text + "\n")
        result = Result(
            status="harness_error", answer="", dir=turn_dir, agent=self.agent.name,
            harness=self.agent.harness, model=self.agent.model, turn=self.turns,
            started_at=utc_now(),
        )
        clock = time.monotonic()
        done = Completed(125, "", "")
        try:
            self.box.start()
            if self._base is None:
                self._base = self.harness.invocation(
                    self.agent, workdir=self.box.workdir, inside=self.box.inside,
                    sandboxed=self.sandboxed,
                )
            invocation = (self.harness.resume(self._base, self.session_id, self.sandboxed)
                          if self.session_id else self._base)
            env = {**invocation.env,
                   **{k: os.path.expandvars(v) for k, v in self.agent.env.items()}}
            argv = invocation.argv if invocation.stdin else [*invocation.argv, text]
            done = self.box.run(argv, stdin=text if invocation.stdin else None, env=env,
                                timeout=self.agent.timeout)
            parsed = self.harness.parse(invocation, done.stdout, self.agent)
            self.session_id = self.session_id or parsed.session_id
            result.status = _status(done, parsed.errors)
            result.error = _error(result.status, done, parsed.errors)
            result.answer = parsed.message
            result.tool_calls, result.builtin_calls = parsed.tool_calls, parsed.builtin_calls
            for path in collect:
                try:
                    self.box.fetch(path, turn_dir / "files" / path)
                    result.files.append(path)
                except SandboxError as exc:
                    result.error = (result.error + f"; could not collect {path}: {exc.detail}")[:500]
        except SandboxError as exc:
            result.status, result.error = "sandbox_error", str(exc)[:500]
        result.session_id = self.session_id
        result.exit_code = done.exit_code
        result.duration = round(time.monotonic() - clock, 2)
        write_text(turn_dir / "stdout.txt", done.stdout)
        write_text(turn_dir / "stderr.txt", done.stderr)
        write_text(turn_dir / "answer.md", result.answer + ("\n" if result.answer else ""))
        write_json(turn_dir / "result.json", result.to_dict())
        return result
