"""Drive a live MCP Apps widget the way a real host does.

`rehearsal.apps_host.renderer` can mount a widget and click/fill it; this module
closes the loop for the *conversational* flow. Given a live MCP client, it:

1. fetches the `ui://` resource the agent's tool call opened,
2. decides what the user does with it — a UI-intent plan derived from the
   persona goal + the widget's own content (LLM-backed when a backend is given,
   heuristic otherwise),
3. renders the widget with a **live relay**, so DOM actions (type essay → click
   Submit → rate feedback) fire real `tools/call`s at the backend, and
4. returns a structured outcome: which backend tools the widget actually called,
   their results, and the widget's final rendered text.

This is what makes GhostLab a real MCP Apps host rather than a text stand-in:
the on-click events in the widget mutate real server state, exactly as they
would inside ChatGPT / Claude.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..mcp_apps import (
    UI_INTENT_SCHEMA,
    UiIntent,
    parse_app_resource,
    parse_ui_intents,
    ui_tools,
    widgets_from_tool_calls,
)


def to_wire_tool_result(result: Any) -> Any:
    """Normalize a captured tool result to the MCP wire shape a widget expects.

    Codex's JSONL capture snake_cases keys (``structured_content``), but widgets
    read the on-wire camelCase (``structuredContent``, ``isError``). We add the
    camelCase aliases without dropping the originals, so a result captured from
    the agent-under-test hydrates the widget the same as a fresh server call.
    """
    if not isinstance(result, dict):
        return result
    out = dict(result)
    aliases = {"structured_content": "structuredContent", "is_error": "isError"}
    for snake, camel in aliases.items():
        if snake in out and camel not in out:
            out[camel] = out[snake]
    return out


def resolve_ui_map(client: Any) -> dict[str, str]:
    """Map ``tool name -> ui:// resource`` from the server's ``tools/list``.

    A widget's *result* often omits the resource URI (it lives in the tool's
    declaration), so we resolve it once from tools/list. Best-effort: returns
    ``{}`` if the listing fails.
    """
    try:
        listing = client._call("tools/list", {}).unwrap("tools/list")
        return {x["tool"]: x["resource_uri"] for x in ui_tools(listing.get("tools", []))}
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# Relay: widget request -> real MCP call
# --------------------------------------------------------------------------- #
def build_relay(client: Any):
    """A relay callback backed by a live :class:`~rehearsal.mcp_client.McpClient`.

    Only the two server-interaction methods are honored; anything else is
    rejected so a widget can't smuggle arbitrary host RPCs through the bridge.
    """

    def relay(method: str, params: dict) -> dict:
        params = params or {}
        if method == "tools/call":
            name = params.get("name")
            if not name:
                raise ValueError("tools/call missing 'name'")
            return client.call_tool(name, params.get("arguments") or {})
        if method == "resources/read":
            uri = params.get("uri")
            if not uri:
                raise ValueError("resources/read missing 'uri'")
            return client.read_resource(uri)
        raise ValueError(f"method not relayable: {method}")

    return relay


# --------------------------------------------------------------------------- #
# Intent planning: what does the user do with this widget?
# --------------------------------------------------------------------------- #
_PLAN_SCHEMA = UI_INTENT_SCHEMA


def _plan_prompt(widget: dict[str, Any], goal: str, persona_note: str) -> str:
    import json

    fields = json.dumps(widget.get("fields") or {}, ensure_ascii=False)[:1500]
    text = (widget.get("text") or "")[:1000]
    return f"""You are the USER interacting with an on-screen widget an assistant just opened for you.
Your goal: {goal}
Who you are: {persona_note or "a realistic user"}

The widget ({widget.get('tool', 'widget')}) shows:
text: {text}
fields: {fields}

