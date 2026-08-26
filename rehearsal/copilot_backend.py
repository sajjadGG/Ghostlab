"""GitHub Copilot CLI runner configuration.

Copilot CLI is the headless runner for both GitHub Copilot and VS Code custom
agents. A custom agent selected with ``--agent`` can be the same
``.github/agents/*.agent.md`` definition used from VS Code.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Iterable

from .config import RunnerConfig


class CopilotError(RuntimeError):
    """Raised when GitHub Copilot CLI cannot be configured."""


def resolve_copilot_bin(override: str = "") -> str:
    """Return an executable Copilot CLI path."""
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which(override)
        if resolved:
            return resolved
        raise CopilotError(f"copilot executable not found: {override}")
    resolved = shutil.which("copilot")
    if not resolved:
        raise CopilotError(
            "GitHub Copilot CLI not found. Install it from "
            "https://docs.github.com/copilot/how-tos/copilot-cli"
        )
    return resolved


def _strings(runtime: dict[str, Any], key: str) -> list[str]:
    value = runtime.get(key)
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"Copilot runtime '{key}' must be a string or list")
    return [str(item) for item in value]


def _append_repeated(command: list[str], flag: str, values: Iterable[str]) -> None:
    for value in values:
        command.extend([flag, value])


def _mcp_entry(entry: dict[str, Any]) -> dict[str, Any]:
    transport = str(entry.get("transport") or "streamable-http")
    connection = dict(entry.get("connection") or {})
    tools = list(entry.get("tools") or ["*"])
    if transport == "stdio":
        raw_command = connection.get("command") or []
        if isinstance(raw_command, str):
            parts = [raw_command]
        else:
            parts = [str(part) for part in raw_command]
        parts.extend(str(part) for part in connection.get("args", []) or [])
        if not parts:
            raise ValueError(f"Copilot MCP '{entry.get('id', '?')}' has no command")
        result: dict[str, Any] = {
            "type": "local",
            "command": parts[0],
            "args": parts[1:],
            "tools": tools,
        }
        environment = connection.get("env") or connection.get("environment") or {}
        if environment:
            result["env"] = {str(key): str(value) for key, value in dict(environment).items()}
        return result

    if transport not in ("http", "streamable-http", "sse"):
        raise ValueError(f"Unsupported Copilot MCP transport: {transport}")
    result = {
        "type": "sse" if transport == "sse" else "http",
        "url": str(connection.get("url") or ""),
        "tools": tools,
    }
    if not result["url"]:
        raise ValueError(f"Copilot MCP '{entry.get('id', '?')}' has no URL")
    headers = connection.get("headers") or {}
    if headers:
        result["headers"] = {str(key): str(value) for key, value in dict(headers).items()}
    return result


def build_copilot_mcp_config(mcps: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Translate normalized Ghostlab MCP inputs to Copilot's config schema."""
    servers: dict[str, Any] = {}
    for entry in mcps:
        server_id = str(entry.get("id") or "").strip()
        if not server_id:
            raise ValueError("Copilot MCP entries require an id")
        servers[server_id] = _mcp_entry(entry)
    return {"mcpServers": servers}


_PROTOCOL_FLAGS = ("-p", "--prompt", "--output-format", "--session-id", "--stream")


def _validate_extra_args(args: list[str]) -> None:
    for arg in args:
        if any(arg == flag or arg.startswith(f"{flag}=") for flag in _PROTOCOL_FLAGS):
            raise ValueError(
                f"Copilot runtime extra_args cannot override protocol flag {arg!r}"
            )


