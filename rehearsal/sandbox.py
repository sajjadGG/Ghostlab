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


@dataclass
class OpenShellSandbox:
    config: dict[str, Any]
    role: str = "agent"
    run: RunFn = subprocess.run
    name: str = field(init=False)
    created: bool = field(default=False, init=False)

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
        try:
            result = self.run(
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
        for upload in self.config.get("uploads", []):
            source = Path(upload["source"])
            if not source.exists():
                raise SandboxError("sandbox_upload_missing", str(source))
            command += ["--upload", f"{source}:{upload['target']}"]
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

    def capture_logs(self) -> str:
        if not self.created:
            return ""
        result = self._call([self.binary, "logs", self.name, "--since", "1h"], timeout=30)
        return "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)

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
        if not self.config.get("keep"):
            self._call([self.binary, "sandbox", "delete", self.name], timeout=60)
        self.created = False


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
    sandbox = OpenShellSandbox(runtime_config, role=role)
    sandbox.create()
    connection = dict(target.connection)
    uploads = list(runtime_config.get("uploads", []) or [])

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

    raw_command = connection.get("command") or []
    command = [inside(raw_command)] if isinstance(raw_command, str) else [inside(part) for part in raw_command]
    command += [inside(part) for part in connection.get("args", [])]
    wrapped = [
        sandbox.binary, "sandbox", "exec", "-n", sandbox.name, "--no-tty",
        "--timeout", str(int(target.startup.get("timeout_seconds", 300))),
        "--workdir", str(runtime_config.get("workdir") or "/sandbox"),
    ]
    for key, value in sorted(sandbox.allowed_env(dict(connection.get("env", {}))).items()):
        wrapped += ["--env", f"{key}={value}"]
    wrapped += ["--", *command]
    rewritten = TargetConfig(
        id=target.id, transport="stdio",
        connection={"command": wrapped, "args": [], "env": {}},
        capabilities=target.capabilities, startup=target.startup,
    )
    return rewritten, sandbox
