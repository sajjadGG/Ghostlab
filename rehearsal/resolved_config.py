"""Resolve the effective agent/runtime configuration for human inspection."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def _option(command: list[str], *flags: str) -> str:
    for index, part in enumerate(command[:-1]):
        if part in flags:
            return command[index + 1]
    return ""


def _codex_default_model() -> tuple[str, str]:
    """Read Codex's top-level model without loading or exposing other config."""
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config_path = codex_home / "config.toml"
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "Codex CLI default (not declared)", "Codex built-in default"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        match = re.match(r"model\s*=\s*(['\"])(.*?)\1\s*(?:#.*)?$", stripped)
        if match:
            return match.group(2), str(config_path)
    return "Codex CLI default (not declared)", str(config_path)


def _describe_runner(
    runner: dict[str, Any], runtime: dict[str, Any], source: str
) -> dict[str, Any]:
    command = [str(part) for part in runner.get("command", [])]
    is_codex = bool(command and (
        Path(command[0]).name == "codex"
        or runtime.get("backend") == "codex"
        or runner.get("parser") == "codex-json"
    ))
    is_copilot = bool(
        runtime.get("backend") == "copilot"
        or runner.get("parser") == "copilot-json"
        or (command and Path(command[0]).name == "copilot")
    )
    is_opencode = bool(
        runtime.get("backend") == "opencode"
        or str(runner.get("parser") or "").startswith("opencode")
        or (command and Path(command[0]).name == "opencode")
    )
    planned = not runner and runtime.get("backend") in ("codex", "opencode", "copilot")
    if is_codex:
        backend = "codex"
    elif is_copilot:
        backend = "copilot"
    elif is_opencode:
        backend = "opencode"
    else:
        backend = "custom" if command else "not configured"
    explicit_model = _option(command, "-m", "--model") or str(runtime.get("model") or "")
    default_model, default_model_source = _codex_default_model()
    if backend == "copilot":
        fallback_model = "Copilot CLI default (auto)"
        fallback_source = "Copilot model routing"
    elif backend == "opencode":
        fallback_model = "OpenCode default (not declared)"
        fallback_source = "OpenCode configuration"
    else:
        fallback_model = default_model
        fallback_source = default_model_source
    configured = backend != "not configured"
    default_kind = (
        "copilot-session"
        if backend == "copilot"
        else "process"
        if backend in ("codex", "opencode")
        else "not configured"
    )
    default_prompt_mode = "append-arg" if backend == "copilot" else "stdin"
    default_parser = {
        "codex": "codex-json",
        "opencode": "opencode-json",
        "copilot": "copilot-json",
    }.get(backend, "text")
    return {
        "source": source if runner else "runtime (pending materialization)" if planned else "not configured",
        "kind": runner.get("kind") or runtime.get("kind") or default_kind,
        "command": command,
        "backend": backend,
        "model": (explicit_model or fallback_model) if configured else "n/a",
        "model_source": (
            "runner command" if explicit_model and _option(command, "-m", "--model")
            else "agent.runtime" if explicit_model
            else fallback_source
        ) if configured else "n/a",
        "agent": _option(command, "--agent") or str(runtime.get("agent") or runtime.get("default_agent") or ""),
        "reasoning_effort": _option(command, "--effort", "--reasoning-effort")
        or str(runtime.get("reasoning_effort") or ""),
        "context": _option(command, "--context") or str(runtime.get("context") or ""),
        "approval_mode": _option(command, "-a", "--ask-for-approval")
        or runtime.get("approval_mode")
        or ("default" if backend == "codex" else "n/a"),
        "codex_sandbox": _option(command, "--sandbox")
        or runtime.get("codex_sandbox")
        or ("default" if backend == "codex" else "n/a"),
        "timeout_seconds": int(runner.get("timeout_seconds") or runtime.get("timeout_seconds") or 180),
        "prompt_mode": runner.get("prompt_mode", default_prompt_mode),
        "parser": runner.get("parser", default_parser),
        "environment_keys": sorted(
            set((runner.get("env") or {}).keys()) | set((runtime.get("env") or {}).keys())
        ),
    }


