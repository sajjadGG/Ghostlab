"""Scorer packages: isolated deterministic execution, residual judge, composition.

A scorer decides whether an agent's repository edit was correct, so the threat
model is not "did the code run" but "could the number have been influenced by
anything other than the candidate's behavior". Three separations do that work:

* the scorer never runs in the sandbox that produced the candidate;
* the deterministic phase executes candidate code with no network and no
  provider credentials, so a hostile implementation has nothing to exfiltrate;
* the judge phase has provider credentials but cannot execute anything, so it
  cannot be turned into the exfiltration path either.

Composition, hashing, and validation happen on the host, outside both sandboxes,
because a scorer that computes its own total is a scorer that can lie about it.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
import shutil
import stat
import subprocess
import tarfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any

from .config import (
    JUDGE_PERMISSION_FLOOR,
    ConfigError,
    ScorerConfig,
    load_json,
    load_scorer,
    schema_errors,
)
from .sandbox import OpenShellSandbox, SandboxError, normalize_sandbox

SCORE_INPUT_VERSION = "retro-score-input-v1"
SCORE_REPORT_VERSION = "retro-score-report-v1"
SCORER_ISOLATION_VERSION = "ghostlab-scorer-isolation-v1"
BENCHMARK_TASK_VERSION = "retro-benchmark-task-v1"

# The scorer contract fixes these paths: a scorer package written against one
# Ghostlab release must keep working against the next.
CANDIDATE_ROOT = "/candidate"
CANDIDATE_REPO = "/candidate/repo"
SCORER_ROOT = "/scorer"
FIXTURES_ROOT = "/fixtures"
INPUT_ROOT = "/input"
OUTPUT_ROOT = "/output"
MOUNT_NAMES = ("candidate", "scorer", "fixtures", "input", "output")
CONTRACT_ROOTS = {
    CANDIDATE_ROOT: "/sandbox/mount-candidate/candidate",
    SCORER_ROOT: "/sandbox/mount-scorer/scorer",
    FIXTURES_ROOT: "/sandbox/mount-fixtures/fixtures",
    INPUT_ROOT: "/sandbox/mount-input/input",
    OUTPUT_ROOT: "/sandbox/mount-output/output",
}
PHYSICAL_CANDIDATE_ROOT = CONTRACT_ROOTS[CANDIDATE_ROOT]
PHYSICAL_SCORER_ROOT = CONTRACT_ROOTS[SCORER_ROOT]
PHYSICAL_FIXTURES_ROOT = CONTRACT_ROOTS[FIXTURES_ROOT]
PHYSICAL_INPUT_ROOT = CONTRACT_ROOTS[INPUT_ROOT]
PHYSICAL_OUTPUT_ROOT = CONTRACT_ROOTS[OUTPUT_ROOT]
SCORER_WRAPPER = f"{PHYSICAL_INPUT_ROOT}/ghostlab-scorer-wrapper.py"
SECURE_EXEC_LAUNCHER = f"{INPUT_ROOT}/ghostlab-secure-exec"
PHYSICAL_SECURE_EXEC_LAUNCHER = (
    f"{PHYSICAL_INPUT_ROOT}/ghostlab-secure-exec"
)
SYSTEM_PYTHON = "/usr/bin/python3"

STATUS_SCORED = "scored"
STATUS_INVALID_CANDIDATE = "invalid_candidate_artifact"
STATUS_SCORER_ERROR = "scorer_error"
STATUS_SCORER_TIMEOUT = "scorer_timeout"
STATUS_JUDGE_UNAVAILABLE = "judge_unavailable"

REPORT_STATUSES = (
    STATUS_SCORED,
    STATUS_INVALID_CANDIDATE,
    STATUS_SCORER_ERROR,
    STATUS_SCORER_TIMEOUT,
    STATUS_JUDGE_UNAVAILABLE,
)

JUDGE_VERDICTS = ("MET", "UNMET", "CANNOT_ASSESS")
UNSCORED_WEIGHT_LIMIT = 0.20
MAX_CANDIDATE_ARCHIVE_MEMBERS = 100_000
MAX_CANDIDATE_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_CANDIDATE_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_CANDIDATE_METADATA_BYTES = 1024 * 1024
MAX_CANDIDATE_TOTAL_METADATA_BYTES = 16 * 1024 * 1024
_ARCHIVE_CHUNK = 1024 * 1024
_MAX_ZSTD_STDERR_BYTES = 64 * 1024
_MAX_ZSTD_TRAILING_BYTES = 1024 * 1024
_PAX_METADATA_TYPES = frozenset(
    (
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
    )
)
_TAR_METADATA_TYPES = _PAX_METADATA_TYPES | frozenset(
    (
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    )
)
_ISOLATION_EXEC = (
    "import json,os,pathlib,sys;"
    "pathlib.Path(sys.argv[1]).write_text("
    "json.dumps({'nonce':sys.argv[2],"
    "'secure_exec_available':os.path.isfile(sys.argv[3])"
    " and os.access(sys.argv[3],os.X_OK)})+'\\n');"
    f"os.execve('{SYSTEM_PYTHON}',"
    f"['{SYSTEM_PYTHON}','-I','-S',sys.argv[4],*sys.argv[5:]],os.environ)"
)

# §17 of the pipeline spec. Kept verbatim so the judge's framing does not drift
# with prompt edits elsewhere in the codebase.
RESIDUAL_JUDGE_PROMPT = """\
You are scoring one declared residual criterion for a repository task.
You are not deciding functional correctness; deterministic results are authoritative.
You have read-only access to the candidate repository, task prompt, rubric, and
deterministic ScoreReport. The candidate's model and identity are hidden.

Inspect only evidence relevant to criterion {criterion_id}. Do not reward effort,
verbosity, patch size, or resemblance to an imagined reference implementation.
Return MET, UNMET, or CANNOT_ASSESS with evidence paths and a calibrated value in
[0,1], using the supplied anchors. Do not edit files.
"""

SCORE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "schema_version",
        "task_id",
        "attempt_id",
        "repo_path",
        "task_path",
        "seed",
    ],
    "properties": {
        "schema_version": {"const": SCORE_INPUT_VERSION},
        "task_id": {"type": "string", "minLength": 1},
        "attempt_id": {"type": "string", "minLength": 1},
        "repo_path": {"type": "string", "minLength": 1},
        "task_path": {"type": "string", "minLength": 1},
        "trace_path": {"type": ["string", "null"]},
        "resource_usage_path": {"type": ["string", "null"]},
        "seed": {"type": "integer"},
    },
}

SCORE_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "task_id",
        "attempt_id",
        "status",
        "valid",
        "components",
        "hard_gate_failures",
        "commands",
        "judge",
        "warnings",
        "scorer_package_sha256",
        "duration_ms",
    ],
    "properties": {
        "schema_version": {"const": SCORE_REPORT_VERSION},
        "task_id": {"type": "string", "minLength": 1},
        "attempt_id": {"type": "string"},
        "status": {"enum": list(REPORT_STATUSES)},
        "score_total": {"type": ["number", "null"], "minimum": 0.0, "maximum": 1.0},
        "passed": {"type": ["boolean", "null"]},
        "valid": {"type": "boolean"},
        "pass_threshold": {
            "type": ["number", "null"],
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "unscored_weight": {
            "type": ["number", "null"],
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "value",
                    "weight",
                    "hard_gate",
                    "gate_passed",
                    "evidence",
                ],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "value": {
                        "type": ["number", "null"],
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "hard_gate": {"type": "boolean"},
                    "gate_passed": {"type": ["boolean", "null"]},
                    "evidence": {"type": "array"},
                },
            },
        },
        "hard_gate_failures": {"type": "array", "items": {"type": "string"}},
        "commands": {"type": "array"},
        "judge": {"type": ["object", "null"]},
        "warnings": {"type": "array"},
        "scorer_package_sha256": {"type": "string", "minLength": 1},
        "duration_ms": {"type": "integer", "minimum": 0},
        "repeatability": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "runs",
                "deterministic_stable",
                "unstable_components",
                "max_total_spread",
                "totals",
            ],
            "properties": {
                "runs": {"type": "integer", "minimum": 2},
                "deterministic_stable": {"type": "boolean"},
                "unstable_components": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "max_total_spread": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "totals": {
                    "type": "array",
                    "minItems": 2,
                    "items": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
            },
        },
    },
}

SCORER_ISOLATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "scorer_launcher",
        "candidate_mount",
        "secure_exec_available",
        "judge_launcher",
    ],
    "properties": {
        "schema_version": {"const": SCORER_ISOLATION_VERSION},
        "scorer_launcher": {"const": "landlock"},
        "candidate_mount": {"const": "read_only"},
        "secure_exec_available": {"const": True},
        "judge_launcher": {"enum": ["landlock", "not_run"]},
    },
}

PHASE_REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["schema_version", "task_id", "status", "components"],
    "properties": {
        "schema_version": {"const": SCORE_REPORT_VERSION},
        "task_id": {"type": "string", "minLength": 1},
        "attempt_id": {"type": "string"},
        "status": {"enum": list(REPORT_STATUSES)},
        "components": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "value": {
                        "type": ["number", "null"],
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "weight": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "hard_gate": {"type": "boolean"},
                    "gate_passed": {"type": ["boolean", "null"]},
                    "verdict": {"type": "string"},
                    "evidence": {"type": "array"},
                },
            },
        },
        "commands": {"type": "array"},
        "warnings": {"type": "array"},
    },
}


class ScorerError(RuntimeError):
    """A classified scorer-runtime failure, never an agent failure."""

    def __init__(self, status: str, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")


@dataclass(frozen=True)
class ScorerRunConfig:
    task: Path
    scorer: Path
    candidate: Path
    output: Path
    run_dir: Path
    attempt_id: str = ""
    seed: int = 0
    trace: Path | None = None
    resources: Path | None = None
    keep_sandbox: bool = False
    judge_model: str = ""
    # Repeat the deterministic phase to observe repeatability. The publication
    # gate that consumes this lives on the task-construction side; Ghostlab
    # only reports whether the values actually matched.
    repeat: int = 1


@dataclass
class PhaseResult:
    """What one scoring phase produced, plus how it went."""

    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    commands: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def external_symlinks(package_dir: Path) -> list[str]:
    """Package symlinks whose target leaves the package.

    They are mounted verbatim into ``/scorer`` and ``/fixtures`` — the copy
    preserves links — so an external one imports content the audit never saw and
    no package hash can cover.
    """
    package_dir = Path(package_dir).resolve()
    escaping: list[str] = []
    for path in sorted(package_dir.rglob("*")):
        if not path.is_symlink():
            continue
        target = (path.parent / os.readlink(path)).resolve()
        if not (target == package_dir or target.is_relative_to(package_dir)):
            escaping.append(path.relative_to(package_dir).as_posix())
    return escaping


def package_hash(package_dir: Path) -> str:
    """Content hash of a scorer package, ignoring its own recorded hash.

    Scorer-only skills, fixtures, and judge configuration are all inside the
    package directory, so they are all inside this hash: the audit's guarantee
    that "no oracle solution ships in the skills" is only meaningful if changing
    a skill changes the identity of the scorer.

    Symlinks are hashed by their target rather than skipped. The mount preserves
    them, so a link is effective content: retargeting one changes what the
    scorer reads and must therefore change the scorer's identity.
    """
    package_dir = Path(package_dir).resolve()
    entries: list[dict[str, str]] = []
    for path in sorted(package_dir.rglob("*")):
        relative = path.relative_to(package_dir).as_posix()
        if path.is_symlink():
            entries.append(
                {"path": relative, "kind": "symlink", "target": os.readlink(path)}
            )
            continue
        if not path.is_file():
            continue
        if relative == "scorer.json":
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop("package_sha256", None)
            digest = sha256_text(
                json.dumps(data, sort_keys=True, separators=(",", ":"))
            )
        else:
            digest = sha256_path(path)
        entries.append({"path": relative, "kind": "file", "sha256": digest})
    return sha256_text(json.dumps(entries, sort_keys=True, separators=(",", ":")))


def _positive_int(value: Any, default: int) -> int:
    """Read a numeric manifest field without trusting it to be a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value) if value > 0 else default


