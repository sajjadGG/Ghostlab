from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a Rehearsal config file is invalid."""


def expand_env(value: Any) -> Any:
    """Recursively expand ``$VAR`` / ``${VAR}`` from the environment in strings.

    Lets secrets (auth headers, tokens) stay out of a tracked ``job.yaml``: write
    ``Authorization: "Bearer ${GITHUB_MCP_TOKEN}"`` and export the token in the
    shell instead. An undefined variable is left literal (so the request still
    goes out, just unauthenticated) rather than raising.
    """
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


@dataclass(frozen=True)
class RunnerConfig:
    kind: str
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 180
    prompt_mode: str = "stdin"
    # How to interpret this runner's output: "text" (plain) or "codex-json"
    # (codex `exec --json` JSONL, enabling rich tool-call capture).
    parser: str = "text"
    # Execution boundary. Generated jobs default this to NVIDIA OpenShell;
    # ``{"backend": "local"}`` preserves direct host execution explicitly.
    sandbox: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TargetConfig:
    id: str
    transport: str
    connection: dict[str, Any]
    capabilities: dict[str, Any] = field(default_factory=dict)
    startup: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioConfig:
    id: str
    title: str
    persona: str
    goal: str
    max_turns: int
    success_criteria: list[str]
    failure_signals: list[str]
    opening_message: str
    # Optional generation metadata: which tools the scenario should exercise, and
    # whether it is a happy-path / edge-case / adversarial probe. Used for
    # coverage measurement; ignored by the run loop.
    exercises: list[str] = field(default_factory=list)
    intent: str = ""
    # Optional deterministic golden assertions, checked at evaluation time
    # alongside the LLM judge. Keys: `must_include` / `must_not_include`
    # (case-insensitive substrings in the final assistant turn) and
    # `expected_tool_args` (a list of {tool, arguments} the run must contain).
    # Ignored by the run loop; consumed by `evaluate`.
    expected_outcome: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PersonaConfig:
    """A reusable user profile that drives the user-emulator.

    Decoupled from scenarios so the same persona can be paired with many
    scenarios. `summary` is the headline description; `traits` shape emulation
    style (terse, impatient, non-native, adversarial); `context` holds
    domain attributes the MCP cares about (native_language, target_exam, ...).
    """

    id: str
    name: str
    summary: str
    traits: list[str] = field(default_factory=list)
    context: dict[str, str] = field(default_factory=dict)


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Expected top-level object in {path}")
    return data


def load_target(path: Path, server: str | None = None) -> TargetConfig:
    """Load a target config into the canonical TargetConfig.

    Accepts either a GhostLab native target JSON or a standard MCP client config
    with an ``mcpServers`` map (pick one with ``server``). Normalization lives in
    the adapter layer, `rehearsal.mcp_targets`.
    """
    from .mcp_targets import load_target as _load_target

    return _load_target(path, server=server)


def load_scenario(path: Path) -> ScenarioConfig:
    data = load_json(path)
    missing = [
        key
        for key in ("id", "title", "persona", "goal", "max_turns", "opening_message")
        if key not in data
    ]
    if missing:
        raise ConfigError(f"Scenario {path} is missing required keys: {', '.join(missing)}")

    return ScenarioConfig(
        id=str(data["id"]),
        title=str(data["title"]),
        persona=str(data["persona"]),
        goal=str(data["goal"]),
        max_turns=int(data["max_turns"]),
        success_criteria=[str(item) for item in data.get("success_criteria", [])],
        failure_signals=[str(item) for item in data.get("failure_signals", [])],
        opening_message=str(data["opening_message"]),
        exercises=[str(item) for item in data.get("exercises", [])],
        intent=str(data.get("intent", "")),
        expected_outcome=_load_expected_outcome(data.get("expected_outcome", {}), path),
    )


def _load_expected_outcome(raw: Any, path: Path) -> dict[str, Any]:
    """Validate and normalize a scenario's optional `expected_outcome` block."""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Scenario {path} `expected_outcome` must be an object")
    outcome: dict[str, Any] = {}
    for key in ("must_include", "must_not_include"):
        if key in raw:
            if not isinstance(raw[key], list):
                raise ConfigError(f"Scenario {path} `expected_outcome.{key}` must be a list")
            outcome[key] = [str(item) for item in raw[key]]
    if "expected_tool_args" in raw:
        items = raw["expected_tool_args"]
        if not isinstance(items, list):
            raise ConfigError(f"Scenario {path} `expected_outcome.expected_tool_args` must be a list")
        normalized = []
        for item in items:
            if not isinstance(item, dict) or "tool" not in item:
                raise ConfigError(
                    f"Scenario {path} each expected_tool_args entry needs a `tool` key"
                )
            normalized.append(
                {"tool": str(item["tool"]), "arguments": dict(item.get("arguments", {}))}
            )
        outcome["expected_tool_args"] = normalized
    return outcome


