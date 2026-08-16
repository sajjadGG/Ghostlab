"""Sandbox runtime abstraction with NVIDIA OpenShell as the default backend.

Ghostlab deliberately shells out to the maintained OpenShell CLI instead of
reimplementing isolation. The gateway/supervisor own filesystem, process,
network, credential, and log enforcement; this module owns lifecycle and
normalization only.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


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


def normalize_sandbox(raw: dict[str, Any] | None, base_dir: Path | None = None) -> dict[str, Any]:
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
        if target != "/sandbox" and not target.startswith("/sandbox/"):
            raise SandboxError("sandbox_config", "upload targets must be under /sandbox")
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


def _default_run(command: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
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
    run: "RunFn | None" = None
    name: str = field(init=False)
    created: bool = field(default=False, init=False)
    _ssh_config: "Path | None" = field(default=None, init=False)

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

    def allowed_env(self, requested: dict[str, str]) -> dict[str, str]:
        allow = set(self.config.get("env_allowlist", []))
        requested = {
            **{name: os.environ[name] for name in allow if name in os.environ},
            **requested,
        }
        internal = {key for key in requested if key.startswith("REHEARSAL_") or key.startswith("GHOSTLAB_")}
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
        base_env: "dict[str, str] | None" = None,
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
        self._call(
            [self.binary, "sandbox", "upload", self.name, str(source), destination],
            timeout=120, check=True,
        )
        if mode:
            self.exec(
                ["/bin/sh", "-c", f"chmod {mode} {shlex_quote(destination)}"],
                input_text=None, env={}, timeout=30,
            )

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
# How far to walk up looking for a marker. Deep enough for `build/index.js` or
# `src/server/main.py`, shallow enough never to reach a home directory.
_MARKER_SEARCH_DEPTH = 4


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
