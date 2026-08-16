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


# Agent CLIs echo the prompt back on failure, so a raw backend error can be
# thousands of lines of our own text with the real cause buried at the end.
_BACKEND_ERROR_SIGNALS = (
    "error", "failed", "not found", "unauthorized", "forbidden", "quota",
    "rate limit", "usage limit", "timed out", "unsupported", "invalid",
    "requires a newer version", "not supported",
)


def _backend_error_summary(exc: Exception, max_lines: int = 3) -> str:
    """Condense an LLM-backend failure to the lines that actually explain it."""
    text = str(exc)
    headline = text.split("\n", 1)[0].strip()
    interesting = [
        " ".join(line.split())
        for line in text.splitlines()
        if any(signal in line.lower() for signal in _BACKEND_ERROR_SIGNALS)
    ]
    # Keep the tail: agent CLIs print the real diagnosis last.
    picked = [line for line in interesting[-max_lines:] if line and line != headline]
    summary = "; ".join([headline, *picked]) if headline else "; ".join(picked)
    return (summary or text.strip() or "unknown backend error")[:600]


def _add_llm_backend_arg(parser: argparse.ArgumentParser) -> None:
    """Choose which agent CLI performs generation/judging for this command."""
    parser.add_argument(
        "--llm-backend",
        choices=["codex", "opencode"],
        default="",
        help="LLM CLI used for generation/judging. 'opencode' sources models "
             "from GitHub Copilot and other providers you have authenticated "
             "(pick one with --model, e.g. github-copilot/claude-sonnet-4.5). "
             "Default: the job's generation.backend, else $GHOSTLAB_LLM_BACKEND, "
             "else codex.",
    )


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
    parser = argparse.ArgumentParser(prog="ghostlab", description="Ghostlab — agent evaluation lab.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    init_parser = sub.add_parser(
        "init",
        help="Advanced: scaffold a standalone ghostlab.yaml spec (most users want "
             "`ghostlab create`, which builds a job instead).",
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
    create_parser.add_argument(
        "--skill", type=Path, default=None,
        help="Evaluate a local skill instead of an MCP target; accepts SKILL.md or its directory.",
    )
    create_parser.add_argument(
        "--agent", type=Path, default=None,
        help="Agent JSON/YAML composing a runner with MCPs, skills, workspace, and assets.",
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
    create_parser.add_argument(
        "--resume", action="store_true",
        help="Continue an existing job: reuse discovery/generation artifacts and resume tests.",
    )
    create_parser.add_argument(
        "--sandbox", choices=["openshell", "local"], default=None,
        help="Agent execution backend (default: openshell; local is explicitly unsandboxed).",
    )
    create_parser.add_argument(
        "--provider", action="append", default=None,
        help="OpenShell provider to attach to agent sessions (repeatable).",
    )
    create_parser.add_argument(
        "--image", default=None,
        help="OpenShell sandbox image/community sandbox (default: base).",
    )
    create_parser.add_argument("--model", default="", help="Codex model for the agent under test.")
    create_parser.add_argument("--user-model", default="", help="Codex model for the user emulator.")
    create_parser.add_argument("--judge-model", default="", help="Codex model for judging test outcomes.")
    create_parser.add_argument("--generation-model", default="", help="Codex model for persona/scenario generation.")
    create_parser.add_argument(
        "--runner-kind", choices=["process", "codex-session"], default=None,
        help="Agent-under-test runner lifecycle (default: process).",
    )
    create_parser.add_argument("--runner-timeout", type=int, default=None, help="AUT turn timeout in seconds (default: 600).")
    create_parser.add_argument(
        "--approval-mode", choices=["never", "on-request", "untrusted"], default=None,
        help="Codex approval mode passed with -a (default: never).",
    )
    create_parser.add_argument(
        "--codex-sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"], default=None,
        help="Codex's nested sandbox mode inside OpenShell (default: read-only).",
    )
    create_parser.add_argument("--codex-bin", default="", help="Codex executable for the AUT and generation.")
    _add_llm_backend_arg(create_parser)

    config_parser = sub.add_parser(
        "config", help="Show the fully resolved agent, model, runner, and sandbox configuration."
    )
    _add_job_args(config_parser)
    config_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

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
    discover_parser.add_argument(
        "--sandbox", choices=["openshell", "local"], default=None,
        help="Override local stdio target execution backend (default: openshell).",
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
        "--require-semantic", action="store_true",
        help="Fail when no runnable semantic/security scenario is available.",
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
    _add_llm_backend_arg(plan_parser)
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
        "--require-semantic", action="store_true",
        help="Fail unless at least one semantic/security conversation actually runs.",
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
        "--resume", action="store_true",
        help="Resume the latest matching test run, keeping completed case/host results and "
             "retrying unfinished or harness-failed cases (requires --repeat 1).",
    )
    test_parser.add_argument(
        "--sandbox", choices=["openshell", "local"], default=None,
        help="Override the job sandbox backend (default from job.yaml: openshell).",
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
    _add_llm_backend_arg(test_parser)
    test_parser.add_argument("--model", default="", help="Model override for the codex judge.")
    test_parser.add_argument(
        "--pdf", action="store_true",
        help="Write a full rollout document (config, purpose, transcript, tool "
             "calls, verdict, critique) as rollout.html + rollout.pdf per run.",
    )

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
        "--sandbox", choices=["openshell", "local"], default="openshell",
        help="Runner sandbox backend (default: openshell).",
    )
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
    _add_llm_backend_arg(profile_parser)
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
    _add_llm_backend_arg(gen_parser)
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
    _add_llm_backend_arg(persona_parser)
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
    _add_llm_backend_arg(dataset_parser)
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
        "--sandbox", choices=["openshell", "local"], default="openshell",
        help="Runner sandbox backend (default: openshell).",
    )
    rundataset_parser.add_argument(
        "--provider", action="append", default=None,
        help="OpenShell provider for runner sessions and optional judge (repeatable).",
    )
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
    _add_llm_backend_arg(rundataset_parser)
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

    lab_parser = sub.add_parser(
        "lab",
        help="Guided setup for a configured agent: model, MCPs, skills, "
             "instructions, permissions, and code — then generate scenarios "
             "and run them fully sandboxed.",
    )
    lab_parser.add_argument("--name", default="", help="Evaluation name.")
    lab_parser.add_argument(
        "--image", default="",
        help="Sandbox image: a community name, an image ref, or a Dockerfile "
             "path (default: the bundled agent sandbox).",
    )
    lab_parser.add_argument("--model", default="", help="Model for generation and judging.")
    lab_parser.add_argument("--personas", type=int, default=0, help="Personas to generate.")
    lab_parser.add_argument(
        "--scenarios-per-persona", type=int, default=0, help="Scenarios per persona."
    )
    lab_parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing evaluation."
    )
    _add_llm_backend_arg(lab_parser)

    doctor_parser = sub.add_parser(
        "doctor",
        help="Check the sandbox, LLM backends, and runner presets.",
    )
    doctor_parser.add_argument(
        "--probe", action="store_true",
        help="Send one tiny live request to each LLM backend to prove it can "
             "actually answer (catches expired quota and CLI/model mismatches "
             "that a --version check cannot).",
    )
    doctor_parser.add_argument(
        "--runners", nargs="*", type=Path, default=None, help="Runner JSON configs to validate."
    )
    doctor_parser.add_argument(
        "--codex-bin", default="", help="Path to codex binary (default: auto-detect)."
    )
    _add_llm_backend_arg(doctor_parser)
    doctor_parser.add_argument(
        "--sandbox", choices=["openshell", "local"], default="openshell",
        help="Sandbox runtime to validate (default: openshell).",
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
    _add_llm_backend_arg(eval_parser)
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
    _add_llm_backend_arg(critique_parser)
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
    print(
        tc.muted(
            "  note: this is the advanced spec flow. For the standard job-based "
            "workflow, use `ghostlab create` instead (see README → spec vs job)."
        )
    )
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax

    from .resolved_config import resolved_job_config

    spec = _job_spec(args)
    resolved = resolved_job_config(spec, args.spec)
    payload = json.dumps(resolved, indent=2, ensure_ascii=False)
    if args.json:
        print(payload)
    else:
        Console().print(Panel(
            Syntax(payload, "json", word_wrap=True, background_color="default"),
            title=f"[bold]Resolved configuration · {spec.id}[/bold]",
            border_style="#7c5cff",
        ))
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


def _create_stage(index: int, title: str, detail: str = "") -> None:
    """Render one stable, grep-friendly stage marker for the create pipeline."""
    from .cli_ui import render_stage

    render_stage(index, title, detail)


def _create_summary(spec, source: str = "") -> list[tuple[str, str]]:
    """Build the same concise configuration summary used by CLI/UI tests."""
    from .resolved_config import resolved_job_config

    target = spec.target_config()
    agent = spec.agent or {}
    inputs = agent.get("inputs", {}) or {}
    location = (
        source or target.connection.get("path") or target.connection.get("url")
        or target.connection.get("command") or "configured inline"
    )
    sandbox = spec.sandbox or {}
    providers = ", ".join(sandbox.get("providers", []) or []) or "none"
    runner = agent.get("runner", {}) or {}
    runner_kind = runner.get("kind") or next(
        (host.get("kind") for host in spec.hosts if host.get("kind") in ("process", "codex-session")),
        "auto-configure",
    )
    resolved = resolved_job_config(spec, Path("job.yaml"))
    effective_runner = resolved["agent"]["runner"]
    models = resolved["models"]
    return [
        ("subject", f"{spec.target_type} · {target.id}"),
        ("source", str(location)),
        ("composition", f"{len(inputs.get('mcps', []) or [])} MCP · {len(inputs.get('skills', []) or [])} skill"),
        ("runner", f"{effective_runner['kind'] or runner_kind} · {effective_runner['timeout_seconds']}s · {effective_runner['parser']}"),
        ("AUT model", str(models["agent_under_test"])),
        ("user model", str(models["user_emulator"])),
        ("judge model", str(models["judge"])),
        ("Codex policy", f"{effective_runner['approval_mode']} · {effective_runner['codex_sandbox']}"),
        ("sandbox", f"{sandbox.get('backend', 'openshell')} · image {sandbox.get('image', 'base')}"),
        ("providers", providers),
        ("generation", f"{(spec.generation or {}).get('personas', 2)} personas × {(spec.generation or {}).get('scenarios_per_persona', 2)} scenarios"),
        ("pass gate", f"{float((spec.review or {}).get('gates', {}).get('min_pass_rate', 0.9)):.0%}"),
    ]


def _print_create_summary(name: str, spec, source: str = "") -> None:
    from .cli_ui import render_config_panel

    render_config_panel(name, _create_summary(spec, source))


def cmd_create(args: argparse.Namespace) -> int:
    from .config import load_target
    from .jobs import (
        create_job, default_job_spec, default_skill_job_spec, slugify, target_from_url,
    )

    interactive = not args.yes
    from .cli_ui import Prompter

    prompter = Prompter()

    def ask(prompt: str, default: str = "") -> str:
        if not interactive:
            return default
        return prompter.text(prompt.rstrip(": "), default)

    def ask_yn(prompt: str, default: bool) -> bool:
        return default if not interactive else prompter.confirm(prompt, default)

    def ask_choice(prompt: str, choices: dict[str, str], default: str) -> str:
        if prompter.modern:
            return prompter.select(prompt.rstrip(": "), list(dict.fromkeys(choices.values())), default)
        while True:
            raw = ask(prompt, default).strip().lower()
            selected = choices.get(raw)
            if selected:
                return selected
            print(f"  choose one of: {', '.join(sorted(set(choices.values())))}")

    if interactive:
        print(tc.heading("Ghostlab · create an evaluation"))
        print(tc.muted("Configure once, preview the resolved job, then run the full pipeline."))
        print()

    name = args.name or ask("Job name: ")
    if not name:
        print("A job name is required (pass --name or answer the prompt).")
        return 1
    if getattr(args, "resume", False):
        return _resume_job_pipeline(args, name, ask_yn)

    selected_targets = sum(bool(value) for value in (args.target, args.skill, args.agent))
    if selected_targets > 1:
        print("Pass exactly one of --agent, --skill, or --target.")
        return 1
    if selected_targets == 0 and interactive:
        subject = ask_choice(
            "Evaluate [agent/mcp/skill] (agent): ",
            {"agent": "agent", "a": "agent", "mcp": "mcp", "m": "mcp", "skill": "skill", "s": "skill"},
            "agent",
        )
        if subject == "agent":
            value = ask("Agent config path (JSON/YAML): ")
            args.agent = Path(value) if value else None
        elif subject == "skill":
            value = ask("Skill path (SKILL.md or directory): ")
            args.skill = Path(value) if value else None
        else:
            args.target = ask("MCP URL or config path: ") or None

    if not any((args.target, args.skill, args.agent)):
        print("An agent, MCP target, or skill is required.")
        return 1

    if interactive and args.sandbox is None:
        args.sandbox = ask_choice(
            "Execution [openshell/local] (openshell): ",
            {"openshell": "openshell", "o": "openshell", "local": "local", "l": "local"},
            "openshell",
        )
    if interactive and args.sandbox == "openshell":
        if args.image is None:
            args.image = ask("OpenShell image", "base")
        if args.provider is None:
            providers = ask("OpenShell providers (comma-separated; blank for none)", "")
            args.provider = [part.strip() for part in providers.split(",") if part.strip()]
    if interactive:
        if not args.model:
            args.model = ask("AUT Codex model (blank uses Codex CLI default)", "")
        if args.runner_kind is None:
            args.runner_kind = ask_choice(
                "Runner lifecycle [process/codex-session] (process): ",
                {"process": "process", "p": "process", "codex-session": "codex-session", "session": "codex-session", "s": "codex-session"},
                "process",
            )
        if args.runner_timeout is None:
            try:
                args.runner_timeout = int(ask("AUT turn timeout seconds", "600"))
            except ValueError:
                print("Runner timeout must be an integer.")
                return 1
        if args.approval_mode is None:
            args.approval_mode = ask_choice(
                "Codex approval mode [never/on-request/untrusted] (never): ",
                {"never": "never", "n": "never", "on-request": "on-request", "o": "on-request", "untrusted": "untrusted", "u": "untrusted"},
                "never",
            )
        if args.codex_sandbox is None:
            args.codex_sandbox = ask_choice(
                "Nested Codex sandbox [read-only/workspace-write/danger-full-access] (read-only): ",
                {"read-only": "read-only", "r": "read-only", "workspace-write": "workspace-write", "w": "workspace-write", "danger-full-access": "danger-full-access", "d": "danger-full-access"},
                "read-only",
            )
        if not args.user_model:
            args.user_model = ask("User-emulator model (blank uses AUT/default)", "")
        if not args.generation_model:
            args.generation_model = ask("Persona/scenario generation model (blank uses AUT/default)", "")
        if not args.judge_model:
            args.judge_model = ask("Judge model (blank uses generation/AUT/default)", "")
    if interactive and args.personas is None:
        try:
            args.personas = max(1, int(ask("Personas (2): ", "2")))
        except ValueError:
            print("Personas must be an integer.")
            return 1
    if interactive and args.scenarios_per_persona is None:
        try:
            args.scenarios_per_persona = max(1, int(ask("Scenarios per persona (2): ", "2")))
        except ValueError:
            print("Scenarios per persona must be an integer.")
            return 1
    if interactive and args.min_pass_rate is None:
        try:
            args.min_pass_rate = float(ask("Minimum pass rate (0.90): ", "0.90"))
        except ValueError:
            print("Minimum pass rate must be a number between 0 and 1.")
            return 1
    if args.min_pass_rate is not None and not 0 <= args.min_pass_rate <= 1:
        print("Minimum pass rate must be between 0 and 1.")
        return 1

    target = None
    source_target = ""
    if args.skill is None and args.agent is None:
        target_value = args.target or ask("Target MCP URL or config path: ")
        if not target_value:
            print("A target is required (pass --target/--skill or answer the prompt).")
            return 1
        target_path = Path(target_value)
        is_config_file = target_path.suffix.lower() == ".json" and target_path.exists()
        if is_config_file:
            try:
                target = load_target(target_path, server=args.server)
            except ConfigError as exc:
                print(str(exc))
                return 1
            source_target = str(target_path)
        else:
            target = target_from_url(
                target_value, transport=args.transport or "streamable-http",
                headers=_parse_header_lines(list(args.header or [])),
            )

    generation: dict = {}
    if args.personas is not None:
        generation["personas"] = args.personas
    if args.scenarios_per_persona is not None:
        generation["scenarios_per_persona"] = args.scenarios_per_persona
    generation_model = args.generation_model or args.model
    if generation_model:
        generation["model"] = generation_model
    if args.codex_bin:
        generation["codex_bin"] = args.codex_bin
    review_gates = {"min_pass_rate": args.min_pass_rate} if args.min_pass_rate is not None else None

    if args.agent is not None:
        from .agents import load_agent_definition
        from .jobs import default_agent_job_spec

        try:
            agent, agent_sandbox = load_agent_definition(args.agent)
            spec = default_agent_job_spec(
                name, agent=agent, sandbox=agent_sandbox, generation=generation,
                review_gates=review_gates,
            )
            spec.source_target = str(args.agent)
        except (ConfigError, OSError, ValueError) as exc:
            print(str(exc))
            return 1
    elif args.skill is not None:
        try:
            spec = default_skill_job_spec(
                name, skill_path=args.skill, generation=generation,
                review_gates=review_gates,
                aut_runner=str(args.aut_runner) if args.aut_runner else None,
            )
        except ConfigError as exc:
            print(str(exc))
            return 1
    else:
        assert target is not None
        spec = default_job_spec(
            name, target=target, source_target=source_target, generation=generation,
            review_gates=review_gates,
            aut_runner=str(args.aut_runner) if args.aut_runner else None,
        )
    if getattr(args, "sandbox", None):
        spec.sandbox["backend"] = args.sandbox
    if getattr(args, "image", None):
        spec.sandbox["image"] = args.image
    if getattr(args, "provider", None):
        spec.sandbox["providers"] = list(dict.fromkeys([
            *list(spec.sandbox.get("providers", []) or []), *args.provider,
        ]))
    # An agent config that declares its own runtime (model, instructions,
    # skills, permissions) *is* the agent under test, so the wizard's codex
    # defaults must not overwrite it.
    declared_runtime = dict((spec.agent or {}).get("runtime") or {})
    declared = str(declared_runtime.get("backend") or "") not in ("", "codex")
    if declared:
        if args.model:
            declared_runtime["model"] = args.model
        runtime = declared_runtime
        spec.generation = {
            **(spec.generation or {}),
            "backend": str(declared_runtime["backend"]),
            "model": args.generation_model or str(declared_runtime.get("model") or ""),
        }
    else:
        runtime = {
            "backend": "codex",
            "model": args.model or "",
            "kind": args.runner_kind or "process",
            "timeout_seconds": args.runner_timeout or 600,
            "approval_mode": args.approval_mode or "never",
            "codex_sandbox": args.codex_sandbox or "read-only",
            "codex_bin": args.codex_bin or "codex",
        }
    existing_runner = {} if declared else dict((spec.agent or {}).get("runner") or {})
    if existing_runner:
        runtime["kind"] = args.runner_kind or existing_runner.get("kind", "process")
        runtime["timeout_seconds"] = args.runner_timeout or existing_runner.get("timeout_seconds", 600)
        command = existing_runner.get("command") or []
        if command and Path(str(command[0])).name != "codex":
            runtime["backend"] = "custom"
    spec.agent = {**(spec.agent or {}), "runtime": runtime}
    if existing_runner:
        from .jobs import configure_codex_runner

        spec.agent["runner"] = configure_codex_runner(
            existing_runner,
            model=args.model,
            kind=args.runner_kind or "",
            timeout_seconds=args.runner_timeout,
            approval_mode=args.approval_mode or "",
            codex_sandbox=args.codex_sandbox or "",
            codex_bin=args.codex_bin,
        )
    spec.test = {
        **(spec.test or {}),
        "user_model": args.user_model or args.model or "",
        "judge_model": args.judge_model or generation_model or args.model or "",
    }

    source_preview = str(args.agent or args.skill or source_target or args.target or "")
    _print_create_summary(name, spec, source_preview)
    run_pipeline = bool(args.discover)
    if interactive and run_pipeline:
        run_pipeline = ask_yn("Run discover → plan → test → review now?", True)
    if interactive and run_pipeline and spec.sandbox.get("backend") == "openshell":
        ready, detail = _openshell_status()
        print(tc.verdict(f"  {'✓' if ready else '!'} OpenShell · {detail}", "pass" if ready else "fail"))
        if not ready and not ask_yn("OpenShell is not ready. Create the job anyway?", True):
            print(tc.muted("Cancelled; start OpenShell and try again."))
            return 0
    if interactive and not ask_yn("Create this job?", True):
        print(tc.muted("Cancelled; no files were written."))
        return 0

    _create_stage(1, "Create job", "Write the resolved configuration and workspace.")
    try:
        spec_path = create_job(name, spec, force=args.force)
    except ConfigError as exc:
        print(str(exc))
        return 1

    slug = slugify(name)
    job_dir = spec_path.parent
    print(tc.heading(f"Created job '{slug}' at {job_dir}/  (job.yaml + workspace/ + runs/)"))
    created_target = spec.target_config()
    target_location = (
        created_target.connection.get("path") or created_target.connection.get("url")
        or created_target.connection.get("command") or ""
    )
    print(f"  target: {created_target.transport} {target_location}")

    if not run_pipeline:
        print(tc.muted(f"  next: ghostlab discover --job {slug}"))
        return 0

    # Inspect the target right away so the job is validated + capabilities are
    # populated — especially the point when a config file was handed in.
    _create_stage(2, "Discover", "Inspect capabilities, contract quality, and MCP Apps metadata.")
    try:
        rc = _discover_new_job(slug)
    except Exception as exc:  # noqa: BLE001 — never lose the created job over a bad target
        print(tc.verdict(f"  discovery failed: {exc}", "fail"))
        rc = 1
    if rc != 0:
        print(tc.muted(f"  job created; fix the target/auth then: ghostlab discover --job {slug}"))
        return 1

    _create_stage(3, "Configure agent", "Resolve the AUT runner and semantic test host.")
    _configure_aut_host(spec_path, ask_yn, getattr(args, "llm_backend", ""))

    _create_stage(4, "Build test plan", "Generate coverage, personas, and scenarios.")
    plan_args = argparse.Namespace(
        job=slug, spec=None, db=None, out=None, approve=None, reject=None,
        generate=True, regenerate=False,
        personas=args.personas, scenarios_per_persona=args.scenarios_per_persona,
        codex_bin="", model="", require_semantic=True,
    )
    if cmd_plan(plan_args) != 0:
        print(tc.muted(f"  job created; fix the plan then: ghostlab plan --job {slug}"))
        return 1

    from .plan import load_test_plan

    plan = load_test_plan(job_dir / "test-plan.yaml")
    suite_names = [name for name, entry in plan["suites"].items() if entry["cases"]]
    chosen_suites = None
    if suite_names:
        if interactive:
            picked = prompter.checkbox("Select suites to run", suite_names, suite_names)
            chosen_suites = picked or []
            if not {"semantic", "security"}.intersection(chosen_suites):
                print(tc.verdict(
                    "  Select semantic or security for the complete create workflow. "
                    "Use standalone `ghostlab test --suite ...` for protocol-only runs.",
                    "fail",
                ))
                return 1

    _create_stage(5, "Run and review", "Execute selected suites and apply readiness gates.")
    test_args = argparse.Namespace(
        job=slug, spec=None, db=None, plan=None, suite=chosen_suites, hosts=None,
        approved_only=False, user_runner=None, apps_mode=False, skip_setup=False,
        timeout=30.0, repeat=1, profile=None, strict=False, judge=None,
        codex_bin="", model="", require_semantic=True,
    )
    test_rc = cmd_test(test_args)
    if test_rc != 0:
        print(tc.verdict("  TEST STAGE FAILED; inspect the results above.", "fail"))
        return 1

    semantic_requested = chosen_suites is None or bool(
        {"semantic", "security"}.intersection(chosen_suites)
    )
    if semantic_requested:
        result_files = list(spec.workspace_dir(spec_path).glob(f"test/*-{spec.id}/results.json"))
        latest_results = max(result_files, key=lambda path: path.stat().st_mtime) if result_files else None
        executed_semantic = []
        if latest_results is not None:
            result_data = json.loads(latest_results.read_text(encoding="utf-8"))
            executed_semantic = [
                entry for entry in result_data.get("results", [])
                if entry.get("suite") in ("semantic", "security")
                and entry.get("status") not in ("skip", "error", "harness_error")
                and (entry.get("artifacts") or {}).get("run_dir")
            ]
        if not executed_semantic:
            print(tc.verdict(
                "  SEMANTIC EXECUTION FAILED: no semantic/security conversation ran. "
                "The command is returning failure instead of reporting the evaluation ready.",
                "fail",
            ))
            return 1

    review_args = argparse.Namespace(job=slug, spec=None, db=None, results=None, strict=False)
    cmd_review_spec(review_args)

    print()
    print(tc.heading(f"Evaluation ready · jobs/{slug}"))
    print(f"  rerun       ghostlab test --job {slug}")
    print(f"  review      ghostlab review --job {slug}")
    print(f"  dashboard   ghostlab ui")
    return 0


def _resume_job_pipeline(args: argparse.Namespace, name: str, ask_yn) -> int:
    """Continue the create pipeline while preserving completed stage artifacts."""
    from .jobs import resolve_job, slugify
    from .spec import load_spec

    slug = slugify(name)
    try:
        spec_path = resolve_job(slug)
        spec = load_spec(spec_path)
    except ConfigError as exc:
        print(str(exc))
        return 1
    print(tc.heading(f"Resuming job '{slug}' at {spec_path.parent}/"))

    generated_from = (spec.capabilities or {}).get("generated_from", "")
    discovered = bool(generated_from and (spec_path.parent / generated_from).exists())
    _create_stage(2, "Discover", "Reuse completed evidence or refresh missing discovery.")
    if discovered:
        print(tc.muted("  discover: reused completed artifacts"))
    else:
        if _discover_new_job(slug) != 0:
            print(tc.muted(f"  resume stopped; fix target/auth then rerun with --resume"))
            return 1

    _create_stage(3, "Configure agent", "Reuse or resolve the AUT runner.")
    _configure_aut_host(spec_path, ask_yn, getattr(args, "llm_backend", ""))
    _create_stage(4, "Build test plan", "Reuse cached generation where possible.")
    plan_args = argparse.Namespace(
        job=slug, spec=None, db=None, out=None, approve=None, reject=None,
        generate=True, regenerate=False, personas=args.personas,
        scenarios_per_persona=args.scenarios_per_persona, codex_bin="", model="",
        require_semantic=True,
    )
    if cmd_plan(plan_args) != 0:
        return 1

    spec = load_spec(spec_path)
    test_root = spec.workspace_dir(spec_path) / "test"
    has_results = any(test_root.glob(f"*-{spec.id}/results*.json"))
    _create_stage(5, "Run and review", "Skip completed cases and retry unfinished work.")
    test_args = argparse.Namespace(
        job=slug, spec=None, db=None, plan=None, suite=None, hosts=None,
        approved_only=False, user_runner=None, apps_mode=False, skip_setup=False,
        timeout=30.0, repeat=1, profile=None, strict=False, judge=None,
        codex_bin="", model="", resume=has_results, require_semantic=True,
    )
    if cmd_test(test_args) != 0:
        return 1
    review_args = argparse.Namespace(job=slug, spec=None, db=None, results=None, strict=False)
    cmd_review_spec(review_args)
    return 0


def _configure_aut_host(spec_path: Path, ask_yn, backend: str = "") -> None:
    """Offer to wire an agent-under-test host so semantic/security suites run.

    A fresh job has no host capable of executing conversational cases, so they
    silently skip in `ghostlab test` until one is configured. Both supported
    agent CLIs can be auto-wired: codex via `-c mcp_servers.*` overrides,
    opencode via a generated project `opencode.json`. Anything else stays a
    manual `--aut-runner` / `hosts:` edit.
    """
    from .codex_backend import resolve_codex_bin
    from .jobs import add_aut_host, build_codex_aut_runner, build_opencode_aut_runner
    from .llm_backend import LlmBackendError, resolve_backend_kind
    from .opencode_backend import resolve_opencode_bin
    from .spec import load_spec

    spec = load_spec(spec_path)  # reload: discover just updated `capabilities`
    if (spec.agent or {}).get("runner"):
        return  # An explicit agent config already defines the AUT.
    if any(h.get("kind") in ("process", "codex-session") for h in spec.hosts):
        return  # --aut-runner (or a hand-edited job.yaml) already set one up

    kind = resolve_backend_kind(backend, str((spec.generation or {}).get("backend", "")))
    resolver = resolve_opencode_bin if kind == "opencode" else resolve_codex_bin
    if kind == "opencode" or spec.sandbox.get("backend") != "openshell":
        try:
            resolver()
        except LlmBackendError:
            print(tc.muted(
                f"  {kind} not found — semantic/security suites will skip until an "
                "agent-under-test host is configured (see README)."
            ))
            return

    if not ask_yn(f"\nSet up semantic/E2E testing with {kind}?", True):
        print(tc.muted("  skipping — semantic/security suites will skip for now."))
        return

    runtime = dict((spec.agent or {}).get("runtime") or {})
    if kind == "opencode":
        runner_config = build_opencode_aut_runner(
            spec, spec_path,
            model=str(runtime.get("model") or ""),
            timeout_seconds=int(runtime.get("timeout_seconds") or 600),
        )
    else:
        runner_config = build_codex_aut_runner(
            spec,
            model=str(runtime.get("model") or ""),
            kind=str(runtime.get("kind") or "process"),
            timeout_seconds=int(runtime.get("timeout_seconds") or 600),
            approval_mode=str(runtime.get("approval_mode") or "never"),
            codex_sandbox=str(runtime.get("codex_sandbox") or "read-only"),
            codex_bin=str(runtime.get("codex_bin") or "codex"),
        )
    runner_path = add_aut_host(spec, spec_path, runner_config)
    print(f"  wrote {runner_path}")
    print(f"  updated {spec_path} (hosts: aut)")


def cmd_discover(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from .setup_runtime import SetupError, SetupRuntime

    spec = _job_spec(args)
    # A per-run --sandbox is an override for this invocation only. `discover`
    # rewrites job.yaml (capabilities), so mutating spec.sandbox here would
    # silently make a one-off `--sandbox local` the job's permanent setting.
    runtime_sandbox = dict(spec.sandbox)
    if getattr(args, "sandbox", None):
        runtime_sandbox["backend"] = args.sandbox
    target = spec.target_config()
    timestamp = utc_now().replace("+00:00", "Z").replace(":", "")
    out_dir = spec.workspace_dir(args.spec) / "discover" / f"{timestamp}-{spec.id}"

    if spec.target_type == "skill":
        return _discover_skill(args, spec, target, out_dir)
    if spec.target_type == "agent":
        return _discover_agent(args, spec, target, out_dir)

    sandbox_session = None
    if target.transport == "stdio":
        from .sandbox import SandboxError, normalize_sandbox, sandbox_stdio_target

        try:
            sandbox_config = normalize_sandbox(runtime_sandbox, args.spec.resolve().parent)
            target, sandbox_session = sandbox_stdio_target(
                target, sandbox_config, role="discover", artifact_dir=out_dir,
            )
        except SandboxError as exc:
            print(tc.verdict(f"  sandbox setup failed [{exc.kind}]: {exc.detail}", "fail"))
            return 1

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
        if sandbox_session is not None:
            sandbox_session.close()


def _discover_skill(args, spec, target, out_dir: Path) -> int:
    """Inspect a local SKILL.md without attempting MCP protocol discovery."""
    from dataclasses import asdict

    from .contract import build_contract, render_contract_md
    from .inspect import write_inspect_artifacts
    from .skills import inspect_skill
    from .spec import save_spec

    result = inspect_skill(Path(str(target.connection.get("path", ""))), spec.id)
    inspect_json, inspect_md = write_inspect_artifacts(result, out_dir)
    contract = build_contract(asdict(result))
    contract["target_type"] = "skill"
    contract["mcp"] = result.server_info.get("name", spec.id)
    contract_json = out_dir / "contract.json"
    contract_md = out_dir / "contract.md"
    contract_json.write_text(json.dumps(contract, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    contract_md.write_text(render_contract_md(contract).replace("# MCP Contract:", "# Skill Contract:"), encoding="utf-8")
    try:
        generated_from = str(contract_json.resolve().relative_to(args.spec.resolve().parent))
    except ValueError:
        generated_from = str(contract_json)
    description = result.capabilities.get("description", "")
    spec.capabilities = {
        "generated_from": generated_from, "discovered_at": contract["generated_at"],
        "target_type": "skill", "name": result.server_info.get("name", spec.id),
        "description": description, "tools": [], "ui_resources": [],
    }
    spec.target["capabilities"] = {
        "target_type": "skill", "description": description,
        "instructions": result.instructions,
    }
    save_spec(spec, args.spec)
    print(tc.heading(f"Discovered skill '{result.server_info.get('name', spec.id)}'"))
    print(f"  source: {target.connection.get('path')}")
    print(f"  wrote {inspect_json}")
    print(f"  wrote {inspect_md}")
    print(f"  wrote {contract_json}")
    return 0


def _discover_agent(args, spec, target, out_dir: Path) -> int:
    """Capture an agent-only definition as discovery input for generation."""
    from dataclasses import asdict

    from .contract import build_contract, render_contract_md
    from .inspect import InspectResult, write_inspect_artifacts
    from .spec import save_spec

    instructions = str((spec.agent or {}).get("instructions", ""))
    result = InspectResult(
        target_id=spec.id, transport="agent",
        server_info={"name": (spec.agent or {}).get("name", spec.id), "version": "configured"},
        capabilities={"target_type": "agent"}, instructions=instructions,
    )
    inspect_json, inspect_md = write_inspect_artifacts(result, out_dir)
    contract = build_contract(asdict(result))
    contract.update({"target_type": "agent", "mcp": (spec.agent or {}).get("name", spec.id)})
    contract_json = out_dir / "contract.json"
    contract_json.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    (out_dir / "contract.md").write_text(
        render_contract_md(contract).replace("# MCP Contract:", "# Agent Contract:"),
        encoding="utf-8",
    )
    try:
        generated_from = str(contract_json.resolve().relative_to(args.spec.resolve().parent))
    except ValueError:
        generated_from = str(contract_json)
    spec.capabilities = {
        "generated_from": generated_from, "discovered_at": contract["generated_at"],
        "target_type": "agent", "tools": [], "ui_resources": [],
    }
    save_spec(spec, args.spec)
    print(tc.heading(f"Discovered agent '{(spec.agent or {}).get('name', spec.id)}'"))
    print(f"  wrote {inspect_json}")
    print(f"  wrote {inspect_md}")
    print(f"  wrote {contract_json}")
    return 0


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
    from .llm_backend import LlmBackendError, backend_label, create_backend
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
    from .sandbox import normalize_sandbox

    backend = create_backend(
        getattr(args, "llm_backend", ""), bin_path=args.codex_bin, model=args.model,
        sandbox=normalize_sandbox(spec.sandbox, args.spec.resolve().parent),
        spec_value=str((spec.generation or {}).get("backend", "")),
    )
    try:
        backend._bin()
    except LlmBackendError as exc:
        print(f"  generation skipped ({_backend_error_summary(exc)})")
        return None
    print(
        f"  generating {n_personas} persona(s) x {scenarios_per_persona} scenario(s) "
        f"with {backend_label(backend)}..."
    )

    def progress(event: dict) -> None:
        print(f"    [{event['phase']}] {event['message']} ({event['completed']}/{event['total']})")

    try:
        # A configured agent is profiled by purpose, not just by tool inventory.
        agent = spec.agent if (spec.agent or {}).get("runtime") else None
        dataset = generate_conversational_dataset(
            inspect_data, backend, spec_id=spec.id,
            n_personas=n_personas, scenarios_per_persona=scenarios_per_persona,
            progress=progress, agent=agent,
        )
    except LlmBackendError as exc:
        print(f"  generation skipped ({_backend_error_summary(exc)})")
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
    from .agents import configured_agent_cases

    configured_cases = configured_agent_cases(spec.agent or {})
    if configured_cases:
        generated_cases = [*(generated_cases or []), *configured_cases]

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
        target_type=spec.target_type,
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

    runnable_semantic = [
        case for case in plan.get("cases", [])
        if case.get("suite") in ("semantic", "security")
        and case.get("kind") == "conversational"
        and (case.get("execution") or {}).get("scenario")
        and not (case.get("execution") or {}).get("needs_generation")
        and case.get("status") != "rejected"
    ]

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
    if getattr(args, "require_semantic", False) and not runnable_semantic:
        print(tc.verdict(
            "  SEMANTIC PLAN FAILED: no runnable semantic/security scenario was generated. "
            "Check `ghostlab config`, OpenShell providers, model access, and the generation error above.",
            "fail",
        ))
        return 1
    if runnable_semantic:
        print(tc.verdict(f"  runnable semantic/security cases: {len(runnable_semantic)}", "pass"))
    print(tc.muted("  next: review statuses, then `ghostlab plan --approve` to approve all"))
    return 0


class _SandboxedOpencodeProject:
    """Temporarily points an opencode AUT project at a sandboxed MCP process.

    The agent under test launches its own copy of a stdio MCP (opencode spawns
    it from `opencode.json`), which would otherwise run unsandboxed on the host
    even while the direct-mcp host runs the same server inside OpenShell. This
    swaps the project's command for the sandbox's SSH-wrapped one for the length
    of the run, then puts the original back.
    """

    def __init__(self, path: Path, original: str, sandbox: "object") -> None:
        self.path = path
        self.original = original
        self.sandbox = sandbox

    def restore(self) -> None:
        try:
            self.path.write_text(self.original, encoding="utf-8")
        finally:
            self.sandbox.close()


class _SandboxedAgent:
    """Runs the whole configured agent — CLI, MCPs, and code — in the sandbox.

    The runner JSON is rewritten to an SSH-wrapped `opencode run` inside the
    container for the length of the run, then restored, so the job directory is
    left exactly as the user configured it.
    """

    def __init__(self, runner_path: Path, original: str, handle: "object") -> None:
        self.runner_path = runner_path
        self.original = original
        self.handle = handle

    def restore(self) -> None:
        try:
            self.runner_path.write_text(self.original, encoding="utf-8")
        finally:
            self.handle.close()


def _sandbox_configured_agent(spec, spec_path: Path, out_dir: Path):
    """Put a fully configured agent inside OpenShell for this run.

    Returns ``None`` when the job has no agent runtime, leaving the simpler
    MCP-only path (:func:`_sandbox_opencode_aut`) to handle it.
    """
    from .agent_sandbox import prepare_agent_sandbox, write_sandboxed_project
    from .jobs import RUNNERS_DIR
    from .opencode_config import runtime_input_paths

    agent = dict(spec.agent or {})
    runtime = dict(agent.get("runtime") or {})
    if str(runtime.get("backend")) != "opencode" or not runtime:
        return None
    if str((spec.sandbox or {}).get("backend")) != "openshell":
        return None

    job_dir = spec_path.resolve().parent
    runner_path = job_dir / RUNNERS_DIR / "aut.json"
    if not runner_path.exists():
        return None
    original = runner_path.read_text(encoding="utf-8")
    runner = json.loads(original)

    target = spec.target_config() if spec.target_type == "mcp" else None
    handle = prepare_agent_sandbox(
        agent, dict(spec.sandbox), role="aut", base_dir=job_dir,
        artifact_dir=out_dir,
        extra_paths=runtime_input_paths(
            runtime, list((agent.get("inputs") or {}).get("skills") or [])
        ),
    )
    try:
        remote_dir = write_sandboxed_project(
            handle, out_dir / "opencode-aut", agent, target
        )
        argv = [
            "opencode", "run", "--format", "json", "--log-level", "ERROR",
            "--model", str(runtime.get("model") or ""), "--dir", remote_dir,
        ]
        if runtime.get("default_agent"):
            argv[-2:-2] = ["--agent", str(runtime["default_agent"])]
        runner["command"] = handle.command(argv, workdir=remote_dir)
        runner["parser"] = "opencode-json"
        runner["sandbox"] = {"backend": "local"}  # the wrapper *is* the boundary
        runner_path.write_text(json.dumps(runner, indent=2) + "\n", encoding="utf-8")
    except Exception:
        handle.close()
        raise
    print(tc.muted(
        f"  agent sandboxed in OpenShell ({handle.sandbox.name}): "
        f"CLI, MCPs and workspace all inside"
    ))
    return _SandboxedAgent(runner_path, original, handle)


def _sandbox_opencode_aut(spec, spec_path: Path, out_dir: Path):
    """Wrap the opencode agent-under-test's stdio MCP in the job's sandbox."""
    from .sandbox import normalize_sandbox, sandbox_stdio_target

    if str((spec.sandbox or {}).get("backend")) != "openshell":
        return None
    target = spec.target_config()
    if target.transport != "stdio":
        return None

    project = spec_path.resolve().parent / "runners" / "opencode-aut" / "opencode.json"
    if not project.exists():
        return None  # codex AUT, or no agent host configured
    original = project.read_text(encoding="utf-8")
    config = json.loads(original)
    entry = (config.get("mcp") or {}).get(target.id)
    if not isinstance(entry, dict) or entry.get("type") != "local":
        return None

    sandbox_config = normalize_sandbox(spec.sandbox, spec_path.resolve().parent)
    wrapped, session = sandbox_stdio_target(
        target, sandbox_config, role="aut", artifact_dir=out_dir,
    )
    if session is None:
        return None
    entry["command"] = list(wrapped.connection["command"])
    project.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(tc.muted(f"  agent MCP sandboxed in OpenShell ({session.name})"))
    return _SandboxedOpencodeProject(project, original, session)


def _write_rollouts(spec, results: dict, out_dir: Path) -> None:
    """Emit a full rollout document per conversational run in this test run."""
    from .rollout_report import write_rollout

    profile_path = spec.workspace_dir(out_dir.parent.parent) / "agent-profile.json"
    agent_profile = None
    if profile_path.is_file():
        agent_profile = json.loads(profile_path.read_text(encoding="utf-8"))

    written = 0
    for entry in results.get("results", []):
        run_dir = (entry.get("artifacts") or {}).get("run_dir")
        if not run_dir or not Path(run_dir).is_dir():
            continue
        paths = write_rollout(
            Path(run_dir), title=f"{spec.id} · {entry.get('case', '')}",
            agent=spec.agent, agent_profile=agent_profile, sandbox=spec.sandbox,
        )
        written += 1
        if "pdf" in paths:
            print(f"  wrote {paths['pdf']}")
        else:
            print(tc.muted(f"  wrote {paths['html']} (PDF unavailable: "
                           f"{paths.get('pdf_error', 'no browser')})"))
    if not written:
        print(tc.muted("  no conversational runs to document"))


def cmd_lab(args: argparse.Namespace) -> int:
    """Guided setup for a configured-agent evaluation, then run it."""
    from .agent_profile import write_agent_profile
    from .agent_sandbox import DEFAULT_AGENT_IMAGE
    from .cli_ui import Prompter, render_config_panel, render_stage
    from .jobs import create_job, default_agent_job_spec, jobs_dir
    from .lab import (
        TOTAL_STEPS, build_agent_interactively, confirm_profile, review_scenarios,
        sandbox_settings,
    )
    from .llm_backend import LlmBackendError, backend_label, create_backend
    from .spec import save_spec

    prompter = Prompter()
    print(tc.heading("Ghostlab · configure an agent evaluation"))
    print(tc.muted(
        "Everything runs inside OpenShell: the agent CLI, its MCPs, and its code."
    ))
    print()

    name = args.name or prompter.text("Evaluation name", "my-agent")
    agent = build_agent_interactively(prompter, name)
    sandbox = sandbox_settings(prompter, args.image or DEFAULT_AGENT_IMAGE)

    backend = create_backend(
        getattr(args, "llm_backend", "") or "opencode",
        model=args.model or str(agent["runtime"].get("model") or ""),
    )
    render_stage(8, "Purpose", f"Inferred with {backend_label(backend)}.", TOTAL_STEPS)
    try:
        profile = confirm_profile(prompter, agent, backend)
    except LlmBackendError as exc:
        print(tc.verdict(f"  could not infer a purpose: {_backend_error_summary(exc)}", "fail"))
        return 1

    spec = default_agent_job_spec(name, agent=agent, sandbox=sandbox)
    spec.generation = {
        **(spec.generation or {}),
        "backend": "opencode",
        "model": args.model or str(agent["runtime"].get("model") or ""),
        "personas": args.personas or 2,
        "scenarios_per_persona": args.scenarios_per_persona or 2,
    }
    spec_path = create_job(name, spec, jobs_root=jobs_dir(), force=args.force)
    job_dir = spec_path.parent
    write_agent_profile(profile, job_dir / "workspace")
    print(tc.muted(f"  wrote {job_dir}/workspace/agent-profile.json"))

    # Without an agent-under-test host the conversational cases silently skip.
    # `test` later rewrites this runner to execute inside the sandbox.
    from .jobs import add_aut_host, build_opencode_aut_runner
    from .spec import load_spec

    spec = load_spec(spec_path)
    if not any(host.get("kind") == "process" for host in spec.hosts or []):
        runner = build_opencode_aut_runner(
            spec, spec_path, model=spec.generation["model"], timeout_seconds=900,
        )
        add_aut_host(spec, spec_path, runner)
        save_spec(spec, spec_path)
        print(tc.muted("  configured the agent-under-test host"))

    # `plan` needs the capability inventory, which `discover` writes. For an
    # agent job that also records the agent definition itself.
    discover_args = argparse.Namespace(
        job=name, spec=None, db=None, timeout=30.0, sample="off", skip_setup=True,
        skip_apps=True, strict=False, sandbox=None, approve_mutations=False,
        approve_destructive=False, server=None,
    )
    if cmd_discover(discover_args) != 0:
        print(tc.verdict("  discovery failed; fix the agent's capabilities first", "fail"))
        return 1

    render_stage(9, "Scenarios", "Generated from the purpose you just confirmed.", TOTAL_STEPS)
    plan_args = argparse.Namespace(
        job=name, spec=None, out=None, personas=spec.generation["personas"],
        scenarios_per_persona=spec.generation["scenarios_per_persona"],
        # approve/reject must stay None: any value puts `plan` in curation-only
        # mode, which refuses to run before a plan exists.
        generate=True, regenerate=True, approve=None, reject=None, db=None,
        codex_bin="", model=spec.generation["model"], llm_backend="opencode",
        sandbox=None, strict=False,
    )
    if cmd_plan(plan_args) != 0:
        return 1

    from .plan import load_test_plan, write_test_plan

    plan = load_test_plan(job_dir / "test-plan.yaml")
    conversational = [case for case in plan.get("cases", [])
                      if case.get("kind") == "conversational"]
    kept = review_scenarios(prompter, conversational)
    keep_ids = {case.get("id") for case in kept}
    for case in plan.get("cases", []):
        if case.get("kind") == "conversational" and case.get("id") not in keep_ids:
            case["status"] = "rejected"
    write_test_plan(plan, job_dir / "test-plan.yaml")

    render_config_panel(name, [
        ("job", str(job_dir)),
        ("model", spec.generation["model"]),
        ("sandbox", f"openshell · {sandbox['image']}"),
        ("credentials", "in sandbox" if sandbox["credentials"]["opencode_auth"] else "none"),
        ("scenarios", str(len(kept))),
    ])
    if not prompter.confirm("Run the evaluation now?", True):
        print(tc.muted(f"  later: ghostlab test --job {name}"))
        return 0

    test_args = argparse.Namespace(
        job=name, spec=None, plan=None, suite=None, hosts=None, approved_only=False,
        user_runner=None, apps_mode=False, skip_setup=False, timeout=30.0, repeat=1,
        resume=False, sandbox=None, profile=None, strict=False, judge=True,
        codex_bin="", model=spec.generation["model"], llm_backend="opencode",
        require_semantic=False, pdf=True,
    )
    return cmd_test(test_args)


def cmd_test(args: argparse.Namespace) -> int:
    from dataclasses import replace
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
    if getattr(args, "sandbox", None):
        spec.sandbox["backend"] = args.sandbox
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
        args.model = t.get("judge_model", "") or gen.get("model", "") or ""
    if not args.codex_bin:
        args.codex_bin = gen.get("codex_bin", "") or ""
    plan_path = args.plan or args.spec.resolve().parent / "test-plan.yaml"
    if not plan_path.exists():
        raise ConfigError(f"No plan at {plan_path}; run `ghostlab plan --spec {args.spec}` first.")
    plan = load_test_plan(plan_path)

    backend = None
    if args.judge:
        from .llm_backend import LlmBackendError, backend_label, create_backend

        try:
            from .sandbox import normalize_sandbox

            candidate = create_backend(
                getattr(args, "llm_backend", ""),
                bin_path=args.codex_bin, model=args.model,
                sandbox=normalize_sandbox(spec.sandbox, args.spec.resolve().parent),
            )
            candidate._bin()  # resolve now so a missing codex degrades gracefully
            backend = candidate
        except LlmBackendError as exc:
            print(f"  (judge disabled: {exc})")

    if args.user_runner is not None:
        user_runner_config = load_runner(args.user_runner)
    else:
        # Zero-config default: a plain agent session with no MCP wired in, so it
        # plays a human, never another tool-using agent. Mirrors
        # runners/codex-user-emulator.json.
        from .llm_backend import resolve_backend_kind

        user_model = str(t.get("user_model") or "")
        emulator_backend = resolve_backend_kind(
            getattr(args, "llm_backend", ""),
            str((spec.generation or {}).get("backend", "")),
        )
        if emulator_backend == "opencode":
            from .runner_presets import opencode_user_runner

            user_runner_config = opencode_user_runner(
                spec.workspace_dir(args.spec) / "user-emulator",
                timeout_seconds=600, model=user_model,
            )
        else:
            user_command = ["codex", "--sandbox", "read-only", "-a", "never"]
            if user_model:
                user_command += ["-m", user_model]
            user_command += ["exec", "--skip-git-repo-check", "-"]
            user_runner_config = RunnerConfig(
                kind="process",
                command=user_command,
                timeout_seconds=600,
                prompt_mode="stdin",
                parser="text",
            )
    if user_runner_config.parser not in ("opencode-json", "opencode-text"):
        # opencode manages its own process; only codex runners are OpenShell-wrapped.
        user_runner_config = replace(user_runner_config, sandbox=dict(spec.sandbox))
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

    test_root = spec.workspace_dir(args.spec) / "test"
    resume_results = None
    if getattr(args, "resume", False):
        if args.repeat != 1:
            raise ConfigError("--resume currently requires --repeat 1")
        candidates = sorted(test_root.glob(f"*-{spec.id}/results.partial.json"))
        candidates += sorted(test_root.glob(f"*-{spec.id}/results.json"))
        if candidates:
            resume_path = max(candidates, key=lambda path: path.stat().st_mtime)
            out_dir = resume_path.parent
            resume_results = json.loads(resume_path.read_text(encoding="utf-8"))
            print(tc.muted(f"  resuming {out_dir} ({len(resume_results.get('results', []))} saved result(s))"))
        else:
            raise ConfigError(f"No prior test results found under {test_root}")
    else:
        timestamp = utc_now().replace("+00:00", "Z").replace(":", "")
        out_dir = test_root / f"{timestamp}-{spec.id}"
    print(tc.heading(
        f"Testing '{spec.id}' with host(s): {', '.join(host.id for host in hosts)}"
        + (f" (suites: {', '.join(args.suite)})" if args.suite else "")
    ))

    def progress(line: str) -> None:
        match = re.match(r"^(\s*)-> (pass|fail|error|skip|harness_error)\b(.*)$", line)
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

    from .sandbox import SandboxError

    runtime = SetupRuntime({} if args.skip_setup else spec.setup, out_dir)
    aut_sandbox = None
    try:
        # A configured agent goes in whole; otherwise only its MCP is wrapped.
        aut_sandbox = _sandbox_configured_agent(spec, args.spec, out_dir)
        if aut_sandbox is None:
            aut_sandbox = _sandbox_opencode_aut(spec, args.spec, out_dir)
    except SandboxError as exc:
        print(tc.verdict(f"  sandbox setup failed [{exc.kind}]: {exc.detail}", "fail"))
        return 1
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
            resume_results=resume_results,
            checkpoint_path=out_dir / "results.partial.json",
        )
    finally:
        runtime.teardown()
        if runtime.declared:
            runtime.write_status()
        if aut_sandbox is not None:
            aut_sandbox.restore()

    results_json = out_dir / "results.json"
    results_md = out_dir / "results.md"
    results_json.write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    results_md.write_text(render_results_md(results), encoding="utf-8")
    if getattr(args, "pdf", False):
        _write_rollouts(spec, results, out_dir)
    partial_path = out_dir / "results.partial.json"
    if partial_path.exists():
        partial_path.unlink()

    totals = results["totals"]
    rate = results["pass_rate"]
    print(tc.heading(
        f"  executed {results['executed']} case-run(s): "
        f"{totals['pass']} pass, {totals['fail']} fail, {totals['error']} error, "
        f"{totals.get('harness_error', 0)} harness error "
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

    if getattr(args, "require_semantic", False):
        executed_semantic = [
            entry for entry in results.get("results", [])
            if entry.get("suite") in ("semantic", "security")
            and entry.get("status") not in ("skip", "error", "harness_error")
            and (entry.get("artifacts") or {}).get("run_dir")
        ]
        if not executed_semantic:
            print(tc.verdict(
                "  SEMANTIC EXECUTION FAILED: no semantic/security conversation actually ran. "
                "Check the resolved runner/models with `ghostlab config --job ...`.",
                "fail",
            ))
            return 1

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
    from dataclasses import replace

    target = load_target(args.target, args.server)
    scenario = load_scenario(args.scenario)
    aut_runner = load_runner(args.aut_runner)
    user_runner = load_runner(args.user_runner)
    sandbox = {"backend": getattr(args, "sandbox", "openshell")}
    aut_runner = replace(aut_runner, sandbox={**dict(aut_runner.sandbox or {}), **sandbox})
    user_runner = replace(user_runner, sandbox={**dict(user_runner.sandbox or {}), **sandbox})
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
    print(f"Ghostlab run {result.status} ({result.turns} turns)")
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
    from .llm_backend import LlmBackendError, backend_label, create_backend
    from .profile import build_capability_profile, profile_prompt, write_profile_artifacts

    inspect_path = args.inspect
    if not inspect_path.exists():
        raise ConfigError(f"inspect.json not found: {inspect_path}")
    inspect_data = json.loads(inspect_path.read_text(encoding="utf-8"))

    backend = create_backend(
        getattr(args, "llm_backend", ""), bin_path=args.codex_bin, model=args.model
    )
    print(f"Generating capability profile with {backend_label(backend)}...")
    try:
        profile = build_capability_profile(inspect_data, backend)
    except LlmBackendError as exc:
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
    from .llm_backend import LlmBackendError, backend_label, create_backend
    from .generate import generate_scenarios, write_scenarios

    if not args.profile.exists():
        raise ConfigError(f"capabilities.json not found: {args.profile}")
    profile = json.loads(args.profile.read_text(encoding="utf-8"))

    backend = create_backend(
        getattr(args, "llm_backend", ""), bin_path=args.codex_bin, model=args.model
    )
    print(f"Generating {args.n} scenario(s) with {backend_label(backend)}...")
    try:
        scenarios = generate_scenarios(profile, backend, args.n)
    except LlmBackendError as exc:
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
    from .llm_backend import LlmBackendError, backend_label, create_backend
    from .personas import generate_personas, write_personas

    if not args.profile.exists():
        raise ConfigError(f"capabilities.json not found: {args.profile}")
    profile = json.loads(args.profile.read_text(encoding="utf-8"))

    backend = create_backend(
        getattr(args, "llm_backend", ""), bin_path=args.codex_bin, model=args.model
    )
    print(f"Generating {args.n} persona(s) with {backend_label(backend)}...")
    try:
        personas = generate_personas(profile, backend, args.n)
    except LlmBackendError as exc:
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
    from .llm_backend import LlmBackendError, backend_label, create_backend
    from .dataset import build_dataset, write_dataset

    if not args.profile.exists():
        raise ConfigError(f"capabilities.json not found: {args.profile}")
    profile = json.loads(args.profile.read_text(encoding="utf-8"))

    mcp_name = str(profile.get("mcp", "mcp")).split("@")[0]
    name = args.name or mcp_name
    backend = create_backend(
        getattr(args, "llm_backend", ""), bin_path=args.codex_bin, model=args.model
    )
    print(
        f"Generating dataset '{name}': {args.personas} personas x "
        f"{args.scenarios_per_persona} scenarios with {backend_label(backend)}..."
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
    except LlmBackendError as exc:
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
    from .sandbox import normalize_sandbox

    if not (args.dataset / "dataset.json").exists():
        raise ConfigError(f"No dataset.json in {args.dataset}")

    # Override only the backend on runner files so their image, providers,
    # uploads, and environment policy remain intact.
    sandbox = {"backend": args.sandbox}
    if args.provider:
        sandbox["providers"] = list(args.provider)
    judge_sandbox = normalize_sandbox(sandbox)
    backend = None
    capabilities = None
    if args.evaluate:
        from .llm_backend import create_backend

        backend = create_backend(
            getattr(args, "llm_backend", ""), bin_path=args.codex_bin,
            model=args.model, sandbox=judge_sandbox,
            spec_value=str((spec.generation or {}).get("backend", "")),
        )
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
            sandbox=sandbox,
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


def _openshell_status() -> tuple[bool, str]:
    import shutil
    import subprocess

    openshell = shutil.which("openshell")
    if not openshell:
        return False, "CLI not found (install NVIDIA OpenShell)"
    try:
        status = subprocess.run([openshell, "status"], capture_output=True, text=True, timeout=20)
        detail = (status.stdout or status.stderr).strip().replace("\n", " ")[:300]
        return status.returncode == 0, detail or openshell
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def _probe_backend(kind: str, bin_path: str) -> tuple[bool, str]:
    """Ask a backend for one trivial JSON object to prove it can actually answer.

    A version string only proves a binary exists. It does not prove the account
    behind it has quota, or that the CLI is new enough for the model it is
    configured to use — the failures that otherwise surface much later as
    "generation skipped".
    """
    from .llm_backend import LlmBackendError, create_backend

    backend = create_backend(kind, bin_path=bin_path, timeout_seconds=180)
    schema = {
        "type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}},
    }
    try:
        result = backend.generate_json("Reply with {\"ok\": true}", schema)
    except LlmBackendError as exc:
        return False, _backend_error_summary(exc)
    if isinstance(result, dict) and result.get("ok") is not None:
        return True, "answered a live generation probe"
    return False, f"unexpected probe reply: {str(result)[:200]}"


def _check_llm_backends(args: argparse.Namespace) -> bool:
    """Report which generation backends are installed, and optionally usable."""
    import subprocess

    from .codex_backend import resolve_codex_bin
    from .llm_backend import LlmBackendError, resolve_backend_kind
    from .opencode_backend import resolve_opencode_bin

    selected = resolve_backend_kind(getattr(args, "llm_backend", "") or "")
    probe = getattr(args, "probe", False)
    results: dict[str, bool] = {}

    for kind, resolver, override in (
        ("codex", resolve_codex_bin, getattr(args, "codex_bin", "")),
        ("opencode", resolve_opencode_bin, ""),
    ):
        marker = " (selected)" if kind == selected else ""
        try:
            binary = override or resolver()
            version = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=30
            )
            tag = (version.stdout or version.stderr).strip().splitlines()[-1][:60]
        except (LlmBackendError, OSError, subprocess.SubprocessError, IndexError) as exc:
            results[kind] = False
            print(f"  [--] {kind}{marker}: not available ({exc})")
            continue

        if not probe:
            results[kind] = True
            print(
                f"  [ok] {kind}{marker}: {binary} ({tag}) "
                "— installed, not verified (use --probe for a live check)"
            )
            continue
        works, detail = _probe_backend(kind, binary)
        results[kind] = works
        print(f"  [{'ok' if works else '!!'}] {kind}{marker}: {binary} ({tag}) — {detail}")

    if not any(results.values()):
        print(
            "  no usable generation backend: install codex, or install opencode "
            "and authenticate a provider (`opencode auth login`), then select it "
            "with --llm-backend opencode."
        )
        return False
    if not results.get(selected, False):
        alternative = next((k for k, v in results.items() if v and k != selected), "")
        if alternative:
            print(
                f"  note: the selected backend '{selected}' is unusable; rerun with "
                f"--llm-backend {alternative} (or set generation.backend in job.yaml)."
            )
        return False
    return True


def cmd_doctor(args: argparse.Namespace) -> int:
    import subprocess

    from .codex_backend import CodexError, resolve_codex_bin

    ok = True
    print("Ghostlab doctor")
    if args.sandbox == "openshell":
        reachable, detail = _openshell_status()
        ok = ok and reachable
        print(f"  [{'ok' if reachable else '!!'}] openshell: {detail}")
    else:
        print("  [!!] sandbox: local (explicitly unsandboxed compatibility mode)")
    backend_ok = _check_llm_backends(args)
    ok = ok and backend_ok

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
    from .llm_backend import LlmBackendError, backend_label, create_backend
    from .evaluate import evaluate_run, write_verdict_artifacts

    if not (args.run / "events.jsonl").exists():
        raise ConfigError(f"No events.jsonl in {args.run}")
    capabilities = None
    if args.capabilities:
        if not args.capabilities.exists():
            raise ConfigError(f"capabilities.json not found: {args.capabilities}")
        capabilities = json.loads(args.capabilities.read_text(encoding="utf-8"))

    backend = create_backend(
        getattr(args, "llm_backend", ""), bin_path=args.codex_bin, model=args.model
    )
    print(f"Evaluating {args.run} with the {backend_label(backend)} judge...")
    store = _open_store(args)
    try:
        verdict = evaluate_run(args.run, backend, capabilities, store=store)
    except LlmBackendError as exc:
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
    from .llm_backend import LlmBackendError, backend_label, create_backend
    from .critique import critique_run, write_critique_artifacts

    if not (args.run / "events.jsonl").exists():
        raise ConfigError(f"No events.jsonl in {args.run}")
    inspect = None
    if args.inspect:
        if not args.inspect.exists():
            raise ConfigError(f"inspect.json not found: {args.inspect}")
        inspect = json.loads(args.inspect.read_text(encoding="utf-8"))

    backend = create_backend(
        getattr(args, "llm_backend", ""), bin_path=args.codex_bin, model=args.model
    )
    print(f"Critiquing tool usability in {args.run} with {backend_label(backend)}...")
    try:
        critique = critique_run(args.run, backend, inspect)
    except LlmBackendError as exc:
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
    print(f"Launching Ghostlab UI at http://{args.server_address}:{args.port}")
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
        "config": cmd_config,
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
        "lab": cmd_lab,
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
    from .mcp_client import McpClientError
    from .sandbox import SandboxError

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
    except McpClientError as exc:
        # A target that won't talk is an expected outcome of testing an MCP, not
        # a Ghostlab crash — report it as a failure, without a traceback.
        print(tc.verdict(f"MCP connection failed: {exc}", "fail"))
        if "did not answer" in str(exc):
            print(
                "  the server started but never replied. Check that its command "
                "and dependencies are reachable where it runs; if it needs "
                "host-only resources, run with --sandbox local."
            )
        return 1
    except SandboxError as exc:
        print(tc.verdict(f"Sandbox error [{exc.kind}]: {exc.detail}", "fail"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
