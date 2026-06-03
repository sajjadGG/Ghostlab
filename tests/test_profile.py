"""Tests for the deterministic parts of capability profiling (no codex needed)."""
from __future__ import annotations

import unittest

from rehearsal.profile import state_surfaces, taxonomy

TOOLS = [
    {"name": "student_get_status", "annotations": {"readOnlyHint": True}},
    {"name": "student_set_level", "annotations": {"readOnlyHint": False}},
    {"name": "memory_get", "annotations": {"readOnlyHint": True}},
    {"name": "memory_put", "annotations": {"readOnlyHint": False}},
    {"name": "views_create_flash_cards", "annotations": {}},
]


class TaxonomyTest(unittest.TestCase):
    def test_groups_by_name_family(self) -> None:
        tax = taxonomy(TOOLS)
        self.assertEqual(set(tax.keys()), {"student", "memory", "views"})
        self.assertIn("student_get_status", tax["student"])
        self.assertIn("student_set_level", tax["student"])
        self.assertEqual(tax["views"], ["views_create_flash_cards"])

    def test_sorted_keys(self) -> None:
        self.assertEqual(list(taxonomy(TOOLS).keys()), ["memory", "student", "views"])


class StateSurfaceTest(unittest.TestCase):
    def test_splits_read_and_write(self) -> None:
        surfaces = state_surfaces(TOOLS)
        self.assertEqual(set(surfaces["read"]), {"student_get_status", "memory_get"})
        self.assertIn("memory_put", surfaces["write"])
        # Missing readOnlyHint defaults to mutating.
        self.assertIn("views_create_flash_cards", surfaces["write"])


if __name__ == "__main__":
    unittest.main()