def load_persona(path: Path) -> PersonaConfig:
    data = load_json(path)
    missing = [key for key in ("id", "summary") if key not in data]
    if missing:
        raise ConfigError(f"Persona {path} is missing required keys: {', '.join(missing)}")

    context = data.get("context", {})
    if not isinstance(context, dict):
        raise ConfigError(f"Persona {path} `context` must be an object")

    return PersonaConfig(
        id=str(data["id"]),
        name=str(data.get("name", data["id"])),
        summary=str(data["summary"]),
        traits=[str(item) for item in data.get("traits", [])],
        context={str(key): str(value) for key, value in context.items()},
    )


def load_runner(path: Path | None, fallback_kind: str = "mock") -> RunnerConfig:
    if path is None:
        return RunnerConfig(kind=fallback_kind)

    data = load_json(path)
    return runner_from_dict(data, fallback_kind=fallback_kind, source=str(path))


def runner_from_dict(
    data: dict[str, Any], *, fallback_kind: str = "mock", source: str = "runner"
) -> RunnerConfig:
    """Normalize an inline or file-backed agent runner definition."""
    kind = str(data.get("kind", fallback_kind))
    command = data.get("command", [])
    if not isinstance(command, list):
        raise ConfigError(f"Runner command must be a list in {source}")
    sandbox = data.get("sandbox", {}) or {}
    if not isinstance(sandbox, dict):
        raise ConfigError(f"Runner sandbox must be an object in {source}")
    return RunnerConfig(
        kind=kind,
        command=[str(part) for part in command],
        env={str(key): str(value) for key, value in dict(data.get("env", {})).items()},
        timeout_seconds=int(data.get("timeout_seconds", 180)),
        prompt_mode=str(data.get("prompt_mode", "stdin")),
        parser=str(data.get("parser", "text")),
        sandbox=dict(sandbox),
    )


# --------------------------------------------------------------------------- #
# JSON Schema validation
# --------------------------------------------------------------------------- #
# Ghostlab ships no schema library and declares no third-party runtime
# dependency for one. The subset below is what benchmark output contracts,
# scorer manifests, score inputs, score reports, and judge replies actually use:
# structural keywords plus the numeric and string bounds that make a contract
# enforceable. Anything unrecognized is ignored rather than silently "passing"
# under a different meaning.
_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
}


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#"):
        raise ConfigError(f"Only local JSON Schema $refs are supported, got {ref!r}")
    node: Any = root
    for part in ref.lstrip("#/").split("/"):
        if not part:
            continue
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or key not in node:
            raise ConfigError(f"Unresolvable JSON Schema $ref: {ref}")
        node = node[key]
    if not isinstance(node, dict):
        raise ConfigError(f"JSON Schema $ref does not point at a schema: {ref}")
    return node


