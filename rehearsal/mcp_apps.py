"""MCP Apps host-layer foundation: detect, fetch, and diagnose `ui://` widgets.

Some MCP servers (e.g. Cortex) ship **MCP Apps UI** resources: a tool's
`_meta.ui.resourceUri` points to a `ui://...` HTML widget that a compatible host
is expected to render and let the user interact with. The vanilla Rehearsal
runner can confirm an agent *called* a UI-producing tool, but cannot prove the
widget rendered or that a user could interact with it (see
``specs/cortex-mcp-apps-e2e.spec`` and issue #13).

This module is **increment 1** of the MCP Apps host layer. It covers the
deterministic, non-browser foundation:

- detect which tools (and tool *results*) reference a UI resource,
- fetch the `ui://` resource and parse the host-relevant metadata
  (MIME profile, CSP connect/resource domains, preferred frame hints),
- run resource/CSP diagnostics (the spec's P2 findings),
- define the **UI-intent contract** the user emulator will emit and the
  **host-bridge message** vocabulary a renderer must implement,
- assemble a **structured app report** with the sections the spec calls for.

Deferred to later increments (require a headless browser / widget host): actual
iframe rendering, screenshots, live host-bridge message exchange, and executing
UI intents against a real widget. Those sections are present in the report as
explicit "pending" placeholders so the report shape is stable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# A resource is an MCP App surface when its MIME type carries this profile,
# e.g. ``text/html;profile=mcp-app``.
MCP_APP_MIME_PROFILE = "mcp-app"

# Host-bridge messages a fully compatible MCP Apps host must handle. Captured
# here as the contract vocabulary; the live exchange is a later increment.
HOST_BRIDGE_MESSAGES = (
    "initialize",
    "initialized",
    "size-changed",
    "call-server-tool",
    "send-follow-up",
    "open-link",
    "display-mode",
    "teardown",
)

# Structured UI intents the user emulator can express against a rendered widget.
# A later increment adds a UI executor that translates these into low-level DOM
# actions; for now this is the validated contract surface.
UI_INTENT_TYPES = (
    "reorder",   # arrange elements into a target order (sentence scramble)
    "choose",    # select an option (multiple choice)
    "type",      # enter text (writing / fill-in-blank)
    "reveal",    # reveal an answer / transcript
    "submit",    # submit / check the exercise
    "rate",      # rate / record feedback (learning_record_feedback)
    "mark",      # mark difficulty, e.g. too_hard / too_easy
)


# --------------------------------------------------------------------------- #
# UI resource references
# --------------------------------------------------------------------------- #
def ui_resource_uri(meta: Optional[dict]) -> Optional[str]:
    """Pull a `ui://` resource URI out of an MCP `_meta` block.

    Accepts both the nested ``{"ui": {"resourceUri": ...}}`` form and the flat
    ``{"ui/resourceUri": ...}`` alias that some servers emit alongside it.
    """
    if not isinstance(meta, dict):
        return None
    ui = meta.get("ui")
    if isinstance(ui, dict):
        uri = ui.get("resourceUri")
        if isinstance(uri, str) and uri:
            return uri
    flat = meta.get("ui/resourceUri")
    if isinstance(flat, str) and flat:
        return flat
    return None


def ui_tools(tools: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return ``[{tool, resource_uri}]`` for every UI-producing tool."""
    out: list[dict[str, str]] = []
    for tool in tools:
        uri = ui_resource_uri(tool.get("_meta"))
        if uri:
            out.append({"tool": tool.get("name", "?"), "resource_uri": uri})
    return out


def tool_result_ui_ref(tool_result: Optional[dict]) -> Optional[str]:
    """Detect a UI resource referenced by a tool *result*'s `_meta`.

    During a run, a tool result is the signal that the agent triggered a widget;
    this lets the host layer know which resource to fetch and render.
    """
    if not isinstance(tool_result, dict):
        return None
    return ui_resource_uri(tool_result.get("_meta"))


# Tool-name shapes that produce an interactive widget even when the result's
# `_meta` omits an explicit `ui://` ref (some Cortex `views_create_*` results
# carry only a `viewUUID`, with the renderable payload in structured_content).
_UI_TOOL_HINTS = ("views_create_", "_practice", "lesson_start", "placement_create", "initial_setup", "mock_exam")


