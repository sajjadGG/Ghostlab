"""Direct MCP protocol host: deterministic case execution without a model.

This host is what makes the smoke/edge suites CI-able — it speaks the protocol
itself, so a `tool_call` case passes or fails on the server's actual behavior
with zero model variance. Expectation semantics:

- ``no_error`` — the call must succeed and the result must not set
  ``isError``.
- ``graceful_error`` — the server must *reject* the call in-protocol (a
  JSON-RPC error object or an ``isError: true`` result). A transport crash,
  hang, or unhandled exception fails the case: bad input should never take
  the server down.

UI ``app_render`` cases execute when the optional Playwright-backed renderer
is installed; otherwise they skip with a reason instead of failing.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from ..config import TargetConfig
from ..mcp_client import McpClient, McpClientError, create_client
from .base import CaseResult, HostAdapter, HostCapabilities


class DirectMcpHost(HostAdapter):
    capabilities = HostCapabilities(
        executes_protocol=True,
        executes_ui=True,  # render-only, via the apps host when installed
        exposes_tool_trace=True,
    )

    def __init__(self, host_id: str, target: TargetConfig, timeout: float = 30.0) -> None:
        self.id = host_id
        self.kind = "direct-mcp"
        self.target = target
        self.timeout = timeout
        self._client: Optional[McpClient] = None

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #
    def open(self) -> None:
        if self._client is None:
            self._client = create_client(self.target, timeout=self.timeout)
            self._client.initialize()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def client(self) -> McpClient:
        if self._client is None:
            self.open()
        assert self._client is not None
        return self._client

    def version_info(self) -> dict[str, Any]:
        info = super().version_info()
        if self._client is not None and self._client.server_info:
            info["server"] = {
                "name": self._client.server_info.get("name", "?"),
                "version": self._client.server_info.get("version", "?"),
            }
        return info

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def execute(self, case: dict[str, Any], out_dir: Path) -> CaseResult:
        execution = case.get("execution", {}) or {}
        exec_type = str(execution.get("type", ""))
        started = time.monotonic()

        def done(status: str, detail: str = "", **artifacts: str) -> CaseResult:
            return CaseResult(
                case_id=case["id"],
                suite=case.get("suite", "?"),
                host=self.id,
                status=status,
                detail=detail,
                duration_ms=(time.monotonic() - started) * 1000,
                artifacts=dict(artifacts),
            )

        if execution.get("blocked"):
            return done("skip", str(execution["blocked"]))
        try:
            if exec_type in ("discovery", "host_smoke"):
                return self._run_discovery(done)
            if exec_type == "tool_call":
                return self._run_tool_call(execution, done)
            if exec_type == "app_render":
                return self._run_app_render(case, execution, out_dir, done)
        except McpClientError as exc:
            return done("error", f"transport failure: {exc}")
        return done("skip", f"direct-mcp cannot execute case type {exec_type!r}")

    def _run_discovery(self, done) -> CaseResult:
        tools = self.client.list_collection("tools/list", "tools")
        resources = self.client.list_collection("resources/list", "resources")
        prompts = self.client.list_collection("prompts/list", "prompts")
        if not tools:
            return done("fail", "server exposed no tools")
        return done(
            "pass",
            f"tools={len(tools)} resources={len(resources)} prompts={len(prompts)}",
        )

    def _run_tool_call(self, execution: dict[str, Any], done) -> CaseResult:
        tool = str(execution.get("tool", ""))
        arguments = dict(execution.get("arguments") or {})
        expect = execution.get("expect") or {}
        response = self.client.call_tool_raw(tool, arguments)

        protocol_error = response.error is not None
        result = response.result or {}
        tool_error = bool(result.get("isError"))

        if expect.get("graceful_error"):
            if protocol_error or tool_error:
                kind = "JSON-RPC error" if protocol_error else "isError result"
                return done("pass", f"rejected gracefully ({kind})")
            return done(
                "fail",
                "invalid input was accepted without an error — the server "
                "should reject it explicitly",
            )
        # Default / no_error expectation.
        if protocol_error:
            return done("fail", f"JSON-RPC error: {response.error}")
        if tool_error:
            first = ""
            for entry in result.get("content") or []:
                if isinstance(entry, dict) and entry.get("type") == "text":
                    first = str(entry.get("text", ""))[:200]
                    break
            return done("fail", f"isError=true for minimal valid arguments: {first}")
        return done("pass", "call succeeded")

    def _run_app_render(
        self, case: dict[str, Any], execution: dict[str, Any], out_dir: Path, done
    ) -> CaseResult:
        try:
            from ..apps_host import renderer as _renderer
            from ..apps_host.assertions import assertions_for, evaluate_assertions
        except ImportError:
            return done("skip", "apps host not importable; install ghostlab[apps]")
        if not _renderer.render_available():
            return done("skip", "Playwright not installed; install ghostlab[apps]")

        from ..mcp_apps import parse_app_resource

        tool = str(execution.get("tool", ""))
        uri = str(execution.get("resource", ""))
        resource = parse_app_resource(uri, self.client.read_resource(uri))
        if not resource.renderable:
            return done("fail", f"resource {uri} is not renderable: {resource.fetch_error or 'empty'}")

        out_dir.mkdir(parents=True, exist_ok=True)
        screenshot = out_dir / f"{case['id']}.png"
        render = _renderer.render_widget(
            uri=uri,
            widget_html=resource.html,
            tool_input={},
            tool_result=None,
            intents=[],
            screenshot_path=screenshot,
        )
        if render.error:
            return done("fail", f"render error: {render.error}")
        assertions = evaluate_assertions(assertions_for(uri), render.summary())
        failed = [a for a in assertions if not a["passed"]]
        artifacts = {"screenshot": str(screenshot)} if render.screenshot_path else {}
        if failed:
            return done(
                "fail",
                "; ".join(f"{a['name']}: {a['description']}" for a in failed[:3]),
                **artifacts,
            )
        return done("pass", f"{len(assertions)} assertion(s) passed", **artifacts)
