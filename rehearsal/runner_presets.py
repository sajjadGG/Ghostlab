"""Build codex runner configs on the fly for a given MCP target.

The UI lets a user point at any MCP by URL, so the agent-under-test runner can't
be a static JSON file with a hard-coded server. These helpers synthesize the
`codex exec` command (with the target injected via `-c mcp_servers.<id>...`) for
the AUT, and a plain codex command for the user emulator.
"""
from __future__ import annotations

import json
import re
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


_BEARER_ENV_RE = re.compile(r"Bearer\s+\$\{(\w+)\}")
_ENV_REF_RE = re.compile(r"\$\{?\w+\}?")


def codex_mcp_overrides(mcps: "list[dict[str, object]]") -> list[str]:
    """`-c mcp_servers.<id>...` overrides that attach every MCP to codex.

    Codex wants `command` as a string and `args` as a list; a list-valued
    `command` makes it refuse to load its configuration at all. Bearer-token
    auth written as ``Bearer ${VAR}`` maps to `bearer_token_env_var`; other
    literal headers become `http_headers`, and env-referencing ones map to
    `env_http_headers` so the secret is read inside codex, not baked in.
    """
    args: list[str] = []
    for entry in mcps:
        server_id = str(entry.get("id") or "mcp")
        transport = str(entry.get("transport") or "streamable-http")
        connection = dict(entry.get("connection") or {})  # type: ignore[arg-type]
        prefix = f"mcp_servers.{server_id}"
        if transport == "stdio":
            raw = connection.get("command") or []
            parts = [raw] if isinstance(raw, str) else list(raw)
            parts = [str(part) for part in [*parts, *(connection.get("args") or [])]]
            if not parts:
                raise ValueError(f"stdio MCP {server_id!r} has no command")
            args += ["-c", f"{prefix}.command={json.dumps(parts[0])}"]
            args += ["-c", f"{prefix}.args={json.dumps(parts[1:])}"]
            for key, value in dict(connection.get("env") or {}).items():
                args += ["-c", f"{prefix}.env.{key}={json.dumps(str(value))}"]
            continue
        if transport not in ("sse", "streamable-http", "http"):
            raise ValueError(f"Unsupported transport for codex runner: {transport}")
        args += ["-c", f"{prefix}.url={json.dumps(str(connection.get('url', '')))}"]
        for name, value in dict(connection.get("headers") or {}).items():
            text = str(value)
            bearer = _BEARER_ENV_RE.search(text)
            if bearer and str(name).lower() == "authorization":
                args += ["-c", f"{prefix}.bearer_token_env_var={json.dumps(bearer.group(1))}"]
            elif _ENV_REF_RE.fullmatch(text.strip()):
                variable = text.strip().strip("${}")
                args += ["-c", f"{prefix}.env_http_headers.{name}={json.dumps(variable)}"]
            elif not _ENV_REF_RE.search(text):
                args += ["-c", f"{prefix}.http_headers.{name}={json.dumps(text)}"]
    return args


def _mcp_config_args(target: TargetConfig) -> list[str]:
    """`-c mcp_servers.<id>...` overrides that point codex at the target."""
    return codex_mcp_overrides([{
        "id": target.id, "transport": target.transport, "connection": target.connection,
    }])


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
    agent: str = "",
) -> RunnerConfig:
    """User-emulator runner: opencode with no MCP at all, plain-text output.

    The emulated human must never share the agent-under-test's MCP config — that
    would collapse the dual-agent premise into one agent talking to itself.
    """
    write_opencode_project(project_dir, None)
    return RunnerConfig(
        kind="process",
        command=_opencode_command(opencode_bin, model, project_dir, agent),
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
