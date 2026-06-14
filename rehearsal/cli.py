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
    "evaluate",
    "critique",
    "compare",
    "scorecard",
    "apps-probe",
    "apps-render",
    "ui",
    "db",
}


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path (default: ./ghostlab.sqlite3 or $GHOSTLAB_DB).",
    )


def _open_store(args: argparse.Namespace):
    """Open the persistence store, or None if it can't be opened (best-effort)."""
    from .storage import GhostlabStore

    try:
        return GhostlabStore.open(getattr(args, "db", None))
    except Exception as exc:  # noqa: BLE001
        print(f"  (persistence disabled: {exc})")
        return None


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
    _add_db_arg(run_parser)

    inspect_parser = sub.add_parser("inspect", help="Introspect a target MCP server.")
    inspect_parser.add_argument("--target", required=True, type=Path, help="Path to target JSON config.")
    inspect_parser.add_argument(
        "--output-dir", type=Path, default=Path("runs"), help="Directory for inspect artifacts."
    )
    inspect_parser.add_argument(
        "--timeout", type=float, default=30.0, help="Per-request timeout in seconds."
    )
    _add_db_arg(inspect_parser)

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
    _add_db_arg(profile_parser)

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
    _add_db_arg(dataset_parser)

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
    rundataset_parser.add_argument(
        "--evaluate", action="store_true", help="Score each case with the codex judge."
    )
    rundataset_parser.add_argument(
        "--capabilities", type=Path, help="capabilities.json for evaluation (hallucinated-tool checks)."
    )
    rundataset_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary for evaluation (default: auto-detect)."
    )
    rundataset_parser.add_argument("--model", default="", help="Model override for the codex judge.")
    _add_db_arg(rundataset_parser)

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
    _add_db_arg(review_parser)

    doctor_parser = sub.add_parser(
        "doctor", help="Check codex availability and validate runner presets."
    )
    doctor_parser.add_argument(
        "--runners", nargs="*", type=Path, default=None, help="Runner JSON configs to validate."
    )
    doctor_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary (default: auto-detect)."
    )

    eval_parser = sub.add_parser(
        "evaluate", help="Score a run into a pass/fail verdict (uses codex as judge)."
    )
    eval_parser.add_argument("--run", required=True, type=Path, help="Path to a run directory.")
    eval_parser.add_argument(
        "--capabilities", type=Path, help="Optional capabilities.json for hallucinated-tool checks."
    )
    eval_parser.add_argument(
        "--strict", action="store_true", help="Exit non-zero unless the verdict is a full pass."
    )
    eval_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary (default: auto-detect)."
    )
    eval_parser.add_argument("--model", default="", help="Model override for codex.")
    _add_db_arg(eval_parser)

    critique_parser = sub.add_parser(
        "critique", help="Critique an MCP's tool usability from a run (uses codex)."
    )
    critique_parser.add_argument("--run", required=True, type=Path, help="Path to a run directory.")
    critique_parser.add_argument(
        "--inspect", type=Path, help="Optional inspect.json so the judge can see tool definitions."
    )
    critique_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary (default: auto-detect)."
    )
    critique_parser.add_argument("--model", default="", help="Model override for codex.")

    compare_parser = sub.add_parser(
        "compare", help="Diff two run-dataset result sets for regressions."
    )
    compare_parser.add_argument(
        "--base", required=True, type=Path, help="Base summary dir or results.json."
    )
    compare_parser.add_argument(
        "--candidate", required=True, type=Path, help="Candidate summary dir or results.json."
    )
    compare_parser.add_argument(
        "--output", type=Path, default=None, help="Where to write comparison.md (default: stdout only)."
    )

    scorecard_parser = sub.add_parser(
        "scorecard", help="Roll a dataset run up into one MCP validation report."
    )
    scorecard_parser.add_argument(
        "--results", required=True, type=Path, help="Summary dir or results.json from run-dataset."
    )
    scorecard_parser.add_argument(
        "--output-dir", type=Path, default=None, help="Where to write scorecard.* (default: the summary dir)."
    )

    apps_parser = sub.add_parser(
        "apps-probe",
        help="Probe a target's MCP Apps (ui://) widgets: fetch resources + CSP diagnostics.",
    )
    apps_parser.add_argument("--target", required=True, type=Path, help="Path to target JSON config.")
    apps_parser.add_argument(
        "--tool", action="append", default=None,
        help="Restrict to specific UI tool(s) by name (repeatable).",
    )
    apps_parser.add_argument(
        "--output-dir", type=Path, default=Path("runs"), help="Directory for app-probe artifacts."
    )
    apps_parser.add_argument(
        "--timeout", type=float, default=30.0, help="Per-request timeout in seconds."
    )

    render_parser = sub.add_parser(
        "apps-render",
        help="Render an MCP Apps (ui://) widget in headless Chrome and capture proof.",
    )
    render_parser.add_argument("--target", required=True, type=Path, help="Path to target JSON config.")
    render_parser.add_argument(
        "--tool", default=None, help="UI tool to render (default: first UI-producing tool)."
    )
    render_parser.add_argument(
        "--arguments", default=None,
        help="Tool-call arguments as JSON (inline or @file). Used to call the tool for its result.",
    )
    render_parser.add_argument(
        "--no-call", action="store_true",
        help="Don't call the tool; render from --arguments as tool-input only.",
    )
    render_parser.add_argument(
        "--intent", action="append", default=None,
        help="UI intent to execute after render, as JSON (repeatable).",
    )
    render_parser.add_argument(
        "--output-dir", type=Path, default=Path("runs"), help="Directory for app-render artifacts."
    )
    render_parser.add_argument(
        "--timeout", type=float, default=30.0, help="Per-request timeout in seconds."
    )

    ui_parser = sub.add_parser("ui", help="Launch the Streamlit pipeline UI.")
    ui_parser.add_argument("--port", type=int, default=8501, help="Port to serve the UI on.")
    ui_parser.add_argument(
        "--server-address", default="localhost", help="Address Streamlit binds to."
    )

    db_parser = sub.add_parser("db", help="Manage the SQLite persistence database.")
    db_parser.add_argument(
        "action", choices=["init", "verify"], help="init: apply migrations. verify: integrity check."
    )
    _add_db_arg(db_parser)
    return parser


