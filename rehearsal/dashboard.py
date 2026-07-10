"""Self-contained HTML dashboard for a `ghostlab test` run.

A semantic run buries the interesting parts — the emulated-user turns, every
tool call's arguments and result, the widgets the agent opened, and the judge's
verdict — inside `events.jsonl` and a pile of markdown. This renders one
standalone `dashboard.html` (no external assets, works offline, light/dark) with
collapsible turns and click-to-expand tool calls, so a run is reviewable at a
glance instead of scrolling raw logs.

Usage: `build_dashboard(results_dir)` writes `<results_dir>/dashboard.html`.
`ghostlab dashboard <run-dir>` regenerates it for an existing run.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Data extraction
# --------------------------------------------------------------------------- #
def _read_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _turns_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold the event stream into an ordered list of conversation turns."""
    turns: dict[int, dict[str, Any]] = {}

    def turn(index: int) -> dict[str, Any]:
        return turns.setdefault(
            index,
            {
                "turn": index, "user": None, "assistant": None,
                "tool_calls": [], "widgets": [], "interactions": [],
            },
        )

    for event in events:
        etype = event.get("type")
        data = event.get("data", {}) or {}
        idx = data.get("turn")
        if not isinstance(idx, int):
            continue
        if etype == "user_message":
            turn(idx)["user"] = data.get("content", "")
        elif etype == "aut_result":
            entry = turn(idx)
            entry["assistant"] = data.get("output", "")
            entry["tool_calls"] = data.get("tool_calls") or []
            entry["aut_failed"] = data.get("exit_code") not in (0, None) or data.get("timed_out")
        elif etype == "widgets_shown":
            turn(idx)["widgets"] = data.get("widgets") or []
        elif etype == "widget_interaction":
            outcome = data.get("outcome")
            if outcome:
                turn(idx)["interactions"].append(outcome)
    # Runs recorded before the live `widgets_shown` event still have the raw
    # tool calls, so derive widgets from those when none were captured — the
    # dashboard shows the same interactive surfaces either way.
    from .mcp_apps import widgets_from_tool_calls

    for entry in turns.values():
        if not entry["widgets"] and entry["tool_calls"]:
            entry["widgets"] = widgets_from_tool_calls(entry["tool_calls"])
    return [turns[k] for k in sorted(turns)]


