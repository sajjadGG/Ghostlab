"""Tests for scenario config parsing of the optional expected_outcome block."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rehearsal.config import ConfigError, load_scenario

BASE = {
    "id": "s1",
    "title": "t",
    "persona": "p",
    "goal": "g",
    "max_turns": 3,
    "opening_message": "hi",
}


def _write(data: dict) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "scenario.json"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    return tmp


class ExpectedOutcomeConfigTest(unittest.TestCase):
    def test_absent_defaults_empty(self) -> None:
        scenario = load_scenario(_write(BASE))
        self.assertEqual(scenario.expected_outcome, {})

    def test_parses_full_block(self) -> None:
        data = {
            **BASE,
            "expected_outcome": {
                "must_include": ["7.5"],
                "must_not_include": ["error"],
                "expected_tool_args": [{"tool": "student_get_status", "arguments": {"id": "u1"}}],
            },
        }
        scenario = load_scenario(_write(data))
        self.assertEqual(scenario.expected_outcome["must_include"], ["7.5"])
        self.assertEqual(
            scenario.expected_outcome["expected_tool_args"],
            [{"tool": "student_get_status", "arguments": {"id": "u1"}}],
        )

    def test_tool_args_default_empty_arguments(self) -> None:
        data = {**BASE, "expected_outcome": {"expected_tool_args": [{"tool": "ping"}]}}
        scenario = load_scenario(_write(data))
        self.assertEqual(
            scenario.expected_outcome["expected_tool_args"], [{"tool": "ping", "arguments": {}}]
        )

    def test_rejects_non_object(self) -> None:
        data = {**BASE, "expected_outcome": ["nope"]}
        with self.assertRaises(ConfigError):
            load_scenario(_write(data))

    def test_rejects_tool_entry_without_tool_key(self) -> None:
        data = {**BASE, "expected_outcome": {"expected_tool_args": [{"arguments": {}}]}}
        with self.assertRaises(ConfigError):
            load_scenario(_write(data))


if __name__ == "__main__":
    unittest.main()
