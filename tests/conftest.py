from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ghostlab import Agent, Sandbox

FAKES = Path(__file__).parent / "fakes"
FAKE_AGENT = FAKES / "fake_agent.py"


def executable(path: Path, script: Path) -> Path:
    """A shell wrapper so a harness `bin` can point at a Python fake."""
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def local_agent() -> Agent:
    """A `command`-harness agent that runs the fake CLI on the host."""
    return Agent(harness="command", name="fake",
                 options={"command": [sys.executable, str(FAKE_AGENT)]},
                 sandbox=Sandbox(backend="local"), timeout=30)


@pytest.fixture
def fake_claude(tmp_path: Path) -> Path:
    return executable(tmp_path / "claude", FAKE_AGENT)


@pytest.fixture
def openshell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "openshell-state"
    monkeypatch.setenv("FAKE_OPENSHELL_ROOT", str(root))
    binary = executable(tmp_path / "openshell", FAKES / "fake_openshell.py")

    class Fake:
        bin = str(binary)

        @staticmethod
        def calls() -> list[list[str]]:
            path = root / "calls.jsonl"
            lines = path.read_text().splitlines() if path.exists() else []
            return [json.loads(line) for line in lines]

        @staticmethod
        def boxes() -> list[Path]:
            return list((root / "boxes").iterdir()) if (root / "boxes").exists() else []

    return Fake()


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Counts fake-agent invocations (one line per call)."""
    path = tmp_path / "agent-calls.txt"
    monkeypatch.setenv("FAKE_CALLS", str(path))
    return lambda: path.read_text().splitlines() if path.exists() else []
