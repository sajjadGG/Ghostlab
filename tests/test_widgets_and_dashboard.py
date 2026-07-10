"""Widget extraction for the emulator + the run HTML dashboard."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rehearsal.dashboard import build_dashboard, build_dashboard_data
from rehearsal.mcp_apps import widgets_from_tool_calls
from rehearsal.prompts import describe_widgets

# A UI-producing call whose result carries only a viewUUID in _meta but the
# real, human-readable payload in structured_content (observed from cortex).
WRITING_CALL = {
    "index": 1,
    "server": "cortex",
    "tool": "views_create_writing_practice",
    "status": "completed",
    "arguments": {},
    "result": {
        "content": [{"type": "text", "text": "Timed writing task ready."}],
        "_meta": {"viewUUID": "abc"},
        "structured_content": {
            "question_prompt": "Discuss the pros and cons of studying abroad.",
            "word_limit": 320,
            "sessionUUID": "internal-should-be-hidden",
        },
    },
}
PLAIN_CALL = {
    "index": 2,
    "server": "cortex",
    "tool": "memory_get",
    "status": "completed",
    "result": {"content": [{"type": "text", "text": "Memory v9 loaded."}]},
}


class WidgetExtractionTest(unittest.TestCase):
    def test_detects_ui_producing_tool_and_keeps_user_facing_fields(self) -> None:
        widgets = widgets_from_tool_calls([WRITING_CALL, PLAIN_CALL])
        self.assertEqual(len(widgets), 1)
        widget = widgets[0]
        self.assertEqual(widget["tool"], "views_create_writing_practice")
        self.assertIn("question_prompt", widget["fields"])
        self.assertEqual(widget["fields"]["word_limit"], 320)
        # Internal plumbing keys are stripped from what the user "sees".
        self.assertNotIn("sessionUUID", widget["fields"])
        self.assertIn("Timed writing task ready.", widget["text"])

    def test_plain_tool_call_is_not_a_widget(self) -> None:
        self.assertEqual(widgets_from_tool_calls([PLAIN_CALL]), [])

    def test_failed_ui_call_is_ignored(self) -> None:
        failed = {**WRITING_CALL, "status": "failed"}
        self.assertEqual(widgets_from_tool_calls([failed]), [])

    def test_describe_widgets_is_first_person_and_actionable(self) -> None:
        note = describe_widgets(widgets_from_tool_calls([WRITING_CALL]))
        self.assertIn("appeared on your screen", note)
        self.assertIn("Discuss the pros and cons", note)
        self.assertIn("actually do the exercise", note)

    def test_describe_widgets_empty(self) -> None:
        self.assertEqual(describe_widgets([]), "")


class DashboardTest(unittest.TestCase):
    def _make_run(self, root: Path) -> Path:
        run_dir = root / "run-1"
        run_dir.mkdir(parents=True)
        events = [
            {"type": "run_started", "data": {"turn": None, "scenario": {"goal": "Write an essay", "title": "Writing"}, "models": {"agent_under_test": "codex", "user_emulator": "codex"}, "persona": {"name": "Impatient Retaker"}}},
            {"type": "user_message", "data": {"turn": 1, "content": "Give me a task."}},
            {"type": "aut_result", "data": {"turn": 1, "output": "Here is a task.", "exit_code": 0, "tool_calls": [WRITING_CALL]}},
            {"type": "run_finished", "data": {"turn": None, "status": "completed"}},
        ]
        (run_dir / "events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
        )
        (run_dir / "verdict.json").write_text(
            json.dumps({"verdict": "pass", "judge": {"summary": "goal met"}, "gates": []}),
            encoding="utf-8",
        )
        results = {
            "id": "cortex-local",
            "generated_at": "2026-07-04T00:00:00Z",
            "hosts": [{"id": "codex-cli", "kind": "process"}],
            "totals": {"pass": 1, "fail": 0, "skip": 0, "error": 0},
            "pass_rate": 1.0,
            "results": [
                {
                    "case": "semantic-writing",
                    "suite": "semantic",
                    "host": "codex-cli",
                    "kind": "conversational",
                    "status": "pass",
                    "detail": "verdict=pass",
                    "duration_ms": 12000,
                    "artifacts": {"run_dir": str(run_dir)},
                }
            ],
        }
        (root / "results.json").write_text(json.dumps(results), encoding="utf-8")
        return root

    def test_build_data_reconstructs_turns_and_widgets(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._make_run(Path(tmp))
            data = build_dashboard_data(root)
            self.assertEqual(len(data["cases"]), 1)
            case = data["cases"][0]
            self.assertEqual(case["status"], "pass")
            self.assertEqual(case["meta"]["persona"], "Impatient Retaker")
            self.assertEqual(len(case["turns"]), 1)
            self.assertEqual(data["summary"]["tool_calls"], 1)
            self.assertEqual(data["summary"]["conversations"], 1)
            turn = case["turns"][0]
            self.assertEqual(turn["user"], "Give me a task.")
            self.assertEqual(len(turn["tool_calls"]), 1)
            # Widget reconstructed from the tool call even without a widgets_shown event.
            self.assertEqual(len(turn["widgets"]), 1)

    def test_build_dashboard_writes_self_contained_html(self) -> None:
        with TemporaryDirectory() as tmp:
            root = self._make_run(Path(tmp))
            out = build_dashboard(root)
            self.assertTrue(out.exists())
            html = out.read_text(encoding="utf-8")
            self.assertIn("semantic-writing", html)
            self.assertIn("interactive widget", html)
            self.assertIn("goal met", html)
            self.assertIn("Ghostlab evaluation report", html)
            self.assertIn("Search cases, goals, personas", html)
            self.assertIn("data-search=", html)
            # Self-contained: no external asset references.
            self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
            self.assertNotIn("src=", html)


if __name__ == "__main__":
    unittest.main()
