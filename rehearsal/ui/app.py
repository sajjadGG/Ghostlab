"""Streamlit app driving the whole MCP Ghostlab pipeline.

Launch via `ghostlab ui`. Mirrors `ghostlab create`'s job-based flow: pick or
create a job, discover its target, configure the agent-under-test host,
generate/curate the test plan, run selected suites, and review results —
by calling the exact same `rehearsal.cli` command handlers the CLI uses, so
the UI and CLI never drift into two different pipelines.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

import streamlit as st

from rehearsal.cli import cmd_discover, cmd_plan, cmd_review_spec, cmd_test
from rehearsal.codex_backend import CodexError, resolve_codex_bin
from rehearsal.config import ConfigError, load_persona, load_scenario, load_target
from rehearsal.jobs import (
    add_aut_host,
    build_codex_aut_runner,
    create_job,
    default_job_spec,
    jobs_dir,
    resolve_job,
    slugify,
    target_from_url,
)
from rehearsal.plan import load_test_plan, set_case_statuses, write_test_plan
from rehearsal.spec import load_spec

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
        background: radial-gradient(circle at 78% 0%, rgba(124, 92, 255, 0.08), transparent 25rem), transparent;
    }
    [data-testid="stSidebar"] { border-right: 1px solid var(--ghost-border); }
    .ghost-brand { display: flex; align-items: center; gap: 0.7rem; margin-bottom: 0.2rem; }
    .ghost-mark {
        display: grid; width: 2.25rem; height: 2.25rem; place-items: center;
        border: 1px solid rgba(124, 92, 255, 0.28); border-radius: 0.75rem;
        background: var(--ghost-accent-soft); font-size: 1.15rem;
    }
    .ghost-brand-name { font-size: 1.12rem; font-weight: 700; letter-spacing: -0.02em; }
    .ghost-hero { max-width: 52rem; padding: 1.2rem 0 1.4rem; }
    .ghost-hero h1 { margin: 0.35rem 0 0.6rem; font-size: clamp(2rem, 4vw, 3.25rem); letter-spacing: -0.055em; line-height: 1.05; }
    .ghost-hero p { max-width: 42rem; margin: 0; color: #7b7f8b; font-size: 1.02rem; }
    .ghost-eyebrow { color: #7c5cff; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; }
    div[data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--ghost-border); border-radius: 1rem; }
    div.stButton > button[kind="primary"] { border-color: #6c4df2; background: #6c4df2; }
    div.stButton > button[kind="primary"]:hover { border-color: #8065f4; background: #8065f4; }
    </style>
    """,
    unsafe_allow_html=True,
)

E2E_SUITES = ("semantic", "security", "error-recovery")


def _spec_path() -> Path:
    return resolve_job(st.session_state["job"])


def _job_dir() -> Path:
    return _spec_path().parent


def _spec():
    return load_spec(_spec_path())


def run_cli(fn, **fields) -> tuple[int, str]:
    """Call a `rehearsal.cli` command handler for the active job, capturing its
    stdout so the UI can show exactly what the CLI would have printed.
    """
    ns = argparse.Namespace(job=st.session_state["job"], spec=None, db=None, **fields)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = fn(ns)
    except Exception as exc:  # noqa: BLE001 — surface the error in the log instead of a crash
        buf.write(f"\nerror: {exc}\n")
        rc = 1
    return rc, buf.getvalue()


def _resolve_path(job_dir: Path, ref: str) -> Path:
    path = Path(ref)
    return path if path.is_absolute() else job_dir / path


def _load_events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def render_trace(run_dir: Path) -> None:
    for event in _load_events(run_dir):
        etype = event.get("type")
        data = event.get("data", {})
        turn = data.get("turn", "?")
        if etype == "user_message":
            with st.chat_message("user"):
                st.caption(f"User emulator · turn {turn}")
                st.write(data.get("content", ""))
        elif etype == "aut_result":
            with st.chat_message("assistant"):
                st.caption(f"Agent under test · turn {turn}")
                st.write(data.get("output", ""))
                for call in data.get("tool_calls") or []:
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
        elif etype == "widgets_shown":
            names = ", ".join(w.get("tool", "?") for w in data.get("widgets") or [])
            st.caption(f"▣ widget shown → user can fill: {names}")
        elif etype == "run_finished":
            st.caption(f"→ conversation {data.get('status', '?')}")


