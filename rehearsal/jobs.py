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
    JOB_WORKSPACE,
    GhostlabSpec,
    save_spec,
    spec_from_target,
)

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
        f"# ghostlab job '{spec.id}' — one self-contained MCP evaluation.",
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


def build_codex_aut_runner(spec: GhostlabSpec) -> dict:
    """Synthesize a codex agent-under-test runner config for this job's target.

    Wires the job's target MCP straight into a codex process via `-c
    mcp_servers.<id>...` overrides (mirrors the hand-written examples in
    `runners/codex-cortex-*-aut.json`), so `ghostlab create` can turn on
    semantic/security testing without the user hand-editing a runner JSON.
    Bearer-token auth (``Authorization: Bearer ${VAR}``) is detected and wired
    via codex's `bearer_token_env_var`; anything else is left unwired (the
    caller should tell the user auth wasn't configured, not fail the wizard).
    """
    target = spec.target_config()
    connection = target.connection or {}
    command = [
        "codex", "--sandbox", "read-only", "-a", "never",
    ]
    if target.transport == "stdio":
        parts = connection.get("command") or []
        if isinstance(parts, str):
            parts = [parts]
        parts = [*parts, *(connection.get("args") or [])]
        command += ["-c", f"mcp_servers.{spec.id}.command={json.dumps([str(p) for p in parts])}"]
    else:
        url = connection.get("url", "")
        command += ["-c", f"mcp_servers.{spec.id}.url={json.dumps(str(url))}"]
        for value in (connection.get("headers") or {}).values():
            match = _BEARER_ENV_RE.search(str(value))
            if match:
                command += [
                    "-c",
                    f"mcp_servers.{spec.id}.bearer_token_env_var={json.dumps(match.group(1))}",
                ]
                break
    command += ["exec", "--json", "--skip-git-repo-check", "-"]
    return {
        "kind": "process",
        "command": command,
        "env": {},
        "timeout_seconds": 600,
        "prompt_mode": "stdin",
        "parser": "codex-json",
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
    spec.hosts.append({
        "id": "aut",
        "kind": "process",
        "config_ref": f"{RUNNERS_DIR}/{AUT_RUNNER_FILE}",
        "roles": ["agent_under_test"],
    })
    save_spec(spec, spec_path)
    return runner_path


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
    save_spec(spec, spec_path, header=job_header(spec))
    return spec_path
