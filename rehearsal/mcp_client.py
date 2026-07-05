"""Minimal MCP client over stdio and streamable-HTTP, using only the stdlib.

Rehearsal needs to connect to a target MCP server and introspect what it
exposes (tools / resources / prompts) without depending on the official MCP
SDK. This client speaks just enough of the protocol for read-only discovery:
the `initialize` handshake plus `*/list` methods.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import TargetConfig, expand_env

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "rehearsal-ghostlab", "version": "0.1.0"}


class McpClientError(RuntimeError):
    """Raised when the MCP transport or JSON-RPC exchange fails."""


@dataclass
class McpResponse:
    """Result of a single JSON-RPC call."""

    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def unwrap(self, context: str) -> dict[str, Any]:
        if self.error is not None:
            raise McpClientError(f"{context} failed: {self.error}")
        return self.result or {}


@dataclass
class McpClient:
    """Abstract MCP client. Subclasses implement a transport."""

    server_info: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    instructions: str = ""

    def _call(self, method: str, params: dict[str, Any] | None) -> McpResponse:
        raise NotImplementedError

    def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - transports override as needed
        pass

    def initialize(self) -> dict[str, Any]:
        response = self._call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
        )
        result = response.unwrap("initialize")
        self.server_info = result.get("serverInfo", {})
        self.capabilities = result.get("capabilities", {})
        self.instructions = result.get("instructions", "") or ""
        self._notify("notifications/initialized", None)
        return result

    def read_resource(self, uri: str) -> dict[str, Any]:
        """Fetch a single resource via `resources/read`.

        Returns the raw result (typically `{"contents": [...]}`). Used by the
        MCP Apps host layer to fetch `ui://` widget resources for diagnostics.
        """
        response = self._call("resources/read", {"uri": uri})
        return response.unwrap(f"resources/read {uri}")

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a tool via `tools/call` and return its result.

        Used by the MCP Apps host layer to obtain the tool result a `ui://`
        widget renders from.
        """
        response = self._call("tools/call", {"name": name, "arguments": arguments or {}})
        return response.unwrap(f"tools/call {name}")

    def call_tool_raw(self, name: str, arguments: dict[str, Any] | None = None) -> McpResponse:
        """Invoke a tool and return the raw response without unwrapping.

        Lets callers distinguish a *graceful* failure (JSON-RPC error object or
        `isError: true` result) from a transport crash — the difference between
        an edge-case test passing and failing.
        """
        return self._call("tools/call", {"name": name, "arguments": arguments or {}})

    def list_collection(self, method: str, key: str) -> list[dict[str, Any]]:
        """Page through a `*/list` method until the cursor is exhausted."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params = {"cursor": cursor} if cursor else {}
            try:
                response = self._call(method, params)
            except McpClientError:
                # Capability not supported by this server; treat as empty.
                return items
            if response.error is not None:
                return items
            result = response.result or {}
            items.extend(result.get(key, []) or [])
            cursor = result.get("nextCursor")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
        return items


# --------------------------------------------------------------------------- #
# Streamable HTTP transport
# --------------------------------------------------------------------------- #
def _parse_sse_messages(body: str) -> list[dict[str, Any]]:
    """Parse every JSON message in an SSE stream, one per event block.

    Events are separated by blank lines; `data:` lines *within* one event are
    joined (a single JSON payload may span several data lines).
    """
    messages: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        data_lines = [
            line[len("data:"):].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        payload = "\n".join(data_lines).strip()
        if not payload:
            continue
        try:
            messages.append(json.loads(payload))
        except json.JSONDecodeError as exc:
            raise McpClientError(f"Bad SSE JSON payload: {payload!r}") from exc
    return messages


def _parse_sse(body: str, expected_id: int | None = None) -> dict[str, Any]:
    """Pick the JSON-RPC *response* out of an SSE-framed body.

    A streamable-HTTP server may interleave notifications (log messages,
    progress) with the response on the same stream; select the message whose
    `id` matches, falling back to the last response-shaped message.
    """
    messages = _parse_sse_messages(body)
    if not messages:
        raise McpClientError(f"Empty SSE body: {body!r}")
    if expected_id is not None:
        for message in messages:
            if message.get("id") == expected_id:
                return message
    for message in reversed(messages):
        if "result" in message or "error" in message:
            return message
    return messages[-1]


class HttpMcpClient(McpClient):
    """Streamable-HTTP / SSE transport.

    Captures and replays the `mcp-session-id` header when the server issues one.
    """

    def __init__(self, url: str, headers: dict[str, str], timeout: float = 30.0) -> None:
        super().__init__()
        if not url:
            raise McpClientError("HTTP target requires a connection.url")
        self.url = url
        self.base_headers = dict(headers or {})
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 0

    def _request(self, payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.base_headers,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                session = resp.headers.get("mcp-session-id")
                if session:
                    self.session_id = session
                raw = resp.read().decode("utf-8")
                content_type = resp.headers.get("Content-Type", "")
                return raw, {"content_type": content_type}
        except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
            detail = exc.read().decode("utf-8", "replace")
            raise McpClientError(f"HTTP {exc.code} from {self.url}: {detail}") from exc
        except urllib.error.URLError as exc:  # type: ignore[attr-defined]
            raise McpClientError(f"Cannot reach {self.url}: {exc.reason}") from exc

    def _decode(
        self, raw: str, meta: dict[str, str], expected_id: int | None = None
    ) -> dict[str, Any]:
        if "text/event-stream" in meta.get("content_type", ""):
            return _parse_sse(raw, expected_id=expected_id)
        if not raw.strip():
            return {}
        return json.loads(raw)

    def _call(self, method: str, params: dict[str, Any] | None) -> McpResponse:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        raw, meta = self._request(payload)
        message = self._decode(raw, meta, expected_id=self._next_id)
        return McpResponse(result=message.get("result"), error=message.get("error"))

    def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        # Notifications get no response; ignore decode errors / empty bodies.
        try:
            self._request(payload)
        except McpClientError:
            pass


# --------------------------------------------------------------------------- #
# stdio transport
# --------------------------------------------------------------------------- #
class StdioMcpClient(McpClient):
    """Newline-delimited JSON-RPC over a child process's stdin/stdout."""

    def __init__(self, command: list[str], args: list[str], env: dict[str, str], timeout: float = 30.0) -> None:
        super().__init__()
        if not command:
            raise McpClientError(
                "stdio target has no command. A GhostLab target needs "
                "connection.command; a standard MCP config needs "
                "mcpServers.<name>.command (pass --server to pick one)."
            )
        import os

        full_command = [command, *args] if isinstance(command, str) else [*command, *args]
        process_env = {**os.environ, **(env or {})}
        self.timeout = timeout
        self._next_id = 0
        self.proc = subprocess.Popen(
            full_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_env,
            text=True,
            bufsize=1,
        )
        # stdout is drained on a daemon thread so reads can time out; a bare
        # readline() would hang the whole pipeline on an unresponsive server.
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._reader = threading.Thread(target=self._pump_stdout, daemon=True)
        self._reader.start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        except ValueError:  # stream closed under us during shutdown
            pass
        self._lines.put(None)  # EOF sentinel

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _read_for_id(self, expected_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpClientError(
                    f"stdio server did not answer request id={expected_id} "
                    f"within {self.timeout:g}s"
                )
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue  # deadline check above raises
            if line is None:
                stderr = ""
                if self.proc.stderr is not None and self.proc.poll() is not None:
                    stderr = self.proc.stderr.read() or ""
                raise McpClientError(f"stdio server closed unexpectedly. stderr:\n{stderr}")
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # Server may log non-JSON to stdout; skip it.
                continue
            if message.get("id") == expected_id:
                return message

    def _call(self, method: str, params: dict[str, Any] | None) -> McpResponse:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        message = self._read_for_id(self._next_id)
        return McpResponse(result=message.get("result"), error=message.get("error"))

    def _notify(self, method: str, params: dict[str, Any] | None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def create_client(target: TargetConfig, timeout: float = 30.0) -> McpClient:
    """Build an MCP client for a target's transport."""
    # Expand $VAR / ${VAR} in connection values (e.g. an auth header token kept
    # in the environment instead of a tracked spec file).
    connection = expand_env(target.connection)
    if target.transport == "stdio":
        return StdioMcpClient(
            command=connection.get("command"),
            args=connection.get("args", []),
            env=connection.get("env", {}),
            timeout=timeout,
        )
    if target.transport in ("sse", "streamable-http", "http"):
        return HttpMcpClient(
            url=connection.get("url"),
            headers=connection.get("headers", {}),
            timeout=timeout,
        )
    raise McpClientError(f"Unsupported transport: {target.transport}")
