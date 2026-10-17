"""Run a configured agent on one task, in any harness, and keep its answer.

Evaluation asks *how well* an agent does its job. Most of the time you just
want the job done: hand a defined agent — model, instructions, skills, MCPs,
workspace — a task and get the answer back. This module is that primitive.

One agent definition drives every supported harness:

* ``codex``    — ``codex exec --json``; MCPs as ``-c mcp_servers.*`` overrides
* ``claude``   — ``claude -p --output-format stream-json``; MCPs via ``--mcp-config``
* ``copilot``  — ``copilot --output-format json``; MCPs via ``--additional-mcp-config``
* ``opencode`` — ``opencode run --format json``; a generated ``opencode.json``
* ``runner``   — the agent's own raw ``runner.command``, as declared

OpenCode loads instruction files and skills natively; for every other harness
they are rendered into a preamble ahead of the task, so the same definition
means the same thing everywhere.

The default boundary is OpenShell: the agent CLI, its stdio MCPs, and an
uploaded copy of its workspace all live in one sandbox that is deleted
afterwards. ``sandbox: local`` runs on the host and must be asked for.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agents import load_agent_definition
from .artifact_run import (
    STATUS_COMPLETED,
    STATUS_HARNESS_ERROR,
    STATUS_SANDBOX_ERROR,
    _classify,
)
from .config import ConfigError
from .runners import RunnerResult
from .sandbox import SandboxError
from .session_provenance import (
    CODEX_ORIGINATOR_ENV,
    GHOSTLAB_ORIGINATOR,
    new_copilot_session_id,
)
from .types import utc_now

EXEC_SCHEMA_VERSION = "ghostlab-exec-v1"
BACKENDS = ("codex", "claude", "copilot", "opencode", "runner")
SANDBOX_WORKSPACE_ROOT = "/sandbox"
SANDBOX_PROJECT_DIR = "/sandbox/agent/run"

PathMap = Callable[[Any], str]


class ExecError(RuntimeError):
    """The run could not be started (bad config, missing input)."""


@dataclass(frozen=True)
class ExecConfig:
    agent_path: Path
    prompt: str
    run_dir: Path
    workspace: Path | None = None
    sandbox: str = ""
    backend: str = ""
    model: str = ""
    timeout_seconds: int = 0
    image: str = ""
    providers: tuple[str, ...] = ()
    exports: tuple[tuple[str, str], ...] = ()
    keep_sandbox: bool = False


@dataclass(frozen=True)
class ExecResult:
    status: str
    answer: str
    manifest: dict[str, Any]
    run_dir: Path

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED


@dataclass
class Invocation:
    """One fully resolved harness command, before any sandbox wrapping."""

    argv: list[str]
    prompt_mode: str  # "stdin" or "append-arg"
    parser: str
    env: dict[str, str] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # name -> content, placed in workdir


def resolve_backend(agent: dict[str, Any], override: str = "") -> str:
    """Which harness runs this agent: explicit flag, runtime.backend, or raw runner."""
    backend = override or str((agent.get("runtime") or {}).get("backend") or "")
    if not backend and (agent.get("runner") or {}).get("command"):
        backend = "runner"
    if not backend:
        raise ExecError(
            "the agent config names no harness: set runtime.backend to one of "
            f"{', '.join(BACKENDS[:-1])}, give it a runner.command, or pass --backend"
        )
    if backend not in BACKENDS:
        raise ExecError(f"unsupported backend {backend!r}; choose from {', '.join(BACKENDS)}")
    if backend == "runner" and not (agent.get("runner") or {}).get("command"):
        raise ExecError("backend 'runner' needs a runner.command in the agent config")
    return backend


def _read(path: Any) -> str:
    try:
        return Path(str(path)).expanduser().read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ExecError(f"cannot read {path}: {exc}") from exc


def _skill_files(agent: dict[str, Any]) -> list[Path]:
    runtime = dict(agent.get("runtime") or {})
    paths = [Path(str(item["path"])) for item in (agent.get("inputs") or {}).get("skills") or []
             if isinstance(item, dict) and item.get("path")]
    for item in ((runtime.get("skills") or {}).get("paths") or []):
        candidate = Path(str(item))
        paths.append(candidate / "SKILL.md" if candidate.is_dir() else candidate)
    return list(dict.fromkeys(paths))


def compose_prompt(agent: dict[str, Any], task: str, backend: str) -> str:
    """Put the agent's instructions and skills in front of the task.

    OpenCode reads instruction files and skills from its own config, so only
    the free-text ``instructions`` travel in the prompt there. Everything else
    gets the full composition, so one definition behaves the same in every
    harness.
    """
    sections: list[str] = []
    if str(agent.get("instructions") or "").strip():
        sections.append(str(agent["instructions"]).strip())
    if backend != "opencode":
        for path in (agent.get("runtime") or {}).get("instructions") or []:
            sections.append(_read(path))
        for skill in _skill_files(agent):
            sections.append(f"## Skill: {skill.parent.name}\n\n{_read(skill)}")
    if not sections:
        return task
    preamble = "\n\n".join(sections)
    return (
        "You are operating as a configured agent. Follow these instructions and "
        f"use these skills when they apply.\n\n{preamble}\n\n---\n\nTask:\n{task}"
    )


def _map_mcps(mcps: list[dict[str, Any]], path_map: PathMap) -> list[dict[str, Any]]:
    """Rewrite stdio MCP program paths to where they live inside the sandbox."""
    mapped = []
    for mcp in mcps:
        entry = dict(mcp)
        if str(entry.get("transport")) == "stdio":
            connection = dict(entry.get("connection") or {})
            raw = connection.get("command") or []
            connection["command"] = (
                path_map(raw) if isinstance(raw, str) else [path_map(part) for part in raw]
            )
            connection["args"] = [path_map(part) for part in connection.get("args") or []]
            entry["connection"] = connection
        mapped.append(entry)
    return mapped


def claude_mcp_config(mcps: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalized Ghostlab MCPs in Claude Code's ``--mcp-config`` shape."""
    servers: dict[str, Any] = {}
    for mcp in mcps:
        server_id = str(mcp.get("id") or "mcp")
        transport = str(mcp.get("transport") or "streamable-http")
        connection = dict(mcp.get("connection") or {})
        if transport == "stdio":
            raw = connection.get("command") or []
            parts = [raw] if isinstance(raw, str) else list(raw)
            parts = [str(part) for part in [*parts, *(connection.get("args") or [])]]
            if not parts:
                raise ExecError(f"stdio MCP {server_id!r} has no command")
            entry: dict[str, Any] = {"type": "stdio", "command": parts[0], "args": parts[1:]}
            if connection.get("env"):
                entry["env"] = {str(k): str(v) for k, v in dict(connection["env"]).items()}
        elif transport in ("sse", "streamable-http", "http"):
            entry = {
                "type": "sse" if transport == "sse" else "http",
                "url": str(connection.get("url") or ""),
            }
            if connection.get("headers"):
                entry["headers"] = {
                    str(k): str(v) for k, v in dict(connection["headers"]).items()
                }
        else:
            raise ExecError(f"unsupported MCP transport for Claude Code: {transport}")
        servers[server_id] = entry
    return {"mcpServers": servers}


