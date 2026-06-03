"""Structured MCP tool-call capture from agent-host output.

Coding agents surface MCP activity in their output. Codex prints plain-text
progress lines on stderr:

    mcp: <server>/<tool> started
    mcp: <server>/<tool> (completed)
    mcp: <server>/<tool> (failed)

This module pairs those start/end lines into structured tool-call records
(server, tool, status, ordering) so runs can be analyzed for tool selection and
failures. It is intentionally tolerant: an unmatched `started` is still reported
(status "unknown"), and unrecognized output simply yields no calls.
"""
from __future__ import annotations

import json
import re
from typing import Any

# `mcp: cortex/student_get_status started` / `... (completed)` / `... (failed)`
_MCP_LINE_RE = re.compile(
    r"mcp:\s+(?P<server>[A-Za-z0-9_.-]+)/(?P<tool>[A-Za-z0-9_.-]+)\s+"
    r"(?P<state>started|\(completed\)|\(failed\)|\(succeeded\))"
)

_END_STATES = {
    "(completed)": "completed",
    "(succeeded)": "completed",
    "(failed)": "failed",
}


def parse_tool_calls(*streams: str) -> list[dict[str, Any]]:
    """Extract ordered tool-call records from one or more output streams.

    Each `started` opens a pending call; the next matching end line for the same
    server/tool closes it. Pending calls left open at the end are emitted with
    status "unknown" so nothing is silently dropped.
    """
    calls: list[dict[str, Any]] = []
    # Pending calls keyed by (server, tool) -> indices into `calls`, FIFO.
    pending: dict[tuple[str, str], list[int]] = {}

    for stream in streams:
        if not stream:
            continue
        for match in _MCP_LINE_RE.finditer(stream):
            server = match.group("server")
            tool = match.group("tool")
            state = match.group("state")
            key = (server, tool)
            if state == "started":
                calls.append(
                    {
                        "index": len(calls) + 1,
                        "server": server,
                        "tool": tool,
                        "status": "unknown",
                    }
                )
                pending.setdefault(key, []).append(len(calls) - 1)
            else:
                status = _END_STATES.get(state, "unknown")
                queue = pending.get(key)
                if queue:
                    calls[queue.pop(0)]["status"] = status
                else:
                    # End line with no matching start; record it anyway.
                    calls.append(
                        {
                            "index": len(calls) + 1,
                            "server": server,
                            "tool": tool,
                            "status": status,
                        }
                    )
    return calls


def parse_codex_output(jsonl_text: str) -> dict[str, Any]:
    """Parse codex `exec --json` (experimental thread/turn/item) output.

    Returns the assistant message (concatenated `agent_message` items) and rich
    tool-call records from `mcp_tool_call` items — including arguments, result,
    and error, which the plain-text path cannot recover. Non-JSON lines (e.g.
    stray logs on stdout) are ignored so the parse degrades gracefully.
    """
    messages: list[str] = []
    calls: list[dict[str, Any]] = []
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        item_type = item.get("type")
        if item_type == "agent_message":
            text = item.get("text")
            if text:
                messages.append(text)
        elif item_type == "mcp_tool_call":
            error = item.get("error")
            status = item.get("status") or ("failed" if error else "completed")
            if status not in ("completed", "failed"):
                status = "failed" if error else "completed"
            calls.append(
                {
                    "index": len(calls) + 1,
                    "server": item.get("server", "?"),
                    "tool": item.get("tool", "?"),
                    "status": status,
                    "arguments": item.get("arguments"),
                    "result": item.get("result"),
                    "error": error,
                }
            )
    return {"message": "\n".join(messages).strip(), "tool_calls": calls}


def summarize_tool_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate counts by tool and by status for quick reporting."""
    by_tool: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for call in calls:
        name = f"{call['server']}/{call['tool']}"
        by_tool[name] = by_tool.get(name, 0) + 1
        by_status[call["status"]] = by_status.get(call["status"], 0) + 1
    return {"total": len(calls), "by_tool": by_tool, "by_status": by_status}
