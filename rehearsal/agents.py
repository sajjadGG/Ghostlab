"""Agent-definition normalization.

An evaluated agent is a runner plus composable inputs. MCP-only and skill-only
jobs are compatibility shorthands for this more general shape.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import ConfigError
from .mcp_targets import load_target


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            from .spec import parse_yaml

            data = parse_yaml(text)
        else:
            data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ConfigError(f"Agent config {path} must contain a top-level mapping")
    return data


def _localize_stdio(connection: dict[str, Any], base: Path) -> dict[str, Any]:
    """Resolve a referenced MCP config's relative program paths against itself.

    An agent config points at an MCP config with ``config_ref``, which is
    resolved relative to the agent file — so a `node server.js` sitting beside
    that config should resolve the same way. Only rewrites entries that name a
    file which actually exists there, so flags (`-y`) and package names
    (`safari-mcp`) are left alone.
    """
    resolved = dict(connection)

    def localize(value: Any) -> Any:
        text = str(value)
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            return value
        sibling = base / text
        return str(sibling.resolve()) if sibling.is_file() else value

    raw = resolved.get("command")
    if isinstance(raw, list):
        resolved["command"] = [localize(part) for part in raw]
    elif raw:
        resolved["command"] = localize(raw)
    if resolved.get("args"):
        resolved["args"] = [localize(part) for part in resolved["args"]]
    return resolved


def _resolve(value: Any, base: Path) -> str:
    """Resolve a path referenced by an agent config, relative to that config."""
    candidate = Path(str(value)).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return str(candidate)


def load_agent_definition(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load an agent JSON/YAML and resolve referenced MCPs, skills, and assets.

    Returns ``(agent, sandbox_overrides)`` so the job spec keeps the execution
    boundary separate from the logical agent definition.
    """
    path = path.expanduser().resolve()
    if not path.exists():
        raise ConfigError(f"Agent config not found: {path}")
    data = _load_mapping(path)
    base = path.parent
    runner = data.get("runner", {}) or {}
    if not isinstance(runner, dict):
        raise ConfigError(f"Agent config {path}: runner must be a mapping")
    inputs = data.get("inputs", {}) or {}
    if not isinstance(inputs, dict):
        raise ConfigError(f"Agent config {path}: inputs must be a mapping")

    mcps: list[dict[str, Any]] = []
    for item in inputs.get("mcps", []) or []:
        if not isinstance(item, dict):
            raise ConfigError(f"Agent config {path}: each MCP input must be a mapping")
        if item.get("config_ref"):
            ref = Path(str(item["config_ref"])).expanduser()
            if not ref.is_absolute():
                ref = base / ref
            target = load_target(ref, server=item.get("server"))
            mcps.append({
                "id": target.id, "transport": target.transport,
                "connection": _localize_stdio(target.connection, ref.parent),
                "capabilities": target.capabilities, "startup": target.startup,
            })
        else:
            missing = [key for key in ("id", "transport", "connection") if key not in item]
            if missing:
                raise ConfigError(
                    f"Agent config {path}: MCP input missing {', '.join(missing)}"
                )
            mcps.append(dict(item))

    skills: list[dict[str, Any]] = []
    for item in inputs.get("skills", []) or []:
        entry = dict(item) if isinstance(item, dict) else {"path": str(item)}
        skill_path = Path(str(entry.get("path", ""))).expanduser()
        if not skill_path.is_absolute():
            skill_path = base / skill_path
        if skill_path.is_dir():
            skill_path = skill_path / "SKILL.md"
        if not skill_path.exists():
            raise ConfigError(f"Agent config {path}: skill not found: {skill_path}")
        skills.append({**entry, "path": str(skill_path.resolve())})

    sandbox = dict(data.get("sandbox", {}) or {})
    uploads = list(sandbox.get("uploads", []) or [])
    workspace = data.get("workspace")
    workspace_source: Path | None = None
    if workspace:
        source = Path(str(workspace)).expanduser()
        if not source.is_absolute():
            source = base / source
        workspace_source = source.resolve()
        remote_workspace = str(Path("/sandbox") / workspace_source.name)
        uploads.append({"source": str(workspace_source), "target": "/sandbox"})
        sandbox.setdefault("workdir", remote_workspace)
    for asset in inputs.get("assets", []) or []:
        entry = dict(asset) if isinstance(asset, dict) else {"source": str(asset)}
        source = Path(str(entry.get("source", ""))).expanduser()
        if not source.is_absolute():
            source = base / source
        uploads.append({
            "source": str(source.resolve()),
            "target": str(entry.get("target") or f"/sandbox/assets/{source.name}"),
        })
    sandbox["uploads"] = uploads

    def sandbox_path(value: Any) -> str:
        text = str(value)
        if workspace_source is None:
            return text
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            return text
        try:
            relative = candidate.resolve().relative_to(workspace_source)
        except ValueError:
            return text
        remote_root = Path(str(sandbox.get("workdir") or "/sandbox"))
        return str(remote_root / relative)

    runner = {**runner, "command": [sandbox_path(part) for part in runner.get("command", [])]}
    for mcp in mcps:
        if mcp.get("transport") != "stdio":
            continue
        connection = dict(mcp.get("connection", {}))
        raw_command = connection.get("command")
        if isinstance(raw_command, list):
            connection["command"] = [sandbox_path(part) for part in raw_command]
        elif raw_command:
            connection["command"] = sandbox_path(raw_command)
        connection["args"] = [sandbox_path(part) for part in connection.get("args", [])]
        mcp["connection"] = connection

    agent = {
        "id": str(data.get("id") or path.stem),
        "name": str(data.get("name") or data.get("id") or path.stem),
        "instructions": str(data.get("instructions", "")),
        "runner": dict(runner),
        "inputs": {"mcps": mcps, "skills": skills, "assets": list(inputs.get("assets", []) or [])},
        "tests": list(data.get("tests", []) or []),
    }
    # The declarative runtime is what makes an agent config drivable from a file
    # instead of the wizard: it carries model, instructions, skills, subagents,
    # and permissions, and it feeds purpose inference. `description` is the
    # owner's own words, which purpose inference treats as authoritative.
    if data.get("runtime"):
        runtime = dict(data["runtime"])
        for key in ("instructions",):
            if runtime.get(key):
                runtime[key] = [_resolve(item, base) for item in runtime[key]]
        if (runtime.get("skills") or {}).get("paths"):
            runtime["skills"] = {
                **dict(runtime["skills"]),
                "paths": [_resolve(item, base) for item in runtime["skills"]["paths"]],
            }
        for definition in (runtime.get("agents") or {}).values():
            prompt = (definition or {}).get("prompt")
            if isinstance(prompt, str) and Path(prompt).suffix in (".md", ".txt"):
                definition["prompt"] = _resolve(prompt, base)
        agent["runtime"] = runtime
    if data.get("description"):
        agent["description"] = str(data["description"])
    if workspace_source is not None:
        agent["workspace"] = str(workspace_source)
    return agent, sandbox


