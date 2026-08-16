"""Codex as the default LLM backend for Rehearsal's generation stages.

Wraps `codex exec` in non-interactive mode with a JSON output schema, so callers
get a parsed, schema-shaped object back. Codex is the default backend for all
data generation (capability profiles, scenarios, personas, datasets).
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

# Fallback locations checked when `codex` is not on PATH and the env var is unset.
_DEFAULT_CODEX_PATHS = [
    "/Applications/Codex.app/Contents/Resources/codex",
]


class CodexError(LlmBackendError):
    """Raised when the codex backend cannot run or returns unusable output."""


def resolve_codex_bin() -> str:
    """Locate the codex binary: $REHEARSAL_CODEX_BIN, then PATH, then app bundle."""
    override = os.environ.get("REHEARSAL_CODEX_BIN")
    if override:
        if Path(override).exists():
            return override
        raise CodexError(f"REHEARSAL_CODEX_BIN does not exist: {override}")
    found = shutil.which("codex")
    if found:
        return found
    for candidate in _DEFAULT_CODEX_PATHS:
        if Path(candidate).exists():
            return candidate
    raise CodexError(
        "codex binary not found. Install codex, put it on PATH, or set "
        "REHEARSAL_CODEX_BIN."
    )


@dataclass(frozen=True)
class CodexBackend:
    bin_path: str = ""
    model: str = ""  # empty => codex default
    timeout_seconds: int = 600
    sandbox: dict[str, Any] | None = None

    def _bin(self) -> str:
        if (self.sandbox or {}).get("backend") == "openshell":
            return str((self.sandbox or {}).get("codex_bin") or "codex")
        return self.bin_path or resolve_codex_bin()

    def generate_json(self, prompt: str, schema: dict[str, Any]) -> Any:
        """Run codex with a JSON output schema and return the parsed result."""
        if (self.sandbox or {}).get("backend") == "openshell":
            return self._generate_json_openshell(prompt, schema)
        codex = self._bin()
        with tempfile.TemporaryDirectory() as tmp:
            schema_path = Path(tmp) / "schema.json"
            out_path = Path(tmp) / "last.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")

            command = [
                codex,
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "--output-schema",
                str(schema_path),
                "-o",
                str(out_path),
                "-",
            ]
            if self.model:
                command[2:2] = ["--model", self.model]

            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise CodexError(f"codex timed out after {self.timeout_seconds}s") from exc

            if completed.returncode != 0:
                raise CodexError(
                    f"codex exited {completed.returncode}:\n{completed.stderr.strip()[-2000:]}"
                )
            if not out_path.exists() or not out_path.read_text().strip():
                raise CodexError("codex produced no output-last-message file")
            raw = out_path.read_text(encoding="utf-8").strip()

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexError(f"codex output was not valid JSON:\n{raw[:2000]}") from exc

    def _generate_json_openshell(self, prompt: str, schema: dict[str, Any]) -> Any:
        """Run generation/judging inside the configured OpenShell boundary."""
        from .sandbox import OpenShellSandbox, SandboxError, normalize_sandbox

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema_path = root / "schema.json"
            out_path = root / "last.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            config = normalize_sandbox(self.sandbox)
            config["uploads"] = [
                *list(config.get("uploads", []) or []),
                {"source": str(schema_path), "target": "/sandbox/schema.json"},
            ]
            sandbox = OpenShellSandbox(config, role="codex-backend")
            command = [
                self._bin(), "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                "--ephemeral", "--output-schema", "/sandbox/schema.json",
                "-o", "/sandbox/last.json", "-",
            ]
            if self.model:
                command[2:2] = ["--model", self.model]
            try:
                completed = sandbox.exec(
                    command, input_text=prompt, env={}, timeout=self.timeout_seconds,
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout).strip()[-2000:]
                    raise CodexError(f"sandboxed codex exited {completed.returncode}:\n{detail}")
                sandbox.download("/sandbox/last.json", out_path)
                if not out_path.exists() or not out_path.read_text(encoding="utf-8").strip():
                    raise CodexError("sandboxed codex produced no output-last-message file")
                raw = out_path.read_text(encoding="utf-8").strip()
            except SandboxError as exc:
                raise CodexError(f"OpenShell {exc.kind}: {exc.detail}") from exc
            finally:
                sandbox.close()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexError(f"sandboxed codex output was not valid JSON:\n{raw[:2000]}") from exc
