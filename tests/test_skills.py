from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rehearsal.skills import inspect_skill, resolve_skill_path
from rehearsal.spec import spec_from_skill


class SkillTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "demo-skill"
        self.root.mkdir()
        self.path = self.root / "SKILL.md"
        self.path.write_text(
            "---\nname: demo\ndescription: Helps write concise release notes.\n---\n"
            "# Workflow\nAsk for the version, then draft notes.\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_directory_resolves_to_skill_file(self) -> None:
        self.assertEqual(resolve_skill_path(self.root), self.path.resolve())

    def test_skill_inspection_preserves_instructions(self) -> None:
        result = inspect_skill(self.path, "demo")
        self.assertEqual(result.transport, "skill")
        self.assertEqual(result.server_info["name"], "demo")
        self.assertIn("Ask for the version", result.instructions)

    def test_skill_inspection_inventories_companion_scripts(self) -> None:
        script = self.root / "scripts" / "init.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        result = inspect_skill(self.root, "demo")
        self.assertEqual(result.capabilities["scripts"], ["scripts/init.sh"])
        self.assertTrue(result.capabilities["requires_shell"])
        self.assertTrue(any(item["name"] == "scripts/init.sh" for item in result.resources))

    def test_skill_spec_is_first_class_target(self) -> None:
        spec = spec_from_skill(self.root, name="Release Notes")
        self.assertEqual(spec.target_type, "skill")
        self.assertEqual(spec.target_config().transport, "skill")
        self.assertEqual(spec.hosts, [])
        self.assertEqual(spec.agent["inputs"]["skills"][0]["path"], str(self.root.resolve()))