def render_verdict(run_dir: Path) -> None:
    verdict_path = run_dir / "verdict.json"
    if not verdict_path.exists():
        st.caption("No verdict recorded for this run (judge disabled or not yet run).")
        return
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    color = {"pass": "green", "partial": "orange", "fail": "red"}.get(verdict["verdict"], "gray")
    st.markdown(f"**Verdict:** :{color}[{verdict['verdict'].upper()}]")
    if verdict.get("gates"):
        st.error("Gates: " + ", ".join(verdict["gates"]))
    judge = verdict.get("judge", {})
    st.write(judge.get("summary", ""))
    crit_col, sig_col = st.columns(2)
    with crit_col:
        st.markdown("**Success criteria**")
        for item in judge.get("criteria", []):
            st.markdown(f"- {'✅' if item.get('met') else '❌'} {item.get('evidence', '')}")
    with sig_col:
        st.markdown("**Failure signals**")
        for item in judge.get("failure_signals", []):
            st.markdown(f"- {'⚠️ triggered' if item.get('triggered') else '✅ clear'} {item.get('evidence', '')}")
    det = verdict.get("deterministic", {})
    st.caption(
        f"coverage {det.get('coverage', 'n/a')} · "
        f"failed calls: {', '.join(det.get('tool_failures', [])) or 'none'}"
    )


# --------------------------------------------------------------------------- #
# Sidebar: job picker (everything else needs an active job)
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
    st.caption("Create → discover → configure → plan → run → review — one job at a time.")
    st.divider()

    existing_jobs = sorted(p.parent.name for p in jobs_dir().glob("*/job.yaml")) if jobs_dir().exists() else []
    options = ["+ New job", *existing_jobs]
    default_index = options.index(st.session_state["job"]) if st.session_state.get("job") in existing_jobs else 0
    choice = st.selectbox("Job", options, index=default_index)

    if choice == "+ New job":
        with st.form("new_job_form"):
            new_name = st.text_input("Job name", placeholder="cortex-eval")
            new_target = st.text_input(
                "Target MCP URL or config path", placeholder="http://localhost:8000/mcp"
            )
            create_clicked = st.form_submit_button("Create job", type="primary")
        if create_clicked:
            if not new_name or not new_target:
                st.error("A job name and target are both required.")
            else:
                target_path = Path(new_target)
                try:
                    if target_path.suffix.lower() == ".json" and target_path.exists():
                        target = load_target(target_path)
                        source_target = str(target_path)
                    else:
                        target = target_from_url(new_target)
                        source_target = ""
                    spec = default_job_spec(new_name, target=target, source_target=source_target)
                    create_job(new_name, spec)
                    st.session_state["job"] = slugify(new_name)
                    st.rerun()
                except ConfigError as exc:
                    st.error(str(exc))
        st.stop()

    st.session_state["job"] = choice
    spec = _spec()
    st.caption(f"Target: `{spec.target_config().id}` · {spec.target_config().transport}")
    host_kinds = [h.get("kind") for h in spec.hosts]
    st.caption(
        "Semantic host: " + ("✅ configured" if any(k in ("process", "codex-session") for k in host_kinds)
                              else "⚠️ not configured")
    )
    with st.expander("Advanced"):
        st.session_state["codex_bin"] = st.text_input("Codex binary override", value=st.session_state.get("codex_bin", ""))
        st.session_state["model"] = st.text_input("Model override", value=st.session_state.get("model", ""))

codex_bin = st.session_state.get("codex_bin", "") or ""
model = st.session_state.get("model", "") or ""

tabs = st.tabs(["1 · Discover", "2 · Configure & Plan", "3 · Run", "4 · Results"])

