"""Tests for dataset comparison diff logic (no codex needed)."""
from __future__ import annotations

import unittest

from rehearsal.compare import diff_results


def _set(name, rows):
    return {"dataset": name, "version": "0.1.0", "results": rows}


class CompareTest(unittest.TestCase):
    def test_detects_regression_and_fix_by_verdict(self) -> None:
        base = _set("base", [
            {"case": "a", "verdict": "pass", "turns": 3},
            {"case": "b", "verdict": "fail", "turns": 4},
            {"case": "c", "verdict": "partial", "turns": 2},
        ])
        cand = _set("cand", [
            {"case": "a", "verdict": "fail", "turns": 3},      # regression
            {"case": "b", "verdict": "pass", "turns": 4},      # fix
            {"case": "c", "verdict": "partial", "turns": 5},   # unchanged outcome
        ])
        diff = diff_results(base, cand)
        self.assertEqual([r["case"] for r in diff["regressions"]], ["a"])
        self.assertEqual([r["case"] for r in diff["fixes"]], ["b"])
        self.assertEqual(diff["unchanged"], 1)

    def test_falls_back_to_status_without_verdicts(self) -> None:
        base = _set("base", [{"case": "a", "status": "completed", "turns": 3}])
        cand = _set("cand", [{"case": "a", "status": "aut_failed", "turns": 1}])
        diff = diff_results(base, cand)
        self.assertEqual(len(diff["regressions"]), 1)
        self.assertEqual(diff["regressions"][0]["candidate"], "aut_failed")

    def test_added_and_removed_cases(self) -> None:
        base = _set("base", [{"case": "a", "verdict": "pass"}])
        cand = _set("cand", [{"case": "b", "verdict": "pass"}])
        diff = diff_results(base, cand)
        self.assertEqual(diff["added"], ["b"])
        self.assertEqual(diff["removed"], ["a"])

    def test_same_outcome_is_unchanged(self) -> None:
        base = _set("base", [{"case": "a", "verdict": "pass", "turns": 3}])
        cand = _set("cand", [{"case": "a", "verdict": "pass", "turns": 9}])
        diff = diff_results(base, cand)
        self.assertEqual(diff["unchanged"], 1)
        self.assertEqual(diff["regressions"], [])


if __name__ == "__main__":
    unittest.main()
