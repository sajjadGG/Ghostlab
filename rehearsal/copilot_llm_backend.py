"""GitHub Copilot CLI as an LLM backend for Ghostlab's generation stages.

Ghostlab already drives Copilot as an *agent runner* (:mod:`rehearsal.copilot_backend`
builds a ``RunnerConfig`` for the dual-agent loop). This module covers the other
half: Copilot as the plain text/JSON generator behind capability profiles,
personas, scenarios, datasets, judging, and critique.

That gap mattered in practice. Copilot is the credential most developers already
have, and reaching it previously meant routing through OpenCode
(``github-copilot/...`` model strings) — an extra binary, an extra config format,
and an extra failure surface for something the Copilot CLI does natively.

Three differences from codex drive the design:

* Copilot has no ``--output-schema``. The JSON Schema is embedded in the prompt
  and the reply parsed defensively, the same way :mod:`rehearsal.opencode_backend`
  does it.
* Copilot emits newline-delimited events on stdout; the answer is the ``content``
  of the final ``assistant.message`` event.
* Generation must not touch the host, so every built-in tool and MCP server is
  sealed off and the process runs in a scratch directory with custom
  instructions disabled.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm_backend import LlmBackendError

# Copilot's own error line when --model names a model the account cannot use.
# Matched exactly: the phrase "is not available" also occurs inside the system
# prompt that Copilot echoes back in its message-snapshot events.
_MODEL_UNAVAILABLE = "from --model flag is not available"

# Sealed generation: no shell, no filesystem writes, no network fetches. Copilot
# ignores names it does not recognize, so listing retired tools is harmless and
# keeps the seal intact across CLI versions.
_SEALED_TOOLS = (
    "shell", "bash", "write", "edit", "create", "str_replace",
    "view", "read", "glob", "grep", "fetch", "task",
)

DEFAULT_COPILOT_MODEL = ""  # empty => let the Copilot CLI pick its default


class CopilotLlmError(LlmBackendError):
    """Raised when the Copilot backend cannot run or returns unusable output."""


def resolve_copilot_llm_bin(override: str = "") -> str:
    """Locate the copilot binary: explicit path, then $GHOSTLAB_COPILOT_BIN, then PATH."""
    candidate = override or os.environ.get("GHOSTLAB_COPILOT_BIN") or os.environ.get(
        "REHEARSAL_COPILOT_BIN"
    )
    if candidate:
        path = Path(candidate).expanduser()
        if path.is_file():
            return str(path)
        found = shutil.which(candidate)
        if found:
            return found
        raise CopilotLlmError(f"copilot executable not found: {candidate}")
    found = shutil.which("copilot")
    if not found:
        raise CopilotLlmError(
            "GitHub Copilot CLI not found. Install it from "
            "https://docs.github.com/copilot/how-tos/copilot-cli, put it on PATH, "
            "or set GHOSTLAB_COPILOT_BIN."
        )
    return found


def collect_text(stream_text: str) -> str:
    """Return the assistant reply from a ``copilot --output-format json`` stream.

    Non-JSON lines (banners, stray logs) are skipped so the parse degrades
    gracefully rather than throwing on cosmetic output changes. When a run takes
    several turns, the last ``assistant.message`` is the final answer.
    """
    reply = ""
    for line in stream_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant.message":
            continue
        content = (event.get("data") or {}).get("content")
        if content:
            reply = str(content)
    return reply.strip()


def extract_json(text: str) -> Any:
    """Pull a JSON value out of a reply that may wrap it in prose or code fences."""
    candidate = text.strip()
    if not candidate:
        raise CopilotLlmError("copilot produced an empty reply")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    if "```" in candidate:
        for block in candidate.split("```")[1:]:
            body = block.split("\n", 1)[1] if "\n" in block else block
            body = body.strip()
            if not body:
                continue
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                continue

    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise CopilotLlmError(f"copilot output was not valid JSON:\n{candidate[:2000]}")


@dataclass(frozen=True)
class CopilotUsage:
    """What one Copilot call consumed, as reported by the CLI itself.

    Copilot bills in AI units (``nanoAiu``) and in premium requests, and the two
    are not proportional across models — a frontier model can cost ~25x the AI
    units of a small one but ~45x the premium requests. Callers doing
    accuracy-vs-cost analysis need the CLI's own numbers rather than a token
    estimate, so they are surfaced instead of discarded.

    Every field is best-effort: the CLI omits the checkpoint event on some paths,
    in which case the values stay at zero and ``reported`` is False.
    """

    nano_aiu: int = 0
    premium_requests: float = 0.0
    model: str = ""
    reported: bool = False

    @property
    def aiu(self) -> float:
        """AI units consumed (``nano_aiu`` scaled to whole units)."""
        return self.nano_aiu / 1e9


def collect_usage(stream_text: str) -> CopilotUsage:
    """Read the final ``session.usage_checkpoint`` from a Copilot event stream.

    Each backend call is its own Copilot session, so the last checkpoint's
    cumulative totals are that call's cost.
    """
    usage = CopilotUsage()
    for line in stream_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "session.usage_checkpoint":
            usage = CopilotUsage(
                nano_aiu=int(data.get("totalNanoAiu") or 0),
                premium_requests=float(data.get("totalPremiumRequests") or 0.0),
                model=usage.model,
                reported=True,
            )
        elif kind == "assistant.message" and data.get("model"):
            usage = CopilotUsage(
                nano_aiu=usage.nano_aiu,
                premium_requests=usage.premium_requests,
                model=str(data.get("model")),
                reported=usage.reported,
            )
    return usage


def _schema_prompt(prompt: str, schema: dict[str, Any]) -> str:
    """Embed the output contract in the prompt, since copilot has no schema flag."""
    return (
        f"{prompt}\n\n"
        "---\n"
        "Respond with a single JSON value that validates against this JSON Schema.\n"
        "Output ONLY the JSON. No prose, no explanation, no markdown code fences.\n\n"
        f"JSON Schema:\n{json.dumps(schema, indent=2)}\n"
    )


@dataclass(frozen=True)
class CopilotLlmBackend:
    """Drop-in alternative to :class:`~rehearsal.codex_backend.CodexBackend`."""

    bin_path: str = ""
    model: str = ""  # empty => the Copilot CLI default
    timeout_seconds: int = 600
    sandbox: dict[str, Any] | None = None
    disabled_mcp_servers: tuple[str, ...] = ()

    def _bin(self) -> str:
        return self.bin_path or resolve_copilot_llm_bin()

    def _model(self) -> str:
        return self.model or DEFAULT_COPILOT_MODEL

    def build_command(self) -> list[str]:
        """The sealed, non-interactive Copilot invocation used for generation.

        ``--prompt`` is last so callers append the prompt text as the final arg.
        """
        command = [
            self._bin(),
            "--output-format", "json",
            "--stream", "off",
            "--no-color",
            "--no-remote",
            "--no-auto-update",
            "--no-ask-user",
            "--no-custom-instructions",
            "--disable-builtin-mcps",
            "--allow-all-tools",
            f"--excluded-tools={','.join(_SEALED_TOOLS)}",
        ]
        for server in self.disabled_mcp_servers:
            command.extend(["--disable-mcp-server", str(server)])
        model = self._model()
        if model:
            command.extend(["--model", model])
        command.append("--prompt")
        return command

    def generate_text(self, prompt: str) -> str:
        """Run copilot once and return the assistant's reply as plain text."""
        return self.generate_text_with_usage(prompt)[0]

    def generate_text_with_usage(self, prompt: str) -> tuple[str, CopilotUsage]:
        """Run copilot once and return ``(reply, usage)``.

        The usage half is what makes cost-aware callers possible — budgeting a
        sweep, or plotting accuracy against spend — without re-deriving prices
        from token counts the CLI never exposes.
        """
        command = [*self.build_command(), prompt]
        # A scratch cwd keeps copilot from reading the caller's repo or writing
        # session state into it.
        with tempfile.TemporaryDirectory() as tmp:
            try:
                completed = subprocess.run(
                    command,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds,
                    check=False,
                    cwd=tmp,
                )
            except subprocess.TimeoutExpired as exc:
                raise CopilotLlmError(
                    f"copilot timed out after {self.timeout_seconds}s"
                ) from exc

        stream = completed.stdout or ""
        detail = (completed.stderr or "").strip()
        if _MODEL_UNAVAILABLE in stream or _MODEL_UNAVAILABLE in detail:
            raise CopilotLlmError(
                f"copilot model {self._model()!r} is not available on this account"
            )
        if completed.returncode != 0:
            raise CopilotLlmError(
                f"copilot exited {completed.returncode}:\n{(detail or stream).strip()[-2000:]}"
            )
        reply = collect_text(stream)
        if not reply:
            raise CopilotLlmError(
                "copilot produced no assistant message"
                + (f":\n{detail[-1000:]}" if detail else "")
            )
        return reply, collect_usage(stream)

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> Any:
        """Run copilot and return the parsed JSON value it replied with."""
        return extract_json(self.generate_text(_schema_prompt(prompt, schema)))
