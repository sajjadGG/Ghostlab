"""Sandbox runtime abstraction with NVIDIA OpenShell as the default backend.

Ghostlab deliberately shells out to the maintained OpenShell CLI instead of
reimplementing isolation. The gateway/supervisor own filesystem, process,
network, credential, and log enforcement; this module owns lifecycle and
normalization only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .session_provenance import CODEX_ORIGINATOR_ENV

DEFAULT_SANDBOX: dict[str, Any] = {
    "backend": "openshell",
    "image": "base",
    "workdir": "/sandbox",
    "network": "disabled",
    "env_allowlist": [],
    "providers": [],
    "uploads": [],
    "keep": False,
}


class SandboxError(RuntimeError):
    """A classified sandbox setup/lifecycle failure."""

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


def normalize_sandbox(
    raw: dict[str, Any] | None,
    base_dir: Path | None = None,
    *,
    allow_roots: tuple[str, ...] = ("/sandbox",),
) -> dict[str, Any]:
    config = {**DEFAULT_SANDBOX, **dict(raw or {})}
    backend = str(config.get("backend", "openshell"))
    if backend not in ("openshell", "local"):
        raise SandboxError("sandbox_config", f"unsupported backend {backend!r}")
    config["backend"] = backend
    network = str(config.get("network", "disabled"))
    if network not in ("disabled", "policy"):
        raise SandboxError(
            "sandbox_config",
            "network must be 'disabled' or 'policy'; OpenShell requires explicit egress policy",
        )
    config["network"] = network
    allowlist = config.get("env_allowlist", []) or []
    if not isinstance(allowlist, list):
        raise SandboxError("sandbox_config", "env_allowlist must be a list")
    config["env_allowlist"] = [str(name) for name in allowlist]
    uploads = []
    for item in config.get("uploads", []) or []:
        if not isinstance(item, dict) or not item.get("source"):
            raise SandboxError("sandbox_config", "each upload needs source and optional target")
        source = Path(str(item["source"])).expanduser()
        if not source.is_absolute() and base_dir is not None:
            source = base_dir / source
        target = str(item.get("target") or f"/sandbox/workspace/{source.name}")
        if not any(
            target == root or target.startswith(f"{root.rstrip('/')}/") for root in allow_roots
        ):
            raise SandboxError(
                "sandbox_config",
                "upload targets must be under " + " or ".join(allow_roots),
            )
        uploads.append({"source": str(source.resolve()), "target": target})
    config["uploads"] = uploads
    policy = config.get("policy")
    if policy:
        path = Path(str(policy)).expanduser()
        if not path.is_absolute() and base_dir is not None:
            path = base_dir / path
        config["policy"] = str(path.resolve())
        config["network"] = "policy"
    if network == "policy" and not config.get("policy"):
        raise SandboxError("sandbox_config", "network: policy requires a policy file")
    return config


RunFn = Callable[..., subprocess.CompletedProcess[str]]


def _default_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Late-bound indirection to ``subprocess.run``.

    Binding the dataclass default directly to ``subprocess.run`` would capture
    the function object at class-definition time, leaving sandboxes created
    inside helpers impossible to intercept from a test.
    """
    return subprocess.run(command, **kwargs)


