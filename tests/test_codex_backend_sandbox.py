from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from rehearsal.codex_backend import CodexBackend


class FakeSandbox:
    instances = []

    def __init__(self, config, role):
        self.config = config
        self.role = role
        self.commands = []
        self.closed = False
        self.__class__.instances.append(self)

    def exec(self, command, *, input_text, env, timeout):
        self.commands.append((command, input_text, env, timeout))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def download(self, source: str, destination: Path) -> None:
        destination.write_text(json.dumps({"answer": "ok"}), encoding="utf-8")

    def close(self) -> None:
        self.closed = True


class SandboxedCodexBackendTest(unittest.TestCase):
    def test_generation_runs_and_retrieves_output_inside_openshell(self) -> None:
        FakeSandbox.instances.clear()
        backend = CodexBackend(
            model="gpt-test", sandbox={"backend": "openshell", "bin": "openshell"}
        )
        with patch("rehearsal.sandbox.OpenShellSandbox", FakeSandbox):
            result = backend.generate_json("prompt", {"type": "object"})
        self.assertEqual(result, {"answer": "ok"})
        sandbox = FakeSandbox.instances[0]
        command = sandbox.commands[0][0]
        self.assertEqual(command[0], "codex")
        self.assertIn("/sandbox/schema.json", command)
        self.assertIn("/sandbox/last.json", command)
        self.assertTrue(sandbox.closed)
