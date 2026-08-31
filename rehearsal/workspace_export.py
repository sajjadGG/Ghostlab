"""Canonical, filtered export of a workspace an agent was allowed to mutate.

The same module runs in two places, which is the whole point: Ghostlab imports
it on the host to fingerprint the workspace it is about to upload, and sends
the identical source over stdin to an isolated system Python interpreter in the
sandbox to fingerprint and archive the workspace the agent left behind. One
algorithm, two sides, so ``workspace_input_sha256`` and
``workspace_output_sha256`` are comparable rather than two independent guesses.

It therefore depends on nothing but the standard library and must stay that way.

Outputs (``--out``):

``status.json``     sorted relative paths with mode, byte size, and SHA-256,
                    plus the canonical ``state_sha256`` over that listing and
                    the exclusion set that produced it.
``diff.patch``      ``git diff HEAD`` when the root is a Git worktree.
``untracked.json``  untracked paths reported by ``git status --porcelain=v2``.
``state.tar.zst``   deterministic archive of exactly the listed files
                    (``state.tar.gz`` when no ``zstd`` binary exists).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
import threading
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = "ghostlab-workspace-state-v1"

# Directories that are build output, dependency caches, or version-control
# internals. They are reproducible from the tracked files and would otherwise
# dominate both the archive and the state hash.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".venv",
    "node_modules",
    "target",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
)

ARTIFACT_ROOT = "/sandbox/artifacts/workspace"
DEFAULT_ARCHIVE_NAME = "state.tar.zst"

# Printed on stdout so the host learns the produced archive name and state hash
# even before anything is downloaded.
SUMMARY_PREFIX = "GHOSTLAB_WORKSPACE_EXPORT "

_CHUNK = 1024 * 1024
MAX_VERIFIED_FILES = 100_000
MAX_VERIFIED_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_VERIFIED_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_VERIFIED_METADATA_BYTES = 1024 * 1024
MAX_VERIFIED_TOTAL_METADATA_BYTES = 16 * 1024 * 1024
MAX_STATUS_BYTES = 64 * 1024 * 1024
_MAX_ZSTD_STDERR_BYTES = 64 * 1024
_MAX_ZSTD_TRAILING_BYTES = 1024 * 1024
_TAR_METADATA_TYPES = frozenset(
    (
        tarfile.XHDTYPE,
        tarfile.XGLTYPE,
        tarfile.SOLARIS_XHDTYPE,
        tarfile.GNUTYPE_LONGNAME,
        tarfile.GNUTYPE_LONGLINK,
    )
)


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
            for block in iter(lambda: self._stream.read(_CHUNK), b""):
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
    """Bound extension records before ``tarfile`` reads or parses their payload."""

    def _proc_member(self, archive: tarfile.TarFile) -> tarfile.TarInfo | None:
        state: Any = archive
        header_count = int(getattr(state, "_ghostlab_header_count", 0)) + 1
        state._ghostlab_header_count = header_count
        if header_count > MAX_VERIFIED_FILES:
            raise ValueError("workspace archive exceeds the raw member-count limit")

        if self.type == tarfile.GNUTYPE_SPARSE:
            raise ValueError("unsupported workspace archive GNU sparse metadata")
        if self.type in _TAR_METADATA_TYPES:
            if self.size < 0 or self.size > MAX_VERIFIED_METADATA_BYTES:
                raise ValueError(
                    "workspace archive metadata exceeds the per-record size limit"
                )
            padded_size = (self.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
            padded_size *= tarfile.BLOCKSIZE
            metadata_bytes = (
                int(getattr(state, "_ghostlab_metadata_bytes", 0)) + padded_size
            )
            state._ghostlab_metadata_bytes = metadata_bytes
            if metadata_bytes > MAX_VERIFIED_TOTAL_METADATA_BYTES:
                raise ValueError(
                    "workspace archive metadata exceeds the total size limit"
                )
        return super()._proc_member(archive)  # type: ignore[misc]

    def _reject_gnu_sparse(self) -> None:
        raise ValueError("unsupported workspace archive GNU sparse metadata")

    def _proc_gnusparse_00(self, *_args: Any, **_kwargs: Any) -> None:
        self._reject_gnu_sparse()

    def _proc_gnusparse_01(self, *_args: Any, **_kwargs: Any) -> None:
        self._reject_gnu_sparse()

    def _proc_gnusparse_10(self, *_args: Any, **_kwargs: Any) -> None:
        self._reject_gnu_sparse()


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
        block = stream.read(min(_CHUNK, remaining))
        if not block:
            return
        remaining -= len(block)
    raise ValueError("zstd produced excessive data after the workspace archive")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _retained(relative: str, retain: frozenset[str]) -> bool:
    """Whether an explicitly retained path covers ``relative``."""
    if not retain:
        return False
    parts = PurePosixPath(relative).parts
    for index in range(1, len(parts) + 1):
        if "/".join(parts[:index]) in retain:
            return True
    return False


def _excluded(relative: str, excludes: frozenset[str], retain: frozenset[str]) -> bool:
    if _retained(relative, retain):
        return False
    return any(part in excludes for part in PurePosixPath(relative).parts)


def iter_entries(
    root: Path, excludes: Iterable[str] = (), retain: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Sorted status entries for every retained file under ``root``.

    Symlinks are recorded by target instead of being followed: following them
    would either duplicate content or escape the workspace entirely.
    """
    exclude_set = frozenset(excludes or DEFAULT_EXCLUDES)
    retain_set = frozenset(retain or ())
    entries: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        relative_dir = here.relative_to(root).as_posix()
        prefix = "" if relative_dir == "." else f"{relative_dir}/"
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not _excluded(f"{prefix}{name}", exclude_set, retain_set)
        )
        for name in sorted(filenames):
            relative = f"{prefix}{name}"
            if _excluded(relative, exclude_set, retain_set):
                continue
            path = here / name
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                entries.append(
                    {
                        "path": relative,
                        "kind": "symlink",
                        "mode": "0777",
                        "size": 0,
                        "sha256": "",
                        "target": os.readlink(path),
                    }
                )
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    # Only the executable bit is meaningful across hosts and
                    # container users; anything finer makes the hash unstable.
                    "mode": "0755" if info.st_mode & stat.S_IXUSR else "0644",
                    "size": int(info.st_size),
                    "sha256": sha256_file(path),
                }
            )
    entries.sort(key=lambda entry: entry["path"])
    return entries