def build_copilot_command(
    runtime: dict[str, Any],
    *,
    mcps: Iterable[dict[str, Any]] = (),
    disabled_mcp_servers: Iterable[str] = (),
) -> list[str]:
    """Build a non-interactive Copilot CLI JSONL command.

    Every supported setting remains declarative in ``runtime``. ``extra_args``
    is the forward-compatible escape hatch for new Copilot CLI options, except
    for protocol flags Ghostlab must own to drive and parse each turn.
    """
    command = [
        str(runtime.get("copilot_bin") or "copilot"),
        "--output-format",
        "json",
        "--stream",
        "off",
        "--no-color",
        "--no-remote",
        "--no-auto-update",
    ]
    if runtime.get("no_ask_user", True):
        command.append("--no-ask-user")
    if runtime.get("no_custom_instructions", False):
        command.append("--no-custom-instructions")
    if runtime.get("disable_builtin_mcps", True):
        command.append("--disable-builtin-mcps")

    scalar_options = (
        ("model", "--model"),
        ("agent", "--agent"),
        ("reasoning_effort", "--effort"),
        ("context", "--context"),
        ("mode", "--mode"),
        ("log_level", "--log-level"),
        ("max_ai_credits", "--max-ai-credits"),
        ("max_autopilot_continues", "--max-autopilot-continues"),
        ("bash_env", "--bash-env"),
    )
    for key, flag in scalar_options:
        value = runtime.get(key)
        if value not in (None, ""):
            command.extend([flag, str(value)])
    working_directory = runtime.get("working_directory")
    if working_directory:
        command.extend(["-C", str(working_directory)])

    boolean_options = (
        ("allow_all", "--allow-all"),
        ("allow_all_paths", "--allow-all-paths"),
        ("allow_all_urls", "--allow-all-urls"),
        ("allow_all_mcp_server_instructions", "--allow-all-mcp-server-instructions"),
        ("enable_all_github_mcp_tools", "--enable-all-github-mcp-tools"),
        ("enable_memory", "--enable-memory"),
        ("enable_reasoning_summaries", "--enable-reasoning-summaries"),
        ("disallow_temp_dir", "--disallow-temp-dir"),
        ("experimental", "--experimental"),
    )
    for key, flag in boolean_options:
        if runtime.get(key, False):
            command.append(flag)
    if not runtime.get("allow_all") and runtime.get("allow_all_tools", True):
        command.append("--allow-all-tools")

    repeated_options = (
        ("add_dirs", "--add-dir"),
        ("allow_tools", "--allow-tool"),
        ("deny_tools", "--deny-tool"),
        ("allow_urls", "--allow-url"),
        ("deny_urls", "--deny-url"),
        ("plugin_dirs", "--plugin-dir"),
        ("add_github_mcp_tools", "--add-github-mcp-tool"),
        ("add_github_mcp_toolsets", "--add-github-mcp-toolset"),
    )
    for key, flag in repeated_options:
        _append_repeated(command, flag, _strings(runtime, key))

    available_tools = _strings(runtime, "available_tools")
    if available_tools:
        command.append(f"--available-tools={','.join(available_tools)}")
    excluded_tools = _strings(runtime, "excluded_tools")
    if excluded_tools:
        command.append(f"--excluded-tools={','.join(excluded_tools)}")
    secret_env_vars = _strings(runtime, "secret_env_vars")
    if secret_env_vars:
        command.append(f"--secret-env-vars={','.join(secret_env_vars)}")

    disabled = list(
        dict.fromkeys(
            [*_strings(runtime, "disable_mcp_servers"), *map(str, disabled_mcp_servers)]
        )
    )
    _append_repeated(command, "--disable-mcp-server", disabled)
    _append_repeated(
        command,
        "--additional-mcp-config",
        _strings(runtime, "additional_mcp_configs"),
    )
    generated_mcp = build_copilot_mcp_config(mcps)
    if generated_mcp["mcpServers"]:
        command.extend(
            [
                "--additional-mcp-config",
                json.dumps(generated_mcp, separators=(",", ":"), ensure_ascii=False),
            ]
        )

    extra_args = _strings(runtime, "extra_args")
    _validate_extra_args(extra_args)
    command.extend(extra_args)
    command.append("--prompt")
    return command


def copilot_runner(
    runtime: dict[str, Any],
    *,
    mcps: Iterable[dict[str, Any]] = (),
    disabled_mcp_servers: Iterable[str] = (),
    sandbox: dict[str, Any] | None = None,
) -> RunnerConfig:
    """Create a fully configured Copilot process/session runner."""
    kind = str(runtime.get("kind") or "copilot-session")
    if kind not in ("process", "copilot-session"):
        raise ValueError("Copilot runtime kind must be 'process' or 'copilot-session'")
    environment = runtime.get("env") or {}
    if not isinstance(environment, dict):
        raise ValueError("Copilot runtime 'env' must be an object")
    return RunnerConfig(
        kind=kind,
        command=build_copilot_command(
            runtime, mcps=mcps, disabled_mcp_servers=disabled_mcp_servers
        ),
        env={str(key): str(value) for key, value in environment.items()},
        timeout_seconds=int(runtime.get("timeout_seconds") or 600),
        prompt_mode="append-arg",
        parser="copilot-json",
        sandbox=dict(sandbox or runtime.get("sandbox") or {}),
    )
