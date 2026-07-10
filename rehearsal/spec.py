"""`ghostlab.yaml` — the canonical spec for an agent under test.

Today the pipeline's knowledge about a target is scattered across target JSON,
inspect artifacts, profiles, datasets, and runner configs. The spec collects it
into one durable, human-editable artifact that `init` creates, `discover`
enriches, and later stages (`plan`, `test`, `review`) will consume.

The project is intentionally stdlib-only, so this module carries a small YAML
subset reader/writer of its own: block mappings and sequences, quoted/plain
scalars, inline `[a, b]` / `{}` / `[]` flow forms, and `#` comments. That covers
everything GhostLab emits. If PyYAML happens to be installed it is preferred
for *reading*, so hand-edited specs get full YAML semantics; writing always
uses the built-in emitter so output stays deterministic and dependency-free.
JSON specs (`ghostlab.json`) are supported as a first-class alternative.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError, TargetConfig, load_json
from .sandbox import DEFAULT_SANDBOX

SPEC_SCHEMA_VERSION = 1
# Legacy specs (root ghostlab.yaml) used a hidden .ghostlab workspace. Self-
# contained jobs keep everything under the job dir, so a job's workspace is a
# plain sibling folder (see JOB_WORKSPACE / rehearsal.jobs).
DEFAULT_WORKSPACE = ".ghostlab"
JOB_WORKSPACE = "workspace"

_TOP_LEVEL_KEYS = (
    "schema_version",
    "id",
    "name",
    "source_target",
    "workspace",
    "target",
    "agent",
    "sandbox",
    "setup",
    "hosts",
    "capabilities",
    "generation",
    "test",
    "prompts",
    "test_plan",
    "review",
)

# Curated, editable defaults surfaced into every generated job.yaml so a user
# can see and tune every knob in one place. Kept here (not in argparse) so the
# CLI can resolve `explicit flag -> spec setting -> this default`.
DEFAULT_GENERATION = {
    "personas": 2,
    "scenarios_per_persona": 2,
    "model": "",
    "codex_bin": "",
    "regenerate": False,
}
DEFAULT_TEST = {
    "suites": [],  # empty = all suites
    "judge": True,
    "apps_mode": False,
    "repeat": 1,
    "timeout": 30.0,
    "user_runner": "",
    "approved_only": False,
}
# Each prompt is overridable; "" means "use the built-in template". The
# placeholders each template accepts are documented in the job.yaml header.
DEFAULT_PROMPTS = {
    "aut": "",
    "agent_aut": "",
    "skill_aut": "",
    "user_emulator": "",
    "judge": "",
    "critique": "",
    "persona_gen": "",
    "scenario_gen": "",
    "profile": "",
}


@dataclass
class GhostlabSpec:
    """Typed view of a `ghostlab.yaml` / `ghostlab.json` spec."""

    id: str
    name: str = ""
    schema_version: int = SPEC_SCHEMA_VERSION
    source_target: str = ""
    workspace: str = DEFAULT_WORKSPACE
    # TargetConfig-shaped: transport, connection, capabilities, startup.
    target: dict[str, Any] = field(default_factory=dict)
    # Canonical evaluation subject. An agent can compose zero or more MCPs,
    # skills, workspace assets, and an arbitrary runner command.
    agent: dict[str, Any] = field(default_factory=dict)
    # Execution boundary for agent and local-target processes.
    sandbox: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SANDBOX))
    # Setup runtime primitives (commands/health/reset/teardown) — populated by
    # hand or by `discover-setup` in a later phase; validated loosely for now.
    setup: dict[str, Any] = field(default_factory=dict)
    hosts: list[dict[str, Any]] = field(default_factory=list)
    # Discovered capabilities: generated_from, tools, ui_resources.
    capabilities: dict[str, Any] = field(default_factory=dict)
    # Editable knobs for `plan`/`test` (persona/scenario counts, suites, judge,
    # etc.); the CLI resolves explicit flag -> these -> code default.
    generation: dict[str, Any] = field(default_factory=dict)
    test: dict[str, Any] = field(default_factory=dict)
    # Optional prompt overrides ("" per key = use the built-in template).
    prompts: dict[str, Any] = field(default_factory=dict)
    test_plan: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    # Keys we don't model yet; preserved verbatim on save so human edits and
    # future schema additions survive a load/save cycle.
    extras: dict[str, Any] = field(default_factory=dict)

    def target_config(self) -> TargetConfig:
        """Materialize the embedded target as the pipeline's TargetConfig."""
        if not self.target and self.agent:
            inputs = self.agent.get("inputs", {}) or {}
            mcps = inputs.get("mcps", []) or []
            if mcps:
                entry = dict(mcps[0])
                return TargetConfig(
                    id=str(entry.get("id", self.id)),
                    transport=str(entry.get("transport", "streamable-http")),
                    connection=dict(entry.get("connection", {})),
                    capabilities=dict(entry.get("capabilities", {})),
                    startup=dict(entry.get("startup", {})),
                )
            skills = inputs.get("skills", []) or []
            if skills:
                entry = skills[0]
                path = entry.get("path") if isinstance(entry, dict) else entry
                return TargetConfig(
                    id=self.id, transport="skill", connection={"path": str(path)},
                    capabilities={}, startup={},
                )
        missing = [key for key in ("transport", "connection") if key not in self.target]
        if missing:
            raise ConfigError(
                f"Spec '{self.id}' target section is missing: {', '.join(missing)}"
            )
        return TargetConfig(
            id=self.id,
            transport=str(self.target["transport"]),
            connection=dict(self.target["connection"]),
            capabilities=dict(self.target.get("capabilities", {})),
            startup=dict(self.target.get("startup", {})),
        )

    @property
    def target_type(self) -> str:
        if self.target:
            return str(self.target.get("kind", "mcp"))
        return "agent" if self.agent else "mcp"

    def evaluation_target(self, spec_path: Path) -> TargetConfig:
        """Target-shaped context used by the existing conversation pipeline.

        The target remains the primary discovery input, while the embedded
        ``agent_definition`` makes prompts/execution aware of the composition.
        """
        target = self.target_config()
        if not self.agent:
            return target
        agent = dict(self.agent)
        inputs = dict(agent.get("inputs", {}) or {})
        skill_texts: list[str] = []
        for item in inputs.get("skills", []) or []:
            raw_path = item.get("path") if isinstance(item, dict) else item
            path = Path(str(raw_path)).expanduser()
            if not path.is_absolute():
                path = spec_path.resolve().parent / path
            if path.is_dir():
                path = path / "SKILL.md"
            try:
                skill_texts.append(path.read_text(encoding="utf-8"))
            except OSError:
                skill_texts.append(f"(skill unavailable: {path})")
        capabilities = {
            **target.capabilities,
            "agent_definition": agent,
            "agent_instructions": str(agent.get("instructions", "")),
            "skill_instructions": "\n\n".join(skill_texts),
        }
        return TargetConfig(
            id=str(agent.get("id") or self.id), transport=target.transport,
            connection=target.connection, capabilities=capabilities,
            startup=target.startup,
        )

    def workspace_dir(self, spec_path: Path) -> Path:
        """Workspace directory for artifacts, resolved relative to the spec file."""
        workspace = Path(self.workspace or DEFAULT_WORKSPACE)
        if workspace.is_absolute():
            return workspace
        return spec_path.resolve().parent / workspace

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "id": self.id,
        }
        if self.name:
            data["name"] = self.name
        if self.source_target:
            data["source_target"] = self.source_target
        data["workspace"] = self.workspace
        data["target"] = self.target
        data["agent"] = self.agent
        data["sandbox"] = self.sandbox
        data["setup"] = self.setup
        data["hosts"] = self.hosts
        data["capabilities"] = self.capabilities
        data["generation"] = self.generation
        data["test"] = self.test
        data["prompts"] = self.prompts
        data["test_plan"] = self.test_plan
        data["review"] = self.review
        data.update(self.extras)
        return data


