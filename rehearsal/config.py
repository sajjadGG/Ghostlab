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
