"""Setup runtime: execute a spec's `setup` section (roadmap Phase A2).

Turns the spec's declarative `setup` block into first-class primitives:

- **commands** — shell steps that prepare or start the target. Foreground
  commands must exit 0 before the next step runs; `background: true` commands
  (typically the server itself) stay alive until teardown and are killed then.
- **health** — readiness probes polled until they pass or time out:
  `http` (2xx/3xx from a URL), `tcp` (port accepts a connection), and
  `command` (exit 0).
- **reset** — state-restoration hooks run after anything mutated the target:
  `tool` (an MCP tool call, `optional: true` tolerated to fail) or `command`.
- **teardown** — cleanup commands, always attempted, plus termination of any
  background processes.

Everything a step prints is appended to `setup.log` in the runtime's log
directory, and `status()` returns a JSON-able report (per-command exit codes,
per-check health results, and an environment fingerprint) that discover/test
stages embed in their artifacts. Values in a command's `env` are resolved but
never logged, so secrets referenced via `env` don't leak into artifacts.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import __version__


class SetupError(RuntimeError):
    """Raised when a required setup step fails."""


def _as_argv(command: Any) -> list[str]:
    """Accept either a list argv or a shell-ish string."""
    if isinstance(command, list):
        return [str(part) for part in command]
    if isinstance(command, str) and command.strip():
        return shlex.split(command)
    raise SetupError(f"setup command must be a string or list, got: {command!r}")


def environment_fingerprint(server_info: Optional[dict] = None) -> dict[str, Any]:
    """Version fingerprint recorded with every setup/discover artifact."""
    import platform

    fingerprint = {
        "ghostlab_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
    }
    if server_info:
        fingerprint["server"] = {
            "name": server_info.get("name", "?"),
            "version": server_info.get("version", "?"),
        }
    return fingerprint


class SetupRuntime:
    """Runs one spec's `setup` section; use as a context manager.

    ``with SetupRuntime(spec.setup, log_dir) as runtime:`` starts the commands
    and guarantees teardown (including killing background processes) on exit.
    """

    def __init__(self, setup: dict[str, Any], log_dir: Path) -> None:
        self.setup = setup or {}
        self.log_dir = log_dir
        self.log_path = log_dir / "setup.log"
        self._background: list[tuple[str, subprocess.Popen]] = []
        self._command_results: list[dict[str, Any]] = []
        self._health_results: list[dict[str, Any]] = []
        self._reset_results: list[dict[str, Any]] = []
        self._teardown_results: list[dict[str, Any]] = []
        self._started = False
        # Remembered by write_status so a post-teardown rewrite keeps the
        # server fingerprint captured earlier in the run.
        self.server_info: Optional[dict] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "SetupRuntime":
        self.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.teardown()

    @property
    def declared(self) -> bool:
        """Whether the spec declares any setup work at all."""
        return bool(
            self.setup.get("commands")
            or self.setup.get("health")
            or self.setup.get("teardown")
        )

    def start(self) -> None:
        self._started = True
        for entry in self.setup.get("commands", []) or []:
            self._run_setup_command(entry)

    def _run_setup_command(self, entry: dict[str, Any]) -> None:
        command_id = str(entry.get("id") or entry.get("command", "?"))
        argv = _as_argv(entry.get("command"))
        env = {**os.environ, **{str(k): str(v) for k, v in (entry.get("env") or {}).items()}}
        cwd = entry.get("cwd") or None
        background = bool(entry.get("background", False))
        timeout = float(entry.get("timeout_seconds", 60))

        self._log(f"[command:{command_id}] {' '.join(argv)}" + (" (background)" if background else ""))
        self.log_dir.mkdir(parents=True, exist_ok=True)
        if background:
            log_handle = (self.log_dir / f"{_safe_name(command_id)}.log").open("ab")
            proc = subprocess.Popen(
                argv, env=env, cwd=cwd, stdout=log_handle, stderr=subprocess.STDOUT
            )
            log_handle.close()
            self._background.append((command_id, proc))
            self._command_results.append(
                {"id": command_id, "background": True, "pid": proc.pid, "ok": True}
            )
            return

        try:
            completed = subprocess.run(
                argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            self._command_results.append(
                {"id": command_id, "background": False, "ok": False, "error": "timeout"}
            )
            raise SetupError(f"setup command '{command_id}' timed out after {timeout:g}s") from exc
        except OSError as exc:
            self._command_results.append(
                {"id": command_id, "background": False, "ok": False, "error": str(exc)}
            )
            raise SetupError(f"setup command '{command_id}' failed to start: {exc}") from exc

        self._log_output(command_id, completed.stdout, completed.stderr)
        self._command_results.append(
            {
                "id": command_id,
                "background": False,
                "ok": completed.returncode == 0,
                "exit_code": completed.returncode,
            }
        )
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip()[-400:]
            raise SetupError(
                f"setup command '{command_id}' exited {completed.returncode}: {tail}"
            )

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #
    def wait_healthy(self) -> bool:
        """Poll every declared health check; returns True when all pass."""
        all_ok = True
        for check in self.setup.get("health", []) or []:
            result = self._wait_one(check)
            self._health_results.append(result)
            all_ok = all_ok and result["ok"]
        return all_ok

    def _wait_one(self, check: dict[str, Any]) -> dict[str, Any]:
        check_type = str(check.get("type", "http"))
        timeout = float(check.get("timeout_seconds", 30))
        interval = max(0.05, float(check.get("interval_seconds", 0.5)))
        label = check.get("url") or check.get("command") or (
            f"{check.get('host', '127.0.0.1')}:{check.get('port', '?')}"
        )
        deadline = time.monotonic() + timeout
        started = time.monotonic()
        error = ""
        while True:
            ok, error = self._probe(check_type, check)
            if ok or time.monotonic() >= deadline:
                break
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        elapsed = round(time.monotonic() - started, 3)
        self._log(
            f"[health:{check_type}] {label} -> {'ok' if ok else f'FAIL ({error})'} "
            f"after {elapsed}s"
        )
        result = {"type": check_type, "target": str(label), "ok": ok, "elapsed_seconds": elapsed}
        if not ok and error:
            result["error"] = error
        return result

    @staticmethod
    def _probe(check_type: str, check: dict[str, Any]) -> tuple[bool, str]:
        try:
            if check_type == "http":
                url = check.get("url")
                if not url:
                    return False, "http check needs a url"
                request = urllib.request.Request(str(url), method="GET")
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status < 400, f"status {response.status}"
            if check_type == "tcp":
                host = str(check.get("host", "127.0.0.1"))
                port = int(check.get("port", 0))
                with socket.create_connection((host, port), timeout=5):
                    return True, ""
            if check_type == "command":
                completed = subprocess.run(
                    _as_argv(check.get("command")), capture_output=True, timeout=15
                )
                return completed.returncode == 0, f"exit {completed.returncode}"
        except (urllib.error.URLError, OSError, subprocess.SubprocessError, ValueError) as exc:
            return False, str(exc)
        return False, f"unknown health check type: {check_type!r}"

    # ------------------------------------------------------------------ #
    # Reset
    # ------------------------------------------------------------------ #
    def run_reset(self, client: Any = None) -> bool:
        """Run reset hooks; `tool` hooks need an MCP ``client`` with call_tool."""
        all_ok = True
        for hook in self.setup.get("reset", []) or []:
            hook_type = str(hook.get("type", "command"))
            optional = bool(hook.get("optional", False))
            label = hook.get("name") or hook.get("command", "?")
            ok, error = True, ""
            try:
                if hook_type == "tool":
                    if client is None:
                        ok, error = False, "no MCP client available for tool reset"
                    else:
                        client.call_tool(str(hook.get("name")), dict(hook.get("arguments") or {}))
                elif hook_type == "command":
                    completed = subprocess.run(
                        _as_argv(hook.get("command")), capture_output=True, timeout=60
                    )
                    ok = completed.returncode == 0
                    error = "" if ok else f"exit {completed.returncode}"
                else:
                    ok, error = False, f"unknown reset type: {hook_type!r}"
            except Exception as exc:  # noqa: BLE001 — reported, optionally fatal
                ok, error = False, str(exc)
            self._log(f"[reset:{hook_type}] {label} -> {'ok' if ok else f'FAIL ({error})'}")
            self._reset_results.append(
                {"type": hook_type, "target": str(label), "ok": ok, "optional": optional,
                 **({"error": error} if error else {})}
            )
            if not ok and not optional:
                all_ok = False
        return all_ok

    # ------------------------------------------------------------------ #
    # Teardown
    # ------------------------------------------------------------------ #
    def teardown(self) -> None:
        if not self._started:
            return
        self._started = False
        for entry in self.setup.get("teardown", []) or []:
            label = str(entry.get("id") or entry.get("command", "?"))
            try:
                completed = subprocess.run(
                    _as_argv(entry.get("command")), capture_output=True, timeout=60
                )
                ok = completed.returncode == 0
                self._teardown_results.append(
                    {"id": label, "ok": ok, "exit_code": completed.returncode}
                )
                self._log(f"[teardown] {label} -> exit {completed.returncode}")
            except Exception as exc:  # noqa: BLE001 — teardown is best-effort
                self._teardown_results.append({"id": label, "ok": False, "error": str(exc)})
                self._log(f"[teardown] {label} -> FAIL ({exc})")
        for command_id, proc in self._background:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._log(f"[teardown] stopped background '{command_id}' (pid {proc.pid})")
        self._background.clear()

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #
    def status(self, server_info: Optional[dict] = None) -> dict[str, Any]:
        return {
            "declared": self.declared,
            "commands": self._command_results,
            "health": self._health_results,
            "reset": self._reset_results,
            "teardown": self._teardown_results,
            "fingerprint": environment_fingerprint(server_info),
        }

    def write_status(self, server_info: Optional[dict] = None) -> Path:
        if server_info is not None:
            self.server_info = server_info
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / "setup.json"
        path.write_text(
            json.dumps(self.status(self.server_info), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def _log(self, line: str) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H:%M:%S")
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {line}\n")

    def _log_output(self, command_id: str, stdout: str, stderr: str) -> None:
        for stream, text in (("stdout", stdout), ("stderr", stderr)):
            text = (text or "").strip()
            if text:
                self._log(f"[command:{command_id}] {stream}:\n{text}")


def _safe_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
