"""`ghostlab lab` — set up a configured agent evaluation, step by step.

The other commands assume you already know what you want to test. This one is
for the case where you have an agent and a hunch: it walks from "here is my
agent" to "here are the scenarios I want to run against it", using the model to
propose and the user to decide.

Two rules shape every step:

* nothing generated is used before it is shown — the user accepts, edits, or
  regenerates each artifact;
* every answer lands in ``job.yaml``, so the outcome is a reproducible file
  rather than a conversation that happened once.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from . import termcolor as tc
from .cli_ui import Prompter, render_stage
from .config import ConfigError

TOTAL_STEPS = 10

PERMISSION_PRESETS: dict[str, dict[str, str]] = {
    "read-only": {"bash": "deny", "edit": "deny", "webfetch": "deny",
                  "external_directory": "deny"},
    "edit-workspace": {"bash": "deny", "edit": "allow", "webfetch": "deny",
                       "external_directory": "deny"},
    "full-shell": {"bash": "allow", "edit": "allow", "webfetch": "allow",
                   "external_directory": "deny"},
}

PRESET_BLAST_RADIUS = {
    "read-only": "reads only; cannot change files or run commands",
    "edit-workspace": "can rewrite files in the uploaded workspace copy",
    "full-shell": "can run shell commands inside the sandbox",
}


def authenticated_providers() -> list[str]:
    """Providers the local OpenCode install has credentials for."""
    import json as _json

    from .agent_sandbox import opencode_auth_path

    try:
        data = _json.loads(opencode_auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(name) for name in data] if isinstance(data, dict) else []


def available_models(limit: int = 60) -> list[str]:
    """Models the local OpenCode install can actually reach, best-effort.

    Bare ``opencode models`` omits providers that need a credential lookup, so
    each authenticated provider is queried by name — otherwise the very model
    the user authenticated for is missing from the picker.
    """
    import subprocess

    from .runner_presets import _opencode_bin

    binary = _opencode_bin()

    def query(*args: str) -> list[str]:
        try:
            result = subprocess.run(
                [binary, "models", *args], capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return [
            line.strip() for line in (result.stdout or "").splitlines()
            if "/" in line.strip() and " " not in line.strip()
        ]

    models: list[str] = []
    for provider in authenticated_providers():
        models += query(provider)
    models += query()
    return list(dict.fromkeys(models))[:limit]


def _read_agent_source(path: Path) -> dict[str, Any]:
    """Import an existing OpenCode project or Ghostlab agent config."""
    path = path.expanduser().resolve()
    if path.is_dir():
        candidate = path / "opencode.json"
        if not candidate.is_file():
            raise ConfigError(f"No opencode.json found in {path}")
        path = candidate
    if not path.is_file():
        raise ConfigError(f"Agent source not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else {}
    if path.name == "opencode.json":
        runtime: dict[str, Any] = {"backend": "opencode"}
        for key in ("model", "small_model", "instructions", "skills", "tools",
                    "permission", "default_agent", "subagent_depth"):
            if data.get(key):
                runtime[key] = data[key]
        if data.get("agent"):
            runtime["agents"] = data["agent"]
        mcps = []
        for name, entry in (data.get("mcp") or {}).items():
            if not isinstance(entry, dict) or entry.get("enabled") is False:
                continue
            if entry.get("type") == "local":
                command = list(entry.get("command") or [])
                mcps.append({
                    "id": name, "transport": "stdio",
                    "connection": {
                        "command": command[:1], "args": command[1:],
                        "env": entry.get("environment") or {},
                    },
                })
            else:
                mcps.append({
                    "id": name, "transport": "streamable-http",
                    "connection": {
                        "url": entry.get("url", ""), "headers": entry.get("headers") or {},
                    },
                })
        return {"runtime": runtime, "inputs": {"mcps": mcps, "skills": []},
                "workspace": str(path.parent)}

    from .agents import load_agent_definition

    agent, _sandbox = load_agent_definition(path)
    return agent


def _prompt_paths(prompter: Prompter, message: str) -> list[str]:
    raw = prompter.text(f"{message} (comma-separated, blank to skip)", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def build_agent_interactively(
    prompter: Prompter, name: str, runner_backend: str = ""
) -> dict[str, Any]:
    """Steps 1-6: what the agent *is*."""
    render_stage(1, "Agent source", "Import an existing agent, or start from scratch.", TOTAL_STEPS)
    selected_backend = runner_backend or prompter.select(
        "Coding-agent runner", ["opencode", "copilot"], "opencode"
    )
    source = prompter.select(
        "Where does this agent come from?",
        ["scratch", "opencode project", "agent config"], "scratch",
    )
    agent: dict[str, Any] = {"id": name, "name": name,
                             "runtime": {"backend": selected_backend},
                             "inputs": {"mcps": [], "skills": []}}
    if source != "scratch":
        location = prompter.text(
            "Path to the opencode.json / project dir / agent config", ""
        )
        if location:
            imported = _read_agent_source(Path(location))
            agent = {**agent, **imported}
            agent.setdefault("runtime", {})["backend"] = selected_backend
            print(tc.muted(
                f"  imported: {len((agent.get('inputs') or {}).get('mcps') or [])} MCP(s), "
                f"model={agent['runtime'].get('model') or 'unset'}"
            ))

    render_stage(2, "Purpose", "Your words win over anything the model infers.", TOTAL_STEPS)
    agent["description"] = prompter.text(
        "What is this agent for? (blank = infer it from the configuration)",
        str(agent.get("description") or ""),
    )

    model_detail = (
        "A model available to GitHub Copilot CLI."
        if selected_backend == "copilot"
        else "Only models your OpenCode install can reach."
    )
    render_stage(3, "Model", model_detail, TOTAL_STEPS)
    current = str(agent["runtime"].get("model") or "")
    if selected_backend == "copilot":
        agent["runtime"]["model"] = prompter.text("Model", current or "auto")
        agent["runtime"]["agent"] = prompter.text(
            "Custom agent name (blank uses the default Copilot agent)",
            str(agent["runtime"].get("agent") or ""),
        )
        effort = prompter.select(
            "Reasoning effort",
            ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
            "default",
        )
        if effort != "default":
            agent["runtime"]["reasoning_effort"] = effort
        agent["runtime"]["context"] = prompter.select(
            "Context tier", ["default", "long_context"], "default"
        )
        extra = prompter.text("Additional Copilot CLI arguments", "")
        if extra:
            agent["runtime"]["extra_args"] = shlex.split(extra)
    else:
        models = available_models()
        if models:
            default = current if current in models else models[0]
            agent["runtime"]["model"] = prompter.select("Model", models, default)
        else:
            agent["runtime"]["model"] = prompter.text(
                "Model (provider/model)", current or "github-copilot/claude-sonnet-4.5"
            )

    render_stage(4, "Capabilities", "MCP servers this agent can call.", TOTAL_STEPS)
    config_path = prompter.text(
        "Path to an mcpServers config to import (blank to skip)", ""
    )
    if config_path:
        from .mcp_targets import normalize_target

        raw = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
        names = list((raw.get("mcpServers") or {}).keys())
        chosen = prompter.checkbox("Which servers?", names, names) if names else []
        mcps = list((agent.get("inputs") or {}).get("mcps") or [])
        for server in chosen:
            target = normalize_target(raw, server=server)
            mcps.append({
                "id": target.id, "transport": target.transport,
                "connection": target.connection,
            })
        agent.setdefault("inputs", {})["mcps"] = mcps

    render_stage(5, "Instructions, skills, and code", "", TOTAL_STEPS)
    instructions = _prompt_paths(prompter, "Instruction files (e.g. AGENTS.md)")
    if instructions:
        agent["runtime"]["instructions"] = instructions
    skills = _prompt_paths(prompter, "Skill folders")
    if skills:
        agent["runtime"]["skills"] = {"paths": skills}
    workspace = prompter.text(
        "Workspace the agent operates on (blank for none)",
        str(agent.get("workspace") or ""),
    )
    if workspace:
        agent["workspace"] = workspace

    render_stage(6, "Permissions", "What the agent may do inside the sandbox.", TOTAL_STEPS)
    for preset, blast in PRESET_BLAST_RADIUS.items():
        print(tc.muted(f"  {preset:16} {blast}"))
    preset = prompter.select(
        "Permission preset", list(PERMISSION_PRESETS), "read-only"
    )
    if selected_backend == "copilot":
        agent["runtime"]["allow_all_tools"] = True
        if preset == "read-only":
            agent["runtime"]["deny_tools"] = ["shell", "write"]
        elif preset == "edit-workspace":
            agent["runtime"]["deny_tools"] = ["shell"]
    else:
        agent["runtime"]["permission"] = dict(PERMISSION_PRESETS[preset])
    return agent


def confirm_profile(
    prompter: Prompter, agent: dict[str, Any], backend: Any,
    inspect: "dict[str, Any] | None" = None,
) -> dict[str, Any]:
    """Step 8: infer the agent's purpose and let the user correct it."""
    from .agent_profile import build_agent_profile

    while True:
        print(tc.muted("  inferring purpose from the configuration..."))
        profile = build_agent_profile(agent, backend, inspect)
        print()
        print(tc.heading("  Purpose"))
        print(f"  {profile.get('purpose', '')}")
        print(tc.heading("  Audience"))
        print(f"  {profile.get('audience', '')}")
        print(tc.heading("  Workflows"))
        for workflow in profile.get("workflows", []) or []:
            print(f"  - {workflow.get('name')}: " +
                  " -> ".join(str(step) for step in workflow.get("steps", [])))
        print(tc.heading("  Risk surface"))
        for risk in profile.get("risk_surface", []) or []:
            print(f"  ! {risk.get('risk')}: {risk.get('why')}")
        print()

        choice = prompter.select(
            "Does this describe your agent?", ["accept", "describe it myself", "regenerate"],
            "accept",
        )
        if choice == "accept":
            return profile
        if choice == "describe it myself":
            agent["description"] = prompter.text(
                "Describe the agent in your own words", str(agent.get("description") or "")
            )


