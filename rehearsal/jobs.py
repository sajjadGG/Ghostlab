"""Jobs — self-contained evaluation directories.

A *job* is one MCP evaluation, and everything about it lives under one folder:

    jobs/<name>/
      job.yaml          # the whole config: target, setup, hosts, generation,
                        # test, prompts, gates — populated with editable defaults
      test-plan.yaml    # produced by `ghostlab plan`
      workspace/        # discover/, generated/, test/ artifacts + ghostlab.sqlite3
      runs/             # dual-agent run output

A job is just a `GhostlabSpec` re-homed under the job dir (its `workspace` points
at the sibling `workspace/` folder), so every stage resolves paths relative to
`job.yaml` exactly as before. This module is the thin layer that finds, creates,
and seeds those directories; the heavy lifting stays in `spec.py`.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .config import ConfigError, TargetConfig
from .spec import (
    DEFAULT_GENERATION,
    DEFAULT_PROMPTS,
    DEFAULT_TEST,
    JOB_WORKSPACE,
    GhostlabSpec,
    save_spec,
    spec_from_target,
    spec_from_skill,
)
from .sandbox import DEFAULT_SANDBOX

JOB_FILE = "job.yaml"
DEFAULT_JOBS_DIR = "jobs"
RUNNERS_DIR = "runners"
AUT_RUNNER_FILE = "aut.json"

_BEARER_ENV_RE = re.compile(r"Bearer\s+\$\{(\w+)\}")

# Documented in the generated job.yaml so users know what each prompt override
# can interpolate. Keep in sync with the *_TEMPLATE placeholders in the prompt
# builders (rehearsal/prompts.py and the generation modules).
_PROMPT_PLACEHOLDERS = {
    "aut": "target_id, transport, capabilities, mcp_config_path, scenario_id, "
           "scenario_title, goal, transcript, user_message",
    "agent_aut": "agent_definition, agent_instructions, skill_instructions, scenario_id, "
                 "scenario_title, goal, transcript, user_message",
    "skill_aut": "target_id, transport, capabilities, mcp_config_path, scenario_id, "
                 "scenario_title, goal, transcript, user_message",
    "user_emulator": "persona, goal, widget_section, transcript, last_assistant_message",
    "judge": "goal, criteria_block, signals_block, tools_line, tool_calls, transcript",
    "critique": "goal, tools, transcript",
    "persona_gen": "mcp, domain_summary, categories, n",
    "scenario_gen": "profile_digest, persona_section, n, persona_field_help",
    "profile": "digest, families",
}


def jobs_dir() -> Path:
    """Root directory that holds all jobs (override with $GHOSTLAB_JOBS_DIR)."""
    return Path(os.environ.get("GHOSTLAB_JOBS_DIR", DEFAULT_JOBS_DIR))


def slugify(name: str) -> str:
    """Filesystem-safe kebab-case slug for a job name."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return slug or "job"


def resolve_job(job: str | None) -> Path:
    """Resolve a ``--job`` value to its ``job.yaml`` path.

    Accepts a job *name* (``jobs/<name>/job.yaml``), a job *directory*, or a
    direct path to a spec file. With no value, auto-detects a ``job.yaml`` in the
    current directory. Raises ``ConfigError`` (with a create hint) otherwise.
    """
    if job:
        path = Path(job)
        if path.suffix.lower() in (".yaml", ".yml", ".json"):
            if path.exists():
                return path
            raise ConfigError(f"Spec file not found: {path}")
        if path.is_dir():
            spec_path = path / JOB_FILE
            if spec_path.exists():
                return spec_path
            raise ConfigError(f"No {JOB_FILE} in {path}")
        candidate = jobs_dir() / job / JOB_FILE
        if candidate.exists():
            return candidate
        raise ConfigError(
            f"Job '{job}' not found (looked for {candidate}). "
            f"Create it with: ghostlab create --name {job}"
        )
    cwd_spec = Path(JOB_FILE)
    if cwd_spec.exists():
        return cwd_spec
    raise ConfigError(
        "No --job given and no job.yaml in the current directory. "
        "Create a job with: ghostlab create"
    )


