"""One configured agent, one prompt, one mutable workspace, declared exports.

`ghostlab run` evaluates a conversation: two agents, many turns, graded on what
was said. A benchmark task is the opposite shape — a single message, a
repository the agent may rewrite, and a verdict that depends only on the state
it left behind. Overloading `ScenarioConfig` for that would mean carrying a user
emulator, turn limits, and message assertions that have no meaning here.

So this module generalizes the existing `RunnerConfig` / `create_runner` /
OpenShell path instead: one runner, one turn, no emulator, and — the primitive
that was actually missing — an export taken *before* `runner.close()` deletes
the sandbox.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .config import (
    ARTIFACT_RUN_SCHEMA_VERSION,
    ArtifactRunConfig,
    ConfigError,
    RunnerConfig,
    load_json,
    runner_from_dict,
    schema_errors,
)
from .logging import JsonlLogger
from .runners import AgentRunner, RunnerResult, create_sandboxed_runner
from .sandbox import WORKSPACE_ARTIFACT_ROOT, SandboxError, normalize_sandbox
from .types import Event, utc_now

# Terminal states. Every one of them is distinguishable because a benchmark
# must never fold "the harness broke" into "the agent failed".
STATUS_COMPLETED = "completed"
STATUS_TIMED_OUT = "timed_out"
STATUS_MODEL_UNAVAILABLE = "model_unavailable"
STATUS_AGENT_ERROR = "agent_error"
STATUS_EXPORT_FAILED = "export_failed"
STATUS_OUTPUT_CONTRACT_FAILED = "output_contract_failed"
STATUS_SANDBOX_ERROR = "sandbox_error"
STATUS_HARNESS_ERROR = "harness_error"

STATUSES = (
    STATUS_COMPLETED,
    STATUS_TIMED_OUT,
    STATUS_MODEL_UNAVAILABLE,
    STATUS_AGENT_ERROR,
    STATUS_EXPORT_FAILED,
    STATUS_OUTPUT_CONTRACT_FAILED,
    STATUS_SANDBOX_ERROR,
    STATUS_HARNESS_ERROR,
)

# Provider-side failures an agent CLI reports on stderr after the runners have
# already normalized their own JSONL error frames into a non-zero exit. Matched
# against stderr only: the agent's own transcript legitimately contains words
# like "quota" and "503" when it is working on code that mentions them.
_MODEL_OUTAGE_SIGNALS = (
    "opencode error", "copilot error", "model not found", "unauthorized",
    "quota", "rate limit", "usage limit", "authentication failed", "no auth",
    "provider error", "overloaded", "service unavailable", "invalid api key",
)

# OpenShell-backed runners report a classified sandbox failure as exit 125 with
# the SandboxError kind in stderr, rather than raising.
SANDBOX_EXIT_CODE = 125

WORKSPACE_UPLOAD_TARGET = "/sandbox"


class ArtifactRunError(RuntimeError):
    """A failure that prevents the run from starting at all."""


@dataclass(frozen=True)
class ArtifactRunResult:
    status: str
    manifest: dict[str, Any]
    manifest_path: Path
    run_dir: Path

    @property
    def ok(self) -> bool:
        return self.status == STATUS_COMPLETED


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _classify(result: RunnerResult) -> str:
    """Separate "the harness broke" from "the model was unavailable" from "the agent failed"."""
    if result.timed_out:
        return STATUS_TIMED_OUT
    if result.exit_code == 0:
        return STATUS_COMPLETED
    stderr = result.stderr.lower()
    if result.exit_code == SANDBOX_EXIT_CODE or "sandbox_" in stderr:
        return STATUS_SANDBOX_ERROR
    if any(signal in stderr for signal in _MODEL_OUTAGE_SIGNALS):
        return STATUS_MODEL_UNAVAILABLE
    return STATUS_AGENT_ERROR


def _model_of(agent: dict[str, Any], runner: RunnerConfig) -> str:
    command = list(runner.command or [])
    for index, part in enumerate(command[:-1]):
        if part in ("-m", "--model"):
            return str(command[index + 1])
    return str((agent.get("runtime") or {}).get("model") or "")


def _parse_calls(result: RunnerResult, parser: str) -> list[dict[str, Any]]:
    from .tool_capture import parse_codex_output, parse_copilot_output, parse_opencode_output

    try:
        if parser == "codex-json":
            return list(parse_codex_output(result.output).get("tool_calls") or [])
        if parser.startswith("opencode"):
            parsed = parse_opencode_output(result.output)
            return list(parsed.get("tool_calls") or []) + list(parsed.get("builtin_calls") or [])
        if parser == "copilot-json":
            parsed = parse_copilot_output(result.output)
            return list(parsed.get("tool_calls") or []) + list(parsed.get("builtin_calls") or [])
    except (ValueError, TypeError, KeyError):
        return []
    return []


def build_runner_config(
    agent: dict[str, Any],
    sandbox_overrides: dict[str, Any],
    config: ArtifactRunConfig,
) -> tuple[RunnerConfig, Path, str]:
    """Resolve the single agent runner and the workspace it may rewrite.

    Returns ``(runner config, host workspace, sandbox workdir)``.
    """
    runner_data = dict(agent.get("runner") or {})
    if not runner_data.get("command"):
        raise ArtifactRunError(
            f"Agent config {config.agent_path} declares no runner command. "
            "artifact-run drives the agent's own runner; add a `runner` block "
            "with the command that starts it."
        )
    runner = runner_from_dict(runner_data, source=str(config.agent_path))

    workspace = config.workspace or (Path(str(agent["workspace"])) if agent.get("workspace") else None)
    if workspace is None:
        raise ArtifactRunError(
            "artifact-run needs a mutable workspace: pass --workspace or give the "
            "agent config a `workspace` key."
        )
    workspace = Path(workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise ArtifactRunError(f"Workspace directory not found: {workspace}")

    sandbox = dict(sandbox_overrides or {})
    agent_workspace = str(Path(str(agent["workspace"])).resolve()) if agent.get("workspace") else ""
    uploads = [
        dict(item)
        for item in (sandbox.get("uploads") or [])
        if str(item.get("source")) != agent_workspace
    ]
    uploads.append({"source": str(workspace), "target": WORKSPACE_UPLOAD_TARGET})
    sandbox["uploads"] = uploads
    workdir = f"{WORKSPACE_UPLOAD_TARGET}/{workspace.name}"
    sandbox["workdir"] = workdir
    sandbox.setdefault("backend", "openshell")
    if str(sandbox.get("backend")) != "openshell":
        raise ArtifactRunError(
            "artifact-run executes the agent inside OpenShell so its edits stay in "
            f"a throwaway copy; sandbox.backend is {sandbox.get('backend')!r}."
        )
    sandbox["keep"] = bool(config.keep_sandbox or sandbox.get("keep"))
    normalized = normalize_sandbox(sandbox, config.agent_path.parent)

    runner = replace(
        runner,
        sandbox=normalized,
        timeout_seconds=int(config.timeout_seconds or runner.timeout_seconds),
    )
    return runner, workspace, workdir


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _collect_exports(
    runner: AgentRunner, config: ArtifactRunConfig, run_dir: Path
) -> list[dict[str, Any]]:
    """Download declared artifacts before the sandbox is torn down."""
    exports: list[dict[str, Any]] = []
    for remote, local in config.exports:
        destination = run_dir / local
        runner.export_artifact(remote, destination)
        if not destination.exists():
            raise SandboxError("sandbox_download_failed", f"{remote} did not produce {destination}")
        exports.append(
            {
                "path": local,
                "source": remote,
                "sha256": sha256_path(destination),
                "bytes": destination.stat().st_size,
            }
        )
    return exports


def _validate_contract(
    config: ArtifactRunConfig, run_dir: Path
) -> tuple[bool, list[str], dict[str, Any] | None]:
    if config.output_contract is None:
        return True, [], None
    target = config.contract_export()
    if not target:
        return False, ["--output-contract has no --export to validate"], None
    path = run_dir / target
    if not path.is_file():
        return False, [f"declared output {target} was not exported"], None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, [f"{target} is not valid JSON: {exc}"], None
    try:
        schema = load_json(config.output_contract)
        errors = schema_errors(document, schema)
    except ConfigError as exc:
        return False, [f"output contract is unusable: {exc}"], None
    return (not errors), errors, document


def _finish_run(
    config: ArtifactRunConfig,
    run_dir: Path,
    runner: AgentRunner,
    runner_config: RunnerConfig,
    result: RunnerResult,
    status: str,
    manifest: dict[str, Any],
    events: JsonlLogger,
    excludes: list[str],
    retain: list[str],
) -> str:
    """Record the turn, export before teardown, and check the output contract."""
    _write_text(run_dir / "stdout.txt", result.output)
    _write_text(run_dir / "stderr.txt", result.stderr)

    tool_calls = _parse_calls(result, runner_config.parser)
    for call in tool_calls:
        events.write(Event.create("agent.tool_call", **call))
    events.write(
        Event.create(
            "agent.reply",
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            chars=len(result.output),
            stderr=result.stderr[-2000:],
        )
    )
    manifest["tool_calls"] = len(tool_calls)

    # Export before close(): the sandbox — and everything the agent produced in
    # it — stops existing the moment the runner is closed.
    export_error = ""
    try:
        if config.export_workspace:
            summary = runner.export_workspace(
                destination=run_dir / "workspace-export",
                workdir=str(runner_config.sandbox.get("workdir") or "/sandbox"),
                excludes=excludes,
                retain=retain,
                archive_name=config.export_workspace,
                timeout=max(runner_config.timeout_seconds, 300),
            )
            manifest["workspace_output_sha256"] = str(summary.get("state_sha256") or "")
            archive = Path(str(summary["archive_path"]))
            final = run_dir / archive.name
            if archive.resolve() != final.resolve():
                shutil.copy2(archive, final)
            if archive.name != config.export_workspace:
                manifest["warnings"].append(
                    f"workspace archive written as {archive.name}: "
                    f"{config.export_workspace} was requested but the sandbox has no zstd"
                )
            manifest["workspace_export"] = {
                "archive": final.name,
                "state_sha256": manifest["workspace_output_sha256"],
                "file_count": summary.get("file_count"),
                "total_bytes": summary.get("total_bytes"),
                "git": summary.get("git", False),
                "status": "workspace-export/status.json",
                "diff": "workspace-export/diff.patch",
                "untracked": "workspace-export/untracked.json",
            }
            manifest["exports"].append(
                {
                    "path": final.name,
                    "source": WORKSPACE_ARTIFACT_ROOT,
                    "sha256": sha256_path(final),
                    "bytes": final.stat().st_size,
                }
            )
        manifest["exports"] += _collect_exports(runner, config, run_dir)
    except (SandboxError, OSError, KeyError, ValueError) as exc:
        export_error = f"{type(exc).__name__}: {exc}"
        manifest["export_error"] = export_error
        events.write(Event.create("artifact_run.export_failed", error=export_error))

    if status == STATUS_COMPLETED and export_error:
        return STATUS_EXPORT_FAILED
    if status != STATUS_COMPLETED:
        return status

    valid, errors, _document = _validate_contract(config, run_dir)
    if not valid:
        manifest["contract_errors"] = errors
        events.write(Event.create("artifact_run.contract_failed", errors=errors[:20]))
        return STATUS_OUTPUT_CONTRACT_FAILED
    if config.output_contract is not None:
        manifest["output_contract"] = str(config.output_contract)
        manifest["output_contract_sha256"] = sha256_path(config.output_contract)
    return STATUS_COMPLETED


def run_artifact(
    config: ArtifactRunConfig,
    *,
    runner_factory: Callable[[RunnerConfig, str], AgentRunner] = create_sandboxed_runner,
) -> ArtifactRunResult:
    """Execute one artifact run and always leave an ``artifact-run.json`` behind."""
    from .agents import load_agent_definition
    from .workspace_export import DEFAULT_EXCLUDES, workspace_state_hash

    run_dir = Path(config.run_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    agent, sandbox_overrides = load_agent_definition(config.agent_path)
    runner_config, workspace, _workdir = build_runner_config(agent, sandbox_overrides, config)

    # Project-specific exclusions extend the canonical set rather than replacing
    # it; `--workspace-retain` is how a task keeps a normally excluded artifact.
    excludes = sorted(set(DEFAULT_EXCLUDES) | set(config.workspace_excludes))
    retain = list(config.workspace_retain)
    manifest: dict[str, Any] = {
        "schema_version": ARTIFACT_RUN_SCHEMA_VERSION,
        "status": STATUS_SANDBOX_ERROR,
        "agent_id": str(agent.get("id") or ""),
        "agent_config": str(config.agent_path),
        "agent_config_sha256": sha256_path(config.agent_path),
        "workspace": str(workspace),
        "workspace_input_sha256": workspace_state_hash(workspace, excludes, retain),
        "workspace_output_sha256": "",
        "prompt_source": config.prompt_source,
        "prompt_sha256": sha256_bytes(config.prompt.encode("utf-8")),
        "runner": {
            "kind": runner_config.kind,
            "command": list(runner_config.command),
            "prompt_mode": runner_config.prompt_mode,
            "parser": runner_config.parser,
            "timeout_seconds": runner_config.timeout_seconds,
            "sandbox": {
                key: value
                for key, value in runner_config.sandbox.items()
                if key in ("backend", "image", "workdir", "network", "policy", "providers",
                           "cpu", "memory", "env_allowlist", "keep")
            },
            "user_emulator": False,
        },
        "model": _model_of(agent, runner_config),
        "started_at": utc_now(),
        "finished_at": "",
        "exit_code": None,
        "timed_out": False,
        "duration_ms": 0,
        "exports": [],
        "events_path": "events.jsonl",
        "warnings": [],
    }
    manifest_path = run_dir / "artifact-run.json"

    def publish(status: str) -> ArtifactRunResult:
        manifest["status"] = status
        manifest["finished_at"] = manifest["finished_at"] or utc_now()
        _write_text(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        return ArtifactRunResult(
            status=status, manifest=manifest, manifest_path=manifest_path, run_dir=run_dir
        )

    _write_text(run_dir / "prompt.txt", config.prompt)
    events = JsonlLogger(run_dir / "events.jsonl")
    events.write(
        Event.create(
            "artifact_run.started",
            agent=manifest["agent_id"],
            workspace=str(workspace),
            workspace_input_sha256=manifest["workspace_input_sha256"],
            prompt_sha256=manifest["prompt_sha256"],
        )
    )

    # Exactly one runner is created, and it is the agent's. There is no user
    # emulator in this path, by construction rather than by configuration.
    # `create_sandboxed_runner`, not `create_runner`: this path needs the runner
    # to own the sandbox so state can be exported before teardown, and asks for
    # that explicitly instead of changing what every other flow gets.
    try:
        runner = runner_factory(runner_config, "aut")
    except (ValueError, SandboxError) as exc:
        manifest["error"] = str(exc)
        events.write(Event.create("artifact_run.failed", error=str(exc)))
        return publish(STATUS_SANDBOX_ERROR)

    started = time.monotonic()
    events.write(Event.create("agent.prompt", chars=len(config.prompt)))
    try:
        result = runner.run_turn(config.prompt)
    except SandboxError as exc:
        manifest["error"] = f"{exc.kind}: {exc.detail}"
        events.write(Event.create("artifact_run.failed", error=manifest["error"]))
        try:
            runner.close()
        except SandboxError:
            pass
        return publish(STATUS_SANDBOX_ERROR)

    manifest["duration_ms"] = int((time.monotonic() - started) * 1000)
    manifest["exit_code"] = result.exit_code
    manifest["timed_out"] = bool(result.timed_out)
    status = _classify(result)
    try:
        status = _finish_run(
            config, run_dir, runner, runner_config, result, status, manifest, events, excludes, retain
        )
    except Exception as exc:  # noqa: BLE001 — the manifest is the only record of the attempt
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        events.write(Event.create("artifact_run.failed", error=manifest["error"]))
        status = STATUS_HARNESS_ERROR
    finally:
        try:
            runner.close()
        except SandboxError as exc:
            manifest["warnings"].append(f"sandbox close failed: {exc.kind}: {exc.detail}")

    events.write(Event.create("artifact_run.finished", status=status))
    return publish(status)
