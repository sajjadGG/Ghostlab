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
from ..sandbox import SandboxError, normalize_sandbox, sandbox_stdio_target
from .base import CaseResult, HostAdapter, HostCapabilities


class DirectMcpHost(HostAdapter):
    capabilities = HostCapabilities(
        executes_protocol=True,
        executes_ui=True,  # render-only, via the apps host when installed
        exposes_tool_trace=True,
    )

    def __init__(
        self, host_id: str, target: TargetConfig, timeout: float = 30.0,
        sandbox: dict[str, Any] | None = None, base_dir: Path | None = None,
    ) -> None:
        self.id = host_id
        self.kind = "direct-mcp"
        self.target = target
        self.timeout = timeout
        self._client: Optional[McpClient] = None
        self._sandbox_config = normalize_sandbox(sandbox or {"backend": "local"}, base_dir)
        self._sandbox_session = None

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #
    def open(self) -> None:
        if self._client is None:
            runtime_target, self._sandbox_session = sandbox_stdio_target(
                self.target, self._sandbox_config, role="direct-mcp",
            )
            self._client = create_client(runtime_target, timeout=self.timeout)
            self._client.initialize()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._sandbox_session is not None:
            self._sandbox_session.close()
            self._sandbox_session = None

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
        self._sandbox_config["artifact_dir"] = str(out_dir)
        exec_type = str(execution.get("type", ""))
        started = time.monotonic()

        def done(status: str, detail: str = "", **artifacts: str) -> CaseResult:
            return CaseResult(
                case_id=case["id"],
                suite=case.get("suite", "?"),
                host=self.id,
                status=status,
                kind=case.get("kind", ""),
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
        except SandboxError as exc:
            return done("error", f"{exc.kind}: {exc.detail}")
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

        tool_input, tool_result, shell_reason = self._safe_tool_result_for_render(tool)

        out_dir.mkdir(parents=True, exist_ok=True)
        screenshot = out_dir / f"{case['id']}.png"
        render = _renderer.render_widget(
            uri=uri,
            widget_html=resource.html,
            tool_input=tool_input,
            tool_result=tool_result,
            intents=[],
            screenshot_path=screenshot,
        )
        if render.error:
            return done("fail", f"render error: {render.error}")

        assertions = assertions_for(uri)
        if tool_result is None:
            # Shell render: without tool data a widget legitimately shows no
            # controls or content-specific text. Keep only render-integrity
            # checks (handshake, body, console); data-driven assertions need
            # the full app loop (Phase A5) or a fixture-backed tool result.
            integrity = {"handshake_completed", "body_rendered", "no_console_errors"}
            assertions = [a for a in assertions if a.name in integrity]
        evaluated = evaluate_assertions(assertions, render.summary())
        failed = [a for a in evaluated if not a["passed"]]
        artifacts = {"screenshot": str(screenshot)} if render.screenshot_path else {}
        mode = f"shell render ({shell_reason})" if tool_result is None else "full render"
        if failed:
            return done(
                "fail",
                f"{mode}: " + "; ".join(f"{a['name']}: {a['description']}" for a in failed[:3]),
                **artifacts,
            )
        return done("pass", f"{mode}: {len(evaluated)} assertion(s) passed", **artifacts)

    def _safe_tool_result_for_render(
        self, tool_name: str
    ) -> tuple[dict[str, Any], Optional[dict[str, Any]], str]:
        """A real tool result to hydrate the widget — only when calling is safe.

        Returns (tool_input, tool_result, reason-if-shell). Mirrors the
        sampling safety model: read-only classification plus generatable
        arguments; anything else renders as a shell.
        """
        from ..contract import classify_tool_risk
        from ..sampling import ArgGenerationError, generate_arguments

        tool = self._tool_by_name(tool_name)
        if tool is None:
            return {}, None, f"tool {tool_name!r} not found in tools/list"
        risk = classify_tool_risk(tool)
        if risk.get("read_only") is not True:
            return {}, None, "no tool result: tool is not classified read-only"
        try:
            arguments = generate_arguments(tool)
        except ArgGenerationError as exc:
            return {}, None, f"no tool result: {exc}"
        try:
            result = self.client.call_tool(tool_name, arguments)
        except McpClientError as exc:
            return {}, None, f"no tool result: call failed ({exc})"
        if isinstance(result, dict) and result.get("isError"):
            return {}, None, "no tool result: tool returned isError"
        return arguments, result, ""

    def _tool_by_name(self, name: str) -> Optional[dict[str, Any]]:
        if not hasattr(self, "_tools_cache"):
            self._tools_cache = {
                tool.get("name"): tool
                for tool in self.client.list_collection("tools/list", "tools")
            }
        return self._tools_cache.get(name)