def _strings(runtime: dict[str, Any], key: str) -> list[str]:
    value = runtime.get(key) or []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def build_invocation(
    agent: dict[str, Any],
    backend: str,
    *,
    workdir: str,
    model: str = "",
    sandboxed: bool = False,
    path_map: PathMap | None = None,
) -> Invocation:
    """Translate one agent definition into the chosen harness's command line."""
    runtime = dict(agent.get("runtime") or {})
    inside: PathMap = path_map or (lambda value: str(value))
    mcps = _map_mcps(list((agent.get("inputs") or {}).get("mcps") or []), inside)
    model = model or str(runtime.get("model") or "")
    extra = _strings(runtime, "extra_args")

    def binary(key: str, default: str) -> str:
        # Inside the sandbox the image provides the CLI; a host path is meaningless.
        return default if sandboxed else str(runtime.get(key) or default)

    if backend == "codex":
        from .runner_presets import codex_mcp_overrides

        argv = [
            binary("codex_bin", "codex"),
            "--sandbox", str(runtime.get("codex_sandbox") or "workspace-write"),
            "-a", str(runtime.get("approval_mode") or "never"),
        ]
        if model:
            argv += ["-m", model]
        if runtime.get("reasoning_effort"):
            argv += ["-c", f"model_reasoning_effort={json.dumps(str(runtime['reasoning_effort']))}"]
        argv += codex_mcp_overrides(mcps)
        argv += ["-C", workdir, *extra, "exec", "--json", "--skip-git-repo-check", "-"]
        return Invocation(argv, "stdin", "codex-json", {CODEX_ORIGINATOR_ENV: GHOSTLAB_ORIGINATOR})

    if backend == "claude":
        argv = [binary("claude_bin", "claude"), "-p", "--output-format", "stream-json", "--verbose"]
        if model:
            argv += ["--model", model]
        if runtime.get("permission_mode"):
            argv += ["--permission-mode", str(runtime["permission_mode"])]
        if runtime.get("max_turns"):
            argv += ["--max-turns", str(runtime["max_turns"])]
        # Headless Claude Code denies any tool that would need approval, so the
        # agent's own MCPs are pre-approved; built-ins follow allow_tools.
        allowed = [f"mcp__{mcp.get('id') or 'mcp'}" for mcp in mcps] + _strings(runtime, "allow_tools")
        if allowed:
            argv += ["--allowedTools", ",".join(dict.fromkeys(allowed))]
        denied = _strings(runtime, "deny_tools")
        if denied:
            argv += ["--disallowedTools", ",".join(denied)]
        if mcps:
            argv += [
                "--mcp-config",
                json.dumps(claude_mcp_config(mcps), separators=(",", ":")),
                "--strict-mcp-config",
            ]
        return Invocation([*argv, *extra], "stdin", "claude-json")

    if backend == "copilot":
        from .copilot_backend import build_copilot_command

        # The process already starts in ``workdir``; `-C` is not in every
        # Copilot CLI release (the OpenShell base image's rejects it).
        configured = {**runtime}
        configured.pop("working_directory", None)
        if sandboxed:
            configured["copilot_bin"] = "copilot"
        if model:
            configured["model"] = model
        argv = build_copilot_command(configured, mcps=mcps)
        if sandboxed:
            from .runners import _copilot_sandbox_command

            argv = _copilot_sandbox_command(argv, resume=False)
        else:
            # Tag the session as Ghostlab's so retro tooling can tell it apart.
            argv[-1:-1] = ["--session-id", new_copilot_session_id()]
        env = {str(k): str(v) for k, v in dict(runtime.get("env") or {}).items()}
        return Invocation(argv, "append-arg", "copilot-json", env)

    if backend == "opencode":
        from .opencode_backend import DEFAULT_OPENCODE_MODEL
        from .opencode_config import build_project_config

        config = build_project_config(
            runtime, mcps, path_map=inside,
            skills=list((agent.get("inputs") or {}).get("skills") or []),
        )
        argv = [
            binary("opencode_bin", "opencode"), "run", "--format", "json",
            "--log-level", "ERROR", "--model", model or DEFAULT_OPENCODE_MODEL,
        ]
        if runtime.get("default_agent"):
            argv += ["--agent", str(runtime["default_agent"])]
        argv += ["--dir", workdir, *extra]
        return Invocation(
            argv, "stdin", "opencode-json",
            files={"opencode.json": json.dumps(config, indent=2) + "\n"},
        )

    runner = dict(agent.get("runner") or {})
    env = {str(k): str(v) for k, v in dict(runner.get("env") or {}).items()}
    mode = str(runner.get("prompt_mode") or "stdin")
    if mode not in ("stdin", "append-arg"):
        raise ExecError(f"exec supports runner prompt_mode stdin or append-arg, not {mode!r}")
    return Invocation(
        [str(part) for part in runner.get("command") or []], mode,
        str(runner.get("parser") or "text"), env,
    )


