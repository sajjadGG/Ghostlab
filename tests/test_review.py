"""Tests for dataset review/curation logic (no codex needed)."""
from __future__ import annotations

import unittest

from rehearsal.review import build_review, ensure_statuses, set_statuses

PROFILE = {"taxonomy": {"memory": ["memory_get", "memory_put"], "student": ["student_get_status"]}}


def _dataset():
    manifest = {
        "name": "d",
        "mcp": "x@1",
        "seed": 0,
        "cases": [
            {"id": "c1", "persona": "p1", "scenario": "s1", "intent": "happy_path"},
            {"id": "c2", "persona": "p2", "scenario": "s2", "intent": "edge_case"},
        ],
    }
    personas = {
        "p1": {"id": "p1", "summary": "P1", "traits": ["terse"]},
        "p2": {"id": "p2", "summary": "P2", "traits": []},
        "p3": {"id": "p3", "summary": "orphan", "traits": []},  # no case -> flagged
    }
    scenarios = {
        "s1": {"id": "s1", "goal": "do a thing", "opening_message": "hi", "exercises": ["memory_get"]},
        "s2": {
            "id": "s2",
            "goal": "do a thing",  # near-duplicate of s1
            "opening_message": "hi",
            "exercises": ["memory_get", "ghost_tool"],  # unknown tool -> flagged
        },
    }
    return {"manifest": manifest, "personas": personas, "scenarios": scenarios}


class StatusTest(unittest.TestCase):
    def test_ensure_statuses_initializes_pending(self) -> None:
        manifest = _dataset()["manifest"]
        self.assertTrue(ensure_statuses(manifest))
        self.assertTrue(all(c["status"] == "pending" for c in manifest["cases"]))
        # Idempotent second call.
        self.assertFalse(ensure_statuses(manifest))

    def test_set_statuses_subset(self) -> None:
        manifest = _dataset()["manifest"]
        ensure_statuses(manifest)
        updated = set_statuses(manifest, {"c1"}, "approved")
        self.assertEqual(updated, ["c1"])
        statuses = {c["id"]: c["status"] for c in manifest["cases"]}
        self.assertEqual(statuses, {"c1": "approved", "c2": "pending"})

    def test_set_statuses_all_when_empty(self) -> None:
        manifest = _dataset()["manifest"]
        ensure_statuses(manifest)
        updated = set_statuses(manifest, set(), "rejected")
        self.assertEqual(set(updated), {"c1", "c2"})

    def test_invalid_status_raises(self) -> None:
        with self.assertRaises(ValueError):
            set_statuses(_dataset()["manifest"], set(), "bogus")


class ReviewTest(unittest.TestCase):
    def test_flags_dup_unknown_tool_and_orphan_persona(self) -> None:
        review = build_review(_dataset(), PROFILE)
        kinds = {f["kind"] for f in review["flags"]}
        self.assertIn("near_duplicate", kinds)
        self.assertIn("unknown_tool_exercised", kinds)
        self.assertIn("persona_without_scenarios", kinds)

    def test_coverage_marks_unexercised(self) -> None:
        review = build_review(_dataset(), PROFILE)
        cov = review["coverage"]
        self.assertIn("memory_get", cov["exercised_tools"])
        self.assertIn("memory_put", cov["unexercised_tools"])
        self.assertIn("student_get_status", cov["unexercised_tools"])

    def test_no_profile_skips_coverage(self) -> None:
        review = build_review(_dataset(), None)
        self.assertEqual(review["coverage"], {})


if __name__ == "__main__":
    unittest.main()
