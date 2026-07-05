from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from . import __version__
from . import termcolor as tc
from .config import ConfigError, RunnerConfig, load_persona, load_runner, load_scenario, load_target
from .inspect import inspect_target, write_inspect_artifacts
from .orchestrator import run_scenario
from .types import utc_now

# Command -> handler registry; populated after the cmd_* definitions below.
# `KNOWN_COMMANDS` is derived from it so the two can never drift apart, and a
# test asserts the registry matches the subparsers declared in build_parser().
_HANDLERS: "dict[str, object]" = {}


def _add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite database path (default: the job's workspace/ghostlab.sqlite3, "
             "or ./ghostlab.sqlite3 / $GHOSTLAB_DB).",
    )


def _add_server_arg(parser: argparse.ArgumentParser) -> None:
    """Server selector for a standard `mcpServers` config given via --target."""
    parser.add_argument(
        "--server", default=None,
        help="When --target is a standard MCP config (mcpServers) with more than "
             "one server, which server to test. Auto-selected when only one.",
    )


def _add_job_args(parser: argparse.ArgumentParser) -> None:
    """Job selector for the discover/plan/test/review loop commands."""
    parser.add_argument(
        "--job", default=None,
        help="Job to operate on: a name under jobs/<name>/, a job directory, or "
             "(with no value) a job.yaml in the current directory.",
    )
    parser.add_argument(
        "--spec", type=Path, default=None,
        help="Explicit spec file path; overrides --job. For advanced use.",
    )


def _job_spec(args: argparse.Namespace):
    """Resolve --job/--spec, load the spec, seed prompt overrides, default the db.

    Sets ``args.spec`` to the resolved spec path so the rest of a handler (which
    anchors every relative path on the spec's directory) works unchanged.
    """
    from . import prompts
    from .jobs import resolve_job
    from .spec import load_spec

    spec_path = args.spec if getattr(args, "spec", None) else resolve_job(getattr(args, "job", None))
    args.spec = spec_path
    spec = load_spec(spec_path)
    prompts.set_overrides(spec.prompts)
    if hasattr(args, "db") and args.db is None:
        # Keep each job self-contained: its db lives in the job workspace.
        args.db = spec.workspace_dir(spec_path) / "ghostlab.sqlite3"
    return spec


def _job_output_dir(args: argparse.Namespace, default_name: str = "runs") -> Path:
    """For run/run-dataset: honor an explicit --output-dir, else route into a
    --job's folder (seeding its prompt overrides + db), else fall back to ./runs.
    """
    if getattr(args, "output_dir", None) is not None:
        return args.output_dir
    if getattr(args, "job", None):
        from . import prompts
        from .jobs import resolve_job
        from .spec import load_spec

        spec_path = resolve_job(args.job)
        spec = load_spec(spec_path)
        prompts.set_overrides(spec.prompts)
        if hasattr(args, "db") and args.db is None:
            args.db = spec.workspace_dir(spec_path) / "ghostlab.sqlite3"
        return spec_path.parent / default_name
    return Path(default_name)