def state_hash(
    entries: list[dict[str, Any]], excludes: Iterable[str] = (), retain: Iterable[str] = ()
) -> str:
    """Canonical hash of the retained file set and the rules that produced it.

    The exclusion set is part of the hash because two exports of the same
    directory under different project exclusions are different states.
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "excludes": sorted(set(excludes or DEFAULT_EXCLUDES)),
        "retain": sorted(set(retain or ())),
        "files": [
            {
                "path": entry["path"],
                "kind": entry.get("kind", "file"),
                "mode": entry["mode"],
                "size": entry["size"],
                "sha256": entry["sha256"],
                "target": entry.get("target", ""),
            }
            for entry in entries
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def status_document(
    root: Path, excludes: Iterable[str] = (), retain: Iterable[str] = ()
) -> dict[str, Any]:
    exclude_list = sorted(set(excludes or DEFAULT_EXCLUDES))
    retain_list = sorted(set(retain or ()))
    entries = iter_entries(root, exclude_list, retain_list)
    return {
        "schema_version": SCHEMA_VERSION,
        "root": root.name,
        "excludes": exclude_list,
        "retain": retain_list,
        "file_count": len(entries),
        "total_bytes": sum(int(entry["size"]) for entry in entries),
        "state_sha256": state_hash(entries, exclude_list, retain_list),
        "files": entries,
    }


def workspace_state_hash(
    root: Path, excludes: Iterable[str] = (), retain: Iterable[str] = ()
) -> str:
    """Canonical state hash of a directory on whichever side is asking."""
    exclude_list = sorted(set(excludes or DEFAULT_EXCLUDES))
    retain_list = sorted(set(retain or ()))
    return state_hash(iter_entries(root, exclude_list, retain_list), exclude_list, retain_list)


def verify_export(status_path: Path, archive_path: Path) -> dict[str, Any]:
    """Verify a downloaded archive against its canonical status document."""
    try:
        if Path(status_path).stat().st_size > MAX_STATUS_BYTES:
            raise ValueError("workspace status exceeds the size limit")
        status = json.loads(Path(status_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid workspace status: {exc}") from exc
    if not isinstance(status, dict) or status.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("invalid workspace status schema")
    expected_entries = status.get("files")
    if not isinstance(expected_entries, list):
        raise ValueError("workspace status has no file list")
    if len(expected_entries) > MAX_VERIFIED_FILES:
        raise ValueError("workspace status exceeds the file-count limit")
    declared_total = status.get("total_bytes")
    if (
        not isinstance(declared_total, int)
        or declared_total < 0
        or declared_total > MAX_VERIFIED_TOTAL_BYTES
    ):
        raise ValueError("workspace status has an invalid or excessive byte count")
    if status.get("archive_sha256") != sha256_file(Path(archive_path)):
        raise ValueError("workspace archive hash does not match status")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    process: subprocess.Popen[bytes] | None = None
    stderr_reader: _BoundedStderr | None = None
    process_reaped = False
    try:
        if Path(archive_path).name.endswith(".tar.zst"):
            process = subprocess.Popen(
                ["zstd", "-q", "-d", "-c", "--", str(archive_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if process.stdout is None or process.stderr is None:
                raise ValueError("zstd produced no output or error stream")
            stderr_reader = _BoundedStderr(process.stderr)
            archive = tarfile.open(
                fileobj=process.stdout,
                mode="r|",
                tarinfo=_PrevalidatedTarInfo,
            )
        else:
            archive = tarfile.open(
                archive_path,
                mode="r|*",
                tarinfo=_PrevalidatedTarInfo,
            )
        with archive:
            total_bytes = 0
            for member in archive:
                if len(entries) >= MAX_VERIFIED_FILES:
                    raise ValueError("workspace archive exceeds the file-count limit")
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or ".." in relative.parts or member.name in seen:
                    raise ValueError(f"unsafe workspace archive member: {member.name}")
                seen.add(member.name)
                if member.issym():
                    if member.size:
                        raise ValueError(
                            f"workspace archive symlink has data: {member.name}"
                        )
                    entries.append(
                        {
                            "path": member.name,
                            "kind": "symlink",
                            "mode": "0777",
                            "size": 0,
                            "sha256": "",
                            "target": member.linkname,
                        }
                    )
                    continue
                if not member.isfile():
                    raise ValueError(f"unsupported workspace archive member: {member.name}")
                if member.size < 0 or member.size > MAX_VERIFIED_MEMBER_BYTES:
                    raise ValueError(f"workspace archive member is too large: {member.name}")
                total_bytes += member.size
                if total_bytes > MAX_VERIFIED_TOTAL_BYTES:
                    raise ValueError("workspace archive exceeds the expanded-size limit")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError(f"workspace archive member has no data: {member.name}")
                digest = hashlib.sha256()
                size = 0
                with extracted:
                    while block := extracted.read(_CHUNK):
                        size += len(block)
                        digest.update(block)
                if size != member.size:
                    raise ValueError(
                        f"workspace archive member is truncated: {member.name}"
                    )
                entries.append(
                    {
                        "path": member.name,
                        "kind": "file",
                        "mode": "0755" if member.mode & stat.S_IXUSR else "0644",
                        "size": size,
                        "sha256": digest.hexdigest(),
                    }
                )
        if process is not None:
            if process.stdout is None:
                raise ValueError("zstd produced no output stream")
            _drain_zstd_stdout(process.stdout)
            returncode, stderr = _reap_zstd(process, stderr_reader, abort=False)
            process_reaped = True
            if returncode != 0:
                raise ValueError(
                    "zstd could not read "
                    f"{Path(archive_path).name}: {stderr.decode(errors='replace')[-500:]}"
                )
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError(f"invalid workspace archive: {exc}") from exc
    finally:
        if process is not None and not process_reaped:
            _reap_zstd(process, stderr_reader, abort=True)

    entries.sort(key=lambda entry: entry["path"])
    if entries != expected_entries:
        raise ValueError("workspace archive contents do not match status")
    excludes = status.get("excludes")
    retain = status.get("retain")
    if not isinstance(excludes, list) or not isinstance(retain, list):
        raise ValueError("workspace status has invalid filter lists")
    if status.get("state_sha256") != state_hash(entries, excludes, retain):
        raise ValueError("workspace state hash does not match archive contents")
    if status.get("file_count") != len(entries):
        raise ValueError("workspace file count does not match archive contents")
    if status.get("total_bytes") != sum(int(entry["size"]) for entry in entries):
        raise ValueError("workspace byte count does not match archive contents")
    return status


def _tar_info(entry: dict[str, Any]) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=entry["path"])
    # Owner, timestamp, and group are host facts, not workspace state. Zeroing
    # them is what makes two archives of the same tree byte-identical.
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    if entry.get("kind") == "symlink":
        info.type = tarfile.SYMTYPE
        info.linkname = str(entry.get("target", ""))
        info.mode = 0o777
        info.size = 0
        return info
    info.type = tarfile.REGTYPE
    info.mode = int(entry["mode"], 8)
    info.size = int(entry["size"])
    return info


def write_archive(root: Path, entries: list[dict[str, Any]], destination: Path) -> Path:
    """Write a deterministic archive of ``entries``; returns the actual path.

    The requested ``.tar.zst`` name is honoured only when a ``zstd`` binary is
    present. Otherwise the archive is written as ``.tar.gz`` under the real
    extension rather than a gzip stream wearing a zstd name.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    name = destination.name
    for suffix in (".tar.zst", ".tar.gz", ".tgz", ".tar"):
        if name.endswith(suffix):
            base = destination.parent / name[: -len(suffix)]
            break
    else:
        base = destination
    tar_path = base.with_name(base.name + ".tar")
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            info = _tar_info(entry)
            if info.type == tarfile.SYMTYPE:
                archive.addfile(info)
                continue
            with (root / entry["path"]).open("rb") as handle:
                archive.addfile(info, handle)

    if name.endswith(".tar"):
        return tar_path

    import shutil

    if name.endswith(".tar.zst") and shutil.which("zstd"):
        final = base.with_name(base.name + ".tar.zst")
        completed = subprocess.run(
            ["zstd", "-19", "-q", "-f", "--no-progress", "-o", str(final), str(tar_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            tar_path.unlink()
            return final

    import gzip

    final = base.with_name(base.name + ".tar.gz")
    with final.open("wb") as raw:
        # mtime=0 and an empty stored filename keep the gzip header from
        # embedding the moment and place the archive happened to be built.
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as target:
            with tar_path.open("rb") as source:
                for block in iter(lambda: source.read(_CHUNK), b""):
                    target.write(block)
    tar_path.unlink()
    return final


def _git(root: Path, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def git_state(root: Path) -> tuple[str, dict[str, Any]]:
    """``(diff.patch text, untracked.json document)`` for a Git worktree."""
    probe = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if probe is None or probe.returncode != 0 or probe.stdout.strip() != "true":
        return "", {"git": False, "untracked": [], "changed": []}

    diff = _git(root, ["diff", "HEAD", "--no-color", "--binary"])
    status = _git(root, ["status", "--porcelain=v2", "-z", "--untracked-files=all"])
    untracked: list[str] = []
    changed: list[str] = []
    if status is not None and status.returncode == 0:
        fields = [field for field in status.stdout.split("\0") if field]
        index = 0
        while index < len(fields):
            record = fields[index]
            index += 1
            if record.startswith("? "):
                untracked.append(record[2:])
            elif record.startswith("1 "):
                changed.append(record.split(" ", 8)[-1])
            elif record.startswith("2 "):
                # Rename/copy entries carry the source path in the next field.
                changed.append(record.split(" ", 9)[-1])
                index += 1
    head = _git(root, ["rev-parse", "HEAD"])
    return (
        diff.stdout if diff is not None and diff.returncode == 0 else "",
        {
            "git": True,
            "head": (head.stdout.strip() if head is not None and head.returncode == 0 else ""),
            "untracked": sorted(untracked),
            "changed": sorted(changed),
        },
    )


def export(
    root: Path,
    out_dir: Path,
    *,
    excludes: Iterable[str] = (),
    retain: Iterable[str] = (),
    archive_name: str = DEFAULT_ARCHIVE_NAME,
) -> dict[str, Any]:
    """Produce the four canonical artifacts and return the status document."""
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"workspace root does not exist: {root}")
    out_dir.mkdir(parents=True, exist_ok=True)

    status = status_document(root, excludes, retain)
    diff_text, untracked = git_state(root)

    archive = write_archive(root, status["files"], out_dir / archive_name)
    status["archive"] = archive.name
    status["archive_sha256"] = sha256_file(archive)
    status["archive_bytes"] = archive.stat().st_size
    status["git"] = bool(untracked.get("git"))

    (out_dir / "diff.patch").write_text(diff_text, encoding="utf-8")
    (out_dir / "untracked.json").write_text(
        json.dumps(untracked, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "status.json").write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return status


def summary(status: dict[str, Any]) -> dict[str, Any]:
    result = {
        "schema_version": status["schema_version"],
        "state_sha256": status["state_sha256"],
        "file_count": status["file_count"],
        "total_bytes": status["total_bytes"],
        "git": status.get("git", False),
    }
    for key in ("archive", "archive_sha256", "archive_bytes"):
        if key in status:
            result[key] = status[key]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical workspace export.")
    parser.add_argument("--root", required=True, help="Workspace directory to export.")
    parser.add_argument("--out", default=ARTIFACT_ROOT, help="Where to write the artifacts.")
    parser.add_argument(
        "--exclude", action="append", default=[],
        help="Additional excluded directory name, on top of the defaults (repeatable).",
    )
    parser.add_argument(
        "--retain",
        action="append",
        default=[],
        help="Relative path kept even when a default exclusion covers it (repeatable).",
    )
    parser.add_argument("--archive-name", default=DEFAULT_ARCHIVE_NAME)
    parser.add_argument(
        "--hash-only",
        action="store_true",
        help="Compute and print the canonical state hash without writing artifacts.",
    )
    args = parser.parse_args(argv)

    excludes = sorted(set(DEFAULT_EXCLUDES) | set(args.exclude))
    try:
        if args.hash_only:
            status = status_document(Path(args.root).resolve(), excludes, args.retain)
        else:
            status = export(
                Path(args.root),
                Path(args.out),
                excludes=excludes,
                retain=args.retain,
                archive_name=args.archive_name,
            )
    except (OSError, FileNotFoundError) as exc:
        print(f"workspace export failed: {exc}", file=sys.stderr)
        return 1
    print(SUMMARY_PREFIX + json.dumps(summary(status), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
