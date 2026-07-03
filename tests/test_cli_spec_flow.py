"""End-to-end tests for `ghostlab init` + `ghostlab discover` over a stdio MCP."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from rehearsal.cli import _HANDLERS, build_parser, main
from rehearsal.spec import load_spec

FAKE_SERVER = '''
import json, sys

TOOLS = [
    {"name": "notes_list", "description": "List all saved notes for the user.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"readOnlyHint": True}},
    {"name": "notes_delete", "description": "del",
     "inputSchema": {"type": "object", "properties": {"note_id": {"type": "string"}},
                     "required": ["note_id", "confirm"]}},
]

def handle(msg):
    method = msg.get("method")
    if method == "initialize":
        return {"protocolVersion": "2025-06-18",
                "serverInfo": {"name": "fake-notes", "version": "0.0.1"},
                "capabilities": {"tools": {}},
                "instructions": "Call `notes_archive` to tidy up."}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        if msg["params"]["name"] == "notes_list":
            return {"content": [{"type": "text", "text": "3 notes"}]}
        return {"isError": True, "content": [{"type": "text", "text": "nope"}]}
    return {}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}), flush=True)
'''


class CliSpecFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        server = self.tmp / "fake_mcp.py"
        server.write_text(FAKE_SERVER, encoding="utf-8")
        self.target_path = self.tmp / "target.json"
        self.target_path.write_text(
            json.dumps(
                {
                    "id": "fake-notes",
                    "transport": "stdio",
                    "connection": {"command": [sys.executable, str(server)]},
                }
            ),
            encoding="utf-8",
        )
        self.spec_path = self.tmp / "ghostlab.yaml"
        self.db_path = self.tmp / "ghostlab.sqlite3"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_init_then_discover_updates_spec(self) -> None:
        code = main(
            ["init", "--target", str(self.target_path), "--out", str(self.spec_path)]
        )
        self.assertEqual(code, 0)
        spec = load_spec(self.spec_path)
        self.assertEqual(spec.id, "fake-notes")
        self.assertEqual(spec.capabilities, {})

        code = main(
            ["discover", "--spec", str(self.spec_path), "--db", str(self.db_path)]
        )
        self.assertEqual(code, 0)
        spec = load_spec(self.spec_path)
        tool_names = {tool["name"] for tool in spec.capabilities["tools"]}
        self.assertEqual(tool_names, {"notes_list", "notes_delete"})
        contract_path = self.spec_path.parent / spec.capabilities["generated_from"]
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        kinds = {finding["kind"] for finding in contract["findings"]}
        # Seeded defects: undefined required param, weak description, and an
        # instructions reference to a tool the server does not expose.
        self.assertIn("required_param_undefined", kinds)
        self.assertIn("weak_tool_description", kinds)
        self.assertIn("missing_tool_reference", kinds)

    def test_discover_strict_fails_on_schema_errors(self) -> None:
        main(["init", "--target", str(self.target_path), "--out", str(self.spec_path)])
        code = main(
            [
                "discover",
                "--spec", str(self.spec_path),
                "--strict",
                "--db", str(self.db_path),
            ]
        )
        self.assertEqual(code, 1)

    def test_discover_with_setup_and_sampling(self) -> None:
        from rehearsal.spec import load_spec as _load
        from rehearsal.spec import save_spec

        main(["init", "--target", str(self.target_path), "--out", str(self.spec_path)])
        flag = self.tmp / "prepared.flag"
        spec = _load(self.spec_path)
        spec.setup["commands"] = [{"id": "prepare", "command": ["touch", str(flag)]}]
        spec.setup["health"] = [
            {"type": "command", "command": ["test", "-f", str(flag)], "timeout_seconds": 5}
        ]
        spec.setup["teardown"] = [{"id": "clean", "command": ["rm", "-f", str(flag)]}]
        save_spec(spec, self.spec_path)

        code = main(
            [
                "discover",
                "--spec", str(self.spec_path),
                "--sample", "safe",
                "--db", str(self.db_path),
            ]
        )
        self.assertEqual(code, 0)
        self.assertFalse(flag.exists())  # teardown removed it

        spec = _load(self.spec_path)
        discover_dir = (
            self.spec_path.parent / spec.capabilities["generated_from"]
        ).parent
        setup_status = json.loads((discover_dir / "setup.json").read_text(encoding="utf-8"))
        self.assertTrue(setup_status["commands"][0]["ok"])
        self.assertTrue(setup_status["health"][0]["ok"])
        self.assertTrue(setup_status["teardown"][0]["ok"])
        self.assertIn("fingerprint", setup_status)

        samples = json.loads((discover_dir / "samples.json").read_text(encoding="utf-8"))
        by_tool = {sample["tool"]: sample for sample in samples["samples"]}
        self.assertEqual(by_tool["notes_list"]["status"], "ok")
        self.assertEqual(by_tool["notes_delete"]["status"], "skipped")

    def test_discover_fails_when_health_never_passes(self) -> None:
        from rehearsal.spec import load_spec as _load
        from rehearsal.spec import save_spec

        main(["init", "--target", str(self.target_path), "--out", str(self.spec_path)])
        spec = _load(self.spec_path)
        spec.setup["health"] = [
            {"type": "command", "command": ["test", "-f", str(self.tmp / "never.flag")],
             "timeout_seconds": 0.3, "interval_seconds": 0.05}
        ]
        save_spec(spec, self.spec_path)
        code = main(["discover", "--spec", str(self.spec_path), "--db", str(self.db_path)])
        self.assertEqual(code, 1)

    def test_plan_after_discover_and_curation(self) -> None:
        main(["init", "--target", str(self.target_path), "--out", str(self.spec_path)])
        main(["discover", "--spec", str(self.spec_path), "--db", str(self.db_path)])

        code = main(["plan", "--spec", str(self.spec_path), "--no-generate"])
        self.assertEqual(code, 0)
        plan_path = self.spec_path.parent / "test-plan.yaml"
        self.assertTrue(plan_path.exists())

        from rehearsal.plan import load_test_plan

        plan = load_test_plan(plan_path)
        self.assertTrue(plan["cases"])
        self.assertTrue(all(case["reason"] for case in plan["cases"]))
        # notes_delete is destructive -> a security case exists for it.
        self.assertIn(
            "security-destructive-notes-delete",
            {case["id"] for case in plan["cases"]},
        )
        # Spec's test_plan section was refreshed.
        spec = load_spec(self.spec_path)
        self.assertEqual(spec.test_plan["plan_file"], "test-plan.yaml")
        self.assertEqual(spec.test_plan["cases"], len(plan["cases"]))

        # Curate, then regenerate: status must survive.
        code = main(["plan", "--spec", str(self.spec_path), "--no-generate", "--approve", "smoke-discovery"])
        self.assertEqual(code, 0)
        main(["plan", "--spec", str(self.spec_path), "--no-generate"])
        plan = load_test_plan(plan_path)
        statuses = {case["id"]: case["status"] for case in plan["cases"]}
        self.assertEqual(statuses["smoke-discovery"], "approved")

    def test_full_pipeline_through_test_command(self) -> None:
        main(["init", "--target", str(self.target_path), "--out", str(self.spec_path)])
        main(["discover", "--spec", str(self.spec_path), "--db", str(self.db_path)])
        main(["plan", "--spec", str(self.spec_path), "--no-generate"])

        # edge: notes_delete with {} -> fake server answers isError -> graceful pass.
        # semantic: conversational seeds -> skipped by the direct host with reasons.
        code = main(
            [
                "test",
                "--spec", str(self.spec_path),
                "--suite", "edge", "--suite", "semantic",
            ]
        )
        self.assertEqual(code, 0)

        spec = load_spec(self.spec_path)
        test_root = self.spec_path.parent / spec.workspace / "test"
        results_files = sorted(test_root.glob("*/results.json"))
        self.assertTrue(results_files)
        results = json.loads(results_files[-1].read_text(encoding="utf-8"))
        by_case = {entry["case"]: entry for entry in results["results"]}
        self.assertEqual(by_case["edge-notes-delete-missing-required"]["status"], "pass")
        self.assertEqual(by_case["semantic-notes-workflow"]["status"], "skip")
        self.assertEqual(results["totals"]["fail"], 0)
        self.assertEqual(results["pass_rate"], 1.0)
        self.assertIn("fingerprint", results)

        # Strict mode passes here (100% >= 0.9 gate from the starter spec).
        code = main(
            [
                "test",
                "--spec", str(self.spec_path),
                "--suite", "edge",
                "--strict",
            ]
        )
        self.assertEqual(code, 0)

    def test_review_after_full_pipeline(self) -> None:
        main(["init", "--target", str(self.target_path), "--out", str(self.spec_path)])
        main(["discover", "--spec", str(self.spec_path), "--db", str(self.db_path)])
        main(["plan", "--spec", str(self.spec_path), "--no-generate"])
        main(["test", "--spec", str(self.spec_path), "--suite", "edge"])

        code = main(["review", "--spec", str(self.spec_path)])
        self.assertEqual(code, 0)

        spec = load_spec(self.spec_path)
        results_dirs = sorted((self.spec_path.parent / spec.workspace / "test").glob("*"))
        readiness = json.loads(
            (results_dirs[-1] / "readiness.json").read_text(encoding="utf-8")
        )
        # The fake server ships a schema error (undefined required param), so
        # the schema gate fails and the verdict is not-ready.
        self.assertEqual(readiness["verdict"], "not-ready")
        by_gate = {gate["gate"]: gate["status"] for gate in readiness["gates"]}
        self.assertEqual(by_gate["no_tool_schema_errors"], "fail")
        self.assertEqual(by_gate["min_pass_rate"], "pass")  # edge suite passed
        kinds = {repair["kind"] for repair in readiness["repairs"]}
        self.assertIn("required_param_undefined", kinds)

        # --strict turns not-ready into a failing exit code.
        self.assertEqual(main(["review", "--spec", str(self.spec_path), "--strict"]), 1)

    @patch("rehearsal.plan_generate.build_dataset")
    @patch("rehearsal.plan_generate.build_capability_profile")
    @patch("rehearsal.codex_backend.CodexBackend._bin", return_value="codex")
    def test_plan_generate_produces_real_conversational_cases(
        self, _bin_mock, profile_mock, dataset_mock
    ) -> None:
        main(["init", "--target", str(self.target_path), "--out", str(self.spec_path)])
        main(["discover", "--spec", str(self.spec_path), "--db", str(self.db_path)])

        profile_mock.return_value = {"mcp": "fake-notes@0.0.1"}
        dataset_mock.return_value = {
            "manifest": {"name": "fake-notes", "cases": [
                {"id": "alice--happy", "persona": "alice", "scenario": "alice--happy",
                 "intent": "happy_path", "exercises": ["notes_list"]},
                {"id": "bob--adv", "persona": "bob", "scenario": "bob--adv",
                 "intent": "adversarial", "exercises": ["notes_delete"]},
            ]},
            "personas": [
                {"id": "alice", "name": "Alice", "summary": "s"},
                {"id": "bob", "name": "Bob", "summary": "s"},
            ],
            "scenarios": [
                {"id": "alice--happy", "title": "t", "persona": "alice", "goal": "g",
                 "max_turns": 4, "opening_message": "hi", "intent": "happy_path",
                 "exercises": ["notes_list"]},
                {"id": "bob--adv", "title": "t2", "persona": "bob", "goal": "g2",
                 "max_turns": 4, "opening_message": "hi2", "intent": "adversarial",
                 "exercises": ["notes_delete"]},
            ],
        }

        code = main(["plan", "--spec", str(self.spec_path)])  # --generate is the default
        self.assertEqual(code, 0)
        dataset_mock.assert_called_once()
        self.assertEqual(dataset_mock.call_args.kwargs["n_personas"], 2)
        self.assertEqual(dataset_mock.call_args.kwargs["scenarios_per_persona"], 2)

        from rehearsal.plan import load_test_plan

        plan = load_test_plan(self.spec_path.parent / "test-plan.yaml")
        ids = {case["id"] for case in plan["cases"]}
        self.assertIn("semantic-gen-alice--happy", ids)
        self.assertIn("security-gen-bob--adv", ids)
        # Inert per-family seeds are dropped once real generation exists.
        self.assertFalse(any(cid.startswith("semantic-notes") for cid in ids))

        spec = load_spec(self.spec_path)
        generated_dir = self.spec_path.parent / spec.test_plan["generated_dataset"]
        self.assertTrue((generated_dir / "dataset.json").exists())
        self.assertTrue((generated_dir / "personas" / "alice.json").exists())

        # Re-plan reuses the cached dataset: zero further codex calls.
        main(["plan", "--spec", str(self.spec_path)])
        dataset_mock.assert_called_once()

        # --regenerate forces a fresh call.
        main(["plan", "--spec", str(self.spec_path), "--regenerate"])
        self.assertEqual(dataset_mock.call_count, 2)

    def test_test_command_requires_plan(self) -> None:
        main(["init", "--target", str(self.target_path), "--out", str(self.spec_path)])
        with self.assertRaises(SystemExit):
            main(["test", "--spec", str(self.spec_path)])

    def test_plan_without_discover_errors(self) -> None:
        main(["init", "--target", str(self.target_path), "--out", str(self.spec_path)])
        with self.assertRaises(SystemExit):  # ConfigError -> parser.error
            main(["plan", "--spec", str(self.spec_path), "--no-generate"])

    def test_init_refuses_overwrite_without_force(self) -> None:
        args = ["init", "--target", str(self.target_path), "--out", str(self.spec_path)]
        self.assertEqual(main(args), 0)
        self.assertEqual(main(args), 1)
        self.assertEqual(main([*args, "--force"]), 0)


class HandlerRegistryTest(unittest.TestCase):
    def test_registry_matches_subparsers(self) -> None:
        parser = build_parser()
        subparser_actions = [
            action
            for action in parser._actions  # noqa: SLF001 — argparse introspection
            if action.__class__.__name__ == "_SubParsersAction"
        ]
        self.assertEqual(len(subparser_actions), 1)
        declared = set(subparser_actions[0].choices)
        self.assertEqual(declared, set(_HANDLERS))


if __name__ == "__main__":
    unittest.main()