def _looks_ui_producing(tool_name: str) -> bool:
    name = (tool_name or "").lower()
    return any(hint in name for hint in _UI_TOOL_HINTS)


def widgets_from_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract the interactive widgets an agent's tool calls put in front of the user.

    A conversational user-emulator is text-only: it never sees the rendered
    ``ui://`` widget, so it cannot know it was just asked to (say) write a timed
    essay. This surfaces, from a turn's captured tool calls, the widgets that
    appeared and the *content a human would read and act on* — the prompt, word
    limit, options, rubric — pulled from the result's ``structured_content`` and
    text blocks. The orchestrator feeds this to the emulator so it can respond as
    a real user filling the widget in, not blind.
    """
    widgets: list[dict[str, Any]] = []
    for call in tool_calls or []:
        if call.get("status") not in (None, "completed"):
            continue
        result = call.get("result")
        tool_name = call.get("tool", "?")
        uri = tool_result_ui_ref(result)
        if uri is None and not _looks_ui_producing(tool_name):
            continue
        widgets.append(
            {
                "tool": tool_name,
                "resource_uri": uri,
                "fields": _widget_fields(result),
                "text": _widget_text(result),
            }
        )
    return widgets


def _widget_fields(result: Optional[dict]) -> dict[str, Any]:
    """The human-relevant contents of a widget, from its structured payload."""
    if not isinstance(result, dict):
        return {}
    structured = result.get("structured_content")
    if not isinstance(structured, dict):
        return {}
    # Drop internal plumbing keys; keep what a user would actually read/answer.
    noise = {
        "sessionUUID", "viewUUID", "sourcePath", "viewPath", "viewTool",
        "sourceResourceUri", "resourceUri", "views", "source", "selected",
    }
    return {k: v for k, v in structured.items() if k not in noise}


def _widget_text(result: Optional[dict]) -> str:
    """Concatenated visible text blocks from a tool result's ``content``."""
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# Fetched resource model
# --------------------------------------------------------------------------- #
@dataclass
class AppResource:
    """Parsed `ui://` resource as seen by the host layer."""

    uri: str
    mime_type: str = ""
    html: Optional[str] = None
    html_length: int = 0
    prefers_border: Optional[bool] = None
    csp_connect_domains: list[str] = field(default_factory=list)
    csp_resource_domains: list[str] = field(default_factory=list)
    meta_ui: dict[str, Any] = field(default_factory=dict)
    fetch_error: Optional[str] = None

    @property
    def is_mcp_app(self) -> bool:
        return MCP_APP_MIME_PROFILE in self.mime_type

    @property
    def renderable(self) -> bool:
        """A host could mount this: it was fetched and carries HTML."""
        return self.fetch_error is None and self.html_length > 0

    def to_json(self) -> dict[str, Any]:
        # The HTML body can be hundreds of KB; record its size, not the bytes.
        return {
            "uri": self.uri,
            "mime_type": self.mime_type,
            "is_mcp_app": self.is_mcp_app,
            "html_length": self.html_length,
            "renderable": self.renderable,
            "prefers_border": self.prefers_border,
            "csp": {
                "connectDomains": self.csp_connect_domains,
                "resourceDomains": self.csp_resource_domains,
            },
            "fetch_error": self.fetch_error,
        }


def parse_app_resource(uri: str, read_result: Optional[dict]) -> AppResource:
    """Parse a `resources/read` result into an :class:`AppResource`.

    Picks the content entry whose URI matches (falling back to the first), and
    pulls MIME type, HTML body, and `_meta.ui` (border hint + CSP domains).
    """
    contents = (read_result or {}).get("contents") or []
    chosen: Optional[dict] = None
    for entry in contents:
        if isinstance(entry, dict) and entry.get("uri") == uri:
            chosen = entry
            break
    if chosen is None and contents:
        first = contents[0]
        chosen = first if isinstance(first, dict) else None

    if chosen is None:
        return AppResource(uri=uri, fetch_error="resource returned no contents")

    text = chosen.get("text")
    meta_ui = ((chosen.get("_meta") or {}).get("ui")) or {}
    csp = meta_ui.get("csp") or {}
    return AppResource(
        uri=uri,
        mime_type=chosen.get("mimeType", "") or "",
        html=text if isinstance(text, str) else None,
        html_length=len(text) if isinstance(text, str) else 0,
        prefers_border=meta_ui.get("prefersBorder"),
        csp_connect_domains=list(csp.get("connectDomains") or []),
        csp_resource_domains=list(csp.get("resourceDomains") or []),
        meta_ui=meta_ui if isinstance(meta_ui, dict) else {},
    )


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def _accepts_remote_media(tool: Optional[dict]) -> list[str]:
    """Input-schema properties that imply the widget loads remote media.

    Matches any property whose name carries a ``url`` token (``audio_url``,
    ``image_url``, ``passage_image_url``, ``image_urls``, …) — these are the
    media references a resource CSP can block.
    """
    if not isinstance(tool, dict):
        return []
    props = ((tool.get("inputSchema") or {}).get("properties") or {})
    return [name for name in props if "url" in name.lower()]


