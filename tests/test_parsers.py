from __future__ import annotations

import json

from ghostlab.parsers import parse_claude, parse_codex, parse_copilot, parse_opencode


def stream(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\nstray log line"


def test_claude_pairs_tool_results_and_takes_the_final_result() -> None:
    parsed = parse_claude(stream(
        {"type": "system", "subtype": "init", "session_id": "s-1"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "mcp__tiny__host_info", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "linux"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "permission denied", "is_error": True}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "draft"}]}},
        {"type": "result", "subtype": "success", "result": "Kernel: linux"},
    ))
    assert parsed.message == "Kernel: linux"
    assert parsed.session_id == "s-1"
    assert [(c["server"], c["tool"], c["status"], c["result"]) for c in parsed.tool_calls] == [
        ("tiny", "host_info", "completed", "linux")]
    assert parsed.builtin_calls[0]["failure_cause"] == "permission_denied"
    assert parse_claude(stream({"type": "result", "subtype": "error_max_turns", "is_error": True})
                        ).errors == ["error_max_turns"]


def test_codex_items_errors_and_thread_id() -> None:
    parsed = parse_codex(stream(
        {"type": "thread.started", "thread_id": "th-9"},
        {"type": "item.started", "item": {"type": "mcp_tool_call", "server": "c", "tool": "t"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "c", "tool": "t",
                                            "arguments": {}, "result": {"ok": 1}, "status": "completed"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "c", "tool": "u",
                                            "error": {"message": "boom"}, "status": "failed"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}},
        {"type": "turn.failed", "error": {"message": "quota exceeded"}},
    ))
    assert parsed.session_id == "th-9"
    assert parsed.message == "Done."
    assert [c["status"] for c in parsed.tool_calls] == ["completed", "failed"]
    assert parsed.errors == ["quota exceeded"]


def test_copilot_separates_mcp_from_builtin_calls() -> None:
    parsed = parse_copilot(stream(
        {"type": "assistant.message", "data": {"content": "Looking.", "toolRequests": [
            {"toolCallId": "m1", "name": "notes_search", "mcpServerName": "notes", "mcpToolName": "search"}]}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "m1", "success": True, "result": "found"}},
        {"type": "tool.execution_start", "data": {"toolCallId": "b1", "toolName": "shell"}},
        {"type": "tool.execution_complete", "data": {"toolCallId": "b1", "success": False,
                                                     "error": {"message": "permission denied"}}},
        {"type": "assistant.message", "data": {"content": "Found it."}},
        {"type": "session.error", "data": {"message": "model unavailable"}},
    ))
    assert parsed.message == "Found it."
    assert [(c["server"], c["tool"], c["status"]) for c in parsed.tool_calls] == [
        ("notes", "search", "completed")]
    assert parsed.builtin_calls[0]["failure_cause"] == "permission_denied"
    assert parsed.errors == ["model unavailable"]


def test_opencode_only_claims_known_servers_as_mcp() -> None:
    text = stream(
        {"type": "text", "sessionID": "ses_1", "part": {"text": "working"}},
        {"type": "tool_use", "part": {"tool": "safari_list_tabs", "state": {
            "status": "completed", "input": {}, "output": "[]", "time": {"start": 1000, "end": 1150}}}},
        {"type": "tool_use", "part": {"tool": "read", "state": {"status": "error", "error": "timed out"}}},
        {"type": "error", "error": {"name": "APIError", "data": {"message": "model is not supported"}}},
    )
    parsed = parse_opencode(text, ["safari"])
    assert parsed.session_id == "ses_1"
    assert parsed.message == "working"
    assert parsed.tool_calls[0]["tool"] == "list_tabs"
    assert parsed.tool_calls[0]["duration_ms"] == 150.0
    assert parsed.builtin_calls[0]["failure_cause"] == "timeout"
    assert parsed.errors == ["model is not supported"]
    assert parse_opencode(text).tool_calls == []