def cmd_run(args: argparse.Namespace) -> int:
    target = load_target(args.target)
    scenario = load_scenario(args.scenario)
    aut_runner = load_runner(args.aut_runner)
    user_runner = load_runner(args.user_runner)
    persona = load_persona(args.persona) if args.persona else None
    store = _open_store(args)
    try:
        result = run_scenario(
            target=target,
            scenario=scenario,
            aut_runner_config=aut_runner,
            user_runner_config=user_runner,
            output_dir=args.output_dir,
            persona=persona,
            store=store,
        )
    finally:
        if store is not None:
            store.close()
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

    store = _open_store(args)
    if store is not None:
        try:
            info = store.record_inspection(target, result)
            store.index_artifact("inspection", info["inspection_public_id"], "inspect.json", json_path)
            store.index_artifact("inspection", info["inspection_public_id"], "inspect.md", md_path)
            print(f"  saved as version v{info['version']} ({info['inspection_public_id']})")
        finally:
            store.close()
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    from .codex_backend import CodexBackend, CodexError
    from .profile import build_capability_profile, profile_prompt, write_profile_artifacts

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

    store = _open_store(args)
    if store is not None:
        try:
            inspection_public = store.find_inspection_by_mcp(profile.get("mcp", ""))
            if inspection_public is None:
                print("  (not persisted: no matching inspection in the database — run `inspect` first)")
            else:
                info = store.record_profile(
                    inspection_public, profile,
                    model=args.model or "codex default",
                    prompt_text=profile_prompt(inspect_data),
                )
                store.index_artifact("profile", info["profile_public_id"], "capabilities.json", json_path)
                store.index_artifact("profile", info["profile_public_id"], "capabilities.md", md_path)
                print(f"  saved profile {info['profile_public_id']}")
        finally:
            store.close()
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

    store = _open_store(args)
    if store is not None:
        try:
            profile_public = store.find_profile_by_mcp(profile.get("mcp", ""))
            info = store.record_dataset(
                dataset,
                profile_public_id=profile_public,
                model=args.model or "codex default",
                params={
                    "n_personas": args.personas,
                    "scenarios_per_persona": args.scenarios_per_persona,
                    "seed": args.seed,
                },
            )
            store.index_artifact("dataset", info["dataset_public_id"], "dataset.json", manifest_path)
            print(f"  saved dataset {info['dataset_public_id']}")
        finally:
            store.close()
    return 0


