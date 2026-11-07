"""A stand-in for the OpenShell CLI: each sandbox is a directory under
$FAKE_OPENSHELL_ROOT/boxes, uploads and downloads are copies, and
``sandbox exec`` really runs the command with sandbox paths rewritten into
that directory. Every invocation is appended to calls.jsonl."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ["FAKE_OPENSHELL_ROOT"])
ROOT.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
with open(ROOT / "calls.jsonl", "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")


def box(name):
    return ROOT / "boxes" / name


def inside(name, path):
    text = str(path)
    if text.startswith(("/sandbox", "/opt/agent")):
        return str(box(name) / text.lstrip("/"))
    return text


def copy(source, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if Path(source).is_dir():
        shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
    else:
        shutil.copy2(source, dest)


def option(flag):
    return args[args.index(flag) + 1]


verb = args[:2]
if verb == ["sandbox", "create"]:
    if os.environ.get("FAKE_OPENSHELL_FAIL_CREATE"):
        print("gateway unavailable: connection refused", file=sys.stderr)
        sys.exit(1)
    name = option("--name")
    box(name).mkdir(parents=True)
    for index, part in enumerate(args):
        if part == "--upload":
            source, _, target = args[index + 1].rpartition(":")
            copy(source, Path(inside(name, target)) / Path(source).name)
elif verb == ["sandbox", "exec"]:
    name = option("-n")
    env = dict(os.environ)
    for index, part in enumerate(args):
        if part == "--env":
            key, _, value = args[index + 1].partition("=")
            env[key] = value
    workdir = Path(inside(name, option("--workdir")))
    workdir.mkdir(parents=True, exist_ok=True)
    inner = [inside(name, part) for part in args[args.index("--") + 1:]]
    done = subprocess.run(inner, stdin=sys.stdin, cwd=workdir, env=env, check=False)
    sys.exit(done.returncode)
elif verb == ["sandbox", "upload"]:
    copy(args[3], inside(args[2], args[4]))
elif verb == ["sandbox", "download"]:
    source = Path(inside(args[2], args[3]))
    if not source.exists():
        print(f"no such file: {args[3]}", file=sys.stderr)
        sys.exit(1)
    copy(source, args[4])
elif verb == ["sandbox", "delete"]:
    shutil.rmtree(box(args[2]), ignore_errors=True)
elif args[:1] == ["logs"]:
    print("fake openshell logs")