def schema_errors(
    instance: Any, schema: dict[str, Any], *, path: str = "$", root: dict[str, Any] | None = None
) -> list[str]:
    """Validate ``instance`` against a JSON Schema subset; returns error strings."""
    root = schema if root is None else root
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    if "$ref" in schema:
        return schema_errors(instance, _resolve_ref(str(schema["$ref"]), root), path=path, root=root)

    errors: list[str] = []
    declared = schema.get("type")
    if declared is not None:
        options = declared if isinstance(declared, list) else [declared]
        if not any(_TYPE_CHECKS.get(str(name), lambda _: False)(instance) for name in options):
            return [f"{path}: expected type {'|'.join(str(o) for o in options)}"]

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and instance not in list(schema["enum"]):
        errors.append(f"{path}: must be one of {schema['enum']!r}")

    for keyword, combinator in (("allOf", all), ("anyOf", any)):
        subschemas = schema.get(keyword)
        if not isinstance(subschemas, list):
            continue
        results = [
            schema_errors(instance, sub, path=path, root=root) for sub in subschemas
        ]
        if keyword == "allOf":
            for result in results:
                errors += result
        elif not combinator(not result for result in results):
            errors.append(f"{path}: does not match any schema in anyOf")
    if isinstance(schema.get("oneOf"), list):
        matched = sum(
            1 for sub in schema["oneOf"] if not schema_errors(instance, sub, path=path, root=root)
        )
        if matched != 1:
            errors.append(f"{path}: must match exactly one schema in oneOf, matched {matched}")
    if isinstance(schema.get("not"), dict) and not schema_errors(
        instance, schema["not"], path=path, root=root
    ):
        errors.append(f"{path}: must not match the 'not' schema")

    if isinstance(instance, dict):
        for name in schema.get("required", []) or []:
            if str(name) not in instance:
                errors.append(f"{path}: missing required property {name!r}")
        properties = schema.get("properties") or {}
        for name, subschema in properties.items():
            if name in instance:
                errors += schema_errors(
                    instance[name], subschema, path=f"{path}.{name}", root=root
                )
        additional = schema.get("additionalProperties")
        if additional is False:
            extra = sorted(set(instance) - set(properties))
            if extra:
                errors.append(f"{path}: unexpected properties {', '.join(extra)}")
        elif isinstance(additional, dict):
            for name in sorted(set(instance) - set(properties)):
                errors += schema_errors(
                    instance[name], additional, path=f"{path}.{name}", root=root
                )
        if isinstance(schema.get("minProperties"), int) and len(instance) < schema["minProperties"]:
            errors.append(f"{path}: needs at least {schema['minProperties']} properties")

    if isinstance(instance, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                errors += schema_errors(item, items, path=f"{path}[{index}]", root=root)
        elif isinstance(items, list):
            for index, (item, subschema) in enumerate(zip(instance, items)):
                errors += schema_errors(item, subschema, path=f"{path}[{index}]", root=root)
        if isinstance(schema.get("minItems"), int) and len(instance) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if isinstance(schema.get("maxItems"), int) and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        if schema.get("uniqueItems") and len(
            {json.dumps(item, sort_keys=True) for item in instance}
        ) != len(instance):
            errors.append(f"{path}: items must be unique")

    if isinstance(instance, str):
        if isinstance(schema.get("minLength"), int) and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
        if isinstance(schema.get("maxLength"), int) and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than maxLength {schema['maxLength']}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            import re

            if re.search(pattern, instance) is None:
                errors.append(f"{path}: does not match pattern {pattern!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        for keyword, ok, text in (
            ("minimum", lambda v, b: v >= b, "is below minimum"),
            ("maximum", lambda v, b: v <= b, "is above maximum"),
            ("exclusiveMinimum", lambda v, b: v > b, "is not above exclusiveMinimum"),
            ("exclusiveMaximum", lambda v, b: v < b, "is not below exclusiveMaximum"),
        ):
            bound = schema.get(keyword)
            if isinstance(bound, (int, float)) and not ok(instance, bound):
                errors.append(f"{path}: {text} {bound}")
        multiple = schema.get("multipleOf")
        if isinstance(multiple, (int, float)) and multiple and instance % multiple != 0:
            errors.append(f"{path}: is not a multiple of {multiple}")

    return errors


def validate_against_schema(instance: Any, schema: dict[str, Any], *, source: str = "document") -> None:
    """Raise :class:`ConfigError` listing every schema violation."""
    errors = schema_errors(instance, schema)
    if errors:
        raise ConfigError(f"{source} does not satisfy its schema: " + "; ".join(errors[:20]))


# --------------------------------------------------------------------------- #
# Artifact runs
# --------------------------------------------------------------------------- #
ARTIFACT_RUN_SCHEMA_VERSION = "ghostlab-artifact-run-v1"


@dataclass(frozen=True)
class ArtifactRunConfig:
    """One configured agent, one prompt, one mutable workspace, declared exports.

    Deliberately separate from :class:`ScenarioConfig`: a scenario is a
    multi-turn conversation graded on messages, while an artifact run is a
    single turn graded on what it left on disk.
    """

    agent_path: Path
    run_dir: Path
    prompt: str
    prompt_source: str = "inline"
    workspace: Path | None = None
    # (remote sandbox path, run-dir relative name)
    exports: tuple[tuple[str, str], ...] = ()
    optional_exports: tuple[tuple[str, str], ...] = ()
    export_workspace: str = ""
    output_contract: Path | None = None
    contract_target: str = ""
    timeout_seconds: int = 0
    sandbox_image: str = ""
    setup_commands: tuple[tuple[str, ...], ...] = ()
    workspace_excludes: tuple[str, ...] = ()
    workspace_retain: tuple[str, ...] = ()
    keep_sandbox: bool = False

    def contract_export(self) -> str:
        """Which downloaded export the output contract applies to."""
        if self.contract_target:
            return self.contract_target
        return self.exports[0][1] if self.exports else ""


def parse_export(value: str) -> tuple[str, str]:
    """Parse ``--export /sandbox/output/x.json=x.json`` into (remote, local)."""
    remote, separator, local = str(value).partition("=")
    remote = remote.strip()
    local = local.strip()
    if not remote.startswith("/"):
        raise ConfigError(f"--export source must be an absolute sandbox path: {value!r}")
    if not separator or not local:
        local = Path(remote).name
    if (
        not local
        or Path(local) == Path(".")
        or Path(local).is_absolute()
        or ".." in Path(local).parts
    ):
        raise ConfigError(f"--export destination must be a name inside the run dir: {value!r}")
    return remote, local


def parse_command(value: str) -> tuple[str, ...]:
    """Parse one JSON argument array without invoking a shell."""
    try:
        command = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"--setup-command must be a JSON array: {exc}") from exc
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part for part in command)
    ):
        raise ConfigError("--setup-command must be a non-empty JSON array of strings")
    return tuple(command)


