from __future__ import annotations

import json
from pathlib import Path

from .config import TargetConfig, expand_env


def build_mcp_servers_config(target: TargetConfig) -> dict[str, object]:
    # Expand $VAR / ${VAR} so a token kept in the environment reaches the
    # agent-under-test's injected MCP config (not just the direct client).
    connection = expand_env(target.connection)

    if target.transport == "stdio":
        server_config: dict[str, object] = {
            "command": connection.get("command"),
            "args": connection.get("args", []),
            "env": connection.get("env", {}),
        }
    else:
        server_config = {
            "transport": target.transport,
            "url": connection.get("url"),
            "headers": connection.get("headers", {}),
        }

    return {"mcpServers": {target.id: server_config}}


def write_mcp_servers_config(path: Path, target: TargetConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_mcp_servers_config(target), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