def _runner(spec, spec_path: Path) -> dict[str, Any]:
    runner = dict((spec.agent or {}).get("runner") or {})
    runtime = dict((spec.agent or {}).get("runtime") or {})
    source = "agent.runner"
    if not runner:
        for host in spec.hosts or []:
            ref = host.get("config_ref")
            if host.get("kind") not in (
                "process", "codex-session", "copilot-session"
            ) or not ref:
                continue
            path = Path(str(ref))
            if not path.is_absolute():
                path = spec_path.resolve().parent / path
            if path.exists():
                runner = json.loads(path.read_text(encoding="utf-8"))
                source = str(path)
                break
    return _describe_runner(runner, runtime, source)


def _user_runner(spec, spec_path: Path) -> dict[str, Any]:
    test = spec.test or {}
    runtime = dict(test.get("user_runtime") or {})
    configured = test.get("user_runner")
    runner: dict[str, Any] = {}
    source = "test.user_runtime"
    if isinstance(configured, dict):
        runner = dict(configured)
        source = "test.user_runner"
    elif isinstance(configured, str) and configured:
        path = Path(configured)
        if not path.is_absolute():
            path = spec_path.resolve().parent / path
        if path.exists():
            runner = json.loads(path.read_text(encoding="utf-8"))
            source = str(path)
    if test.get("user_model") and not runtime.get("model"):
        runtime["model"] = test["user_model"]
    return _describe_runner(runner, runtime, source)


def resolved_job_config(spec, spec_path: Path) -> dict[str, Any]:
    agent = spec.agent or {}
    inputs = agent.get("inputs", {}) or {}
    sandbox = spec.sandbox or {}
    generation = spec.generation or {}
    test = spec.test or {}
    runner = _runner(spec, spec_path)
    user_runner = _user_runner(spec, spec_path)
    codex_default_model, codex_default_source = _codex_default_model()
    return {
        "job": {"id": spec.id, "name": spec.name, "type": spec.target_type},
        "agent": {
            "id": agent.get("id", spec.id),
            "name": agent.get("name", ""),
            "instructions": agent.get("instructions", ""),
            "runner": runner,
            "mcps": inputs.get("mcps", []) or [],
            "skills": inputs.get("skills", []) or [],
            "assets": inputs.get("assets", []) or [],
        },
        "user_emulator": {"runner": user_runner},
        "sandbox": {
            "backend": sandbox.get("backend", "openshell"),
            "image": sandbox.get("image", "base"),
            "workdir": sandbox.get("workdir", "/sandbox"),
            "network": sandbox.get("network", "disabled"),
            "providers": sandbox.get("providers", []) or [],
            "env_allowlist": sandbox.get("env_allowlist", []) or [],
            "uploads": sandbox.get("uploads", []) or [],
            "policy": sandbox.get("policy", ""),
            "keep": bool(sandbox.get("keep", False)),
        },
        "models": {
            "agent_under_test": runner["model"],
            "user_emulator": (
                user_runner["model"]
                if user_runner["model"] != "n/a"
                else test.get("user_model") or codex_default_model
            ),
            "generation": generation.get("model") or codex_default_model,
            "judge": test.get("judge_model") or generation.get("model") or codex_default_model,
            "codex_default_source": codex_default_source,
        },
        "generation": {
            "personas": int(generation.get("personas", 2)),
            "scenarios_per_persona": int(generation.get("scenarios_per_persona", 2)),
            "codex_bin": generation.get("codex_bin") or "auto-detect",
        },
        "test": {
            "judge": bool(test.get("judge", True)),
            "suites": test.get("suites", []) or [],
            "repeat": int(test.get("repeat", 1)),
            "timeout": float(test.get("timeout", 30.0)),
            "approved_only": bool(test.get("approved_only", False)),
        },
        "review_gates": (spec.review or {}).get("gates", {}),
    }