def spec_from_dict(data: dict[str, Any], source: str = "spec") -> GhostlabSpec:
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: expected a top-level mapping")
    if "id" not in data:
        raise ConfigError(f"{source}: missing required key 'id'")
    target = data.get("target", {})
    if not isinstance(target, dict):
        raise ConfigError(f"{source}: 'target' must be a mapping")
    agent = data.get("agent", {}) or {}
    if not isinstance(agent, dict):
        raise ConfigError(f"{source}: 'agent' must be a mapping")
    sandbox = data.get("sandbox", DEFAULT_SANDBOX) or {}
    if not isinstance(sandbox, dict):
        raise ConfigError(f"{source}: 'sandbox' must be a mapping")
    if not target and not agent:
        raise ConfigError(f"{source}: provide either 'target' or 'agent'")
    hosts = data.get("hosts", []) or []
    if not isinstance(hosts, list):
        raise ConfigError(f"{source}: 'hosts' must be a list")
    for host in hosts:
        if not isinstance(host, dict) or "id" not in host or "kind" not in host:
            raise ConfigError(f"{source}: each host needs 'id' and 'kind' keys")
    version = int(data.get("schema_version", SPEC_SCHEMA_VERSION))
    if version > SPEC_SCHEMA_VERSION:
        raise ConfigError(
            f"{source}: schema_version {version} is newer than supported "
            f"({SPEC_SCHEMA_VERSION}); upgrade ghostlab"
        )

    extras = {key: value for key, value in data.items() if key not in _TOP_LEVEL_KEYS}
    return GhostlabSpec(
        id=str(data["id"]),
        name=str(data.get("name", "") or ""),
        schema_version=version,
        source_target=str(data.get("source_target", "") or ""),
        workspace=str(data.get("workspace", DEFAULT_WORKSPACE) or DEFAULT_WORKSPACE),
        target=dict(target),
        agent=dict(agent),
        sandbox={**DEFAULT_SANDBOX, **dict(sandbox)},
        setup=dict(data.get("setup", {}) or {}),
        hosts=[dict(host) for host in hosts],
        capabilities=dict(data.get("capabilities", {}) or {}),
        generation=dict(data.get("generation", {}) or {}),
        test=dict(data.get("test", {}) or {}),
        prompts=dict(data.get("prompts", {}) or {}),
        test_plan=dict(data.get("test_plan", {}) or {}),
        review=dict(data.get("review", {}) or {}),
        extras=extras,
    )


