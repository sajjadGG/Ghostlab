"""Streamlit app driving the whole MCP Ghostlab pipeline.

Launch via `ghostlab ui`. Walks an MCP through: inspect and understand -> build
runnable persona x scenario cases -> run and evaluate -> review traces and
verdicts. Every codex-backed stage exposes its model and its exact prompt.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from rehearsal.codex_backend import CodexBackend, CodexError, resolve_codex_bin
from rehearsal.config import TargetConfig, load_persona, load_scenario
from rehearsal.dataset import build_dataset, write_dataset
from rehearsal.evaluate import (
    evidence_references,
    evaluate_run,
    judge_prompt,
    read_run,
    write_verdict_artifacts,
)
from rehearsal.generate import scenario_prompt
from rehearsal.inspect import inspect_target, write_inspect_artifacts
from rehearsal.orchestrator import run_scenario
from rehearsal.personas import persona_prompt
from rehearsal.profile import build_capability_profile, profile_prompt, write_profile_artifacts
from rehearsal.prompts import build_aut_prompt, build_user_emulator_prompt
from rehearsal.review import (
    build_review,
    ensure_statuses,
    load_dataset,
    save_manifest,
    set_statuses,
)
from rehearsal.runner_presets import codex_aut_runner, codex_user_runner, mock_runner

st.set_page_config(page_title="MCP Ghostlab", page_icon="👻", layout="wide")

st.markdown(
    """
    <style>
    :root {
        --ghost-accent: #7c5cff;
        --ghost-accent-soft: rgba(124, 92, 255, 0.12);
        --ghost-border: rgba(128, 128, 128, 0.22);
    }

    [data-testid="stAppViewContainer"] > .main {
        background:
            radial-gradient(circle at 78% 0%, rgba(124, 92, 255, 0.08), transparent 25rem),
            transparent;
    }

    [data-testid="stSidebar"] {
        border-right: 1px solid var(--ghost-border);
    }

    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
        line-height: 1.45;
    }

    .ghost-brand {
        display: flex;
        align-items: center;
        gap: 0.7rem;
        margin-bottom: 0.2rem;
    }

    .ghost-mark {
        display: grid;
        width: 2.25rem;
        height: 2.25rem;
        place-items: center;
        border: 1px solid rgba(124, 92, 255, 0.28);
        border-radius: 0.75rem;
        background: var(--ghost-accent-soft);
        font-size: 1.15rem;
    }

    .ghost-brand-name {
        font-size: 1.12rem;
        font-weight: 700;
        letter-spacing: -0.02em;
    }

    .ghost-eyebrow {
        color: #7c5cff;
        font-size: 0.72rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        text-transform: uppercase;
    }

    .ghost-hero {
        max-width: 52rem;
        padding: 1.2rem 0 1.4rem;
    }

    .ghost-hero h1 {
        margin: 0.35rem 0 0.6rem;
        font-size: clamp(2rem, 4vw, 3.25rem);
        letter-spacing: -0.055em;
        line-height: 1.05;
    }

    .ghost-hero p {
        max-width: 42rem;
        margin: 0;
        color: #7b7f8b;
        font-size: 1.02rem;
    }

    .ghost-step {
        display: flex;
        align-items: center;
        gap: 0.65rem;
        padding: 0.42rem 0;
        color: #7b7f8b;
        font-size: 0.88rem;
    }

    .ghost-step-dot {
        width: 0.55rem;
        height: 0.55rem;
        flex: 0 0 auto;
        border: 1px solid var(--ghost-border);
        border-radius: 999px;
        background: transparent;
    }

    .ghost-step.done {
        color: inherit;
        font-weight: 600;
    }

    .ghost-step.done .ghost-step-dot {
        border-color: #42b883;
        background: #42b883;
        box-shadow: 0 0 0 4px rgba(66, 184, 131, 0.12);
    }

    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-color: var(--ghost-border);
        border-radius: 1rem;
    }

    div.stButton > button[kind="primary"] {
        border-color: #6c4df2;
        background: #6c4df2;
    }

    div.stButton > button[kind="primary"]:hover {
        border-color: #8065f4;
        background: #8065f4;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

DEFAULTS = {
    "target": None,
    "inspect": None,
    "inspect_dir": None,
    "profile": None,
    "dataset_dir": None,
    "run_results": [],
    "model": "",
}
for key, value in DEFAULTS.items():
    st.session_state.setdefault(key, value)


def workspace() -> Path:
    path = Path(st.session_state.get("workspace", "ghostlab_workspace"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def backend() -> CodexBackend:
    return CodexBackend(bin_path=st.session_state.get("codex_bin", ""), model=st.session_state.get("model", ""))


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def prompt_view(label: str, builder, *args) -> None:
    """Render a read-only expander showing the exact codex prompt for a stage."""
    with st.expander(f"🔍 View prompt — {label}"):
        try:
            st.code(builder(*args), language="markdown")
        except Exception as exc:  # noqa: BLE001
            st.caption(f"(prompt unavailable: {exc})")


def duration_label(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "unknown"
    try:
        seconds = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
    except ValueError:
        return "unknown"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def trace_anchor(reference: str) -> str:
    """Return the trace DOM anchor for an evidence-reference label."""
    match = re.search(r"^(user|assistant) turn (\d+)$", reference)
    if match:
        return f"trace-{match.group(1)}-{match.group(2)}"
    tool_match = re.search(r"^(.+) · turn (\d+)$", reference)
    if tool_match:
        tool = re.sub(r"[^a-z0-9]+", "-", tool_match.group(1).lower()).strip("-")
        return f"trace-tool-{tool_match.group(2)}-{tool}"
    return ""


def render_case_workspace(dataset_dir: Path) -> None:
    """Render the generated persona + scenario pairs that Ghostlab can run."""
    ds = load_dataset(dataset_dir)
    if ensure_statuses(ds["manifest"]):
        save_manifest(dataset_dir, ds["manifest"])
    review = build_review(ds, st.session_state["profile"])
    totals = review["totals"]

    st.markdown("### Runnable cases")
    st.caption(
        "A case is one persona paired with one scenario. Each included case becomes "
        "one multi-turn run and one evaluation."
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Personas", totals["personas"])
    m2.metric("Scenarios", totals["scenarios"])
    m3.metric("Runnable cases", totals["cases"])
    m4.metric("Quality flags", len(review["flags"]))

    with st.expander("Dataset quality and tool coverage", expanded=bool(review["flags"])):
        cov = review.get("coverage", {})
        if cov:
            st.table(
                [
                    {"tool category": key, "covered": f"{value['exercised']}/{value['total']}"}
                    for key, value in cov["by_category"].items()
                ]
            )
            if cov.get("unexercised_tools"):
                st.caption(
                    "Not covered by the generated cases: "
                    + ", ".join(f"`{tool}`" for tool in cov["unexercised_tools"])
                )
        if review["flags"]:
            st.warning("Review these generated-case warnings before running:")
            st.table([{"kind": flag["kind"], "detail": flag["detail"]} for flag in review["flags"]])
        else:
            st.success("No duplicate, unknown-tool, or orphan-persona warnings.")

    statuses: dict[str, str] = {}
    options = ["approved", "pending", "needs-edit", "rejected"]
    for index, case in enumerate(review["cases"], start=1):
        persona = ds["personas"].get(case["persona"], {})
        scenario = ds["scenarios"].get(case["scenario"], {})
        label = (
            f"{index}. {persona.get('name', case['persona'])} + "
            f"{case['scenario_title'] or case['goal']}"
        )
        with st.expander(label, expanded=index == 1):
            status_col, identity_col = st.columns([1, 3])
            with status_col:
                current = case["status"]
                statuses[case["id"]] = st.selectbox(
                    "Run status",
                    options,
                    index=options.index(current),
                    key=f"status-{case['id']}",
                    help="Approved cases can be selected as the run queue.",
                )
                st.caption(f"Case id: `{case['id']}`")
                st.caption(f"Intent: `{case['intent'] or 'unspecified'}`")
                st.caption(f"Max turns: `{case['max_turns'] or '?'}`")
            with identity_col:
                persona_col, scenario_col = st.columns(2)
                with persona_col:
                    st.markdown("**Persona · who is testing the MCP**")
                    st.write(persona.get("summary", case["persona_summary"]) or "No summary.")
                    traits = persona.get("traits", [])
                    if traits:
                        st.caption("Traits: " + ", ".join(traits))
                    if persona.get("context"):
                        st.json(persona["context"])
                with scenario_col:
                    st.markdown("**Scenario · what they need to accomplish**")
                    st.write(case["goal"])
                    st.caption(f'Opening message: "{case["opening_message"]}"')
                    if case["situation"]:
                        st.caption("Situation: " + case["situation"])

                criteria_col, signals_col = st.columns(2)
                with criteria_col:
                    st.markdown("**Evaluation success criteria**")
                    if case["success_criteria"]:
                        for criterion in case["success_criteria"]:
                            st.markdown(f"- {criterion}")
                    else:
                        st.caption("No success criteria.")
                with signals_col:
                    st.markdown("**Failure signals to probe**")
                    if case["failure_signals"]:
                        for signal in case["failure_signals"]:
                            st.markdown(f"- {signal}")
                    else:
                        st.caption("No failure signals.")
                st.caption(
                    "Expected tools: "
                    + (", ".join(f"`{tool}`" for tool in case["exercises"]) or "none specified")
                )
                if st.session_state.get("target"):
                    st.markdown("**Resolved prompts for this case**")
                    st.caption(
                        "These previews show exactly where the persona and scenario are inserted. "
                        "During a live run, Ghostlab also inserts the current transcript and latest reply."
                    )
                    target = TargetConfig(**st.session_state["target"])
                    persona_config = load_persona(dataset_dir / "personas" / f"{case['persona']}.json")
                    scenario_config = load_scenario(dataset_dir / "scenarios" / f"{case['scenario']}.json")
                    agent_prompt_tab, emulator_prompt_tab = st.tabs(
                        ["Agent under test", "User emulator"]
                    )
                    with agent_prompt_tab:
                        st.caption(
                            "The MCP-enabled assistant receives the scenario goal and the current user message."
                        )
                        st.code(
                            build_aut_prompt(
                                target,
                                scenario_config,
                                [],
                                scenario_config.opening_message,
                                "<run directory>/target.mcp.json",
                            ),
                            language="markdown",
                        )
                    with emulator_prompt_tab:
                        st.caption(
                            "The user emulator role-plays this persona and pursues this scenario goal."
                        )
                        st.code(
                            build_user_emulator_prompt(
                                scenario_config,
                                [],
                                "<latest agent-under-test reply>",
                                persona_config,
                            ),
                            language="markdown",
                        )

    if st.button("Save case selection", type="primary", width="stretch"):
        for case_id, status in statuses.items():
            set_statuses(ds["manifest"], {case_id}, status)
        save_manifest(dataset_dir, ds["manifest"])
        st.success("Case statuses saved. Approved cases are ready to run.")


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown(
        """
        <div class="ghost-brand">
            <div class="ghost-mark">👻</div>
            <div class="ghost-brand-name">MCP Ghostlab</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("Test an MCP with realistic, codex-driven agent sessions.")

    st.divider()
    st.markdown("**Workflow progress**")
    steps = [
        ("MCP understood", bool(st.session_state["profile"])),
        ("Test cases ready", bool(st.session_state["dataset_dir"])),
        ("Cases run", bool(st.session_state["run_results"])),
        ("Results ready", bool(st.session_state["run_results"])),
    ]
    completed = sum(done for _, done in steps)
    st.progress(completed / len(steps), text=f"{completed} of {len(steps)} stages complete")
    for name, done in steps:
        state = " done" if done else ""
        st.markdown(
            f'<div class="ghost-step{state}"><span class="ghost-step-dot"></span>{name}</div>',
            unsafe_allow_html=True,
        )

    st.divider()
    st.markdown("**Session context**")
    st.caption("Model: **" + (st.session_state["model"] or "codex default") + "**")
    if st.session_state["target"]:
        st.caption("Target: `" + st.session_state["target"]["id"] + "`")
    if st.session_state["dataset_dir"]:
        st.caption("Dataset: `" + Path(st.session_state["dataset_dir"]).name + "`")

    with st.expander("Advanced settings"):
        st.session_state["workspace"] = st.text_input(
            "Workspace directory",
            value=st.session_state.get("workspace", "ghostlab_workspace"),
            help="Generated profiles, datasets, runs, and verdicts are written here.",
        )
        st.session_state["codex_bin"] = st.text_input(
            "Codex binary",
            value=st.session_state.get("codex_bin", ""),
            placeholder="Auto-detect",
        )
        st.session_state["model"] = st.text_input(
            "Codex model",
            value=st.session_state.get("model", ""),
            placeholder="Codex default",
            help="Passed to codex as -m and to the judge/generation backend.",
        )
        if st.button("Check codex", width="stretch"):
            try:
                chosen = st.session_state["codex_bin"] or resolve_codex_bin()
                st.success(f"Ready: {chosen}")
                st.caption(f"Model: {st.session_state['model'] or 'codex default'}")
            except CodexError as exc:
                st.error(str(exc))


tabs = st.tabs(
    [
        "1 · Inspect MCP",
        "2 · Build Test Cases",
        "3 · Run & Evaluate",
        "4 · Review Results",
    ]
)

# --------------------------------------------------------------------------- #
# 1. Inspect and understand the MCP
# --------------------------------------------------------------------------- #
with tabs[0]:
    st.markdown(
        """
        <div class="ghost-hero">
            <div class="ghost-eyebrow">Stage 1 · Inspect MCP</div>
            <h1>Understand what your MCP exposes</h1>
            <p>Connect the server, verify its tools and schemas, then let Ghostlab map the workflows needed to build realistic tests.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    setup_col, guide_col = st.columns([1.65, 1], gap="large")
    with setup_col:
        with st.container(border=True):
            st.subheader("Connection details")
            st.caption("Use a running HTTP endpoint or launch a local stdio server.")

            target_id = st.text_input(
                "Target name",
                value="my-mcp",
                placeholder="customer-support-mcp",
                help="A short identifier used in generated artifact names.",
            )
            transport = st.selectbox(
                "Transport",
                ["streamable-http", "sse", "stdio"],
                help="Choose the transport exposed by the MCP server.",
            )
            if transport == "stdio":
                command = st.text_input("Command", value="python", placeholder="python")
                args_raw = st.text_input(
                    "Arguments",
                    value="-m my_server",
                    placeholder="-m package.server",
                    help="Space-separated arguments passed to the command.",
                )
                url = ""
                connection_hint = f"`{command} {args_raw}`"
            else:
                url = st.text_input(
                    "Server URL",
                    value="http://localhost:8000/mcp",
                    placeholder="http://localhost:8000/mcp",
                )
                command, args_raw = "", ""
                connection_hint = f"`{url}`"

            st.caption(f"Ghostlab will connect via **{transport}** to {connection_hint}")
            inspect_clicked = st.button(
                "Connect and inspect",
                type="primary",
                width="stretch",
            )

        if inspect_clicked:
            if transport == "stdio":
                connection = {"command": command, "args": args_raw.split()}
            else:
                connection = {"url": url, "headers": {}}
            target = TargetConfig(id=target_id, transport=transport, connection=connection)
            try:
                with st.spinner("Connecting and mapping MCP capabilities..."):
                    result = inspect_target(target)
                out_dir = workspace() / f"{stamp()}-{target_id}-inspect"
                write_inspect_artifacts(result, out_dir)
                st.session_state["target"] = asdict(target)
                st.session_state["inspect"] = asdict(result)
                st.session_state["inspect_dir"] = str(out_dir)
                # New MCP invalidates downstream artifacts.
                st.session_state["profile"] = None
                st.session_state["dataset_dir"] = None
                st.success(f"Inspected {result.server_info.get('name', '?')}@{result.server_info.get('version', '?')} → {out_dir}")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Inspect failed: {exc}")

    with guide_col:
        with st.container(border=True):
            st.subheader("What Ghostlab discovers")
            st.markdown(
                """
                **Tools and schemas**

                Understand what agents can call and which inputs they need.

                **Resources and prompts**

                Capture the full surface area exposed by the server.

                **Description gaps**

                Flag references to tools that the server does not expose.
                """
            )
        st.info("After inspection, Ghostlab analyzes these capabilities so it can generate realistic personas and scenarios.")

    insp = st.session_state["inspect"]
    if insp:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Tools", len(insp["tools"]))
        c2.metric("Resources", len(insp["resources"]))
        c3.metric("Prompts", len(insp["prompts"]))
        c4.metric("Lint findings", len(insp["lint"]))
        if insp["lint"]:
            st.warning("Descriptions reference non-exposed tools:")
            st.table([{"referenced": f["referenced"], "in": f["in"]} for f in insp["lint"]])
        with st.expander("Tools"):
            for tool in insp["tools"]:
                st.markdown(f"**`{tool['name']}`** — {tool.get('title', '')}")
                st.caption((tool.get("description", "") or "")[:300])
        st.markdown("### Prepare test generation")
        st.caption(
            "Ghostlab turns the inspected tools into a capability map of categories and "
            "likely workflows. This internal analysis drives persona and scenario generation."
        )
        prompt_view("capability analysis", profile_prompt, st.session_state["inspect"])
        if st.button("Analyze MCP capabilities", type="primary"):
            try:
                with st.spinner("Analyzing tools and inferring workflows..."):
                    profile = build_capability_profile(st.session_state["inspect"], backend())
                write_profile_artifacts(profile, Path(st.session_state["inspect_dir"]))
                st.session_state["profile"] = profile
                st.success("MCP understood. You can now build test cases.")
            except CodexError as exc:
                st.error(f"codex error: {exc}")

    profile = st.session_state["profile"]
    if profile:
        st.markdown("### Capability analysis")
        st.write(profile.get("domain_summary", ""))
        analysis_col, workflow_col = st.columns(2)
        with analysis_col:
            st.markdown("**Tool categories**")
            for cat in profile.get("categories", []):
                st.markdown(f"- **{cat.get('label')}**: {cat.get('description')}")
        with workflow_col:
            st.markdown("**Inferred workflows**")
            for wf in profile.get("workflows", []):
                st.markdown(f"- **{wf.get('name')}**: " + " → ".join(f"`{s}`" for s in wf.get("steps", [])))
        st.success("Ready to build personas, scenarios, and runnable cases.")

# --------------------------------------------------------------------------- #
# 2. Build runnable test cases
# --------------------------------------------------------------------------- #
with tabs[1]:
    st.markdown(
        """
        <div class="ghost-hero">
            <div class="ghost-eyebrow">Stage 2 · Build test cases</div>
            <h1>Create the tests Ghostlab will run</h1>
            <p>Ghostlab generates personas and scenarios, then pairs them into concrete runnable cases.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.container(border=True):
        concept_cols = st.columns(3)
        concept_cols[0].markdown("**Persona**")
        concept_cols[0].caption("Who the emulated user is.")
        concept_cols[1].markdown("**Scenario**")
        concept_cols[1].caption("What that user needs to accomplish.")
        concept_cols[2].markdown("**Runnable case**")
        concept_cols[2].caption("One persona + one scenario. This becomes one run.")

    if not st.session_state["profile"]:
        st.info("Inspect and analyze an MCP first. Ghostlab needs its capability map before generating cases.")
    else:
        with st.container(border=True):
            st.subheader("Generation setup")
            c1, c2, c3 = st.columns(3)
            name = c1.text_input("Test set name", value=str(st.session_state["profile"].get("mcp", "mcp")).split("@")[0])
            n_personas = c1.number_input("Personas", min_value=1, max_value=20, value=2)
            spp = c2.number_input("Scenarios per persona", min_value=1, max_value=10, value=2)
            seed = c2.number_input("Seed", min_value=0, value=7)
            total_cases = int(n_personas) * int(spp)
            c3.metric("Cases to create", total_cases, help="Each persona is paired with its generated scenarios.")
            c3.caption(f"Using `{st.session_state['model'] or 'codex default'}`")
            prompt_view("persona generation", persona_prompt, st.session_state["profile"], int(n_personas))
            prompt_view("scenario generation", scenario_prompt, st.session_state["profile"], int(spp))
            generate_clicked = st.button("Generate personas, scenarios, and cases", type="primary", width="stretch")

        if generate_clicked:
            try:
                generation_progress = st.progress(0.0, text="Starting generation")
                generation_status = st.empty()
                total_units = int(n_personas) + 2

                def update_generation(event: dict) -> None:
                    phase = event["phase"]
                    if phase == "personas":
                        units = 1 if event["completed"] else 0
                    elif phase == "scenarios":
                        units = 1 + event["completed"]
                    else:
                        units = total_units if event["completed"] else total_units - 1
                    generation_progress.progress(
                        min(units / total_units, 1.0),
                        text=f"{units}/{total_units} generation stages complete",
                    )
                    generation_status.info(event["message"])

                with st.status("Building test cases", expanded=True) as generation_box:
                    dataset = build_dataset(
                        st.session_state["profile"],
                        backend(),
                        n_personas=int(n_personas),
                        scenarios_per_persona=int(spp),
                        seed=int(seed),
                        name=name,
                        progress=update_generation,
                    )
                    generation_box.update(label="Test cases generated", state="complete")
                out_dir = workspace() / "datasets" / name
                write_dataset(dataset, out_dir)
                st.session_state["dataset_dir"] = str(out_dir)
                st.success(f"Created {total_cases} runnable cases.")
            except CodexError as exc:
                st.error(f"codex error: {exc}")

    if st.session_state["dataset_dir"]:
        render_case_workspace(Path(st.session_state["dataset_dir"]))

# --------------------------------------------------------------------------- #
# 3. Run and evaluate
# --------------------------------------------------------------------------- #
with tabs[2]:
    st.markdown(
        """
        <div class="ghost-hero">
            <div class="ghost-eyebrow">Stage 3 · Run & evaluate</div>
            <h1>Run each selected test case</h1>
            <p>For every case, the user emulator role-plays its persona and pursues its scenario while the MCP-enabled agent responds.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if not st.session_state["dataset_dir"] or not st.session_state["target"]:
        st.info("Build runnable test cases first.")
    else:
        dataset_dir = Path(st.session_state["dataset_dir"])
        ds = load_dataset(dataset_dir)
        target = TargetConfig(**st.session_state["target"])
        st.caption(f"Target: `{target.id}` · model: `{st.session_state['model'] or 'codex default'}`")

        c1, c2, c3 = st.columns(3)
        approved_only = c1.checkbox("Run approved cases only", value=False)
        use_session = c1.checkbox("Persistent session runner", value=True)
        user_mock = c2.checkbox("Mock user (free, no codex)", value=False)
        do_eval = c2.checkbox("Evaluate (codex judge)", value=True)
        limit = c3.number_input("Limit cases (0 = all)", min_value=0, value=1)

        cases = ds["manifest"].get("cases", [])
        if approved_only:
            cases = [c for c in cases if c.get("status") == "approved"]
        if limit:
            cases = cases[: int(limit)]
        st.caption(f"{len(cases)} selected case(s) will run. Each case produces one trace and one optional verdict.")

        if cases:
            first = cases[0]
            persona0 = load_persona(dataset_dir / "personas" / f"{first['persona']}.json")
            scenario0 = load_scenario(dataset_dir / "scenarios" / f"{first['scenario']}.json")
            prompt_view(
                "agent under test · first selected case",
                build_aut_prompt, target, scenario0, [], scenario0.opening_message, "<mcp config>",
            )
            prompt_view(
                "user emulator · first selected case",
                build_user_emulator_prompt, scenario0, [], "<assistant reply>", persona0,
            )

        if st.button("▶️ Run", type="primary", disabled=not cases):
            runs_dir = workspace() / "runs"
            codex_bin = st.session_state.get("codex_bin", "")
            model = st.session_state.get("model", "")
            aut_cfg = codex_aut_runner(target, session=use_session, codex_bin=codex_bin, model=model)
            user_cfg = mock_runner() if user_mock else codex_user_runner(codex_bin=codex_bin, model=model)
            results = []
            overall_progress = st.progress(0.0, text=f"0/{len(cases)} cases complete")
            with st.status(f"Running {len(cases)} case(s)…", expanded=True) as status:
                for i, case in enumerate(cases, start=1):
                    persona = load_persona(dataset_dir / "personas" / f"{case['persona']}.json")
                    scenario = load_scenario(dataset_dir / "scenarios" / f"{case['scenario']}.json")
                    st.write(f"**[{i}/{len(cases)}] {case['id']}** ({case.get('intent', '?')})")
                    case_progress = st.progress(0.0, text=f"Starting case · 0/{scenario.max_turns} turns")
                    trace = st.container(border=True)

                    def show_event(event) -> None:
                        turn = event.data.get("turn", 0)
                        if event.type == "user_message":
                            case_progress.progress(
                                min((turn - 1) / scenario.max_turns, 1.0),
                                text=f"Turn {turn}/{scenario.max_turns} · user message",
                            )
                            with trace:
                                st.markdown(f"**User · turn {turn}**")
                                st.write(event.data.get("content", ""))
                        elif event.type == "aut_result":
                            case_progress.progress(
                                min((turn - 0.5) / scenario.max_turns, 1.0),
                                text=f"Turn {turn}/{scenario.max_turns} · agent responded",
                            )
                            with trace:
                                st.markdown(f"**Agent under test · turn {turn}**")
                                st.write(event.data.get("output", ""))
                                for call in event.data.get("tool_calls", []):
                                    name = f"{call.get('server', '?')}/{call.get('tool', '?')}"
                                    with st.expander(f"Tool call · {name} · {call.get('status', '?')}"):
                                        if call.get("arguments") is not None:
                                            st.caption("Arguments")
                                            st.json(call["arguments"])
                                        if call.get("result") is not None:
                                            st.caption("Result")
                                            st.json(call["result"])
                                        if call.get("error"):
                                            st.error(call["error"])
                        elif event.type == "user_emulator_result":
                            case_progress.progress(
                                min(turn / scenario.max_turns, 1.0),
                                text=f"Turn {turn}/{scenario.max_turns} · user emulator responded",
                            )
                        elif event.type == "run_finished":
                            case_progress.progress(1.0, text=f"Conversation complete · {event.data.get('status', '?')}")

                    try:
                        run = run_scenario(
                            target=target, scenario=scenario, aut_runner_config=aut_cfg,
                            user_runner_config=user_cfg, output_dir=runs_dir, persona=persona,
                            event_callback=show_event,
                        )
                        row = {
                            "case": case["id"], "intent": case.get("intent", ""), "status": run.status,
                            "turns": run.turns, "run_dir": str(run.run_dir), "verdict": "-",
                        }
                        if do_eval:
                            case_progress.progress(1.0, text="Conversation complete · evaluating with codex judge")
                            st.write("   · evaluating trace and tool evidence…")
                            verdict = evaluate_run(run.run_dir, backend(), st.session_state["profile"])
                            write_verdict_artifacts(verdict, run.run_dir)
                            row["verdict"] = verdict["verdict"]
                        results.append(row)
                        st.write(f"   → **{row['status']}** · {row['turns']} turns · verdict **{row['verdict']}**")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"   case failed: {exc}")
                    overall_progress.progress(
                        i / len(cases),
                        text=f"{i}/{len(cases)} cases complete",
                    )
                status.update(label=f"Done — ran {len(results)}/{len(cases)} case(s)", state="complete")
            st.session_state["run_results"] = results

    if st.session_state["run_results"]:
        st.subheader("Results")
        st.dataframe(
            [{k: r[k] for k in ("case", "intent", "status", "turns", "verdict")} for r in st.session_state["run_results"]],
            width="stretch",
        )
        st.success("Runs complete. Review their traces and verdicts in Results.")

# --------------------------------------------------------------------------- #
# 4. Review results and traces
# --------------------------------------------------------------------------- #
with tabs[3]:
    st.markdown(
        """
        <div class="ghost-hero">
            <div class="ghost-eyebrow">Stage 4 · Review results</div>
            <h1>Understand how every case went</h1>
            <p>A trace is the complete record of one case run: messages, tool calls, results, and evaluation evidence.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    runs_dir = workspace() / "runs"
    run_dirs = sorted([p for p in runs_dir.glob("*") if (p / "events.jsonl").exists()], reverse=True) if runs_dir.exists() else []
    if not run_dirs:
        st.info("No results yet. Run one or more cases first.")
    else:
        run_index = []
        for path in run_dirs:
            indexed_run = read_run(path)
            verdict_path = path / "verdict.json"
            verdict_name = "not evaluated"
            if verdict_path.exists():
                verdict_name = json.loads(verdict_path.read_text(encoding="utf-8")).get("verdict", "unknown")
            run_index.append(
                {
                    "path": path,
                    "run": indexed_run,
                    "target": indexed_run.get("target", {}).get("id", "unknown"),
                    "status": indexed_run.get("status", "unknown"),
                    "verdict": verdict_name,
                    "scenario": indexed_run.get("scenario", {}).get("title")
                    or indexed_run.get("scenario", {}).get("id", path.name),
                }
            )

        st.markdown("### Find a run")
        filter_col1, filter_col2, filter_col3 = st.columns(3)
        targets = ["All targets", *sorted({item["target"] for item in run_index})]
        statuses = ["All statuses", *sorted({item["status"] for item in run_index})]
        verdicts = ["All verdicts", *sorted({item["verdict"] for item in run_index})]
        target_filter = filter_col1.selectbox("Target", targets)
        status_filter = filter_col2.selectbox("Run status", statuses)
        verdict_filter = filter_col3.selectbox("Verdict", verdicts)
        search = st.text_input("Search runs", placeholder="Scenario, target, or run id")
        filtered_runs = [
            item
            for item in run_index
            if (target_filter == "All targets" or item["target"] == target_filter)
            and (status_filter == "All statuses" or item["status"] == status_filter)
            and (verdict_filter == "All verdicts" or item["verdict"] == verdict_filter)
            and (
                not search
                or search.lower() in f"{item['scenario']} {item['target']} {item['path'].name}".lower()
            )
        ]
        st.caption(f"{len(filtered_runs)} of {len(run_index)} runs shown")
        if not filtered_runs:
            st.info("No runs match these filters.")
            st.stop()
        choice = st.selectbox(
            "Run",
            [item["path"].name for item in filtered_runs],
            format_func=lambda name: next(
                f"{item['scenario']} · {item['target']} · {item['verdict']} · {name[:20]}"
                for item in filtered_runs
                if item["path"].name == name
            ),
        )
        selected = next(item for item in filtered_runs if item["path"].name == choice)
        run_dir = selected["path"]
        run = selected["run"]

        scenario = run["scenario"]
        persona = run.get("persona") or {}
        target = run.get("target") or {}
        assistant_turns = sum(1 for item in run["trace"] if item.get("role") == "assistant")
        st.subheader(f"{scenario.get('title') or scenario.get('id', '?')}")
        h1, h2, h3, h4, h5 = st.columns(5)
        h1.metric("Run status", run["status"])
        h2.metric("Turns", assistant_turns)
        h3.metric("Tool calls", len(run["tool_calls"]))
        h4.metric("Target", target.get("id", "?"))
        h5.metric("Duration", duration_label(run.get("started_at"), run.get("finished_at")))
        models = run.get("models", {})
        st.caption(
            f"Started: `{run.get('started_at') or 'unknown'}` · "
            f"Agent model: `{models.get('agent_under_test', 'unknown')}` · "
            f"User-emulator model: `{models.get('user_emulator', 'unknown')}`"
        )

        with st.expander("Case setup", expanded=True):
            persona_col, scenario_col = st.columns(2)
            with persona_col:
                st.markdown("**Persona · who the user emulator role-plays**")
                st.write(persona.get("name") or persona.get("id") or "No reusable persona.")
                st.caption(persona.get("summary", ""))
                if persona.get("traits"):
                    st.caption("Traits: " + ", ".join(persona["traits"]))
                if persona.get("context"):
                    st.json(persona["context"])
            with scenario_col:
                st.markdown("**Scenario · what this run tests**")
                st.write(scenario.get("goal", "No goal recorded."))
                st.caption(f'Opening message: "{scenario.get("opening_message", "")}"')
                if scenario.get("exercises"):
                    st.caption("Expected tools: " + ", ".join(f"`{tool}`" for tool in scenario["exercises"]))

        with st.expander("Exact runtime prompts"):
            if not run.get("prompts"):
                st.caption("This older run predates persisted runtime prompts.")
            for prompt in run.get("prompts", []):
                role = "Agent under test" if prompt["type"] == "aut_prompt" else "User emulator"
                resume = " · stateful resume message" if prompt.get("stateful_resume") else ""
                st.markdown(f"**{role} · turn {prompt.get('turn', '?')}{resume}**")
                st.code(prompt.get("prompt", ""), language="markdown")
            evaluation = run.get("evaluation", {})
            if evaluation.get("prompt"):
                st.markdown(f"**Codex judge · model `{evaluation.get('model', 'unknown')}`**")
                st.code(evaluation["prompt"], language="markdown")

        st.markdown("### Chronological trace")
        st.caption("Tool calls are shown inside the assistant turn that made them.")
        if not run["trace"]:
            st.info("This run does not contain turn-level events.")
        else:
            jump_links = [
                f"[{item.get('role', '?')} {item.get('turn', '?')}](#trace-{item.get('role', '?')}-{item.get('turn', '?')})"
                for item in run["trace"]
            ]
            st.markdown("Jump to turn: " + " · ".join(jump_links))
        for item in run["trace"]:
            role = item.get("role", "?")
            st.markdown(
                f'<span id="trace-{role}-{item.get("turn", "?")}"></span>',
                unsafe_allow_html=True,
            )
            with st.chat_message("user" if role == "user" else "assistant"):
                label = "User emulator" if role == "user" else "Agent under test"
                st.caption(f"{label} · turn {item.get('turn', '?')} · {item.get('timestamp', '')}")
                st.write(item.get("content", ""))
                for call in item.get("tool_calls", []):
                    name = f"{call.get('server', '?')}/{call.get('tool', '?')}"
                    tool_anchor = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                    st.markdown(
                        f'<span id="trace-tool-{item.get("turn", "?")}-{tool_anchor}"></span>',
                        unsafe_allow_html=True,
                    )
                    with st.expander(f"Tool call · {name} · {call.get('status', '?')}"):
                        if call.get("arguments") is not None:
                            st.caption("Arguments")
                            st.json(call["arguments"])
                        if call.get("result") is not None:
                            st.caption("Result")
                            st.json(call["result"])
                        if call.get("error"):
                            st.error(call["error"])

        st.markdown("### Evaluation")
        verdict_path = run_dir / "verdict.json"
        if verdict_path.exists():
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            color = {"pass": "green", "partial": "orange", "fail": "red"}.get(verdict["verdict"], "gray")
            st.markdown(f"**Verdict:** :{color}[{verdict['verdict'].upper()}]")
            if verdict["gates"]:
                st.error("Gates: " + ", ".join(verdict["gates"]))
            st.write(verdict["judge"].get("summary", ""))
            st.markdown("**Success criteria**")
            for item in verdict["judge"].get("criteria", []):
                st.markdown(f"- {'✅' if item.get('met') else '❌'} {item.get('evidence', '')}")
                refs = evidence_references(run, item.get("evidence", ""))
                if refs:
                    st.markdown(
                        "Likely evidence: "
                        + " · ".join(f"[{ref}](#{trace_anchor(ref)})" for ref in refs)
                    )
            st.markdown("**Failure signals**")
            for item in verdict["judge"].get("failure_signals", []):
                st.markdown(f"- {'⚠️ triggered' if item.get('triggered') else '✅ clear'}: {item.get('evidence', '')}")
                refs = evidence_references(run, item.get("evidence", ""))
                if refs:
                    st.markdown(
                        "Likely evidence: "
                        + " · ".join(f"[{ref}](#{trace_anchor(ref)})" for ref in refs)
                    )
            det = verdict.get("deterministic", {})
            st.caption(f"coverage {det.get('coverage', 'n/a')} · failed calls: {', '.join(det.get('tool_failures', [])) or 'none'}")
        else:
            st.caption("No verdict yet. Run this case with evaluation enabled.")
        prompt_view("judge", judge_prompt, run, st.session_state.get("profile"))
