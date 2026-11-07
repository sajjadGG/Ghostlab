"""Where an agent runs: on this machine, or inside an NVIDIA OpenShell sandbox.

Both boundaries implement the same small interface (:class:`Box`), so the
session layer never cares which one it has:

* ``start()`` / ``close()`` — lifecycle; a box lives for one session.
* ``inside(path)`` — where a host path is visible to the agent.
* ``run(argv, stdin=, env=, timeout=)`` — one harness invocation.
* ``fetch(path, dest)`` — copy a workspace file out before teardown.

The workspace is always a copy, so parallel items never share edits and the
caller's directory is never touched. OpenShell is the default; ``local`` must
be asked for, and an OpenShell failure never falls back to it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

SANDBOX_ROOT = "/sandbox"
MCP_ROOT = "/sandbox/mcp"
AGENT_ROOT = "/sandbox/agent"


class SandboxError(RuntimeError):
    """A classified sandbox lifecycle failure (never the agent's fault)."""

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}")


@dataclass(frozen=True)
class Sandbox:
    """The execution boundary declared on an agent (``sandbox:`` in its config)."""

    backend: str = "openshell"  # openshell | local
    image: str = "base"
    providers: tuple[str, ...] = ()
    policy: Path | None = None
    env_allowlist: tuple[str, ...] = ()
    uploads: tuple[tuple[Path, str], ...] = ()  # (host path, sandbox dir) at create time
    files: Mapping[str, Path] = field(default_factory=dict)  # sandbox path -> host file, mode 600
    cpu: str = ""
    memory: str = ""
    keep: bool = False
    bin: str = ""
    startup_timeout: int = 300

    def __post_init__(self) -> None:
        if self.backend not in ("openshell", "local"):
            raise ValueError(f"unsupported sandbox backend {self.backend!r}; use openshell or local")
        for _, target in self.uploads:
            if not (target == SANDBOX_ROOT or target.startswith(SANDBOX_ROOT + "/")):
                raise ValueError(f"upload targets must be under {SANDBOX_ROOT}: {target!r}")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | str, base: Path) -> "Sandbox":
        if isinstance(data, str):
            data = {"backend": data}
        data = dict(data)

        def path(value: Any) -> Path:
            candidate = Path(str(value)).expanduser()
            return candidate if candidate.is_absolute() else (base / candidate).resolve()

        if data.get("policy"):
            data["policy"] = path(data["policy"])
        for key in ("providers", "env_allowlist"):
            if key in data:
                data[key] = tuple(str(v) for v in data[key] or ())
        if "uploads" in data:
            data["uploads"] = tuple(
                (path(u["source"]), str(u.get("target") or SANDBOX_ROOT)) for u in data["uploads"]
            )
        if "files" in data:
            data["files"] = {str(remote): path(local) for remote, local in data["files"].items()}
        for key in ("cpu", "memory"):
            if key in data:
                data[key] = str(data[key])
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown sandbox field(s): {', '.join(unknown)}")
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend, "image": self.image, "providers": list(self.providers),
            "policy": str(self.policy) if self.policy else None,
            "env_allowlist": list(self.env_allowlist),
            "uploads": [{"source": str(s), "target": t} for s, t in self.uploads],
            "files": {remote: str(local) for remote, local in self.files.items()},
            "cpu": self.cpu, "memory": self.memory, "keep": self.keep,
        }


@dataclass
class Completed:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class Box(Protocol):
    workdir: str

    def start(self) -> None: ...
    def inside(self, host_path: Any) -> str: ...
    def run(self, argv: Sequence[str], *, stdin: str | None, env: Mapping[str, str],
            timeout: int) -> Completed: ...
    def fetch(self, path: str, dest: Path) -> None: ...
    def close(self) -> None: ...


def _text(value: Any) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")