Decide the sequence of concrete UI actions you take to actually use this widget toward your goal.
Use these action types: type (enter text into a field — put the real content, e.g. the full essay, in "value"),
choose (select an option by its label in "target"), reorder (value = ordered list of labels),
reveal (show an answer), submit (submit/check the exercise), rate (give feedback, target = label),
mark (mark difficulty, target = e.g. "just right"/"too hard").
Do the exercise for real: if it's a writing task, WRITE the essay in a `type` action, then `submit`.
Output only JSON: {{"intents": [ ... ]}}."""


def plan_widget_intents(
    widget: dict[str, Any],
    goal: str,
    persona_note: str = "",
    backend: Optional[Any] = None,
) -> list[UiIntent]:
    """Plan the user's actions on a widget.

    With a ``backend`` the plan is model-generated from the widget's content (the
    user "reads" the widget and acts); without one, a small heuristic covers the
    common writing/practice/feedback shapes so the loop still runs offline.
    """
    if backend is not None:
        try:
            raw = backend.generate_json(_plan_prompt(widget, goal, persona_note), _PLAN_SCHEMA)
            intents = parse_ui_intents(raw.get("intents", []) if isinstance(raw, dict) else [])
            if intents:
                return intents
        except Exception:  # noqa: BLE001 — fall back to the heuristic below
            pass
    return _heuristic_intents(widget, goal)


def _heuristic_intents(widget: dict[str, Any], goal: str) -> list[UiIntent]:
    fields = widget.get("fields") or {}
    tool = (widget.get("tool") or "").lower()
    intents: list[UiIntent] = []
    # A writing task: compose a short essay from the prompt, then submit.
    prompt = fields.get("question_prompt") or fields.get("prompt") or ""
    if "writing" in tool or prompt:
        # Long enough to clear a widget's minimum-word gate that keeps Submit
        # disabled — a real timed-writing response, not a stub.
        essay = (
            "In recent years the question of whether students should study abroad "
            "has attracted considerable debate. In my view the advantages clearly "
            "outweigh the drawbacks for most motivated learners. First, studying in "
            "another country exposes students to new academic methods and to a "
            "language environment that accelerates fluency far faster than classroom "
            "study at home; a student immersed in daily conversation absorbs idiom and "
            "nuance that textbooks rarely convey. Second, living independently abroad "
            "builds resilience, cultural awareness, and a professional network that "
            "employers increasingly value. There are, admittedly, real disadvantages: "
            "the financial burden can be severe, and some students experience isolation "
            "or homesickness that undermines their performance. However, these costs can "
            "be mitigated through scholarships, structured support services, and careful "
            "preparation. On balance, therefore, the personal and professional gains of "
            "studying abroad justify the expense and difficulty involved, provided the "
            "student chooses a programme that matches their goals."
        )
        intents.append(UiIntent(type="type", value=essay))
        intents.append(UiIntent(type="submit"))
    else:
        # Generic practice/feedback widget: try to submit, then mark difficulty.
        intents.append(UiIntent(type="submit"))
        intents.append(UiIntent(type="mark", target="just right"))
    return intents


# --------------------------------------------------------------------------- #
# Drive
# --------------------------------------------------------------------------- #
@dataclass
class WidgetOutcome:
    """What happened when the user operated one widget."""

    tool: str
    resource_uri: str
    rendered: bool = False
    handshake_completed: bool = False
    intents: list[dict] = field(default_factory=list)
    server_tool_calls: list[dict] = field(default_factory=list)
    # `ui/message` follow-ups the widget emitted (a submitted essay, etc.) — the
    # orchestrator threads this back as the user's next turn.
    follow_up_messages: list[dict] = field(default_factory=list)
    model_context_updates: list[dict] = field(default_factory=list)
    final_text: str = ""
    console_errors: list[str] = field(default_factory=list)
    screenshot_path: Optional[str] = None
    error: Optional[str] = None

    def follow_up_text(self) -> str:
        """Flattened text of the widget's follow-up message(s), if any."""
        from .protocol import widget_message_text

        return "\n\n".join(
            t for t in (widget_message_text(m) for m in self.follow_up_messages) if t
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "resource_uri": self.resource_uri,
            "rendered": self.rendered,
            "handshake_completed": self.handshake_completed,
            "intents": self.intents,
            "server_tool_calls": self.server_tool_calls,
            "follow_up_messages": self.follow_up_messages,
            "model_context_updates": self.model_context_updates,
            "final_text": self.final_text,
            "console_errors": self.console_errors,
            "screenshot_path": self.screenshot_path,
            "error": self.error,
        }


