from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a Rehearsal config file is invalid."""


@dataclass(frozen=True)
class RunnerConfig:
    kind: str
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 180
    prompt_mode: str = "stdin"


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


def load_target(path: Path) -> TargetConfig:
    data = load_json(path)
    missing = [key for key in ("id", "transport", "connection") if key not in data]
    if missing:
        raise ConfigError(f"Target {path} is missing required keys: {', '.join(missing)}")

    return TargetConfig(
        id=str(data["id"]),
        transport=str(data["transport"]),
        connection=dict(data["connection"]),
        capabilities=dict(data.get("capabilities", {})),
        startup=dict(data.get("startup", {})),
    )


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
    )


def load_runner(path: Path | None, fallback_kind: str = "mock") -> RunnerConfig:
    if path is None:
        return RunnerConfig(kind=fallback_kind)

    data = load_json(path)
    kind = str(data.get("kind", fallback_kind))
    command = data.get("command", [])
    if not isinstance(command, list):
        raise ConfigError(f"Runner command must be a list in {path}")

    return RunnerConfig(
        kind=kind,
        command=[str(part) for part in command],
        env={str(key): str(value) for key, value in dict(data.get("env", {})).items()},
        timeout_seconds=int(data.get("timeout_seconds", 180)),
        prompt_mode=str(data.get("prompt_mode", "stdin")),
    )
