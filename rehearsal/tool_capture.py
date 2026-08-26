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


def classify_tool_failure(error: Any) -> str:
    """Classify a failed/cancelled tool call without blaming a nonexistent user."""
    text = json.dumps(error, ensure_ascii=False).lower() if not isinstance(error, str) else error.lower()
    if any(token in text for token in ("permission denied", "approval denied", "not approved")):
        return "permission_denied"
    if any(token in text for token in ("timed out", "timeout", "deadline exceeded")):
        return "client_timeout"
    if any(token in text for token in (
        "text/event-stream", "stream closed", "stream error", "internal_error", "zero bytes",
    )):
        return "server_stream_error"
    if any(token in text for token in ("backend cancelled", "backend canceled", "upstream cancelled")):
        return "backend_cancelled"
    if "cancel" in text or "abort" in text:
        return "client_cancelled"
    if text and text not in ("null", "\"\""):
        return "tool_error"
    return "unknown_failure"


def annotate_tool_failures(calls: list[dict[str, Any]], diagnostic: str = "") -> list[dict[str, Any]]:
    """Attach stable cause/detail fields to failed calls for logs and reports."""
    for call in calls:
        if call.get("status") != "failed":
            continue
        detail = call.get("error") or diagnostic
        call["failure_cause"] = classify_tool_failure(detail)
        if detail:
            call["failure_detail"] = " ".join(str(detail).split())[:500]
    return calls


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
            record: dict[str, Any] = {
                "index": len(calls) + 1,
                "server": item.get("server", "?"),
                "tool": item.get("tool", "?"),
                "status": status,
                "arguments": item.get("arguments"),
                "result": item.get("result"),
                "error": error,
            }
            if status == "failed":
                record["failure_cause"] = classify_tool_failure(error)
                if error:
                    record["failure_detail"] = " ".join(str(error).split())[:500]
            # Capture per-call latency when the stream provides it (forward
            # compatible: absent in current codex output, so simply omitted).
            duration = item.get("duration_ms")
            if isinstance(duration, (int, float)):
                record["duration_ms"] = duration
            calls.append(record)
    return {"message": "\n".join(messages).strip(), "tool_calls": calls}


def parse_opencode_output(
    jsonl_text: str, servers: "list[str] | None" = None
) -> dict[str, Any]:
    """Parse opencode `run --format json` output into a message + tool calls.

    OpenCode namespaces an MCP tool as ``<server>_<tool>`` and mixes those in with
    its own built-in tools (read/write/bash/...). Only names matching a known
    server prefix are reported as MCP `tool_calls`; the rest are kept separately
    as `builtin_calls` so the judge's hallucination check never sees a host tool
    and mistakes it for an invented MCP tool.

    Unlike codex, opencode timestamps each call, so `duration_ms` is always
    populated here.
    """
    known = sorted([s for s in (servers or []) if s], key=len, reverse=True)
    messages: list[str] = []
    calls: list[dict[str, Any]] = []
    builtins: list[dict[str, Any]] = []
    errors: list[str] = []

    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        part = event.get("part") or {}

        if kind == "error":
            # opencode reports API/model failures as an event and still exits 0;
            # collecting them here is what lets the runner fail loudly instead of
            # handing a raw protocol frame to the other agent as conversation.
            detail = event.get("error") or {}
            data = detail.get("data") if isinstance(detail, dict) else {}
            message = ""
            if isinstance(data, dict):
                message = str(data.get("message") or "")
            if not message and isinstance(detail, dict):
                message = str(detail.get("name") or "")
            errors.append(message or json.dumps(detail)[:300])
            continue

        if kind == "text":
            text = part.get("text")
            if text:
                messages.append(str(text))
            continue
        if kind not in ("tool_use", "tool"):
            continue

        name = str(part.get("tool") or "?")
        state = part.get("state") or {}
        raw_status = str(state.get("status") or "")
        status = {
            "completed": "completed", "error": "failed", "failed": "failed",
        }.get(raw_status, "unknown" if not raw_status else "failed")

        server = ""
        tool = name
        for candidate in known:
            if name.startswith(f"{candidate}_"):
                server, tool = candidate, name[len(candidate) + 1 :]
                break

        error = state.get("error")
        if status == "failed" and not error:
            error = state.get("output")
        record: dict[str, Any] = {
            "index": 0,  # assigned below, per stream
            "server": server or "opencode",
            "tool": tool,
            "status": status,
            "arguments": state.get("input"),
            "result": state.get("output"),
            "error": error,
        }
        times = state.get("time") or {}
        start, end = times.get("start"), times.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            record["duration_ms"] = round(float(end) - float(start), 1)
        if status == "failed":
            record["failure_cause"] = classify_tool_failure(error)
            if error:
                record["failure_detail"] = " ".join(str(error).split())[:500]

        target = calls if server else builtins
        target.append(record)
        record["index"] = len(target)

    return {
        "message": "\n".join(messages).strip(),
        "tool_calls": calls,
        "builtin_calls": builtins,
        "errors": errors,
    }