def drive_widget(
    *,
    client: Any,
    widget: dict[str, Any],
    tool_input: Optional[dict],
    tool_result: Optional[dict],
    goal: str,
    persona_note: str = "",
    backend: Optional[Any] = None,
    screenshot_path: Optional[Path] = None,
) -> WidgetOutcome:
    """Fetch, render, and operate a single widget against the live server."""
    from . import renderer as _renderer

    resource_uri = widget.get("resource_uri") or ""
    outcome = WidgetOutcome(tool=widget.get("tool", "?"), resource_uri=resource_uri)
    if not resource_uri:
        outcome.error = "no ui:// resource uri on this widget"
        return outcome
    if not _renderer.render_available():
        outcome.error = "Playwright not installed; install ghostlab[apps]"
        return outcome

    try:
        resource = parse_app_resource(resource_uri, client.read_resource(resource_uri))
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"failed to read resource: {exc}"
        return outcome
    if not resource.renderable:
        outcome.error = f"resource not renderable: {resource.fetch_error or 'empty'}"
        return outcome

    intents = plan_widget_intents(widget, goal, persona_note, backend=backend)
    outcome.intents = [i.to_json() for i in intents]

    render = _renderer.render_widget(
        uri=resource_uri,
        widget_html=resource.html,
        tool_input=tool_input,
        tool_result=to_wire_tool_result(tool_result),
        intents=intents,
        relay=build_relay(client),
        screenshot_path=screenshot_path,
    )
    outcome.rendered = render.error is None
    outcome.handshake_completed = render.handshake_completed
    outcome.server_tool_calls = render.server_tool_calls
    outcome.follow_up_messages = render.widget_messages
    outcome.model_context_updates = render.model_context_updates
    outcome.final_text = render.final_body_text or render.body_text
    outcome.console_errors = render.console_errors
    outcome.screenshot_path = render.final_screenshot_path or render.screenshot_path
    outcome.error = render.error
    return outcome


class AppsHostSession:
    """A live MCP Apps host bound to one target, reused across a scenario's turns.

    Holds the MCP client and the tool→resource map, and drives each turn's
    widgets. Created only in apps mode; ``close`` releases the transport.
    """

    def __init__(self, client: Any, backend: Optional[Any] = None, out_dir: Optional[Path] = None):
        self.client = client
        self.backend = backend
        self.out_dir = out_dir
        self.ui_map = resolve_ui_map(client)
        self._n = 0

    @classmethod
    def connect(cls, target: Any, backend: Optional[Any] = None, out_dir: Optional[Path] = None):
        from ..mcp_client import create_client

        client = create_client(target)
        client.initialize()
        return cls(client, backend=backend, out_dir=out_dir)

    def drive_turn(
        self, tool_calls: list[dict], goal: str, persona_note: str = ""
    ) -> list["WidgetOutcome"]:
        outcomes: list[WidgetOutcome] = []
        for widget in widgets_in_turn(tool_calls, self.ui_map):
            self._n += 1
            shot = (self.out_dir / f"widget-{self._n}.png") if self.out_dir else None
            outcomes.append(
                drive_widget(
                    client=self.client,
                    widget=widget,
                    tool_input=widget.get("_tool_input"),
                    tool_result=widget.get("_tool_result"),
                    goal=goal,
                    persona_note=persona_note,
                    backend=self.backend,
                    screenshot_path=shot,
                )
            )
        return outcomes

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def widgets_in_turn(
    tool_calls: list[dict], ui_map: Optional[dict[str, str]] = None
) -> list[dict[str, Any]]:
    """Widgets an agent turn opened, keeping the raw result for hydration.

    Reuses the text-mode detector but pairs each widget with the originating
    tool call's ``arguments``/``result`` so the renderer can feed the same
    tool-input/tool-result a real host would. ``ui_map`` (from
    :func:`resolve_ui_map`) fills in the ``resource_uri`` for tools whose result
    didn't carry one inline.
    """
    ui_map = ui_map or {}
    widgets = widgets_from_tool_calls(tool_calls)
    by_tool: dict[str, dict] = {}
    for call in tool_calls or []:
        by_tool.setdefault(call.get("tool", "?"), call)
    for widget in widgets:
        if not widget.get("resource_uri"):
            widget["resource_uri"] = ui_map.get(widget.get("tool"))
        origin = by_tool.get(widget.get("tool"))
        if origin:
            widget["_tool_input"] = origin.get("arguments")
            widget["_tool_result"] = origin.get("result")
    return widgets