def _validate_export_destinations(
    required: tuple[tuple[str, str], ...],
    optional: tuple[tuple[str, str], ...],
    workspace_archive: str,
) -> None:
    reserved = {
        "artifact-run.json",
        "events.jsonl",
        "stdout.txt",
        "stderr.txt",
        "prompt.txt",
        "workspace-export",
    }
    destinations = [local for _remote, local in (*required, *optional)]
    if workspace_archive:
        archive = Path(workspace_archive)
        if archive.is_absolute() or ".." in archive.parts or archive.name != workspace_archive:
            raise ConfigError("--export-workspace must be a filename inside the run dir")
        if any(
            workspace_archive == suffix
            for suffix in (".tar.zst", ".tar.gz", ".tgz", ".tar")
        ):
            raise ConfigError("--export-workspace must include a filename before its archive suffix")
        destinations.append(workspace_archive)
        if workspace_archive.endswith(".tar.zst"):
            destinations.append(
                workspace_archive[: -len(".tar.zst")] + ".tar.gz"
            )
        elif workspace_archive.endswith(".tgz"):
            destinations.append(workspace_archive[: -len(".tgz")] + ".tar.gz")
        elif not workspace_archive.endswith((".tar.gz", ".tar")):
            destinations.append(workspace_archive + ".tar.gz")
    paths = [Path(item) for item in destinations]
    for index, path in enumerate(paths):
        if path.parts and path.parts[0] in reserved:
            raise ConfigError(f"export destination {path.as_posix()!r} is reserved")
        for other in paths[index + 1 :]:
            if path == other or path in other.parents or other in path.parents:
                raise ConfigError(
                    "export destinations overlap: "
                    f"{path.as_posix()!r} and {other.as_posix()!r}"
                )