def job_header(spec: GhostlabSpec) -> str:
    """Comment banner written at the top of a generated job.yaml."""
    lines = [
        f"# ghostlab job '{spec.id}' — one self-contained agent evaluation.",
        "# Everything about this job lives in this folder: job.yaml (this file),",
        "# test-plan.yaml, workspace/ (artifacts + db), and runs/.",
        "#",
        "# Edit any setting below; `ghostlab discover` only rewrites `capabilities`.",
        "# `generation`/`test` values are the defaults each command uses (an explicit",
        "# CLI flag still wins). Leave a `prompts:` entry blank to use the built-in;",
        "# set it to your own template to override. Available {placeholders}:",
    ]
    for name, placeholders in _PROMPT_PLACEHOLDERS.items():
        lines.append(f"#   {name}: {placeholders}")
    return "\n".join(lines) + "\n"


def target_from_url(url: str, *, transport: str = "streamable-http",
                    timeout_seconds: int = 30,
                    headers: dict | None = None) -> TargetConfig:
    """Build a TargetConfig for a plain MCP URL (the wizard's common case).

    ``headers`` may reference environment variables (``${TOKEN}``) so a secret
    stays out of the tracked job.yaml; they are expanded at connection time.
    """
    return TargetConfig(
        id="target",
        transport=transport,
        connection={"url": url, "headers": dict(headers or {})},
        startup={"timeout_seconds": timeout_seconds},
    )


def default_job_spec(
    name: str,
    *,
    target: TargetConfig,
    source_target: str = "",
    generation: dict | None = None,
    test: dict | None = None,
    review_gates: dict | None = None,
    aut_runner: str | None = None,
) -> GhostlabSpec:
    """Build a fully-defaulted job spec (the pure builder the wizard/tests call)."""
    slug = slugify(name)
    spec = spec_from_target(
        target, source_target=source_target, name=name, workspace=JOB_WORKSPACE
    )
    spec.id = slug
    spec.agent = {**(spec.agent or {}), "id": slug}
    if target.transport == "stdio" and source_target:
        source_dir = Path(source_target).expanduser().resolve().parent
        spec.sandbox["uploads"] = [
            {"source": str(source_dir), "target": "/sandbox"}
        ]
        remote_root = Path("/sandbox") / source_dir.name
        spec.sandbox["workdir"] = str(remote_root)

        def mapped(value):
            text = str(value)
            path = Path(text).expanduser()
            if not path.is_absolute():
                return text
            try:
                relative = path.resolve().relative_to(source_dir)
            except ValueError:
                return text
            return str(remote_root / relative)

        connection = dict(spec.target.get("connection", {}))
        raw_command = connection.get("command")
        if isinstance(raw_command, list):
            connection["command"] = [mapped(part) for part in raw_command]
        elif raw_command:
            connection["command"] = mapped(raw_command)
        connection["args"] = [mapped(part) for part in connection.get("args", [])]
        mcps = ((spec.agent or {}).get("inputs", {}) or {}).get("mcps", []) or []
        if mcps:
            mcps[0]["connection"] = dict(connection)
    if generation:
        spec.generation = {**spec.generation, **{k: v for k, v in generation.items() if v is not None}}
    if test:
        spec.test = {**spec.test, **{k: v for k, v in test.items() if v is not None}}
    if review_gates:
        gates = {**spec.review.get("gates", {}), **review_gates}
        spec.review = {**spec.review, "gates": gates}
    if aut_runner:
        spec.hosts.append({
            "id": "aut",
            "kind": "process",
            "config_ref": aut_runner,
            "roles": ["agent_under_test"],
        })
    return spec


def default_skill_job_spec(
    name: str, *, skill_path: Path, generation: dict | None = None,
    review_gates: dict | None = None, aut_runner: str | None = None,
) -> GhostlabSpec:
    spec = spec_from_skill(skill_path, name=name, workspace=JOB_WORKSPACE)
    if generation:
        spec.generation = {**spec.generation, **generation}
    if review_gates:
        spec.review = {"gates": {**spec.review.get("gates", {}), **review_gates}}
    if aut_runner:
        spec.hosts.append({
            "id": "aut", "kind": "process", "config_ref": aut_runner,
            "roles": ["agent_under_test"],
        })
    return spec