def _open_store(args: argparse.Namespace, workspace=None):
    """Open the persistence store, or None if it can't be opened (best-effort)."""
    from .storage import GhostlabStore

    try:
        return GhostlabStore.open(getattr(args, "db", None), workspace=workspace)
    except Exception as exc:  # noqa: BLE001
        print(f"  (persistence disabled: {exc})")
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ghostlab", description="Rehearsal / MCP Ghostlab.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    init_parser = sub.add_parser(
        "init", help="Create a ghostlab.yaml spec from an existing target JSON."
    )
    init_parser.add_argument(
        "--target", required=True, type=Path,
        help="Path to a GhostLab target JSON or a standard MCP config (mcpServers).",
    )
    _add_server_arg(init_parser)
    init_parser.add_argument(
        "--out", type=Path, default=Path("ghostlab.yaml"),
        help="Spec file to write (.yaml or .json; default: ghostlab.yaml).",
    )
    init_parser.add_argument("--name", default="", help="Human-readable name for the MCP under test.")
    init_parser.add_argument(
        "--workspace", default=None,
        help="Artifact workspace dir, relative to the spec (default: .ghostlab).",
    )
    init_parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing spec file."
    )

    create_parser = sub.add_parser(
        "create",
        help="End-to-end wizard: create a job, discover, configure semantic "
             "testing, generate a plan, run it, and show the results.",
    )
    create_parser.add_argument("--name", default=None, help="Job name (skips the name prompt).")
    create_parser.add_argument(
        "--target", default=None,
        help="Target MCP URL, or a path to a GhostLab target JSON or a standard "
             "MCP config (mcpServers). Skips the target prompt.",
    )
    _add_server_arg(create_parser)
    create_parser.add_argument(
        "--transport", default=None,
        help="MCP transport for a URL target (default: streamable-http).",
    )
    create_parser.add_argument(
        "--header", action="append", default=None, metavar="NAME: VALUE",
        help="Auth/other request header for a URL target (repeatable). Values may "
             "reference env vars, e.g. --header 'Authorization: Bearer ${GH_TOKEN}', "
             "which are expanded at connection time so no secret is written to job.yaml.",
    )
    create_parser.add_argument(
        "--aut-runner", type=Path, default=None,
        help="Runner JSON for the agent-under-test host (adds a process host).",
    )
    create_parser.add_argument(
        "--personas", type=int, default=None, help="Default personas to generate (default: 2)."
    )
    create_parser.add_argument(
        "--scenarios-per-persona", type=int, default=None,
        help="Default scenarios per persona (default: 2).",
    )
    create_parser.add_argument(
        "--min-pass-rate", type=float, default=None, help="Release gate min pass rate (default: 0.9)."
    )
    create_parser.add_argument(
        "--discover", action=argparse.BooleanOptionalAction, default=True,
        help="Run discover -> configure host -> generate plan -> pick suites -> "
             "test -> review, right after creating the job (default: on). "
             "--no-discover just scaffolds the job.",
    )
    create_parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Non-interactive: accept defaults for anything not passed as a flag "
             "(runs every generated suite, sets up codex if available).",
    )
    create_parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing job of the same name."
    )

    discover_parser = sub.add_parser(
        "discover",
        help="Inspect the job's target, lint its contract, and refresh capabilities.",
    )
    _add_job_args(discover_parser)
    discover_parser.add_argument(
        "--timeout", type=float, default=30.0, help="Per-request timeout in seconds."
    )
    discover_parser.add_argument(
        "--skip-apps", action="store_true",
        help="Skip probing ui:// resources even when UI-producing tools exist.",
    )
    discover_parser.add_argument(
        "--sample", choices=["off", "safe", "fixture"], default="off",
        help="Call tools once for real: 'safe' = read-only tools with generated "
             "args; 'fixture' also runs setup.fixtures entries (approval-gated).",
    )
    discover_parser.add_argument(
        "--approve-mutations", action="store_true",
        help="Allow fixture sampling of state-mutating (non-destructive) tools.",
    )
    discover_parser.add_argument(
        "--approve-destructive", action="store_true",
        help="Allow fixture sampling of destructive tools. Use with care.",
    )
    discover_parser.add_argument(
        "--skip-setup", action="store_true",
        help="Skip the spec's setup commands/health checks (target already running).",
    )
    discover_parser.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero when the spec's review gates fail (e.g. schema errors).",
    )
    _add_db_arg(discover_parser)

    plan_parser = sub.add_parser(
        "plan",
        help="Generate (or curate) a coverage-driven test plan from discover artifacts.",
    )
    _add_job_args(plan_parser)
    plan_parser.add_argument(
        "--out", type=Path, default=None,
        help="Plan file to write (default: test-plan.yaml next to the spec).",
    )
    plan_parser.add_argument(
        "--approve", nargs="*", default=None,
        help="Curate only: mark case ids approved (no ids = all cases).",
    )
    plan_parser.add_argument(
        "--reject", nargs="*", default=None,
        help="Curate only: mark case ids rejected (no ids = all cases).",
    )
    plan_parser.add_argument(
        "--generate", action=argparse.BooleanOptionalAction, default=True,
        help="Generate real persona-grounded scenarios for the semantic/security "
             "suites via codex (default: on). Pass --no-generate for a fast, "
             "free, deterministic-only plan.",
    )
    plan_parser.add_argument(
        "--regenerate", action="store_true",
        help="Force fresh persona/scenario generation even if a cached set exists "
             "(each persona and scenario is a codex call).",
    )
    plan_parser.add_argument(
        "--personas", type=int, default=None, help="Number of personas to generate (default: 2)."
    )
    plan_parser.add_argument(
        "--scenarios-per-persona", type=int, default=None,
        help="Scenarios generated per persona (default: 2).",
    )
    plan_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary (default: auto-detect)."
    )
    plan_parser.add_argument("--model", default="", help="Model override for codex.")

    test_parser = sub.add_parser(
        "test", help="Execute the test plan across the job's host adapters."
    )
    _add_job_args(test_parser)
    test_parser.add_argument(
        "--plan", type=Path, default=None,
        help="Plan file (default: test-plan.yaml next to the spec).",
    )
    test_parser.add_argument(
        "--suite", action="append", default=None,
        help="Only run these suite(s) (repeatable, e.g. --suite smoke --suite edge).",
    )
    test_parser.add_argument(
        "--hosts", default=None,
        help="Comma-separated host ids to use (default: all configured hosts).",
    )
    test_parser.add_argument(
        "--approved-only", action="store_true",
        help="Run only cases curated to status=approved.",
    )
    test_parser.add_argument(
        "--user-runner", type=Path, default=None,
        help="Runner JSON for the user-emulator session in conversational cases "
             "(default: a plain codex process with no MCP wired in — it must "
             "never share the agent-under-test's target-MCP config).",
    )
    test_parser.add_argument(
        "--apps-mode", action="store_true",
        help="MCP Apps host: render the ui:// widgets the agent opens in a headless "
             "browser and let the emulated user operate them for real — DOM actions "
             "fire real backend tools/calls and a Submit's follow-up flows back into "
             "the conversation. Requires the apps extra (pip install 'ghostlab[apps]').",
    )
    test_parser.add_argument(
        "--skip-setup", action="store_true",
        help="Skip the spec's setup commands/health checks (target already running).",
    )
    test_parser.add_argument(
        "--timeout", type=float, default=30.0, help="Per-request timeout in seconds."
    )
    test_parser.add_argument(
        "--repeat", type=int, default=1,
        help="Run the plan N times and report per-case variance / flaky cases.",
    )
    test_parser.add_argument(
        "--profile", choices=["smoke", "nightly", "release"], default=None,
        help="CI preset: smoke = smoke+edge suites; nightly = all suites; "
             "release = all suites, repeat 3, strict gates. Explicit flags override.",
    )
    test_parser.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero when review gates (e.g. min_pass_rate) fail.",
    )
    test_parser.add_argument(
        "--judge", action=argparse.BooleanOptionalAction, default=None,
        help="Score conversational runs with the codex judge + tool-usability "
             "critique (default: on). --no-judge runs conversations but grades "
             "pass/fail only by whether they finished.",
    )
    test_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary for judging (default: auto-detect)."
    )
    test_parser.add_argument("--model", default="", help="Model override for the codex judge.")

    review_spec_parser = sub.add_parser(
        "review",
        help="Readiness report over discover + plan + test artifacts (release gate).",
    )
    _add_job_args(review_spec_parser)
    review_spec_parser.add_argument(
        "--results", type=Path, default=None,
        help="Test results dir or results.json (default: latest under the workspace).",
    )
    review_spec_parser.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero unless the verdict is 'ready'.",
    )

    run_parser = sub.add_parser("run", help="Run a dual-agent E2E scenario.")
    run_parser.add_argument(
        "--target", required=True, type=Path,
        help="Path to a GhostLab target JSON or a standard MCP config (mcpServers).",
    )
    _add_server_arg(run_parser)
    run_parser.add_argument("--scenario", required=True, type=Path, help="Path to scenario JSON config.")
    run_parser.add_argument("--aut-runner", type=Path, help="Path to AUT runner JSON config.")
    run_parser.add_argument("--user-runner", type=Path, help="Path to user emulator runner JSON config.")
    run_parser.add_argument("--persona", type=Path, help="Optional persona JSON to drive the user emulator.")
    run_parser.add_argument(
        "--job", default=None,
        help="Route output + db into this job's folder (jobs/<name>/runs/) and use its prompt overrides.",
    )
    run_parser.add_argument("--output-dir", type=Path, default=None, help="Directory for logs and reports (default: the job's runs/ or ./runs).")
    _add_db_arg(run_parser)

    inspect_parser = sub.add_parser("inspect", help="Introspect a target MCP server.")
    inspect_parser.add_argument(
        "--target", required=True, type=Path,
        help="Path to a GhostLab target JSON or a standard MCP config (mcpServers).",
    )
    _add_server_arg(inspect_parser)
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
    rundataset_parser.add_argument(
        "--target", required=True, type=Path,
        help="Path to a GhostLab target JSON or a standard MCP config (mcpServers).",
    )
    _add_server_arg(rundataset_parser)
    rundataset_parser.add_argument("--aut-runner", type=Path, help="Path to AUT runner JSON config.")
    rundataset_parser.add_argument("--user-runner", type=Path, help="Path to user emulator runner JSON config.")
    rundataset_parser.add_argument(
        "--job", default=None,
        help="Route output + db into this job's folder (jobs/<name>/runs/) and use its prompt overrides.",
    )
    rundataset_parser.add_argument(
        "--output-dir", type=Path, default=None, help="Directory for per-case runs and summary (default: the job's runs/ or ./runs)."
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
    apps_parser.add_argument(
        "--target", required=True, type=Path,
        help="Path to a GhostLab target JSON or a standard MCP config (mcpServers).",
    )
    _add_server_arg(apps_parser)
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
    render_parser.add_argument(
        "--target", required=True, type=Path,
        help="Path to a GhostLab target JSON or a standard MCP config (mcpServers).",
    )
    _add_server_arg(render_parser)
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

    dashboard_parser = sub.add_parser(
        "dashboard", help="Build a standalone HTML dashboard for a `ghostlab test` run."
    )
    dashboard_parser.add_argument(
        "run_dir",
        type=Path,
        help="A test-run directory (containing results.json), e.g. .ghostlab/test/<timestamp>-<id>.",
    )
    dashboard_parser.add_argument(
        "--open", action="store_true", help="Open the dashboard in the default browser when done."
    )

    db_parser = sub.add_parser("db", help="Manage the SQLite persistence database.")
    db_parser.add_argument(
        "action", choices=["init", "verify"], help="init: apply migrations. verify: integrity check."
    )
    _add_db_arg(db_parser)
    return parser


def cmd_init(args: argparse.Namespace) -> int:
    from .spec import DEFAULT_WORKSPACE, save_spec, spec_from_target

    target = load_target(args.target, args.server)
    if args.out.exists() and not args.force:
        print(f"{args.out} already exists; use --force to overwrite.")
        return 1
    spec = spec_from_target(
        target,
        source_target=str(args.target),
        name=args.name,
        workspace=args.workspace or DEFAULT_WORKSPACE,
    )
    path = save_spec(spec, args.out)
    print(f"Initialized spec for '{spec.id}' at {path}")
    print(f"  transport={target.transport} workspace={spec.workspace}")
    print("  next: ghostlab discover --spec " + str(path))
    return 0


def _parse_header_lines(lines: list[str]) -> dict[str, str]:
    """Parse `Name: Value` header strings into a dict (malformed ones warned)."""
    headers: dict[str, str] = {}
    for line in lines:
        field, sep, value = line.partition(":")
        if not sep:
            print(f"  (ignoring malformed --header {line!r}; expected 'Name: Value')")
            continue
        headers[field.strip()] = value.strip()
    return headers


def _discover_new_job(slug: str) -> int:
    """Run `discover` on a freshly created job (reuses the discover handler)."""
    disc_args = argparse.Namespace(
        job=slug, spec=None, db=None, skip_setup=False, timeout=30.0,
        sample="off", approve_mutations=False, approve_destructive=False,
        skip_apps=False, strict=False,
    )
    return cmd_discover(disc_args)


def cmd_create(args: argparse.Namespace) -> int:
    from .config import load_target
    from .jobs import create_job, default_job_spec, slugify, target_from_url

    interactive = not args.yes

    def ask(prompt: str, default: str = "") -> str:
        if not interactive:
            return default
        try:
            raw = input(prompt).strip()
        except EOFError:
            return default
        return raw or default

    def ask_yn(prompt: str, default: bool) -> bool:
        raw = ask(f"{prompt} [{'Y/n' if default else 'y/N'}] ", "y" if default else "n")
        return raw.strip().lower() not in ("n", "no")

    # Only the two things GhostLab can't infer are ever prompted for; everything
    # else comes from flags or documented defaults (all editable in job.yaml).
    name = args.name or ask("Job name: ")
    if not name:
        print("A job name is required (pass --name or answer the prompt).")
        return 1

    target_value = args.target or ask("Target MCP URL or config path: ")
    if not target_value:
        print("A target is required (pass --target or answer the prompt).")
        return 1

    source_target = ""
    target_path = Path(target_value)
    is_config_file = target_path.suffix.lower() == ".json" and target_path.exists()
    if is_config_file:
        # A config file already carries transport + headers/env — don't re-ask.
        try:
            target = load_target(target_path, server=args.server)
        except ConfigError as exc:
            print(str(exc))
            return 1
        source_target = str(target_path)
    else:
        target = target_from_url(
            target_value,
            transport=args.transport or "streamable-http",
            headers=_parse_header_lines(list(args.header or [])),
        )

    generation: dict = {}
    if args.personas is not None:
        generation["personas"] = args.personas
    if args.scenarios_per_persona is not None:
        generation["scenarios_per_persona"] = args.scenarios_per_persona
    review_gates = {"min_pass_rate": args.min_pass_rate} if args.min_pass_rate is not None else None

    spec = default_job_spec(
        name,
        target=target,
        source_target=source_target,
        generation=generation,
        review_gates=review_gates,
        aut_runner=str(args.aut_runner) if args.aut_runner else None,
    )
    try:
        spec_path = create_job(name, spec, force=args.force)
    except ConfigError as exc:
        print(str(exc))
        return 1

    slug = slugify(name)
    job_dir = spec_path.parent
    print(tc.heading(f"Created job '{slug}' at {job_dir}/  (job.yaml + workspace/ + runs/)"))
    print(f"  target: {target.transport} {target.connection.get('url') or target.connection.get('command') or ''}")

    if not args.discover:
        print(tc.muted(f"  next: ghostlab discover --job {slug}"))
        return 0

    # Inspect the target right away so the job is validated + capabilities are
    # populated — especially the point when a config file was handed in.
    print()
    try:
        rc = _discover_new_job(slug)
    except Exception as exc:  # noqa: BLE001 — never lose the created job over a bad target
        print(tc.verdict(f"  discovery failed: {exc}", "fail"))
        rc = 1
    if rc != 0:
        print(tc.muted(f"  job created; fix the target/auth then: ghostlab discover --job {slug}"))
        return 0

    _configure_aut_host(spec_path, ask_yn)

    print()
    plan_args = argparse.Namespace(
        job=slug, spec=None, db=None, out=None, approve=None, reject=None,
        generate=True, regenerate=False,
        personas=args.personas, scenarios_per_persona=args.scenarios_per_persona,
        codex_bin="", model="",
    )
    if cmd_plan(plan_args) != 0:
        print(tc.muted(f"  job created; fix the plan then: ghostlab plan --job {slug}"))
        return 0

    from .plan import load_test_plan

    plan = load_test_plan(job_dir / "test-plan.yaml")
    suite_names = [name for name, entry in plan["suites"].items() if entry["cases"]]
    chosen_suites = None
    if suite_names:
        raw = ask(f"\nRun which suites? [all: {', '.join(suite_names)}] ", "all")
        if raw.strip().lower() not in ("", "all"):
            picked = [s.strip() for s in raw.split(",") if s.strip()]
            unknown = [s for s in picked if s not in suite_names]
            if unknown:
                print(f"  (ignoring unknown suite(s): {', '.join(unknown)})")
            chosen_suites = [s for s in picked if s in suite_names] or None

    print()
    test_args = argparse.Namespace(
        job=slug, spec=None, db=None, plan=None, suite=chosen_suites, hosts=None,
        approved_only=False, user_runner=None, apps_mode=False, skip_setup=False,
        timeout=30.0, repeat=1, profile=None, strict=False, judge=None,
        codex_bin="", model="",
    )
    cmd_test(test_args)

    print()
    review_args = argparse.Namespace(job=slug, spec=None, db=None, results=None, strict=False)
    cmd_review_spec(review_args)

    print()
    print(tc.muted(
        f"  rerun anytime: ghostlab test --job {slug}   |   "
        f"gate report: ghostlab review --job {slug}"
    ))
    return 0


def _configure_aut_host(spec_path: Path, ask_yn) -> None:
    """Offer to wire an agent-under-test host so semantic/security suites run.

    A fresh job has no host capable of executing conversational cases, so they
    silently skip in `ghostlab test` until one is configured. Codex is the only
    auto-detected/auto-wired backend today; anything else stays a manual
    `--aut-runner` / `hosts:` edit.
    """
    from .codex_backend import CodexError, resolve_codex_bin
    from .jobs import add_aut_host, build_codex_aut_runner
    from .spec import load_spec

    spec = load_spec(spec_path)  # reload: discover just updated `capabilities`
    if any(h.get("kind") in ("process", "codex-session") for h in spec.hosts):
        return  # --aut-runner (or a hand-edited job.yaml) already set one up

    try:
        resolve_codex_bin()
    except CodexError:
        print(tc.muted(
            "  codex not found — semantic/security suites will skip until an "
            "agent-under-test host is configured (see README)."
        ))
        return

    if not ask_yn("\nSet up semantic/E2E testing with codex?", True):
        print(tc.muted("  skipping — semantic/security suites will skip for now."))
        return

    runner_config = build_codex_aut_runner(spec)
    runner_path = add_aut_host(spec, spec_path, runner_config)
    print(f"  wrote {runner_path}")
    print(f"  updated {spec_path} (hosts: aut)")


def cmd_discover(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from .setup_runtime import SetupError, SetupRuntime

    spec = _job_spec(args)
    target = spec.target_config()
    timestamp = utc_now().replace("+00:00", "Z").replace(":", "")
    out_dir = spec.workspace_dir(args.spec) / "discover" / f"{timestamp}-{spec.id}"

    print(tc.heading(f"Discovering '{spec.id}' ({target.transport})..."))
    runtime = SetupRuntime({} if args.skip_setup else spec.setup, out_dir)
    try:
        if runtime.declared:
            try:
                runtime.start()
            except SetupError as exc:
                print(f"  setup failed: {exc}")
                print(f"  see {runtime.log_path}")
                runtime.write_status()
                return 1
            healthy = runtime.wait_healthy()
            checks = runtime.status()["health"]
            if checks:
                print(f"  health: {sum(1 for c in checks if c['ok'])}/{len(checks)} check(s) ok")
            if not healthy:
                print("  target is not healthy; aborting discover")
                print(f"  see {runtime.log_path}")
                runtime.write_status()
                return 1
        return _discover_inspect(args, spec, target, out_dir, runtime)
    finally:
        runtime.teardown()
        if runtime.declared:
            runtime.write_status()  # capture teardown results in setup.json


def _discover_inspect(args, spec, target, out_dir: Path, runtime) -> int:
    """Inspect + contract + sampling + apps probe, once the target is up."""
    from dataclasses import asdict

    from .contract import build_contract, merge_findings, render_contract_md
    from .spec import save_spec

    result = inspect_target(target, timeout=args.timeout)
    inspect_json, inspect_md = write_inspect_artifacts(result, out_dir)

    contract = build_contract(asdict(result))
    sample_report = None
    if args.sample != "off":
        sample_report = _sample_tools_for_discover(
            args, spec, target, contract, result.tools, out_dir, runtime
        )
        if sample_report is not None:
            merge_findings(contract, sample_report["findings"])

    contract_json = out_dir / "contract.json"
    contract_md = out_dir / "contract.md"
    contract_json.write_text(
        json.dumps(contract, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    contract_md.write_text(render_contract_md(contract), encoding="utf-8")
    runtime.write_status(result.server_info)

    apps_report = None
    ui_tool_count = contract["counts"]["ui_tools"]
    if ui_tool_count and not args.skip_apps:
        apps_report = _probe_apps_for_discover(target, result.tools, out_dir, args.timeout)

    _update_spec_capabilities(spec, contract, contract_json, args.spec)
    save_spec(spec, args.spec)

    severities = contract["summary"]["findings_by_severity"]
    server = result.server_info or {}
    print(f"  server {server.get('name', '?')}@{server.get('version', '?')}")
    print(
        f"  tools={contract['counts']['tools']} (ui={ui_tool_count}) "
        f"resources={contract['counts']['resources']} prompts={contract['counts']['prompts']}"
    )
    error_status = "fail" if severities["error"] else "pass"
    print(
        "  contract findings: "
        + tc.verdict(f"{severities['error']} error(s)", error_status)
        + f", {severities['warning']} warning(s), {severities['info']} info"
    )
    for finding in contract["findings"]:
        if finding["severity"] == "error":
            print(tc.verdict(f"  ! [{finding['kind']}] {finding['in']}: {finding['message']}", "fail"))
    if apps_report is not None:
        summary = apps_report["summary"]
        print(
            f"  apps: {summary['renderable_resources']}/{summary['ui_tools']} renderable, "
            f"{summary['diagnostic_findings']} finding(s)"
        )
    if sample_report is not None:
        sample_summary = sample_report["summary"]
        print(
            f"  samples: {sample_summary['called']} called "
            f"({sample_summary['failed']} failed), {sample_summary['skipped']} skipped"
        )
        for sample in sample_report["samples"]:
            if sample["status"] in ("error", "tool_error"):
                detail = sample.get("error") or sample.get("result", {}).get("first_text", "")
                print(f"  ! sample {sample['tool']}: {sample['status']} — {detail[:160]}")
    print(f"  wrote {inspect_json}")
    print(f"  wrote {contract_json}")
    print(f"  wrote {contract_md}")
    print(f"  updated {args.spec} (capabilities)")

    store = _open_store(args)
    if store is not None:
        try:
            info = store.record_inspection(target, result)
            public_id = info["inspection_public_id"]
            store.index_artifact("inspection", public_id, "inspect.json", inspect_json)
            store.index_artifact("inspection", public_id, "inspect.md", inspect_md)
            store.index_artifact("inspection", public_id, "contract.json", contract_json)
            store.index_artifact("inspection", public_id, "contract.md", contract_md)
            for extra in ("setup.json", "samples.json", "apps-probe.json"):
                extra_path = out_dir / extra
                if extra_path.exists():
                    store.index_artifact("inspection", public_id, extra, extra_path)
            print(f"  saved as version v{info['version']} ({public_id})")
        finally:
            store.close()

    gates = (spec.review or {}).get("gates", {})
    if args.strict and gates.get("no_tool_schema_errors") and severities["error"]:
        print(f"  GATE FAILED: no_tool_schema_errors ({severities['error']} error finding(s))")
        return 1
    return 0


def _sample_tools_for_discover(args, spec, target, contract, tools, out_dir: Path, runtime):
    """Live tool sampling during discover (best-effort, safety-gated)."""
    from .mcp_client import McpClientError, create_client
    from .sampling import plan_samples, run_samples

    plan = plan_samples(
        contract,
        tools,
        mode=args.sample,
        fixtures=(spec.setup or {}).get("fixtures"),
        approve_mutations=args.approve_mutations,
        approve_destructive=args.approve_destructive,
    )
    if not plan:
        return None

    mutated = any("skipped" not in entry and entry["source"] == "fixture" for entry in plan)
    try:
        client = create_client(target, timeout=args.timeout)
        try:
            client.initialize()
            report = run_samples(client, plan, tools)
            # Fixture samples may have written state; restore it while we still
            # hold a connected client for `tool`-type reset hooks.
            if mutated and (spec.setup or {}).get("reset"):
                if not runtime.run_reset(client):
                    report["findings"].append({
                        "kind": "reset_failed", "severity": "error", "in": "setup:reset",
                        "message": "state reset failed after fixture sampling; "
                                   "the target may be left dirty",
                    })
        finally:
            client.close()
    except McpClientError as exc:
        print(f"  (sampling skipped: {exc})")
        return None

    (out_dir / "samples.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report


def _probe_apps_for_discover(target, tools, out_dir: Path, timeout: float):
    """Fetch + diagnose ui:// resources during discover (best-effort)."""
    from .mcp_apps import build_app_report, probe_ui_tools, render_app_report_md
    from .mcp_client import McpClientError, create_client

    try:
        client = create_client(target, timeout=timeout)
        try:
            client.initialize()
            probes = probe_ui_tools(client, tools)
        finally:
            client.close()
    except McpClientError as exc:
        print(f"  (apps probe skipped: {exc})")
        return None

    report = build_app_report(target.id, probes)
    (out_dir / "apps-probe.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out_dir / "apps-probe.md").write_text(render_app_report_md(report), encoding="utf-8")
    return report


def _update_spec_capabilities(spec, contract, contract_path: Path, spec_path: Path) -> None:
    """Refresh the spec's `capabilities` section from a freshly built contract."""
    try:
        generated_from = str(contract_path.resolve().relative_to(spec_path.resolve().parent))
    except ValueError:  # workspace outside the spec's directory
        generated_from = str(contract_path)
    spec.capabilities = {
        "generated_from": generated_from,
        "discovered_at": contract["generated_at"],
        "mcp": contract["mcp"],
        "tools": [
            {
                "name": entry["name"],
                "labels": entry["risk"]["labels"],
                "produces_ui": entry["risk"]["produces_ui"],
            }
            for entry in contract["tools"]
        ],
        "ui_resources": sorted(
            {entry["ui_resource"] for entry in contract["tools"] if entry["ui_resource"]}
        ),
    }


def _get_generated_cases(args: argparse.Namespace, spec, inspect_data: dict) -> list | None:
    """Reuse a cached persona/scenario dataset, or generate a fresh one.

    Each persona and scenario is a real codex call, so a previously written
    dataset is reused by default (tracked in spec.test_plan.generated_dataset);
    --regenerate forces a fresh one. Falls back to None (deterministic-only
    plan) on any codex failure rather than aborting `plan` entirely.
    """
    from .codex_backend import CodexBackend, CodexError
    from .plan_generate import (
        DEFAULT_N_PERSONAS,
        DEFAULT_SCENARIOS_PER_PERSONA,
        generate_conversational_dataset,
        generation_dir_name,
        load_generated_cases,
        write_conversational_dataset,
    )

    spec_dir = args.spec.resolve().parent
    cached = (spec.test_plan or {}).get("generated_dataset")
    if cached and not args.regenerate:
        cached_dir = spec_dir / cached
        cases = load_generated_cases(cached_dir)
        if cases:
            print(f"  reusing generated scenarios from {cached_dir} (--regenerate to refresh)")
            return cases

    n_personas = args.personas or DEFAULT_N_PERSONAS
    scenarios_per_persona = args.scenarios_per_persona or DEFAULT_SCENARIOS_PER_PERSONA
    backend = CodexBackend(bin_path=args.codex_bin, model=args.model)
    print(
        f"  generating {n_personas} persona(s) x {scenarios_per_persona} scenario(s) "
        f"with codex ({backend._bin()})..."
    )

    def progress(event: dict) -> None:
        print(f"    [{event['phase']}] {event['message']} ({event['completed']}/{event['total']})")

    try:
        dataset = generate_conversational_dataset(
            inspect_data, backend, spec_id=spec.id,
            n_personas=n_personas, scenarios_per_persona=scenarios_per_persona,
            progress=progress,
        )
    except CodexError as exc:
        print(f"  generation skipped (codex error): {exc}")
        return None

    out_dir = spec.workspace_dir(args.spec) / "generated" / f"{generation_dir_name()}-{spec.id}"
    write_conversational_dataset(dataset, out_dir)
    try:
        relative = str(out_dir.resolve().relative_to(spec_dir.resolve()))
    except ValueError:
        relative = str(out_dir)
    spec.test_plan = {**(spec.test_plan or {}), "generated_dataset": relative}
    print(f"  wrote generated dataset to {out_dir}")
    return load_generated_cases(out_dir)


def cmd_plan(args: argparse.Namespace) -> int:
    from .plan import (
        build_test_plan,
        load_test_plan,
        render_plan_md,
        set_case_statuses,
        write_test_plan,
    )
    from .spec import save_spec

    spec = _job_spec(args)
    # Editable defaults: explicit flag -> spec.generation -> code default.
    gen = spec.generation or {}
    if args.personas is None:
        args.personas = gen.get("personas")
    if args.scenarios_per_persona is None:
        args.scenarios_per_persona = gen.get("scenarios_per_persona")
    if not args.model:
        args.model = gen.get("model", "") or ""
    if not args.codex_bin:
        args.codex_bin = gen.get("codex_bin", "") or ""
    if not args.regenerate:
        args.regenerate = bool(gen.get("regenerate", False))
    plan_path = args.out or args.spec.resolve().parent / "test-plan.yaml"

    # Curation-only mode: --approve / --reject touch statuses, no regeneration.
    if args.approve is not None or args.reject is not None:
        if not plan_path.exists():
            raise ConfigError(f"No plan to curate at {plan_path}; run `ghostlab plan` first.")
        plan = load_test_plan(plan_path)
        for status, ids in (("approved", args.approve), ("rejected", args.reject)):
            if ids is None:
                continue
            updated = set_case_statuses(plan, set(ids), status)
            print(f"Marked {len(updated)} case(s) {status}.")
        write_test_plan(plan, plan_path)
        print(f"  wrote {plan_path}")
        return 0

    generated_from = (spec.capabilities or {}).get("generated_from", "")
    if not generated_from:
        raise ConfigError(
            f"Spec {args.spec} has no discovered capabilities; run `ghostlab discover` first."
        )
    discover_dir = (args.spec.resolve().parent / generated_from).parent
    contract_path = discover_dir / "contract.json"
    inspect_path = discover_dir / "inspect.json"
    for required in (contract_path, inspect_path):
        if not required.exists():
            raise ConfigError(f"Missing discover artifact: {required}; re-run `ghostlab discover`.")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    inspect_data = json.loads(inspect_path.read_text(encoding="utf-8"))
    samples = None
    samples_path = discover_dir / "samples.json"
    if samples_path.exists():
        samples = json.loads(samples_path.read_text(encoding="utf-8"))

    prior_plan = load_test_plan(plan_path) if plan_path.exists() else None
    generated_cases = None
    if args.generate:
        generated_cases = _get_generated_cases(args, spec, inspect_data)

    plan = build_test_plan(
        spec.id,
        contract,
        inspect_data.get("tools", []),
        hosts=spec.hosts,
        samples=samples,
        prior_plan=prior_plan,
        contract_ref=generated_from,
        fixtures=(spec.setup or {}).get("fixtures"),
        generated_cases=generated_cases,
    )
    write_test_plan(plan, plan_path)
    md_path = plan_path.with_suffix(".md")
    md_path.write_text(render_plan_md(plan), encoding="utf-8")

    spec.test_plan = {
        **(spec.test_plan or {}),  # preserve generated_dataset set by _get_generated_cases
        "plan_file": plan_path.name,
        "generated_at": plan["generated_at"],
        "cases": len(plan["cases"]),
        "suites": {suite: entry["cases"] for suite, entry in plan["suites"].items() if entry["cases"]},
    }
    save_spec(spec, args.spec)

    print(tc.heading(f"Planned {len(plan['cases'])} case(s) for '{spec.id}'"))
    e2e_suites = ("semantic", "security", "error-recovery", "apps", "host-compat", "regression")
    for suite in e2e_suites:
        entry = plan["suites"].get(suite)
        if entry and entry["cases"]:
            print(f"  {suite}: {entry['cases']}")
    protocol_total = sum(
        entry["cases"] for suite, entry in plan["suites"].items()
        if suite not in e2e_suites and entry["cases"]
    )
    if protocol_total:
        print(tc.muted(f"  + {protocol_total} protocol case(s) (smoke/edge)"))
    gaps = plan["coverage"]["gaps"]
    if gaps:
        print(f"  coverage gaps: {len(gaps)}")
        for gap in gaps[:5]:
            print(f"  ! {gap}")
    for note in plan["notes"]:
        print(tc.muted(f"  note: {note}"))
    print(f"  wrote {plan_path}")
    print(f"  wrote {md_path}")
    print(f"  updated {args.spec} (test_plan)")
    print(tc.muted("  next: review statuses, then `ghostlab plan --approve` to approve all"))
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    from .hosts import build_hosts
    from .plan import load_test_plan
    from .setup_runtime import SetupError, SetupRuntime
    from .testrun import evaluate_gates, execute_plan_repeated, render_results_md

    # CI profile presets; explicit flags win (and a profile beats job.yaml too,
    # so this runs before the spec-default resolution below).
    if args.profile == "smoke" and not args.suite:
        args.suite = ["smoke", "edge"]
    elif args.profile == "release":
        if args.repeat == 1:
            args.repeat = 3
        args.strict = True

    spec = _job_spec(args)
    # Editable defaults: explicit flag (or profile) -> spec.test/generation -> code default.
    t = spec.test or {}
    if args.suite is None and t.get("suites"):
        args.suite = list(t["suites"])
    if args.judge is None:
        args.judge = bool(t.get("judge", True))
    if not args.apps_mode and t.get("apps_mode"):
        args.apps_mode = True
    if not args.approved_only and t.get("approved_only"):
        args.approved_only = True
    if args.user_runner is None and t.get("user_runner"):
        args.user_runner = Path(t["user_runner"])
    if args.repeat == 1 and t.get("repeat"):
        args.repeat = int(t["repeat"])
    if args.timeout == 30.0 and t.get("timeout") is not None:
        args.timeout = float(t["timeout"])
    gen = spec.generation or {}
    if not args.model:
        args.model = gen.get("model", "") or ""
    if not args.codex_bin:
        args.codex_bin = gen.get("codex_bin", "") or ""
    plan_path = args.plan or args.spec.resolve().parent / "test-plan.yaml"
    if not plan_path.exists():
        raise ConfigError(f"No plan at {plan_path}; run `ghostlab plan --spec {args.spec}` first.")
    plan = load_test_plan(plan_path)

    backend = None
    if args.judge:
        from .codex_backend import CodexBackend, CodexError

        try:
            candidate = CodexBackend(bin_path=args.codex_bin, model=args.model)
            candidate._bin()  # resolve now so a missing codex degrades gracefully
            backend = candidate
        except CodexError as exc:
            print(f"  (judge disabled: {exc})")

    if args.user_runner is not None:
        user_runner_config = load_runner(args.user_runner)
    else:
        # Zero-config default: a plain codex session with no MCP wired in, so
        # it plays a human, never another tool-using agent. Mirrors
        # runners/codex-user-emulator.json.
        user_runner_config = RunnerConfig(
            kind="process",
            command=["codex", "--sandbox", "read-only", "-a", "never",
                    "exec", "--skip-git-repo-check", "-"],
            timeout_seconds=600,
            prompt_mode="stdin",
            parser="text",
        )
    hosts = build_hosts(
        spec, args.spec, timeout=args.timeout, backend=backend,
        user_runner_config=user_runner_config,
        apps_mode=args.apps_mode,
    )
    if args.hosts:
        wanted = {part.strip() for part in args.hosts.split(",") if part.strip()}
        unknown = wanted - {host.id for host in hosts}
        if unknown:
            raise ConfigError(f"Unknown host id(s): {', '.join(sorted(unknown))}")
        hosts = [host for host in hosts if host.id in wanted]

    timestamp = utc_now().replace("+00:00", "Z").replace(":", "")
    out_dir = spec.workspace_dir(args.spec) / "test" / f"{timestamp}-{spec.id}"
    print(tc.heading(
        f"Testing '{spec.id}' with host(s): {', '.join(host.id for host in hosts)}"
        + (f" (suites: {', '.join(args.suite)})" if args.suite else "")
    ))

    def progress(line: str) -> None:
        match = re.match(r"^(\s*)-> (pass|fail|error|skip)\b(.*)$", line)
        if match:
            indent, status, rest = match.groups()
            print(f"{indent}-> {tc.verdict(status, status)}{rest}")
        elif ": skip (" in line:
            print(tc.muted(line))
        elif line.startswith("==="):
            print(tc.heading(line))
        elif line.endswith("..."):
            print(tc.muted(line))
        else:
            print(line)

    runtime = SetupRuntime({} if args.skip_setup else spec.setup, out_dir)
    try:
        if runtime.declared:
            try:
                runtime.start()
            except SetupError as exc:
                print(tc.verdict(f"  setup failed: {exc}", "fail"))
                runtime.write_status()
                return 1
            if not runtime.wait_healthy():
                print(tc.verdict("  target is not healthy; aborting test run", "fail"))
                runtime.write_status()
                return 1
        results = execute_plan_repeated(
            plan, hosts, out_dir,
            repeat=max(1, args.repeat),
            suites=args.suite,
            approved_only=args.approved_only,
            progress=progress,
        )
    finally:
        runtime.teardown()
        if runtime.declared:
            runtime.write_status()

    results_json = out_dir / "results.json"
    results_md = out_dir / "results.md"
    results_json.write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    results_md.write_text(render_results_md(results), encoding="utf-8")

    totals = results["totals"]
    rate = results["pass_rate"]
    print(tc.heading(
        f"  executed {results['executed']} case-run(s): "
        f"{totals['pass']} pass, {totals['fail']} fail, {totals['error']} error "
        f"({totals['skip']} skipped)"
    ))
    rate_status = "pass" if rate is not None and rate >= 0.9 else ("fail" if rate is not None else "skip")
    print(tc.verdict(f"  pass rate: {'n/a' if rate is None else f'{rate:.0%}'}", rate_status)
          + (f" across {results['attempts']} attempts" if results.get("attempts") else ""))
    reported: set[str] = set()
    for entry in results["results"]:
        if entry["status"] in ("fail", "error") and entry["case"] not in reported:
            reported.add(entry["case"])
            print(tc.verdict(
                f"  ! {entry['case']} [{entry['host']}] {entry['status']}: {entry.get('detail', '')}",
                entry["status"],
            ))
    flaky = results.get("variance", {}).get("flaky_cases", [])
    if flaky:
        print(tc.verdict(f"  FLAKY: {', '.join(flaky)}", "partial"))
    if results.get("variance"):
        variance_path = out_dir / "variance.json"
        variance_path.write_text(
            json.dumps(results["variance"], indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"  wrote {variance_path}")
    print(f"  wrote {results_json}")
    print(f"  wrote {results_md}")
    try:
        from .dashboard import build_dashboard

        dashboard_path = build_dashboard(out_dir)
        print(f"  wrote {dashboard_path}")
    except Exception as exc:  # noqa: BLE001 — dashboard is a convenience, never fail the run
        print(f"  (dashboard skipped: {exc})")

    gate_failures = evaluate_gates(results, (spec.review or {}).get("gates", {}))
    for failure in gate_failures:
        print(tc.verdict(f"  GATE FAILED: {failure}", "fail"))
    if args.strict and gate_failures:
        return 1
    return 0


def cmd_review_spec(args: argparse.Namespace) -> int:
    from .plan import load_test_plan
    from .readiness import build_readiness, render_readiness_md

    spec = _job_spec(args)
    spec_dir = args.spec.resolve().parent

    contract = None
    generated_from = (spec.capabilities or {}).get("generated_from", "")
    if generated_from and (spec_dir / generated_from).exists():
        contract = json.loads((spec_dir / generated_from).read_text(encoding="utf-8"))

    plan = None
    plan_path = spec_dir / (spec.test_plan or {}).get("plan_file", "test-plan.yaml")
    if plan_path.exists():
        plan = load_test_plan(plan_path)

    results = None
    results_file: Path | None = None
    if args.results:
        results_file = args.results / "results.json" if args.results.is_dir() else args.results
        if not results_file.exists():
            raise ConfigError(f"results.json not found: {results_file}")
    else:
        candidates = sorted(spec.workspace_dir(args.spec).glob("test/*/results.json"))
        results_file = candidates[-1] if candidates else None
    critiques: list[dict] = []
    if results_file is not None:
        results = json.loads(results_file.read_text(encoding="utf-8"))
        for entry in results.get("results", []):
            critique_path = entry.get("artifacts", {}).get("critique")
            if not critique_path:
                continue
            try:
                critiques.append(json.loads(Path(critique_path).read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass  # a missing/corrupt critique file just drops out of the aggregate

    readiness = build_readiness(
        spec.id,
        (spec.review or {}).get("gates", {}),
        contract=contract,
        plan=plan,
        results=results,
        critiques=critiques,
    )

    out_dir = results_file.parent if results_file is not None else spec.workspace_dir(args.spec)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "readiness.json"
    md_path = out_dir / "readiness.md"
    json_path.write_text(
        json.dumps(readiness, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_readiness_md(readiness), encoding="utf-8")

    verdict_status = {"ready": "pass", "needs-work": "partial", "not-ready": "fail"}[readiness["verdict"]]
    print(tc.heading(f"Readiness for '{spec.id}': ") + tc.verdict(readiness["verdict"].upper(), verdict_status))
    for gate in readiness["gates"]:
        marker = {"pass": "ok", "fail": "!!", "not-evaluated": "--"}[gate["status"]]
        print(tc.verdict(f"  [{marker}] {gate['gate']}: {gate['detail']}", gate["status"]))
    if readiness["failures"]:
        print(tc.verdict(f"  failure clusters: {len(readiness['failures'])}", "fail"))
        for cluster in readiness["failures"][:3]:
            print(tc.verdict(f"  ! {cluster['category']} x{cluster['count']}: {cluster['signature']}", "fail"))
    if readiness["repairs"]:
        top = readiness["repairs"][0]
        print(f"  repairs: {len(readiness['repairs'])} (start with P{top['priority']} {top['kind']})")
    feedback = readiness.get("mcp_feedback")
    if feedback:
        avg = feedback.get("avg_overall_score")
        print(
            f"  mcp feedback: {feedback['runs_critiqued']} run(s) critiqued"
            + (f", avg tool-ergonomics {avg}/5" if avg is not None else "")
        )
        if feedback["top_recommendations"]:
            print(f"  -> {feedback['top_recommendations'][0]}")
    for note in readiness["coverage_notes"][:3]:
        print(f"  note: {note}")
    print(f"  wrote {json_path}")
    print(f"  wrote {md_path}")

    if args.strict and readiness["verdict"] != "ready":
        return 1
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    target = load_target(args.target, args.server)
    scenario = load_scenario(args.scenario)
    aut_runner = load_runner(args.aut_runner)
    user_runner = load_runner(args.user_runner)
    persona = load_persona(args.persona) if args.persona else None
    output_dir = _job_output_dir(args)
    store = _open_store(args)
    try:
        result = run_scenario(
            target=target,
            scenario=scenario,
            aut_runner_config=aut_runner,
            user_runner_config=user_runner,
            output_dir=output_dir,
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
    target = load_target(args.target, args.server)
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

    output_dir = _job_output_dir(args)
    store = _open_store(args)
    try:
        summary_path = run_dataset(
            args.dataset,
            target_path=args.target,
            aut_runner_path=args.aut_runner,
            user_runner_path=args.user_runner,
            output_dir=output_dir,
            limit=args.limit,
            approved_only=args.approved_only,
            evaluate=args.evaluate,
            capabilities=capabilities,
            backend=backend,
            store=store,
            server=args.server,
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

    target = load_target(args.target, args.server)
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

    target = load_target(args.target, args.server)
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


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import build_dashboard

    run_dir = args.run_dir
    if not (run_dir / "results.json").exists():
        raise ConfigError(
            f"No results.json in {run_dir}; pass a `ghostlab test` run directory."
        )
    path = build_dashboard(run_dir)
    print(f"Wrote {path}")
    if args.open:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())
    return 0


_HANDLERS.update(
    {
        "init": cmd_init,
        "create": cmd_create,
        "dashboard": cmd_dashboard,
        "discover": cmd_discover,
        "plan": cmd_plan,
        "test": cmd_test,
        "review": cmd_review_spec,
        "run": cmd_run,
        "inspect": cmd_inspect,
        "profile": cmd_profile,
        "generate-scenarios": cmd_generate_scenarios,
        "generate-personas": cmd_generate_personas,
        "generate-dataset": cmd_generate_dataset,
        "run-dataset": cmd_run_dataset,
        "review-dataset": cmd_review_dataset,
        "doctor": cmd_doctor,
        "evaluate": cmd_evaluate,
        "critique": cmd_critique,
        "compare": cmd_compare,
        "scorecard": cmd_scorecard,
        "apps-probe": cmd_apps_probe,
        "apps-render": cmd_apps_render,
        "db": cmd_db,
        "ui": cmd_ui,
    }
)
KNOWN_COMMANDS = frozenset(_HANDLERS)


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
    handler = _HANDLERS.get(args.command or "")
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except ConfigError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