# --------------------------------------------------------------------------- #
# 1. Discover
# --------------------------------------------------------------------------- #
with tabs[0]:
    st.markdown(
        """
        <div class="ghost-hero">
            <div class="ghost-eyebrow">Stage 1 · Discover</div>
            <h1>Inspect the target</h1>
            <p>Connects to the MCP, lints its contract, and probes any MCP Apps widgets.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    spec = _spec()
    target = spec.target_config()
    st.caption(f"`{target.transport}` → {target.connection.get('url') or target.connection.get('command') or ''}")
    if st.button("Run discover", type="primary"):
        with st.spinner("Connecting and inspecting..."):
            rc, log = run_cli(cmd_discover, timeout=30.0, skip_apps=False, sample="off",
                               approve_mutations=False, approve_destructive=False,
                               skip_setup=False, strict=False)
        (st.success if rc == 0 else st.error)("Discover finished" if rc == 0 else "Discover failed")
        st.code(log or "(no output)")

    caps = (_spec().capabilities or {})
    if caps.get("tools"):
        c1, c2 = st.columns(2)
        c1.metric("Tools", len(caps["tools"]))
        c2.metric("UI resources", len(caps.get("ui_resources", [])))
        with st.expander("Discovered tools"):
            st.table([{"tool": t["name"], "labels": ", ".join(t.get("labels", []))} for t in caps["tools"]])
    else:
        st.info("No discover artifacts yet — run discover above.")

# --------------------------------------------------------------------------- #
# 2. Configure the agent-under-test host + generate/curate the plan
# --------------------------------------------------------------------------- #
with tabs[1]:
    st.markdown(
        """
        <div class="ghost-hero">
            <div class="ghost-eyebrow">Stage 2 · Configure &amp; plan</div>
            <h1>Set up semantic testing and generate the plan</h1>
            <p>Semantic/security suites need a real agent-under-test session; the coverage plan needs discover to have run first.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    spec = _spec()
    has_aut = any(h.get("kind") in ("process", "codex-session") for h in spec.hosts)
    with st.container(border=True):
        st.subheader("Agent-under-test host")
        if has_aut:
            for host in spec.hosts:
                if host.get("kind") in ("process", "codex-session"):
                    st.success(f"`{host['id']}` ({host['kind']}) — {host.get('config_ref', '')}")
        else:
            try:
                resolve_codex_bin()
                codex_available = True
            except CodexError:
                codex_available = False
            if codex_available:
                st.warning("No agent-under-test host yet — semantic/security suites will skip.")
                if st.button("Set up semantic/E2E testing with codex", type="primary"):
                    runner_config = build_codex_aut_runner(spec)
                    runner_path = add_aut_host(spec, _spec_path(), runner_config)
                    st.success(f"Wrote {runner_path} and added it as host `aut`.")
                    st.rerun()
            else:
                st.error(
                    "codex not found on this machine — semantic/security suites will skip "
                    "until a host is configured (see README's Runner Configs section)."
                )

    if not (spec.capabilities or {}).get("tools"):
        st.info("Run discover (tab 1) before generating a plan.")
    else:
        with st.container(border=True):
            st.subheader("Generate the test plan")
            gen = spec.generation or {}
            c1, c2, c3 = st.columns(3)
            personas = c1.number_input("Personas", min_value=1, max_value=20, value=int(gen.get("personas", 2)))
            spp = c2.number_input("Scenarios per persona", min_value=1, max_value=10, value=int(gen.get("scenarios_per_persona", 2)))
            regenerate = c3.checkbox("Regenerate (new codex calls)", value=False)
            if st.button("Generate plan", type="primary"):
                with st.spinner("Generating persona/scenario cases and building the coverage plan..."):
                    rc, log = run_cli(
                        cmd_plan, out=None, approve=None, reject=None, generate=True,
                        regenerate=regenerate, personas=int(personas), scenarios_per_persona=int(spp),
                        codex_bin=codex_bin, model=model,
                    )
                (st.success if rc == 0 else st.error)("Plan generated" if rc == 0 else "Plan generation failed")
                st.code(log or "(no output)")

        plan_path = _job_dir() / "test-plan.yaml"
        if plan_path.exists():
            plan = load_test_plan(plan_path)
            st.markdown("### Suites")
            suite_rows = [
                {"suite": name, "cases": entry["cases"]}
                for name, entry in plan["suites"].items() if entry["cases"]
            ]
            st.table(suite_rows)
            gaps = plan["coverage"]["gaps"]
            if gaps:
                with st.expander(f"Coverage gaps ({len(gaps)})"):
                    for gap in gaps:
                        st.caption(f"- {gap}")

            e2e_cases = [c for c in plan["cases"] if c["suite"] in E2E_SUITES and not c["execution"].get("needs_generation")]
            if e2e_cases:
                st.markdown("### Semantic / security cases")
                job_dir = _job_dir()
                status_options = ["proposed", "approved", "rejected", "needs-edit"]
                pending_statuses: dict[str, str] = {}
                for case in e2e_cases:
                    with st.expander(f"{case['id']} — {case['title']}"):
                        try:
                            scenario = load_scenario(_resolve_path(job_dir, case["execution"]["scenario"]))
                            persona = (
                                load_persona(_resolve_path(job_dir, case["execution"]["persona"]))
                                if case["execution"].get("persona") else None
                            )
                            st.write(f"**Goal:** {scenario.goal}")
                            st.caption(f"Opening message: \"{scenario.opening_message}\"")
                            if persona:
                                st.caption(f"Persona: {persona.name} — {persona.summary}")
                        except (ConfigError, OSError, KeyError) as exc:
                            st.caption(f"(scenario detail unavailable: {exc})")
                        st.caption("Tools: " + (", ".join(case["tools"]) or "none"))
                        pending_statuses[case["id"]] = st.selectbox(
                            "Status", status_options, index=status_options.index(case["status"]),
                            key=f"status-{case['id']}",
                        )
                if st.button("Save case statuses"):
                    for status in set(pending_statuses.values()):
                        ids = [cid for cid, s in pending_statuses.items() if s == status]
                        if ids:
                            set_case_statuses(plan, set(ids), status)
                    write_test_plan(plan, plan_path)
                    st.success("Saved.")
                    st.rerun()
        else:
            st.info("No test plan yet — generate one above.")

# --------------------------------------------------------------------------- #
# 3. Run
# --------------------------------------------------------------------------- #
with tabs[2]:
    st.markdown(
        """
        <div class="ghost-hero">
            <div class="ghost-eyebrow">Stage 3 · Run</div>
            <h1>Execute the plan</h1>
            <p>Runs every selected suite against the job's configured hosts. Semantic/security cases are live, judged, multi-turn conversations — this can take minutes.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    plan_path = _job_dir() / "test-plan.yaml"
    if not plan_path.exists():
        st.info("Generate a test plan first (tab 2).")
    else:
        plan = load_test_plan(plan_path)
        suite_names = [name for name, entry in plan["suites"].items() if entry["cases"]]
        selected_suites = st.multiselect("Suites to run", suite_names, default=suite_names)
        c1, c2, c3, c4 = st.columns(4)
        approved_only = c1.checkbox("Approved cases only", value=False)
        apps_mode = c2.checkbox("Apps mode (render + drive widgets)", value=False)
        judge = c3.checkbox("Judge conversational runs", value=True)
        repeat = c4.number_input("Repeat", min_value=1, max_value=5, value=1)

        if st.button("▶️ Run tests", type="primary", disabled=not selected_suites):
            suite_arg = None if set(selected_suites) == set(suite_names) else selected_suites
            with st.spinner(f"Running {', '.join(selected_suites)}... semantic/security cases run a live judged conversation."):
                rc, log = run_cli(
                    cmd_test, plan=None, suite=suite_arg, hosts=None, approved_only=approved_only,
                    user_runner=None, apps_mode=apps_mode, skip_setup=False, timeout=30.0,
                    repeat=int(repeat), profile=None, strict=False, judge=judge,
                    codex_bin=codex_bin, model=model,
                )
            (st.success if rc == 0 else st.error)("Run finished" if rc == 0 else "Run finished with failures")
            st.code(log or "(no output)")
            st.info("See tab 4 for a structured breakdown and traces.")

# --------------------------------------------------------------------------- #
# 4. Results
# --------------------------------------------------------------------------- #
with tabs[3]:
    st.markdown(
        """
        <div class="ghost-hero">
            <div class="ghost-eyebrow">Stage 4 · Results</div>
            <h1>Review runs, traces, and the readiness gate</h1>
        </div>
        """,
        unsafe_allow_html=True,
    )
    spec = _spec()
    result_dirs = sorted((_spec_path().resolve().parent / "workspace").glob("test/*/results.json"), reverse=True)
    if not result_dirs:
        st.info("No test runs yet — run tests in tab 3.")
    else:
        labels = {str(p): p.parent.name for p in result_dirs}
        choice = st.selectbox("Run", list(labels), format_func=lambda p: labels[p])
        results_path = Path(choice)
        results = json.loads(results_path.read_text(encoding="utf-8"))

        totals = results["totals"]
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Executed", results["executed"])
        m2.metric("Pass", totals["pass"])
        m3.metric("Fail", totals["fail"])
        m4.metric("Error", totals["error"])
        m5.metric("Pass rate", "n/a" if results["pass_rate"] is None else f"{results['pass_rate']:.0%}")

        st.markdown("### Cases")
        status_color = {"pass": "green", "fail": "red", "error": "red", "skip": "gray"}
        for entry in results["results"]:
            color = status_color.get(entry["status"], "gray")
            with st.expander(
                f":{color}[{entry['status'].upper()}] {entry['case']} [{entry['suite']}/{entry['kind']}] on {entry['host']}"
            ):
                if entry.get("detail"):
                    st.caption(entry["detail"])
                run_dir = entry.get("artifacts", {}).get("run_dir")
                if run_dir:
                    st.markdown("**Conversation trace**")
                    render_trace(Path(run_dir))
                    st.markdown("**Evaluation**")
                    render_verdict(Path(run_dir))

        st.markdown("### Readiness / release gate")
        rc, log = run_cli(cmd_review_spec, results=results_path, strict=False)
        readiness_path = results_path.parent / "readiness.json"
        if readiness_path.exists():
            readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
            verdict_color = {"ready": "green", "needs-work": "orange", "not-ready": "red"}.get(readiness["verdict"], "gray")
            st.markdown(f"**Verdict:** :{verdict_color}[{readiness['verdict'].upper()}]")
            for gate in readiness["gates"]:
                marker = {"pass": "✅", "fail": "❌", "not-evaluated": "⏸️"}[gate["status"]]
                st.markdown(f"{marker} **{gate['gate']}** — {gate['detail']}")
            if readiness["repairs"]:
                st.markdown("**Top repairs**")
                for repair in readiness["repairs"][:5]:
                    st.markdown(f"- P{repair['priority']} {repair['kind']}: {repair.get('detail', '')}")
        else:
            st.caption(log or "(readiness unavailable)")
