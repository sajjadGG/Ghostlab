from __future__ import annotations

import argparse
from pathlib import Path

from .config import ConfigError, load_runner, load_scenario, load_target
from .inspect import inspect_target, write_inspect_artifacts
from .orchestrator import run_scenario
from .types import utc_now

KNOWN_COMMANDS = {"run", "inspect"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rehearsal / MCP Ghostlab.")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run a dual-agent E2E scenario.")
    run_parser.add_argument("--target", required=True, type=Path, help="Path to target JSON config.")
    run_parser.add_argument("--scenario", required=True, type=Path, help="Path to scenario JSON config.")
    run_parser.add_argument("--aut-runner", type=Path, help="Path to AUT runner JSON config.")
    run_parser.add_argument("--user-runner", type=Path, help="Path to user emulator runner JSON config.")
    run_parser.add_argument("--output-dir", type=Path, default=Path("runs"), help="Directory for logs and reports.")

    inspect_parser = sub.add_parser("inspect", help="Introspect a target MCP server.")
    inspect_parser.add_argument("--target", required=True, type=Path, help="Path to target JSON config.")
    inspect_parser.add_argument(
        "--output-dir", type=Path, default=Path("runs"), help="Directory for inspect artifacts."
    )
    inspect_parser.add_argument(
        "--timeout", type=float, default=30.0, help="Per-request timeout in seconds."
    )
    return parser


def cmd_run(args: argparse.Namespace) -> int:
    target = load_target(args.target)
    scenario = load_scenario(args.scenario)
    aut_runner = load_runner(args.aut_runner)
    user_runner = load_runner(args.user_runner)
    report_path = run_scenario(
        target=target,
        scenario=scenario,
        aut_runner_config=aut_runner,
        user_runner_config=user_runner,
        output_dir=args.output_dir,
    )
    print(f"Rehearsal report written to {report_path}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    target = load_target(args.target)
    result = inspect_target(target, timeout=args.timeout)
    timestamp = utc_now().replace("+00:00", "Z").replace(":", "")
    out_dir = args.output_dir / f"{timestamp}-{target.id}-inspect"
    json_path, md_path = write_inspect_artifacts(result, out_dir)

    server = result.server_info or {}
    print(f"Inspected {server.get('name', '?')}@{server.get('version', '?')} ({target.transport})")
    print(
        f"  tools={len(result.tools)} resources={len(result.resources)} "
        f"prompts={len(result.prompts)} lint={len(result.lint)}"
    )
    if result.lint:
        for finding in result.lint:
            print(f"  ! {finding['referenced']} referenced in {finding['in']}")
    print(f"  wrote {json_path}")
    print(f"  wrote {md_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # Backward compatibility: bare `--target ... --scenario ...` defaults to `run`.
    import sys

    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] not in KNOWN_COMMANDS and not raw[0].startswith("-"):
        parser.error(f"Unknown command: {raw[0]}")
    if raw and raw[0].startswith("-"):
        raw = ["run", *raw]

    args = parser.parse_args(raw)
    if args.command is None:
        parser.print_help()
        return 2

    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "inspect":
            return cmd_inspect(args)
    except ConfigError as exc:
        parser.error(str(exc))
        return 2

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
