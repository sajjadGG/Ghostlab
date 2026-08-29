from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

from .config import RunnerConfig

GHOSTLAB_ORIGINATOR = "ghostlab"
CODEX_ORIGINATOR_ENV = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"
# The first six UUID bytes spell "ghostl"; the UUIDv4 version and variant bits
# remain random and valid because they live in later bytes.
GHOSTLAB_COPILOT_SESSION_ID_PREFIX = "67686f73-746c-"


def new_copilot_session_id() -> str:
    value = bytearray(uuid.uuid4().bytes)
    value[:6] = b"ghostl"
    return str(uuid.UUID(bytes=bytes(value)))


def with_ghostlab_provenance(config: RunnerConfig) -> RunnerConfig:
    if not _is_codex_runner(config):
        return config
    return replace(
        config,
        env={
            **config.env,
            CODEX_ORIGINATOR_ENV: GHOSTLAB_ORIGINATOR,
        },
    )


def _is_codex_runner(config: RunnerConfig) -> bool:
    executable = Path(config.command[0]).name.lower() if config.command else ""
    return (
        config.kind == "codex-session"
        or config.parser == "codex-json"
        or executable in {"codex", "codex.exe"}
    )
