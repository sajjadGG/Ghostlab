"""Assemble the structured app report from a render (spec's report sections).

Turns a :class:`~rehearsal.apps_host.renderer.RenderResult` plus app-aware
assertion results into the report sections increment 1 reserved as ``pending``:
host-bridge transcript, interaction transcript, render artifacts, and final app
state — now populated with real captured evidence.
"""
from __future__ import annotations

from typing import Any, Optional

from .renderer import RenderResult


def build_render_report(
    target_id: str,
    tool: str,
    render: RenderResult,
    assertions: list[dict],
) -> dict[str, Any]:
    passed = sum(1 for a in assertions if a.get("passed"))
    return {
        "target_id": target_id,
        "tool": tool,
        "resource_uri": render.uri,
        "summary": {
            "rendered": render.error is None,
            "handshake_completed": render.handshake_completed,
            "interactive_elements": render.interactive_count,
            "console_errors": len(render.console_errors),
            "assertions_passed": passed,
            "assertions_total": len(assertions),
        },
        "assertions": assertions,
        "host_bridge_transcript": {
            "status": "captured" if render.transcript else "empty",
            "handshake_completed": render.handshake_completed,
            "messages": render.transcript,
        },
        "interaction_transcript": {
            "status": "captured" if render.intent_log else "none",
            "events": render.intent_log,
        },
        "render_artifacts": {
            "status": "captured" if render.error is None else "failed",
            "screenshot": render.screenshot_path,
            "final_screenshot": render.final_screenshot_path,
            "interactive_count": render.interactive_count,
            "console_errors": render.console_errors,
            "network_failures": render.network_failures,
            "error": render.error,
        },
        "final_app_state": {
            "status": "captured" if render.final_body_text else "empty",
            "body_text": render.final_body_text,
        },
    }


def render_report_md(report: dict[str, Any]) -> str:
    s = report.get("summary", {})
    lines = [
        "# MCP Apps Render: %s" % report.get("target_id", "?"),
        "",
        "- Tool: `%s`" % report.get("tool", "?"),
        "- Resource: `%s`" % report.get("resource_uri", "?"),
        "- Rendered: %s | handshake: %s" % (s.get("rendered"), s.get("handshake_completed")),
        "- Interactive elements: %s | console errors: %s" % (
            s.get("interactive_elements"), s.get("console_errors")),
        "- Assertions: %s/%s passed" % (s.get("assertions_passed"), s.get("assertions_total")),
        "",
        "## Assertions",
        "",
    ]
    for a in report.get("assertions", []):
        mark = "✅" if a.get("passed") else "❌"
        lines.append("- %s **%s** — %s" % (mark, a.get("name"), a.get("description")))
    lines.append("")

    bridge = report.get("host_bridge_transcript", {})
    lines += ["## Host-bridge transcript", "", "Status: %s" % bridge.get("status"), ""]
    for m in bridge.get("messages", []):
        lines.append("- `%s` %s %s" % (m.get("direction"), m.get("kind"), m.get("method") or ""))
    lines.append("")

    interaction = report.get("interaction_transcript", {})
    if interaction.get("events"):
        lines += ["## Interaction transcript", ""]
        for e in interaction["events"]:
            mark = "✅" if e.get("ok") else "❌"
            lines.append("- %s %s" % (mark, e.get("intent")))
            if e.get("error"):
                lines.append("  - error: %s" % e["error"])
        lines.append("")

    artifacts = report.get("render_artifacts", {})
    lines += ["## Render artifacts", ""]
    if artifacts.get("screenshot"):
        lines.append("- screenshot: `%s`" % artifacts["screenshot"])
    if artifacts.get("final_screenshot"):
        lines.append("- final screenshot: `%s`" % artifacts["final_screenshot"])
    if artifacts.get("error"):
        lines.append("- render error: %s" % artifacts["error"])
    for err in artifacts.get("console_errors", []):
        lines.append("- console error: %s" % err)
    for fail in artifacts.get("network_failures", []):
        lines.append("- network failure: %s" % fail)
    lines.append("")

    final = report.get("final_app_state", {})
    lines += ["## Final app state", "", "```", (final.get("body_text") or "")[:1500], "```", ""]
    return "\n".join(lines)


def first_ui_tool(tools: list, only: Optional[str] = None) -> Optional[dict]:
    """Pick a UI-producing tool: the named one if given, else the first."""
    from ..mcp_apps import ui_resource_uri

    for tool in tools:
        if ui_resource_uri(tool.get("_meta")):
            if only is None or tool.get("name") == only:
                return tool
    return None
