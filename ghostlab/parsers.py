"""Turn each harness's JSONL stream into one shape.

Every parser returns ``Parsed``: the final assistant message, MCP tool calls,
built-in tool calls (Bash, Edit, ...), in-stream errors, and the harness
session id used to resume a conversation. Tool calls are dicts with
``server``, ``tool``, ``status`` (completed | failed | unknown),
``arguments``, ``result``, ``error``, and for failures a ``failure_cause``.
Non-JSON lines are ignored, so stray logs never break a parse.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence


@dataclass
class Parsed:
    message: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    builtin_calls: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    session_id: str = ""


def classify_tool_failure(error: Any) -> str:
    text = error.lower() if isinstance(error, str) else json.dumps(error, default=str).lower()
    if any(t in text for t in ("permission denied", "approval denied", "not approved")):
        return "permission_denied"
    if any(t in text for t in ("timed out", "timeout", "deadline exceeded")):
        return "timeout"
    if any(t in text for t in ("text/event-stream", "stream closed", "stream error", "zero bytes")):
        return "server_stream_error"
    if "cancel" in text or "abort" in text:
        return "cancelled"
    if text and text not in ("null", '""'):
        return "tool_error"
    return "unknown_failure"


def _fail(record: dict[str, Any], error: Any) -> None:
    record["status"] = "failed"
    record["error"] = error
    record["failure_cause"] = classify_tool_failure(error)


def _events(stream: str) -> Iterator[dict[str, Any]]:
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _append(target: list[dict[str, Any]], record: dict[str, Any]) -> dict[str, Any]:
    target.append(record)
    record["index"] = len(target)
    return record


def parse_codex(stream: str, servers: Sequence[str] = ()) -> Parsed:
    """``codex exec --json``: thread/turn/item events."""
    out = Parsed()
    messages: list[str] = []
    for event in _events(stream):
        kind = event.get("type")
        if kind == "thread.started":
            out.session_id = str(event.get("thread_id") or "")
        elif kind == "turn.failed":
            out.errors.append(str((event.get("error") or {}).get("message") or "turn failed"))
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(str(item["text"]))
            elif item.get("type") == "mcp_tool_call":
                record = _append(out.tool_calls, {
                    "server": item.get("server", "?"), "tool": item.get("tool", "?"),
                    "status": "completed", "arguments": item.get("arguments"),
                    "result": item.get("result"), "error": None,
                })
                if item.get("error") or item.get("status") == "failed":
                    _fail(record, item.get("error"))
            elif item.get("type") == "command_execution":
                record = _append(out.builtin_calls, {
                    "server": "builtin", "tool": "shell", "status": "completed",
                    "arguments": {"command": item.get("command")},
                    "result": item.get("aggregated_output"), "error": None,
                })
                if item.get("status") == "failed":
                    _fail(record, item.get("aggregated_output"))
    out.message = "\n".join(messages).strip()
    return out


def parse_claude(stream: str, servers: Sequence[str] = ()) -> Parsed:
    """``claude -p --output-format stream-json --verbose``.

    MCP tools are named ``mcp__<server>__<tool>``; every other tool_use is a
    built-in. The final ``result`` frame is the reply.
    """
    out = Parsed()
    messages: list[str] = []
    pending: dict[str, dict[str, Any]] = {}
    final: str | None = None
    for event in _events(stream):
        kind = event.get("type")
        if event.get("session_id"):
            out.session_id = str(event["session_id"])
        payload = event.get("message")
        content = payload.get("content") if isinstance(payload, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if kind == "assistant":
            for block in blocks:
                if block.get("type") == "text" and block.get("text"):
                    messages.append(str(block["text"]))
                elif block.get("type") == "tool_use":
                    name = str(block.get("name") or "?")
                    if name.startswith("mcp__") and name.count("__") >= 2:
                        _, server, tool = name.split("__", 2)
                        target = out.tool_calls
                    else:
                        server, tool, target = "builtin", name, out.builtin_calls
                    pending[str(block.get("id"))] = _append(target, {
                        "server": server, "tool": tool, "status": "completed",
                        "arguments": block.get("input"), "result": None, "error": None,
                    })
        elif kind == "user":
            for block in blocks:
                if block.get("type") != "tool_result":
                    continue
                record = pending.pop(str(block.get("tool_use_id")), None)
                if record is None:
                    continue
                if block.get("is_error"):
                    _fail(record, block.get("content"))
                else:
                    record["result"] = block.get("content")
        elif kind == "result":
            if event.get("is_error") or event.get("subtype") not in (None, "success"):
                out.errors.append(str(event.get("result") or event.get("subtype") or "error"))
            if isinstance(event.get("result"), str):
                final = event["result"]
    out.message = (final if final is not None else (messages[-1] if messages else "")).strip()
    return out


def parse_copilot(stream: str, servers: Sequence[str] = ()) -> Parsed:
    """``copilot --output-format json``: MCP calls carry ``mcpServerName``."""
    out = Parsed()
    messages: list[str] = []
    pending: dict[str, dict[str, Any]] = {}

    def start(data: dict[str, Any]) -> dict[str, Any]:
        call_id = str(data.get("toolCallId") or "")
        server = str(data.get("mcpServerName") or "")
        tool = str(data.get("mcpToolName") or data.get("toolName") or data.get("name") or "?")
        record = pending.get(call_id)
        if record is None:
            record = _append(out.tool_calls if server else out.builtin_calls, {
                "server": server or "copilot", "tool": tool, "status": "unknown",
                "arguments": data.get("arguments"), "result": None, "error": None,
            })
            if call_id:
                pending[call_id] = record
        elif server and record in out.builtin_calls:
            # A later event revealed this call was an MCP tool after all.
            out.builtin_calls.remove(record)
            for index, other in enumerate(out.builtin_calls, start=1):
                other["index"] = index
            record.update(server=server, tool=tool)
            _append(out.tool_calls, record)
        return record

    for event in _events(stream):
        kind = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if data.get("sessionId"):
            out.session_id = str(data["sessionId"])
        if kind == "assistant.message":
            if data.get("content"):
                messages.append(str(data["content"]))
            for request in data.get("toolRequests") or []:
                if isinstance(request, dict):
                    start({**request, "toolName": request.get("name")})
        elif kind == "tool.execution_start":
            start(data)
        elif kind == "tool.execution_complete":
            record = pending.get(str(data.get("toolCallId") or "")) or start(data)
            if data.get("success"):
                record.update(status="completed", result=data.get("result"))
            else:
                _fail(record, data.get("error"))
        elif kind in ("session.error", "assistant.error"):
            out.errors.append(str(data.get("message") or data.get("error") or kind))
        elif kind == "result" and int(event.get("exitCode") or 0) != 0:
            out.errors.append(f"Copilot CLI exited with code {event.get('exitCode')}")
    # Earlier messages narrate tool intent; the last one is the reply.
    out.message = messages[-1].strip() if messages else ""
    return out


def parse_opencode(stream: str, servers: Sequence[str] = ()) -> Parsed:
    """``opencode run --format json``: MCP tools are named ``<server>_<tool>``.

    Only names with a known server prefix count as MCP calls, so a built-in
    is never mistaken for an MCP tool.
    """
    out = Parsed()
    messages: list[str] = []
    known = sorted((s for s in servers if s), key=len, reverse=True)
    for event in _events(stream):
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        session = event.get("sessionID") or part.get("sessionID")
        if session:
            out.session_id = str(session)
        kind = event.get("type")
        if kind == "error":
            detail = event.get("error") or {}
            data = detail.get("data") if isinstance(detail, dict) else None
            message = str((data or {}).get("message") or "") if isinstance(data, dict) else ""
            if not message and isinstance(detail, dict):
                message = str(detail.get("name") or "")
            out.errors.append(message or json.dumps(detail)[:300])
        elif kind == "text" and part.get("text"):
            messages.append(str(part["text"]))
        elif kind in ("tool_use", "tool"):
            name = str(part.get("tool") or "?")
            server, tool = "", name
            for candidate in known:
                if name.startswith(f"{candidate}_"):
                    server, tool = candidate, name[len(candidate) + 1:]
                    break
            state = part.get("state") or {}
            record = _append(out.tool_calls if server else out.builtin_calls, {
                "server": server or "builtin", "tool": tool, "status": "completed",
                "arguments": state.get("input"), "result": state.get("output"), "error": None,
            })
            status = str(state.get("status") or "")
            if status in ("error", "failed"):
                _fail(record, state.get("error") or state.get("output"))
            elif status != "completed":
                record["status"] = "unknown"
            times = state.get("time") or {}
            if isinstance(times.get("start"), (int, float)) and isinstance(times.get("end"), (int, float)):
                record["duration_ms"] = round(float(times["end"]) - float(times["start"]), 1)
    out.message = "\n".join(messages).strip()
    return out


def parse_text(stream: str, servers: Sequence[str] = ()) -> Parsed:
    return Parsed(message=stream.strip())


PARSERS = {
    "codex": parse_codex, "claude": parse_claude, "copilot": parse_copilot,
    "opencode": parse_opencode, "text": parse_text,
}