def default_agent_job_spec(
    name: str, *, agent: dict, sandbox: dict | None = None,
    generation: dict | None = None, review_gates: dict | None = None,
) -> GhostlabSpec:
    """Build a job around an arbitrary composed agent definition."""
    slug = slugify(name)
    inputs = dict(agent.get("inputs", {}) or {})
    mcps = inputs.get("mcps", []) or []
    skills = inputs.get("skills", []) or []
    if mcps:
        first = dict(mcps[0])
        target = {
            "kind": "mcp", "transport": first.get("transport", "streamable-http"),
            "connection": dict(first.get("connection", {})),
            "capabilities": dict(first.get("capabilities", {})),
            "startup": dict(first.get("startup", {})),
        }
    elif skills:
        first = skills[0]
        skill_path = first.get("path") if isinstance(first, dict) else first
        target = {
            "kind": "skill", "transport": "skill",
            "connection": {"path": str(skill_path)}, "capabilities": {}, "startup": {},
        }
    else:
        target = {
            "kind": "agent", "transport": "agent", "connection": {},
            "capabilities": {}, "startup": {},
        }
    spec = GhostlabSpec(
        id=slug, name=name, workspace=JOB_WORKSPACE, target=target,
        agent={**agent, "id": str(agent.get("id") or slug)},
        sandbox={**DEFAULT_SANDBOX, **dict(sandbox or {})},
        setup={"commands": [], "health": [], "reset": [], "teardown": [], "fixtures": []},
        hosts=[], capabilities={}, generation=dict(DEFAULT_GENERATION),
        test=dict(DEFAULT_TEST), prompts=dict(DEFAULT_PROMPTS), test_plan={},
        review={"gates": {"min_pass_rate": 0.9}},
    )
    if generation:
        spec.generation.update(generation)
    if review_gates:
        spec.review["gates"].update(review_gates)
    return spec


def _set_cli_option(command: list[str], flags: tuple[str, ...], value: str) -> list[str]:
    """Replace a two-token CLI option, or insert it before `exec`."""
    cleaned: list[str] = []
    skip = False
    for index, part in enumerate(command):
        if skip:
            skip = False
            continue
        if part in flags:
            if index + 1 < len(command):
                skip = True
            continue
        cleaned.append(part)
    if not value:
        return cleaned
    position = cleaned.index("exec") if "exec" in cleaned else len(cleaned)
    return [*cleaned[:position], flags[0], value, *cleaned[position:]]


def configure_codex_runner(
    runner: dict, *, model: str = "", kind: str = "", timeout_seconds: int | None = None,
    approval_mode: str = "", codex_sandbox: str = "", codex_bin: str = "",
) -> dict:
    """Apply explicit Codex runtime settings while preserving MCP overrides."""
    configured = dict(runner)
    command = [str(part) for part in configured.get("command", [])]
    if not command or (
        Path(command[0]).name != "codex" and configured.get("parser") != "codex-json"
    ):
        return configured
    if codex_bin:
        command[0] = codex_bin
    if model:
        command = _set_cli_option(command, ("-m", "--model"), model)
    if approval_mode:
        command = _set_cli_option(command, ("-a", "--ask-for-approval"), approval_mode)
    if codex_sandbox:
        command = _set_cli_option(command, ("--sandbox",), codex_sandbox)
    configured["command"] = command
    if kind:
        configured["kind"] = kind
    if timeout_seconds is not None:
        configured["timeout_seconds"] = int(timeout_seconds)
    return configured


