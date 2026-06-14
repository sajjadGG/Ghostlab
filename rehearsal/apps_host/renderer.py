"""Headless-Chrome renderer for MCP Apps widgets (Playwright).

This is the only part of the host layer that needs a browser. It mounts a widget
in a sandboxed iframe behind the host bridge (:mod:`protocol`), feeds it the tool
input/result, and captures render proof: visible text, a screenshot, console
errors, the host-bridge transcript, and interactive-element counts. It can then
execute a UI-intent plan (:mod:`executor`) against the rendered widget.

Requires the optional ``ghostlab[apps]`` extra (``playwright``) and a Chrome
install; everything else in :mod:`rehearsal.apps_host` is browser-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..mcp_apps import UiIntent
from . import protocol
from .executor import IntentPlan, plan_intents

# Selectors counted as "interactive controls" for render assertions.
_INTERACTIVE_SELECTOR = "button, [role='button'], a[href], input, textarea, select, [draggable='true']"


class RenderUnavailable(RuntimeError):
    """Raised when Playwright or a browser is not available."""


def render_available() -> bool:
    """True if Playwright is importable (browser availability checked at launch)."""
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


@dataclass
class RenderResult:
    """Everything captured from rendering (and optionally driving) a widget."""

    uri: str
    handshake_completed: bool = False
    body_text: str = ""
    interactive_count: int = 0
    console_errors: list[str] = field(default_factory=list)
    network_failures: list[str] = field(default_factory=list)
    transcript: list[dict] = field(default_factory=list)  # classified BridgeMessages as json
    screenshot_path: Optional[str] = None
    final_screenshot_path: Optional[str] = None
    final_body_text: str = ""
    intent_log: list[dict] = field(default_factory=list)
    error: Optional[str] = None

    def summary(self) -> dict[str, Any]:
        """The browser-free dict consumed by :mod:`assertions`."""
        return {
            "handshake_completed": self.handshake_completed,
            "console_errors": self.console_errors,
            "body_text": self.body_text,
            "interactive_count": self.interactive_count,
        }


def render_widget(
    uri: str,
    widget_html: str,
    tool_input: Optional[dict] = None,
    tool_result: Optional[dict] = None,
    intents: Optional[list[UiIntent]] = None,
    screenshot_path: Optional[Path] = None,
    width: int = 640,
    height: int = 560,
    settle_ms: int = 2500,
    action_timeout_ms: int = 4000,
) -> RenderResult:
    """Render a widget behind the host bridge and capture proof.

    ``tool_input`` are the tool-call arguments; ``tool_result`` is the MCP tool
    result the widget renders from. If ``intents`` are given, they are executed
    in order after the initial render. Never raises for a widget-side failure —
    the failure is captured on the result; only a missing browser raises.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - depends on optional extra
        raise RenderUnavailable(
            "Playwright is not installed. Install the apps extra: pip install 'ghostlab[apps]'"
        ) from exc

    result = RenderResult(uri=uri)
    host_page = protocol.build_host_page(width=width, height=height)

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="chrome", headless=True)
            except Exception:
                # Fall back to a bundled chromium if the Chrome channel is absent.
                browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width + 80, "height": height + 80})
            page.on("console", lambda m: _on_console(result, m))
            page.on("requestfailed", lambda r: result.network_failures.append(
                "%s %s" % (r.url, (r.failure or ""))))

            page.set_content(host_page, wait_until="load")
            page.evaluate(
                "([h,a,r]) => window.__ghostlabMount(h,a,r)",
                [widget_html, tool_input, tool_result],
            )
            page.wait_for_timeout(settle_ms)

            frame = _widget_frame(page)
            result.body_text = _safe_text(frame)
            result.interactive_count = frame.locator(_INTERACTIVE_SELECTOR).count() if frame else 0

            raw_trace = page.evaluate("window.__ghostlabTrace") or []
            messages = protocol.classify_transcript(raw_trace)
            result.transcript = [m.to_json() for m in messages]
            result.handshake_completed = protocol.handshake_completed(messages)

            if screenshot_path is not None:
                screenshot_path = Path(screenshot_path)
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot_path), full_page=True)
                result.screenshot_path = str(screenshot_path)

            if intents and frame is not None:
                _run_intents(frame, intents, result, action_timeout_ms)
                page.wait_for_timeout(400)  # let the widget settle after actions
                result.final_body_text = _safe_text(frame)
                if result.screenshot_path is not None:
                    final_path = Path(result.screenshot_path).with_name("widget-final.png")
                    page.screenshot(path=str(final_path), full_page=True)
                    result.final_screenshot_path = str(final_path)
            else:
                result.final_body_text = result.body_text

            browser.close()
    except RenderUnavailable:
        raise
    except Exception as exc:  # browser/launch failure
        result.error = str(exc)
    return result


def _on_console(result: RenderResult, message: Any) -> None:
    if message.type == "error":
        result.console_errors.append(message.text[:300])


def _widget_frame(page: Any):
    """The iframe frame hosting the widget (the second frame on the page)."""
    frames = page.frames
    return frames[1] if len(frames) > 1 else None


def _safe_text(frame: Any) -> str:
    if frame is None:
        return ""
    try:
        return frame.inner_text("body").strip()
    except Exception:
        return ""


def _run_intents(frame: Any, intents: list[UiIntent], result: RenderResult, timeout_ms: int) -> None:
    for plan in plan_intents(intents):
        _run_plan(frame, plan, result, timeout_ms)


def _run_plan(frame: Any, plan: IntentPlan, result: RenderResult, timeout_ms: int) -> None:
    entry: dict[str, Any] = {"intent": plan.intent.to_json(), "ok": True, "steps": []}
    if plan.error:
        entry["ok"] = False
        entry["error"] = plan.error
        result.intent_log.append(entry)
        return
    for action in plan.actions:
        step = {"action": action.to_json(), "ok": True}
        try:
            _run_action(frame, action, timeout_ms)
        except Exception as exc:
            step["ok"] = False
            step["error"] = str(exc)[:200]
            entry["ok"] = False
        entry["steps"].append(step)
    result.intent_log.append(entry)


def _run_action(frame: Any, action: Any, timeout_ms: int) -> None:
    if action.op == "click_text":
        # Prefer a button with this accessible name; fall back to any text node.
        button = frame.get_by_role("button", name=action.text, exact=True)
        if button.count() > 0:
            button.first.click(timeout=timeout_ms)
        else:
            frame.get_by_text(action.text, exact=True).first.click(timeout=timeout_ms)
    elif action.op == "fill":
        frame.locator(action.selector).first.fill(action.text, timeout=timeout_ms)
    elif action.op == "click":
        frame.locator(action.selector).first.click(timeout=timeout_ms)
    else:  # pragma: no cover
        raise ValueError("unknown action op: %s" % action.op)
