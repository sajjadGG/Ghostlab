from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .config import ConfigError, load_persona, load_runner, load_scenario, load_target
from .inspect import inspect_target, write_inspect_artifacts
from .orchestrator import run_scenario
from .types import utc_now

KNOWN_COMMANDS = {
    "run",
    "inspect",
    "profile",
    "generate-scenarios",
    "generate-personas",
    "generate-dataset",
    "run-dataset",
    "review-dataset",
    "doctor",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ghostlab", description="Rehearsal / MCP Ghostlab.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
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

    dataset_parser = sub.add_parser(
        "generate-dataset",
        help="Generate a persona x scenario dataset from a profile (uses codex).",
    )
    dataset_parser.add_argument(
        "--profile", required=True, type=Path, help="Path to a capabilities.json from `profile`."
    )
    dataset_parser.add_argument("--name", default="", help="Dataset name (default: derived from MCP).")
    dataset_parser.add_argument("--personas", type=int, default=2, help="Number of personas.")
    dataset_parser.add_argument(
        "--scenarios-per-persona", type=int, default=2, help="Scenarios generated per persona."
    )
    dataset_parser.add_argument("--seed", type=int, default=0, help="Seed for case ordering.")
    dataset_parser.add_argument(
        "--output-dir", type=Path, default=Path("datasets"), help="Base directory for datasets."
    )
    dataset_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary (default: auto-detect)."
    )
    dataset_parser.add_argument("--model", default="", help="Model override for codex.")

    rundataset_parser = sub.add_parser("run-dataset", help="Run every case in a dataset.")
    rundataset_parser.add_argument(
        "--dataset", required=True, type=Path, help="Path to a dataset directory (with dataset.json)."
    )
    rundataset_parser.add_argument("--target", required=True, type=Path, help="Path to target JSON config.")
    rundataset_parser.add_argument("--aut-runner", type=Path, help="Path to AUT runner JSON config.")
    rundataset_parser.add_argument("--user-runner", type=Path, help="Path to user emulator runner JSON config.")
    rundataset_parser.add_argument(
        "--output-dir", type=Path, default=Path("runs"), help="Directory for per-case runs and summary."
    )
    rundataset_parser.add_argument(
        "--limit", type=int, default=None, help="Only run the first N cases (for small dev runs)."
    )
    rundataset_parser.add_argument(
        "--approved-only", action="store_true", help="Only run cases with status=approved."
    )

    review_parser = sub.add_parser(
        "review-dataset", help="Review and curate a dataset (coverage, previews, flags, approve/reject)."
    )
    review_parser.add_argument(
        "--dataset", required=True, type=Path, help="Path to a dataset directory (with dataset.json)."
    )
    review_parser.add_argument(
        "--profile", type=Path, help="Optional capabilities.json for tool-coverage analysis."
    )
    review_parser.add_argument("--approve", nargs="*", default=None, help="Case ids to approve (no ids = all).")
    review_parser.add_argument("--reject", nargs="*", default=None, help="Case ids to reject (no ids = all).")
    review_parser.add_argument(
        "--needs-edit", nargs="*", default=None, dest="needs_edit", help="Case ids to mark needs-edit."
    )

    doctor_parser = sub.add_parser(
        "doctor", help="Check codex availability and validate runner presets."
    )
    doctor_parser.add_argument(
        "--runners", nargs="*", type=Path, default=None, help="Runner JSON configs to validate."
    )
    doctor_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary (default: auto-detect)."
    )
    return parser


def cmd_run(args: argparse.Namespace) -> int:
    target = load_target(args.target)
    scenario = load_scenario(args.scenario)
    aut_runner = load_runner(args.aut_runner)
    user_runner = load_runner(args.user_runner)
    persona = load_persona(args.persona) if args.persona else None
    result = run_scenario(
        target=target,
        scenario=scenario,
        aut_runner_config=aut_runner,
        user_runner_config=user_runner,
        output_dir=args.output_dir,
        persona=persona,
    )
    print(f"Rehearsal run {result.status} ({result.turns} turns)")
    print(f"  report: {result.report_path}")
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


def cmd_generate_dataset(args: argparse.Namespace) -> int:
    from .codex_backend import CodexBackend, CodexError
    from .dataset import build_dataset, write_dataset

    if not args.profile.exists():
        raise ConfigError(f"capabilities.json not found: {args.profile}")
    profile = json.loads(args.profile.read_text(encoding="utf-8"))

    mcp_name = str(profile.get("mcp", "mcp")).split("@")[0]
    name = args.name or mcp_name
    backend = CodexBackend(bin_path=args.codex_bin, model=args.model)
    print(
        f"Generating dataset '{name}': {args.personas} personas x "
        f"{args.scenarios_per_persona} scenarios with codex ({backend._bin()})..."
    )
    try:
        dataset = build_dataset(
            profile,
            backend,
            n_personas=args.personas,
            scenarios_per_persona=args.scenarios_per_persona,
            seed=args.seed,
            name=name,
        )
    except CodexError as exc:
        print(f"codex backend error: {exc}")
        return 1

    out_dir = args.output_dir / name
    manifest_path = write_dataset(dataset, out_dir)
    cases = dataset["manifest"]["cases"]
    print(f"Dataset written: {manifest_path}")
    print(f"  personas={len(dataset['personas'])} scenarios={len(dataset['scenarios'])} cases={len(cases)}")
    for case in cases:
        print(f"  - {case['id']} [{case.get('intent', '?')}]")
    return 0