def artifact_run_config(
    *,
    agent: Path,
    run_dir: Path,
    prompt: str = "",
    prompt_file: Path | None = None,
    workspace: Path | None = None,
    exports: list[str] | None = None,
    optional_exports: list[str] | None = None,
    export_workspace: str = "",
    output_contract: Path | None = None,
    contract_target: str = "",
    timeout_seconds: int = 0,
    sandbox_image: str = "",
    setup_commands: list[str] | None = None,
    workspace_excludes: list[str] | None = None,
    workspace_retain: list[str] | None = None,
    keep_sandbox: bool = False,
) -> ArtifactRunConfig:
    if prompt_file is not None:
        if not Path(prompt_file).is_file():
            raise ConfigError(f"Prompt file not found: {prompt_file}")
        prompt_text = Path(prompt_file).read_text(encoding="utf-8")
        prompt_source = str(Path(prompt_file).resolve())
    else:
        prompt_text = prompt
        prompt_source = "inline"
    if not prompt_text.strip():
        raise ConfigError("An artifact run needs a non-empty prompt (--prompt or --prompt-file)")
    if output_contract is not None and not Path(output_contract).is_file():
        raise ConfigError(f"Output contract not found: {output_contract}")
    parsed = tuple(parse_export(item) for item in (exports or []))
    parsed_optional = tuple(parse_export(item) for item in (optional_exports or []))
    if output_contract is not None and not parsed and not contract_target:
        raise ConfigError("--output-contract needs an --export to validate")
    if contract_target and output_contract is None:
        raise ConfigError("--contract-target requires --output-contract")
    _validate_export_destinations(parsed, parsed_optional, str(export_workspace or ""))
    if contract_target:
        contract_path = Path(contract_target)
        if (
            contract_path.is_absolute()
            or contract_path == Path(".")
            or ".." in contract_path.parts
            or contract_target not in {local for _remote, local in (*parsed, *parsed_optional)}
        ):
            raise ConfigError("--contract-target must name a declared export inside the run dir")
    return ArtifactRunConfig(
        agent_path=Path(agent).expanduser(),
        run_dir=Path(run_dir).expanduser(),
        prompt=prompt_text,
        prompt_source=prompt_source,
        workspace=Path(workspace).expanduser() if workspace else None,
        exports=parsed,
        optional_exports=parsed_optional,
        export_workspace=str(export_workspace or ""),
        output_contract=Path(output_contract).expanduser() if output_contract else None,
        contract_target=str(contract_target or ""),
        timeout_seconds=int(timeout_seconds or 0),
        sandbox_image=str(sandbox_image or ""),
        setup_commands=tuple(parse_command(item) for item in (setup_commands or [])),
        workspace_excludes=tuple(str(item) for item in (workspace_excludes or [])),
        workspace_retain=tuple(str(item) for item in (workspace_retain or [])),
        keep_sandbox=bool(keep_sandbox),
    )


# --------------------------------------------------------------------------- #
# Scorer packages
# --------------------------------------------------------------------------- #
SCORER_SCHEMA_VERSION = "retro-scorer-v1"
SCORER_MODES = ("deterministic", "judge", "hybrid", "agentic")
SCORER_COMPONENT_KINDS = ("deterministic", "judge", "performance")
# A judge or agentic scorer talks to a provider from inside a sandbox; the
# permission floor below is what keeps it from also executing candidate code.
JUDGE_PERMISSION_FLOOR = {"bash": "deny", "edit": "deny", "external_directory": "deny"}
PERFORMANCE_REQUIRED_KEYS = (
    "metric", "warmup_runs", "measured_runs", "statistic", "comparison",
    "full_credit_at", "zero_credit_at", "per_run_timeout_seconds",
)


@dataclass(frozen=True)
class ScorerComponent:
    id: str
    kind: str
    weight: float
    hard_gate: bool
    value_range: tuple[float, float] = (0.0, 1.0)
    performance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScorerConfig:
    """A validated ``scorer.json`` plus the package directory it came from."""

    path: Path
    package_dir: Path
    task_id: str
    mode: str
    entrypoint: tuple[str, ...]
    runtime: dict[str, Any]
    components: tuple[ScorerComponent, ...]
    pass_threshold: float
    judge: dict[str, Any] = field(default_factory=dict)
    required_artifacts: tuple[str, ...] = ()
    package_sha256: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCORER_SCHEMA_VERSION

    @property
    def component_ids(self) -> tuple[str, ...]:
        return tuple(component.id for component in self.components)

    def component(self, component_id: str) -> ScorerComponent | None:
        for component in self.components:
            if component.id == component_id:
                return component
        return None

    def judge_criteria(self) -> tuple[str, ...]:
        """Residual criteria, and only when a judge is actually configured."""
        if not self.judge.get("enabled", False):
            return ()
        return tuple(str(item) for item in (self.judge.get("criteria") or []))

    def deterministic_ids(self) -> tuple[str, ...]:
        residual = set(self.judge_criteria())
        return tuple(
            component.id for component in self.components if component.id not in residual
        )

    def timeout_seconds(self) -> int:
        return int(self.runtime.get("timeout_seconds", 900))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def _scorer_path(package_dir: Path, value: Any, source: str, field_name: str) -> Path:
    """Resolve a manifest path against the package, refusing to leave it.

    A scorer package is the unit that gets hashed, audited, and mounted. A
    manifest that points at ``/etc/passwd`` or ``../../oracle.patch`` would put
    content inside the scorer that no audit ever saw and no package hash covers,
    so those are rejected rather than resolved.
    """
    raw = str(value or "")
    _require(bool(raw), f"Scorer {source}: {field_name} is required")
    package_dir = package_dir.resolve()
    if raw.startswith("/scorer/"):
        candidate = package_dir / raw[len("/scorer/"):]
    elif raw.startswith("/fixtures/"):
        candidate = package_dir / "fixtures" / raw[len("/fixtures/"):]
    elif Path(raw).is_absolute():
        candidate = Path(raw)
    else:
        candidate = package_dir / raw
    resolved = candidate.resolve()
    _require(
        resolved.is_relative_to(package_dir),
        f"Scorer {source}: {field_name} must resolve inside the scorer package "
        f"{package_dir}, got {raw!r}",
    )
    _require(resolved.is_file(), f"Scorer {source}: {field_name} not found: {candidate}")
    return resolved


