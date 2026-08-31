"""A stand-in for the OpenShell CLI at the subprocess boundary.

Ghostlab shells out to `openshell`; these tests replace that binary, not the
sandbox abstraction. Every sandbox becomes a directory on the host, uploads and
downloads are copies, and `sandbox exec` really runs the command with its
sandbox paths rewritten into that directory. That keeps the code under test —
lifecycle, uploads, the pre-close export, the scorer entrypoint — running for
real, while the only thing faked is the runtime Ghostlab does not own.

Commands executed inside a fake sandbox get ``GHOSTLAB_FAKE_SANDBOX_ROOT`` so a
fixture script can resolve the absolute contract paths it is handed.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Absolute roots the scorer and artifact-run contracts pin. Anything starting
# with one of these is a sandbox path and gets rewritten into the fake root.
MOUNT_PREFIXES = (
    "/sandbox", "/candidate", "/scorer", "/fixtures", "/input", "/output", "/tmp/ghostlab",
)


class FakeOpenShell:
    """Callable drop-in for ``subprocess.run`` that speaks the OpenShell CLI."""

    def __init__(self, base: Path) -> None:
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.calls: list[list[str]] = []
        self.roots: dict[str, Path] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.uploads: dict[str, list[tuple[str, str]]] = {}
        self.execs: list[tuple[str, list[str]]] = []
        # name -> handler(argv, input_text, root) -> CompletedProcess | None
        self.exec_hook: Callable[..., Any] | None = None
        self.timeout_on: str = ""
        self.fail_download: str = ""
        self.fail_create: str = ""

    # -- helpers ----------------------------------------------------------- #
    def root(self, name: str) -> Path:
        return self.roots.setdefault(name, self.base / name)

    def inside(self, name: str, remote: str) -> Path:
        # A deleted sandbox keeps its tree under `deleted/` so a test can still
        # assert on what was (and was not) mounted after teardown.
        root = self.root(name)
        if not root.exists():
            archived = self.base / "deleted" / name
            if archived.exists():
                root = archived
        text = str(remote)
        if text.startswith("/"):
            return root / text.lstrip("/")
        return root / text

    def translate(self, name: str, value: str) -> str:
        text = str(value)
        if any(text == prefix or text.startswith(prefix + "/") for prefix in MOUNT_PREFIXES):
            return str(self.inside(name, text))
        return text

    def read(self, name: str, remote: str) -> str:
        return self.inside(name, remote).read_text(encoding="utf-8")

    def exists(self, name: str, remote: str) -> bool:
        return self.inside(name, remote).exists()

    # -- CLI emulation ----------------------------------------------------- #
    def __call__(self, command, *, input=None, timeout=None, **kwargs):
        argv = [str(part) for part in command]
        self.calls.append(argv)
        verb = argv[1:3]
        if verb == ["sandbox", "create"]:
            return self._create(argv)
        if verb == ["sandbox", "exec"]:
            return self._exec(argv, input, timeout)
        if verb == ["sandbox", "upload"]:
            return self._upload(argv)
        if verb == ["sandbox", "download"]:
            return self._download(argv)
        if verb == ["sandbox", "delete"]:
            name = argv[3]
            self.deleted.append(name)
            live = self.root(name)
            if live.exists():
                archived = self.base / "deleted" / name
                shutil.rmtree(archived, ignore_errors=True)
                archived.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(live), str(archived))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if verb == ["policy", "set"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[1:2] == ["logs"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def _option(self, argv: list[str], flag: str) -> str:
        return argv[argv.index(flag) + 1] if flag in argv else ""

    def _create(self, argv: list[str]):
        name = self._option(argv, "--name")
        if self.fail_create:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=self.fail_create)
        root = self.root(name)
        root.mkdir(parents=True, exist_ok=True)
        self.created.append(name)
        uploads: list[tuple[str, str]] = []
        for index, part in enumerate(argv):
            if part != "--upload":
                continue
            source, _, target = argv[index + 1].rpartition(":")
            uploads.append((source, target))
            destination = self.inside(name, target) / Path(source).name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if Path(source).is_dir():
                shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
        self.uploads[name] = uploads
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def _exec(self, argv: list[str], input_text, timeout):
        name = self._option(argv, "-n")
        inner = argv[argv.index("--") + 1:]
        self.execs.append((name, list(inner)))
        if self.timeout_on and any(self.timeout_on in part for part in inner):
            raise subprocess.TimeoutExpired(argv, timeout or 1)
        if self.exec_hook is not None:
            handled = self.exec_hook(name, list(inner), input_text, self.root(name))
            if handled is not None:
                return handled

        workdir = self._option(argv, "--workdir") or "/sandbox"
        cwd = self.inside(name, workdir)
        cwd.mkdir(parents=True, exist_ok=True)
        translated = [self.translate(name, part) for part in inner]
        if translated and translated[0] == "python3":
            translated[0] = sys.executable
        env = dict(os.environ)
        env["GHOSTLAB_FAKE_SANDBOX_ROOT"] = str(self.root(name))
        for index, part in enumerate(argv):
            if part == "--env":
                key, _, value = argv[index + 1].partition("=")
                env[key] = value
        try:
            completed = subprocess.run(
                translated, input=input_text, text=True, capture_output=True,
                cwd=str(cwd), env=env, check=False,
            )
        except OSError as exc:
            return subprocess.CompletedProcess(argv, 127, stdout="", stderr=str(exc))
        return subprocess.CompletedProcess(
            argv, completed.returncode, stdout=completed.stdout, stderr=completed.stderr
        )

    def _upload(self, argv: list[str]):
        name, source, destination = argv[3], argv[4], argv[5]
        target = self.inside(name, destination)
        if target.is_dir():
            target = target / Path(source).name
        target.parent.mkdir(parents=True, exist_ok=True)
        if Path(source).is_dir():
            shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def _download(self, argv: list[str]):
        name, source, destination = argv[3], argv[4], argv[5]
        if self.fail_download and self.fail_download in source:
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr=f"no such file: {source}"
            )
        origin = self.inside(name, source)
        if not origin.exists():
            return subprocess.CompletedProcess(
                argv, 1, stdout="", stderr=f"no such file: {source}"
            )
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(origin, target)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def opencode_stream(reply: str) -> str:
    """An ``opencode run --format json`` stream carrying one assistant reply."""
    import json

    return "\n".join(
        [
            json.dumps({"type": "text", "part": {"text": reply}}),
            json.dumps({"type": "finish"}),
        ]
    )


def opencode_error_stream(message: str) -> str:
    import json

    return json.dumps({"type": "error", "error": {"data": {"message": message}}})
