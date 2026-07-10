"""Build codex runner configs on the fly for a given MCP target.

The UI lets a user point at any MCP by URL, so the agent-under-test runner can't
be a static JSON file with a hard-coded server. These helpers synthesize the
`codex exec` command (with the target injected via `-c mcp_servers.<id>...`) for
the AUT, and a plain codex command for the user emulator.
"""
from __future__ import annotations

import json
from pathlib import Path

from .codex_backend import CodexError, resolve_codex_bin
from .config import RunnerConfig, TargetConfig


def _codex_bin(override: str = "") -> str:
    """Resolve the codex executable, falling back to bare 'codex' on PATH."""
    if override:
        return override
    try:
        return resolve_codex_bin()
    except CodexError:
        return "codex"


def _codex_base(codex_bin: str) -> list[str]:
    return [codex_bin, "--sandbox", "read-only", "-a", "never"]


def _mcp_config_args(target: TargetConfig) -> list[str]:
    """`-c mcp_servers.<id>...` overrides that point codex at the target."""
    conn = target.connection
    sid = target.id
    if target.transport in ("sse", "streamable-http", "http"):
        url = conn.get("url", "")
        return ["-c", f'mcp_servers.{sid}.url="{url}"']
    if target.transport == "stdio":
        command = conn.get("command", "")
        args = conn.get("args", [])
        return [
            "-c",
            f'mcp_servers.{sid}.command="{command}"',
            "-c",
            f"mcp_servers.{sid}.args={json.dumps(args)}",
        ]
    raise ValueError(f"Unsupported transport for codex runner: {target.transport}")


def _model_args(model: str) -> list[str]:
    return ["-m", model] if model else []


def codex_aut_runner(
    target: TargetConfig,
    *,
    session: bool = True,
    timeout_seconds: int = 600,
    codex_bin: str = "",
    model: str = "",
) -> RunnerConfig:
    """AUT runner: codex with the target MCP injected, JSON output for capture."""
    command = [
        *_codex_base(_codex_bin(codex_bin)),
        *_model_args(model),
        *_mcp_config_args(target),
        "exec",
        "--json",
        "--skip-git-repo-check",
        "-",
    ]
    return RunnerConfig(
        kind="codex-session" if session else "process",
        command=command,
        timeout_seconds=timeout_seconds,
        prompt_mode="stdin",
        parser="codex-json",
    )


def codex_user_runner(timeout_seconds: int = 600, codex_bin: str = "", model: str = "") -> RunnerConfig:
    """User-emulator runner: plain codex, no MCP, plain-text output."""
    command = [
        *_codex_base(_codex_bin(codex_bin)),
        *_model_args(model),
        "exec",
        "--skip-git-repo-check",
        "-",
    ]
    return RunnerConfig(
        kind="process",
        command=command,
        timeout_seconds=timeout_seconds,
        prompt_mode="stdin",
        parser="text",
    )


def mock_runner() -> RunnerConfig:
    return RunnerConfig(kind="mock")


def write_runner_config(config: RunnerConfig, path: Path) -> Path:
    payload = {
        "kind": config.kind,
        "command": config.command,
        "env": config.env,
        "timeout_seconds": config.timeout_seconds,
        "prompt_mode": config.prompt_mode,
        "parser": config.parser,
        "sandbox": config.sandbox,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