def configured_agent_cases(agent: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn inline agent test definitions into conversational plan cases."""
    import re

    cases: list[dict[str, Any]] = []
    for index, test in enumerate(agent.get("tests", []) or [], start=1):
        if not isinstance(test, dict):
            raise ConfigError("agent.tests entries must be mappings")
        raw_id = str(test.get("id") or f"case-{index}")
        case_id = re.sub(r"[^a-z0-9]+", "-", raw_id.lower()).strip("-") or f"case-{index}"
        intent = str(test.get("intent", "happy_path"))
        suite = "security" if intent == "adversarial" else str(test.get("suite", "semantic"))
        cases.append({
            "id": f"{suite}-agent-{case_id}", "suite": suite,
            "kind": "conversational", "title": str(test.get("title") or raw_id),
            "reason": "agent_config:test", "tools": list(test.get("exercises", []) or []),
            "status": str(test.get("status", "proposed")),
            "execution": {"type": "scenario", "scenario": f"scenarios/{case_id}.json"},
        })
    return cases


def write_agent_scenarios(agent: dict[str, Any], job_dir: Path) -> list[dict[str, Any]]:
    """Persist inline agent tests as normal ScenarioConfig JSON files."""
    cases = configured_agent_cases(agent)
    tests = list(agent.get("tests", []) or [])
    scenario_dir = job_dir / "scenarios"
    if cases:
        scenario_dir.mkdir(parents=True, exist_ok=True)
    for test, case in zip(tests, cases):
        scenario_path = job_dir / case["execution"]["scenario"]
        scenario = {
            "id": scenario_path.stem,
            "title": str(test.get("title") or test.get("id") or scenario_path.stem),
            "intent": str(test.get("intent", "happy_path")),
            "persona": str(test.get("persona", "A realistic user")),
            "goal": str(test.get("goal", "")),
            "max_turns": int(test.get("max_turns", 4)),
            "opening_message": str(test.get("opening_message", test.get("goal", ""))),
            "success_criteria": [str(value) for value in test.get("success_criteria", [])],
            "failure_signals": [str(value) for value in test.get("failure_signals", [])],
            "exercises": [str(value) for value in test.get("exercises", [])],
        }
        scenario_path.write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")
    return cases
