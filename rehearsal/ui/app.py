"""Streamlit app driving the complete Ghostlab agent-evaluation pipeline.

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
import shlex
from pathlib import Path

import streamlit as st

from rehearsal.agents import load_agent_definition
from rehearsal.cli import _openshell_status, cmd_discover, cmd_plan, cmd_review_spec, cmd_test
from rehearsal.codex_backend import CodexError, resolve_codex_bin
from rehearsal.config import ConfigError, load_persona, load_scenario, load_target
from rehearsal.dashboard import build_dashboard
from rehearsal.jobs import (
    add_aut_host,
    build_codex_aut_runner,
    configure_codex_runner,
    create_job,
    default_agent_job_spec,
    default_job_spec,
    default_skill_job_spec,
    jobs_dir,
    materialize_job_runners,
    refresh_job_runners,
    resolve_job,
    slugify,
    target_from_url,
    update_agent_runtime,
)
from rehearsal.plan import load_test_plan, set_case_statuses, write_test_plan
from rehearsal.resolved_config import resolved_job_config
from rehearsal.spec import load_spec, save_spec

st.set_page_config(page_title="Ghostlab", page_icon="👻", layout="wide")

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
    .ghost-brand-sub { color: #7b7f8b; font-size: 0.76rem; }
    .ghost-hero { max-width: 52rem; padding: 1.2rem 0 1.4rem; }
    .ghost-hero h1 { margin: 0.35rem 0 0.6rem; font-size: clamp(2rem, 4vw, 3.25rem); letter-spacing: -0.055em; line-height: 1.05; }
    .ghost-hero p { max-width: 42rem; margin: 0; color: #7b7f8b; font-size: 1.02rem; }
    .ghost-eyebrow { color: #7c5cff; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase; }
    div[data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--ghost-border); border-radius: 1rem; }
    div.stButton > button[kind="primary"] { border-color: #6c4df2; background: #6c4df2; }
    div.stButton > button[kind="primary"]:hover { border-color: #8065f4; background: #8065f4; }
    .pipeline { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.55rem; margin:.4rem 0 1.4rem; }
    .pipeline-step { border:1px solid var(--ghost-border); border-radius:.8rem; padding:.72rem .8rem; color:#7b7f8b; font-size:.78rem; }
    .pipeline-step strong { display:block; color:inherit; font-size:.82rem; }
    .pipeline-step.done { border-color:rgba(38,166,91,.35); background:rgba(38,166,91,.08); color:#27915a; }
    .pipeline-step.next { border-color:rgba(124,92,255,.38); background:var(--ghost-accent-soft); color:#7c5cff; }
    .config-card { border:1px solid var(--ghost-border); border-radius:1rem; padding:1rem 1.1rem; background:rgba(127,127,127,.035); }
    @media (max-width: 760px) { .pipeline { grid-template-columns:1fr 1fr; } }
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


@st.cache_data(ttl=30)
def openshell_status() -> tuple[bool, str]:
    return _openshell_status()


def run_cli(fn, *, live: bool = False, **fields) -> tuple[int, str]:
    """Call a `rehearsal.cli` command handler for the active job, capturing its
    stdout so the UI can show exactly what the CLI would have printed.
    """
    ns = argparse.Namespace(job=st.session_state["job"], spec=None, db=None, **fields)
    placeholder = st.empty() if live else None

    class LiveBuffer(io.StringIO):
        def write(self, value: str) -> int:
            written = super().write(value)
            if placeholder is not None:
                visible = self.getvalue()[-8000:]
                placeholder.code(visible or "Starting…", language=None)
            return written

    buf = LiveBuffer()
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


def _pipeline_state(spec_path: Path) -> list[tuple[str, bool]]:
    spec = load_spec(spec_path)
    job_dir = spec_path.parent
    discovered = bool((spec.capabilities or {}).get("generated_from"))
    planned = (job_dir / "test-plan.yaml").exists()
    tested = any(spec.workspace_dir(spec_path).glob("test/*/results.json"))
    return [("Discover", discovered), ("Plan", planned), ("Run", tested), ("Review", tested)]


def render_pipeline(spec_path: Path) -> None:
    stages = _pipeline_state(spec_path)
    first_pending = next((index for index, (_, done) in enumerate(stages) if not done), None)
    cards = []
    for index, (label, done) in enumerate(stages):
        state = "done" if done else "next" if index == first_pending else ""
        marker = "Complete" if done else "Next" if state == "next" else "Pending"
        cards.append(
            f'<div class="pipeline-step {state}"><strong>{index + 1}. {label}</strong>{marker}</div>'
        )
    st.markdown(f'<div class="pipeline">{"".join(cards)}</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Sidebar: job picker (everything else needs an active job)
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown(
        """
        <div class="ghost-brand">
            <div class="ghost-mark">👻</div>
            <div><div class="ghost-brand-name">Ghostlab</div><div class="ghost-brand-sub">Agent evaluation lab</div></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption("Configure → discover → plan → run → review")
    st.divider()

    existing_jobs = sorted(p.parent.name for p in jobs_dir().glob("*/job.yaml")) if jobs_dir().exists() else []
    options = ["+ New job", *existing_jobs]
    default_index = options.index(st.session_state["job"]) if st.session_state.get("job") in existing_jobs else 0
    choice = st.selectbox("Job", options, index=default_index)

    if choice != "+ New job":
        st.session_state["job"] = choice
        spec = _spec()
        target = spec.target_config()
        st.caption(f"Subject: `{target.id}` · {spec.target_type}")
        st.caption(f"Sandbox: `{(spec.sandbox or {}).get('backend', 'openshell')}`")
        if (spec.sandbox or {}).get("backend", "openshell") == "openshell":
            ready, _detail = openshell_status()
            st.caption("OpenShell: " + ("✅ connected" if ready else "❌ unavailable · run ghostlab doctor"))
        host_kinds = [h.get("kind") for h in spec.hosts]
        st.caption(
            "Semantic host: " + ("✅ configured" if any(k in ("process", "codex-session", "copilot-session") for k in host_kinds)
                                  else "⚠️ not configured")
        )
        with st.expander("Advanced"):
            st.session_state["codex_bin"] = st.text_input("Codex binary override", value=st.session_state.get("codex_bin", ""))
            st.session_state["model"] = st.text_input("Model override", value=st.session_state.get("model", ""))