def render_scorer_policy(
    read_only: list[str], read_write: list[str], hosts: list[str] | None = None
) -> str:
    """An OpenShell policy that constrains the scorer's filesystem and egress.

    With no ``hosts`` the policy declares no network section at all, which is
    how "the deterministic scorer sandbox has no network" is actually enforced
    rather than merely intended. OpenShell needs ``/sandbox`` writable while it
    uploads inputs; the scorer launcher adds the strict per-process Landlock
    boundary before any scorer or candidate code executes.
    """
    lines = [
        "# Generated by Ghostlab: scorer isolation policy.",
        "#",
        "# Protected roots are reinforced by Ghostlab's Landlock scorer launcher.",
        "# Only the output and temporary roots remain writable to scorer code.",
        "version: 1",
        "",
        "filesystem_policy:",
        "  include_workdir: true",
        "  read_only: [" + ", ".join(sorted(set(read_only))) + "]",
        "  read_write: [" + ", ".join(sorted(set(read_write))) + "]",
        "",
        "landlock:",
        "  compatibility: best_effort",
        "",
    ]
    if hosts:
        endpoints = "\n".join(
            f"      - host: {host}\n        port: 443" for host in dict.fromkeys(hosts)
        )
        lines += [
            "network_policies:",
            "  scorer_provider:",
            "    name: scorer-provider",
            "    binaries:",
            "      - path: /usr/bin/node",
            "      - path: /usr/bin/opencode",
            "    endpoints:",
            endpoints,
            "",
        ]
    return "\n".join(lines)


def materialize_candidate(archive: Path, destination: Path) -> Path:
    """Extract a candidate workspace archive into ``destination/repo``.

    Anything wrong with the archive is ``invalid_candidate_artifact``: the
    candidate produced an unusable artifact, which is neither a scorer failure
    nor a zero.
    """
    archive = Path(archive).expanduser()
    if not archive.exists():
        raise ScorerError(
            STATUS_INVALID_CANDIDATE, f"candidate artifact not found: {archive}"
        )
    repo = Path(destination) / "repo"
    if archive.is_dir():
        shutil.copytree(archive, repo, symlinks=True, dirs_exist_ok=True)
        return repo
    repo.mkdir(parents=True, exist_ok=True)
    opened: tarfile.TarFile | None = None
    process: subprocess.Popen[bytes] | None = None
    stderr_reader: _BoundedStderr | None = None
    process_reaped = False
    try:
        opened, process, stderr_reader = _open_archive(archive)
        _safe_extract(opened, repo)
        opened.close()
        opened = None
        if process is not None:
            if process.stdout is None:
                raise ValueError("zstd produced no output stream")
            _drain_zstd_stdout(process.stdout)
            returncode, stderr = _reap_zstd(process, stderr_reader, abort=False)
            process_reaped = True
            if returncode != 0:
                detail = stderr.decode(errors="replace").strip()
                suffix = f": {detail[-500:]}" if detail else ""
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    f"zstd could not read {archive.name}{suffix}",
                )
    except ScorerError:
        raise
    except (
        tarfile.TarError,
        OSError,
        EOFError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        raise ScorerError(
            STATUS_INVALID_CANDIDATE,
            f"candidate artifact is not a readable archive: {exc}",
        ) from exc
    finally:
        try:
            if opened is not None:
                opened.close()
        finally:
            if process is not None and not process_reaped:
                _reap_zstd(process, stderr_reader, abort=True)
    return repo


class _BoundedStderr:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._tail = bytearray()
        self._thread = threading.Thread(
            target=self._drain,
            name="ghostlab-zstd-stderr",
            daemon=True,
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            for block in iter(lambda: self._stream.read(_ARCHIVE_CHUNK), b""):
                if len(block) >= _MAX_ZSTD_STDERR_BYTES:
                    self._tail[:] = block[-_MAX_ZSTD_STDERR_BYTES:]
                    continue
                overflow = len(self._tail) + len(block) - _MAX_ZSTD_STDERR_BYTES
                if overflow > 0:
                    del self._tail[:overflow]
                self._tail.extend(block)
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass

    def finish(self) -> bytes:
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass
            self._thread.join(timeout=1)
        return bytes(self._tail)


class _PrevalidatedTarInfo(tarfile.TarInfo):
    """Reject raw extension records before ``tarfile`` reads their payload."""

    def _proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo | None:
        state: Any = archive
        header_count = int(getattr(state, "_ghostlab_header_count", 0)) + 1
        state._ghostlab_header_count = header_count
        if header_count > MAX_CANDIDATE_ARCHIVE_MEMBERS:
            raise ScorerError(
                STATUS_INVALID_CANDIDATE,
                "candidate archive exceeds the raw member-count limit",
            )

        if self.type == tarfile.GNUTYPE_SPARSE:
            raise ScorerError(
                STATUS_INVALID_CANDIDATE,
                "unsupported candidate archive GNU sparse metadata",
            )
        if self.type in _TAR_METADATA_TYPES:
            if self.size < 0 or self.size > MAX_CANDIDATE_METADATA_BYTES:
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    "candidate archive metadata exceeds the per-record size limit",
                )
            padded_size = (self.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
            padded_size *= tarfile.BLOCKSIZE
            metadata_bytes = (
                int(getattr(state, "_ghostlab_metadata_bytes", 0)) + padded_size
            )
            state._ghostlab_metadata_bytes = metadata_bytes
            if metadata_bytes > MAX_CANDIDATE_TOTAL_METADATA_BYTES:
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    "candidate archive metadata exceeds the total size limit",
                )
            original = state.fileobj
            payload = original.read(padded_size)
            if self.type in _PAX_METADATA_TYPES and _pax_has_gnu_sparse(
                payload[: self.size]
            ):
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    "candidate archive contains unsupported GNU sparse PAX metadata",
                )
            state.fileobj = _ReplayReader(payload, original)
            try:
                return super()._proc_member(archive)  # type: ignore[misc]
            finally:
                state.fileobj = original
        return super()._proc_member(archive)  # type: ignore[misc]


class _ReplayReader:
    def __init__(self, prefix: bytes, stream: Any) -> None:
        self._prefix = prefix
        self._stream = stream
        self._index = 0
        self._start = int(stream.tell()) - len(prefix)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            prefix = self._prefix[self._index :]
            self._index = len(self._prefix)
            return prefix + self._stream.read()
        prefix = self._prefix[self._index : self._index + size]
        self._index += len(prefix)
        if len(prefix) == size:
            return prefix
        return prefix + self._stream.read(size - len(prefix))

    def tell(self) -> int:
        if self._index < len(self._prefix):
            return self._start + self._index
        return int(self._stream.tell())