def agent_runner_config(
    agent: dict[str, Any],
    backend: str,
    *,
    sandbox: dict[str, Any],
    workdir: str,
    model: str = "",
    timeout_seconds: int = 0,
) -> "RunnerConfig":
    """Resolve an agent definition into a runner that owns its own sandbox.

    The evaluation pipeline and ``artifact-run`` drive agents through
    :class:`RunnerConfig`, where the runner creates the OpenShell sandbox and
    runs each turn with ``openshell sandbox exec``. This is the same
    translation ``ghostlab exec`` uses, adapted to that shape: stdio MCP
    programs (and OpenCode's instruction and skill files) are added to the
    sandbox uploads and every path is rewritten to its in-sandbox location;
    OpenCode's project config travels inline as ``OPENCODE_CONFIG_CONTENT``.

    Skills and instruction files for non-OpenCode harnesses travel in the
    prompt — see :func:`compose_prompt` — so callers that build their own
    prompt must apply it themselves.
    """
    from .agent_sandbox import remote_path_for
    from .config import RunnerConfig
    from .opencode_config import runtime_input_paths
    from .sandbox import MCP_UPLOAD_ROOT, auto_uploads_for_command

    sandbox = dict(sandbox or {})
    sandboxed = str(sandbox.get("backend") or "openshell") == "openshell"
    path_map: PathMap | None = None
    if sandboxed:
        uploads = [dict(item) for item in sandbox.get("uploads") or []]
        for mcp in (agent.get("inputs") or {}).get("mcps") or []:
            if str(mcp.get("transport")) != "stdio":
                continue
            connection = dict(mcp.get("connection") or {})
            raw = connection.get("command") or []
            parts = ([raw] if isinstance(raw, str) else list(raw)) + list(
                connection.get("args") or []
            )
            uploads += auto_uploads_for_command(parts, uploads, root=MCP_UPLOAD_ROOT)
        if backend == "opencode":
            skills = list((agent.get("inputs") or {}).get("skills") or [])
            for item in runtime_input_paths(agent.get("runtime"), skills):
                path = Path(str(item)).expanduser()
                if path.exists():
                    source = path if path.is_dir() else path.parent
                    entry = {"source": str(source.resolve()), "target": "/sandbox/agent"}
                    if entry not in uploads:
                        uploads.append(entry)
        sandbox["uploads"] = uploads

        def path_map(value: Any) -> str:
            return remote_path_for(uploads, value)

    invocation = build_invocation(
        agent, backend, workdir=workdir, model=model,
        sandboxed=sandboxed, path_map=path_map,
    )
    env = dict(invocation.env)
    if "opencode.json" in invocation.files:
        env["OPENCODE_CONFIG_CONTENT"] = invocation.files["opencode.json"]
        if sandboxed:
            sandbox["env_allowlist"] = list(dict.fromkeys([
                *list(sandbox.get("env_allowlist") or []), "OPENCODE_CONFIG_CONTENT",
            ]))
    runtime = dict(agent.get("runtime") or {})
    return RunnerConfig(
        kind="process",
        command=list(invocation.argv),
        env=env,
        timeout_seconds=int(timeout_seconds or runtime.get("timeout_seconds") or 600),
        prompt_mode=invocation.prompt_mode,
        parser=invocation.parser,
        sandbox=sandbox,
    )


