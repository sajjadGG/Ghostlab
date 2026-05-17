from __future__ import annotations

import argparse
from pathlib import Path

from .config import ConfigError, load_runner, load_scenario, load_target
from .orchestrator import run_scenario


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run MCP Rehearsal E2E scenarios.")
    parser.add_argument("--target", required=True, type=Path, help="Path to target JSON config.")
    parser.add_argument("--scenario", required=True, type=Path, help="Path to scenario JSON config.")
    parser.add_argument("--aut-runner", type=Path, help="Path to AUT runner JSON config.")
    parser.add_argument("--user-runner", type=Path, help="Path to user emulator runner JSON config.")
    parser.add_argument("--output-dir", type=Path, default=Path("runs"), help="Directory for logs and reports.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
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
    except ConfigError as exc:
        parser.error(str(exc))
        return 2

    print(f"Rehearsal report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