def build_codex_aut_runner(
    spec: GhostlabSpec, *, model: str = "", kind: str = "process",
    timeout_seconds: int = 600, approval_mode: str = "never",
    codex_sandbox: str = "read-only", codex_bin: str = "codex",
) -> dict:
    """Synthesize a codex agent-under-test runner config for this job's target.

    Wires the job's target MCP straight into a codex process via `-c
    mcp_servers.<id>...` overrides (mirrors the hand-written examples in
    `runners/codex-cortex-*-aut.json`), so `ghostlab create` can turn on
    semantic/security testing without the user hand-editing a runner JSON.
    Bearer-token auth (``Authorization: Bearer ${VAR}``) is detected and wired
    via codex's `bearer_token_env_var`; anything else is left unwired (the
    caller should tell the user auth wasn't configured, not fail the wizard).
    """
    command = [codex_bin, "--sandbox", codex_sandbox, "-a", approval_mode]
    if model:
        command += ["-m", model]
    agent_mcps = ((spec.agent or {}).get("inputs", {}) or {}).get("mcps", []) or []
    if not agent_mcps and spec.target_type == "mcp":
        target = spec.target_config()
        agent_mcps = [{
            "id": target.id, "transport": target.transport,
            "connection": target.connection,
        }]
    for entry in agent_mcps:
        server_id = str(entry.get("id") or spec.id)
        transport = str(entry.get("transport") or "streamable-http")
        connection = dict(entry.get("connection") or {})
        if transport == "stdio":
            parts = connection.get("command") or []
            if isinstance(parts, str):
                parts = [parts]
            parts = [*parts, *(connection.get("args") or [])]
            command += [
                "-c", f"mcp_servers.{server_id}.command={json.dumps([str(p) for p in parts])}",
            ]
            continue
        url = connection.get("url", "")
        command += ["-c", f"mcp_servers.{server_id}.url={json.dumps(str(url))}"]
        for value in (connection.get("headers") or {}).values():
            match = _BEARER_ENV_RE.search(str(value))
            if match:
                command += [
                    "-c",
                    f"mcp_servers.{server_id}.bearer_token_env_var={json.dumps(match.group(1))}",
                ]
                break
    command += ["exec", "--json", "--skip-git-repo-check", "-"]
    return {
        "kind": kind,
        "command": command,
        "env": {},
        "timeout_seconds": timeout_seconds,
        "prompt_mode": "stdin",
        "parser": "codex-json",
        "sandbox": dict(spec.sandbox),
    }


def build_opencode_aut_runner(
    spec: GhostlabSpec, spec_path: Path, *, model: str = "", timeout_seconds: int = 600,
    opencode_bin: str = "",
) -> dict:
    """Synthesize an opencode agent-under-test runner for this job's target.

    The opencode counterpart of :func:`build_codex_aut_runner`. Where codex takes
    the MCP as `-c` overrides, opencode reads a project `opencode.json`, so the
    job gets one written under `runners/opencode-aut/` and the runner simply
    points at that directory.
    """
    from .runner_presets import opencode_aut_runner

    agent_mcps = ((spec.agent or {}).get("inputs", {}) or {}).get("mcps", []) or []
    if agent_mcps:
        entry = agent_mcps[0]
        target = TargetConfig(
            id=str(entry.get("id") or spec.id),
            transport=str(entry.get("transport") or "streamable-http"),
            connection=dict(entry.get("connection") or {}),
        )
    else:
        target = spec.target_config()

    project_dir = spec_path.resolve().parent / RUNNERS_DIR / "opencode-aut"
    runner = opencode_aut_runner(
        target, project_dir, timeout_seconds=timeout_seconds,
        opencode_bin=opencode_bin, model=model,
    )
    return {
        "kind": runner.kind,
        "command": list(runner.command),
        "env": {},
        "timeout_seconds": runner.timeout_seconds,
        "prompt_mode": runner.prompt_mode,
        "parser": runner.parser,
        # opencode drives the host's own MCP process; OpenShell wrapping of the
        # agent CLI itself is not supported yet, so keep the boundary explicit.
        "sandbox": {"backend": "local"},
    }


def add_aut_host(spec: GhostlabSpec, spec_path: Path, runner_config: dict) -> Path:
    """Write ``runner_config`` under the job dir and append it as an AUT host.

    Returns the runner JSON path (relative `config_ref` is stored in the spec
    so `job.yaml` stays portable). No-op-safe to call at most once per job —
    callers should check `spec.hosts` for an existing process/codex-session
    host first, since this always appends.
    """
    job_dir = spec_path.resolve().parent
    runners_dir = job_dir / RUNNERS_DIR
    runners_dir.mkdir(exist_ok=True)
    runner_path = runners_dir / AUT_RUNNER_FILE
    runner_path.write_text(json.dumps(runner_config, indent=2) + "\n", encoding="utf-8")
    runner_kind = str(runner_config.get("kind", "process"))
    spec.hosts.append({
        "id": "aut",
        "kind": runner_kind,
        "config_ref": f"{RUNNERS_DIR}/{AUT_RUNNER_FILE}",
        "roles": ["agent_under_test"],
    })
    spec.agent = {
        **(spec.agent or {"id": spec.id, "inputs": {"mcps": [], "skills": []}}),
        "runner": dict(runner_config),
    }
    save_spec(spec, spec_path)
    return runner_path