def diagnose_resource(
    resource: AppResource, tool: Optional[dict] = None
) -> list[dict[str, Any]]:
    """Resource/CSP diagnostics for a fetched widget (spec P2 findings).

    ``tool`` (the tool whose result produced this resource) lets the check
    cross-reference remote-media parameters against the resource CSP.
    """
    findings: list[dict[str, Any]] = []

    def add(kind: str, severity: str, message: str, **extra: Any) -> None:
        findings.append({"kind": kind, "severity": severity, "message": message, **extra})

    if resource.fetch_error:
        add("resource_unfetchable", "error", resource.fetch_error)
        return findings

    if not resource.renderable:
        add("resource_empty", "error", "resource carries no HTML body to render")
    if not resource.is_mcp_app:
        add(
            "resource_not_mcp_app",
            "warning",
            "MIME type %r lacks the 'profile=mcp-app' marker; a host may not "
            "treat it as an app surface" % resource.mime_type,
        )

    media_props = _accepts_remote_media(tool)
    csp_open = bool(resource.csp_connect_domains or resource.csp_resource_domains)
    if media_props and not csp_open:
        add(
            "csp_blocks_remote_media",
            "warning",
            "tool accepts %s but the resource CSP allows no connect/resource "
            "domains; remote media may render-but-fail to load"
            % ", ".join(media_props),
            media_params=media_props,
        )
    return findings


# --------------------------------------------------------------------------- #
# UI-intent contract
# --------------------------------------------------------------------------- #
class UiIntentError(ValueError):
    """Raised when emulator UI-intent output is malformed."""


@dataclass
class UiIntent:
    """A structured UI action the user emulator wants performed on a widget."""

    type: str
    target: Optional[str] = None
    value: Any = None
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.target is not None:
            out["target"] = self.target
        if self.value is not None:
            out["value"] = self.value
        if self.note:
            out["note"] = self.note
        return out


def parse_ui_intent(obj: Any) -> UiIntent:
    """Validate one emulator-emitted UI intent into a :class:`UiIntent`."""
    if not isinstance(obj, dict):
        raise UiIntentError("UI intent must be an object")
    intent_type = obj.get("type")
    if intent_type not in UI_INTENT_TYPES:
        raise UiIntentError(
            "unknown UI intent type %r (expected one of %s)"
            % (intent_type, ", ".join(UI_INTENT_TYPES))
        )
    return UiIntent(
        type=intent_type,
        target=obj.get("target"),
        value=obj.get("value"),
        note=obj.get("note", "") or "",
    )


def parse_ui_intents(items: Any) -> list[UiIntent]:
    """Validate a list of UI intents (the emulator's per-turn UI plan)."""
    if not isinstance(items, list):
        raise UiIntentError("UI intents must be a list")
    return [parse_ui_intent(item) for item in items]


# JSON schema for the emulator's structured UI output, suitable for codex
# `--output-schema`. Kept inline so the contract lives next to its validator.
UI_INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(UI_INTENT_TYPES)},
                    "target": {"type": "string"},
                    "value": {},
                    "note": {"type": "string"},
                },
                "required": ["type"],
            },
        }
    },
    "required": ["intents"],
}


# --------------------------------------------------------------------------- #
# Structured app report
# --------------------------------------------------------------------------- #
@dataclass
class AppProbe:
    """Result of probing one UI-producing tool's resource."""

    tool: str
    resource_uri: str
    resource: AppResource
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "resource_uri": self.resource_uri,
            "resource": self.resource.to_json(),
            "diagnostics": self.diagnostics,
        }