def parse_answer(parser: str, stdout: str, servers: "list[str] | None" = None) -> dict[str, Any]:
    """The agent's final message plus whatever tool activity its stream records."""
    from .tool_capture import (
        parse_claude_output,
        parse_codex_output,
        parse_copilot_output,
        parse_opencode_output,
    )

    if parser == "codex-json":
        parsed = parse_codex_output(stdout)
    elif parser == "claude-json":
        parsed = parse_claude_output(stdout)
    elif parser == "copilot-json":
        parsed = parse_copilot_output(stdout)
    elif parser.startswith("opencode"):
        parsed = parse_opencode_output(stdout, servers=servers)
    else:
        return {"message": stdout.strip(), "tool_calls": [], "builtin_calls": [], "errors": []}
    return {
        "message": str(parsed.get("message") or ""),
        "tool_calls": list(parsed.get("tool_calls") or []),
        "builtin_calls": list(parsed.get("builtin_calls") or []),
        "errors": list(parsed.get("errors") or []),
    }


def _run(argv: list[str], *, stdin: str | None, env: dict[str, str], cwd: str | None,
         timeout: int) -> RunnerResult:
    full_env = {**os.environ, **{k: os.path.expandvars(v) for k, v in env.items()}}
    try:
        done = subprocess.run(
            argv, input=stdin, text=True, capture_output=True, env=full_env,
            cwd=cwd, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        def text(value: Any) -> str:
            return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")

        return RunnerResult(text(exc.stdout), 124, timed_out=True, stderr=text(exc.stderr))
    except FileNotFoundError as exc:
        return RunnerResult("", 127, stderr=f"harness binary not found: {exc}")
    return RunnerResult(done.stdout, done.returncode, stderr=done.stderr)


def _with_prompt(invocation: Invocation, prompt: str) -> tuple[list[str], str | None]:
    if invocation.prompt_mode == "append-arg":
        return [*invocation.argv, prompt], None
    return list(invocation.argv), prompt


def _write(run_dir: Path, name: str, text: str) -> None:
    (run_dir / name).write_text(text, encoding="utf-8")


def _sandbox_settings(agent_sandbox: dict[str, Any], config: ExecConfig,
                      workspace: Path | None) -> dict[str, Any]:
    sandbox = dict(agent_sandbox or {})
    if config.image:
        sandbox["image"] = config.image
    if config.providers:
        sandbox["providers"] = list(dict.fromkeys([
            *list(sandbox.get("providers") or []), *config.providers,
        ]))
    if config.keep_sandbox:
        sandbox["keep"] = True
    # The loader stages the agent's own workspace; an explicit --workspace
    # replaces it rather than uploading both.
    uploads = [
        dict(item) for item in sandbox.get("uploads") or []
        if str(item.get("target")) != SANDBOX_WORKSPACE_ROOT
    ]
    if workspace is not None:
        uploads.append({"source": str(workspace), "target": SANDBOX_WORKSPACE_ROOT})
    sandbox["uploads"] = uploads
    sandbox.pop("workdir", None)
    return sandbox


def execute(config: ExecConfig) -> ExecResult:
    """Run the agent once on ``config.prompt`` and record what it produced."""
    try:
        agent, agent_sandbox = load_agent_definition(config.agent_path)
    except ConfigError as exc:
        raise ExecError(str(exc)) from exc
    backend = resolve_backend(agent, config.backend)
    workspace = config.workspace or (Path(agent["workspace"]) if agent.get("workspace") else None)
    if workspace is not None:
        workspace = Path(workspace).expanduser().resolve()
        if not workspace.is_dir():
            raise ExecError(f"workspace directory not found: {workspace}")
    if not config.prompt.strip():
        raise ExecError("the task is empty: pass it as an argument, --prompt-file, or stdin")
    for remote, local_name in config.exports:
        local = Path(local_name)
        if local.is_absolute() or ".." in local.parts or not local.parts:
            raise ExecError(f"--export name must be a relative path inside the run dir: {local_name!r}")
        if not remote.startswith("/sandbox"):
            raise ExecError(f"--export source must be a sandbox path under /sandbox: {remote!r}")
    boundary = config.sandbox or str(agent_sandbox.get("backend") or "openshell")
    if boundary not in ("openshell", "local"):
        raise ExecError(f"unsupported sandbox {boundary!r}; use openshell or local")
    timeout = int(
        config.timeout_seconds
        or (agent.get("runtime") or {}).get("timeout_seconds")
        or (agent.get("runner") or {}).get("timeout_seconds")
        or 900
    )
    prompt = compose_prompt(agent, config.prompt, backend)

    run_dir = config.run_dir.expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    _write(run_dir, "prompt.txt", prompt + "\n")
    started = utc_now()
    clock = time.monotonic()
    manifest: dict[str, Any] = {
        "schema_version": EXEC_SCHEMA_VERSION,
        "agent_id": agent.get("id"),
        "agent_path": str(config.agent_path.expanduser().resolve()),
        "backend": backend,
        "model": config.model or str((agent.get("runtime") or {}).get("model") or ""),
        "sandbox": boundary,
        "workspace": str(workspace) if workspace else None,
        "mcps": [str(m.get("id")) for m in (agent.get("inputs") or {}).get("mcps") or []],
        "skills": [path.parent.name for path in _skill_files(agent)],
        "started_at": started,
        "exports": [],
    }

    if boundary == "local":
        result, error, parser = _execute_local(
            agent, backend, config, workspace, run_dir, prompt, timeout
        )
    else:
        result, error, parser = _execute_openshell(
            agent, agent_sandbox, backend, config, workspace, run_dir, prompt, timeout, manifest
        )
    parsed = parse_answer(parser, result.output, servers=manifest["mcps"])
    if parsed["errors"] and result.exit_code == 0:
        # Several harnesses exit 0 on a provider failure and only say so in-stream.
        result = RunnerResult(
            result.output, 1, result.timed_out,
            (f"{backend} error: {'; '.join(parsed['errors'])[:500]}\n{result.stderr}").strip(),
        )
    if error:
        status = STATUS_SANDBOX_ERROR if error.startswith("sandbox_") else STATUS_HARNESS_ERROR
    else:
        status = _classify(result)
    answer = parsed["message"] if parser != "text" else result.output.strip()

    _write(run_dir, "stdout.txt", result.output)
    _write(run_dir, "stderr.txt", result.stderr)
    _write(run_dir, "answer.md", answer + ("\n" if answer else ""))
    from .tool_capture import summarize_tool_calls

    manifest.update({
        "status": status,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "error": _error_summary(status, error, parsed["errors"], result.stderr),
        "finished_at": utc_now(),
        "duration_seconds": round(time.monotonic() - clock, 2),
        "tool_calls": parsed["tool_calls"],
        "builtin_calls": parsed["builtin_calls"],
        "tool_summary": summarize_tool_calls(
            [{"server": c.get("server", "?"), "tool": c.get("tool", "?"),
              "status": c.get("status", "completed")} for c in parsed["tool_calls"]]
        ),
    })
    if status == STATUS_COMPLETED and not answer:
        manifest["warnings"] = ["the agent finished without a final message"]
    _write(run_dir, "exec.json", json.dumps(manifest, indent=2, default=str) + "\n")
    return ExecResult(status, answer, manifest, run_dir)


def _error_summary(status: str, error: str, stream_errors: list[str], stderr: str) -> str:
    """One line saying why the run did not complete, preferring the harness's own words."""
    if status == STATUS_COMPLETED:
        return ""
    if error:
        return error[:500]
    if stream_errors:
        return "; ".join(stream_errors)[:500]
    lines = [line for line in stderr.strip().splitlines() if line.strip()]
    return lines[-1][:500] if lines else ""


def _execute_local(agent, backend, config, workspace, run_dir, prompt, timeout):
    workdir = workspace or (run_dir / "workdir")
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        invocation = build_invocation(agent, backend, workdir=str(workdir), model=config.model)
    except (ValueError, ConfigError) as exc:
        raise ExecError(str(exc)) from exc
    env = dict(invocation.env)
    for name, content in invocation.files.items():
        # Never drop harness config into the user's own workspace on the host.
        path = run_dir / name
        path.write_text(content, encoding="utf-8")
        if name == "opencode.json":
            env["OPENCODE_CONFIG"] = str(path)
    argv, stdin = _with_prompt(invocation, prompt)
    result = _run(argv, stdin=stdin, env=env, cwd=str(workdir), timeout=timeout)
    return result, "", invocation.parser


def _execute_openshell(agent, agent_sandbox, backend, config, workspace, run_dir, prompt,
                       timeout, manifest):
    from .agent_sandbox import AGENT_ENV, prepare_agent_sandbox
    from .opencode_config import runtime_input_paths

    runtime = dict(agent.get("runtime") or {})
    staged = {key: value for key, value in agent.items() if key != "workspace"}
    if config.model:
        staged["runtime"] = {**runtime, "model": config.model}
    sandbox_cfg = _sandbox_settings(agent_sandbox, config, workspace)
    extra = runtime_input_paths(
        runtime, list((agent.get("inputs") or {}).get("skills") or [])
    ) if backend == "opencode" else []
    try:
        handle = prepare_agent_sandbox(
            staged, sandbox_cfg, role="agent", base_dir=config.agent_path.parent,
            artifact_dir=run_dir, extra_paths=extra,
            egress_policy=backend == "opencode",
        )
    except SandboxError as exc:
        return RunnerResult("", 125, stderr=str(exc)), f"{exc.kind}: {exc.detail}", "text"
    manifest["sandbox_name"] = handle.sandbox.name
    parser = "text"
    try:
        if workspace is not None:
            workdir = f"{SANDBOX_WORKSPACE_ROOT}/{workspace.name}"
        elif backend == "opencode":
            workdir = SANDBOX_PROJECT_DIR
        else:
            workdir = SANDBOX_WORKSPACE_ROOT
        try:
            invocation = build_invocation(
                staged, backend, workdir=workdir, model=config.model,
                sandboxed=True, path_map=handle.remote_path,
            )
        except (ValueError, ConfigError) as exc:
            raise ExecError(str(exc)) from exc
        parser = invocation.parser
        for file_name, content in invocation.files.items():
            local = run_dir / file_name
            local.write_text(content, encoding="utf-8")
            handle.sandbox.upload_file(local, f"{workdir}/{file_name}")
        argv, stdin = _with_prompt(invocation, prompt)
        # OpenCode's data dirs live outside the workspace (see agent_sandbox);
        # other harnesses keep their own defaults so provider auth still resolves.
        wrapped = handle.sandbox.ssh_command(
            argv, env=invocation.env, workdir=workdir,
            base_env=dict(AGENT_ENV) if backend == "opencode" else {},
        )
        result = _run(wrapped, stdin=stdin, env={}, cwd=None, timeout=timeout)
        for remote, local_name in config.exports:
            destination = run_dir / local_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                handle.sandbox.download(remote, destination)
                manifest["exports"].append({"source": remote, "path": local_name})
            except SandboxError as exc:
                manifest["exports"].append({"source": remote, "error": exc.detail[:300]})
        return result, "", parser
    except SandboxError as exc:
        return RunnerResult("", 125, stderr=str(exc)), f"{exc.kind}: {exc.detail}", parser
    finally:
        handle.close()
