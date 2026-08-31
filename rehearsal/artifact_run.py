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
import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

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
from .sandbox import (
    WORKSPACE_ARTIFACT_ROOT,
    WORKSPACE_EXPORT_PYTHON,
    SandboxError,
    normalize_sandbox,
    verify_workspace_export_runtime,
)
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
_MAX_PRIOR_MANIFEST_BYTES = 16 * 1024 * 1024
_RUN_METADATA = (
    "artifact-run.json",
    "events.jsonl",
    "stdout.txt",
    "stderr.txt",
    "prompt.txt",
    "workspace-export",
)


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
    path = Path(path)
    if path.is_dir():
        entries: list[dict[str, Any]] = []
        for item in sorted(path.rglob("*")):
            relative = item.relative_to(path).as_posix()
            if item.is_symlink():
                entries.append(
                    {"path": relative, "kind": "symlink", "target": item.readlink().as_posix()}
                )
            elif item.is_file():
                entries.append(
                    {"path": relative, "kind": "file", "sha256": sha256_path(item)}
                )
        return sha256_bytes(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def path_bytes(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_dir():
        return sum(
            item.stat().st_size
            for item in path.rglob("*")
            if not item.is_symlink() and item.is_file()
        )
    return path.stat().st_size


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
    from .tool_capture import (
        parse_codex_output,
        parse_copilot_output,
        parse_opencode_output,
    )

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
    if config.sandbox_image:
        sandbox["image"] = config.sandbox_image
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


def _workspace_archive_candidates(requested: str) -> tuple[str, ...]:
    if not requested:
        return ()
    candidates = [requested]
    if requested.endswith(".tar.zst"):
        candidates.append(requested[: -len(".tar.zst")] + ".tar.gz")
    elif requested.endswith(".tgz"):
        candidates.append(requested[: -len(".tgz")] + ".tar.gz")
    elif not requested.endswith((".tar.gz", ".tar")):
        candidates.append(requested + ".tar.gz")
    return tuple(dict.fromkeys(candidates))


def _run_output_path(run_dir: Path, relative: str | Path) -> Path:
    relative_path = Path(relative)
    if (
        relative_path.is_absolute()
        or relative_path == Path(".")
        or ".." in relative_path.parts
        or not relative_path.parts
    ):
        raise ArtifactRunError(f"unsafe run output path: {relative_path}")
    return run_dir.joinpath(*relative_path.parts)


def _previous_manifest_outputs(run_dir: Path) -> tuple[str, ...]:
    path = _run_output_path(run_dir, "artifact-run.json")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ()
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactRunError(
            f"unsafe previous artifact-run.json: expected a regular file, found {path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ArtifactRunError(
                    "unsafe previous artifact-run.json: file changed while opening"
                )
            raw = handle.read(_MAX_PRIOR_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise ArtifactRunError(
            f"cannot safely read previous artifact-run.json: {exc}"
        ) from exc
    if len(raw) > _MAX_PRIOR_MANIFEST_BYTES:
        raise ArtifactRunError(
            "previous artifact-run.json is too large to validate safely"
        )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactRunError(
            f"previous artifact-run.json is invalid: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise ArtifactRunError("previous artifact-run.json must contain an object")

    outputs: list[str] = []

    def add(value: Any, source: str) -> None:
        if not isinstance(value, str) or not value:
            raise ArtifactRunError(
                f"unsafe prior manifest path in {source}: expected a non-empty string"
            )
        try:
            _run_output_path(run_dir, value)
        except ArtifactRunError as exc:
            raise ArtifactRunError(
                f"unsafe prior manifest path in {source}: {value!r}"
            ) from exc
        outputs.append(value)

    def add_archive(value: Any, source: str) -> None:
        if not isinstance(value, str) or Path(value).name != value:
            raise ArtifactRunError(
                f"unsafe prior manifest archive name in {source}: {value!r}"
            )
        add(value, source)

    exports = document.get("exports", [])
    if not isinstance(exports, list):
        raise ArtifactRunError("previous artifact-run.json exports must be a list")
    for index, exported in enumerate(exports):
        if not isinstance(exported, dict) or "path" not in exported:
            raise ArtifactRunError(
                f"previous artifact-run.json exports[{index}] has no path"
            )
        add(exported["path"], f"exports[{index}].path")

    configured = document.get("configured_exports", [])
    if not isinstance(configured, list):
        raise ArtifactRunError(
            "previous artifact-run.json configured_exports must be a list"
        )
    for index, configured_path in enumerate(configured):
        add(configured_path, f"configured_exports[{index}]")

    archives = document.get("workspace_archive_candidates", [])
    if not isinstance(archives, list):
        raise ArtifactRunError(
            "previous artifact-run.json workspace_archive_candidates must be a list"
        )
    for index, archive in enumerate(archives):
        add_archive(archive, f"workspace_archive_candidates[{index}]")

    workspace_export = document.get("workspace_export")
    if workspace_export is not None:
        if not isinstance(workspace_export, dict):
            raise ArtifactRunError(
                "previous artifact-run.json workspace_export must be an object"
            )
        if "archive" in workspace_export:
            add_archive(workspace_export["archive"], "workspace_export.archive")
    return tuple(dict.fromkeys(outputs))


def _remove_run_output(run_dir: Path, relative: str | Path) -> None:
    """Remove an output without traversing a stale symlink below ``run_dir``."""
    relative_path = Path(relative)
    target = _run_output_path(run_dir, relative_path)
    current = run_dir
    for part in relative_path.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if current != target and stat.S_ISDIR(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(current)
        else:
            current.unlink()
        return


def _safe_run_output(
    run_dir: Path,
    relative: str | Path,
    *,
    create_parents: bool = False,
) -> Path:
    """Return a run output only when none of its components is a symlink."""
    relative_path = Path(relative)
    target = _run_output_path(run_dir, relative_path)
    current = run_dir
    for part in relative_path.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create_parents:
                raise ArtifactRunError(f"run output parent does not exist: {current}")
            current.mkdir()
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ArtifactRunError(f"run output path has an unsafe component: {current}")
    try:
        info = target.lstat()
    except FileNotFoundError:
        return target
    if stat.S_ISLNK(info.st_mode):
        raise ArtifactRunError(f"run output path is a symlink: {target}")
    return target


def _write_text(run_dir: Path, relative: str | Path, text: str) -> None:
    path = _safe_run_output(run_dir, relative, create_parents=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def _copy_regular_run_output(
    run_dir: Path,
    source_relative: str | Path,
    destination_relative: str | Path,
) -> Path:
    source = _safe_run_output(run_dir, source_relative)
    destination = _safe_run_output(run_dir, destination_relative, create_parents=True)
    source_info = source.lstat()
    if not stat.S_ISREG(source_info.st_mode):
        raise ArtifactRunError(f"workspace archive is not a regular file: {source}")
    _remove_run_output(run_dir, destination_relative)
    destination = _safe_run_output(run_dir, destination_relative, create_parents=True)
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    destination_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        source_descriptor = os.open(source, source_flags)
        try:
            destination_descriptor = os.open(destination, destination_flags, 0o600)
        except Exception:
            os.close(source_descriptor)
            raise
        with (
            os.fdopen(source_descriptor, "rb") as source_handle,
            os.fdopen(destination_descriptor, "wb") as destination_handle,
        ):
            shutil.copyfileobj(source_handle, destination_handle)
    except Exception:
        _remove_run_output(run_dir, destination_relative)
        raise
    return destination


def _cleanup_run_outputs(config: ArtifactRunConfig, run_dir: Path) -> None:
    previous = _previous_manifest_outputs(run_dir)
    names = [
        *(name for name in _RUN_METADATA if name != "artifact-run.json"),
        *(local for _remote, local in config.exports),
        *(local for _remote, local in config.optional_exports),
        *_workspace_archive_candidates(config.export_workspace),
        *previous,
        "artifact-run.json",
    ]
    unique = tuple(
        name for name in dict.fromkeys(names) if name != "artifact-run.json"
    ) + ("artifact-run.json",)
    for name in unique:
        _run_output_path(run_dir, name)
    for name in unique:
        _remove_run_output(run_dir, name)


def _collect_exports(
    runner: AgentRunner, config: ArtifactRunConfig, run_dir: Path
) -> list[dict[str, Any]]:
    """Download declared artifacts before the sandbox is torn down."""
    exports: list[dict[str, Any]] = []
    requested = [
        *((remote, local, True) for remote, local in config.exports),
        *((remote, local, False) for remote, local in config.optional_exports),
    ]
    for remote, local, required in requested:
        _remove_run_output(run_dir, local)
        destination = _safe_run_output(run_dir, local, create_parents=True)
        try:
            runner.export_artifact(remote, destination)
        except SandboxError as exc:
            detail = exc.detail.lower()
            if not required and (
                "no such file" in detail or "failed to resolve sandbox source path" in detail
            ):
                continue
            raise
        destination = _safe_run_output(run_dir, local)
        if not destination.exists():
            if required:
                raise SandboxError(
                    "sandbox_download_failed", f"{remote} did not produce {destination}"
                )
            continue
        exports.append(
            {
                "path": local,
                "source": remote,
                "sha256": sha256_path(destination),
                "bytes": path_bytes(destination),
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
    try:
        path = _safe_run_output(run_dir, target)
    except ArtifactRunError as exc:
        return False, [str(exc)], None
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
    _write_text(run_dir, "stdout.txt", result.output)
    _write_text(run_dir, "stderr.txt", result.stderr)

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
            workspace_export_dir = _safe_run_output(
                run_dir, "workspace-export", create_parents=True
            )
            summary = runner.export_workspace(
                destination=workspace_export_dir,
                workdir=str(runner_config.sandbox.get("workdir") or "/sandbox"),
                excludes=excludes,
                retain=retain,
                archive_name=config.export_workspace,
                timeout=max(runner_config.timeout_seconds, 300),
            )
            manifest["workspace_output_sha256"] = str(summary.get("state_sha256") or "")
            archive_name = str(summary["archive"])
            candidates = _workspace_archive_candidates(config.export_workspace)
            if archive_name not in candidates or Path(archive_name).name != archive_name:
                raise SandboxError(
                    "sandbox_export_failed",
                    f"workspace exporter returned unexpected archive name {archive_name!r}",
                )
            archive = Path(str(summary["archive_path"]))
            expected_archive = workspace_export_dir / archive_name
            if archive.absolute() != expected_archive.absolute():
                raise SandboxError(
                    "sandbox_export_failed",
                    f"workspace exporter returned archive outside run directory: {archive}",
                )
            final = _copy_regular_run_output(
                run_dir,
                Path("workspace-export") / archive_name,
                archive_name,
            )
            if archive_name != config.export_workspace:
                manifest["warnings"].append(
                    f"workspace archive written as {archive_name}: "
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
        else:
            from .sandbox import fingerprint_workspace

            handle = runner.sandbox_handle
            if handle is None:
                raise SandboxError(
                    "sandbox_export_failed",
                    "artifact-run runner exposes no live sandbox for workspace fingerprinting",
                )
            summary = fingerprint_workspace(
                handle,
                workdir=str(runner_config.sandbox.get("workdir") or "/sandbox"),
                excludes=excludes,
                retain=retain,
                timeout=max(runner_config.timeout_seconds, 300),
            )
            manifest["workspace_output_sha256"] = str(summary.get("state_sha256") or "")
        manifest["exports"] += _collect_exports(runner, config, run_dir)
    except (ArtifactRunError, SandboxError, OSError, KeyError, ValueError) as exc:
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
    _cleanup_run_outputs(config, run_dir)

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
        "setup_commands": [list(command) for command in config.setup_commands],
        "setup_results": [],
        "workspace_export_python": WORKSPACE_EXPORT_PYTHON,
        "started_at": utc_now(),
        "finished_at": "",
        "exit_code": None,
        "timed_out": False,
        "duration_ms": 0,
        "exports": [],
        "configured_exports": list(
            dict.fromkeys(
                local
                for _remote, local in (*config.exports, *config.optional_exports)
            )
        ),
        "workspace_archive_candidates": list(
            _workspace_archive_candidates(config.export_workspace)
        ),
        "events_path": "events.jsonl",
        "warnings": [],
    }
    manifest_path = run_dir / "artifact-run.json"

    def publish(status: str) -> ArtifactRunResult:
        manifest["status"] = status
        manifest["finished_at"] = manifest["finished_at"] or utc_now()
        _write_text(
            run_dir,
            "artifact-run.json",
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        )
        return ArtifactRunResult(
            status=status, manifest=manifest, manifest_path=manifest_path, run_dir=run_dir
        )

    _write_text(run_dir, "prompt.txt", config.prompt)
    events = JsonlLogger(_safe_run_output(run_dir, "events.jsonl", create_parents=True))
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

    handle = runner.sandbox_handle

    def close_after_setup_failure() -> None:
        try:
            runner.close()
        except SandboxError as exc:
            manifest["warnings"].append(
                f"sandbox close failed after setup error: {exc.kind}: {exc.detail}"
            )

    if handle is None:
        manifest["error"] = "artifact-run runner exposes no sandbox for workspace export"
        manifest["exit_code"] = SANDBOX_EXIT_CODE
        events.write(Event.create("artifact_run.failed", error=manifest["error"]))
        close_after_setup_failure()
        return publish(STATUS_HARNESS_ERROR)

    def check_exporter_runtime() -> ArtifactRunResult | None:
        try:
            verify_workspace_export_runtime(
                handle,
                python=WORKSPACE_EXPORT_PYTHON,
                timeout=30,
            )
        except SandboxError as exc:
            manifest["error"] = (
                f"workspace exporter runtime trust check failed: {exc.kind}: {exc.detail}"
            )
            if exc.kind != "sandbox_runtime_untrusted":
                manifest["exit_code"] = SANDBOX_EXIT_CODE
            events.write(Event.create("artifact_run.failed", error=manifest["error"]))
            close_after_setup_failure()
            status = (
                STATUS_HARNESS_ERROR
                if exc.kind == "sandbox_runtime_untrusted"
                else STATUS_SANDBOX_ERROR
            )
            return publish(status)
        return None

    if config.setup_commands:
        runtime_failure = check_exporter_runtime()
        if runtime_failure is not None:
            return runtime_failure

    for command in config.setup_commands:
        setup_started = time.monotonic()
        try:
            setup_result = handle.exec(
                list(command),
                input_text=None,
                env=runner_config.env,
                timeout=runner_config.timeout_seconds,
            )
        except SandboxError as exc:
            manifest["error"] = f"setup failed: {exc.kind}: {exc.detail}"
            events.write(Event.create("artifact_run.failed", error=manifest["error"]))
            close_after_setup_failure()
            return publish(STATUS_SANDBOX_ERROR)
        setup_record = {
            "argv": list(command),
            "exit_code": setup_result.returncode,
            "duration_ms": int((time.monotonic() - setup_started) * 1000),
        }
        manifest["setup_results"].append(setup_record)
        events.write(Event.create("artifact_run.setup", **setup_record))
        if setup_result.returncode != 0:
            detail = (setup_result.stderr or setup_result.stdout or "").strip()[-2000:]
            manifest["error"] = (
                f"setup command exited {setup_result.returncode}: {detail or list(command)!r}"
            )
            close_after_setup_failure()
            return publish(STATUS_HARNESS_ERROR)

    runtime_failure = check_exporter_runtime()
    if runtime_failure is not None:
        return runtime_failure

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
