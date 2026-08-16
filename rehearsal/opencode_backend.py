"""OpenCode as an alternative LLM backend for Ghostlab's generation stages.

Mirrors :mod:`rehearsal.codex_backend` so every generation stage (capability
profiles, personas, scenarios, datasets, judge, critique) can run on OpenCode —
most usefully with GitHub Copilot as the model source, which is what a developer
already has authenticated when a Codex/ChatGPT plan is unavailable or out of
quota.

Two differences from codex drive the design:

* OpenCode has no ``--output-schema``. The JSON Schema is embedded in the prompt
  and the reply is parsed defensively (fenced blocks, prose-wrapped objects).
* OpenCode streams newline-delimited JSON events with ``--format json`` rather
  than writing a final-message file, so the answer is reassembled from the
  ``text`` parts of the stream.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .llm_backend import LlmBackendError

# Fallback locations checked when `opencode` is not on PATH and the env var is unset.
_DEFAULT_OPENCODE_PATHS = [
    str(Path.home() / ".opencode" / "bin" / "opencode"),
    "/usr/local/bin/opencode",
    "/opt/homebrew/bin/opencode",
]

# Copilot is the intended default source: it is the credential a developer is
# most likely to already have when codex is unusable.
DEFAULT_OPENCODE_MODEL = "github-copilot/claude-sonnet-4.5"


class OpencodeError(LlmBackendError):
    """Raised when the opencode backend cannot run or returns unusable output."""


def resolve_opencode_bin() -> str:
    """Locate the opencode binary: $GHOSTLAB_OPENCODE_BIN, then PATH, then defaults."""
    override = os.environ.get("GHOSTLAB_OPENCODE_BIN") or os.environ.get(
        "REHEARSAL_OPENCODE_BIN"
    )
    if override:
        if Path(override).exists():
            return override
        raise OpencodeError(f"GHOSTLAB_OPENCODE_BIN does not exist: {override}")
    found = shutil.which("opencode")
    if found:
        return found
    for candidate in _DEFAULT_OPENCODE_PATHS:
        if Path(candidate).exists():
            return candidate
    raise OpencodeError(
        "opencode binary not found. Install opencode (https://opencode.ai), put it "
        "on PATH, or set GHOSTLAB_OPENCODE_BIN."
    )


def collect_text(stream_text: str) -> str:
    """Reassemble the assistant reply from an ``opencode run --format json`` stream.

    Non-JSON lines (startup banners, stray logs) are ignored so the parse degrades
    gracefully rather than throwing on cosmetic output changes.
    """
    chunks: list[str] = []
    for line in stream_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "text":
            continue
        text = (event.get("part") or {}).get("text")
        if text:
            chunks.append(str(text))
    return "\n".join(chunks).strip()


def extract_json(text: str) -> Any:
    """Pull a JSON value out of a model reply that may wrap it in prose or fences."""
    candidate = text.strip()
    if not candidate:
        raise OpencodeError("opencode produced an empty reply")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # ```json ... ``` (or a bare ``` fence) is the most common wrapper.
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

    # Last resort: the outermost balanced {...} / [...] span in the reply.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue

    raise OpencodeError(f"opencode output was not valid JSON:\n{candidate[:2000]}")


def _schema_prompt(prompt: str, schema: dict[str, Any]) -> str:
    """Embed the output contract in the prompt, since opencode has no schema flag."""
    return (
        f"{prompt}\n\n"
        "---\n"
        "Respond with a single JSON value that validates against this JSON Schema.\n"
        "Output ONLY the JSON. No prose, no explanation, no markdown code fences.\n\n"
        f"JSON Schema:\n{json.dumps(schema, indent=2)}\n"
    )


# Generation must not touch the host: deny every side-effecting built-in tool.
_SEALED_CONFIG: dict[str, Any] = {
    "$schema": "https://opencode.ai/config.json",
    "permission": {"bash": "deny", "edit": "deny", "webfetch": "deny"},
}


@dataclass(frozen=True)
class OpencodeBackend:
    """Drop-in alternative to :class:`~rehearsal.codex_backend.CodexBackend`."""

    bin_path: str = ""
    model: str = ""  # empty => DEFAULT_OPENCODE_MODEL
    timeout_seconds: int = 600
    sandbox: dict[str, Any] | None = None

    def _bin(self) -> str:
        return self.bin_path or resolve_opencode_bin()

    def _model(self) -> str:
        return self.model or DEFAULT_OPENCODE_MODEL

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> Any:
        """Run opencode and return the parsed JSON object it replied with."""
        command = [
            self._bin(), "run",
            "--model", self._model(),
            "--format", "json",
            "--log-level", "ERROR",
        ]
        # A scratch project dir keeps opencode from picking up the caller's
        # AGENTS.md/opencode.json and from writing session state into the repo.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "opencode.json").write_text(
                json.dumps(_SEALED_CONFIG, indent=2), encoding="utf-8"
            )
            command += ["--dir", str(root)]
            try:
                completed = subprocess.run(
                    command,
                    input=_schema_prompt(prompt, schema),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds,
                    check=False,
                    cwd=str(root),
                )
            except subprocess.TimeoutExpired as exc:
                raise OpencodeError(
                    f"opencode timed out after {self.timeout_seconds}s"
                ) from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
            raise OpencodeError(f"opencode exited {completed.returncode}:\n{detail}")

        reply = collect_text(completed.stdout)
        if not reply:
            detail = (completed.stderr or "").strip()[-1000:]
            raise OpencodeError(
                "opencode produced no assistant text"
                + (f":\n{detail}" if detail else "")
            )
        return extract_json(reply)
