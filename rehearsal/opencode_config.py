"""Build and validate the OpenCode project config for an agent under test.

OpenCode's configuration is declarative, so Ghostlab mirrors its schema instead
of inventing a parallel vocabulary: what you can set on a real OpenCode agent is
what you can set here. This module owns the mapping from a job's
``agent.runtime`` block to the ``opencode.json`` the runner points at.

Three Ghostlab-side names are pluralized for readability and renamed on the way
out (``agents`` -> ``agent``, ``commands`` -> ``command``, ``plugins`` ->
``plugin``); everything else keeps OpenCode's own key.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .config import ConfigError, expand_env

SCHEMA_URL = "https://opencode.ai/config.json"

# Vendored from the published schema (opencode 1.4.x). Kept as a key list rather
# than a full validator: the point is to reject typos and configuration that
# OpenCode would silently ignore, not to re-implement JSON Schema.
OPENCODE_CONFIG_KEYS = frozenset({
    "$schema", "agent", "attachment", "autoshare", "autoupdate", "command",
    "compaction", "default_agent", "disabled_providers", "enabled_providers",
    "enterprise", "experimental", "formatter", "instructions", "layout",
    "logLevel", "lsp", "mcp", "mode", "model", "permission", "plugin",
    "provider", "reference", "references", "server", "share", "shell", "skills",
    "small_model", "snapshot", "subagent_depth", "tool_output", "tools",
    "username", "watcher",
})

OPENCODE_AGENT_KEYS = frozenset({
    "color", "description", "disable", "hidden", "maxSteps", "mode", "model",
    "options", "permission", "prompt", "steps", "temperature", "tools",
    "top_p", "variant",
})

# Ghostlab name -> OpenCode name.
_RENAMES = {"agents": "agent", "commands": "command", "plugins": "plugin"}

# Runtime keys Ghostlab consumes itself and never forwards to OpenCode.
_GHOSTLAB_ONLY = frozenset({
    "backend", "kind", "timeout_seconds", "approval_mode", "codex_sandbox",
    "codex_bin", "opencode_bin",
})

# Keys whose values are host paths that must be rewritten for the sandbox.
_PATH_LIST_KEYS = ("instructions",)

# Deny every side-effecting built-in unless the agent config asks otherwise, so
# an unconfigured agent cannot reach around the capabilities under evaluation.
DEFAULT_PERMISSION: dict[str, Any] = {
    "bash": "deny", "edit": "deny", "webfetch": "deny", "external_directory": "deny",
}


class OpencodeConfigError(ConfigError):
    """Raised when an agent runtime cannot be expressed as OpenCode config."""


def _check_keys(where: str, value: Any, allowed: frozenset[str]) -> None:
    if not isinstance(value, dict):
        raise OpencodeConfigError(f"{where} must be a mapping")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise OpencodeConfigError(
            f"{where}: unknown OpenCode option(s) {', '.join(unknown)}. "
            f"Valid keys are documented at {SCHEMA_URL}"
        )


def validate_runtime(runtime: dict[str, Any]) -> None:
    """Fail fast on runtime keys OpenCode would silently ignore."""
    if not isinstance(runtime, dict):
        raise OpencodeConfigError("agent.runtime must be a mapping")
    forwarded = {
        _RENAMES.get(key, key) for key in runtime if key not in _GHOSTLAB_ONLY
    }
    unknown = sorted(forwarded - OPENCODE_CONFIG_KEYS - {"mcp"})
    if unknown:
        raise OpencodeConfigError(
            f"agent.runtime: unknown option(s) {', '.join(unknown)}. "
            f"Valid keys are documented at {SCHEMA_URL}"
        )
    for name, definition in (runtime.get("agents") or {}).items():
        _check_keys(f"agent.runtime.agents.{name}", definition, OPENCODE_AGENT_KEYS)


def mcp_entry(mcp: dict[str, Any], path_map: Callable[[Any], str] | None = None) -> dict[str, Any]:
    """One `mcp.<name>` entry for a normalized Ghostlab MCP input."""
    inside = path_map or (lambda value: str(value))
    transport = str(mcp.get("transport") or "streamable-http")
    connection = expand_env(dict(mcp.get("connection") or {}))

    if transport == "stdio":
        raw = connection.get("command") or []
        parts = [raw] if isinstance(raw, str) else list(raw)
        entry: dict[str, Any] = {
            "type": "local",
            "command": [inside(part) for part in parts]
            + [inside(part) for part in connection.get("args", [])],
            "enabled": True,
        }
        if connection.get("env"):
            entry["environment"] = {
                str(key): str(value) for key, value in dict(connection["env"]).items()
            }
        return entry
    if transport in ("sse", "streamable-http", "http"):
        entry = {"type": "remote", "url": str(connection.get("url", "")), "enabled": True}
        if connection.get("headers"):
            entry["headers"] = {
                str(key): str(value) for key, value in dict(connection["headers"]).items()
            }
        return entry
    raise OpencodeConfigError(f"Unsupported MCP transport for OpenCode: {transport}")


def build_project_config(
    runtime: dict[str, Any] | None = None,
    mcps: "list[dict[str, Any]] | None" = None,
    *,
    path_map: Callable[[Any], str] | None = None,
    skills: "list[dict[str, Any]] | None" = None,
) -> dict[str, Any]:
    """Render an agent's runtime + inputs as an ``opencode.json`` mapping.

    ``path_map`` rewrites host paths to their in-sandbox location; omit it to
    keep host paths (local execution).
    """
    runtime = dict(runtime or {})
    validate_runtime(runtime)
    inside = path_map or (lambda value: str(value))

    config: dict[str, Any] = {
        "$schema": SCHEMA_URL,
        # Never let a sandboxed agent spend a turn upgrading itself.
        "autoupdate": False,
        "permission": dict(DEFAULT_PERMISSION),
    }

    for key, value in runtime.items():
        if key in _GHOSTLAB_ONLY or value in (None, "", [], {}):
            continue
        name = _RENAMES.get(key, key)
        if key in _PATH_LIST_KEYS:
            config[name] = [inside(item) for item in value]
        elif key == "skills":
            entry = {}
            if value.get("paths"):
                entry["paths"] = [inside(item) for item in value["paths"]]
            if value.get("urls"):
                entry["urls"] = [str(item) for item in value["urls"]]
            config[name] = entry
        elif key == "agents":
            config[name] = {
                str(agent_name): _agent_definition(definition, inside)
                for agent_name, definition in value.items()
            }
        elif key == "permission":
            config[name] = {**DEFAULT_PERMISSION, **dict(value)}
        else:
            config[name] = value

    # Skills attached as job inputs sit alongside any declared in the runtime.
    skill_paths = [inside(item["path"]) for item in (skills or []) if item.get("path")]
    if skill_paths:
        existing = dict(config.get("skills") or {})
        existing["paths"] = list(existing.get("paths") or []) + skill_paths
        config["skills"] = existing

    entries = {}
    for mcp in mcps or []:
        entries[str(mcp.get("id") or "mcp")] = mcp_entry(mcp, inside)
    if entries:
        config["mcp"] = entries
    return config


def _agent_definition(definition: Any, inside: Callable[[Any], str]) -> dict[str, Any]:
    """One `agent.<name>` block; a file-backed prompt is carried as a path."""
    if not isinstance(definition, dict):
        raise OpencodeConfigError("each agent definition must be a mapping")
    resolved = dict(definition)
    prompt = resolved.get("prompt")
    # A prompt that names an existing file travels as a path; inline text stays.
    if isinstance(prompt, str) and prompt.strip() and Path(prompt).suffix in (".md", ".txt"):
        resolved["prompt"] = inside(prompt)
    return resolved


def runtime_input_paths(
    runtime: dict[str, Any] | None, skills: "list[dict[str, Any]] | None" = None
) -> list[str]:
    """Every host path an OpenCode runtime references, for sandbox uploading."""
    runtime = dict(runtime or {})
    paths: list[str] = [str(item) for item in (runtime.get("instructions") or [])]
    paths += [str(item) for item in ((runtime.get("skills") or {}).get("paths") or [])]
    for definition in (runtime.get("agents") or {}).values():
        prompt = (definition or {}).get("prompt")
        if isinstance(prompt, str) and Path(prompt).suffix in (".md", ".txt"):
            paths.append(prompt)
    paths += [str(item["path"]) for item in (skills or []) if item.get("path")]
    return [path for path in paths if path]


def write_project(
    directory: Path,
    runtime: dict[str, Any] | None = None,
    mcps: "list[dict[str, Any]] | None" = None,
    *,
    path_map: Callable[[Any], str] | None = None,
    skills: "list[dict[str, Any]] | None" = None,
) -> Path:
    """Materialize an OpenCode project directory and return its config path."""
    directory.mkdir(parents=True, exist_ok=True)
    config = build_project_config(runtime, mcps, path_map=path_map, skills=skills)
    path = directory / "opencode.json"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path
