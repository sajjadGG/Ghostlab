"""Build codex runner configs on the fly for a given MCP target.

The UI lets a user point at any MCP by URL, so the agent-under-test runner can't
be a static JSON file with a hard-coded server. These helpers synthesize the
`codex exec` command (with the target injected via `-c mcp_servers.<id>...`) for
the AUT, and a plain codex command for the user emulator.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .codex_backend import CodexError, resolve_codex_bin
from .config import RunnerConfig, TargetConfig, expand_env


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


def _opencode_bin(override: str = "") -> str:
    """Resolve the opencode executable, falling back to bare 'opencode' on PATH."""
    from .opencode_backend import OpencodeError, resolve_opencode_bin

    if override:
        return override
    try:
        return resolve_opencode_bin()
    except OpencodeError:
        return "opencode"


def opencode_project_config(
    target: TargetConfig | None,
    runtime: "dict[str, object] | None" = None,
    skills: "list[dict[str, object]] | None" = None,
    *,
    path_map: "Callable[[object], str] | None" = None,
) -> dict[str, object]:
    """Build the `opencode.json` a runner session uses.

    With no ``runtime`` this is the MCP-only shorthand: exactly one MCP — the
    target — and nothing else, with side-effecting built-ins denied so the agent
    cannot reach around the capability under evaluation and satisfy the user
    some other way. A configured agent passes its full runtime instead, and
    :mod:`rehearsal.opencode_config` renders the complete surface.
    """
    from .opencode_config import build_project_config

    mcps = []
    if target is not None:
        mcps.append({
            "id": target.id,
            "transport": target.transport,
            "connection": target.connection,
        })
    return build_project_config(runtime, mcps, path_map=path_map, skills=skills)


def write_opencode_project(
    directory: Path,
    target: TargetConfig | None,
    runtime: "dict[str, object] | None" = None,
    skills: "list[dict[str, object]] | None" = None,
    *,
    path_map: "Callable[[object], str] | None" = None,
) -> Path:
    """Materialize an opencode project dir whose config wires up the agent."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "opencode.json"
    config = opencode_project_config(target, runtime, skills, path_map=path_map)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def _opencode_command(
    bin_path: str, model: str, project_dir: Path, agent: str = "",
    remote_dir: str = "",
) -> list[str]:
    from .opencode_backend import DEFAULT_OPENCODE_MODEL

    # Always pin a model. Falling through to opencode's own default silently
    # picks whatever that install is configured for, which may not be available
    # on the user's provider — and the failure only shows up mid-conversation.
    command = [
        _opencode_bin(bin_path), "run", "--format", "json", "--log-level", "ERROR",
        "--model", model or DEFAULT_OPENCODE_MODEL,
    ]
    if agent:
        command += ["--agent", agent]
    return command + ["--dir", remote_dir or str(project_dir)]


def opencode_aut_runner(
    target: TargetConfig | None,
    project_dir: Path,
    *,
    timeout_seconds: int = 600,
    opencode_bin: str = "",
    model: str = "",
    runtime: "dict[str, object] | None" = None,
    skills: "list[dict[str, object]] | None" = None,
    path_map: "Callable[[object], str] | None" = None,
    remote_dir: str = "",
) -> RunnerConfig:
    """AUT runner: opencode configured as the agent under test.

    ``runtime`` carries the agent's full OpenCode configuration (model,
    instructions, skills, subagents, permissions, extra MCPs). ``path_map`` and
    ``remote_dir`` rewrite host paths for a sandboxed run, where the project
    directory is uploaded and OpenCode executes inside the container.
    """
    runtime = dict(runtime or {})
    write_opencode_project(project_dir, target, runtime, skills, path_map=path_map)
    return RunnerConfig(
        kind="process",
        command=_opencode_command(
            opencode_bin,
            model or str(runtime.get("model") or ""),
            project_dir,
            str(runtime.get("default_agent") or ""),
            remote_dir,
        ),
        timeout_seconds=timeout_seconds,
        prompt_mode="stdin",
        parser="opencode-json",
    )


def opencode_user_runner(
    project_dir: Path,
    *,
    timeout_seconds: int = 600,
    opencode_bin: str = "",
    model: str = "",
) -> RunnerConfig:
    """User-emulator runner: opencode with no MCP at all, plain-text output.

    The emulated human must never share the agent-under-test's MCP config — that
    would collapse the dual-agent premise into one agent talking to itself.
    """
    write_opencode_project(project_dir, None)
    return RunnerConfig(
        kind="process",
        command=_opencode_command(opencode_bin, model, project_dir),
        timeout_seconds=timeout_seconds,
        prompt_mode="stdin",
        parser="opencode-text",
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