def _pax_has_gnu_sparse(payload: bytes) -> bool:
    position = 0
    while position < len(payload) and payload[position] != 0:
        separator = payload.find(b" ", position)
        if separator < 0:
            return False
        try:
            length = int(payload[position:separator])
        except ValueError:
            return False
        end = position + length
        if length < 5 or end > len(payload) or payload[end - 1 : end] != b"\n":
            return False
        equals = payload.find(b"=", separator + 1, end - 1)
        if equals < 0:
            return False
        if payload[separator + 1 : equals].startswith(b"GNU.sparse."):
            return True
        position = end
    return False


def _reap_zstd(
    process: subprocess.Popen[bytes],
    stderr_reader: _BoundedStderr | None,
    *,
    abort: bool,
) -> tuple[int, bytes]:
    if process.stdout is not None and not process.stdout.closed:
        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass
    if abort and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        try:
            returncode = process.wait(timeout=5 if abort else 30)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        if stderr_reader is not None:
            stderr = stderr_reader.finish()
        else:
            stderr = b""
            if process.stderr is not None and not process.stderr.closed:
                try:
                    process.stderr.close()
                except (OSError, ValueError):
                    pass
    return returncode, stderr


def _drain_zstd_stdout(stream: Any) -> None:
    remaining = _MAX_ZSTD_TRAILING_BYTES + 1
    while remaining:
        block = stream.read(min(_ARCHIVE_CHUNK, remaining))
        if not block:
            return
        remaining -= len(block)
    raise ValueError("zstd produced excessive data after the candidate archive")


def _open_archive(
    archive: Path,
) -> tuple[
    tarfile.TarFile,
    subprocess.Popen[bytes] | None,
    _BoundedStderr | None,
]:
    if archive.name.endswith(".tar.zst"):
        if not shutil.which("zstd"):
            raise ScorerError(
                STATUS_INVALID_CANDIDATE,
                f"{archive.name} needs a zstd binary to decompress and none is installed",
            )
        process = subprocess.Popen(
            ["zstd", "-q", "-d", "-c", "--", str(archive)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stderr_reader: _BoundedStderr | None = None
        try:
            if process.stdout is None or process.stderr is None:
                raise ValueError("zstd produced no output or error stream")
            stderr_reader = _BoundedStderr(process.stderr)
            opened = tarfile.open(  # noqa: SIM115
                fileobj=process.stdout,
                mode="r|",
                tarinfo=_PrevalidatedTarInfo,
            )
            return opened, process, stderr_reader
        except BaseException:
            _reap_zstd(process, stderr_reader, abort=True)
            raise
    return (
        tarfile.open(archive, mode="r|*", tarinfo=_PrevalidatedTarInfo),
        None,
        None,
    )


def _contained(path: Path, root: Path) -> bool:
    """Whether ``path`` is really inside ``root``.

    A string prefix test would accept ``<root>-evil``, which is a sibling of the
    candidate mount, not a child of it.
    """
    return path == root or path.is_relative_to(root)


def _archive_target(name: str, root: Path, *, subject: str) -> tuple[str, Path]:
    if not name or "\0" in name:
        raise ScorerError(
            STATUS_INVALID_CANDIDATE,
            f"candidate archive {subject} has an unsafe empty name",
        )
    relative_path = PurePosixPath(name)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ScorerError(
            STATUS_INVALID_CANDIDATE,
            f"candidate archive {subject} escapes the extraction root: {name}",
        )
    relative = relative_path.as_posix()
    target = root if relative == "." else root / relative
    if not _contained(target.resolve(), root):
        raise ScorerError(
            STATUS_INVALID_CANDIDATE,
            f"candidate archive {subject} escapes the extraction root: {name}",
        )
    return relative, target


def _ensure_archive_parent(target: Path, root: Path) -> None:
    parent = target.parent
    if not _contained(parent.resolve(), root):
        raise ScorerError(
            STATUS_INVALID_CANDIDATE,
            f"candidate archive member escapes the extraction root: {target}",
        )
    parent.mkdir(parents=True, exist_ok=True)
    if not parent.is_dir():
        raise ScorerError(
            STATUS_INVALID_CANDIDATE,
            f"candidate archive member has a non-directory parent: {target}",
        )


def _reject_nonzero_size(member: tarfile.TarInfo) -> None:
    if member.size:
        raise ScorerError(
            STATUS_INVALID_CANDIDATE,
            f"candidate archive member has data for its type: {member.name}",
        )


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
    """Stream an allowlisted archive into ``destination`` within fixed limits."""
    root = destination.resolve()
    seen: set[str] = set()
    regular_files: dict[str, Path] = {}
    directory_modes: list[tuple[Path, int]] = []
    member_count = 0
    total_bytes = 0

    for member in archive:
        member_count += 1
        if member_count > MAX_CANDIDATE_ARCHIVE_MEMBERS:
            raise ScorerError(
                STATUS_INVALID_CANDIDATE,
                "candidate archive exceeds the member-count limit",
            )
        relative, target = _archive_target(member.name, root, subject="member")
        if relative in seen:
            raise ScorerError(
                STATUS_INVALID_CANDIDATE,
                f"candidate archive has a duplicate member: {member.name}",
            )
        seen.add(relative)

        if relative == ".":
            if not member.isdir():
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    "candidate archive root member is not a directory",
                )
            _reject_nonzero_size(member)
            continue

        if member.isdir():
            _reject_nonzero_size(member)
            _ensure_archive_parent(target, root)
            target.mkdir(exist_ok=True)
            if target.is_symlink() or not target.is_dir():
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    f"candidate archive directory conflicts with another member: {member.name}",
                )
            directory_modes.append((target, member.mode & 0o777))
            continue

        if member.isfile():
            if member.size < 0 or member.size > MAX_CANDIDATE_MEMBER_BYTES:
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    f"candidate archive member exceeds the per-member size limit: {member.name}",
                )
            total_bytes += member.size
            if total_bytes > MAX_CANDIDATE_TOTAL_BYTES:
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    "candidate archive exceeds the total expanded-size limit",
                )
            _ensure_archive_parent(target, root)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    f"candidate archive member has no data: {member.name}",
                )
            created = False
            try:
                with extracted, target.open("xb") as output:
                    created = True
                    remaining = member.size
                    while remaining:
                        block = extracted.read(min(_ARCHIVE_CHUNK, remaining))
                        if not block:
                            raise ScorerError(
                                STATUS_INVALID_CANDIDATE,
                                f"candidate archive member is truncated: {member.name}",
                            )
                        if len(block) > remaining:
                            raise ScorerError(
                                STATUS_INVALID_CANDIDATE,
                                f"candidate archive member exceeds its declared size: {member.name}",
                            )
                        output.write(block)
                        remaining -= len(block)
            except BaseException:
                if created:
                    try:
                        target.unlink()
                    except OSError:
                        pass
                raise
            target.chmod(member.mode & 0o777)
            regular_files[relative] = target.resolve()
            continue

        if member.issym():
            _reject_nonzero_size(member)
            if not member.linkname or "\0" in member.linkname:
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    f"candidate archive symlink has an unsafe target: {member.name}",
                )
            link_target = (target.parent / member.linkname).resolve()
            if not _contained(link_target, root):
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    f"candidate archive link escapes the extraction root: {member.name}",
                )
            _ensure_archive_parent(target, root)
            target.symlink_to(member.linkname)
            continue

        if member.islnk():
            _reject_nonzero_size(member)
            link_relative, _ = _archive_target(member.linkname, root, subject="link")
            source = regular_files.get(link_relative)
            if source is None or not stat.S_ISREG(source.lstat().st_mode):
                raise ScorerError(
                    STATUS_INVALID_CANDIDATE,
                    "candidate archive hard link must reference an earlier regular file: "
                    f"{member.name}",
                )
            _ensure_archive_parent(target, root)
            os.link(source, target, follow_symlinks=False)
            regular_files[relative] = target.resolve()
            continue

        raise ScorerError(
            STATUS_INVALID_CANDIDATE,
            f"unsupported candidate archive member type: {member.name}",
        )

    for path, mode in sorted(
        directory_modes,
        key=lambda item: len(item[0].parts),
        reverse=True,
    ):
        path.chmod(mode)


def load_task(path: Path) -> dict[str, Any]:
    task = load_json(Path(path))
    version = str(task.get("schema_version", ""))
    if version != BENCHMARK_TASK_VERSION:
        raise ScorerError(
            STATUS_SCORER_ERROR,
            f"task {path}: schema_version must be {BENCHMARK_TASK_VERSION!r}, got {version!r}",
        )
    if not str(task.get("task_id", "")):
        raise ScorerError(STATUS_SCORER_ERROR, f"task {path}: task_id is required")
    return task