def parse_copilot_output(jsonl_text: str) -> dict[str, Any]:
    """Parse GitHub Copilot CLI ``--output-format json`` events.

    Copilot identifies MCP tools explicitly on both request and execution-start
    events. That lets Ghostlab separate evaluated MCP calls from built-in coding
    tools without relying on name prefixes.
    """
    messages: list[str] = []
    calls: list[dict[str, Any]] = []
    builtins: list[dict[str, Any]] = []
    errors: list[str] = []
    pending: dict[str, dict[str, Any]] = {}

    def start_call(data: dict[str, Any]) -> dict[str, Any]:
        call_id = str(data.get("toolCallId") or "")
        existing = pending.get(call_id)
        if existing is not None:
            server = str(data.get("mcpServerName") or "")
            existing["arguments"] = data.get("arguments", existing.get("arguments"))
            existing["server"] = str(
                server or existing.get("server") or "copilot"
            )
            existing["tool"] = str(
                data.get("mcpToolName")
                or data.get("toolName")
                or existing.get("tool")
                or "?"
            )
            if server and existing in builtins:
                builtins.remove(existing)
                calls.append(existing)
                for index, record in enumerate(builtins, start=1):
                    record["index"] = index
                existing["index"] = len(calls)
            return existing
        server = str(data.get("mcpServerName") or "")
        record: dict[str, Any] = {
            "index": 0,
            "server": server or "copilot",
            "tool": str(data.get("mcpToolName") or data.get("toolName") or data.get("name") or "?"),
            "status": "unknown",
            "arguments": data.get("arguments"),
            "result": None,
            "error": None,
        }
        target = calls if server else builtins
        target.append(record)
        record["index"] = len(target)
        if call_id:
            pending[call_id] = record
        return record

    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = str(event.get("type") or "")
        data = event.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        if kind == "assistant.message":
            content = data.get("content")
            if content:
                messages.append(str(content))
            for request in data.get("toolRequests") or []:
                if isinstance(request, dict):
                    start_call(
                        {
                            "toolCallId": request.get("toolCallId"),
                            "toolName": request.get("name"),
                            "arguments": request.get("arguments"),
                            "mcpServerName": request.get("mcpServerName"),
                            "mcpToolName": request.get("mcpToolName"),
                        }
                    )
            continue

        if kind == "tool.execution_start":
            start_call(data)
            continue

        if kind == "tool.execution_complete":
            call_id = str(data.get("toolCallId") or "")
            record = pending.get(call_id)
            if record is None:
                record = start_call(
                    {
                        "toolCallId": call_id,
                        "toolName": data.get("toolName") or "?",
                    }
                )
            success = bool(data.get("success"))
            error = data.get("error")
            record.update(
                {
                    "status": "completed" if success else "failed",
                    "result": data.get("result"),
                    "error": error,
                }
            )
            if not success:
                record["failure_cause"] = classify_tool_failure(error)
                if error:
                    record["failure_detail"] = " ".join(str(error).split())[:500]
            continue

        if kind in ("session.error", "assistant.error"):
            detail = data.get("message") or data.get("error") or event.get("error")
            errors.append(str(detail or kind))
        elif kind == "result" and int(event.get("exitCode") or 0) != 0:
            errors.append(f"Copilot CLI exited with code {event.get('exitCode')}")

    return {
        # Intermediate assistant messages normally contain tool intent. The last
        # non-empty message is the conversational reply after tool execution.
        "message": messages[-1].strip() if messages else "",
        "tool_calls": calls,
        "builtin_calls": builtins,
        "errors": errors,
    }


def summarize_tool_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate counts by tool and by status for quick reporting."""
    by_tool: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_failure_cause: dict[str, int] = {}
    for call in calls:
        name = f"{call['server']}/{call['tool']}"
        by_tool[name] = by_tool.get(name, 0) + 1
        by_status[call["status"]] = by_status.get(call["status"], 0) + 1
        cause = call.get("failure_cause")
        if cause:
            by_failure_cause[cause] = by_failure_cause.get(cause, 0) + 1
    return {
        "total": len(calls), "by_tool": by_tool, "by_status": by_status,
        "by_failure_cause": by_failure_cause,
    }


def _args_key(arguments: Any) -> str:
    try:
        return json.dumps(arguments, sort_keys=True)
    except TypeError:
        return repr(arguments)


def efficiency_metrics(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic efficiency signals over a run's tool calls.

    Beyond raw counts, this flags two tool-design smells: `redundant_calls`
    (the same tool invoked again with identical arguments — useful work is rarely
    repeated verbatim) and `max_calls_to_one_tool` (hammering a single tool, often
    a sign of an unclear schema or missing capability). Per-call latency is
    aggregated only when the capture provided `duration_ms`.
    """
    by_tool: dict[str, int] = {}
    seen: set[tuple[Any, Any, str]] = set()
    redundant = 0
    durations: list[float] = []
    for call in calls:
        name = f"{call.get('server', '?')}/{call.get('tool', '?')}"
        by_tool[name] = by_tool.get(name, 0) + 1
        args = call.get("arguments")
        if args is not None:  # args unknown on the text parser; can't judge repeats
            key = (call.get("server"), call.get("tool"), _args_key(args))
            if key in seen:
                redundant += 1
            seen.add(key)
        duration = call.get("duration_ms")
        if isinstance(duration, (int, float)):
            durations.append(duration)

    metrics: dict[str, Any] = {
        "total_calls": len(calls),
        "unique_tools": len(by_tool),
        "redundant_calls": redundant,
        "max_calls_to_one_tool": max(by_tool.values()) if by_tool else 0,
    }
    if durations:
        metrics["total_duration_ms"] = round(sum(durations), 1)
        metrics["avg_duration_ms"] = round(sum(durations) / len(durations), 1)
    return metrics