def update_agent_runtime(
    spec: GhostlabSpec, spec_path: Path, *, model: str, kind: str,
    timeout_seconds: int, approval_mode: str, codex_sandbox: str, codex_bin: str,
    user_model: str, generation_model: str, judge_model: str,
) -> None:
    """Persist runtime choices to the inline agent and any materialized host file.

    An agent config that declares its own non-codex runtime is left alone: those
    settings are the agent under test, not wizard defaults to be overwritten.
    """
    existing = dict((spec.agent or {}).get("runtime") or {})
    if existing and str(existing.get("backend") or "") not in ("", "codex"):
        spec.generation = {
            **(spec.generation or {}),
            "backend": str(existing["backend"]),
            "model": generation_model or str(existing.get("model") or ""),
        }
        spec.test = {
            **(spec.test or {}), "user_model": user_model, "judge_model": judge_model,
        }
        return

    runner = dict((spec.agent or {}).get("runner") or {})
    if not runner:
        runner = build_codex_aut_runner(spec)
    runner = configure_codex_runner(
        runner, model=model, kind=kind, timeout_seconds=timeout_seconds,
        approval_mode=approval_mode, codex_sandbox=codex_sandbox, codex_bin=codex_bin,
    )
    spec.agent = {
        **(spec.agent or {}),
        "runner": runner,
        "runtime": {
            "backend": "codex", "model": model, "kind": kind,
            "timeout_seconds": timeout_seconds, "approval_mode": approval_mode,
            "codex_sandbox": codex_sandbox, "codex_bin": codex_bin,
        },
    }
    spec.generation = {
        **(spec.generation or {}), "model": generation_model, "codex_bin": codex_bin,
    }
    spec.test = {
        **(spec.test or {}), "user_model": user_model, "judge_model": judge_model,
    }
    for host in spec.hosts or []:
        ref = host.get("config_ref")
        if host.get("kind") not in ("process", "codex-session") or not ref:
            continue
        path = Path(str(ref))
        if not path.is_absolute():
            path = spec_path.resolve().parent / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(runner, indent=2) + "\n", encoding="utf-8")
        host["kind"] = kind
    save_spec(spec, spec_path)


def create_job(
    name: str, spec: GhostlabSpec, *, jobs_root: Path | None = None, force: bool = False
) -> Path:
    """Create ``jobs/<slug>/`` with job.yaml + workspace/ + runs/. Returns job.yaml."""
    root = jobs_root or jobs_dir()
    job_dir = root / slugify(name)
    spec_path = job_dir / JOB_FILE
    if spec_path.exists() and not force:
        raise ConfigError(f"Job already exists: {spec_path} (use --force to overwrite).")
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / JOB_WORKSPACE).mkdir(exist_ok=True)
    (job_dir / "runs").mkdir(exist_ok=True)
    if (spec.agent or {}).get("tests"):
        from .agents import write_agent_scenarios
        from .plan import build_test_plan, write_test_plan

        configured = write_agent_scenarios(spec.agent, job_dir)
        contract = {
            "mcp": spec.agent.get("name", spec.id), "tools": [],
            "counts": {"tools": 0, "resources": 0, "prompts": 0, "ui_tools": 0},
            "findings": [],
        }
        plan = build_test_plan(
            spec.id, contract, [], generated_cases=configured, target_type="agent",
        )
        write_test_plan(plan, job_dir / "test-plan.yaml")
        spec.test_plan = {
            "plan_file": "test-plan.yaml", "generated_at": plan["generated_at"],
            "cases": len(plan["cases"]),
        }
    save_spec(spec, spec_path, header=job_header(spec))
    return spec_path
