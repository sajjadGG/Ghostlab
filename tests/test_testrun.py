"""Unit tests for host adapters and the test-plan executor."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rehearsal.config import TargetConfig
from rehearsal.hosts import CaseResult, DirectMcpHost, HostAdapter, HostCapabilities
from rehearsal.mcp_client import McpResponse
from rehearsal.testrun import evaluate_gates, execute_plan, select_cases


def _case(case_id: str, suite: str, kind: str, execution: dict, status: str = "proposed") -> dict:
    return {"id": case_id, "suite": suite, "kind": kind, "title": case_id,
            "reason": "r", "tools": [], "status": status, "execution": execution}


def _plan(cases: list[dict]) -> dict:
    return {"id": "t", "generated_at": "now", "cases": cases}


class FakeClient:
    """Just enough of McpClient for DirectMcpHost."""

    server_info = {"name": "fake", "version": "1"}

    def __init__(self, tool_responses: dict | None = None, tools: int = 2) -> None:
        self.tool_responses = tool_responses or {}
        self.tools = tools
        self.calls: list = []

    def list_collection(self, method: str, key: str) -> list:
        if key == "tools":
            return [{"name": f"tool_{i}"} for i in range(self.tools)]
        return []

    def call_tool_raw(self, name: str, arguments: dict) -> McpResponse:
        self.calls.append((name, arguments))
        return self.tool_responses.get(name, McpResponse(result={}))

    def close(self) -> None:
        pass


def _direct_host(client: FakeClient) -> DirectMcpHost:
    target = TargetConfig(id="t", transport="stdio", connection={"command": ["x"]})
    host = DirectMcpHost("direct-mcp", target)
    host._client = client  # inject; open() would spawn a process
    return host


class DirectHostTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_discovery_passes_with_tools(self) -> None:
        host = _direct_host(FakeClient(tools=3))
        result = host.execute(_case("smoke-discovery", "smoke", "protocol",
                                    {"type": "discovery"}), self.out)
        self.assertEqual(result.status, "pass")
        self.assertIn("tools=3", result.detail)

    def test_discovery_fails_without_tools(self) -> None:
        host = _direct_host(FakeClient(tools=0))
        result = host.execute(_case("smoke-discovery", "smoke", "protocol",
                                    {"type": "discovery"}), self.out)
        self.assertEqual(result.status, "fail")

    def test_tool_call_no_error_semantics(self) -> None:
        client = FakeClient(tool_responses={
            "ok_tool": McpResponse(result={"content": [{"type": "text", "text": "hi"}]}),
            "err_tool": McpResponse(result={"isError": True,
                                            "content": [{"type": "text", "text": "denied"}]}),
        })
        host = _direct_host(client)
        ok = host.execute(_case("c1", "smoke", "protocol",
                                {"type": "tool_call", "tool": "ok_tool", "arguments": {},
                                 "expect": {"no_error": True}}), self.out)
        self.assertEqual(ok.status, "pass")
        err = host.execute(_case("c2", "smoke", "protocol",
                                 {"type": "tool_call", "tool": "err_tool", "arguments": {},
                                  "expect": {"no_error": True}}), self.out)
        self.assertEqual(err.status, "fail")
        self.assertIn("denied", err.detail)

    def test_tool_call_graceful_error_semantics(self) -> None:
        client = FakeClient(tool_responses={
            "rejects": McpResponse(error={"code": -32602, "message": "invalid params"}),
            "is_error": McpResponse(result={"isError": True}),
            "accepts": McpResponse(result={}),
        })
        host = _direct_host(client)
        expect = {"graceful_error": True}
        for tool, expected in (("rejects", "pass"), ("is_error", "pass"), ("accepts", "fail")):
            result = host.execute(_case(tool, "edge", "protocol",
                                        {"type": "tool_call", "tool": tool,
                                         "arguments": {}, "expect": expect}), self.out)
            self.assertEqual(result.status, expected, tool)

    def test_blocked_case_skips(self) -> None:
        host = _direct_host(FakeClient())
        result = host.execute(_case("b", "smoke", "protocol",
                                    {"type": "tool_call", "tool": "x",
                                     "blocked": "cannot generate arguments"}), self.out)
        self.assertEqual(result.status, "skip")


class StubHost(HostAdapter):
    def __init__(self, host_id: str, caps: HostCapabilities, status: str = "pass") -> None:
        self.id = host_id
        self.kind = "stub"
        self.capabilities = caps
        self.status = status

    def execute(self, case: dict, out_dir) -> CaseResult:
        return CaseResult(case_id=case["id"], suite=case["suite"], host=self.id,
                          status=self.status)


class ExecutePlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_select_cases_filters(self) -> None:
        plan = _plan([
            _case("a", "smoke", "protocol", {}, status="approved"),
            _case("b", "smoke", "protocol", {}, status="rejected"),
            _case("c", "edge", "protocol", {}, status="proposed"),
        ])
        self.assertEqual([c["id"] for c in select_cases(plan)], ["a", "c"])
        self.assertEqual([c["id"] for c in select_cases(plan, approved_only=True)], ["a"])
        self.assertEqual([c["id"] for c in select_cases(plan, suites=["edge"])], ["c"])

    def test_routes_by_capability_and_reports_skips(self) -> None:
        protocol_host = StubHost("p", HostCapabilities(executes_protocol=True))
        plan = _plan([
            _case("proto", "smoke", "protocol", {"type": "discovery"}),
            _case("conv", "semantic", "conversational", {"type": "scenario"}),
        ])
        results = execute_plan(plan, [protocol_host], self.out)
        by_case = {entry["case"]: entry for entry in results["results"]}
        self.assertEqual(by_case["proto"]["status"], "pass")
        self.assertEqual(by_case["conv"]["status"], "skip")
        self.assertIn("does not execute conversational", by_case["conv"]["detail"])
        self.assertEqual(results["totals"], {"pass": 1, "fail": 0, "skip": 1, "error": 0})
        self.assertEqual(results["pass_rate"], 1.0)

    def test_host_smoke_targets_named_host_only(self) -> None:
        host_a = StubHost("a", HostCapabilities(executes_protocol=True))
        host_b = StubHost("b", HostCapabilities(executes_protocol=True))
        plan = _plan([
            _case("host-compat-b-smoke", "host-compat", "protocol",
                  {"type": "host_smoke", "host": "b"}),
            _case("host-compat-missing-smoke", "host-compat", "protocol",
                  {"type": "host_smoke", "host": "gone"}),
        ])
        results = execute_plan(plan, [host_a, host_b], self.out)
        by_case = {entry["case"]: entry for entry in results["results"]}
        self.assertEqual(by_case["host-compat-b-smoke"]["host"], "b")
        self.assertEqual(by_case["host-compat-missing-smoke"]["status"], "skip")

    def test_host_exception_becomes_error_result(self) -> None:
        class ExplodingHost(StubHost):
            def execute(self, case, out_dir):
                raise RuntimeError("kaboom")

        host = ExplodingHost("x", HostCapabilities(executes_protocol=True))
        results = execute_plan(_plan([_case("c", "smoke", "protocol", {})]), [host], self.out)
        self.assertEqual(results["results"][0]["status"], "error")
        self.assertIn("kaboom", results["results"][0]["detail"])

    def test_resume_keeps_completed_and_retries_harness_errors(self) -> None:
        class CountingHost(StubHost):
            def __init__(self):
                super().__init__("p", HostCapabilities(executes_protocol=True))
                self.calls = []

            def execute(self, case, out_dir):
                self.calls.append(case["id"])
                return super().execute(case, out_dir)

        host = CountingHost()
        plan = _plan([
            _case("done", "smoke", "protocol", {}),
            _case("retry", "smoke", "protocol", {}),
        ])
        prior = {
            "results": [
                {"case": "done", "suite": "smoke", "kind": "protocol", "host": "p", "status": "pass"},
                {"case": "retry", "suite": "smoke", "kind": "protocol", "host": "p", "status": "harness_error"},
            ]
        }
        checkpoint = self.out / "results.partial.json"
        results = execute_plan(
            plan, [host], self.out, resume_results=prior, checkpoint_path=checkpoint,
        )
        self.assertEqual(host.calls, ["retry"])
        self.assertEqual(results["totals"]["pass"], 2)
        self.assertTrue(checkpoint.exists())

    def test_harness_error_is_excluded_from_pass_rate(self) -> None:
        host = StubHost("p", HostCapabilities(executes_protocol=True), status="harness_error")
        results = execute_plan(_plan([_case("c", "smoke", "protocol", {})]), [host], self.out)
        self.assertEqual(results["executed"], 0)
        self.assertIsNone(results["pass_rate"])
        self.assertEqual(results["totals"]["harness_error"], 1)

    def test_repeat_aggregates_variance_and_detects_flakes(self) -> None:
        from rehearsal.testrun import execute_plan_repeated

        class FlakyHost(StubHost):
            def __init__(self):
                super().__init__("f", HostCapabilities(executes_protocol=True))
                self.calls = 0

            def execute(self, case, out_dir):
                self.calls += 1
                status = "pass" if self.calls % 2 == 1 else "fail"
                return CaseResult(case_id=case["id"], suite=case["suite"],
                                  host=self.id, status=status)

        plan = _plan([_case("c", "smoke", "protocol", {"type": "discovery"})])
        results = execute_plan_repeated(plan, [FlakyHost()], self.out, repeat=4)
        self.assertEqual(results["attempts"], 4)
        self.assertEqual(results["totals"], {"pass": 2, "fail": 2, "skip": 0, "error": 0})
        self.assertEqual(results["pass_rate"], 0.5)
        variance = results["variance"]
        self.assertEqual(variance["flaky_cases"], ["c@f"])
        stats = variance["per_case"][0]
        self.assertEqual((stats["runs"], stats["pass"], stats["fail"]), (4, 2, 2))
        self.assertTrue(stats["flaky"])
        # Every merged result knows which attempt produced it.
        self.assertEqual({entry["attempt"] for entry in results["results"]}, {1, 2, 3, 4})

    def test_repeat_one_keeps_single_run_shape(self) -> None:
        from rehearsal.testrun import execute_plan_repeated

        host = StubHost("p", HostCapabilities(executes_protocol=True))
        plan = _plan([_case("c", "smoke", "protocol", {})])
        results = execute_plan_repeated(plan, [host], self.out, repeat=1)
        self.assertNotIn("attempts", results)
        self.assertNotIn("variance", results)

    def test_stable_results_are_not_flaky(self) -> None:
        from rehearsal.testrun import execute_plan_repeated

        host = StubHost("p", HostCapabilities(executes_protocol=True), status="fail")
        plan = _plan([_case("c", "smoke", "protocol", {})])
        results = execute_plan_repeated(plan, [host], self.out, repeat=3)
        self.assertEqual(results["variance"]["flaky_cases"], [])
        self.assertEqual(results["pass_rate"], 0.0)

    def test_evaluate_gates(self) -> None:
        results = {"executed": 10, "pass_rate": 0.8,
                   "totals": {"pass": 8, "fail": 2, "error": 0, "skip": 0}}
        failures = evaluate_gates(results, {"min_pass_rate": 0.9})
        self.assertEqual(len(failures), 1)
        self.assertIn("min_pass_rate", failures[0])
        self.assertEqual(evaluate_gates(results, {"min_pass_rate": 0.7}), [])
        # No executed cases -> gate does not fire on an empty run.
        empty = {"executed": 0, "pass_rate": None,
                 "totals": {"pass": 0, "fail": 0, "error": 0, "skip": 3}}
        self.assertEqual(evaluate_gates(empty, {"min_pass_rate": 0.9}), [])


if __name__ == "__main__":
    unittest.main()
