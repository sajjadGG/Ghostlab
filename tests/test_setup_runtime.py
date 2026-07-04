"""Unit tests for the spec setup runtime (commands, health, reset, teardown)."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from rehearsal.setup_runtime import SetupError, SetupRuntime, environment_fingerprint


class SetupRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_foreground_command_runs_and_logs(self) -> None:
        flag = self.tmp / "prepared.flag"
        setup = {"commands": [{"id": "prepare", "command": ["touch", str(flag)]}]}
        with SetupRuntime(setup, self.tmp / "logs") as runtime:
            self.assertTrue(flag.exists())
            status = runtime.status()
        self.assertTrue(status["commands"][0]["ok"])
        self.assertEqual(status["commands"][0]["exit_code"], 0)

    def test_failing_foreground_command_raises_with_detail(self) -> None:
        setup = {
            "commands": [
                {
                    "id": "boom",
                    "command": [sys.executable, "-c", "import sys; print('nope', file=sys.stderr); sys.exit(3)"],
                }
            ]
        }
        runtime = SetupRuntime(setup, self.tmp / "logs")
        with self.assertRaises(SetupError) as ctx:
            runtime.start()
        self.assertIn("exited 3", str(ctx.exception))
        self.assertIn("nope", str(ctx.exception))
        runtime.teardown()

    def test_background_command_is_terminated_on_teardown(self) -> None:
        setup = {
            "commands": [
                {
                    "id": "server",
                    "command": [sys.executable, "-c", "import time; time.sleep(60)"],
                    "background": True,
                }
            ]
        }
        runtime = SetupRuntime(setup, self.tmp / "logs")
        runtime.start()
        _, proc = runtime._background[0]
        self.assertIsNone(proc.poll())  # alive
        runtime.teardown()
        self.assertIsNotNone(proc.poll())  # stopped

    def test_command_health_check_polls_until_pass(self) -> None:
        flag = self.tmp / "ready.flag"
        # A background "server" that becomes ready after ~0.3s.
        setup = {
            "commands": [
                {
                    "id": "slow-server",
                    "command": [
                        sys.executable, "-c",
                        f"import time, pathlib; time.sleep(0.3); "
                        f"pathlib.Path({str(flag)!r}).touch(); time.sleep(30)",
                    ],
                    "background": True,
                }
            ],
            "health": [
                {"type": "command", "command": ["test", "-f", str(flag)],
                 "timeout_seconds": 5, "interval_seconds": 0.05}
            ],
        }
        with SetupRuntime(setup, self.tmp / "logs") as runtime:
            started = time.monotonic()
            self.assertTrue(runtime.wait_healthy())
            self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_health_check_times_out(self) -> None:
        setup = {
            "health": [
                {"type": "command", "command": ["test", "-f", str(self.tmp / "never.flag")],
                 "timeout_seconds": 0.3, "interval_seconds": 0.05}
            ]
        }
        with SetupRuntime(setup, self.tmp / "logs") as runtime:
            self.assertFalse(runtime.wait_healthy())
            self.assertFalse(runtime.status()["health"][0]["ok"])

    def test_tcp_health_check(self) -> None:
        import socket

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            setup = {"health": [{"type": "tcp", "host": "127.0.0.1", "port": port,
                                 "timeout_seconds": 2}]}
            with SetupRuntime(setup, self.tmp / "logs") as runtime:
                self.assertTrue(runtime.wait_healthy())
        finally:
            server.close()

    def test_reset_tool_hook_uses_client(self) -> None:
        calls = []

        class FakeClient:
            def call_tool(self, name, arguments):
                calls.append((name, arguments))
                return {}

        setup = {"reset": [{"type": "tool", "name": "test_reset_state", "arguments": {"scope": "all"}}]}
        runtime = SetupRuntime(setup, self.tmp / "logs")
        self.assertTrue(runtime.run_reset(FakeClient()))
        self.assertEqual(calls, [("test_reset_state", {"scope": "all"})])

    def test_optional_reset_failure_is_tolerated(self) -> None:
        class FailingClient:
            def call_tool(self, name, arguments):
                raise RuntimeError("no such tool")

        setup = {
            "reset": [
                {"type": "tool", "name": "missing_tool", "optional": True},
            ]
        }
        runtime = SetupRuntime(setup, self.tmp / "logs")
        self.assertTrue(runtime.run_reset(FailingClient()))
        setup["reset"][0]["optional"] = False
        runtime = SetupRuntime(setup, self.tmp / "logs")
        self.assertFalse(runtime.run_reset(FailingClient()))

    def test_teardown_results_and_fingerprint_in_status(self) -> None:
        setup = {
            "commands": [{"id": "noop", "command": ["true"]}],
            "teardown": [{"id": "clean", "command": ["true"]}],
        }
        runtime = SetupRuntime(setup, self.tmp / "logs")
        runtime.start()
        runtime.teardown()
        path = runtime.write_status({"name": "srv", "version": "1.2"})
        status = runtime.status()
        self.assertTrue(status["teardown"][0]["ok"])
        self.assertTrue(path.exists())
        fingerprint = environment_fingerprint({"name": "srv", "version": "1.2"})
        self.assertEqual(fingerprint["server"], {"name": "srv", "version": "1.2"})


if __name__ == "__main__":
    unittest.main()