def cmd_run_dataset(args: argparse.Namespace) -> int:
    from .dataset import run_dataset

    if not (args.dataset / "dataset.json").exists():
        raise ConfigError(f"No dataset.json in {args.dataset}")

    backend = None
    capabilities = None
    if args.evaluate:
        from .codex_backend import CodexBackend

        backend = CodexBackend(bin_path=args.codex_bin, model=args.model)
        if args.capabilities:
            if not args.capabilities.exists():
                raise ConfigError(f"capabilities.json not found: {args.capabilities}")
            capabilities = json.loads(args.capabilities.read_text(encoding="utf-8"))

    store = _open_store(args)
    try:
        summary_path = run_dataset(
            args.dataset,
            target_path=args.target,
            aut_runner_path=args.aut_runner,
            user_runner_path=args.user_runner,
            output_dir=args.output_dir,
            limit=args.limit,
            approved_only=args.approved_only,
            evaluate=args.evaluate,
            capabilities=capabilities,
            backend=backend,
            store=store,
        )
    finally:
        if store is not None:
            store.close()
    print(f"Dataset summary written to {summary_path}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    from .compare import diff_results, load_results, render_comparison_md

    base = load_results(args.base)
    candidate = load_results(args.candidate)
    diff = diff_results(base, candidate)
    md = render_comparison_md(diff)

    print(
        f"Comparison: regressions={len(diff['regressions'])} fixes={len(diff['fixes'])} "
        f"changed={len(diff['changed'])} unchanged={diff['unchanged']}"
    )
    for entry in diff["regressions"]:
        print(f"  REGRESSION {entry['case']}: {entry['base']} -> {entry['candidate']}")
    if args.output:
        args.output.write_text(md, encoding="utf-8")
        print(f"  wrote {args.output}")
    else:
        print()
        print(md)
    return 1 if diff["regressions"] else 0


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


def cmd_evaluate(args: argparse.Namespace) -> int:
    from .codex_backend import CodexBackend, CodexError
    from .evaluate import evaluate_run, write_verdict_artifacts

    if not (args.run / "events.jsonl").exists():
        raise ConfigError(f"No events.jsonl in {args.run}")
    capabilities = None
    if args.capabilities:
        if not args.capabilities.exists():
            raise ConfigError(f"capabilities.json not found: {args.capabilities}")
        capabilities = json.loads(args.capabilities.read_text(encoding="utf-8"))

    backend = CodexBackend(bin_path=args.codex_bin, model=args.model)
    print(f"Evaluating {args.run} with codex judge ({backend._bin()})...")
    store = _open_store(args)
    try:
        verdict = evaluate_run(args.run, backend, capabilities, store=store)
    except CodexError as exc:
        print(f"codex backend error: {exc}")
        return 1
    finally:
        if store is not None:
            store.close()

    json_path, md_path = write_verdict_artifacts(verdict, args.run)
    det = verdict["deterministic"]
    print(f"Verdict: {verdict['verdict'].upper()} ({verdict['scenario']})")
    print(f"  coverage={det['coverage']} failed_calls={len(det['tool_failures'])} gates={verdict['gates'] or 'none'}")
    print(f"  {verdict['judge'].get('summary', '')}")
    print(f"  wrote {md_path}")
    print(f"  wrote {json_path}")

    if verdict["verdict"] == "pass":
        return 0
    if verdict["verdict"] == "partial" and not args.strict:
        return 0
    return 1


def cmd_scorecard(args: argparse.Namespace) -> int:
    from .scorecard import build_scorecard, load_summary, write_scorecard_artifacts

    results_path = args.results
    summary_file = results_path / "results.json" if results_path.is_dir() else results_path
    if not summary_file.exists():
        raise ConfigError(f"results.json not found: {summary_file}")

    summary = load_summary(results_path)
    base_dir = summary_file.parent
    scorecard = build_scorecard(summary, base_dir)

    out_dir = args.output_dir or base_dir
    json_path, md_path = write_scorecard_artifacts(scorecard, out_dir)
    totals = scorecard["totals"]
    pass_rate = scorecard.get("pass_rate")
    print(f"Scorecard for '{scorecard['dataset']}' ({totals['cases']} cases)")
    print(f"  pass_rate={'n/a' if pass_rate is None else f'{pass_rate * 100:.0f}%'}"
          f" hallucinated={len(scorecard['hallucinated_tools'])}"
          f" golden_mismatches={scorecard['golden_mismatches']}")
    worst = scorecard["per_tool"][:3]
    for tool in worst:
        if tool["failures"]:
            print(f"  ! {tool['tool']}: {tool['failures']}/{tool['calls']} failed")
    if scorecard["missing_runs"]:
        print(f"  (missing run dirs for: {', '.join(scorecard['missing_runs'])})")
    print(f"  wrote {md_path}")
    print(f"  wrote {json_path}")
    return 0


def cmd_critique(args: argparse.Namespace) -> int:
    from .codex_backend import CodexBackend, CodexError
    from .critique import critique_run, write_critique_artifacts

    if not (args.run / "events.jsonl").exists():
        raise ConfigError(f"No events.jsonl in {args.run}")
    inspect = None
    if args.inspect:
        if not args.inspect.exists():
            raise ConfigError(f"inspect.json not found: {args.inspect}")
        inspect = json.loads(args.inspect.read_text(encoding="utf-8"))

    backend = CodexBackend(bin_path=args.codex_bin, model=args.model)
    print(f"Critiquing tool usability in {args.run} with codex ({backend._bin()})...")
    try:
        critique = critique_run(args.run, backend, inspect)
    except CodexError as exc:
        print(f"codex backend error: {exc}")
        return 1

    json_path, md_path = write_critique_artifacts(critique, args.run)
    judged = critique["critique"]
    print(f"Tool-ergonomics score: {judged.get('overall_score', '?')}/5 ({critique['scenario']})")
    print(f"  exercised tools: {', '.join(critique['exercised_tools']) or 'none'}")
    for rec in judged.get("top_recommendations", []):
        print(f"  -> {rec}")
    print(f"  wrote {md_path}")
    print(f"  wrote {json_path}")
    return 0


def cmd_apps_probe(args: argparse.Namespace) -> int:
    from .mcp_apps import build_app_report, probe_ui_tools, render_app_report_md
    from .mcp_client import create_client

    target = load_target(args.target)
    client = create_client(target, timeout=args.timeout)
    try:
        client.initialize()
        tools = client.list_collection("tools/list", "tools")
        only = set(args.tool) if args.tool else None
        probes = probe_ui_tools(client, tools, only=only)
    finally:
        client.close()

    report = build_app_report(target.id, probes)
    timestamp = utc_now().replace("+00:00", "Z").replace(":", "")
    out_dir = args.output_dir / f"{timestamp}-{target.id}-apps"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "apps-probe.json"
    md_path = out_dir / "apps-probe.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_app_report_md(report), encoding="utf-8")

    summary = report["summary"]
    print(
        f"Probed {summary['ui_tools']} UI tool(s): "
        f"{summary['renderable_resources']} renderable, "
        f"{summary['diagnostic_findings']} finding(s)"
    )
    for probe in probes:
        for finding in probe.diagnostics:
            print(f"  ! [{finding['severity']}] {probe.tool}: {finding['message']}")
    print(f"  wrote {json_path}")
    print(f"  wrote {md_path}")
    return 0


def _load_json_arg(value: str | None) -> dict:
    """Parse a JSON CLI argument given inline or as `@path/to/file.json`."""
    if not value:
        return {}
    text = Path(value[1:]).read_text(encoding="utf-8") if value.startswith("@") else value
    return json.loads(text)


def cmd_apps_render(args: argparse.Namespace) -> int:
    from .apps_host import renderer as _renderer
    from .apps_host.assertions import assertions_for, evaluate_assertions
    from .apps_host.report import build_render_report, first_ui_tool, render_report_md
    from .mcp_apps import parse_app_resource, parse_ui_intent, ui_resource_uri
    from .mcp_client import create_client

    if not _renderer.render_available():
        print(
            "Playwright is not installed. Install the apps extra:\n"
            "  pip install 'ghostlab[apps]'   (then: playwright install chrome)"
        )
        return 1

    target = load_target(args.target)
    arguments = _load_json_arg(args.arguments)
    intents = [parse_ui_intent(json.loads(item)) for item in (args.intent or [])]

    client = create_client(target, timeout=args.timeout)
    try:
        client.initialize()
        tools = client.list_collection("tools/list", "tools")
        tool = first_ui_tool(tools, args.tool)
        if tool is None:
            which = f" named {args.tool!r}" if args.tool else ""
            raise ConfigError(f"no UI-producing tool{which} found on {target.id}")
        tool_name = tool.get("name")
        uri = ui_resource_uri(tool.get("_meta"))
        resource = parse_app_resource(uri, client.read_resource(uri))
        if not resource.renderable:
            raise ConfigError(f"resource {uri} is not renderable: {resource.fetch_error or 'empty'}")
        tool_result = None
        if not args.no_call:
            tool_result = client.call_tool(tool_name, arguments)
    finally:
        client.close()

    timestamp = utc_now().replace("+00:00", "Z").replace(":", "")
    out_dir = args.output_dir / f"{timestamp}-{target.id}-render"
    out_dir.mkdir(parents=True, exist_ok=True)
    screenshot = out_dir / "widget.png"

    print(f"Rendering `{tool_name}` ({uri}) in headless Chrome...")
    result = _renderer.render_widget(
        uri=uri,
        widget_html=resource.html,
        tool_input=arguments,
        tool_result=tool_result,
        intents=intents,
        screenshot_path=screenshot,
    )
    assertions = evaluate_assertions(assertions_for(uri), result.summary())
    report = build_render_report(target.id, tool_name, result, assertions)

    json_path = out_dir / "apps-render.json"
    md_path = out_dir / "apps-render.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_report_md(report), encoding="utf-8")

    summary = report["summary"]
    if result.error:
        print(f"  render error: {result.error}")
    print(
        f"  handshake={summary['handshake_completed']} "
        f"interactive={summary['interactive_elements']} "
        f"assertions={summary['assertions_passed']}/{summary['assertions_total']}"
    )
    for a in assertions:
        if not a["passed"]:
            print(f"  ! failed assertion: {a['name']} — {a['description']}")
    if result.screenshot_path:
        print(f"  screenshot {result.screenshot_path}")
    if result.final_screenshot_path:
        print(f"  final screenshot {result.final_screenshot_path}")
    print(f"  wrote {json_path}")
    print(f"  wrote {md_path}")
    return 0 if (result.error is None and summary["assertions_passed"] == summary["assertions_total"]) else 1


def cmd_db(args: argparse.Namespace) -> int:
    from .storage import get_connection, integrity_check, resolve_db_path

    path = resolve_db_path(args.db)
    conn = get_connection(args.db)  # applies pending migrations
    try:
        if args.action == "init":
            applied = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            print(f"Database ready at {path} ({applied} migration(s) applied).")
            return 0
        # verify
        result = integrity_check(conn)
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        print(f"Database: {path}")
        print(f"  migrations: {versions or 'none'}")
        print(f"  integrity_check: {result}")
        return 0 if result == "ok" else 1
    finally:
        conn.close()


def cmd_ui(args: argparse.Namespace) -> int:
    import subprocess
    import sys
    from importlib import util as importlib_util

    if importlib_util.find_spec("streamlit") is None:
        print(
            "Streamlit is not installed. Install the UI extra:\n"
            "  pip install 'ghostlab[ui]'   (or: pip install streamlit)"
        )
        return 1

    app_path = Path(__file__).resolve().parent / "ui" / "app.py"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(args.port),
        "--server.address",
        args.server_address,
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    print(f"Launching MCP Ghostlab UI at http://{args.server_address}:{args.port}")
    return subprocess.call(command)


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
        if args.command == "evaluate":
            return cmd_evaluate(args)
        if args.command == "critique":
            return cmd_critique(args)
        if args.command == "compare":
            return cmd_compare(args)
        if args.command == "scorecard":
            return cmd_scorecard(args)
        if args.command == "apps-probe":
            return cmd_apps_probe(args)
        if args.command == "apps-render":
            return cmd_apps_render(args)
        if args.command == "db":
            return cmd_db(args)
        if args.command == "ui":
            return cmd_ui(args)
    except ConfigError as exc:
        parser.error(str(exc))
        return 2

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
