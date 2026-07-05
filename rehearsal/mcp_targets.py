"""MCP target adapters — one canonical model, many provider formats.

GhostLab's internal representation of "an MCP under test" is `TargetConfig`
(transport + connection). This module is the **adapter layer** that translates
*provider* config shapes into that canonical model, so GhostLab can ingest the
same config a user already gives Codex, Claude Desktop, Cursor, or VS Code
instead of requiring a bespoke translation step (issue #32).

Ingest adapters (provider -> canonical) live here:

- ``mcpServers`` JSON: the near-universal client format —
  ``{"mcpServers": {"<name>": {"command"/"args"/"env"} | {"url"/"headers"}}}``.
- GhostLab native target JSON: ``{"id","transport","connection"}``.

The reverse direction (canonical -> a provider config) already exists as
`rehearsal.mcp_config.build_mcp_servers_config`, which emits an ``mcpServers``
document to inject into an agent-under-test runner. Future providers plug in by
adding an ingest adapter here and/or an emit adapter there; nothing else in the
pipeline needs to know which client format the user started from.

Secrets stay out of any stored spec: header/env values may reference environment
variables (``${TOKEN}``) and are expanded at connection time by `expand_env`,
not here — normalization preserves the placeholder verbatim.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import ConfigError, TargetConfig, load_json

# Normalize the various spellings providers use for an HTTP-family transport.
_HTTP_TRANSPORTS = {
    "http": "streamable-http",
    "streamable-http": "streamable-http",
    "streamable_http": "streamable-http",
    "streamablehttp": "streamable-http",
    "sse": "sse",
}


def is_mcp_servers_config(data: Any) -> bool:
    """True if ``data`` is a standard client config with an ``mcpServers`` map."""
    return isinstance(data, dict) and isinstance(data.get("mcpServers"), dict)


def select_server(
    servers: dict[str, Any], server: str | None, source: str
) -> tuple[str, dict[str, Any]]:
    """Pick one server from an ``mcpServers`` map.

    Auto-selects when exactly one server is present; requires ``server`` when
    several are, and errors clearly (listing names) otherwise — the #32 rule.
    """
    if not servers:
        raise ConfigError(f"{source}: 'mcpServers' is empty")
    if server is not None:
        if server not in servers:
            names = ", ".join(sorted(servers))
            raise ConfigError(
                f"{source}: no server '{server}' in mcpServers (have: {names})"
            )
        return server, servers[server]
    if len(servers) == 1:
        name = next(iter(servers))
        return name, servers[name]
    names = ", ".join(sorted(servers))
    raise ConfigError(
        f"{source}: {len(servers)} servers in mcpServers ({names}); "
        f"pass --server <name> to choose one."
    )


def normalize_server_entry(name: str, entry: dict[str, Any], source: str) -> TargetConfig:
    """Translate a single ``mcpServers.<name>`` entry into a TargetConfig."""
    if not isinstance(entry, dict):
        raise ConfigError(f"{source}: mcpServers.{name} must be an object")
    if entry.get("url"):
        raw_transport = str(entry.get("transport") or entry.get("type") or "streamable-http")
        transport = _HTTP_TRANSPORTS.get(raw_transport.lower().strip(), "streamable-http")
        connection: dict[str, Any] = {
            "url": str(entry["url"]),
            "headers": {str(k): str(v) for k, v in dict(entry.get("headers", {})).items()},
        }
    elif entry.get("command"):
        transport = "stdio"
        connection = {
            "command": entry["command"],
            "args": [str(a) for a in entry.get("args", [])],
            "env": {str(k): str(v) for k, v in dict(entry.get("env", {})).items()},
        }
    else:
        raise ConfigError(
            f"{source}: mcpServers.{name} needs a 'command' (stdio) or 'url' (http/sse)"
        )
    return TargetConfig(
        id=name, transport=transport, connection=connection, capabilities={}, startup={}
    )


def normalize_target(
    data: dict[str, Any], *, server: str | None = None, source: str = "config"
) -> TargetConfig:
    """Normalize provider config (or GhostLab native) into a TargetConfig."""
    if is_mcp_servers_config(data):
        name, entry = select_server(data["mcpServers"], server, source)
        return normalize_server_entry(name, entry, source)
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a JSON object")
    missing = [key for key in ("id", "transport", "connection") if key not in data]
    if missing:
        raise ConfigError(
            f"{source}: unrecognized target config. Provide either a GhostLab "
            f"target (missing: {', '.join(missing)}) or a standard MCP config "
            f"with an 'mcpServers' object (e.g. your Codex/Claude Desktop config)."
        )
    return TargetConfig(
        id=str(data["id"]),
        transport=str(data["transport"]),
        connection=dict(data["connection"]),
        capabilities=dict(data.get("capabilities", {})),
        startup=dict(data.get("startup", {})),
    )


def load_target(path: Path, *, server: str | None = None) -> TargetConfig:
    """Load + normalize a target config from disk (either supported format)."""
    return normalize_target(load_json(path), server=server, source=str(path))
