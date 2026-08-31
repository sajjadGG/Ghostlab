"""Apply a fail-closed Landlock policy before starting an untrusted scorer."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

LANDLOCK_CREATE_RULESET = 444
LANDLOCK_ADD_RULE = 445
LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

ACCESS_EXECUTE = 1 << 0
ACCESS_WRITE_FILE = 1 << 1
ACCESS_READ_FILE = 1 << 2
ACCESS_READ_DIR = 1 << 3
ACCESS_REMOVE_DIR = 1 << 4
ACCESS_REMOVE_FILE = 1 << 5
ACCESS_MAKE_CHAR = 1 << 6
ACCESS_MAKE_DIR = 1 << 7
ACCESS_MAKE_REG = 1 << 8
ACCESS_MAKE_SOCK = 1 << 9
ACCESS_MAKE_FIFO = 1 << 10
ACCESS_MAKE_BLOCK = 1 << 11
ACCESS_MAKE_SYM = 1 << 12
ACCESS_REFER = 1 << 13
ACCESS_TRUNCATE = 1 << 14


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _existing_paths(values: list[str]) -> list[str]:
    return [str(Path(value).resolve()) for value in values if Path(value).exists()]


def apply_landlock(*, read_only: list[str], read_write: list[str]) -> None:
    if sys.platform != "linux":
        raise RuntimeError("scorer isolation requires Linux Landlock")

    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(
        LANDLOCK_CREATE_RULESET,
        0,
        0,
        LANDLOCK_CREATE_RULESET_VERSION,
    )
    if abi < 3:
        raise RuntimeError(
            f"scorer isolation requires Landlock ABI 3 or newer, got {abi}"
        )

    handled = (
        ACCESS_EXECUTE
        | ACCESS_WRITE_FILE
        | ACCESS_READ_FILE
        | ACCESS_READ_DIR
        | ACCESS_REMOVE_DIR
        | ACCESS_REMOVE_FILE
        | ACCESS_MAKE_CHAR
        | ACCESS_MAKE_DIR
        | ACCESS_MAKE_REG
        | ACCESS_MAKE_SOCK
        | ACCESS_MAKE_FIFO
        | ACCESS_MAKE_BLOCK
        | ACCESS_MAKE_SYM
        | ACCESS_REFER
        | ACCESS_TRUNCATE
    )
    ruleset_attr = RulesetAttr(handled)
    ruleset_fd = libc.syscall(
        LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, "landlock_create_ruleset failed")

    read_access = ACCESS_EXECUTE | ACCESS_READ_FILE | ACCESS_READ_DIR
    try:
        for path, access in [
            *((path, read_access) for path in _existing_paths(read_only)),
            *((path, handled) for path in _existing_paths(read_write)),
        ]:
            path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                if not os.path.isdir(path):
                    access &= (
                        ACCESS_EXECUTE
                        | ACCESS_WRITE_FILE
                        | ACCESS_READ_FILE
                        | ACCESS_TRUNCATE
                    )
                rule = PathBeneathAttr(access, path_fd)
                if (
                    libc.syscall(
                        LANDLOCK_ADD_RULE,
                        ruleset_fd,
                        LANDLOCK_RULE_PATH_BENEATH,
                        ctypes.byref(rule),
                        0,
                    )
                    < 0
                ):
                    error = ctypes.get_errno()
                    raise OSError(error, f"landlock_add_rule failed for {path}")
            finally:
                os.close(path_fd)

        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            error = ctypes.get_errno()
            raise OSError(error, "PR_SET_NO_NEW_PRIVS failed")
        if libc.syscall(LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) < 0:
            error = ctypes.get_errno()
            raise OSError(error, "landlock_restrict_self failed")
    finally:
        os.close(ruleset_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-only", action="append", default=[])
    parser.add_argument("--read-write", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a scorer command is required after --")

    if not os.environ.get("GHOSTLAB_FAKE_SANDBOX_ROOT"):
        apply_landlock(read_only=args.read_only, read_write=args.read_write)
    os.execvpe(command[0], command, os.environ)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