def _subprocess(argv: Sequence[str], *, stdin: str | None, env: Mapping[str, str] | None,
                cwd: str | None, timeout: int) -> Completed:
    try:
        done = subprocess.run(
            list(argv), input=stdin, text=True, capture_output=True, env=env,
            cwd=cwd, timeout=timeout, check=False,
            **({} if stdin is not None else {"stdin": subprocess.DEVNULL}),
        )
    except subprocess.TimeoutExpired as exc:
        return Completed(124, _text(exc.stdout), _text(exc.stderr), timed_out=True)
    except FileNotFoundError as exc:
        return Completed(127, "", f"harness binary not found: {exc.filename or exc}")
    return Completed(done.returncode, done.stdout, done.stderr)


def _copy_workspace(source: Path | None, dest: Path) -> None:
    if dest.exists():
        return  # a resumed session keeps the edits it already made
    if source is None:
        dest.mkdir(parents=True)
    else:
        shutil.copytree(source, dest, symlinks=True)


class LocalBox:
    """Runs the harness on the host, in a private copy of the workspace."""

    def __init__(self, sandbox: Sandbox, workspace: Path | None, run_dir: Path) -> None:
        self.sandbox = sandbox
        self.source = workspace
        self.path = run_dir / "workspace"
        self.workdir = str(self.path)

    def start(self) -> None:
        _copy_workspace(self.source, self.path)

    def inside(self, host_path: Any) -> str:
        return str(host_path)

    def run(self, argv: Sequence[str], *, stdin: str | None, env: Mapping[str, str],
            timeout: int) -> Completed:
        merged = {**os.environ, **env}
        return _subprocess(argv, stdin=stdin, env=merged, cwd=self.workdir, timeout=timeout)

    def fetch(self, path: str, dest: Path) -> None:
        source = self.path / path
        if not source.exists():
            raise SandboxError("sandbox_download_failed", f"no such file in workspace: {path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, dest)

    def close(self) -> None:
        pass


# --- OpenShell ------------------------------------------------------------- #

# Files that mark the root of an installed program: `build/index.js` needs the
# `node_modules/` beside `package.json`, one level up.
PROJECT_MARKERS = (
    "package.json", "node_modules", "pyproject.toml", "requirements.txt",
    "setup.py", ".venv", "venv", "go.mod", "Cargo.toml", "deno.json",
)
# Language runtimes come from the image, never from the host.
_INTERPRETERS = (
    "node", "nodejs", "npx", "bun", "deno", "python", "python2", "python3",
    "uv", "uvx", "ruby", "sh", "bash", "zsh", "env", "java", "dotnet",
)


def program_root(path: Path) -> Path:
    """The directory to upload so ``path`` can run: the nearest project root
    within two levels, else its own directory — never home or ``/``."""
    start = (path.parent if path.is_file() else path).resolve()
    stop = {Path("/"), Path.home().resolve(), Path("/tmp"), Path("/private/tmp")}
    candidate = start
    for _ in range(2):
        if candidate in stop or candidate.parent == candidate:
            break
        if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
            return candidate
        candidate = candidate.parent
    return start


def _is_interpreter(path: Path) -> bool:
    name = path.name.lower()
    return name in _INTERPRETERS or any(
        name.startswith(n) and name[len(n):].replace(".", "").rstrip("m").isdigit()
        for n in ("python", "node")
    )


def program_uploads(command: Sequence[str]) -> list[Path]:
    """Host directories a stdio MCP command needs inside the sandbox."""
    roots: list[Path] = []
    for part in command:
        if not str(part).startswith(("/", "~")):
            continue
        path = Path(str(part)).expanduser()
        if not path.exists() or (path.is_file() and _is_interpreter(path)):
            continue
        root = program_root(path)
        if root not in roots and root not in (Path("/"), Path.home().resolve()):
            roots.append(root)
    return roots


def render_egress_policy(hosts: Sequence[str], binaries: Sequence[str]) -> str:
    """A least-privilege OpenShell policy allowing only ``hosts`` on 443."""
    endpoints = "\n".join(f"      - host: {h}\n        port: 443" for h in dict.fromkeys(hosts))
    paths = "\n".join(f"      - path: {b}" for b in binaries)
    return (
        "# Generated by Ghostlab: egress for the agent's model provider only.\n"
        "version: 1\n\n"
        "filesystem_policy:\n"
        "  include_workdir: true\n"
        "  read_only: [/usr, /lib, /proc, /dev/urandom, /app, /etc, /var/log]\n"
        "  read_write: [/sandbox, /tmp, /dev/null, /opt/agent]\n\n"
        "landlock:\n"
        "  compatibility: best_effort\n\n"
        "network_policies:\n"
        "  agent_provider:\n"
        "    name: agent-provider\n"
        "    binaries:\n"
        f"{paths}\n"
        "    endpoints:\n"
        f"{endpoints}\n"
    )


def _classify(detail: str) -> str:
    lowered = detail.lower()
    if "gateway" in lowered or "connection refused" in lowered:
        return "sandbox_gateway_unavailable"
    if "policy" in lowered:
        return "sandbox_policy_invalid"
    if "image" in lowered or "pull" in lowered or "manifest" in lowered:
        return "sandbox_image_unavailable"
    return "sandbox_setup_failed"


class OpenShellBox:
    """One OpenShell sandbox holding the harness CLI, its MCPs, and a workspace copy.

    Everything the agent references on the host is uploaded at create time —
    the workspace, stdio MCP programs, and any harness files — and
    :meth:`inside` maps each host path to where it landed.
    """

    def __init__(
        self, sandbox: Sandbox, workspace: Path | None, run_dir: Path, *,
        mcp_commands: Sequence[Sequence[str]] = (), agent_files: Sequence[Path] = (),
        egress: tuple[Sequence[str], Sequence[str]] | None = None, name: str = "ghostlab",
    ) -> None:
        self.sandbox = sandbox
        self.run_dir = run_dir
        self.name = f"{re.sub(r'[^a-z0-9-]+', '-', name.lower()).strip('-') or 'ghostlab'}-{uuid.uuid4().hex[:8]}"
        self.created = False
        uploads: list[tuple[Path, str]] = list(sandbox.uploads)
        self.workdir = SANDBOX_ROOT
        if workspace is not None:
            uploads.append((workspace.resolve(), SANDBOX_ROOT))
            self.workdir = f"{SANDBOX_ROOT}/{workspace.resolve().name}"
        for command in mcp_commands:
            uploads += [(root, MCP_ROOT) for root in program_uploads(command)
                        if not self._covered(root, uploads)]
        for path in agent_files:
            folder = (path if path.is_dir() else path.parent).resolve()
            if not self._covered(folder, uploads):
                uploads.append((folder, AGENT_ROOT))
        self.uploads = list(dict.fromkeys(uploads))
        self.policy = sandbox.policy
        if self.policy is None and egress is not None:
            self.policy = run_dir / "egress-policy.yaml"
            run_dir.mkdir(parents=True, exist_ok=True)
            self.policy.write_text(render_egress_policy(*egress), encoding="utf-8")

    @staticmethod
    def _covered(path: Path, uploads: Sequence[tuple[Path, str]]) -> bool:
        return any(path.resolve().is_relative_to(source.resolve()) for source, _ in uploads)

    @property
    def binary(self) -> str:
        found = self.sandbox.bin or shutil.which("openshell")
        if not found:
            raise SandboxError(
                "sandbox_runtime_missing",
                "NVIDIA OpenShell CLI not found; install it and run `openshell status`, "
                "or set sandbox.backend: local to run unsandboxed on this machine",
            )
        return found

    def _call(self, argv: list[str], *, stdin: str | None = None, timeout: int = 120,
              check: bool = True) -> Completed:
        done = _subprocess(argv, stdin=stdin, env=None, cwd=None, timeout=timeout)
        if done.timed_out:
            raise SandboxError("sandbox_timeout", f"timed out: {' '.join(argv[:4])}")
        if check and done.exit_code != 0:
            detail = (done.stderr or done.stdout or "unknown OpenShell error").strip()[-2000:]
            raise SandboxError(_classify(detail), detail)
        return done

    def inside(self, host_path: Any) -> str:
        text = str(host_path)
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            return text
        for source, target in self.uploads:
            source = source.resolve()
            if candidate.resolve().is_relative_to(source):
                relative = candidate.resolve().relative_to(source)
                root = f"{target.rstrip('/')}/{source.name}"
                return root if str(relative) == "." else f"{root}/{relative.as_posix()}"
        return text

    def start(self) -> None:
        if self.created:
            return
        argv = [self.binary, "sandbox", "create", "--name", self.name, "--from", self.sandbox.image]
        for provider in self.sandbox.providers:
            argv += ["--provider", provider]
        if not self.sandbox.providers:
            argv.append("--no-auto-providers")
        if self.sandbox.cpu:
            argv += ["--cpu", self.sandbox.cpu]
        if self.sandbox.memory:
            argv += ["--memory", self.sandbox.memory]
        if self.policy is not None:
            if not Path(self.policy).is_file():
                raise SandboxError("sandbox_policy_missing", str(self.policy))
            argv += ["--policy", str(self.policy)]
        for source, target in self.uploads:
            if not source.exists():
                raise SandboxError("sandbox_upload_missing", str(source))
            argv += ["--upload", f"{source}:{target}"]
        if self.uploads:
            # OpenShell applies .gitignore to uploads by default, which drops
            # exactly what a server needs at runtime (node_modules, .venv).
            argv.append("--no-git-ignore")
        argv += ["--", "/bin/true"]
        try:
            self._call(argv, timeout=self.sandbox.startup_timeout)
        except SandboxError:
            # Creation can fail after the gateway allocated the sandbox.
            self._call([self.binary, "sandbox", "delete", self.name], timeout=60, check=False)
            raise
        self.created = True
        try:
            for remote, local in self.sandbox.files.items():
                self._put(Path(local), remote)
        except SandboxError:
            self.close()
            raise

    def _put(self, local: Path, remote: str) -> None:
        if not local.is_file():
            raise SandboxError("sandbox_upload_missing", str(local))
        parent = remote.rsplit("/", 1)[0] or "/"
        self._exec(["/bin/mkdir", "-p", parent], stdin=None, env={}, timeout=30, check=True)
        self._call([self.binary, "sandbox", "upload", self.name, str(local), remote,
                    "--no-git-ignore"])
        self._exec(["/bin/chmod", "600", remote], stdin=None, env={}, timeout=30, check=True)

    def _exec(self, argv: Sequence[str], *, stdin: str | None, env: Mapping[str, str],
              timeout: int, check: bool = False) -> Completed:
        call = [self.binary, "sandbox", "exec", "-n", self.name, "--no-tty",
                "--timeout", str(timeout), "--workdir", self.workdir]
        forwarded = {name: os.environ[name] for name in self.sandbox.env_allowlist
                     if name in os.environ}
        for key, value in sorted({**forwarded, **env}.items()):
            call += ["--env", f"{key}={value}"]
        done = _subprocess([*call, "--", *argv], stdin=stdin, env=None, cwd=None,
                           timeout=timeout + 30)
        if check and done.exit_code != 0:
            raise SandboxError("sandbox_setup_failed", (done.stderr or done.stdout).strip()[-500:])
        return done

    def run(self, argv: Sequence[str], *, stdin: str | None, env: Mapping[str, str],
            timeout: int) -> Completed:
        self.start()
        return self._exec(argv, stdin=stdin, env=env, timeout=timeout)

    def fetch(self, path: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        remote = path if path.startswith("/") else f"{self.workdir}/{path}"
        done = self._call([self.binary, "sandbox", "download", self.name, remote, str(dest)],
                          check=False)
        if done.exit_code != 0:
            raise SandboxError("sandbox_download_failed", (done.stderr or done.stdout).strip())

    def close(self) -> None:
        if not self.created:
            return
        self.created = False
        logs = self._call([self.binary, "logs", self.name, "--since", "1h"], timeout=30,
                          check=False)
        text = "\n".join(p for p in (logs.stdout.strip(), logs.stderr.strip()) if p)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "openshell.log").write_text(text + ("\n" if text else ""), encoding="utf-8")
        if not self.sandbox.keep:
            self._call([self.binary, "sandbox", "delete", self.name], timeout=60, check=False)