def build_score_input(
    manifest: ScorerConfig, config: ScorerRunConfig, *, attempt_id: str
) -> dict[str, Any]:
    document = {
        "schema_version": SCORE_INPUT_VERSION,
        "task_id": manifest.task_id,
        "attempt_id": attempt_id,
        "repo_path": CANDIDATE_REPO,
        "task_path": f"{INPUT_ROOT}/task.json",
        "trace_path": (
            f"{INPUT_ROOT}/aut-events.jsonl"
            if config.trace and Path(config.trace).is_file()
            else None
        ),
        "resource_usage_path": (
            f"{INPUT_ROOT}/resources.json"
            if config.resources and Path(config.resources).is_file()
            else None
        ),
        "seed": int(config.seed),
    }
    errors = schema_errors(document, SCORE_INPUT_SCHEMA)
    if errors:
        raise ScorerError(
            STATUS_SCORER_ERROR, "score input is invalid: " + "; ".join(errors)
        )
    return document


def validate_report_document(
    document: Any,
    manifest: ScorerConfig,
    *,
    required_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Schema- and contract-validate a report a scorer produced."""
    if not isinstance(document, dict):
        raise ScorerError(STATUS_SCORER_ERROR, "scorer report is not a JSON object")
    errors = schema_errors(document, PHASE_REPORT_SCHEMA)
    if errors:
        raise ScorerError(
            STATUS_SCORER_ERROR,
            "scorer report failed schema validation: " + "; ".join(errors[:10]),
        )
    if str(document.get("task_id")) != manifest.task_id:
        raise ScorerError(
            STATUS_SCORER_ERROR,
            f"scorer report task_id {document.get('task_id')!r} does not match the "
            f"scorer manifest task_id {manifest.task_id!r}",
        )
    reported = {str(item.get("id")) for item in document.get("components", [])}
    unknown = sorted(reported - set(manifest.component_ids))
    if unknown:
        raise ScorerError(
            STATUS_SCORER_ERROR,
            "scorer report declares components absent from the manifest: "
            + ", ".join(unknown),
        )
    if str(document.get("status")) != "scored":
        # A report that says it is not a measurement owes no components.
        return document
    for required in required_ids or ():
        if required not in reported:
            raise ScorerError(
                STATUS_SCORER_ERROR,
                f"scorer report is missing declared component {required!r}",
            )
    return document


def _gate_passed(value: float | None, declared: Any) -> bool:
    if isinstance(declared, bool):
        return declared
    if value is None:
        return False
    # A hard gate is a behavior that must work, so full credit is the bar unless
    # the scorer says otherwise explicitly.
    return float(value) >= 1.0 - 1e-9


def compose_report(
    manifest: ScorerConfig,
    *,
    attempt_id: str,
    status: str,
    phases: list[PhaseResult],
    hashes: dict[str, Any],
    duration_ms: int,
    warnings: list[str] | None = None,
    judge: dict[str, Any] | None = None,
    repeatability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge phase results into one validated ``retro-score-report-v1``.

    Composed on the host: the total, the gates, and the unscored-weight verdict
    are Ghostlab's arithmetic over the scorer's observations, never the scorer's
    own claim about its total.
    """
    collected: dict[str, dict[str, Any]] = {}
    commands: list[dict[str, Any]] = []
    notes = list(warnings or [])
    for phase in phases:
        collected.update(phase.components)
        commands += phase.commands
        notes += phase.warnings

    components: list[dict[str, Any]] = []
    hard_gate_failures: list[str] = []
    unscored_weight = 0.0
    total = 0.0
    for declared in manifest.components:
        observed = dict(collected.get(declared.id) or {})
        verdict = str(observed.get("verdict") or "")
        raw_value = observed.get("value")
        scored = (
            raw_value is not None
            and verdict != "CANNOT_ASSESS"
            and isinstance(raw_value, (int, float))
            and not isinstance(raw_value, bool)
        )
        value = float(raw_value) if scored and raw_value is not None else None
        if value is not None:
            value = min(max(value, declared.value_range[0]), declared.value_range[1])
        entry = {
            "id": declared.id,
            "value": value,
            "weight": declared.weight,
            "hard_gate": declared.hard_gate,
            "gate_passed": None,
            "evidence": list(observed.get("evidence") or []),
        }
        if declared.hard_gate:
            passed = _gate_passed(value, observed.get("gate_passed"))
            entry["gate_passed"] = passed
            if not passed:
                hard_gate_failures.append(declared.id)
        if scored and value is not None:
            total += value * declared.weight
        else:
            unscored_weight += declared.weight
        components.append(entry)

    valid = unscored_weight <= UNSCORED_WEIGHT_LIMIT + 1e-9
    if not valid:
        notes.append(
            f"{unscored_weight:.3f} of total weight is unscored, above the "
            f"{UNSCORED_WEIGHT_LIMIT:.0%} limit; this task result is invalid"
        )
    elif unscored_weight > 0:
        notes.append(
            f"{unscored_weight:.3f} of total weight is unscored and was not renormalized"
        )
    if hard_gate_failures:
        total = 0.0

    scored_status = status == STATUS_SCORED
    report = {
        "schema_version": SCORE_REPORT_VERSION,
        "task_id": manifest.task_id,
        "attempt_id": attempt_id,
        "status": status,
        "valid": bool(scored_status and valid),
        "pass_threshold": manifest.pass_threshold,
        "unscored_weight": round(unscored_weight, 6),
        "components": components,
        "hard_gate_failures": hard_gate_failures,
        "commands": commands,
        "judge": judge,
        "warnings": notes,
        "scorer_package_sha256": str(hashes.get("scorer_package_sha256") or ""),
        "duration_ms": int(duration_ms),
    }
    if scored_status:
        report["score_total"] = round(total, 6)
        report["passed"] = bool(
            valid and not hard_gate_failures and total + 1e-9 >= manifest.pass_threshold
        )
    if repeatability:
        report["repeatability"] = repeatability
    errors = schema_errors(report, SCORE_REPORT_SCHEMA)
    if errors:
        raise ScorerError(
            STATUS_SCORER_ERROR, "composed report is invalid: " + "; ".join(errors[:10])
        )
    return report


def error_report(
    *,
    task_id: str,
    attempt_id: str,
    status: str,
    detail: str,
    duration_ms: int = 0,
    hashes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A schema-valid report for a run that produced no number.

    Harness and scorer failures are never converted to zero, so numeric fields
    are omitted and aggregation counts the attempt as invalid instead.
    """
    public_warning = {
        STATUS_INVALID_CANDIDATE: "Candidate artifact is invalid.",
        STATUS_SCORER_TIMEOUT: "Scorer execution timed out.",
        STATUS_JUDGE_UNAVAILABLE: "Judge was unavailable.",
        STATUS_SCORER_ERROR: "Scorer execution failed.",
    }.get(status, "Scoring did not produce a result.")
    report = {
        "schema_version": SCORE_REPORT_VERSION,
        "task_id": task_id or "unknown",
        "attempt_id": attempt_id,
        "status": status,
        "valid": False,
        "components": [],
        "hard_gate_failures": [],
        "commands": [],
        "judge": None,
        "warnings": [public_warning],
        "scorer_package_sha256": str(
            (hashes or {}).get("scorer_package_sha256") or ("0" * 64)
        ),
        "duration_ms": int(duration_ms),
    }
    errors = schema_errors(report, SCORE_REPORT_SCHEMA)
    if errors:
        raise RuntimeError("Ghostlab generated an invalid error report: " + "; ".join(errors))
    return report


# --------------------------------------------------------------------------- #
# Staging and sandboxes
# --------------------------------------------------------------------------- #
def _copy_package(
    manifest: ScorerConfig, scorer_mount: Path, fixtures_mount: Path
) -> None:
    """Split the package into the scorer mount and the hidden fixtures mount."""
    scorer_mount.mkdir(parents=True, exist_ok=True)
    fixtures_mount.mkdir(parents=True, exist_ok=True)
    for entry in sorted(manifest.package_dir.iterdir()):
        if entry.name == "fixtures":
            if entry.is_dir():
                shutil.copytree(
                    entry, fixtures_mount, symlinks=True, dirs_exist_ok=True
                )
            continue
        destination = scorer_mount / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, destination)


def _mount_uploads(staging: Path, names: tuple[str, ...]) -> list[dict[str, str]]:
    """Uploads that place ``staging/<name>`` under its protected sandbox root.

    The public scorer contract keeps stable logical roots, while the runtime maps
    them below ``/sandbox`` so OpenShell can upload them before locking the
    filesystem policy. Targets are constructed here, never taken from user input.
    """
    uploads = []
    for name in names:
        source = staging / name
        if not source.is_dir():
            continue
        uploads.append({"source": str(source), "target": f"/sandbox/mount-{name}"})
    return uploads


def _resolved_image_reference(image: str) -> str:
    if any(character.isspace() for character in image):
        raise ScorerError(
            STATUS_SCORER_ERROR,
            f"scorer runtime image contains whitespace: {image!r}",
        )
    if any(separator in image for separator in ("/", ":", ".")):
        return image
    registry = os.environ.get(
        "OPENSHELL_COMMUNITY_REGISTRY",
        "ghcr.io/nvidia/openshell-community/sandboxes",
    ).rstrip("/")
    if not registry or any(character.isspace() for character in registry):
        raise ScorerError(
            STATUS_SCORER_ERROR,
            "OPENSHELL_COMMUNITY_REGISTRY is not a valid image registry prefix",
        )
    return f"{registry}/{image}:latest"


def _dockerignore_rules(
    context: Path,
) -> list[tuple[str, bool, bool, bool]]:
    path = context / ".dockerignore"
    if not path.exists():
        return []
    if path.is_symlink():
        raise ScorerError(
            STATUS_SCORER_ERROR,
            "scorer runtime image context has an unsafe .dockerignore symlink",
        )
    rules: list[tuple[str, bool, bool, bool]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        value = raw.strip()
        if not value or value == "." or value.startswith("#"):
            continue
        negated = value.startswith("!")
        if negated or value.startswith(("\\!", "\\#")):
            value = value[1:]
        anchored = value.startswith("/")
        directory_only = value.endswith("/")
        value = value.strip("/")
        if value:
            rules.append((value, negated, anchored, directory_only))
    return rules


def _dockerignore_match(
    relative: PurePosixPath,
    *,
    is_dir: bool,
    rules: list[tuple[str, bool, bool, bool]],
) -> bool:
    path = relative.as_posix()
    candidates = [(path, is_dir)]
    candidates += [
        (parent.as_posix(), True)
        for parent in relative.parents
        if parent.as_posix() != "."
    ]
    excluded = False
    for pattern, negated, anchored, directory_only in rules:
        matched = False
        for candidate, candidate_is_dir in candidates:
            if directory_only and not candidate_is_dir:
                continue
            if anchored or "/" in pattern:
                matches = fnmatchcase(candidate, pattern)
                if pattern.startswith("**/"):
                    matches = matches or fnmatchcase(candidate, pattern[3:])
            else:
                matches = fnmatchcase(PurePosixPath(candidate).name, pattern)
            if matches:
                matched = True
                break
        if matched:
            excluded = not negated
    return excluded


def _copy_image_context(source: Path, destination: Path) -> tuple[Path, str]:
    source = source.resolve()
    context = source if source.is_dir() else source.parent
    dockerfile = source / "Dockerfile" if source.is_dir() else source
    dockerfile_name = dockerfile.name.lower()
    if not dockerfile.is_file() or (
        "dockerfile" not in dockerfile_name
        and not dockerfile_name.endswith(".dockerfile")
    ):
        raise ScorerError(
            STATUS_SCORER_ERROR,
            f"scorer runtime image path has no Dockerfile: {source}",
        )

    excluded_root = (
        destination.relative_to(context).parts[0]
        if destination.is_relative_to(context)
        else ""
    )
    dockerfile_body = dockerfile.read_text(encoding="utf-8")
    rules = _dockerignore_rules(context)
    entries = sorted(context.rglob("*"))
    destination.mkdir(mode=0o700)
    directories: list[tuple[Path, Path]] = []
    for entry in entries:
        relative = entry.relative_to(context)
        if excluded_root and relative.parts[:1] == (excluded_root,):
            continue
        if relative.as_posix() != ".dockerignore" and _dockerignore_match(
            PurePosixPath(relative.as_posix()),
            is_dir=entry.is_dir(),
            rules=rules,
        ):
            continue
        target = destination / relative
        if entry.is_symlink():
            link_target = Path(os.readlink(entry))
            if link_target.is_absolute() or not _contained(
                (entry.parent / link_target).resolve(), context
            ):
                raise ScorerError(
                    STATUS_SCORER_ERROR,
                    f"scorer runtime image context has an unsafe symlink: {relative}",
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(link_target, target_is_directory=entry.is_dir())
        elif entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            directories.append((entry, target))
        elif entry.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
        else:
            raise ScorerError(
                STATUS_SCORER_ERROR,
                f"scorer runtime image context has an unsupported file: {relative}",
            )
    for original, copied in reversed(directories):
        shutil.copystat(original, copied)
    return destination, dockerfile_body


def _contract_image(manifest: ScorerConfig, destination: Path) -> str:
    """Layer immutable logical-root aliases over the configured scorer image."""
    try:
        image = str(manifest.runtime.get("image") or "base")
        source = Path(image).expanduser()
        if source.exists():
            context, dockerfile_body = _copy_image_context(source, destination)
            dockerfile = context / "Dockerfile"
        else:
            context = destination
            context.mkdir(mode=0o700, parents=True)
            dockerfile = context / "Dockerfile"
            dockerfile_body = f"FROM {_resolved_image_reference(image)}\n"

        aliases = context / ".ghostlab-contract-roots.tar"
        if aliases.exists() or aliases.is_symlink():
            raise ScorerError(
                STATUS_SCORER_ERROR,
                "scorer runtime image context reserves .ghostlab-contract-roots.tar",
            )
        with tarfile.open(aliases, "w", format=tarfile.GNU_FORMAT) as archive:
            for logical, physical in CONTRACT_ROOTS.items():
                link = tarfile.TarInfo(logical.lstrip("/"))
                link.type = tarfile.SYMTYPE
                link.linkname = physical
                link.mode = 0o777
                archive.addfile(link)

        if dockerfile.is_symlink():
            dockerfile.unlink()
        dockerfile.write_text(
            dockerfile_body.rstrip()
            + "\n\n# Stable retro-scorer-v1 paths backed by OpenShell-safe uploads.\n"
            + "ADD .ghostlab-contract-roots.tar /\n",
            encoding="utf-8",
        )
        dockerignore = context / ".dockerignore"
        if dockerignore.is_symlink():
            raise ScorerError(
                STATUS_SCORER_ERROR,
                "scorer runtime image context has an unsafe .dockerignore symlink",
            )
        with dockerignore.open("a", encoding="utf-8") as handle:
            handle.write("\n!.ghostlab-contract-roots.tar\n")
        return str(context)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _sandbox_config(
    manifest: ScorerConfig,
    staging: Path,
    *,
    names: tuple[str, ...],
    read_only: list[str],
    read_write: list[str],
    policy_path: Path,
    providers: list[str] | None = None,
    hosts: list[str] | None = None,
    workdir: str = PHYSICAL_OUTPUT_ROOT,
    keep: bool = False,
    artifact_dir: Path | None = None,
) -> dict[str, Any]:
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        render_scorer_policy(read_only, read_write, hosts), encoding="utf-8"
    )
    runtime = manifest.runtime
    image_context = policy_path.parent / (
        f".scorer-image-context-{secrets.token_hex(8)}"
    )
    config: dict[str, Any] = {
        "backend": "openshell",
        "name": "ghostlab-scorer",
        "image": _contract_image(manifest, image_context),
        "_transient_image_context": str(image_context),
        "workdir": workdir,
        "network": "policy",
        "policy": str(policy_path),
        "providers": list(providers or []),
        "env_allowlist": [],
        "uploads": _mount_uploads(staging, names),
        "keep": bool(keep),
        "startup_timeout": _positive_int(runtime.get("startup_timeout"), 300),
    }
    if _positive_int(runtime.get("cpu"), 0):
        config["cpu"] = _positive_int(runtime.get("cpu"), 0)
    if _positive_int(runtime.get("memory_mb"), 0):
        config["memory"] = f"{_positive_int(runtime.get('memory_mb'), 0)}Mi"
    if artifact_dir is not None:
        config["artifact_dir"] = str(artifact_dir)
    # Mount roots are Ghostlab-constructed absolute paths, not user input, so
    # the /sandbox upload guard does not apply to them.
    try:
        return normalize_sandbox(config, allow_roots=("/",))
    except BaseException:
        shutil.rmtree(image_context, ignore_errors=True)
        raise


def _create_sandbox(
    sandbox_factory: Callable[..., OpenShellSandbox],
    sandbox_config: dict[str, Any],
    *,
    role: str,
) -> OpenShellSandbox:
    context = Path(str(sandbox_config["_transient_image_context"]))
    try:
        sandbox = sandbox_factory(sandbox_config, role=role)
        sandbox.create()
        return sandbox
    finally:
        shutil.rmtree(context, ignore_errors=True)


def _attested_command(
    command: list[str],
    *,
    remote_marker: str,
    nonce: str,
    read_only: tuple[str, ...],
    read_write: tuple[str, ...],
) -> list[str]:
    return [
        SYSTEM_PYTHON,
        "-I",
        "-S",
        "-c",
        _ISOLATION_EXEC,
        remote_marker,
        nonce,
        PHYSICAL_SECURE_EXEC_LAUNCHER,
        SCORER_WRAPPER,
        *[
            item
            for path in read_only
            for item in ("--read-only", path)
        ],
        *[
            item
            for path in read_write
            for item in ("--read-write", path)
        ],
        "--",
        *command,
    ]


def _landlock_command(
    command: list[str],
    *,
    read_only: tuple[str, ...],
    read_write: tuple[str, ...],
) -> list[str]:
    return [
        SYSTEM_PYTHON,
        "-I",
        "-S",
        SCORER_WRAPPER,
        *[
            item
            for path in read_only
            for item in ("--read-only", path)
        ],
        *[
            item
            for path in read_write
            for item in ("--read-write", path)
        ],
        "--",
        *command,
    ]


def _consume_isolation_marker(
    sandbox: OpenShellSandbox,
    *,
    remote_marker: str,
    local_marker: Path,
    nonce: str,
) -> bool:
    local_marker.unlink(missing_ok=True)
    try:
        sandbox.download(remote_marker, local_marker)
        marker = json.loads(local_marker.read_text(encoding="utf-8"))
    except (SandboxError, json.JSONDecodeError, OSError):
        return False
    finally:
        local_marker.unlink(missing_ok=True)
    return (
        isinstance(marker, dict)
        and marker.get("nonce") == nonce
        and marker.get("secure_exec_available") is True
    )


def _record_isolation(
    isolation: dict[str, Any],
    *,
    judge: bool,
) -> None:
    isolation.update(
        {
            "schema_version": SCORER_ISOLATION_VERSION,
            "scorer_launcher": "landlock",
            "candidate_mount": "read_only",
            "secure_exec_available": True,
            "judge_launcher": "landlock" if judge else "not_run",
        }
    )
    errors = schema_errors(isolation, SCORER_ISOLATION_SCHEMA)
    if errors:
        raise RuntimeError(
            "Ghostlab generated an invalid isolation attestation: "
            + "; ".join(errors)
        )


def _secure_exec_script(wrapper: str) -> str:
    return (
        "#!/bin/sh\n"
        "unset PYTHONHOME PYTHONPATH PYTHONSTARTUP PYTHONINSPECT "
        "PYTHONUSERBASE PYTHONSAFEPATH PYTHONPYCACHEPREFIX\n"
        f"exec {SYSTEM_PYTHON} -I -S {shlex.quote(wrapper)} \"$@\"\n"
    )


def run_deterministic_phase(
    manifest: ScorerConfig,
    config: ScorerRunConfig,
    staging: Path,
    *,
    sandbox_factory: Callable[..., OpenShellSandbox] = OpenShellSandbox,
    index: int = 0,
    isolation: dict[str, Any] | None = None,
) -> PhaseResult:
    """Run the scorer entrypoint in a network-free, credential-free sandbox."""
    sandbox_config = _sandbox_config(
        manifest,
        staging,
        names=("candidate", "scorer", "fixtures", "input", "output"),
        read_only=[
            PHYSICAL_CANDIDATE_ROOT,
            PHYSICAL_SCORER_ROOT,
            PHYSICAL_FIXTURES_ROOT,
            PHYSICAL_INPUT_ROOT,
            "/usr",
            "/lib",
            "/etc",
        ],
        read_write=["/sandbox", PHYSICAL_OUTPUT_ROOT, "/tmp", "/dev/null"],
        policy_path=config.run_dir / "scorer-policy.yaml",
        providers=[],
        hosts=[],
        keep=config.keep_sandbox,
        artifact_dir=config.run_dir,
    )
    sandbox = _create_sandbox(sandbox_factory, sandbox_config, role="scorer")
    sandbox_name = sandbox.name
    started = time.monotonic()
    suffix = "" if index == 0 else f"-{index + 1}"
    local_report = config.run_dir / f"deterministic-report{suffix}.json"
    remote_marker = f"{PHYSICAL_OUTPUT_ROOT}/.isolation-scorer{suffix}.json"
    local_marker = config.run_dir / f".isolation-scorer{suffix}.json"
    nonce = secrets.token_hex(32)
    read_only = (
        PHYSICAL_CANDIDATE_ROOT,
        PHYSICAL_SCORER_ROOT,
        PHYSICAL_FIXTURES_ROOT,
        PHYSICAL_INPUT_ROOT,
        "/usr",
        "/bin",
        "/lib",
        "/lib64",
        "/etc",
        "/proc",
        "/dev/urandom",
        "/sandbox/.venv",
        "/sandbox/.uv",
        "/app",
        "/opt",
        "/var",
    )
    read_write = (PHYSICAL_OUTPUT_ROOT, "/tmp", "/dev/null")
    try:
        try:
            attested = _attested_command(
                list(manifest.entrypoint),
                remote_marker=remote_marker,
                nonce=nonce,
                read_only=read_only,
                read_write=read_write,
            )
            command = _landlock_command(
                attested,
                read_only=read_only,
                read_write=read_write,
            )
            result = sandbox.exec(
                command,
                input_text=None,
                env={
                    "GHOSTLAB_CANDIDATE_ROOT": CANDIDATE_ROOT,
                    "GHOSTLAB_SCORER_ROOT": SCORER_ROOT,
                    "GHOSTLAB_FIXTURES_ROOT": FIXTURES_ROOT,
                    "GHOSTLAB_INPUT_ROOT": INPUT_ROOT,
                    "GHOSTLAB_OUTPUT_ROOT": OUTPUT_ROOT,
                    "GHOSTLAB_SECURE_EXEC": SECURE_EXEC_LAUNCHER,
                },
                timeout=manifest.timeout_seconds(),
            )
        except SandboxError as exc:
            if (
                isolation is not None
                and sandbox.created
                and _consume_isolation_marker(
                    sandbox,
                    remote_marker=remote_marker,
                    local_marker=local_marker,
                    nonce=nonce,
                )
            ):
                _record_isolation(isolation, judge=False)
            status = (
                STATUS_SCORER_TIMEOUT
                if exc.kind == "sandbox_timeout"
                else STATUS_SCORER_ERROR
            )
            raise ScorerError(status, f"{exc.kind}: {exc.detail}") from exc

        if not _consume_isolation_marker(
            sandbox,
            remote_marker=remote_marker,
            local_marker=local_marker,
            nonce=nonce,
        ):
            raise ScorerError(
                STATUS_SCORER_ERROR,
                "scorer Landlock launcher did not produce a valid isolation marker",
            )
        if isolation is not None:
            _record_isolation(isolation, judge=False)

        duration_ms = int((time.monotonic() - started) * 1000)
        (config.run_dir / f"scorer-stdout{suffix}.txt").write_text(
            result.stdout or "", encoding="utf-8"
        )
        (config.run_dir / f"scorer-stderr{suffix}.txt").write_text(
            result.stderr or "", encoding="utf-8"
        )

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            raise ScorerError(
                STATUS_SCORER_ERROR,
                f"scorer entrypoint exited {result.returncode}: {detail}",
            )

        try:
            sandbox.download(
                f"{PHYSICAL_OUTPUT_ROOT}/score-report.json", local_report
            )
        except SandboxError as exc:
            raise ScorerError(
                STATUS_SCORER_ERROR,
                "scorer produced no "
                f"{OUTPUT_ROOT}/score-report.json: {exc.detail}",
            ) from exc
    finally:
        # Step 6 of the contract: the deterministic sandbox is deleted as soon
        # as its component report is on the host — on every path, so a crash
        # here cannot leave a live workload behind either.
        sandbox.close()

    try:
        document = json.loads(local_report.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ScorerError(
            STATUS_SCORER_ERROR, f"scorer report is not valid JSON: {exc}"
        ) from exc

    residual = set(manifest.judge_criteria())
    required = tuple(
        component.id
        for component in manifest.components
        if component.hard_gate and component.id not in residual
    )
    validated = validate_report_document(document, manifest, required_ids=required)
    declared = str(validated.get("status") or STATUS_SCORED)
    if declared != STATUS_SCORED:
        # The scorer said its own run was not a measurement. Republishing that
        # as `scored` would turn a harness failure into a zero.
        detail = "; ".join(str(item) for item in (validated.get("warnings") or []))
        raise ScorerError(
            declared if declared in REPORT_STATUSES else STATUS_SCORER_ERROR,
            f"scorer reported status {declared!r}" + (f": {detail}" if detail else ""),
        )
    components = {
        str(item.get("id")): dict(item)
        for item in validated.get("components", [])
        if str(item.get("id")) not in residual
    }
    return PhaseResult(
        components=components,
        commands=list(validated.get("commands") or []),
        warnings=list(validated.get("warnings") or []),
        detail={
            "duration_ms": duration_ms,
            "report": str(local_report),
            "sandbox": sandbox_name,
            "run": index + 1,
        },
    )


def _repeatability(manifest: ScorerConfig, runs: list[PhaseResult]) -> dict[str, Any]:
    """Whether repeated deterministic executions produced the same values.

    Section 11.2 requires deterministic components to match exactly and bounds
    the spread of measured ones. Ghostlab reports both; whether that clears the
    publication gate is the task builder's decision, not the runtime's.
    """
    observed: dict[str, list[Any]] = {}
    for phase in runs:
        for component_id, payload in phase.components.items():
            observed.setdefault(component_id, []).append(payload.get("value"))
    unstable = sorted(
        component_id
        for component_id, values in observed.items()
        if len(set(map(repr, values))) > 1
    )
    totals = []
    for phase in runs:
        total = 0.0
        for declared in manifest.components:
            value = (phase.components.get(declared.id) or {}).get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += float(value) * declared.weight
        totals.append(total)
    return {
        "runs": len(runs),
        "deterministic_stable": not unstable,
        "unstable_components": unstable,
        "max_total_spread": round(max(totals) - min(totals), 6) if totals else 0.0,
        "totals": [round(total, 6) for total in totals],
    }


def load_judge_agent(manifest: ScorerConfig) -> dict[str, Any]:
    """Load the pinned judge agent config and enforce the permission floor."""
    path = Path(str(manifest.judge.get("agent_config")))
    data = load_json(path)
    runtime = dict(data.get("runtime") or {})
    model = os.path.expandvars(str(runtime.get("model") or ""))
    if not model or "$" in model:
        raise ScorerError(
            STATUS_JUDGE_UNAVAILABLE,
            f"judge agent {path} has no pinned model (got {runtime.get('model')!r}); a judge "
            "may not fall back to whatever default model happens to be configured",
        )
    permission = {
        str(k): str(v) for k, v in dict(runtime.get("permission") or {}).items()
    }
    violations = [
        f"permission.{key} must be {expected!r}, got {permission.get(key)!r}"
        for key, expected in JUDGE_PERMISSION_FLOOR.items()
        if permission.get(key) != expected
    ]
    tools = dict(runtime.get("tools") or {})
    for tool in ("bash", "webfetch"):
        if tools.get(tool) is not False:
            violations.append(f"tools.{tool} must be false, got {tools.get(tool)!r}")
    if violations:
        raise ScorerError(
            STATUS_SCORER_ERROR,
            f"judge agent {path} violates the scorer permission floor: "
            + "; ".join(violations),
        )
    return {**data, "runtime": {**runtime, "model": model}}


def _judge_prompt(
    manifest: ScorerConfig,
    criteria: list[str],
    rubric: str,
    deterministic: dict[str, Any],
) -> str:
    schema = Path(str(manifest.judge["output_schema"])).read_text(encoding="utf-8")
    return (
        RESIDUAL_JUDGE_PROMPT.format(criterion_id=", ".join(criteria))
        + "\n---\nRubric:\n"
        + rubric
        + "\n---\nDeterministic ScoreReport (authoritative for correctness):\n"
        + json.dumps(deterministic, indent=2, sort_keys=True)
        + f"\n---\nCandidate repository (read-only): {CANDIDATE_REPO}\n"
        + f"Task: {INPUT_ROOT}/task.json\n"
        + "\n---\nReply with a single JSON value validating against this schema. "
        "Output only JSON.\n" + schema + "\n"
    )


def _judge_results(document: Any, criteria: list[str]) -> dict[str, dict[str, Any]]:
    """Read judge output in either the list or mapping shape."""
    entries: list[dict[str, Any]] = []
    if isinstance(document, dict) and isinstance(document.get("criteria"), list):
        entries = [item for item in document["criteria"] if isinstance(item, dict)]
    elif isinstance(document, list):
        entries = [item for item in document if isinstance(item, dict)]
    elif isinstance(document, dict):
        entries = [
            {"id": key, **value}
            for key, value in document.items()
            if isinstance(value, dict) and key in criteria
        ]
    results: dict[str, dict[str, Any]] = {}
    for entry in entries:
        component_id = str(entry.get("id") or entry.get("criterion") or "")
        if component_id not in criteria:
            continue
        verdict = str(entry.get("verdict") or "").upper()
        if verdict not in JUDGE_VERDICTS:
            verdict = "CANNOT_ASSESS"
        raw = entry.get("value")
        value = (
            float(raw)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool)
            else None
        )
        if verdict == "CANNOT_ASSESS":
            value = None
        results[component_id] = {
            "value": value,
            "verdict": verdict,
            "evidence": list(entry.get("evidence") or []),
            "rationale": str(entry.get("rationale") or "")[:2000],
        }
    return results


def run_judge_phase(
    manifest: ScorerConfig,
    config: ScorerRunConfig,
    staging: Path,
    deterministic: dict[str, Any],
    *,
    sandbox_factory: Callable[..., OpenShellSandbox] = OpenShellSandbox,
    isolation: dict[str, Any] | None = None,
) -> PhaseResult:
    """Score declared residual criteria in a sandbox that cannot execute code."""
    from .agent_sandbox import provider_endpoints
    from .opencode_backend import collect_text, extract_json, first_stream_error

    criteria = list(manifest.judge_criteria())
    agent = load_judge_agent(manifest)
    runtime = dict(agent.get("runtime") or {})
    model = str(config.judge_model or runtime.get("model") or "")
    rubric = Path(str(manifest.judge["prompt"])).read_text(encoding="utf-8")
    prompt = _judge_prompt(manifest, criteria, rubric, deterministic)

    judge_input = staging / "judge" / "input"
    judge_output = staging / "judge" / "output"
    judge_input.mkdir(parents=True, exist_ok=True)
    judge_output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(staging / "input" / "task.json", judge_input / "task.json")
    (judge_input / "rubric.md").write_text(rubric, encoding="utf-8")
    (judge_input / "deterministic-report.json").write_text(
        json.dumps(deterministic, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for runtime_file in (
        "ghostlab-scorer-wrapper.py",
        "ghostlab-secure-exec",
    ):
        shutil.copy2(staging / "input" / runtime_file, judge_input / runtime_file)
    (judge_output / ".keep").write_text("", encoding="utf-8")

    from .opencode_config import build_project_config

    # Only the pinned model and the permission floor cross into the sandbox.
    # Forwarding the judge agent's own instruction paths would point OpenCode at
    # host files that do not exist inside the container.
    project = build_project_config(
        {
            "model": model,
            "permission": dict(JUDGE_PERMISSION_FLOOR),
            "tools": {"bash": False, "webfetch": False},
        }
    )
    (judge_output / "opencode.json").write_text(
        json.dumps(project, indent=2), encoding="utf-8"
    )

    judge_staging = staging / "judge"
    candidate_source = staging / "candidate"
    candidate_destination = judge_staging / "candidate"
    if candidate_source.is_dir() and not candidate_destination.exists():
        shutil.copytree(candidate_source, candidate_destination, symlinks=True)

    sandbox_config = _sandbox_config(
        manifest,
        judge_staging,
        names=("candidate", "input", "output"),
        read_only=[
            PHYSICAL_CANDIDATE_ROOT,
            PHYSICAL_INPUT_ROOT,
            "/usr",
            "/lib",
            "/etc",
        ],
        read_write=[
            "/sandbox",
            PHYSICAL_OUTPUT_ROOT,
            "/tmp",
            "/dev/null",
            "/opt/agent",
        ],
        policy_path=config.run_dir / "judge-policy.yaml",
        providers=list(
            (agent.get("sandbox") or {}).get("providers") or [model.split("/", 1)[0]]
        ),
        hosts=provider_endpoints(model),
        keep=config.keep_sandbox,
        artifact_dir=config.run_dir,
    )
    sandbox = _create_sandbox(sandbox_factory, sandbox_config, role="judge")
    sandbox_name = sandbox.name
    remote_marker = f"{PHYSICAL_OUTPUT_ROOT}/.isolation-judge.json"
    local_marker = config.run_dir / ".isolation-judge.json"
    nonce = secrets.token_hex(32)
    read_only = (
        PHYSICAL_CANDIDATE_ROOT,
        PHYSICAL_INPUT_ROOT,
        "/usr",
        "/bin",
        "/lib",
        "/lib64",
        "/etc",
        "/proc",
        "/dev/urandom",
        "/sandbox/.venv",
        "/sandbox/.uv",
        "/app",
        "/opt",
        "/var",
    )
    read_write = (
        PHYSICAL_OUTPUT_ROOT,
        "/tmp",
        "/dev/null",
        "/opt/agent",
    )
    judge_command = [
        "opencode",
        "run",
        "--format",
        "json",
        "--log-level",
        "ERROR",
        "--model",
        model,
        "--dir",
        OUTPUT_ROOT,
    ]
    command = _landlock_command(
        _attested_command(
            judge_command,
            remote_marker=remote_marker,
            nonce=nonce,
            read_only=read_only,
            read_write=read_write,
        ),
        read_only=read_only,
        read_write=read_write,
    )
    try:
        try:
            result = sandbox.exec(
                command,
                input_text=prompt,
                env={
                    "GHOSTLAB_CANDIDATE_ROOT": CANDIDATE_ROOT,
                    "GHOSTLAB_INPUT_ROOT": INPUT_ROOT,
                    "GHOSTLAB_OUTPUT_ROOT": OUTPUT_ROOT,
                },
                timeout=_positive_int(
                    runtime.get("timeout_seconds"), manifest.timeout_seconds()
                ),
            )
        except SandboxError as exc:
            if (
                isolation is not None
                and sandbox.created
                and _consume_isolation_marker(
                    sandbox,
                    remote_marker=remote_marker,
                    local_marker=local_marker,
                    nonce=nonce,
                )
            ):
                _record_isolation(isolation, judge=True)
            raise ScorerError(
                STATUS_JUDGE_UNAVAILABLE, f"{exc.kind}: {exc.detail}"
            ) from exc

        if not _consume_isolation_marker(
            sandbox,
            remote_marker=remote_marker,
            local_marker=local_marker,
            nonce=nonce,
        ):
            raise ScorerError(
                STATUS_JUDGE_UNAVAILABLE,
                "judge Landlock launcher did not produce a valid isolation marker",
            )
        if isolation is not None:
            _record_isolation(isolation, judge=True)
    finally:
        sandbox.close()

    (config.run_dir / "judge-stdout.txt").write_text(
        result.stdout or "", encoding="utf-8"
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-1000:]
        raise ScorerError(
            STATUS_JUDGE_UNAVAILABLE,
            f"judge agent exited {result.returncode}: {detail}",
        )
    stream_error = first_stream_error(result.stdout or "")
    if stream_error:
        raise ScorerError(
            STATUS_JUDGE_UNAVAILABLE, f"judge provider error: {stream_error}"
        )

    try:
        document = extract_json(collect_text(result.stdout or ""))
    except Exception as exc:
        raise ScorerError(
            STATUS_JUDGE_UNAVAILABLE, f"judge reply was not JSON: {exc}"
        ) from exc

    schema = load_json(Path(str(manifest.judge["output_schema"])))
    errors = schema_errors(document, schema)
    if errors:
        raise ScorerError(
            STATUS_JUDGE_UNAVAILABLE,
            "judge reply failed its declared output schema: " + "; ".join(errors[:5]),
        )

    results = _judge_results(document, criteria)
    missing = [component_id for component_id in criteria if component_id not in results]
    hard_missing = [
        component_id
        for component_id in missing
        if (manifest.component(component_id) or manifest.components[0]).hard_gate
    ]
    if hard_missing:
        raise ScorerError(
            STATUS_JUDGE_UNAVAILABLE,
            "judge did not score hard-gate criteria: " + ", ".join(hard_missing),
        )
    (config.run_dir / "judge-report.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return PhaseResult(
        components=results,
        warnings=[f"judge did not score {component_id}" for component_id in missing],
        detail={
            "model": model,
            "prompt_sha256": sha256_text(prompt),
            "criteria": criteria,
            "sandbox": sandbox_name,
        },
    )


# The trace an artifact run writes is a record of *who* ran as much as *what*
# happened: it carries the agent id, the host workspace path, input/output
# hashes, run status, and the model's own stderr. A scorer that can read any of
# that can be steered by the candidate's identity, which is exactly what the
# information boundary forbids. So only tool and timing evidence survives, and
# only field by field.
TRACE_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "agent.tool_call": ("server", "tool", "status", "duration_ms"),
}


def redact_trace(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Keep the allowed tool/timing evidence from an artifact-run trace.

    Allowlisted rather than filtered: a new event type added upstream must be
    reviewed before a scorer can see it, instead of leaking by default.
    """
    events: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        allowed = TRACE_EVENT_FIELDS.get(str(event.get("type")))
        if allowed is None:
            continue
        raw = event.get("data")
        payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
        events.append(
            {
                "type": str(event.get("type")),
                "timestamp": str(event.get("timestamp") or ""),
                "data": {key: payload[key] for key in allowed if key in payload},
            }
        )
    return events


def write_redacted_trace(source: Path, destination: Path) -> int:
    with Path(source).open("r", encoding="utf-8", errors="replace") as handle:
        events = redact_trace(handle)
    destination.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    return len(events)


def stage_inputs(
    manifest: ScorerConfig, config: ScorerRunConfig, staging: Path
) -> dict[str, str]:
    """Lay out the mounts the scorer contract promises, on the host."""
    for name in MOUNT_NAMES:
        (staging / name).mkdir(parents=True, exist_ok=True)
    materialize_candidate(config.candidate, staging / "candidate")
    _copy_package(manifest, staging / "scorer", staging / "fixtures")
    (staging / "fixtures" / ".keep").write_text("", encoding="utf-8")
    (staging / "output" / ".keep").write_text("", encoding="utf-8")

    shutil.copy2(config.task, staging / "input" / "task.json")
    wrapper_source = Path(__file__).with_name("scorer_wrapper.py")
    wrapper = staging / "input" / "ghostlab-scorer-wrapper.py"
    wrapper.write_text(
        f"#!{SYSTEM_PYTHON}\n" + wrapper_source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    shutil.copystat(wrapper_source, wrapper)
    wrapper.chmod(wrapper.stat().st_mode | 0o111)
    secure_exec = staging / "input" / "ghostlab-secure-exec"
    secure_exec.write_text(_secure_exec_script(SCORER_WRAPPER), encoding="utf-8")
    secure_exec.chmod(0o755)
    if config.trace and Path(config.trace).is_file():
        write_redacted_trace(Path(config.trace), staging / "input" / "aut-events.jsonl")
    if config.resources and Path(config.resources).is_file():
        shutil.copy2(config.resources, staging / "input" / "resources.json")
    return {"staging": str(staging)}


def run_scorer(
    config: ScorerRunConfig,
    *,
    sandbox_factory: Callable[..., OpenShellSandbox] = OpenShellSandbox,
) -> dict[str, Any]:
    """Score one candidate attempt and always write a schema-valid report."""
    run_dir = Path(config.run_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    output = Path(config.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    attempt_id = config.attempt_id or f"attempt-{int(time.time())}"
    task_id = ""
    hashes: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    isolation: dict[str, Any] = {}

    def publish(report: dict[str, Any]) -> dict[str, Any]:
        output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (run_dir / "scorer-run.json").write_text(
            json.dumps(
                {
                    "schema_version": "ghostlab-scorer-run-v1",
                    "task_id": report.get("task_id"),
                    "attempt_id": report.get("attempt_id"),
                    "status": report.get("status"),
                    "hashes": hashes,
                    **({"isolation": isolation} if isolation else {}),
                    **diagnostics,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return report

    try:
        task = load_task(config.task)
        task_id = str(task.get("task_id"))
        try:
            manifest = load_scorer(config.scorer)
        except ConfigError as exc:
            raise ScorerError(STATUS_SCORER_ERROR, str(exc)) from exc
        if manifest.task_id != task_id:
            raise ScorerError(
                STATUS_SCORER_ERROR,
                f"scorer task_id {manifest.task_id!r} does not match task {task_id!r}",
            )

        escaping = external_symlinks(manifest.package_dir)
        if escaping:
            raise ScorerError(
                STATUS_SCORER_ERROR,
                "scorer package contains symlinks pointing outside the package: "
                + ", ".join(escaping),
            )
        computed = package_hash(manifest.package_dir)
        if manifest.package_sha256 and manifest.package_sha256 != computed:
            raise ScorerError(
                STATUS_SCORER_ERROR,
                "scorer package hash mismatch: manifest declares "
                f"{manifest.package_sha256} but the package hashes to {computed}",
            )
        hashes = {
            "scorer_package_sha256": computed,
            "task_sha256": sha256_path(config.task),
            "candidate_sha256": (
                sha256_path(config.candidate)
                if Path(config.candidate).is_file()
                else ""
            ),
            "image": str(manifest.runtime.get("image") or "base"),
            "seed": int(config.seed),
        }

        staging = run_dir / "scorer-staging"
        if staging.exists():
            shutil.rmtree(staging)
        stage_inputs(manifest, config, staging)
        score_input = build_score_input(manifest, config, attempt_id=attempt_id)
        (staging / "input" / "score-input.json").write_text(
            json.dumps(score_input, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        hashes["score_input_sha256"] = sha256_text(
            json.dumps(score_input, sort_keys=True, separators=(",", ":"))
        )
        if any(component.kind == "performance" for component in manifest.components):
            # A runtime ratio is only comparable against the machine that
            # produced it, so a performance component pins one.
            from .setup_runtime import environment_fingerprint

            hashes["machine_fingerprint"] = environment_fingerprint()

        phases: list[PhaseResult] = []
        deterministic_detail: dict[str, Any] = {}
        repeatability: dict[str, Any] = {}
        extra_warnings: list[str] = []
        if manifest.mode != "judge":
            runs = [
                run_deterministic_phase(
                    manifest,
                    config,
                    staging,
                    sandbox_factory=sandbox_factory,
                    index=index,
                    isolation=isolation,
                )
                for index in range(max(1, int(config.repeat)))
            ]
            phases.append(runs[0])
            deterministic_detail = runs[0].detail
            if len(runs) > 1:
                repeatability = _repeatability(manifest, runs)
                if not repeatability["deterministic_stable"]:
                    extra_warnings.append(
                        "deterministic components did not reproduce across "
                        f"{repeatability['runs']} runs: "
                        + ", ".join(repeatability["unstable_components"])
                    )

        judge_detail: dict[str, Any] | None = None
        if manifest.judge_criteria():
            summary = {
                "components": [
                    {"id": key, "value": value.get("value")}
                    for phase in phases
                    for key, value in phase.components.items()
                ]
            }
            judge = run_judge_phase(
                manifest,
                config,
                staging,
                summary,
                sandbox_factory=sandbox_factory,
                isolation=isolation,
            )
            phases.append(judge)
            judge_detail = judge.detail
            hashes["judge_model"] = judge.detail.get("model", "")
            hashes["judge_prompt_sha256"] = judge.detail.get("prompt_sha256", "")

        report = compose_report(
            manifest,
            attempt_id=attempt_id,
            status=STATUS_SCORED,
            phases=phases,
            hashes=hashes,
            duration_ms=int((time.monotonic() - started) * 1000),
            judge=judge_detail,
            warnings=extra_warnings,
            repeatability=repeatability or None,
        )
        diagnostics["deterministic"] = deterministic_detail
        if repeatability:
            diagnostics["repeatability"] = repeatability
        return publish(report)
    except ScorerError as exc:
        diagnostics["error"] = {"status": exc.status, "detail": exc.detail}
        return publish(
            error_report(
                task_id=task_id,
                attempt_id=attempt_id,
                status=exc.status,
                detail=exc.detail,
                duration_ms=int((time.monotonic() - started) * 1000),
                hashes=hashes,
            )
        )
    except Exception as exc:  # noqa: BLE001 — a run without a report has no status at all
        detail = (
            str(exc)
            if isinstance(
                exc, (ConfigError, OSError, SandboxError, json.JSONDecodeError)
            )
            else f"{type(exc).__name__}: {exc}"
        )
        diagnostics["error"] = {"status": STATUS_SCORER_ERROR, "detail": detail}
        return publish(
            error_report(
                task_id=task_id,
                attempt_id=attempt_id,
                status=STATUS_SCORER_ERROR,
                detail=detail,
                duration_ms=int((time.monotonic() - started) * 1000),
                hashes=hashes,
            )
        )