def probe_ui_tools(
    client: Any,
    tools: list[dict[str, Any]],
    only: Optional[set] = None,
) -> list[AppProbe]:
    """Fetch and diagnose the resource behind each UI-producing tool.

    ``client`` is any object exposing ``read_resource(uri)`` (an
    :class:`~rehearsal.mcp_client.McpClient`). ``only`` optionally restricts to a
    set of tool names. Each resource is fetched once even if shared by several
    tools. A fetch error is captured on the resource, never raised.
    """
    by_name = {t.get("name"): t for t in tools}
    probes: list[AppProbe] = []
    cache: dict[str, AppResource] = {}
    for ref in ui_tools(tools):
        tool_name = ref["tool"]
        if only is not None and tool_name not in only:
            continue
        uri = ref["resource_uri"]
        if uri not in cache:
            try:
                read_result = client.read_resource(uri)
                cache[uri] = parse_app_resource(uri, read_result)
            except Exception as exc:  # transport/JSON-RPC failure → record it
                cache[uri] = AppResource(uri=uri, fetch_error=str(exc))
        resource = cache[uri]
        probes.append(
            AppProbe(
                tool=tool_name,
                resource_uri=uri,
                resource=resource,
                diagnostics=diagnose_resource(resource, by_name.get(tool_name)),
            )
        )
    return probes


def build_app_report(target_id: str, probes: list[AppProbe]) -> dict[str, Any]:
    """Assemble the structured app report (spec's report sections).

    The render/host-bridge/interaction sections are marked ``pending`` until the
    browser-backed increment lands; their presence keeps the shape stable.
    """
    renderable = sum(1 for p in probes if p.resource.renderable)
    findings = sum(len(p.diagnostics) for p in probes)
    return {
        "target_id": target_id,
        "summary": {
            "ui_tools": len(probes),
            "renderable_resources": renderable,
            "diagnostic_findings": findings,
        },
        # Implemented sections.
        "ui_resources": [p.to_json() for p in probes],
        # Deferred sections (browser-backed increment).
        "host_bridge_transcript": {"status": "pending", "messages": []},
        "interaction_transcript": {"status": "pending", "events": []},
        "render_artifacts": {"status": "pending", "artifacts": []},
        "final_app_state": {"status": "pending"},
    }


def render_app_report_md(report: dict[str, Any]) -> str:
    """Render the app report as readable Markdown."""
    summary = report.get("summary", {})
    lines = [
        "# MCP Apps Report: %s" % report.get("target_id", "?"),
        "",
        "- UI tools: %d" % summary.get("ui_tools", 0),
        "- Renderable resources: %d" % summary.get("renderable_resources", 0),
        "- Diagnostic findings: %d" % summary.get("diagnostic_findings", 0),
        "",
        "## UI resources",
        "",
    ]
    probes = report.get("ui_resources", [])
    if not probes:
        lines.append("No UI-producing tools found.")
    for probe in probes:
        res = probe.get("resource", {})
        csp = res.get("csp", {})
        lines.append("### `%s` → `%s`" % (probe.get("tool"), probe.get("resource_uri")))
        lines.append("- mime: `%s`%s" % (
            res.get("mime_type", ""),
            "" if res.get("is_mcp_app") else " (not an mcp-app profile)",
        ))
        lines.append("- renderable: %s (%d bytes html)" % (
            res.get("renderable"), res.get("html_length", 0),
        ))
        lines.append("- csp connect: %s | resource: %s" % (
            csp.get("connectDomains") or [], csp.get("resourceDomains") or [],
        ))
        diags = probe.get("diagnostics", [])
        if diags:
            lines.append("- findings:")
            for d in diags:
                lines.append("  - **%s** (%s): %s" % (
                    d.get("severity"), d.get("kind"), d.get("message"),
                ))
        lines.append("")

    lines += [
        "## Deferred sections",
        "",
        "The following require a browser-backed widget host (next increment):",
        "",
        "- Host-bridge transcript: _pending_",
        "- Interaction transcript: _pending_",
        "- Render artifacts (screenshot/DOM): _pending_",
        "- Final app state: _pending_",
        "",
    ]
    return "\n".join(lines)