def review_scenarios(prompter: Prompter, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Steps 9-10: show what will actually be run, and let the user prune it."""
    if not cases:
        print(tc.muted("  no conversational cases were generated"))
        return cases
    labels = []
    for case in cases:
        scenario = case.get("scenario") or {}
        labels.append(
            f"{scenario.get('title') or case.get('id')} "
            f"[{scenario.get('intent', 'happy_path')}]"
        )
    keep = prompter.checkbox("Which scenarios should run?", labels, labels)
    kept = [case for case, label in zip(cases, labels) if label in keep]
    print(tc.muted(f"  keeping {len(kept)} of {len(cases)} scenario(s)"))
    return kept


def sandbox_settings(
    prompter: Prompter, image_default: str, runner_backend: str = "opencode"
) -> dict[str, Any]:
    """Step 7: the execution boundary, including the credential opt-in."""
    render_stage(7, "Sandbox", "The agent, its MCPs, and its code all run inside.", TOTAL_STEPS)
    image = prompter.text("Sandbox image (name, image ref, or Dockerfile)", image_default)
    credentials = (
        prompter.confirm(
            "Copy your OpenCode credentials into the sandbox? "
            "(required for the agent to call its model)", True,
        )
        if runner_backend == "opencode"
        else False
    )
    if credentials:
        print(tc.muted(
            "  the auth file is uploaded outside the workspace, mode 600, and is "
            "redacted from every report"
        ))
    settings = {
        "backend": "openshell",
        "image": image,
        "credentials": {"opencode_auth": credentials},
    }
    if runner_backend == "copilot":
        settings["env_allowlist"] = [
            "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"
        ]
    return settings
