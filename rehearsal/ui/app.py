"""Streamlit app driving the whole MCP Ghostlab pipeline.

Launch via `ghostlab ui`. Walks an MCP through: connect & inspect -> capability
profile -> generate a persona x scenario dataset (with tunable parameters) ->
review & curate -> run multi-turn sims + evaluate -> view traces and verdicts.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

from rehearsal.codex_backend import CodexBackend, CodexError, resolve_codex_bin
from rehearsal.config import TargetConfig, load_persona, load_scenario
from rehearsal.dataset import build_dataset, write_dataset
from rehearsal.evaluate import evaluate_run, read_run, write_verdict_artifacts
from rehearsal.generate import generate_scenarios  # noqa: F401  (kept for parity)
from rehearsal.inspect import inspect_target, write_inspect_artifacts
from rehearsal.orchestrator import run_scenario
from rehearsal.profile import build_capability_profile, write_profile_artifacts
from rehearsal.review import (
    build_review,
    ensure_statuses,
    load_dataset,
    save_manifest,
    set_statuses,
)
from rehearsal.runner_presets import codex_aut_runner, codex_user_runner, mock_runner

st.set_page_config(page_title="MCP Ghostlab", page_icon="👻", layout="wide")

# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
DEFAULTS = {
    "target": None,
    "inspect": None,
    "inspect_dir": None,
    "profile": None,
    "dataset_dir": None,
    "run_results": [],
}
for key, value in DEFAULTS.items():
    st.session_state.setdefault(key, value)


def workspace() -> Path:
    path = Path(st.session_state.get("workspace", "ghostlab_workspace"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def backend() -> CodexBackend:
    return CodexBackend(bin_path=st.session_state.get("codex_bin", ""))


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


# --------------------------------------------------------------------------- #
# Sidebar: global config
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.title("👻 MCP Ghostlab")
    st.caption("Validate any MCP end-to-end with codex-driven agents.")
    st.session_state["workspace"] = st.text_input("Workspace dir", value="ghostlab_workspace")
    st.session_state["codex_bin"] = st.text_input(
        "Codex binary (blank = auto-detect)", value=st.session_state.get("codex_bin", "")
    )
    if st.button("Check codex"):
        try:
            st.success(f"codex: {resolve_codex_bin() if not st.session_state['codex_bin'] else st.session_state['codex_bin']}")
        except CodexError as exc:
            st.error(str(exc))

    st.divider()
    st.markdown("**Pipeline state**")
    st.write("✅ inspected" if st.session_state["inspect"] else "⬜ inspect")
    st.write("✅ profiled" if st.session_state["profile"] else "⬜ profile")
    st.write("✅ dataset" if st.session_state["dataset_dir"] else "⬜ dataset")
    st.write(f"✅ {len(st.session_state['run_results'])} runs" if st.session_state["run_results"] else "⬜ runs")


tabs = st.tabs(
    [
        "1 · Connect & Inspect",
        "2 · Profile",
        "3 · Generate Dataset",
        "4 · Review & Curate",
        "5 · Run & Evaluate",
        "6 · Trace Viewer",
    ]
)

# --------------------------------------------------------------------------- #
# 1. Connect & Inspect
# --------------------------------------------------------------------------- #
with tabs[0]:
    st.header("Connect to an MCP and inspect it")
    col1, col2 = st.columns(2)
    with col1:
        target_id = st.text_input("Target id", value="my-mcp")
        transport = st.selectbox("Transport", ["streamable-http", "sse", "stdio"])
    with col2:
        if transport == "stdio":
            command = st.text_input("Command", value="python")
            args_raw = st.text_input("Args (space separated)", value="-m my_server")
            url = ""
        else:
            url = st.text_input("URL", value="http://localhost:8000/mcp")
            command, args_raw = "", ""

    if st.button("🔍 Inspect", type="primary"):
        if transport == "stdio":
            connection = {"command": command, "args": args_raw.split()}
        else:
            connection = {"url": url, "headers": {}}
        target = TargetConfig(id=target_id, transport=transport, connection=connection)
        try:
            with st.spinner("Connecting and introspecting the MCP..."):
                result = inspect_target(target)
            out_dir = workspace() / f"{stamp()}-{target_id}-inspect"
            write_inspect_artifacts(result, out_dir)
            st.session_state["target"] = asdict(target)
            st.session_state["inspect"] = asdict(result)
            st.session_state["inspect_dir"] = str(out_dir)
            st.success(f"Inspected {result.server_info.get('name', '?')}@{result.server_info.get('version', '?')}")
        except Exception as exc:  # noqa: BLE001 - surface any transport error in UI
            st.error(f"Inspect failed: {exc}")

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
        with st.expander("Tools", expanded=False):
            for tool in insp["tools"]:
                st.markdown(f"**`{tool['name']}`** — {tool.get('title', '')}")
                st.caption((tool.get("description", "") or "")[:300])

# --------------------------------------------------------------------------- #
# 2. Profile
# --------------------------------------------------------------------------- #
with tabs[1]:
    st.header("Build a capability profile")
    if not st.session_state["inspect"]:
        st.info("Inspect an MCP first.")
    else:
        if st.button("🧭 Build profile (codex)", type="primary"):
            try:
                with st.spinner("Summarizing capabilities and inferring workflows..."):
                    profile = build_capability_profile(st.session_state["inspect"], backend())
                write_profile_artifacts(profile, Path(st.session_state["inspect_dir"]))
                st.session_state["profile"] = profile
                st.success("Capability profile ready.")
            except CodexError as exc:
                st.error(f"codex error: {exc}")

    profile = st.session_state["profile"]
    if profile:
        st.subheader(profile.get("mcp", "?"))
        st.write(profile.get("domain_summary", ""))
        st.markdown("**Tool categories**")
        for cat in profile.get("categories", []):
            st.markdown(f"- **{cat.get('label')}** (`{cat.get('key')}`): {cat.get('description')}")
        st.markdown("**Inferred workflows**")
        for wf in profile.get("workflows", []):
            st.markdown(f"- **{wf.get('name')}**: " + " → ".join(f"`{s}`" for s in wf.get("steps", [])))
        gaps = profile.get("gaps", {}).get("missing_referenced_tools", [])
        if gaps:
            st.warning("Gaps (referenced but not exposed): " + ", ".join(f"`{g}`" for g in gaps))

# --------------------------------------------------------------------------- #
# 3. Generate Dataset
# --------------------------------------------------------------------------- #
with tabs[2]:
    st.header("Generate a persona × scenario dataset")
    if not st.session_state["profile"]:
        st.info("Build a capability profile first.")
    else:
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Dataset name", value=str(st.session_state["profile"].get("mcp", "mcp")).split("@")[0])
        n_personas = c1.number_input("Personas", min_value=1, max_value=20, value=2)
        spp = c2.number_input("Scenarios per persona", min_value=1, max_value=10, value=2)
        seed = c2.number_input("Seed", min_value=0, value=7)
        st.caption(f"Will generate {n_personas} personas × {spp} scenarios = {n_personas * spp} cases.")
        if c3.button("🧬 Generate dataset (codex)", type="primary"):
            try:
                with st.spinner(f"Generating {n_personas} personas and {n_personas * spp} scenarios..."):
                    dataset = build_dataset(
                        st.session_state["profile"],
                        backend(),
                        n_personas=int(n_personas),
                        scenarios_per_persona=int(spp),
                        seed=int(seed),
                        name=name,
                    )
                out_dir = workspace() / "datasets" / name
                write_dataset(dataset, out_dir)
                st.session_state["dataset_dir"] = str(out_dir)
                st.success(f"Dataset written to {out_dir}")
            except CodexError as exc:
                st.error(f"codex error: {exc}")

    if st.session_state["dataset_dir"]:
        ds = load_dataset(Path(st.session_state["dataset_dir"]))
        st.markdown(f"**Personas ({len(ds['personas'])})**")
        for p in ds["personas"].values():
            with st.expander(f"{p['id']} — {p.get('name', '')}"):
                st.write(p.get("summary", ""))
                st.caption("traits: " + ", ".join(p.get("traits", [])))
                st.json(p.get("context", {}))
        st.markdown(f"**Scenarios ({len(ds['scenarios'])})**")
        for s in ds["scenarios"].values():
            with st.expander(f"[{s.get('intent', '?')}] {s['id']}"):
                st.write("**goal:** " + s.get("goal", ""))
                st.write("**opening:** " + s.get("opening_message", ""))
                st.caption("exercises: " + ", ".join(s.get("exercises", [])))

# --------------------------------------------------------------------------- #
# 4. Review & Curate
# --------------------------------------------------------------------------- #
with tabs[3]:
    st.header("Review & curate the dataset")
    if not st.session_state["dataset_dir"]:
        st.info("Generate a dataset first.")
    else:
        dataset_dir = Path(st.session_state["dataset_dir"])
        ds = load_dataset(dataset_dir)
        if ensure_statuses(ds["manifest"]):
            save_manifest(dataset_dir, ds["manifest"])
        review = build_review(ds, st.session_state["profile"])

        st.subheader("Tool coverage")
        cov = review.get("coverage", {})
        if cov:
            st.table(
                [{"category": k, "exercised": f"{v['exercised']}/{v['total']}"} for k, v in cov["by_category"].items()]
            )
            if cov.get("unexercised_tools"):
                st.caption("Never exercised: " + ", ".join(f"`{t}`" for t in cov["unexercised_tools"]))

        if review["flags"]:
            st.warning("Flags")
            st.table([{"kind": f["kind"], "detail": f["detail"]} for f in review["flags"]])
        else:
            st.success("No flags.")

        st.subheader("Cases")
        statuses: dict[str, str] = {}
        options = ["pending", "approved", "rejected", "needs-edit"]
        for case in review["cases"]:
            with st.expander(f"[{case['status']}] {case['id']} ({case['intent']})"):
                st.write(f"**persona:** {case['persona']} — {case['persona_summary']}")
                if case["situation"]:
                    st.caption("situation: " + case["situation"])
                st.write("**goal:** " + case["goal"])
                st.write("**opening:** " + case["opening_message"])
                if case["success_criteria"]:
                    st.markdown("**success criteria**")
                    for c in case["success_criteria"]:
                        st.markdown(f"- {c}")
                if case["failure_signals"]:
                    st.markdown("**failure signals**")
                    for c in case["failure_signals"]:
                        st.markdown(f"- {c}")
                st.caption("exercises: " + ", ".join(case["exercises"]))
                statuses[case["id"]] = st.selectbox(
                    "status", options, index=options.index(case["status"]), key=f"status-{case['id']}"
                )
        if st.button("💾 Save curation"):
            for case_id, status in statuses.items():
                set_statuses(ds["manifest"], {case_id}, status)
            save_manifest(dataset_dir, ds["manifest"])
            st.success("Statuses saved to dataset.json")

# --------------------------------------------------------------------------- #
# 5. Run & Evaluate
# --------------------------------------------------------------------------- #
with tabs[4]:
    st.header("Run multi-turn sims & evaluate")
    if not st.session_state["dataset_dir"] or not st.session_state["target"]:
        st.info("Generate a dataset first.")
    else:
        dataset_dir = Path(st.session_state["dataset_dir"])
        ds = load_dataset(dataset_dir)
        target = TargetConfig(**st.session_state["target"])

        c1, c2, c3 = st.columns(3)
        approved_only = c1.checkbox("Approved cases only", value=False)
        use_session = c1.checkbox("Persistent session runner", value=True)
        user_mock = c2.checkbox("Mock user (free, no codex)", value=False)
        do_eval = c2.checkbox("Evaluate (codex judge)", value=True)
        limit = c3.number_input("Limit cases (0 = all)", min_value=0, value=1)

        cases = ds["manifest"].get("cases", [])
        if approved_only:
            cases = [c for c in cases if c.get("status") == "approved"]
        if limit:
            cases = cases[: int(limit)]
        st.caption(f"{len(cases)} case(s) will run.")

        if st.button("▶️ Run", type="primary"):
            runs_dir = workspace() / "runs"
            codex_bin = st.session_state.get("codex_bin", "")
            aut_cfg = codex_aut_runner(target, session=use_session, codex_bin=codex_bin)
            user_cfg = mock_runner() if user_mock else codex_user_runner(codex_bin=codex_bin)
            results = []
            progress = st.progress(0.0)
            log = st.container()
            for i, case in enumerate(cases, start=1):
                persona = load_persona(dataset_dir / "personas" / f"{case['persona']}.json")
                scenario = load_scenario(dataset_dir / "scenarios" / f"{case['scenario']}.json")
                log.write(f"▶ [{i}/{len(cases)}] {case['id']} ({case.get('intent', '?')})")
                try:
                    run = run_scenario(
                        target=target,
                        scenario=scenario,
                        aut_runner_config=aut_cfg,
                        user_runner_config=user_cfg,
                        output_dir=runs_dir,
                        persona=persona,
                    )
                    row = {
                        "case": case["id"],
                        "intent": case.get("intent", ""),
                        "status": run.status,
                        "turns": run.turns,
                        "run_dir": str(run.run_dir),
                        "verdict": "-",
                    }
                    if do_eval:
                        verdict = evaluate_run(run.run_dir, backend(), st.session_state["profile"])
                        write_verdict_artifacts(verdict, run.run_dir)
                        row["verdict"] = verdict["verdict"]
                    results.append(row)
                    log.write(f"   → {row['status']} ({row['turns']} turns) verdict={row['verdict']}")
                except Exception as exc:  # noqa: BLE001
                    log.error(f"   case failed: {exc}")
                progress.progress(i / max(1, len(cases)))
            st.session_state["run_results"] = results
            st.success(f"Ran {len(results)} case(s).")

    if st.session_state["run_results"]:
        st.subheader("Results")
        st.table(
            [{k: r[k] for k in ("case", "intent", "status", "turns", "verdict")} for r in st.session_state["run_results"]]
        )

# --------------------------------------------------------------------------- #
# 6. Trace Viewer
# --------------------------------------------------------------------------- #
with tabs[5]:
    st.header("Trace & judgement viewer")
    runs_dir = workspace() / "runs"
    run_dirs = sorted([p for p in runs_dir.glob("*") if (p / "events.jsonl").exists()], reverse=True) if runs_dir.exists() else []
    if not run_dirs:
        st.info("No runs yet. Run some cases first.")
    else:
        labels = [p.name for p in run_dirs]
        choice = st.selectbox("Run", labels)
        run_dir = runs_dir / choice
        run = read_run(run_dir)

        st.subheader(f"{run['scenario'].get('id', '?')} — status `{run['status']}`")
        vc1, vc2 = st.columns([2, 1])
        with vc1:
            st.markdown("### Transcript")
            for turn in run["transcript"]:
                role = turn.get("role", "?")
                with st.chat_message("user" if role == "user" else "assistant"):
                    st.write(turn.get("content", ""))
        with vc2:
            st.markdown("### Tool calls")
            if not run["tool_calls"]:
                st.caption("No tool calls captured.")
            for call in run["tool_calls"]:
                name = f"{call.get('server', '?')}/{call.get('tool', '?')}"
                with st.expander(f"{name} · {call.get('status', '?')}"):
                    if call.get("arguments") is not None:
                        st.caption("arguments")
                        st.json(call["arguments"])
                    if call.get("result") is not None:
                        st.caption("result")
                        st.json(call["result"])
                    if call.get("error"):
                        st.error(call["error"])

        verdict_path = run_dir / "verdict.json"
        st.markdown("### Judgement")
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
            st.markdown("**Failure signals**")
            for item in verdict["judge"].get("failure_signals", []):
                st.markdown(f"- {'⚠️ triggered' if item.get('triggered') else '✅ clear'}: {item.get('evidence', '')}")
            det = verdict.get("deterministic", {})
            st.caption(f"coverage {det.get('coverage', 'n/a')} · failed calls: {', '.join(det.get('tool_failures', [])) or 'none'}")
        else:
            st.caption("No verdict yet — run with Evaluate enabled, or evaluate this run from the CLI.")
