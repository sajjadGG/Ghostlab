"""``ghostlab exec`` runs one agent task; ``ghostlab run`` runs a pipeline."""
from __future__ import annotations

import argparse
import dataclasses
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from ._util import load_file, load_items, read_json
from .agent import Agent, ConfigError
from .pipeline import Pipeline
from .sandbox import SandboxError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghostlab",
        description="Run coding agents headlessly, alone or as checkpointed pipelines.",
    )
    parser.add_argument("--version", action="version", version=f"ghostlab {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_one = sub.add_parser("exec", help="Run one agent on one task and print its answer.")
    run_one.add_argument("--agent", required=True, type=Path, help="Agent config (YAML/JSON).")
    run_one.add_argument("task", nargs="?", default="", help="The task; '-' reads stdin.")
    run_one.add_argument("--prompt-file", type=Path, help="Read the task from a file.")
    run_one.add_argument("--harness", default="", help="Override the agent's harness.")
    run_one.add_argument("--model", default="", help="Override the agent's model.")
    run_one.add_argument("--sandbox", choices=["openshell", "local"], default="",
                         help="Override the execution boundary.")
    run_one.add_argument("--provider", action="append", default=[],
                         help="Attach an OpenShell provider (repeatable).")
    run_one.add_argument("--workspace", type=Path, help="Override the agent's workspace.")
    run_one.add_argument("--timeout", type=int, default=0, help="Turn timeout in seconds.")
    run_one.add_argument("--collect", action="append", default=[], metavar="PATH",
                         help="Copy a workspace path into the run dir (repeatable).")
    run_one.add_argument("--run-dir", type=Path, help="Where to keep the trace.")
    run_one.add_argument("--json", action="store_true", help="Print the full result as JSON.")

    run_many = sub.add_parser("run", help="Run or resume a pipeline defined in Python.")
    run_many.add_argument("pipeline", help="FILE.py or FILE.py:NAME holding a Pipeline.")
    run_many.add_argument("--input", action="append", type=Path, default=[],
                          help="Items as .jsonl, JSON, or YAML (repeatable). Omit to resume.")
    run_many.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                          help="A pipeline parameter; VALUE is parsed as JSON when it can be.")
    run_many.add_argument("--params", type=Path, help="Parameters from a YAML/JSON mapping.")
    run_many.add_argument("--run-dir", type=Path, help="Run directory (default: runs/<name>).")

    status = sub.add_parser("status", help="Summarize a pipeline run directory.")
    status.add_argument("run_dir", type=Path)
    return parser


def _exec(args: argparse.Namespace) -> int:
    if args.prompt_file:
        task = args.prompt_file.read_text(encoding="utf-8")
    elif args.task == "-":
        task = sys.stdin.read()
    else:
        task = args.task
    overrides: dict[str, Any] = {}
    if args.harness:
        overrides["harness"] = args.harness
    if args.model:
        overrides["model"] = args.model
    if args.timeout:
        overrides["timeout"] = args.timeout
    if args.workspace:
        overrides["workspace"] = args.workspace
    agent = Agent.load(args.agent).with_(**overrides)
    if args.sandbox or args.provider:
        agent = agent.with_(sandbox=dataclasses.replace(
            agent.sandbox, backend=args.sandbox or agent.sandbox.backend,
            providers=tuple(dict.fromkeys([*agent.sandbox.providers, *args.provider])),
        ))
    result = agent.run(task, args.run_dir, collect=tuple(args.collect))
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        if result.answer:
            print(result.answer)
        print(f"[{result.status}] {result.dir}" + (f" — {result.error}" if result.error else ""),
              file=sys.stderr)
    return 0 if result.ok else 1


def load_pipeline(target: str) -> Pipeline:
    """Import ``FILE.py[:NAME]`` and return its Pipeline."""
    path_text, _, name = target.partition(":")
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"pipeline file not found: {path}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))  # let the file import its siblings
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    if name:
        found = getattr(module, name, None)
        if not isinstance(found, Pipeline):
            raise ConfigError(f"{path} has no Pipeline named {name!r}")
        return found
    pipelines = [v for v in vars(module).values() if isinstance(v, Pipeline)]
    if len(pipelines) != 1:
        raise ConfigError(f"{path} defines {len(pipelines)} pipelines; pick one with FILE.py:NAME")
    return pipelines[0]


def _params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = dict(load_file(args.params) or {}) if args.params else {}
    for pair in args.param:
        key, sep, value = pair.partition("=")
        if not sep:
            raise ConfigError(f"--param expects KEY=VALUE, got {pair!r}")
        try:
            params[key] = json.loads(value)
        except json.JSONDecodeError:
            params[key] = value
    return params


def _run(args: argparse.Namespace) -> int:
    pipeline = load_pipeline(args.pipeline)
    inputs: list[Any] | None = None
    if args.input:
        inputs = [item for path in args.input for item in load_items(path)]
    report = pipeline.run(inputs, run_dir=args.run_dir or Path("runs") / pipeline.name,
                          params=_params(args))
    print(f"{len(report.results)} results in {report.run_dir / 'results.jsonl'}")
    for failure in report.failures:
        print(f"  failed: {failure['stage']}/{failure['key']}: {failure['error']}", file=sys.stderr)
    return 0 if report.ok else 1


def _status(args: argparse.Namespace) -> int:
    job_file = args.run_dir / "job.json"
    if not job_file.is_file():
        raise ConfigError(f"not a pipeline run directory: {args.run_dir}")
    job = read_json(job_file)
    state = "finished" if job.get("finished_at") else "incomplete"
    print(f"{job['pipeline']} ({state}) — {args.run_dir}")
    for entry in job.get("stages", []):
        print(f"  {entry['name']:<20} {entry['done']:>4} done  {entry['cached']:>4} cached  "
              f"{entry['failed']:>4} failed  -> {entry['outputs']} items")
    for failure in job.get("failures", []):
        print(f"  failed: {failure['stage']}/{failure['key']}: {failure['error']}")
    return 1 if job.get("failures") or state != "finished" else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return {"exec": _exec, "run": _run, "status": _status}[args.command](args)
    except (ConfigError, SandboxError, ValueError, OSError) as exc:
        print(f"ghostlab: {exc}", file=sys.stderr)
        return 2
