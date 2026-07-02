"""Unit tests for the ghostlab spec model and its YAML subset codec."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rehearsal.config import ConfigError, TargetConfig
from rehearsal.spec import (
    GhostlabSpec,
    YamlSubsetError,
    dump_yaml,
    load_spec,
    parse_yaml,
    save_spec,
    spec_from_dict,
    spec_from_target,
)


def _target() -> TargetConfig:
    return TargetConfig(
        id="cortex-local",
        transport="streamable-http",
        connection={"url": "http://127.0.0.1:8020/mcp", "headers": {}},
        capabilities={"tools": ["memory_get"]},
        startup={"timeout_seconds": 30},
    )


class YamlCodecTest(unittest.TestCase):
    def test_round_trips_nested_structures(self) -> None:
        data = {
            "id": "x",
            "nested": {"deep": [{"k": "v", "nums": [1, 2, 3]}]},
            "empty_map": {},
            "empty_list": [],
            "url": "http://127.0.0.1:8020/mcp",
            "flag": True,
            "nothing": None,
            "rate": 0.9,
        }
        self.assertEqual(parse_yaml(dump_yaml(data)), data)

    def test_quotes_ambiguous_scalars(self) -> None:
        data = {
            "colon": "key: value",
            "numberish": "1.5",
            "boolish": "true",
            "empty": "",
            "hash": "text # not a comment",
            "trailing": "space ",
        }
        text = dump_yaml(data)
        self.assertEqual(parse_yaml(text), data)
        # These must not come back as float/bool/None.
        parsed = parse_yaml(text)
        self.assertIsInstance(parsed["numberish"], str)
        self.assertIsInstance(parsed["boolish"], str)

    def test_parses_flow_lists_and_comments(self) -> None:
        text = (
            "# header comment\n"
            "roles: [agent_under_test, judge]\n"
            "count: 3  # trailing comment\n"
            "name: 'single quoted'\n"
        )
        self.assertEqual(
            parse_yaml(text),
            {"roles": ["agent_under_test", "judge"], "count": 3, "name": "single quoted"},
        )

    def test_list_items_at_key_indent(self) -> None:
        text = "tools:\n- name: a\n- name: b\n"
        self.assertEqual(parse_yaml(text), {"tools": [{"name": "a"}, {"name": "b"}]})

    def test_rejects_tabs_and_unsupported_forms(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml("key:\n\tvalue: 1\n")
        with self.assertRaises(YamlSubsetError):
            parse_yaml("key: {a: 1}\n")

    def test_empty_document(self) -> None:
        self.assertEqual(parse_yaml("# only comments\n"), {})


class SpecModelTest(unittest.TestCase):
    def test_spec_from_target_round_trips_target_config(self) -> None:
        target = _target()
        spec = spec_from_target(target, source_target="targets/cortex-local.json")
        self.assertEqual(spec.target_config(), target)
        self.assertEqual(spec.hosts[0]["kind"], "direct-mcp")
        self.assertIn("gates", spec.review)

    def test_save_and_load_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ghostlab.yaml"
            spec = spec_from_target(_target(), name="Cortex")
            save_spec(spec, path)
            loaded = load_spec(path)
            self.assertEqual(loaded.to_dict(), spec.to_dict())

    def test_save_and_load_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ghostlab.json"
            spec = spec_from_target(_target())
            save_spec(spec, path)
            self.assertEqual(load_spec(path).to_dict(), spec.to_dict())

    def test_preserves_unknown_top_level_keys(self) -> None:
        data = spec_from_target(_target()).to_dict()
        data["future_section"] = {"keep": "me"}
        spec = spec_from_dict(data)
        self.assertEqual(spec.to_dict()["future_section"], {"keep": "me"})

    def test_rejects_missing_id_and_newer_schema(self) -> None:
        with self.assertRaises(ConfigError):
            spec_from_dict({"target": {}})
        with self.assertRaises(ConfigError):
            spec_from_dict({"id": "x", "schema_version": 99})

    def test_rejects_malformed_hosts(self) -> None:
        with self.assertRaises(ConfigError):
            spec_from_dict({"id": "x", "hosts": [{"id": "no-kind"}]})

    def test_target_config_requires_transport(self) -> None:
        spec = GhostlabSpec(id="x", target={"connection": {}})
        with self.assertRaises(ConfigError):
            spec.target_config()

    def test_workspace_dir_relative_to_spec(self) -> None:
        spec = GhostlabSpec(id="x", workspace=".ghostlab")
        resolved = spec.workspace_dir(Path("/somewhere/project/ghostlab.yaml"))
        self.assertEqual(resolved, Path("/somewhere/project/.ghostlab"))

    def test_load_reports_parse_errors_as_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("key: {broken: flow}\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_spec(path)


if __name__ == "__main__":
    unittest.main()