def spec_from_target(
    target: TargetConfig,
    *,
    source_target: str = "",
    name: str = "",
    workspace: str = DEFAULT_WORKSPACE,
) -> GhostlabSpec:
    """Starter spec for an existing target JSON (`ghostlab init`)."""
    return GhostlabSpec(
        id=target.id,
        name=name or target.id,
        source_target=source_target,
        workspace=workspace,
        target={
            "transport": target.transport,
            "connection": dict(target.connection),
            "capabilities": dict(target.capabilities),
            "startup": dict(target.startup),
        },
        agent={
            "id": target.id,
            "runner": {},
            "inputs": {
                "mcps": [{
                    "id": target.id, "transport": target.transport,
                    "connection": dict(target.connection),
                    "capabilities": dict(target.capabilities),
                    "startup": dict(target.startup),
                }],
                "skills": [],
            },
        },
        sandbox=dict(DEFAULT_SANDBOX),
        setup={"commands": [], "health": [], "reset": [], "teardown": [], "fixtures": []},
        hosts=[
            {
                "id": "direct-mcp",
                "kind": "direct-mcp",
                "roles": ["contract", "deterministic"],
            }
        ],
        capabilities={},
        generation=dict(DEFAULT_GENERATION),
        test=dict(DEFAULT_TEST),
        prompts=dict(DEFAULT_PROMPTS),
        test_plan={},
        review={
            "gates": {
                "min_pass_rate": 0.9,
                "no_tool_schema_errors": True,
                "no_ui_console_errors": True,
                "no_high_security_findings": True,
            }
        },
    )


def spec_from_skill(
    skill_path: Path, *, name: str = "", workspace: str = DEFAULT_WORKSPACE,
) -> GhostlabSpec:
    """Starter spec for a local SKILL.md target."""
    from .skills import resolve_skill_path

    path = resolve_skill_path(skill_path)
    target_id = re.sub(r"[^a-z0-9]+", "-", (name or path.parent.name).lower()).strip("-") or "skill"
    return GhostlabSpec(
        id=target_id,
        name=name or path.parent.name,
        workspace=workspace,
        source_target=str(path),
        target={
            "kind": "skill", "transport": "skill",
            "connection": {"path": str(path)}, "capabilities": {}, "startup": {},
        },
        agent={
            "id": target_id,
            "runner": {},
            "inputs": {"mcps": [], "skills": [{"path": str(path)}]},
        },
        sandbox=dict(DEFAULT_SANDBOX),
        setup={"commands": [], "health": [], "reset": [], "teardown": [], "fixtures": []},
        hosts=[],
        capabilities={}, generation=dict(DEFAULT_GENERATION), test=dict(DEFAULT_TEST),
        prompts=dict(DEFAULT_PROMPTS), test_plan={},
        review={"gates": {"min_pass_rate": 0.9}},
    )