def load_scorer(path: Path) -> ScorerConfig:
    """Load and strictly validate a ``retro-scorer-v1`` manifest.

    Strict on purpose: a scorer that cannot be trusted to describe itself
    cannot be trusted to produce a number that decides whether an agent passed.
    """
    path = Path(path).expanduser().resolve()
    data = load_json(path)
    source = str(path)
    package_dir = path.parent

    version = str(data.get("schema_version", ""))
    _require(
        version == SCORER_SCHEMA_VERSION,
        f"Scorer {source}: schema_version must be {SCORER_SCHEMA_VERSION!r}, got {version!r}",
    )
    task_id = str(data.get("task_id", ""))
    _require(bool(task_id), f"Scorer {source}: task_id is required")
    mode = str(data.get("mode", ""))
    _require(
        mode in SCORER_MODES,
        f"Scorer {source}: mode must be one of {', '.join(SCORER_MODES)}, got {mode!r}",
    )

    entrypoint = data.get("entrypoint", []) or []
    _require(isinstance(entrypoint, list), f"Scorer {source}: entrypoint must be a list")
    if mode != "judge":
        _require(
            bool(entrypoint),
            f"Scorer {source}: mode {mode!r} requires a non-empty entrypoint",
        )

    runtime = data.get("runtime", {}) or {}
    _require(isinstance(runtime, dict), f"Scorer {source}: runtime must be an object")
    network = str(runtime.get("network", "disabled"))
    _require(
        network in ("disabled", "policy"),
        f"Scorer {source}: runtime.network must be 'disabled' or 'policy'",
    )
    candidate_mount = str(runtime.get("candidate_mount", "read_only"))
    _require(
        candidate_mount == "read_only",
        f"Scorer {source}: runtime.candidate_mount must be 'read_only'",
    )
    timeout = runtime.get("timeout_seconds", 900)
    _require(
        isinstance(timeout, int) and timeout > 0,
        f"Scorer {source}: runtime.timeout_seconds must be a positive integer",
    )

    raw_components = data.get("components", []) or []
    _require(
        isinstance(raw_components, list) and bool(raw_components),
        f"Scorer {source}: components must be a non-empty list",
    )
    components: list[ScorerComponent] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_components):
        label = f"components[{index}]"
        _require(isinstance(item, dict), f"Scorer {source}: {label} must be an object")
        component_id = str(item.get("id", ""))
        _require(bool(component_id), f"Scorer {source}: {label}.id is required")
        _require(
            component_id not in seen, f"Scorer {source}: duplicate component id {component_id!r}"
        )
        seen.add(component_id)
        kind = str(item.get("kind", ""))
        _require(
            kind in SCORER_COMPONENT_KINDS,
            f"Scorer {source}: {label}.kind must be one of {', '.join(SCORER_COMPONENT_KINDS)}",
        )
        weight = item.get("weight")
        _require(
            isinstance(weight, (int, float)) and not isinstance(weight, bool) and 0.0 <= float(weight) <= 1.0,
            f"Scorer {source}: {label}.weight must be a number in [0, 1]",
        )
        value_range = item.get("range", [0.0, 1.0])
        _require(
            isinstance(value_range, list)
            and len(value_range) == 2
            and all(
                isinstance(bound, (int, float)) and not isinstance(bound, bool)
                for bound in value_range
            )
            and [float(value_range[0]), float(value_range[1])] == [0.0, 1.0],
            f"Scorer {source}: {label}.range must be [0.0, 1.0]",
        )
        performance = dict(item.get("performance") or {})
        if kind == "performance":
            missing = [key for key in PERFORMANCE_REQUIRED_KEYS if key not in performance]
            _require(
                not missing,
                f"Scorer {source}: {label} is a performance component and must declare "
                + ", ".join(missing),
            )
        components.append(
            ScorerComponent(
                id=component_id,
                kind=kind,
                weight=float(weight),
                hard_gate=bool(item.get("hard_gate", False)),
                value_range=(0.0, 1.0),
                performance=performance,
            )
        )
    total_weight = sum(component.weight for component in components)
    _require(
        abs(total_weight - 1.0) <= 1e-9,
        f"Scorer {source}: component weights must sum to 1.0 within 1e-9, got {total_weight!r}",
    )

    threshold = data.get("pass_threshold", 0.8)
    _require(
        isinstance(threshold, (int, float))
        and not isinstance(threshold, bool)
        and 0.0 <= float(threshold) <= 1.0,
        f"Scorer {source}: pass_threshold must be a number in [0, 1]",
    )

    judge = dict(data.get("judge") or {})
    judge_components = {
        component.id for component in components if component.kind == "judge"
    }
    judge_active = mode in ("judge", "hybrid", "agentic") or bool(judge_components)
    if judge_active:
        _require(
            bool(judge) and bool(judge.get("enabled", False)),
            f"Scorer {source}: mode {mode!r} requires an enabled judge block",
        )
        criteria = [str(item) for item in (judge.get("criteria") or [])]
        _require(bool(criteria), f"Scorer {source}: judge.criteria must be non-empty")
        unknown = sorted(set(criteria) - set(seen))
        _require(
            not unknown,
            f"Scorer {source}: judge.criteria reference unknown components: {', '.join(unknown)}",
        )
        unclaimed = sorted(judge_components - set(criteria))
        _require(
            not unclaimed,
            f"Scorer {source}: judge components not declared in judge.criteria: "
            + ", ".join(unclaimed),
        )
        judge["criteria"] = criteria
        judge["agent_config"] = str(
            _scorer_path(package_dir, judge.get("agent_config"), source, "judge.agent_config")
        )
        judge["prompt"] = str(
            _scorer_path(package_dir, judge.get("prompt"), source, "judge.prompt")
        )
        judge["output_schema"] = str(
            _scorer_path(package_dir, judge.get("output_schema"), source, "judge.output_schema")
        )
        if mode == "hybrid":
            _require(
                bool(set(seen) - set(criteria)),
                f"Scorer {source}: hybrid mode needs at least one deterministic component",
            )
    elif judge:
        # A residual judge only exists in a judge-bearing mode. Leaving criteria
        # on an inactive block would let a `deterministic` scorer schedule a
        # judge phase against paths this loader never validated.
        _require(
            not judge.get("criteria"),
            f"Scorer {source}: mode {mode!r} declares judge.criteria "
            f"{list(judge.get('criteria') or [])!r} but has no judge component; "
            "use mode 'hybrid' or 'judge', or drop the criteria",
        )
        _require(
            not judge.get("enabled", False),
            f"Scorer {source}: judge.enabled is true but mode {mode!r} has no judge "
            "component for it to score",
        )
        judge = {}

    required_artifacts = data.get("required_artifacts", []) or []
    _require(
        isinstance(required_artifacts, list),
        f"Scorer {source}: required_artifacts must be a list",
    )

    return ScorerConfig(
        path=path,
        package_dir=package_dir,
        task_id=task_id,
        mode=mode,
        entrypoint=tuple(str(part) for part in entrypoint),
        runtime=dict(runtime),
        components=tuple(components),
        pass_threshold=float(threshold),
        judge=judge,
        required_artifacts=tuple(str(item) for item in required_artifacts),
        package_sha256=str(data.get("package_sha256", "")),
        raw=dict(data),
        schema_version=version,
    )
