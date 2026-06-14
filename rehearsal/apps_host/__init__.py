"""MCP Apps host layer — render and interact with `ui://` widgets.

Increment 2 of issue #13. Where ``rehearsal.mcp_apps`` (increment 1) detects and
diagnoses widget resources without a browser, this package actually **renders**
them: it implements the MCP Apps host bridge (JSON-RPC over ``postMessage``),
mounts a widget in a headless-Chrome iframe, feeds it the tool input/result,
captures render proof (screenshot, DOM, console/network errors, bridge
transcript), executes structured UI intents, and runs app-aware assertions.

The protocol/transcript/executor/assertion logic is pure Python (no browser);
only :mod:`rehearsal.apps_host.renderer` needs Playwright + Chrome (the optional
``ghostlab[apps]`` extra).
"""
from __future__ import annotations

from .protocol import (
    PROTOCOL_VERSION,
    BridgeMessage,
    build_host_page,
    build_initialize_result,
    classify_transcript,
)

__all__ = [
    "PROTOCOL_VERSION",
    "BridgeMessage",
    "build_host_page",
    "build_initialize_result",
    "classify_transcript",
]