def load_spec(path: Path) -> GhostlabSpec:
    if not path.exists():
        raise ConfigError(f"Spec file not found: {path}")
    if path.suffix.lower() == ".json":
        return spec_from_dict(load_json(path), source=str(path))
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]  # optional, for full YAML
    except ImportError:
        yaml = None
    try:
        data = yaml.safe_load(text) if yaml is not None else parse_yaml(text)
    except Exception as exc:  # YamlSubsetError or yaml.YAMLError
        raise ConfigError(f"Cannot parse {path}: {exc}") from exc
    if data is None:
        data = {}
    return spec_from_dict(data, source=str(path))


def save_spec(spec: GhostlabSpec, path: Path, header: str | None = None) -> Path:
    """Write a spec to YAML (or JSON). ``header`` overrides the default comment
    banner (ignored for JSON, which has no comments)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(
            json.dumps(spec.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path
    if header is None:
        header = (
            f"# ghostlab spec for {spec.id} — edit freely; `ghostlab discover` updates\n"
            "# the `capabilities` section and leaves the rest of the file to you.\n"
        )
    path.write_text(header + dump_yaml(spec.to_dict()), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# YAML subset emitter
# --------------------------------------------------------------------------- #
_PLAIN_SCALAR_RE = re.compile(r"^[A-Za-z0-9_./][A-Za-z0-9_ .,/@+:()\-]*$")
_BOOLISH = {"true", "false", "yes", "no", "on", "off", "null", "~", "none"}


def _is_plain_safe(text: str) -> bool:
    if not _PLAIN_SCALAR_RE.match(text):
        return False
    if text != text.strip():
        return False
    if ": " in text or text.endswith(":") or " #" in text:
        return False
    if text.lower() in _BOOLISH:
        return False
    try:
        float(text)
        return False  # would round-trip as a number
    except ValueError:
        return True


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    text = str(value)
    return text if _is_plain_safe(text) else json.dumps(text, ensure_ascii=False)


def _dump_node(value: Any, indent: int, lines: list[str]) -> None:
    pad = "  " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = _format_scalar(key)
            if isinstance(item, dict) and item:
                lines.append(f"{pad}{key_text}:")
                _dump_node(item, indent + 1, lines)
            elif isinstance(item, list) and item:
                lines.append(f"{pad}{key_text}:")
                _dump_node(item, indent + 1, lines)
            elif isinstance(item, (dict, list)):
                lines.append(f"{pad}{key_text}: {'{}' if isinstance(item, dict) else '[]'}")
            else:
                lines.append(f"{pad}{key_text}: {_format_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item:
                sub: list[str] = []
                _dump_node(item, 0, sub)
                lines.append(f"{pad}- {sub[0]}")
                lines.extend(f"{pad}  {line}" for line in sub[1:])
            elif isinstance(item, list) and item:
                sub = []
                _dump_node(item, 0, sub)
                lines.append(f"{pad}- {sub[0].strip()}" if sub else f"{pad}- []")
                lines.extend(f"{pad}  {line}" for line in sub[1:])
            elif isinstance(item, (dict, list)):
                lines.append(f"{pad}- {'{}' if isinstance(item, dict) else '[]'}")
            else:
                lines.append(f"{pad}- {_format_scalar(item)}")
    else:
        lines.append(f"{pad}{_format_scalar(value)}")


def dump_yaml(value: Any) -> str:
    """Serialize plain dict/list/scalar data as YAML (subset, deterministic)."""
    if isinstance(value, dict) and not value:
        return "{}\n"
    if isinstance(value, list) and not value:
        return "[]\n"
    lines: list[str] = []
    _dump_node(value, 0, lines)
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# YAML subset parser
# --------------------------------------------------------------------------- #
class YamlSubsetError(ValueError):
    """Raised when text falls outside the supported YAML subset."""


def _strip_trailing_comment(text: str) -> str:
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#" and index > 0 and text[index - 1] in (" ", "\t"):
            return text[:index].rstrip()
    return text.rstrip()


def _parse_flow_list(text: str) -> list[Any]:
    inner = text[1:-1].strip()
    if not inner:
        return []
    if any(char in inner for char in "[]{}"):
        raise YamlSubsetError(f"nested flow collections are not supported: {text!r}")
    return [_parse_scalar(part.strip()) for part in inner.split(",")]


def _parse_scalar(text: str) -> Any:
    if text == "" or text in ("null", "~"):
        return None
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text.startswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise YamlSubsetError(f"bad double-quoted scalar: {text!r}") from exc
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise YamlSubsetError(f"unterminated single-quoted scalar: {text!r}")
        return text[1:-1].replace("''", "'")
    if text.startswith("["):
        if not text.endswith("]"):
            raise YamlSubsetError(f"unterminated flow list: {text!r}")
        return _parse_flow_list(text)
    if text == "{}":
        return {}
    if text.startswith("{"):
        raise YamlSubsetError(f"flow mappings are not supported: {text!r}")
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


class _YamlParser:
    def __init__(self, lines: list[tuple[int, str]]) -> None:
        self.lines = lines
        self.pos = 0

    def peek(self) -> tuple[int, str] | None:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def parse_node(self, min_indent: int) -> Any:
        line = self.peek()
        if line is None or line[0] < min_indent:
            return None
        if line[1].startswith("- ") or line[1] == "-":
            return self.parse_sequence(line[0])
        return self.parse_mapping(line[0])

    def parse_sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while True:
            line = self.peek()
            if line is None or line[0] != indent:
                break
            item_indent, text = line
            if not (text.startswith("- ") or text == "-"):
                break
            content = text[2:].strip() if text.startswith("- ") else ""
            if not content:
                self.pos += 1
                nxt = self.peek()
                if nxt is not None and nxt[0] > indent:
                    items.append(self.parse_node(nxt[0]))
                else:
                    items.append(None)
            elif self._looks_like_mapping_entry(content):
                # `- key: value` opens a mapping whose lines continue at
                # indent + 2; rewrite the line and parse it as such.
                self.lines[self.pos] = (indent + 2, content)
                items.append(self.parse_mapping(indent + 2))
            else:
                items.append(_parse_scalar(content))
                self.pos += 1
        return items

    @staticmethod
    def _looks_like_mapping_entry(content: str) -> bool:
        if content.startswith(('"', "'", "[", "{")):
            return False
        key, sep, rest = content.partition(":")
        return bool(sep) and (rest == "" or rest.startswith(" "))

    def parse_mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            line = self.peek()
            if line is None or line[0] != indent:
                break
            _, text = line
            if text.startswith("- ") or text == "-":
                break
            if not self._looks_like_mapping_entry(text):
                raise YamlSubsetError(f"expected 'key: value', got: {text!r}")
            key_text, _, rest = text.partition(":")
            key = str(_parse_scalar(key_text.strip()))
            rest = rest.strip()
            self.pos += 1
            if rest:
                result[key] = _parse_scalar(rest)
                continue
            nxt = self.peek()
            if nxt is not None and (
                nxt[0] > indent
                or (nxt[0] == indent and (nxt[1].startswith("- ") or nxt[1] == "-"))
            ):
                result[key] = self.parse_node(nxt[0])
            else:
                result[key] = None
        return result


def parse_yaml(text: str) -> Any:
    """Parse the YAML subset this module emits (see module docstring).

    Not a general YAML parser: no anchors, tags, multi-line scalars, multiple
    documents, or nested flow collections. Install PyYAML for full semantics —
    `load_spec` prefers it automatically when available.
    """
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlSubsetError("tabs are not allowed in indentation")
        stripped = _strip_trailing_comment(raw)
        content = stripped.strip()
        if not content or content.startswith("#"):
            continue
        if content == "---":
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, content))
    if not lines:
        return {}
    parser = _YamlParser(lines)
    result = parser.parse_node(0)
    if parser.pos < len(parser.lines):
        indent, content = parser.lines[parser.pos]
        raise YamlSubsetError(f"unexpected line (indent {indent}): {content!r}")
    return result
