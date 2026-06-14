"""Tests for tool-usability critique collection + rendering (no codex)."""
from __future__ import annotations

import unittest

from rehearsal.critique import (
    collect_exercised_tools,
    critique_prompt,
    render_critique_md,
)

RUN = {
    "scenario": {"id": "s1", "goal": "check status"},
    "transcript": [
        {"role": "user", "content": "how am I doing?"},
        {"role": "assistant", "content": "Let me check."},
    ],
    "tool_calls": [
        {"server": "cortex", "tool": "student_get_status", "status": "completed",
         "arguments": {"id": "u1"}, "error": None},
        {"server": "cortex", "tool": "student_get_status", "status": "failed",
         "arguments": {"id": "bad"}, "error": {"message": "not found"}},
        {"server": "cortex", "tool": "kb_find", "status": "completed",
         "arguments": {"q": "x"}, "error": None},
    ],
}

INSPECT = {
    "tools": [
        {
            "name": "student_get_status",
            "description": "Return the student's status.",
            "inputSchema": {"properties": {"id": {}}, "required": ["id"]},
        }
    ]
}


class CollectExercisedToolsTest(unittest.TestCase):
    def test_groups_calls_by_tool(self) -> None:
        tools = collect_exercised_tools(RUN, INSPECT)
        by_name = {t["name"]: t for t in tools}
        self.assertEqual(set(by_name), {"student_get_status", "kb_find"})
        self.assertEqual(len(by_name["student_get_status"]["calls"]), 2)
        self.assertEqual(len(by_name["kb_find"]["calls"]), 1)

    def test_pairs_definition_when_known(self) -> None:
        tools = collect_exercised_tools(RUN, INSPECT)
        known = next(t for t in tools if t["name"] == "student_get_status")
        self.assertTrue(known["known"])
        self.assertEqual(known["description"], "Return the student's status.")
        self.assertEqual(known["params"], "id")

    def test_flags_tool_absent_from_inspect(self) -> None:
        tools = collect_exercised_tools(RUN, INSPECT)
        unknown = next(t for t in tools if t["name"] == "kb_find")
        self.assertFalse(unknown["known"])

    def test_works_without_inspect(self) -> None:
        tools = collect_exercised_tools(RUN, None)
        self.assertEqual(len(tools), 2)
        self.assertTrue(all(not t["known"] for t in tools))

    def test_no_tool_calls(self) -> None:
        tools = collect_exercised_tools({"tool_calls": []}, INSPECT)
        self.assertEqual(tools, [])


class CritiquePromptTest(unittest.TestCase):
    def test_prompt_includes_evidence(self) -> None:
        prompt = critique_prompt(RUN, INSPECT)
        self.assertIn("student_get_status", prompt)
        self.assertIn("Return the student's status.", prompt)
        self.assertIn("not found", prompt)  # observed error surfaces
        self.assertIn("NOT in the inspected server definition", prompt)  # kb_find flagged


class RenderCritiqueMdTest(unittest.TestCase):
    def test_renders_score_and_findings(self) -> None:
        critique = {
            "scenario": "s1",
            "exercised_tools": ["student_get_status"],
            "critique": {
                "overall_score": 3,
                "overall_notes": "Decent but improvable.",
                "top_recommendations": ["Add units to params."],
                "tools": [
                    {
                        "name": "student_get_status",
                        "name_clarity": 4,
                        "description_quality": "adequate",
                        "param_issues": ["`id` format undocumented"],
                        "error_quality": "unclear",
                        "suggestions": ["Document id format"],
                    }
                ],
            },
        }
        md = render_critique_md(critique)
        self.assertIn("**3/5**", md)
        self.assertIn("Add units to params.", md)
        self.assertIn("`id` format undocumented", md)
        self.assertIn("Document id format", md)


if __name__ == "__main__":
    unittest.main()