def _run_meta(run_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    meta: dict[str, Any] = {"status": "", "goal": "", "persona": "", "models": {}}
    for event in events:
        if event.get("type") == "run_started":
            data = event.get("data", {}) or {}
            meta["goal"] = (data.get("scenario") or {}).get("goal", "")
            meta["title"] = (data.get("scenario") or {}).get("title", "")
            meta["models"] = data.get("models", {})
            persona = data.get("persona") or {}
            meta["persona"] = persona.get("name", "") if isinstance(persona, dict) else ""
        elif event.get("type") == "run_finished":
            meta["status"] = (event.get("data", {}) or {}).get("status", "")
    verdict_path = run_dir / "verdict.json"
    if verdict_path.exists():
        try:
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            meta["verdict"] = verdict.get("verdict", "")
            meta["verdict_summary"] = (verdict.get("judge") or {}).get("summary", "")
            meta["gates"] = verdict.get("gates") or []
        except (OSError, json.JSONDecodeError):
            pass
    return meta


def build_dashboard_data(results_dir: Path) -> dict[str, Any]:
    results_path = results_dir / "results.json"
    if not results_path.exists():
        raise FileNotFoundError(f"no results.json in {results_dir}")
    results = json.loads(results_path.read_text(encoding="utf-8"))

    cases: list[dict[str, Any]] = []
    for entry in results.get("results", []):
        run_dir_str = (entry.get("artifacts") or {}).get("run_dir")
        events = _read_events(Path(run_dir_str)) if run_dir_str else []
        meta = _run_meta(Path(run_dir_str), events) if run_dir_str else {}
        turns = _turns_from_events(events)
        cases.append(
            {
                "case": entry.get("case", "?"),
                "suite": entry.get("suite", ""),
                "host": entry.get("host", ""),
                "kind": entry.get("kind", ""),
                "status": entry.get("status", ""),
                "detail": entry.get("detail", ""),
                "duration_ms": entry.get("duration_ms"),
                "meta": meta,
                "turns": turns,
            }
        )
    tool_calls = [
        call for case in cases for turn in case["turns"] for call in turn.get("tool_calls", [])
    ]
    durations = [case["duration_ms"] for case in cases if isinstance(case.get("duration_ms"), (int, float))]
    suites: dict[str, int] = {}
    for case in cases:
        suite = case.get("suite") or "unknown"
        suites[suite] = suites.get(suite, 0) + 1
    return {
        "id": results.get("id", "run"),
        "generated_at": results.get("generated_at", ""),
        "hosts": results.get("hosts", []),
        "totals": results.get("totals", {}),
        "pass_rate": results.get("pass_rate"),
        "cases": cases,
        "summary": {
            "cases": len(cases),
            "conversations": sum(1 for case in cases if case.get("kind") == "conversational"),
            "tool_calls": len(tool_calls),
            "tool_failures": sum(1 for call in tool_calls if call.get("status") not in ("completed", "pass", "success")),
            "avg_duration_ms": (sum(durations) / len(durations)) if durations else None,
            "suites": suites,
        },
    }


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
def _esc(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    return html.escape(value)


def _tool_call_html(call: dict[str, Any]) -> str:
    name = f"{call.get('server', '?')}/{call.get('tool', '?')}"
    status = call.get("status", "?")
    args = call.get("arguments")
    result = call.get("result")
    error = call.get("error")
    body_parts: list[str] = []
    if args is not None:
        body_parts.append(f"<div class='kv'><span>arguments</span><pre>{_esc(args)}</pre></div>")
    if error:
        body_parts.append(f"<div class='kv err'><span>error</span><pre>{_esc(error)}</pre></div>")
    if result is not None:
        body_parts.append(f"<div class='kv'><span>result</span><pre>{_esc(result)}</pre></div>")
    body = "".join(body_parts) or "<div class='kv'><em>no payload captured</em></div>"
    return (
        f"<details class='tool {status}'>"
        f"<summary><span class='dot'></span><code>{_esc(name)}</code>"
        f"<span class='tstatus'>{_esc(status)}</span></summary>"
        f"<div class='toolbody'>{body}</div></details>"
    )


def _widget_html(widget: dict[str, Any]) -> str:
    fields = widget.get("fields") or {}
    rows = "".join(
        f"<div class='kv'><span>{_esc(k)}</span><pre>{_esc(v)}</pre></div>" for k, v in fields.items()
    )
    text = widget.get("text")
    if text:
        rows = f"<pre class='wtext'>{_esc(text)}</pre>" + rows
    uri = widget.get("resource_uri")
    uri_note = f"<span class='uri'>{_esc(uri)}</span>" if uri else ""
    return (
        f"<details class='widget' open>"
        f"<summary>▣ interactive widget — <code>{_esc(widget.get('tool', '?'))}</code>"
        f" (the emulated user can fill this in) {uri_note}</summary>"
        f"<div class='toolbody'>{rows or '<em>no fields captured</em>'}</div></details>"
    )


def _interaction_html(outcome: dict[str, Any]) -> str:
    """A widget the user actually operated (apps mode): DOM actions, backend
    calls the widget fired, and the follow-up it sent into the conversation."""
    tool = outcome.get("tool", "widget")
    calls = outcome.get("server_tool_calls") or []
    follow = outcome.get("follow_up_messages") or []
    rows: list[str] = []
    intents = outcome.get("intents") or []
    if intents:
        acts = ", ".join(i.get("type", "?") for i in intents)
        rows.append(f"<div class='kv'><span>user did</span><pre>{_esc(acts)}</pre></div>")
    for call in calls:
        status = "err" if call.get("error") else ""
        name = call.get("tool") or call.get("method", "?")
        payload = call.get("error") or call.get("result")
        rows.append(
            f"<div class='kv {status}'><span>→ backend {_esc(name)}</span>"
            f"<pre>{_esc(payload)}</pre></div>"
        )
    for msg in follow:
        from .apps_host.protocol import widget_message_text

        rows.append(
            f"<div class='kv'><span>↩ follow-up into chat</span>"
            f"<pre>{_esc(widget_message_text(msg))}</pre></div>"
        )
    if outcome.get("error"):
        rows.append(f"<div class='kv err'><span>error</span><pre>{_esc(outcome['error'])}</pre></div>")
    summary = f"{len(calls)} backend call(s)" + (", submitted" if follow else "")
    return (
        f"<details class='widget interaction' open>"
        f"<summary>⚡ user operated widget — <code>{_esc(tool)}</code> "
        f"<span class='uri'>{_esc(summary)}</span></summary>"
        f"<div class='toolbody'>{''.join(rows) or '<em>rendered; no effects</em>'}</div></details>"
    )


def _turn_html(turn: dict[str, Any]) -> str:
    parts: list[str] = [f"<div class='turnrow'><span class='tn'>#{turn['turn']}</span></div>"]
    if turn.get("user") is not None:
        parts.append(f"<div class='msg user'><span class='who'>user</span><div class='bubble'>{_esc(turn['user'])}</div></div>")
    if turn.get("assistant") is not None:
        parts.append(f"<div class='msg assistant'><span class='who'>assistant</span><div class='bubble'>{_esc(turn['assistant'])}</div></div>")
    calls = turn.get("tool_calls") or []
    if calls:
        parts.append("<div class='tools'>" + "".join(_tool_call_html(c) for c in calls) + "</div>")
    for widget in turn.get("widgets") or []:
        parts.append(_widget_html(widget))
    for outcome in turn.get("interactions") or []:
        parts.append(_interaction_html(outcome))
    return f"<div class='turn'>{''.join(parts)}</div>"


def _case_html(case: dict[str, Any]) -> str:
    meta = case.get("meta", {})
    status = case.get("status", "")
    verdict = meta.get("verdict", "")
    gates = meta.get("gates") or []
    gates_html = (
        f"<div class='gates'>gates: {', '.join(_esc(g) for g in gates)}</div>" if gates else ""
    )
    duration = case.get("duration_ms")
    dur = f"{duration/1000:.0f}s" if isinstance(duration, (int, float)) else ""
    turns_html = "".join(_turn_html(t) for t in case.get("turns", []))
    summary = meta.get("verdict_summary") or case.get("detail", "")
    models = meta.get("models", {})
    model_line = ""
    if models:
        model_line = (
            f"<div class='models'>agent: <code>{_esc(models.get('agent_under_test', '?'))}</code>"
            f" · user: <code>{_esc(models.get('user_emulator', '?'))}</code></div>"
        )
    search_text = " ".join(
        str(value) for value in (
            case.get("case", ""), case.get("suite", ""), case.get("host", ""),
            meta.get("goal", ""), meta.get("persona", ""), summary,
        )
    ).lower()
    return (
        f"<details class='case' data-status='{_esc(status)}' data-suite='{_esc(case.get('suite',''))}' data-search='{_esc(search_text)}'>"
        f"<summary>"
        f"<span class='badge {_esc(status)}'>{_esc(status)}</span>"
        f"<span class='casename'>{_esc(case.get('case', '?'))}</span>"
        f"<span class='pill'>{_esc(case.get('suite',''))}</span>"
        f"<span class='pill'>{_esc(case.get('host',''))}</span>"
        f"<span class='dur'>{dur}</span>"
        f"</summary>"
        f"<div class='casebody'>"
        f"<div class='goal'><strong>Goal:</strong> {_esc(meta.get('goal','') or '—')}"
        f"{f' · persona: <em>' + _esc(meta.get('persona','')) + '</em>' if meta.get('persona') else ''}</div>"
        f"{model_line}"
        f"<div class='verdict {_esc(verdict)}'><strong>Judge:</strong> {_esc(verdict) or 'n/a'} — {_esc(summary)}</div>"
        f"{gates_html}"
        f"<div class='transcript'>{turns_html or '<em>no transcript captured</em>'}</div>"
        f"</div></details>"
    )


def render_dashboard_html(data: dict[str, Any]) -> str:
    totals = data.get("totals", {})
    summary = data.get("summary", {})
    rate = data.get("pass_rate")
    rate_txt = "n/a" if rate is None else f"{rate:.0%}"
    rate_deg = 0 if rate is None else max(0, min(360, int(rate * 360)))
    health = "No executed cases" if rate is None else "Ready" if not totals.get("fail") and not totals.get("error") else "Needs attention"
    stat = lambda label, value, cls="": (
        f"<div class='stat {cls}'><div class='num'>{value}</div><div class='lbl'>{label}</div></div>"
    )
    stats = "".join(
        [
            stat("pass rate", rate_txt),
            stat("pass", totals.get("pass", 0), "pass"),
            stat("fail", totals.get("fail", 0), "fail"),
            stat("error", totals.get("error", 0), "error"),
            stat("skip", totals.get("skip", 0), "skip"),
            stat("tool calls", summary.get("tool_calls", 0)),
            stat("conversations", summary.get("conversations", 0)),
        ]
    )
    # Semantic/conversational cases first — they're the point of the dashboard.
    cases = sorted(
        data.get("cases", []),
        key=lambda c: (c.get("suite") != "semantic", {"fail": 0, "error": 1, "pass": 2, "skip": 3}.get(c.get("status"), 4)),
    )
    cases_html = "".join(_case_html(c) for c in cases)
    host_names = ", ".join(
        h.get("id", "?") if isinstance(h, dict) else str(h) for h in data.get("hosts", [])
    )
    suite_buttons = "".join(
        f"<button data-f='{_esc(suite)}'>{_esc(suite)} <span>{count}</span></button>"
        for suite, count in sorted((summary.get("suites") or {}).items())
    )
    return _TEMPLATE.format(
        title=_esc(data.get("id", "run")),
        generated=_esc(data.get("generated_at", "")),
        hosts=_esc(host_names),
        health=_esc(health),
        rate=rate_txt,
        rate_deg=rate_deg,
        stats=stats,
        suite_buttons=suite_buttons,
        cases=cases_html,
    )


def build_dashboard(results_dir: Path) -> Path:
    data = build_dashboard_data(results_dir)
    out = results_dir / "dashboard.html"
    out.write_text(render_dashboard_html(data), encoding="utf-8")
    return out


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GhostLab · {title}</title>
<style>
:root {{
  --bg:#f4f5f8; --panel:#fff; --ink:#161922; --muted:#6b7280; --border:#e1e4ea;
  --accent:#6c4df2; --accent-soft:#eeeaff;
  --user:#0a6cbf; --user-bg:#e7f1fb; --asst:#1f7a4d; --asst-bg:#e8f6ee;
  --tool:#8a5a00; --tool-bg:#fbf3e2; --widget:#8a2fb0; --widget-bg:#f6e9fb;
  --pass:#1f7a4d; --fail:#c0392b; --error:#a3211a; --skip:#6b7280; --partial:#8a5a00;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#0d1015; --panel:#171b21; --ink:#e6e8eb; --muted:#9aa3ad; --border:#29303a;
    --accent:#9d8aff; --accent-soft:#251f42;
    --user:#5db1ff; --user-bg:#12283d; --asst:#5fd398; --asst-bg:#12291e;
    --tool:#e0b054; --tool-bg:#2c2413; --widget:#d98fee; --widget-bg:#28162f;
    --pass:#5fd398; --fail:#ff7062; --error:#ff8a7a; --skip:#9aa3ad; --partial:#e0b054;
  }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:radial-gradient(circle at 80% -10%,var(--accent-soft),transparent 32rem),var(--bg); color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
.wrap {{ max-width:1120px; margin:0 auto; padding:34px 24px 90px; }}
.hero {{ display:grid; grid-template-columns:1fr auto; gap:24px; align-items:center; margin-bottom:24px; }}
.eyebrow {{ color:var(--accent); font-size:11px; font-weight:800; text-transform:uppercase; letter-spacing:.13em; }}
header h1 {{ margin:5px 0 6px; font-size:clamp(26px,4vw,42px); letter-spacing:-0.045em; line-height:1.05; }}
header .meta {{ color:var(--muted); font-size:13px; }}
.health {{ display:flex; align-items:center; gap:12px; background:var(--panel); border:1px solid var(--border); border-radius:18px; padding:12px 16px; box-shadow:0 12px 35px rgba(25,20,55,.07); }}
.ring {{ width:58px; height:58px; border-radius:50%; display:grid; place-items:center; background:conic-gradient(var(--accent) 0deg,var(--accent) {{rate_deg}}deg,var(--border) {{rate_deg}}deg); position:relative; }}
.ring:before {{ content:""; position:absolute; width:44px; height:44px; border-radius:50%; background:var(--panel); }}
.ring strong {{ position:relative; font-size:12px; }}
.health-label strong,.health-label span {{ display:block; }} .health-label span {{ color:var(--muted); font-size:12px; }}
.stats {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom:22px; }}
.stat {{ background:var(--panel); border:1px solid var(--border); border-radius:12px;
  padding:12px 18px; min-width:110px; flex:1; box-shadow:0 8px 25px rgba(25,20,55,.035); }}
.stat .num {{ font-size:24px; font-weight:700; }}
.stat .lbl {{ font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em; }}
.stat.pass .num {{ color:var(--pass); }} .stat.fail .num {{ color:var(--fail); }}
.stat.error .num {{ color:var(--error); }} .stat.skip .num {{ color:var(--skip); }}
.toolbar {{ display:flex; gap:12px; align-items:center; justify-content:space-between; margin:26px 0 14px; flex-wrap:wrap; }}
.filters {{ display:flex; gap:8px; flex-wrap:wrap; }}
.filters button {{ background:var(--panel); color:var(--ink); border:1px solid var(--border);
  border-radius:999px; padding:5px 14px; font-size:13px; cursor:pointer; }}
.filters button span {{ color:var(--muted); margin-left:4px; }}
.filters button.active {{ background:var(--ink); color:var(--bg); border-color:var(--ink); }}
.search {{ min-width:240px; flex:1; max-width:360px; background:var(--panel); color:var(--ink); border:1px solid var(--border); border-radius:10px; padding:9px 12px; font:inherit; }}
.case-count {{ color:var(--muted); font-size:12px; margin:0 0 10px; }}
.case {{ background:var(--panel); border:1px solid var(--border); border-radius:12px;
  margin-bottom:12px; overflow:hidden; box-shadow:0 8px 24px rgba(25,20,55,.035); }}
.case > summary {{ list-style:none; cursor:pointer; padding:14px 16px; display:flex;
  align-items:center; gap:10px; }}
.case > summary::-webkit-details-marker {{ display:none; }}
.case > summary:hover {{ background:rgba(127,127,127,.05); }}
.badge {{ font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
  padding:3px 9px; border-radius:999px; color:#fff; }}
.badge.pass {{ background:var(--pass); }} .badge.fail {{ background:var(--fail); }}
.badge.error {{ background:var(--error); }} .badge.skip {{ background:var(--skip); }}
.badge.partial {{ background:var(--partial); }}
.casename {{ font-weight:600; flex:1; word-break:break-word; }}
.pill {{ font-size:11px; color:var(--muted); border:1px solid var(--border); border-radius:6px;
  padding:2px 7px; }}
.dur {{ font-size:12px; color:var(--muted); }}
.casebody {{ padding:4px 16px 18px; border-top:1px solid var(--border); }}
.goal, .models {{ font-size:13px; color:var(--muted); margin:12px 0 6px; }}
.verdict {{ font-size:13px; margin:8px 0; padding:8px 12px; border-radius:8px;
  background:rgba(127,127,127,.06); }}
.verdict.pass {{ border-left:3px solid var(--pass); }}
.verdict.fail {{ border-left:3px solid var(--fail); }}
.verdict.partial {{ border-left:3px solid var(--partial); }}
.gates {{ font-size:12px; color:var(--fail); margin:4px 0 10px; }}
.transcript {{ margin-top:14px; display:flex; flex-direction:column; gap:6px; }}
.turnrow {{ margin-top:10px; }}
.tn {{ font-size:11px; color:var(--muted); font-weight:600; }}
.msg {{ display:flex; gap:10px; align-items:flex-start; }}
.who {{ font-size:11px; font-weight:700; text-transform:uppercase; width:74px; flex-shrink:0;
  padding-top:8px; }}
.msg.user .who {{ color:var(--user); }} .msg.assistant .who {{ color:var(--asst); }}
.bubble {{ flex:1; padding:9px 13px; border-radius:10px; white-space:pre-wrap; word-break:break-word; }}
.msg.user .bubble {{ background:var(--user-bg); }}
.msg.assistant .bubble {{ background:var(--asst-bg); }}
.tools {{ margin:6px 0 2px 84px; display:flex; flex-direction:column; gap:4px; }}
.tool, .widget {{ border:1px solid var(--border); border-radius:8px; overflow:hidden; }}
.tool {{ background:var(--tool-bg); }}
.tool > summary, .widget > summary {{ cursor:pointer; padding:7px 11px; font-size:13px;
  display:flex; align-items:center; gap:8px; list-style:none; }}
.tool > summary::-webkit-details-marker, .widget > summary::-webkit-details-marker {{ display:none; }}
.tool code, .widget code {{ font-size:12.5px; }}
.dot {{ width:8px; height:8px; border-radius:50%; background:var(--tool); flex-shrink:0; }}
.tool.failed .dot {{ background:var(--fail); }}
.tstatus {{ margin-left:auto; font-size:11px; color:var(--muted); }}
.toolbody {{ padding:4px 12px 12px; }}
.kv {{ margin:8px 0; }}
.kv > span {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); }}
.kv.err > span {{ color:var(--fail); }}
.kv pre, .wtext {{ margin:3px 0 0; background:rgba(127,127,127,.10); border-radius:6px;
  padding:8px 10px; overflow-x:auto; font-size:12.5px; white-space:pre-wrap; word-break:break-word;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
.widget {{ background:var(--widget-bg); margin:6px 0 2px 84px; }}
.widget > summary {{ color:var(--widget); font-weight:600; }}
.widget .uri {{ font-size:11px; color:var(--muted); margin-left:auto; }}
.widget.interaction {{ border-left:3px solid var(--widget); }}
em {{ color:var(--muted); }}
@media (max-width:700px) {{ .hero {{ grid-template-columns:1fr; }} .health {{ width:max-content; }} .tools,.widget {{ margin-left:0; }} .pill {{ display:none; }} .wrap {{ padding:24px 14px 70px; }} }}
</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <div><div class="eyebrow">Ghostlab evaluation report</div><h1>{title}</h1>
  <div class="meta">generated {generated} · hosts: {hosts}</div></div>
  <div class="health"><div class="ring"><strong>{rate}</strong></div><div class="health-label"><strong>{health}</strong><span>evaluation health</span></div></div>
</header>
<div class="stats">{stats}</div>
<div class="toolbar">
  <div class="filters" id="filters">
    <button data-f="all" class="active">all</button>
    <button data-f="fail">fail</button><button data-f="error">error</button>
    <button data-f="pass">pass</button>{suite_buttons}
  </div>
  <input class="search" id="search" type="search" placeholder="Search cases, goals, personas…" aria-label="Search cases">
</div>
<div class="case-count" id="case-count"></div>
<div id="cases">{cases}</div>
</div>
<script>
(function() {{
  var buttons = document.querySelectorAll('#filters button');
  var cases = document.querySelectorAll('.case');
  var search = document.getElementById('search');
  var count = document.getElementById('case-count');
  var active = 'all';
  function applyFilters() {{
    var query = (search.value || '').trim().toLowerCase();
    var visible = 0;
    cases.forEach(function(c) {{
      var matchesFilter = active === 'all' || c.getAttribute('data-status') === active || c.getAttribute('data-suite') === active;
      var matchesSearch = !query || (c.getAttribute('data-search') || '').indexOf(query) !== -1;
      var show = matchesFilter && matchesSearch;
      c.style.display = show ? '' : 'none';
      if (show) visible += 1;
    }});
    count.textContent = 'Showing ' + visible + ' of ' + cases.length + ' case results';
  }}
  buttons.forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      buttons.forEach(function(b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      active = btn.getAttribute('data-f');
      applyFilters();
    }});
  }});
  search.addEventListener('input', applyFilters);
  applyFilters();
}})();
</script>
</body>
</html>
"""
