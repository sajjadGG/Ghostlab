"""Selects which LLM CLI drives Ghostlab's generation, judging, and critique.

Ghostlab shells out to a coding-agent CLI rather than calling a model API
directly, so "which backend" is a real choice a user has to make: codex needs a
working ChatGPT/Codex plan, copilot uses the GitHub Copilot subscription
directly, and opencode can source models from GitHub Copilot, Azure, and others
the user has already authenticated.

Every backend exposes the same ``generate_json(prompt, schema)`` surface, so the
generation stages stay backend-agnostic.
"""
from __future__ import annotations

import os
from typing import Any

BACKENDS = ("codex", "copilot", "opencode")
DEFAULT_BACKEND = "codex"


class LlmBackendError(RuntimeError):
    """Base for every backend failure, so callers can stay backend-agnostic.

    ``CodexError``, ``CopilotLlmError``, and ``OpencodeError`` all derive from
    this; catch this type to handle "the generation backend could not produce
    usable output" without caring which CLI was configured.
    """


class BackendError(LlmBackendError):
    """Raised when the requested LLM backend is unknown or unusable."""


def resolve_backend_kind(explicit: str = "", spec_value: str = "") -> str:
    """Pick the backend: explicit flag > spec/job setting > env var > default."""
    for candidate in (explicit, spec_value, os.environ.get("GHOSTLAB_LLM_BACKEND", "")):
        value = (candidate or "").strip().lower()
        if not value:
            continue
        if value not in BACKENDS:
            raise BackendError(
                f"Unknown LLM backend {value!r}; expected one of {', '.join(BACKENDS)}"
            )
        return value
    return DEFAULT_BACKEND


def backend_error_types() -> tuple[type[Exception], ...]:
    """Every failure type a caller must catch to be backend-agnostic."""
    from .codex_backend import CodexError
    from .copilot_llm_backend import CopilotLlmError
    from .opencode_backend import OpencodeError

    return (CodexError, CopilotLlmError, OpencodeError)


def create_backend(
    kind: str = "",
    *,
    bin_path: str = "",
    model: str = "",
    timeout_seconds: int = 600,
    sandbox: "dict[str, Any] | None" = None,
    spec_value: str = "",
) -> Any:
    """Build the configured backend. Both share ``generate_json(prompt, schema)``."""
    resolved = resolve_backend_kind(kind, spec_value)
    if resolved == "copilot":
        from .copilot_llm_backend import CopilotLlmBackend

        return CopilotLlmBackend(
            bin_path=bin_path, model=model, timeout_seconds=timeout_seconds,
            sandbox=sandbox,
        )
    if resolved == "opencode":
        from .opencode_backend import OpencodeBackend

        return OpencodeBackend(
            bin_path=bin_path, model=model, timeout_seconds=timeout_seconds,
            sandbox=sandbox,
        )
    from .codex_backend import CodexBackend

    return CodexBackend(
        bin_path=bin_path, model=model, timeout_seconds=timeout_seconds, sandbox=sandbox,
    )


_BACKEND_KINDS = {
    "CodexBackend": "codex",
    "CopilotLlmBackend": "copilot",
    "OpencodeBackend": "opencode",
}


def backend_label(backend: Any) -> str:
    """Human-readable '<kind> (<binary>) [model]' for progress output."""
    kind = _BACKEND_KINDS.get(type(backend).__name__, "codex")
    try:
        binary = backend._bin()
    except Exception:  # noqa: BLE001 - label must never break a run
        binary = kind
    model = getattr(backend, "model", "")
    if not model and kind != "codex":
        # opencode and copilot resolve an implicit default when model is unset.
        try:
            model = backend._model()
        except Exception:  # noqa: BLE001 - label must never break a run
            model = ""
    return f"{kind} ({binary})" + (f" model={model}" if model else "")
