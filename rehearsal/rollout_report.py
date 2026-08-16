"""One document containing everything that happened in a run.

Run artifacts are already complete but scattered across `events.jsonl`,
`report.md`, `verdict.json`, and `critique.md`. Someone reviewing whether an
agent is safe to ship needs them together, in order, with the configuration that
produced them — and usually needs to hand that to someone who will not run a
CLI. This assembles the full rollout as self-contained HTML, and renders it to
PDF when a browser is available.

Secrets never reach the page: the configuration snapshot is redacted before
rendering, not after.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

# Substrings that mark a config value as sensitive. Matching is on the key, so a
# redacted value never has to be guessed at from its shape.
_SECRET_HINTS = ("token", "secret", "password", "authorization", "api_key", "apikey",
                 "access", "refresh", "credential")


def redact(value: Any, key: str = "") -> Any:
    """Recursively blank out values whose key marks them sensitive."""
    lowered = key.lower()
    if isinstance(value, dict):
        return {name: redact(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if key and any(hint in lowered for hint in _SECRET_HINTS) and value:
        return "«redacted»"
    return value


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def collect_rollout(run_dir: Path) -> dict[str, Any]:
    """Gather one conversational run's artifacts into a single structure."""
    run_dir = Path(run_dir)
    events = []
    events_path = run_dir / "events.jsonl"
    if events_path.is_file():
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    turns: list[dict[str, Any]] = []
    by_turn: dict[int, dict[str, Any]] = {}
    for event in events:
        data = event.get("data") or event
        turn = data.get("turn")
        if turn is None:
            continue
        slot = by_turn.setdefault(int(turn), {"turn": int(turn)})
        if event.get("type") == "user_message":
            slot["user"] = data.get("message") or data.get("output") or ""
        elif event.get("type") == "aut_result":
            slot["assistant"] = data.get("output", "")
            slot["tool_calls"] = data.get("tool_calls") or []
        elif event.get("type") == "user_emulator_result":
            slot.setdefault("next_user", data.get("output", ""))
    turns = [by_turn[key] for key in sorted(by_turn)]

    return {
        "run_id": run_dir.name,
        "turns": turns,
        "verdict": _read_json(run_dir / "verdict.json"),
        "critique": _read_json(run_dir / "critique.json"),
        "report_md": (run_dir / "report.md").read_text(encoding="utf-8")
        if (run_dir / "report.md").is_file() else "",
    }


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _code_block(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    return f"<pre>{_esc(text)}</pre>"


def _tool_rows(calls: list[dict[str, Any]]) -> str:
    if not calls:
        return "<p class='muted'>No tool calls this turn.</p>"
    rows = []
    for call in calls:
        duration = call.get("duration_ms")
        rows.append(
            "<tr>"
            f"<td><code>{_esc(call.get('server'))}/{_esc(call.get('tool'))}</code></td>"
            f"<td class='status-{_esc(call.get('status'))}'>{_esc(call.get('status'))}</td>"
            f"<td>{_esc(f'{duration:.0f} ms') if isinstance(duration, (int, float)) else ''}</td>"
            f"<td>{_code_block(call.get('arguments'))}</td>"
            "</tr>"
        )
    return (
        "<table class='tools'><thead><tr><th>Tool</th><th>Status</th><th>Latency</th>"
        "<th>Arguments</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _verdict_section(verdict: dict[str, Any] | None) -> str:
    if not verdict:
        return ""
    judge = verdict.get("judge") or {}
    deterministic = verdict.get("deterministic") or {}
    outcome = str(verdict.get("verdict", "?"))
    parts = [
        f"<h2>Verdict</h2><p class='verdict verdict-{_esc(outcome)}'>{_esc(outcome.upper())}</p>",
        f"<p>{_esc(judge.get('summary', ''))}</p>",
    ]
    if verdict.get("gates"):
        parts.append("<p class='gates'>Gate overrides: " +
                     ", ".join(_esc(gate) for gate in verdict["gates"]) + "</p>")

    criteria = judge.get("criteria") or []
    if criteria:
        rows = "".join(
            f"<tr><td>{'met' if item.get('met') else 'not met'}</td>"
            f"<td>{_esc(item.get('evidence', ''))}</td></tr>"
            for item in criteria
        )
        parts.append(
            "<h3>Success criteria</h3><table><thead><tr><th>Result</th>"
            f"<th>Evidence</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    signals = [item for item in (judge.get("failure_signals") or []) if item.get("triggered")]
    if signals:
        rows = "".join(f"<tr><td>{_esc(item.get('evidence', ''))}</td></tr>" for item in signals)
        parts.append(f"<h3>Failure signals triggered</h3><table><tbody>{rows}</tbody></table>")

    if deterministic:
        parts.append("<h3>Deterministic checks</h3>" + _code_block({
            key: deterministic.get(key) for key in
            ("coverage", "exercises_called", "exercises_missing", "efficiency", "tool_failures")
            if key in deterministic
        }))
    return "".join(parts)


def _critique_section(critique: dict[str, Any] | None) -> str:
    if not critique:
        return ""
    parts = ["<h2>Tool usability</h2>"]
    score = critique.get("overall_score") or critique.get("score")
    if score is not None:
        parts.append(f"<p>Ergonomics score: <strong>{_esc(score)}/5</strong></p>")
    if critique.get("summary"):
        parts.append(f"<p>{_esc(critique['summary'])}</p>")
    recommendations = critique.get("top_recommendations") or critique.get("recommendations") or []
    if recommendations:
        items = "".join(f"<li>{_esc(item)}</li>" for item in recommendations)
        parts.append(f"<h3>Recommendations</h3><ul>{items}</ul>")
    return "".join(parts)


STYLE = """
:root { --ink:#161c26; --soft:#54607a; --rule:#dfe4ec; --accent:#0e6f86;
        --pass:#2f7d4f; --fail:#b4402f; --ground:#ffffff; }
* { box-sizing:border-box; }
body { font-family:-apple-system,Segoe UI,Roboto,sans-serif; color:var(--ink);
       background:var(--ground); margin:0; padding:32px; line-height:1.55; font-size:12pt; }
h1 { font-size:22pt; margin:0 0 4px; }
h2 { font-size:15pt; margin:26px 0 8px; padding-bottom:4px; border-bottom:1px solid var(--rule); }
h3 { font-size:12pt; margin:16px 0 6px; }
p { margin:6px 0; }
.muted { color:var(--soft); }
.meta { color:var(--soft); font-size:10pt; margin-bottom:18px; }
pre { background:#f4f6f9; border:1px solid var(--rule); border-radius:3px; padding:8px 10px;
      overflow-x:auto; font-family:ui-monospace,Menlo,monospace; font-size:9pt;
      white-space:pre-wrap; word-break:break-word; margin:6px 0; }
code { font-family:ui-monospace,Menlo,monospace; font-size:9.5pt; }
table { border-collapse:collapse; width:100%; margin:8px 0; font-size:10pt; }
th,td { border:1px solid var(--rule); padding:6px 8px; text-align:left; vertical-align:top; }
th { background:#f4f6f9; }
.turn { border:1px solid var(--rule); border-left:3px solid var(--accent); border-radius:3px;
        padding:10px 14px; margin:10px 0; page-break-inside:avoid; }
.turn h3 { margin-top:0; }
.role { font-weight:600; color:var(--accent); font-size:10pt; text-transform:uppercase;
        letter-spacing:.06em; }
.bubble { margin:4px 0 12px; white-space:pre-wrap; }
.verdict { font-weight:700; font-size:14pt; }
.verdict-pass { color:var(--pass); } .verdict-fail { color:var(--fail); }
.status-completed { color:var(--pass); } .status-failed { color:var(--fail); }
.gates { color:var(--fail); }
@media print { body { padding:0 12px; } h2 { page-break-after:avoid; } }
"""


def render_html(
    rollout: dict[str, Any],
    *,
    title: str = "Ghostlab rollout",
    agent: "dict[str, Any] | None" = None,
    agent_profile: "dict[str, Any] | None" = None,
    personas: "list[dict[str, Any]] | None" = None,
    scenario: "dict[str, Any] | None" = None,
    sandbox: "dict[str, Any] | None" = None,
) -> str:
    """Render the whole rollout as one self-contained HTML document."""
    sections: list[str] = [
        f"<h1>{_esc(title)}</h1>",
        f"<p class='meta'>Run <code>{_esc(rollout.get('run_id', ''))}</code></p>",
    ]

    if agent:
        sections.append("<h2>Agent under test</h2>" + _code_block(redact(agent)))
    if sandbox:
        sections.append("<h2>Execution boundary</h2>" + _code_block(redact(sandbox)))
    if agent_profile:
        sections.append("<h2>Inferred purpose</h2>")
        sections.append(f"<p>{_esc(agent_profile.get('purpose', ''))}</p>")
        if agent_profile.get("audience"):
            sections.append(f"<p class='muted'>Audience: {_esc(agent_profile['audience'])}</p>")
        for workflow in agent_profile.get("workflows", []) or []:
            steps = " → ".join(_esc(step) for step in workflow.get("steps", []))
            sections.append(f"<p><strong>{_esc(workflow.get('name'))}</strong>: {steps}</p>")
        risks = agent_profile.get("risk_surface") or []
        if risks:
            rows = "".join(
                f"<tr><td>{_esc(item.get('risk'))}</td><td>{_esc(item.get('why'))}</td></tr>"
                for item in risks
            )
            sections.append(
                "<h3>Risk surface</h3><table><thead><tr><th>Risk</th><th>Why</th>"
                f"</tr></thead><tbody>{rows}</tbody></table>"
            )
    if personas:
        sections.append("<h2>Personas</h2>")
        for persona in personas:
            sections.append(
                f"<p><strong>{_esc(persona.get('name') or persona.get('id'))}</strong>: "
                f"{_esc(persona.get('summary', ''))}</p>"
            )
    if scenario:
        sections.append("<h2>Scenario</h2>")
        sections.append(f"<p><strong>{_esc(scenario.get('title', ''))}</strong></p>")
        sections.append(f"<p>{_esc(scenario.get('goal', ''))}</p>")
        for label, key in (("Success criteria", "success_criteria"),
                           ("Failure signals", "failure_signals")):
            values = scenario.get(key) or []
            if values:
                items = "".join(f"<li>{_esc(item)}</li>" for item in values)
                sections.append(f"<h3>{label}</h3><ul>{items}</ul>")

    sections.append("<h2>Conversation</h2>")
    if not rollout.get("turns"):
        sections.append("<p class='muted'>No turns were recorded.</p>")
    for turn in rollout.get("turns", []):
        block = [f"<div class='turn'><h3>Turn {_esc(turn.get('turn'))}</h3>"]
        if turn.get("user"):
            block.append(f"<div class='role'>User</div><div class='bubble'>{_esc(turn['user'])}</div>")
        if turn.get("assistant"):
            block.append(
                f"<div class='role'>Agent</div><div class='bubble'>{_esc(turn['assistant'])}</div>"
            )
        block.append(_tool_rows(turn.get("tool_calls") or []))
        block.append("</div>")
        sections.append("".join(block))

    sections.append(_verdict_section(rollout.get("verdict")))
    sections.append(_critique_section(rollout.get("critique")))

    body = "".join(sections)
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title><style>{STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def write_pdf(html_text: str, destination: Path) -> Path:
    """Render HTML to PDF with the browser the MCP Apps host already uses."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "PDF output needs the browser extra: pip install 'ghostlab[apps]'"
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome")
        try:
            page = browser.new_page()
            page.set_content(html_text, wait_until="load")
            page.pdf(
                path=str(destination), format="A4", print_background=True,
                margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"},
            )
        finally:
            browser.close()
    return destination


def write_rollout(
    run_dir: Path, out_dir: "Path | None" = None, *, pdf: bool = True, **context: Any
) -> dict[str, Path]:
    """Write `rollout.html` (and `rollout.pdf` when possible) for one run."""
    out_dir = Path(out_dir or run_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rollout = collect_rollout(Path(run_dir))
    html_text = render_html(rollout, **context)
    written: dict[str, Path] = {}
    html_path = out_dir / "rollout.html"
    html_path.write_text(html_text, encoding="utf-8")
    written["html"] = html_path
    if pdf:
        try:
            written["pdf"] = write_pdf(html_text, out_dir / "rollout.pdf")
        except Exception as exc:  # noqa: BLE001 - never fail a run over a report
            written["pdf_error"] = exc  # type: ignore[assignment]
    return written
