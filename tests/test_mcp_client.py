"""Unit tests for MCP client transports: SSE framing and stdio timeouts."""
from __future__ import annotations

import sys
import unittest

from rehearsal.mcp_client import McpClientError, StdioMcpClient, _parse_sse

# A server that reads a request and never answers.
HANG_SERVER = "import sys\nsys.stdin.readline()\nimport time\ntime.sleep(30)\n"

# A server that logs noise, emits a notification, then answers.
NOISY_SERVER = (
    "import sys, json\n"
    "line = sys.stdin.readline()\n"
    "msg = json.loads(line)\n"
    "print('starting up...')\n"  # non-JSON stdout noise
    "print(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/message'}))\n"
    "print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'ok': True}}))\n"
    "sys.stdout.flush()\n"
    "sys.stdin.read()\n"
)


class SseSelectionTest(unittest.TestCase):
    def test_picks_response_among_notifications(self) -> None:
        body = (
            'data: {"jsonrpc": "2.0", "method": "notifications/message", '
            '"params": {"level": "info"}}\n\n'
            'data: {"jsonrpc": "2.0", "id": 7, "result": {"ok": true}}\n\n'
        )
        message = _parse_sse(body, expected_id=7)
        self.assertEqual(message["result"], {"ok": True})

    def test_falls_back_to_last_response_shaped_message(self) -> None:
        body = (
            'data: {"jsonrpc": "2.0", "method": "notifications/message"}\n\n'
            'data: {"jsonrpc": "2.0", "id": 3, "error": {"code": -1}}\n\n'
        )
        message = _parse_sse(body)
        self.assertEqual(message["error"], {"code": -1})

    def test_empty_body_raises(self) -> None:
        with self.assertRaises(McpClientError):
            _parse_sse("event: message\n\n")


class StdioClientTest(unittest.TestCase):
    def test_unresponsive_server_times_out(self) -> None:
        client = StdioMcpClient(
            command=[sys.executable, "-c", HANG_SERVER], args=[], env={}, timeout=0.5
        )
        try:
            with self.assertRaises(McpClientError) as ctx:
                client._call("initialize", {})
            self.assertIn("did not answer", str(ctx.exception))
        finally:
            client.close()

    def test_skips_noise_and_notifications(self) -> None:
        client = StdioMcpClient(
            command=[sys.executable, "-c", NOISY_SERVER], args=[], env={}, timeout=5.0
        )
        try:
            response = client._call("initialize", {})
            self.assertEqual(response.result, {"ok": True})
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