if choice == "+ New job":
    st.markdown(
        """
        <div class="ghost-hero">
            <div class="ghost-eyebrow">New evaluation</div>
            <h1>Configure the agent you want to test</h1>
            <p>Start with a complete agent, one MCP, or one skill. Ghostlab resolves all three into the same editable job.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    q1, q2 = st.columns(2)
    subject = q1.radio("Evaluation subject", ["Agent config", "MCP", "Skill"], horizontal=True)
    sandbox = q2.selectbox("Execution", ["openshell", "local"], help="OpenShell is isolated and recommended. Local executes trusted code directly on this host.")
    with st.form("new_job_form"):
        c1, c2 = st.columns([1.1, 1])
        with c1:
            new_name = st.text_input("Job name", placeholder="release-agent-eval")
            source_labels = {
                "Agent config": ("Agent JSON/YAML path", "examples/agent.json"),
                "MCP": ("MCP URL or config path", "http://localhost:8000/mcp"),
                "Skill": ("SKILL.md or skill directory", "./skills/release-notes"),
            }
            label, placeholder = source_labels[subject]
            source = st.text_input(label, placeholder=placeholder)
        with c2:
            image = st.text_input("OpenShell image", value="base", disabled=sandbox == "local")
            providers = st.text_input("OpenShell providers", placeholder="openai, github", disabled=sandbox == "local")
            g1, g2 = st.columns(2)
            personas = g1.number_input("Personas", 1, 20, 2)
            scenarios = g2.number_input("Scenarios each", 1, 10, 2)
            min_rate = st.slider("Minimum pass rate", 0.0, 1.0, 0.9, 0.05)
        with st.expander("Agent runners and models", expanded=True):
            b1, b2 = st.columns(2)
            create_aut_backend = b1.selectbox(
                "AUT runner", ["codex", "opencode", "copilot"]
            )
            create_user_backend = b2.selectbox(
                "User runner", ["codex", "opencode", "copilot"],
                index=["codex", "opencode", "copilot"].index(create_aut_backend),
            )
            m1, m2, m3, m4 = st.columns(4)
            create_aut_model = m1.text_input("AUT model", placeholder="Runner default")
            create_user_model = m2.text_input("User model", placeholder="AUT / runner default")
            create_generation_model = m3.text_input("Generation model", placeholder="AUT / default")
            create_judge_model = m4.text_input("Judge model", placeholder="Generation / default")
            r1, r2, r3, r4 = st.columns(4)
            lifecycle_options = (
                ["copilot-session", "process"]
                if create_aut_backend == "copilot"
                else ["process", "codex-session"]
                if create_aut_backend == "codex"
                else ["process"]
            )
            create_runner_kind = r1.selectbox("Runner lifecycle", lifecycle_options)
            create_runner_timeout = r2.number_input("Turn timeout", 10, 3600, 600)
            create_approval = r3.selectbox(
                "Codex approval mode",
                ["never", "on-request", "untrusted"],
                disabled=create_aut_backend != "codex",
            )
            create_codex_sandbox = r4.selectbox(
                "Codex sandbox",
                ["read-only", "workspace-write", "danger-full-access"],
                disabled=create_aut_backend != "codex",
            )
            a1, a2 = st.columns(2)
            create_aut_agent = a1.text_input(
                "AUT custom agent",
                placeholder="VS Code/Copilot custom agent name",
                disabled=create_aut_backend != "copilot",
            )
            create_user_agent = a2.text_input(
                "User custom agent",
                placeholder="Optional Copilot custom agent name",
                disabled=create_user_backend != "copilot",
            )
            c1, c2, c3, c4 = st.columns(4)
            create_aut_effort = c1.selectbox(
                "AUT reasoning",
                ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
                disabled=create_aut_backend != "copilot",
            )
            create_user_effort = c2.selectbox(
                "User reasoning",
                ["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"],
                disabled=create_user_backend != "copilot",
            )
            create_aut_context = c3.selectbox(
                "AUT context", ["default", "long_context"],
                disabled=create_aut_backend != "copilot",
            )
            create_user_context = c4.selectbox(
                "User context", ["default", "long_context"],
                disabled=create_user_backend != "copilot",
            )
            x1, x2 = st.columns(2)
            create_codex_bin = x1.text_input("Codex executable", value="codex")
            create_copilot_bin = x2.text_input("Copilot executable", value="copilot")
            create_aut_extra = st.text_input(
                "Additional AUT Copilot argv",
                placeholder="--available-tools=shell,read",
                disabled=create_aut_backend != "copilot",
            )
            create_user_extra = st.text_input(
                "Additional user Copilot argv",
                disabled=create_user_backend != "copilot",
            )
        st.caption("Nothing runs yet. This creates an editable job; use the guided stages to discover, plan, run, and review.")
        create_clicked = st.form_submit_button("Create evaluation", type="primary", use_container_width=True)

    if create_clicked:
        if not new_name or not source:
            st.error("Job name and subject source are required.")
        else:
            try:
                generation = {"personas": int(personas), "scenarios_per_persona": int(scenarios)}
                gates = {"min_pass_rate": float(min_rate)}
                source_path = Path(source).expanduser()
                if subject == "Agent config":
                    agent, agent_sandbox = load_agent_definition(source_path)
                    spec = default_agent_job_spec(new_name, agent=agent, sandbox=agent_sandbox, generation=generation, review_gates=gates)
                    spec.source_target = str(source_path)
                elif subject == "Skill":
                    spec = default_skill_job_spec(new_name, skill_path=source_path, generation=generation, review_gates=gates)
                else:
                    if source_path.suffix.lower() == ".json" and source_path.exists():
                        target = load_target(source_path)
                        source_target = str(source_path)
                    else:
                        target = target_from_url(source)
                        source_target = ""
                    spec = default_job_spec(new_name, target=target, source_target=source_target, generation=generation, review_gates=gates)
                spec.sandbox["backend"] = sandbox
                if sandbox == "openshell":
                    spec.sandbox["image"] = image or "base"
                    spec.sandbox["providers"] = [item.strip() for item in providers.split(",") if item.strip()]
                aut_runtime = {
                    **dict((spec.agent or {}).get("runtime") or {}),
                    "backend": create_aut_backend,
                    "model": create_aut_model,
                    "kind": create_runner_kind,
                    "timeout_seconds": int(create_runner_timeout),
                }
                if create_aut_backend == "codex":
                    aut_runtime.update({
                        "approval_mode": create_approval,
                        "codex_sandbox": create_codex_sandbox,
                        "codex_bin": create_codex_bin,
                    })
                elif create_aut_backend == "copilot":
                    aut_runtime.update({
                        "agent": create_aut_agent,
                        "reasoning_effort": "" if create_aut_effort == "default" else create_aut_effort,
                        "context": create_aut_context,
                        "copilot_bin": create_copilot_bin,
                        "extra_args": shlex.split(create_aut_extra),
                    })
                user_runtime = {
                    "backend": create_user_backend,
                    "model": create_user_model or create_aut_model,
                    "timeout_seconds": int(create_runner_timeout),
                }
                if create_user_backend == "copilot":
                    user_runtime.update({
                        "kind": "copilot-session",
                        "agent": create_user_agent,
                        "reasoning_effort": "" if create_user_effort == "default" else create_user_effort,
                        "context": create_user_context,
                        "copilot_bin": create_copilot_bin,
                        "extra_args": shlex.split(create_user_extra),
                    })
                elif create_user_backend == "codex":
                    user_runtime["codex_bin"] = create_codex_bin
                agent_payload = {**(spec.agent or {}), "runtime": aut_runtime}
                existing_runner = dict(agent_payload.get("runner") or {})
                existing_parser = str(existing_runner.get("parser") or "")
                existing_command = list(existing_runner.get("command") or [])
                existing_binary = (
                    Path(str(existing_command[0])).name if existing_command else ""
                )
                existing_backend = (
                    "copilot"
                    if existing_parser == "copilot-json" or existing_binary == "copilot"
                    else "opencode"
                    if existing_parser.startswith("opencode") or existing_binary == "opencode"
                    else "codex"
                    if existing_parser == "codex-json" or existing_binary == "codex"
                    else ""
                )
                if existing_backend == "codex" and create_aut_backend == "codex":
                    agent_payload["runner"] = configure_codex_runner(
                        existing_runner,
                        model=create_aut_model,
                        kind=create_runner_kind,
                        timeout_seconds=int(create_runner_timeout),
                        approval_mode=create_approval,
                        codex_sandbox=create_codex_sandbox,
                        codex_bin=create_codex_bin,
                    )
                elif existing_backend:
                    agent_payload.pop("runner", None)
                spec.agent = agent_payload
                spec.generation = {
                    **(spec.generation or {}),
                    "model": create_generation_model
                    or (create_aut_model if create_aut_backend != "copilot" else ""),
                }
                spec.test = {
                    **(spec.test or {}),
                    "user_runtime": user_runtime,
                    "user_model": user_runtime["model"],
                    "judge_model": create_judge_model
                    or create_generation_model
                    or (create_aut_model if create_aut_backend != "copilot" else ""),
                }
                created_path = create_job(new_name, spec)
                materialize_job_runners(created_path)
                st.session_state["job"] = slugify(new_name)
                st.success("Evaluation created. Opening the guided pipeline…")
                st.rerun()
            except (ConfigError, OSError, ValueError) as exc:
                st.error(str(exc))
    st.stop()

codex_bin = st.session_state.get("codex_bin", "") or ""
model = st.session_state.get("model", "") or ""

spec = _spec()
target = spec.target_config()
st.markdown(f"## {spec.name or spec.id}")
st.caption(f"`{spec.target_type}` · `{target.id}` · `{(spec.sandbox or {}).get('backend', 'openshell')}` sandbox")
render_pipeline(_spec_path())

tabs = st.tabs(["Overview", "1 · Discover", "2 · Configure & Plan", "3 · Run", "4 · Results"])

with tabs[0]:
    st.markdown("### Evaluation configuration")
    resolved = resolved_job_config(spec, _spec_path())
    agent = spec.agent or {}
    inputs = agent.get("inputs", {}) or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("MCPs", len(inputs.get("mcps", []) or []))
    c2.metric("Skills", len(inputs.get("skills", []) or []))
    c3.metric("Personas", int((spec.generation or {}).get("personas", 2)))
    c4.metric("Pass gate", f"{float((spec.review or {}).get('gates', {}).get('min_pass_rate', 0.9)):.0%}")
    left, right = st.columns([1.25, 1])
    with left:
        with st.container(border=True):
            st.markdown("**Subject**")
            st.write(f"{spec.target_type.title()} · `{target.id}`")
            st.caption(str(spec.source_target or target.connection.get("url") or target.connection.get("path") or target.connection.get("command") or "configured inline"))
            if agent.get("instructions"):
                st.markdown("**Instructions**")
                st.write(agent["instructions"])
    with right:
        with st.container(border=True):
            st.markdown("**Execution boundary**")
            st.write(f"`{(spec.sandbox or {}).get('backend', 'openshell')}` · image `{(spec.sandbox or {}).get('image', 'base')}`")
            provider_text = ", ".join((spec.sandbox or {}).get("providers", []) or []) or "No providers attached"
            st.caption(provider_text)
            st.caption("Edit job.yaml for advanced uploads, policy, resources, and environment allowlists.")
    st.markdown("### Effective models and runner")
    model_cols = st.columns(4)
    for column, (label, value) in zip(model_cols, resolved["models"].items()):
        column.metric(label.replace("_", " ").title(), value)
    runner_view = resolved["agent"]["runner"]
    st.code(" ".join(runner_view["command"]) or "Runner not materialized yet", language="bash")
    policy = (
        f"agent {runner_view['agent'] or 'default'} · "
        f"effort {runner_view['reasoning_effort'] or 'default'}"
        if runner_view["backend"] == "copilot"
        else f"approval {runner_view['approval_mode']} · "
        f"Codex sandbox {runner_view['codex_sandbox']}"
    )
    st.caption(
        f"{runner_view['backend']} · {runner_view['kind']} · "
        f"timeout {runner_view['timeout_seconds']}s · {policy} · "
        f"parser {runner_view['parser']}"
    )
    with st.expander("Full resolved configuration"):
        st.json(resolved)

# --------------------------------------------------------------------------- #
# 1. Discover
# --------------------------------------------------------------------------- #
with tabs[1]:
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
            rc, log = run_cli(cmd_discover, live=True, timeout=30.0, skip_apps=False, sample="off",
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
with tabs[2]:
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
    agent_runner = dict((spec.agent or {}).get("runner") or {})
    has_aut = bool(agent_runner) or any(
        h.get("kind") in ("process", "codex-session", "copilot-session")
        for h in spec.hosts
    )
    resolved = resolved_job_config(spec, _spec_path())
    runner_view = resolved["agent"]["runner"]
    with st.container(border=True):
        st.subheader("OpenShell configuration")
        with st.form("sandbox-config"):
            s1, s2 = st.columns(2)
            sandbox_backend = s1.selectbox("Execution backend", ["openshell", "local"], index=0 if resolved["sandbox"]["backend"] == "openshell" else 1)
            sandbox_image = s2.text_input("Image", value=str(resolved["sandbox"]["image"]), disabled=sandbox_backend == "local")
            provider_value = st.text_input("Providers", value=", ".join(resolved["sandbox"]["providers"]), placeholder="openai, github", disabled=sandbox_backend == "local")
            env_value = st.text_input("Environment allowlist", value=", ".join(resolved["sandbox"]["env_allowlist"]), placeholder="API_KEY, PROJECT_ID")
            st.caption("Providers supply managed credentials and matching egress. Environment variables are never inherited unless allowlisted.")
            save_sandbox = st.form_submit_button("Save sandbox configuration")
        if save_sandbox:
            spec.sandbox = {
                **(spec.sandbox or {}), "backend": sandbox_backend,
                "image": sandbox_image or "base",
                "providers": [part.strip() for part in provider_value.split(",") if part.strip()],
                "env_allowlist": [part.strip() for part in env_value.split(",") if part.strip()],
            }
            if (spec.agent or {}).get("runner"):
                spec.agent["runner"]["sandbox"] = dict(spec.sandbox)
            save_spec(spec, _spec_path())
            st.success("Saved sandbox providers, image, and environment policy.")
            st.rerun()
    with st.container(border=True):
        st.subheader("Agent and model configuration")
        st.caption("These are effective runtime values, not hidden defaults. Saving updates job.yaml and the materialized AUT runner together.")
        if runner_view["backend"] in ("codex", "not configured"):
            with st.form("runtime-config"):
                m1, m2, m3, m4 = st.columns(4)
                aut_model = m1.text_input("AUT model", value="" if runner_view["model"] == "Codex CLI default" else runner_view["model"], placeholder="Codex CLI default")
                user_model = m2.text_input("User model", value="" if resolved["models"]["user_emulator"] == "Codex CLI default" else resolved["models"]["user_emulator"], placeholder="AUT / Codex default")
                generation_model = m3.text_input("Generation model", value="" if resolved["models"]["generation"] == "Codex CLI default" else resolved["models"]["generation"], placeholder="AUT / Codex default")
                judge_model = m4.text_input("Judge model", value="" if resolved["models"]["judge"] == "Codex CLI default" else resolved["models"]["judge"], placeholder="Generation / AUT default")
                r1, r2, r3, r4 = st.columns(4)
                kinds = ["process", "codex-session"]
                runner_kind = r1.selectbox("Runner lifecycle", kinds, index=kinds.index(runner_view["kind"]) if runner_view["kind"] in kinds else 0)
                runner_timeout = r2.number_input("Turn timeout", 10, 3600, int(runner_view["timeout_seconds"] or 600))
                approval_values = ["never", "on-request", "untrusted"]
                approval = r3.selectbox("Approval mode", approval_values, index=approval_values.index(runner_view["approval_mode"]) if runner_view["approval_mode"] in approval_values else 0)
                sandbox_values = ["read-only", "workspace-write", "danger-full-access"]
                nested_sandbox = r4.selectbox("Codex sandbox", sandbox_values, index=sandbox_values.index(runner_view["codex_sandbox"]) if runner_view["codex_sandbox"] in sandbox_values else 0)
                command = runner_view["command"] or ["codex"]
                codex_executable = st.text_input("Codex executable", value=str(command[0]))
                st.code(" ".join(command), language="bash")
                save_runtime = st.form_submit_button("Save runtime configuration", type="primary")
            if save_runtime:
                update_agent_runtime(
                    spec, _spec_path(), model=aut_model, kind=runner_kind,
                    timeout_seconds=int(runner_timeout), approval_mode=approval,
                    codex_sandbox=nested_sandbox, codex_bin=codex_executable,
                    user_model=user_model or aut_model,
                    generation_model=generation_model or aut_model,
                    judge_model=judge_model or generation_model or aut_model,
                )
                st.success("Saved the AUT, user, generation, and judge runtime configuration.")
                st.rerun()
        elif runner_view["backend"] == "copilot":
            with st.form("copilot-runtime-config"):
                st.caption(
                    "These JSON objects expose every Copilot runner setting. "
                    "Use extra_args for new Copilot CLI flags and env for role-specific environment."
                )
                runtime = dict((spec.agent or {}).get("runtime") or {})
                user_runtime = dict((spec.test or {}).get("user_runtime") or {})
                left, right = st.columns(2)
                aut_runtime_json = left.text_area(
                    "AUT Copilot runtime JSON",
                    value=json.dumps(runtime, indent=2),
                    height=360,
                )
                user_runtime_json = right.text_area(
                    "User Copilot runtime JSON",
                    value=json.dumps(user_runtime, indent=2),
                    height=360,
                )
                g1, g2 = st.columns(2)
                generation_model = g1.text_input(
                    "Generation model", value=str((spec.generation or {}).get("model") or "")
                )
                judge_model = g2.text_input(
                    "Judge model", value=str((spec.test or {}).get("judge_model") or "")
                )
                st.code(" ".join(runner_view["command"]), language="bash")
                save_copilot_runtime = st.form_submit_button(
                    "Save Copilot runtimes", type="primary"
                )
            if save_copilot_runtime:
                try:
                    parsed_aut = json.loads(aut_runtime_json)
                    parsed_user = json.loads(user_runtime_json)
                    if not isinstance(parsed_aut, dict) or not isinstance(parsed_user, dict):
                        raise ValueError("Both runtime values must be JSON objects")
                    parsed_aut["backend"] = "copilot"
                    parsed_user["backend"] = "copilot"
                    spec.agent = {**(spec.agent or {}), "runtime": parsed_aut}
                    spec.generation = {
                        **(spec.generation or {}), "model": generation_model
                    }
                    spec.test = {
                        **(spec.test or {}),
                        "user_runtime": parsed_user,
                        "user_model": str(parsed_user.get("model") or ""),
                        "judge_model": judge_model,
                    }
                    refresh_job_runners(spec, _spec_path())
                except (json.JSONDecodeError, OSError, ValueError, ConfigError) as exc:
                    st.error(str(exc))
                else:
                    st.success("Saved and rematerialized both Copilot runners.")
                    st.rerun()
        else:
            st.info("This job uses a custom runner. Edit its command in agent.runner/job.yaml; Ghostlab displays the exact resolved command on the Overview tab.")
    with st.container(border=True):
        st.subheader("Agent-under-test host")
        if has_aut:
            if agent_runner:
                st.success(f"`agent.runner` ({agent_runner.get('kind', 'process')}) — configured inline")
            for host in spec.hosts:
                if host.get("kind") in ("process", "codex-session", "copilot-session"):
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

    if not (spec.capabilities or {}).get("generated_from"):
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
                        cmd_plan, live=True, out=None, approve=None, reject=None, generate=True,
                        regenerate=regenerate, personas=int(personas), scenarios_per_persona=int(spp),
                        codex_bin=codex_bin, model=model, require_semantic=True,
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
with tabs[3]:
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
                    cmd_test, live=True, plan=None, suite=suite_arg, hosts=None, approved_only=approved_only,
                    user_runner=None, apps_mode=apps_mode, skip_setup=False, timeout=30.0,
                    repeat=int(repeat), profile=None, strict=False, judge=judge,
                    codex_bin=codex_bin, model=model, require_semantic=True,
                )
            (st.success if rc == 0 else st.error)("Run finished" if rc == 0 else "Run finished with failures")
            st.code(log or "(no output)")
            st.info("See tab 4 for a structured breakdown and traces.")

# --------------------------------------------------------------------------- #
# 4. Results
# --------------------------------------------------------------------------- #
with tabs[4]:
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
        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("Executed", results["executed"])
        m2.metric("Pass", totals["pass"])
        m3.metric("Fail", totals["fail"])
        m4.metric("Error", totals["error"])
        m5.metric("Skip", totals.get("skip", 0))
        m6.metric("Pass rate", "n/a" if results["pass_rate"] is None else f"{results['pass_rate']:.0%}")

        dashboard_path = build_dashboard(results_path.parent)
        st.download_button(
            "Download standalone dashboard",
            dashboard_path.read_bytes(),
            file_name=f"{results.get('id', 'ghostlab')}-dashboard.html",
            mime="text/html",
        )

        st.markdown("### Cases")
        f1, f2 = st.columns(2)
        statuses = sorted({entry["status"] for entry in results["results"]})
        suites = sorted({entry["suite"] for entry in results["results"]})
        selected_statuses = f1.multiselect("Status", statuses, default=statuses)
        selected_result_suites = f2.multiselect("Suite", suites, default=suites)
        status_color = {"pass": "green", "fail": "red", "error": "red", "skip": "gray"}
        visible_results = [
            entry for entry in results["results"]
            if entry["status"] in selected_statuses and entry["suite"] in selected_result_suites
        ]
        st.caption(f"Showing {len(visible_results)} of {len(results['results'])} case results")
        for entry in visible_results:
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