def cmd_run_dataset(args: argparse.Namespace) -> int:
    from .dataset import run_dataset

    if not (args.dataset / "dataset.json").exists():
        raise ConfigError(f"No dataset.json in {args.dataset}")
    summary_path = run_dataset(
        args.dataset,
        target_path=args.target,
        aut_runner_path=args.aut_runner,
        user_runner_path=args.user_runner,
        output_dir=args.output_dir,
        limit=args.limit,
        approved_only=args.approved_only,
    )
    print(f"Dataset summary written to {summary_path}")
    return 0


def cmd_review_dataset(args: argparse.Namespace) -> int:
    from .review import (
        build_review,
        ensure_statuses,
        load_dataset,
        save_manifest,
        set_statuses,
        write_review_artifacts,
    )

    if not (args.dataset / "dataset.json").exists():
        raise ConfigError(f"No dataset.json in {args.dataset}")
    dataset = load_dataset(args.dataset)
    manifest = dataset["manifest"]

    changed = ensure_statuses(manifest)
    # Apply curation actions, if any.
    for status, ids in (
        ("approved", args.approve),
        ("rejected", args.reject),
        ("needs-edit", args.needs_edit),
    ):
        if ids is None:
            continue
        updated = set_statuses(manifest, set(ids), status)
        changed = changed or bool(updated)
        print(f"Marked {len(updated)} case(s) {status}.")
    if changed:
        save_manifest(args.dataset, manifest)

    profile = None
    if args.profile:
        if not args.profile.exists():
            raise ConfigError(f"capabilities.json not found: {args.profile}")
        profile = json.loads(args.profile.read_text(encoding="utf-8"))

    review = build_review(dataset, profile)
    json_path, md_path = write_review_artifacts(review, args.dataset)
    totals = review["totals"]
    print(f"Reviewed dataset '{review['dataset']}' ({review['mcp']})")
    print(
        f"  cases={totals['cases']} intents={totals['by_intent']} "
        f"statuses={totals['by_status']} flags={len(review['flags'])}"
    )
    for flag in review["flags"]:
        print(f"  ! {flag['kind']}: {flag['detail']}")
    print(f"  wrote {md_path}")
    print(f"  wrote {json_path}")
    return 0


def _validate_runner(path: Path) -> tuple[bool, str]:
    try:
        runner = load_runner(path)
    except ConfigError as exc:
        return False, str(exc)
    if runner.kind not in ("mock", "process", "codex-session"):
        return False, f"unknown kind '{runner.kind}'"
    if runner.kind in ("process", "codex-session") and not runner.command:
        return False, "empty command"
    if runner.kind == "codex-session" and "exec" not in runner.command:
        return False, "codex-session command must contain 'exec'"
    if runner.parser not in ("text", "codex-json"):
        return False, f"unknown parser '{runner.parser}'"
    return True, f"kind={runner.kind} parser={runner.parser}"


def cmd_doctor(args: argparse.Namespace) -> int:
    import shutil
    import subprocess

    from .codex_backend import CodexError, resolve_codex_bin

    ok = True
    print("Rehearsal / MCP Ghostlab doctor")
    try:
        codex_bin = args.codex_bin or resolve_codex_bin()
        version = subprocess.run(
            [codex_bin, "--version"], capture_output=True, text=True, timeout=20
        )
        tag = version.stdout.strip() or version.stderr.strip()
        print(f"  [ok] codex: {codex_bin} ({tag})")
    except (CodexError, OSError, subprocess.SubprocessError) as exc:
        ok = False
        print(f"  [!!] codex: {exc}")

    runner_paths = args.runners
    if runner_paths is None:
        runners_dir = Path("runners")
        runner_paths = sorted(runners_dir.glob("*.json")) if runners_dir.is_dir() else []
    for path in runner_paths:
        valid, detail = _validate_runner(path)
        ok = ok and valid
        print(f"  [{'ok' if valid else '!!'}] {path}: {detail}")

    print("All good." if ok else "Problems found.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # Backward compatibility: bare `--target ... --scenario ...` defaults to `run`.
    import sys

    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] not in KNOWN_COMMANDS and not raw[0].startswith("-"):
        parser.error(f"Unknown command: {raw[0]}")
    if raw and raw[0].startswith("-") and raw[0] not in {"-h", "--help", "--version"}:
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
        if args.command == "generate-dataset":
            return cmd_generate_dataset(args)
        if args.command == "run-dataset":
            return cmd_run_dataset(args)
        if args.command == "review-dataset":
            return cmd_review_dataset(args)
        if args.command == "doctor":
            return cmd_doctor(args)
    except ConfigError as exc:
        parser.error(str(exc))
        return 2

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
