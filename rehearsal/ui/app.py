"""Streamlit app driving the whole MCP Ghostlab pipeline.

Launch via `ghostlab ui`. Walks an MCP through: connect & inspect -> capability
profile -> generate a persona x scenario dataset (with tunable parameters) ->
review & curate -> run multi-turn sims + evaluate -> view traces and verdicts.
Every codex-backed stage exposes its model and its exact prompt.
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
from rehearsal.evaluate import evaluate_run, judge_prompt, read_run, write_verdict_artifacts
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


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.title("👻 MCP Ghostlab")
    st.caption("Validate any MCP end-to-end with codex-driven agents.")

    st.session_state["workspace"] = st.text_input("Workspace dir", value=st.session_state.get("workspace", "ghostlab_workspace"))
    st.session_state["codex_bin"] = st.text_input("Codex binary (blank = auto)", value=st.session_state.get("codex_bin", ""))
    st.session_state["model"] = st.text_input(
        "Codex model (blank = codex default)",
        value=st.session_state.get("model", ""),
        help="Passed to codex as -m and to the judge/generation backend.",
    )
    if st.button("Check codex", use_container_width=True):
        try:
            chosen = st.session_state["codex_bin"] or resolve_codex_bin()
            st.success(f"codex: {chosen}")
            st.caption(f"model: {st.session_state['model'] or 'codex default'}")
        except CodexError as exc:
            st.error(str(exc))

    st.divider()
    st.markdown("**Pipeline**")
    steps = [
        ("Inspected", bool(st.session_state["inspect"])),
        ("Profiled", bool(st.session_state["profile"])),
        ("Dataset", bool(st.session_state["dataset_dir"])),
        ("Runs", bool(st.session_state["run_results"])),
    ]
    for name, done in steps:
        st.write(("✅ " if done else "⬜ ") + name)

    st.divider()
    st.caption("Model in use: **" + (st.session_state["model"] or "codex default") + "**")
    if st.session_state["target"]:
        st.caption("Target: `" + st.session_state["target"]["id"] + "`")
    if st.session_state["dataset_dir"]:
        st.caption("Dataset: `" + Path(st.session_state["dataset_dir"]).name + "`")


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
    st.caption("Start here. Point at a running MCP server; everything downstream flows from this.")
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
            # New MCP invalidates downstream artifacts.
            st.session_state["profile"] = None
            st.session_state["dataset_dir"] = None
            st.success(f"Inspected {result.server_info.get('name', '?')}@{result.server_info.get('version', '?')} → {out_dir}")
        except Exception as exc:  # noqa: BLE001
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
        with st.expander("Tools"):
            for tool in insp["tools"]:
                st.markdown(f"**`{tool['name']}`** — {tool.get('title', '')}")
                st.caption((tool.get("description", "") or "")[:300])
        st.success("✅ Ready for step 2 (Profile).")

# --------------------------------------------------------------------------- #
# 2. Profile
# --------------------------------------------------------------------------- #
with tabs[1]:
    st.header("Build a capability profile")
    if not st.session_state["inspect"]:
        st.info("⬅️ Inspect an MCP first (step 1).")
    else:
        st.caption(f"Target: `{st.session_state['target']['id']}` · model: `{st.session_state['model'] or 'codex default'}`")
        prompt_view("capability profile", profile_prompt, st.session_state["inspect"])
        if st.button("🧭 Build profile (codex)", type="primary"):
            try:
                with st.spinner("Summarizing capabilities and inferring workflows..."):
                    profile = build_capability_profile(st.session_state["inspect"], backend())
                write_profile_artifacts(profile, Path(st.session_state["inspect_dir"]))
                st.session_state["profile"] = profile
                st.success(f"Capability profile ready → {st.session_state['inspect_dir']}/capabilities.json")
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
        st.success("✅ Ready for step 3 (Generate Dataset).")

# --------------------------------------------------------------------------- #
# 3. Generate Dataset
# --------------------------------------------------------------------------- #
with tabs[2]:
    st.header("Generate a persona × scenario dataset")
    if not st.session_state["profile"]:
        st.info("⬅️ Build a capability profile first (step 2).")
    else:
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Dataset name", value=str(st.session_state["profile"].get("mcp", "mcp")).split("@")[0])
        n_personas = c1.number_input("Personas", min_value=1, max_value=20, value=2)
        spp = c2.number_input("Scenarios per persona", min_value=1, max_value=10, value=2)
        seed = c2.number_input("Seed", min_value=0, value=7)
        st.caption(f"Will generate {int(n_personas)} personas × {int(spp)} scenarios = {int(n_personas) * int(spp)} cases · model: `{st.session_state['model'] or 'codex default'}`")
        prompt_view("persona generation", persona_prompt, st.session_state["profile"], int(n_personas))
        prompt_view("scenario generation (per persona)", scenario_prompt, st.session_state["profile"], int(spp))
        if c3.button("🧬 Generate dataset (codex)", type="primary"):
            try:
                with st.spinner(f"Generating {int(n_personas)} personas and {int(n_personas) * int(spp)} scenarios..."):
                    dataset = build_dataset(
                        st.session_state["profile"], backend(),
                        n_personas=int(n_personas), scenarios_per_persona=int(spp), seed=int(seed), name=name,
                    )
                out_dir = workspace() / "datasets" / name
                write_dataset(dataset, out_dir)
                st.session_state["dataset_dir"] = str(out_dir)
                st.success(f"Dataset written → {out_dir}")
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
        st.success("✅ Ready for step 4 (Review) or step 5 (Run).")

# --------------------------------------------------------------------------- #
# 4. Review & Curate
# --------------------------------------------------------------------------- #
with tabs[3]:
    st.header("Review & curate the dataset")
    if not st.session_state["dataset_dir"]:
        st.info("⬅️ Generate a dataset first (step 3).")
    else:
        dataset_dir = Path(st.session_state["dataset_dir"])
        ds = load_dataset(dataset_dir)
        if ensure_statuses(ds["manifest"]):
            save_manifest(dataset_dir, ds["manifest"])
        review = build_review(ds, st.session_state["profile"])

        st.subheader("Tool coverage")
        cov = review.get("coverage", {})
        if cov:
            st.table([{"category": k, "exercised": f"{v['exercised']}/{v['total']}"} for k, v in cov["by_category"].items()])
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
        if st.button("💾 Save curation", type="primary"):
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
        st.info("⬅️ Generate a dataset first (step 3).")
    else:
        dataset_dir = Path(st.session_state["dataset_dir"])
        ds = load_dataset(dataset_dir)
        target = TargetConfig(**st.session_state["target"])
        st.caption(f"Target: `{target.id}` · model: `{st.session_state['model'] or 'codex default'}`")

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

        # Prompt preview for the first case.
        if cases:
            first = cases[0]
            persona0 = load_persona(dataset_dir / "personas" / f"{first['persona']}.json")
            scenario0 = load_scenario(dataset_dir / "scenarios" / f"{first['scenario']}.json")
            prompt_view(
                "agent-under-test (first turn, first case)",
                build_aut_prompt, target, scenario0, [], scenario0.opening_message, "<mcp config>",
            )
            prompt_view(
                "user emulator (first case)",
                build_user_emulator_prompt, scenario0, [], "<assistant reply>", persona0,
            )

        if st.button("▶️ Run", type="primary", disabled=not cases):
            runs_dir = workspace() / "runs"
            codex_bin = st.session_state.get("codex_bin", "")
            model = st.session_state.get("model", "")
            aut_cfg = codex_aut_runner(target, session=use_session, codex_bin=codex_bin, model=model)
            user_cfg = mock_runner() if user_mock else codex_user_runner(codex_bin=codex_bin, model=model)
            results = []
            with st.status(f"Running {len(cases)} case(s)…", expanded=True) as status:
                for i, case in enumerate(cases, start=1):
                    persona = load_persona(dataset_dir / "personas" / f"{case['persona']}.json")
                    scenario = load_scenario(dataset_dir / "scenarios" / f"{case['scenario']}.json")
                    st.write(f"**[{i}/{len(cases)}] {case['id']}** ({case.get('intent', '?')})")
                    try:
                        run = run_scenario(
                            target=target, scenario=scenario, aut_runner_config=aut_cfg,
                            user_runner_config=user_cfg, output_dir=runs_dir, persona=persona,
                        )
                        row = {
                            "case": case["id"], "intent": case.get("intent", ""), "status": run.status,
                            "turns": run.turns, "run_dir": str(run.run_dir), "verdict": "-",
                        }
                        if do_eval:
                            st.write("   · judging…")
                            verdict = evaluate_run(run.run_dir, backend(), st.session_state["profile"])
                            write_verdict_artifacts(verdict, run.run_dir)
                            row["verdict"] = verdict["verdict"]
                        results.append(row)
                        st.write(f"   → **{row['status']}** · {row['turns']} turns · verdict **{row['verdict']}**")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"   case failed: {exc}")
                status.update(label=f"Done — ran {len(results)}/{len(cases)} case(s)", state="complete")
            st.session_state["run_results"] = results

    if st.session_state["run_results"]:
        st.subheader("Results")
        st.dataframe(
            [{k: r[k] for k in ("case", "intent", "status", "turns", "verdict")} for r in st.session_state["run_results"]],
            use_container_width=True,
        )
        st.success("✅ Open step 6 (Trace Viewer) to inspect any run.")

# --------------------------------------------------------------------------- #
# 6. Trace Viewer
# --------------------------------------------------------------------------- #
with tabs[5]:
    st.header("Trace & judgement viewer")
    runs_dir = workspace() / "runs"
    run_dirs = sorted([p for p in runs_dir.glob("*") if (p / "events.jsonl").exists()], reverse=True) if runs_dir.exists() else []
    if not run_dirs:
        st.info("No runs yet. Run some cases in step 5.")
    else:
        choice = st.selectbox("Run", [p.name for p in run_dirs])
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

        st.markdown("### Judgement")
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
            st.markdown("**Failure signals**")
            for item in verdict["judge"].get("failure_signals", []):
                st.markdown(f"- {'⚠️ triggered' if item.get('triggered') else '✅ clear'}: {item.get('evidence', '')}")
            det = verdict.get("deterministic", {})
            st.caption(f"coverage {det.get('coverage', 'n/a')} · failed calls: {', '.join(det.get('tool_failures', [])) or 'none'}")
        else:
            st.caption("No verdict yet — run with Evaluate enabled in step 5.")
        prompt_view("judge", judge_prompt, run, st.session_state.get("profile"))
