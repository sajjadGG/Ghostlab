from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import ConfigError, load_persona, load_runner, load_scenario, load_target
from .inspect import inspect_target, write_inspect_artifacts
from .orchestrator import run_scenario
from .types import utc_now

KNOWN_COMMANDS = {"run", "inspect", "profile", "generate-scenarios", "generate-personas"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rehearsal / MCP Ghostlab.")
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run a dual-agent E2E scenario.")
    run_parser.add_argument("--target", required=True, type=Path, help="Path to target JSON config.")
    run_parser.add_argument("--scenario", required=True, type=Path, help="Path to scenario JSON config.")
    run_parser.add_argument("--aut-runner", type=Path, help="Path to AUT runner JSON config.")
    run_parser.add_argument("--user-runner", type=Path, help="Path to user emulator runner JSON config.")
    run_parser.add_argument("--persona", type=Path, help="Optional persona JSON to drive the user emulator.")
    run_parser.add_argument("--output-dir", type=Path, default=Path("runs"), help="Directory for logs and reports.")

    inspect_parser = sub.add_parser("inspect", help="Introspect a target MCP server.")
    inspect_parser.add_argument("--target", required=True, type=Path, help="Path to target JSON config.")
    inspect_parser.add_argument(
        "--output-dir", type=Path, default=Path("runs"), help="Directory for inspect artifacts."
    )
    inspect_parser.add_argument(
        "--timeout", type=float, default=30.0, help="Per-request timeout in seconds."
    )

    profile_parser = sub.add_parser(
        "profile", help="Build a capability profile from an inspect.json (uses codex)."
    )
    profile_parser.add_argument(
        "--inspect", required=True, type=Path, help="Path to an inspect.json from `inspect`."
    )
    profile_parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write capabilities.json/.md (default: alongside inspect.json).",
    )
    profile_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary (default: auto-detect)."
    )
    profile_parser.add_argument("--model", default="", help="Model override for codex.")

    gen_parser = sub.add_parser(
        "generate-scenarios", help="Generate scenarios from a capability profile (uses codex)."
    )
    gen_parser.add_argument(
        "--profile", required=True, type=Path, help="Path to a capabilities.json from `profile`."
    )
    gen_parser.add_argument("--n", type=int, default=3, help="Number of scenarios to generate.")
    gen_parser.add_argument(
        "--output-dir", type=Path, default=Path("scenarios"), help="Where to write scenario JSON files."
    )
    gen_parser.add_argument(
        "--prefix", default="", help="Optional filename prefix for generated scenarios."
    )
    gen_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary (default: auto-detect)."
    )
    gen_parser.add_argument("--model", default="", help="Model override for codex.")

    persona_parser = sub.add_parser(
        "generate-personas", help="Generate a persona library from a capability profile (uses codex)."
    )
    persona_parser.add_argument(
        "--profile", required=True, type=Path, help="Path to a capabilities.json from `profile`."
    )
    persona_parser.add_argument("--n", type=int, default=4, help="Number of personas to generate.")
    persona_parser.add_argument(
        "--output-dir", type=Path, default=Path("personas"), help="Where to write persona JSON files."
    )
    persona_parser.add_argument(
        "--prefix", default="", help="Optional filename prefix for generated personas."
    )
    persona_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary (default: auto-detect)."
    )
    persona_parser.add_argument("--model", default="", help="Model override for codex.")
    return parser


def cmd_run(args: argparse.Namespace) -> int:
    target = load_target(args.target)
    scenario = load_scenario(args.scenario)
    aut_runner = load_runner(args.aut_runner)
    user_runner = load_runner(args.user_runner)
    persona = load_persona(args.persona) if args.persona else None
    report_path = run_scenario(
        target=target,
        scenario=scenario,
        aut_runner_config=aut_runner,
        user_runner_config=user_runner,
        output_dir=args.output_dir,
        persona=persona,
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


def cmd_profile(args: argparse.Namespace) -> int:
    from .codex_backend import CodexBackend, CodexError
    from .profile import build_capability_profile, write_profile_artifacts

    inspect_path = args.inspect
    if not inspect_path.exists():
        raise ConfigError(f"inspect.json not found: {inspect_path}")
    inspect_data = json.loads(inspect_path.read_text(encoding="utf-8"))

    backend = CodexBackend(bin_path=args.codex_bin, model=args.model)
    print(f"Generating capability profile with codex ({backend._bin()})...")
    try:
        profile = build_capability_profile(inspect_data, backend)
    except CodexError as exc:
        print(f"codex backend error: {exc}")
        return 1

    out_dir = args.output_dir or inspect_path.parent
    json_path, md_path = write_profile_artifacts(profile, out_dir)
    print(f"Profiled {profile.get('mcp', '?')}")
    print(
        f"  categories={len(profile.get('categories', []))} "
        f"workflows={len(profile.get('workflows', []))} "
        f"missing_tools={len(profile.get('gaps', {}).get('missing_referenced_tools', []))}"
    )
    print(f"  wrote {json_path}")
    print(f"  wrote {md_path}")
    return 0


def cmd_generate_scenarios(args: argparse.Namespace) -> int:
    from .codex_backend import CodexBackend, CodexError
    from .generate import generate_scenarios, write_scenarios

    if not args.profile.exists():
        raise ConfigError(f"capabilities.json not found: {args.profile}")
    profile = json.loads(args.profile.read_text(encoding="utf-8"))

    backend = CodexBackend(bin_path=args.codex_bin, model=args.model)
    print(f"Generating {args.n} scenario(s) with codex ({backend._bin()})...")
    try:
        scenarios = generate_scenarios(profile, backend, args.n)
    except CodexError as exc:
        print(f"codex backend error: {exc}")
        return 1

    paths = write_scenarios(scenarios, args.output_dir, prefix=args.prefix)
    print(f"Generated {len(paths)} scenario(s) for {profile.get('mcp', '?')}:")
    for scenario, path in zip(scenarios, paths):
        intent = scenario.get("intent", "?")
        exercises = ", ".join(scenario.get("exercises", [])) or "(none)"
        print(f"  [{intent}] {scenario['id']} -> {path}")
        print(f"      exercises: {exercises}")
    return 0


def cmd_generate_personas(args: argparse.Namespace) -> int:
    from .codex_backend import CodexBackend, CodexError
    from .personas import generate_personas, write_personas

    if not args.profile.exists():
        raise ConfigError(f"capabilities.json not found: {args.profile}")
    profile = json.loads(args.profile.read_text(encoding="utf-8"))

    backend = CodexBackend(bin_path=args.codex_bin, model=args.model)
    print(f"Generating {args.n} persona(s) with codex ({backend._bin()})...")
    try:
        personas = generate_personas(profile, backend, args.n)
    except CodexError as exc:
        print(f"codex backend error: {exc}")
        return 1

    paths = write_personas(personas, args.output_dir, prefix=args.prefix)
    print(f"Generated {len(paths)} persona(s) for {profile.get('mcp', '?')}:")
    for persona, path in zip(personas, paths):
        traits = ", ".join(persona.get("traits", [])) or "(none)"
        print(f"  {persona['id']} ({persona.get('name', '')}) -> {path}")
        print(f"      traits: {traits}")
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
        if args.command == "profile":
            return cmd_profile(args)
        if args.command == "generate-scenarios":
            return cmd_generate_scenarios(args)
        if args.command == "generate-personas":
            return cmd_generate_personas(args)
    except ConfigError as exc:
        parser.error(str(exc))
        return 2

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
