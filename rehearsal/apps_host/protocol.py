"""MCP Apps host-bridge protocol: the JSON-RPC-over-postMessage contract.

Reverse-engineered from the live Cortex widget bundle (the MCP Apps SDK, protocol
``2026-01-26``). A widget loaded in an iframe drives the handshake:

1. widget → host: ``ui/initialize`` request (``{appInfo, appCapabilities, ...}``)
2. host → widget: result ``{protocolVersion, hostInfo, hostCapabilities, hostContext}``
3. widget → host: ``ui/notifications/initialized``
4. host → widget: ``ui/notifications/tool-input`` (``{arguments}``) and
   ``ui/notifications/tool-result`` (the MCP tool result) — this is the data the
   widget renders from.
5. widget → host: ``ui/notifications/size-changed``, and on-demand requests
   (``ui/request-display-mode``, ``ui/open-link``, ``ui/download-file``,
   ``ui/request-teardown``) which the host acknowledges.

This module builds the host page (the bridge JS), the initialize result, and
classifies a recorded transcript — all without a browser.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

PROTOCOL_VERSION = "2026-01-26"

# Widget → host.
UI_INITIALIZE = "ui/initialize"
NOTIF_INITIALIZED = "ui/notifications/initialized"
NOTIF_SIZE_CHANGED = "ui/notifications/size-changed"
REQ_DISPLAY_MODE = "ui/request-display-mode"
REQ_TEARDOWN = "ui/request-teardown"
NOTIF_REQUEST_TEARDOWN = "ui/notifications/request-teardown"
OPEN_LINK = "ui/open-link"
DOWNLOAD_FILE = "ui/download-file"
UPDATE_MODEL_CONTEXT = "ui/update-model-context"

# Host → widget.
NOTIF_TOOL_INPUT = "ui/notifications/tool-input"
NOTIF_TOOL_RESULT = "ui/notifications/tool-result"
NOTIF_HOST_CONTEXT_CHANGED = "ui/notifications/host-context-changed"

DEFAULT_HOST_INFO = {"name": "ghostlab", "version": "0.1.0"}
DEFAULT_HOST_CONTEXT = {"theme": "light", "displayMode": "inline"}


def build_initialize_result(
    host_info: Optional[dict] = None,
    host_context: Optional[dict] = None,
    host_capabilities: Optional[dict] = None,
) -> dict[str, Any]:
    """Build the host's ``ui/initialize`` result payload."""
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "hostInfo": host_info or dict(DEFAULT_HOST_INFO),
        "hostCapabilities": host_capabilities or {},
        "hostContext": host_context or dict(DEFAULT_HOST_CONTEXT),
    }


# --------------------------------------------------------------------------- #
# Host page (the bridge)
# --------------------------------------------------------------------------- #
# The widget HTML and data are NOT inlined (widget bundles contain `</script>`);
# the renderer injects them via `window.__ghostlabMount(html, args, result)`.
_HOST_PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>html,body{{margin:0;padding:0}}</style></head>
<body>
<iframe id="ghostlab-widget" sandbox="allow-scripts"
        style="width:{width}px;height:{height}px;border:0;display:block"></iframe>
<script>
const PROTOCOL = {protocol};
const INIT_RESULT = {init_result};
window.__ghostlabTrace = [];
function __record(direction, msg) {{ window.__ghostlabTrace.push({{direction, msg}}); }}
window.__ghostlabMount = function (html, args, result) {{
  const frame = document.getElementById("ghostlab-widget");
  function post(msg) {{ frame.contentWindow.postMessage(msg, "*"); __record("host->widget", msg); }}
  window.addEventListener("message", function (event) {{
    const msg = event.data;
    if (!msg || msg.jsonrpc !== "2.0") return;
    __record("widget->host", msg);
    if (msg.method === "{ui_initialize}") {{
      post({{jsonrpc: "2.0", id: msg.id, result: INIT_RESULT}});
    }} else if (msg.method === "{notif_initialized}") {{
      if (args) post({{jsonrpc: "2.0", method: "{notif_tool_input}", params: {{arguments: args}}}});
      if (result) post({{jsonrpc: "2.0", method: "{notif_tool_result}", params: result}});
    }} else if (msg.id !== undefined && msg.id !== null && typeof msg.method === "string") {{
      // Acknowledge any other widget request (display-mode, open-link, ...).
      post({{jsonrpc: "2.0", id: msg.id, result: {{}}}});
    }}
  }});
  frame.srcdoc = html;
}};
</script>
</body></html>"""


def build_host_page(width: int = 640, height: int = 560) -> str:
    """Return the static host-page HTML implementing the MCP Apps bridge."""
    return _HOST_PAGE_TEMPLATE.format(
        width=int(width),
        height=int(height),
        protocol=json.dumps(PROTOCOL_VERSION),
        init_result=json.dumps(build_initialize_result()),
        ui_initialize=UI_INITIALIZE,
        notif_initialized=NOTIF_INITIALIZED,
        notif_tool_input=NOTIF_TOOL_INPUT,
        notif_tool_result=NOTIF_TOOL_RESULT,
    )


# --------------------------------------------------------------------------- #
# Transcript classification
# --------------------------------------------------------------------------- #
@dataclass
class BridgeMessage:
    """One classified host-bridge message from a recorded transcript."""

    direction: str  # "widget->host" | "host->widget"
    kind: str       # "request" | "response" | "notification" | "error"
    method: Optional[str]
    id: Any = None

    def to_json(self) -> dict[str, Any]:
        return {"direction": self.direction, "kind": self.kind, "method": self.method, "id": self.id}


def _classify_one(direction: str, msg: dict) -> BridgeMessage:
    method = msg.get("method")
    has_id = msg.get("id") is not None
    if "error" in msg:
        kind = "error"
    elif "result" in msg:
        kind = "response"
    elif method and has_id:
        kind = "request"
    else:
        kind = "notification"
    return BridgeMessage(direction=direction, kind=kind, method=method, id=msg.get("id"))


def classify_transcript(raw: list[dict]) -> list[BridgeMessage]:
    """Classify a recorded ``window.__ghostlabTrace`` into BridgeMessages."""
    out: list[BridgeMessage] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        msg = entry.get("msg")
        if not isinstance(msg, dict):
            continue
        out.append(_classify_one(entry.get("direction", "?"), msg))
    return out


def handshake_completed(messages: list[BridgeMessage]) -> bool:
    """True when the widget initialized and the host delivered the tool result.

    This is the spec's "did the UI render" host-bridge signal: the widget ran the
    ``ui/initialize`` handshake and the host pushed tool data to it.
    """
    widget_initialized = any(
        m.direction == "widget->host" and m.method == NOTIF_INITIALIZED for m in messages
    )
    host_delivered = any(
        m.direction == "host->widget" and m.method in (NOTIF_TOOL_INPUT, NOTIF_TOOL_RESULT)
        for m in messages
    )
    return widget_initialized and host_delivered