@dataclass
class OpenShellSandbox:
    config: dict[str, Any]
    role: str = "agent"
    # None => resolved to `_default_run` at call time, so helpers that build
    # their own sandboxes stay interceptable.
    run: RunFn | None = None
    name: str = field(init=False)
    created: bool = field(default=False, init=False)
    _ssh_config: Path | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        prefix = re.sub(r"[^a-z0-9-]+", "-", str(self.config.get("name") or "ghostlab").lower())
        self.name = f"{prefix.strip('-') or 'ghostlab'}-{self.role}-{uuid.uuid4().hex[:8]}"

    @property
    def binary(self) -> str:
        configured = str(self.config.get("bin") or "")
        binary = configured or shutil.which("openshell") or ""
        if not binary:
            raise SandboxError(
                "sandbox_runtime_missing",
                "NVIDIA OpenShell CLI not found; install it and run `openshell status`, "
                "or set sandbox.backend: local for explicit unsandboxed compatibility",
            )
        return binary

    def _call(
        self, command: list[str], *, input_text: str | None = None,
        timeout: int | None = None, check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        runner = self.run or _default_run
        try:
            result = runner(
                command, input=input_text, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxError("sandbox_timeout", f"command timed out: {' '.join(command[:4])}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown OpenShell error").strip()[-2000:]
            lowered = detail.lower()
            if "gateway" in lowered or "connection refused" in lowered:
                kind = "sandbox_gateway_unavailable"
            elif "policy" in lowered:
                kind = "sandbox_policy_invalid"
            elif "image" in lowered or "pull" in lowered or "manifest" in lowered:
                kind = "sandbox_image_unavailable"
            else:
                kind = "sandbox_setup_failed"
            raise SandboxError(kind, detail)
        return result

    def create(self) -> None:
        if self.created:
            return
        command = [
            # OpenShell keeps created sandboxes by default. Cleanup is owned by
            # close(); --no-keep would remove it after this bootstrap command.
            self.binary, "sandbox", "create", "--name", self.name,
            "--from", str(self.config.get("image") or "base"),
        ]
        providers = list(self.config.get("providers", []) or [])
        if providers:
            for provider in providers:
                command += ["--provider", str(provider)]
        else:
            command.append("--no-auto-providers")
        cpu = self.config.get("cpu")
        memory = self.config.get("memory")
        if cpu:
            command += ["--cpu", str(cpu)]
        if memory:
            command += ["--memory", str(memory)]
        policy = self.config.get("policy")
        if policy:
            if not Path(str(policy)).exists():
                raise SandboxError("sandbox_policy_missing", str(policy))
            if not self.config.get("policy_after_upload"):
                command += ["--policy", str(policy)]
        uploads = list(self.config.get("uploads", []) or [])
        for upload in uploads:
            source = Path(upload["source"])
            if not source.exists():
                raise SandboxError("sandbox_upload_missing", str(source))
            command += ["--upload", f"{source}:{upload['target']}"]
        # OpenShell applies .gitignore to --upload by default, which silently
        # drops exactly what a server needs at runtime (node_modules, .venv).
        # Set sandbox.respect_git_ignore: true to opt back into filtering.
        if uploads and not self.config.get("respect_git_ignore"):
            command.append("--no-git-ignore")
        command += ["--", "/bin/true"]
        try:
            self._call(command, timeout=int(self.config.get("startup_timeout", 300)), check=True)
        except SandboxError:
            # Creation can fail after the gateway allocated a sandbox. Best-effort
            # deletion prevents leaked workloads without masking the root cause.
            try:
                self._call([self.binary, "sandbox", "delete", self.name], timeout=60)
            except SandboxError:
                pass
            raise
        self.created = True
        if policy and self.config.get("policy_after_upload"):
            try:
                self._call(
                    [
                        self.binary,
                        "policy",
                        "set",
                        self.name,
                        "--policy",
                        str(policy),
                        "--wait",
                    ],
                    timeout=90,
                    check=True,
                )
            except SandboxError:
                self.close()
                raise

    def allowed_env(self, requested: dict[str, str]) -> dict[str, str]:
        allow = set(self.config.get("env_allowlist", []))
        requested = {
            **{name: os.environ[name] for name in allow if name in os.environ},
            **{
                name: os.path.expandvars(value)
                for name, value in requested.items()
            },
        }
        internal = {
            key
            for key in requested
            if key.startswith("REHEARSAL_")
            or key.startswith("GHOSTLAB_")
            or key == CODEX_ORIGINATOR_ENV
        }
        return {key: value for key, value in requested.items() if key in allow or key in internal}

    def exec(
        self, command: list[str], *, input_text: str | None,
        env: dict[str, str], timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        self.create()
        call = [
            self.binary, "sandbox", "exec", "-n", self.name, "--no-tty",
            "--timeout", str(timeout), "--workdir", str(self.config.get("workdir") or "/sandbox"),
        ]
        for key, value in sorted(self.allowed_env(env).items()):
            call += ["--env", f"{key}={value}"]
        call += ["--", *command]
        return self._call(call, input_text=input_text, timeout=timeout + 30)

    def ssh_config_path(self) -> Path:
        """Write (once) an SSH config for this sandbox and return its path.

        `sandbox exec` buffers stdin until EOF, so it cannot carry a persistent
        bidirectional session. The SSH channel does, which is what a long-lived
        stdio MCP needs.
        """
        if self._ssh_config is not None:
            return self._ssh_config
        result = self._call(
            [self.binary, "sandbox", "ssh-config", self.name], timeout=60, check=True
        )
        directory = Path(tempfile.mkdtemp(prefix=f"ghostlab-ssh-{self.role}-"))
        path = directory / "ssh_config"
        path.write_text(result.stdout, encoding="utf-8")
        self._ssh_config = path
        return path

    def ssh_command(
        self, command: list[str], *, env: dict[str, str], workdir: str = "",
        base_env: dict[str, str] | None = None,
    ) -> list[str]:
        """Build an `ssh` invocation that runs ``command`` inside this sandbox.

        ``env`` is caller-supplied and filtered through the allowlist, so host
        secrets cannot leak in by accident. ``base_env`` is Ghostlab's own
        runtime wiring (data dirs and the like) and is applied verbatim — the
        allowlist exists to gate the user's environment, not ours.
        """
        import shlex

        remote = ""
        if workdir:
            remote += f"cd {shlex.quote(workdir)} && "
        allowed = {**dict(base_env or {}), **self.allowed_env(env)}
        if allowed:
            exports = " ".join(
                f"{key}={shlex.quote(value)}" for key, value in sorted(allowed.items())
            )
            remote += f"env {exports} "
        remote += " ".join(shlex.quote(str(part)) for part in command)
        return [
            "ssh", "-F", str(self.ssh_config_path()), "-T",
            f"openshell-{self.name}", remote,
        ]

    def capture_logs(self) -> str:
        if not self.created:
            return ""
        result = self._call([self.binary, "logs", self.name, "--since", "1h"], timeout=30)
        return "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)

    def upload_file(self, source: Path, destination: str, *, mode: str = "") -> None:
        """Copy one host file into the sandbox after creation.

        `--upload` at create time is restricted to `/sandbox`, which is the
        agent's own workspace. Credentials must land somewhere the agent cannot
        read or rewrite, so they are pushed separately to an absolute path.
        """
        source = Path(source).expanduser()
        if not source.exists():
            raise SandboxError("sandbox_upload_missing", str(source))
        remote = Path(destination)
        remote_parent = remote.parent.as_posix()
        prepared = self.exec(
            ["/bin/mkdir", "-p", remote_parent],
            input_text=None,
            env={},
            timeout=30,
        )
        if prepared.returncode != 0:
            detail = (prepared.stderr or prepared.stdout or "mkdir failed").strip()
            raise SandboxError("sandbox_upload_failed", detail)
        self._call(
            [
                self.binary,
                "sandbox",
                "upload",
                self.name,
                str(source),
                destination,
                "--no-git-ignore",
            ],
            timeout=120, check=True,
        )
        verified = self.exec(
            ["/usr/bin/test", "-f", destination],
            input_text=None,
            env={},
            timeout=30,
        )
        if verified.returncode != 0:
            detail = (verified.stderr or verified.stdout or "uploaded file is missing").strip()
            raise SandboxError("sandbox_upload_failed", detail)
        if mode:
            changed = self.exec(
                ["/bin/sh", "-c", f"chmod {mode} {shlex_quote(destination)}"],
                input_text=None, env={}, timeout=30,
            )
            if changed.returncode != 0:
                detail = (changed.stderr or changed.stdout or "chmod failed").strip()
                raise SandboxError("sandbox_upload_failed", detail)

    def download(self, source: str, destination: Path) -> None:
        """Copy one sandbox artifact back to the host workspace."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = self._call(
            [self.binary, "sandbox", "download", self.name, source, str(destination)],
            timeout=120,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "download failed").strip()
            raise SandboxError("sandbox_download_failed", detail)

    def close(self) -> None:
        if not self.created:
            return
        logs = self.capture_logs()
        artifact_dir = self.config.get("artifact_dir")
        if artifact_dir:
            path = Path(str(artifact_dir)) / f"openshell-{self.role}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(logs + ("\n" if logs else ""), encoding="utf-8")
        if self._ssh_config is not None:
            shutil.rmtree(self._ssh_config.parent, ignore_errors=True)
            self._ssh_config = None
        if not self.config.get("keep"):
            self._call([self.binary, "sandbox", "delete", self.name], timeout=60)
        self.created = False


MCP_UPLOAD_ROOT = "/sandbox/mcp"

WORKSPACE_ARTIFACT_ROOT = "/sandbox/artifacts/workspace"
WORKSPACE_EXPORT_PYTHON = "/usr/bin/python3"
WORKSPACE_EXPORT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
WORKSPACE_RUNTIME_SUMMARY_PREFIX = "GHOSTLAB_WORKSPACE_RUNTIME "

_WORKSPACE_RUNTIME_CHECK = r"""
import argparse
import collections.abc
import gzip
import hashlib
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import typing

prefix = "GHOSTLAB_WORKSPACE_RUNTIME "
errors = []
resources = {os.path.realpath(sys.executable)}
declared = sys.argv[1]
if os.path.isabs(declared):
    resources.add(declared)
    resources.add(os.path.realpath(declared))
library_roots = set()
for entry in sys.path:
    if not entry or not os.path.isabs(entry):
        continue
    candidate = os.path.realpath(entry)
    while not os.path.exists(candidate) and os.path.dirname(candidate) != candidate:
        candidate = os.path.dirname(candidate)
    library_roots.add(candidate)
    resources.add(candidate)
path_roots = set()
for entry in os.environ.get("PATH", "").split(os.pathsep):
    if not entry or not os.path.isabs(entry):
        errors.append(f"workspace exporter PATH entry is not absolute: {entry!r}")
        continue
    candidate = os.path.realpath(entry)
    while not os.path.exists(candidate) and os.path.dirname(candidate) != candidate:
        candidate = os.path.dirname(candidate)
    path_roots.add(candidate)
    resources.add(candidate)
zstd = shutil.which("zstd")
zstd_paths = set()
if zstd:
    if not os.path.isabs(zstd):
        errors.append(f"zstd did not resolve to an absolute path: {zstd!r}")
    else:
        zstd_paths.update((zstd, os.path.realpath(zstd)))
        resources.update(zstd_paths)
module_paths = set()
for module in tuple(sys.modules.values()):
    module_path = getattr(module, "__file__", None)
    if module_path and os.path.isabs(str(module_path)):
        module_paths.add(os.path.realpath(str(module_path)))
resources.update(module_paths)

checked = set(resources)
checked.update(os.path.dirname(path) for path in (declared, os.path.realpath(sys.executable)))
checked.update(os.path.dirname(path) for path in zstd_paths)
for resource in module_paths:
    candidate = os.path.dirname(resource)
    while candidate not in checked and candidate != os.path.dirname(candidate):
        checked.add(candidate)
        if candidate in library_roots:
            break
        candidate = os.path.dirname(candidate)
for candidate in sorted(checked):
    try:
        info = os.stat(candidate)
    except OSError as exc:
        errors.append(f"{candidate}: cannot stat ({exc})")
        continue
    if info.st_uid != 0:
        errors.append(f"{candidate}: uid {info.st_uid}, expected root (0)")
    if info.st_mode & 0o022:
        errors.append(f"{candidate}: group/world writable mode {info.st_mode & 0o777:o}")
    if os.access(candidate, os.W_OK):
        errors.append(f"{candidate}: writable by sandbox uid {os.geteuid()}")
if errors:
    print("workspace exporter runtime is untrusted: " + "; ".join(errors), file=sys.stderr)
    raise SystemExit(1)
print(prefix + json.dumps({
    "module_paths": sorted(module_paths),
    "module_roots": sorted(library_roots),
    "path_roots": sorted(path_roots),
    "paths": sorted(resources),
    "uid": os.geteuid(),
    "zstd": os.path.realpath(zstd) if zstd else "",
}))
"""


def _workspace_python_command(
    python: str,
    arguments: list[str],
    *,
    search_path: str = WORKSPACE_EXPORT_PATH,
) -> list[str]:
    bootstrap = (
        "import os,sys;"
        f"os.environ['PATH']={search_path!r};"
        "exec(compile(sys.stdin.read(),'<ghostlab-workspace-export>','exec'))"
    )
    return [python, "-I", "-S", "-c", bootstrap, *arguments]


def _load_sandbox_policy(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SandboxError(
            "sandbox_runtime_untrusted",
            f"cannot read sandbox policy {path}: {exc}",
        ) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            from .spec import parse_yaml

            loader = parse_yaml
        else:
            loader = yaml.safe_load
        try:
            document = loader(text)
        except Exception as exc:
            raise SandboxError(
                "sandbox_runtime_untrusted",
                f"cannot verify filesystem protections in sandbox policy {path}: {exc}",
            ) from exc
    if not isinstance(document, dict):
        raise SandboxError(
            "sandbox_runtime_untrusted",
            f"sandbox policy {path} is not a mapping",
        )
    return document


def _policy_paths(value: Any, *, field_name: str, policy: Path) -> list[PurePosixPath]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SandboxError(
            "sandbox_runtime_untrusted",
            f"sandbox policy {policy} must define filesystem_policy.{field_name} "
            "as a list of absolute paths",
        )
    paths = [PurePosixPath(item) for item in value]
    if any(not path.is_absolute() or ".." in path.parts for path in paths):
        raise SandboxError(
            "sandbox_runtime_untrusted",
            f"sandbox policy {policy} has an invalid filesystem_policy.{field_name} path",
        )
    return paths


def _contains_posix(root: PurePosixPath, path: PurePosixPath) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_workspace_export_policy(
    config: dict[str, Any],
    protected_paths: tuple[str, ...] = ("/usr",),
) -> None:
    """Reject an explicit policy that lets the agent rewrite the export runtime.

    OpenShell's built-in filesystem policy already mounts ``/usr`` and ``/lib``
    read-only. An explicit policy replaces that default, so it must retain
    read-only coverage for the interpreter and standard-library paths.
    """
    configured = config.get("policy")
    if not configured:
        return
    policy = Path(str(configured))
    document = _load_sandbox_policy(policy)
    filesystem = document.get("filesystem_policy")
    if not isinstance(filesystem, dict):
        raise SandboxError(
            "sandbox_runtime_untrusted",
            f"sandbox policy {policy} has no verifiable filesystem_policy",
        )
    read_only = _policy_paths(
        filesystem.get("read_only"), field_name="read_only", policy=policy
    )
    read_write = _policy_paths(
        filesystem.get("read_write"), field_name="read_write", policy=policy
    )
    for raw_path in protected_paths:
        path = PurePosixPath(raw_path)
        if not path.is_absolute() or not any(_contains_posix(root, path) for root in read_only):
            raise SandboxError(
                "sandbox_runtime_untrusted",
                f"sandbox policy {policy} does not keep {raw_path} read-only",
            )
        if any(
            _contains_posix(root, path) or _contains_posix(path, root)
            for root in read_write
        ):
            raise SandboxError(
                "sandbox_runtime_untrusted",
                f"sandbox policy {policy} grants write access overlapping {raw_path}",
            )


def verify_workspace_export_runtime(
    sandbox: OpenShellSandbox,
    *,
    python: str = WORKSPACE_EXPORT_PYTHON,
    search_path: str = WORKSPACE_EXPORT_PATH,
    timeout: int = 30,
) -> dict[str, Any]:
    """Prove the isolated exporter cannot be replaced by setup or agent code."""
    result = sandbox.exec(
        _workspace_python_command(python, [python], search_path=search_path),
        input_text=_WORKSPACE_RUNTIME_CHECK,
        env={},
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise SandboxError("sandbox_runtime_untrusted", detail[-2000:])
    payload: dict[str, Any] | None = None
    for line in (result.stdout or "").splitlines():
        if not line.startswith(WORKSPACE_RUNTIME_SUMMARY_PREFIX):
            continue
        try:
            candidate = json.loads(line[len(WORKSPACE_RUNTIME_SUMMARY_PREFIX) :])
        except json.JSONDecodeError as exc:
            raise SandboxError(
                "sandbox_runtime_untrusted",
                f"workspace runtime check returned invalid JSON: {exc}",
            ) from exc
        if isinstance(candidate, dict):
            payload = candidate
    if payload is None:
        raise SandboxError(
            "sandbox_runtime_untrusted",
            "workspace runtime check produced no verifiable path inventory",
        )
    paths = payload.get("paths")
    if (
        not isinstance(paths, list)
        or not paths
        or any(not isinstance(path, str) for path in paths)
    ):
        raise SandboxError(
            "sandbox_runtime_untrusted",
            "workspace runtime check produced no verifiable path inventory",
        )
    validate_workspace_export_policy(sandbox.config, tuple(paths))
    return payload


def _workspace_export_source() -> str:
    from . import workspace_export

    return Path(workspace_export.__file__).read_text(encoding="utf-8")


def fingerprint_workspace(
    sandbox: OpenShellSandbox,
    *,
    workdir: str,
    excludes: list[str] | None = None,
    retain: list[str] | None = None,
    timeout: int = 900,
) -> dict[str, Any]:
    """Compute the canonical workspace state inside a live sandbox."""
    from . import workspace_export

    if not sandbox.created:
        raise SandboxError("sandbox_export_failed", "sandbox is not running")
    command = _workspace_python_command(
        WORKSPACE_EXPORT_PYTHON,
        [
            "--root",
            workdir,
            "--hash-only",
        ],
    )
    for name in excludes or list(workspace_export.DEFAULT_EXCLUDES):
        command += ["--exclude", str(name)]
    for name in retain or []:
        command += ["--retain", str(name)]
    result = sandbox.exec(
        command,
        input_text=_workspace_export_source(),
        env={},
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "workspace fingerprint failed").strip()[-2000:]
        raise SandboxError("sandbox_export_failed", detail)
    for line in (result.stdout or "").splitlines():
        if line.startswith(workspace_export.SUMMARY_PREFIX):
            import json as _json

            return _json.loads(line[len(workspace_export.SUMMARY_PREFIX) :])
    raise SandboxError(
        "sandbox_export_failed",
        "workspace fingerprint produced no summary; "
        f"stdout tail: {(result.stdout or '').strip()[-500:]}",
    )


def export_workspace(
    sandbox: OpenShellSandbox,
    *,
    workdir: str,
    destination: Path,
    excludes: list[str] | None = None,
    retain: list[str] | None = None,
    archive_name: str = "state.tar.zst",
    timeout: int = 900,
) -> dict[str, Any]:
    """Canonically export ``workdir`` from a live sandbox into ``destination``.

    Runs :mod:`rehearsal.workspace_export` *inside* the sandbox — the same file
    the host uses to fingerprint the uploaded workspace — then brings the four
    artifacts back with the existing ``download``. Must be called before
    :meth:`OpenShellSandbox.close`, which deletes the workload.
    """
    from . import workspace_export

    if not sandbox.created:
        raise SandboxError("sandbox_export_failed", "sandbox is not running")
    if archive_name.endswith(".tar.zst") and not shutil.which("zstd"):
        archive_name = archive_name[: -len(".tar.zst")] + ".tar.gz"

    command = _workspace_python_command(
        WORKSPACE_EXPORT_PYTHON,
        [
            "--root",
            workdir,
            "--out",
            WORKSPACE_ARTIFACT_ROOT,
            "--archive-name",
            archive_name,
        ],
    )
    for name in excludes or list(workspace_export.DEFAULT_EXCLUDES):
        command += ["--exclude", str(name)]
    for name in retain or []:
        command += ["--retain", str(name)]

    result = sandbox.exec(
        command,
        input_text=_workspace_export_source(),
        env={},
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "workspace export failed").strip()[-2000:]
        raise SandboxError("sandbox_export_failed", detail)

    summary: dict[str, Any] = {}
    for line in (result.stdout or "").splitlines():
        if line.startswith(workspace_export.SUMMARY_PREFIX):
            import json as _json

            summary = _json.loads(line[len(workspace_export.SUMMARY_PREFIX):])
    if not summary:
        raise SandboxError(
            "sandbox_export_failed",
            "workspace exporter produced no summary; "
            f"stdout tail: {(result.stdout or '').strip()[-500:]}",
        )

    destination.mkdir(parents=True, exist_ok=True)
    downloaded: dict[str, str] = {}
    for name in ("status.json", "diff.patch", "untracked.json", str(summary["archive"])):
        local = destination / name
        sandbox.download(f"{WORKSPACE_ARTIFACT_ROOT}/{name}", local)
        if not local.exists():
            raise SandboxError("sandbox_download_failed", f"{name} was not written to {local}")
        downloaded[name] = str(local)
    verified = workspace_export.verify_export(
        Path(downloaded["status.json"]),
        Path(downloaded[str(summary["archive"])]),
    )
    if workspace_export.summary(verified) != summary:
        raise SandboxError(
            "sandbox_export_failed",
            "workspace exporter summary does not match downloaded artifacts",
        )
    return {**summary, "files": downloaded, "archive_path": downloaded[str(summary["archive"])]}


def _covered_by_uploads(path: Path, uploads: list[dict[str, Any]]) -> bool:
    for upload in uploads:
        try:
            path.resolve().relative_to(Path(str(upload["source"])).resolve())
        except ValueError:
            continue
        return True
    return False


# Files that mark the root of an installed program. Uploading the entry file's
# own directory is not enough: `build/index.js` needs the `node_modules/` that
# sits beside `package.json`, one level up.
PROJECT_MARKERS = (
    "package.json", "node_modules", "pyproject.toml", "requirements.txt",
    "setup.py", ".venv", "venv", "go.mod", "Cargo.toml", "deno.json",
)
# How far to walk up looking for a marker. Two levels covers the real layouts
# (`build/index.js`, `dist/server.js`, `src/main.py`) while keeping a standalone
# script from climbing out of its own directory into an enclosing monorepo —
# which would upload that whole repository instead of the server.
_MARKER_SEARCH_DEPTH = 2


def program_root(path: Path) -> Path:
    """The directory that must be uploaded for ``path`` to actually run.

    Walks up from the entry file to the nearest directory holding a project
    marker, so a server's dependencies travel with it. Falls back to the file's
    own directory when nothing identifiable is found, and never returns a home
    directory or filesystem root — uploading those would be catastrophic.
    """
    start = path.parent if path.is_file() else path
    stop = {Path("/"), Path.home().resolve(), Path("/tmp"), Path("/private/tmp")}
    candidate = start.resolve()
    for _ in range(_MARKER_SEARCH_DEPTH):
        if candidate in stop or candidate.parent == candidate:
            break
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
        candidate = candidate.parent
    return start.resolve()


# Language runtimes come from the sandbox image, not from the host. Uploading
# the host's copy would drag in whatever surrounds it — a `.venv` interpreter
# path would otherwise resolve to the entire enclosing repository.
_INTERPRETERS = (
    "node", "nodejs", "npx", "bun", "deno", "python", "python2", "python3",
    "uv", "uvx", "ruby", "sh", "bash", "zsh", "env", "java", "dotnet",
)


def _is_interpreter(path: Path) -> bool:
    stem = path.name.lower()
    if stem in _INTERPRETERS:
        return True
    # python3.13, node22, python3.11m ...
    return any(
        stem.startswith(name) and stem[len(name):].replace(".", "").rstrip("m").isdigit()
        for name in ("python", "node")
    )


def auto_uploads_for_command(
    parts: list[Any], uploads: list[dict[str, Any]], root: str = MCP_UPLOAD_ROOT
) -> list[dict[str, Any]]:
    """Uploads needed so a local stdio MCP's own program exists in the sandbox.

    A stdio MCP is launched by host path (``node /abs/path/server/index.js``).
    Without this the sandbox runs a command that does not exist inside the
    container and the server simply never speaks. The runtime itself is skipped
    — the image provides it — so only the server's own code travels.
    """
    additions: list[dict[str, Any]] = []
    seen = {str(Path(str(item["source"])).resolve()) for item in uploads}
    for part in parts:
        text = str(part)
        if not text.startswith("/") and not text.startswith("~"):
            continue
        path = Path(text).expanduser()
        if not path.exists() or _covered_by_uploads(path, uploads):
            continue
        if path.is_file() and _is_interpreter(path):
            continue
        source = program_root(path)
        key = str(source)
        if key in seen or source == Path("/") or source == Path.home().resolve():
            continue
        seen.add(key)
        additions.append({"source": key, "target": root})
    return additions


def preflight_stdio_command(sandbox: OpenShellSandbox, command: list[str]) -> None:
    """Verify a stdio MCP's program actually exists inside the sandbox.

    ``openshell sandbox exec`` holds stdout open while its stdin stays open, so a
    command that dies instantly produces no EOF — the MCP client would just wait
    out its timeout and report "server did not answer", blaming the MCP for what
    is really a missing file. Checking first turns that into a precise error.
    """
    if not command:
        return
    program = command[0]
    checks = [
        f"command -v {shlex_quote(program)} >/dev/null 2>&1 || "
        f"test -e {shlex_quote(program)} || echo MISSING:{program}"
    ]
    for part in command[1:]:
        if str(part).startswith("/"):
            checks.append(f"test -e {shlex_quote(str(part))} || echo MISSING:{part}")
    result = sandbox.exec(
        ["/bin/sh", "-c", "; ".join(checks)], input_text=None, env={}, timeout=60
    )
    missing = [
        line.split("MISSING:", 1)[1].strip()
        for line in (result.stdout or "").splitlines()
        if "MISSING:" in line
    ]
    if missing:
        raise SandboxError(
            "sandbox_command_missing",
            "the MCP server's program is not present inside the sandbox: "
            + ", ".join(missing)
            + ". Add it to sandbox.uploads, use an image that provides it, or run "
            "this target with --sandbox local (required for MCPs that must reach "
            "host-only resources such as macOS apps).",
        )


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(str(value))


def sandbox_stdio_target(
    target: Any, config: dict[str, Any], *, role: str, artifact_dir: Path | None = None,
) -> tuple[Any, OpenShellSandbox | None]:
    """Rewrite a stdio target so its persistent process runs inside OpenShell."""
    if target.transport != "stdio" or config.get("backend") != "openshell":
        return target, None
    from .config import TargetConfig

    runtime_config = {**config}
    if artifact_dir is not None:
        runtime_config["artifact_dir"] = str(artifact_dir)

    connection = dict(target.connection)
    raw_command = connection.get("command") or []
    command_parts = (
        [raw_command] if isinstance(raw_command, str) else list(raw_command)
    ) + list(connection.get("args", []))
    uploads = list(runtime_config.get("uploads", []) or [])
    # Uploads are applied at create time, so this must happen before create().
    uploads += auto_uploads_for_command(command_parts, uploads)
    runtime_config["uploads"] = uploads

    sandbox = OpenShellSandbox(runtime_config, role=role)
    sandbox.create()

    def inside(value: Any) -> str:
        text = str(value)
        path = Path(text).expanduser()
        if not path.is_absolute():
            return text
        for upload in uploads:
            source = Path(str(upload["source"])).resolve()
            try:
                relative = path.resolve().relative_to(source)
            except ValueError:
                continue
            remote_root = Path(str(upload["target"])) / source.name
            return str(remote_root / relative)
        return text

    command = (
        [inside(raw_command)]
        if isinstance(raw_command, str)
        else [inside(part) for part in raw_command]
    )
    command += [inside(part) for part in connection.get("args", [])]
    try:
        preflight_stdio_command(sandbox, command)
    except SandboxError:
        # The caller never receives the handle when this raises, so nothing else
        # can clean it up — tear it down here rather than leak a live workload.
        sandbox.close()
        raise
    # Deliberately SSH, not `sandbox exec`: exec buffers stdin until EOF, which
    # would deadlock the request/response loop of a persistent stdio MCP.
    wrapped = sandbox.ssh_command(
        command,
        env=dict(connection.get("env", {})),
        workdir=str(runtime_config.get("workdir") or "/sandbox"),
    )
    rewritten = TargetConfig(
        id=target.id, transport="stdio",
        connection={"command": wrapped, "args": [], "env": {}},
        capabilities=target.capabilities, startup=target.startup,
    )
    return rewritten, sandbox
