# MCP Rehearsal / Ghostlab

> A local, end-to-end **testing lab for any MCP server** — coding agents role-play
> real users, drive your tools over multiple turns, and the harness captures
> traces, scores outcomes, and even **renders and clicks through MCP Apps UI
> widgets**.

[![CI](https://github.com/sajjadGG/Rehearsal/actions/workflows/ci.yml/badge.svg)](https://github.com/sajjadGG/Rehearsal/actions)
[![Docs](https://img.shields.io/badge/docs-wiki-blue)](https://sajjadgg.github.io/Rehearsal/)
[![Python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![llms.txt](https://img.shields.io/badge/llms.txt-✓-purple)](llms.txt)

**Test your MCP server the way it's actually used** — not with unit tests against
the protocol, but with a real coding agent (Codex / Claude) that picks tools,
makes mistakes, and tries to accomplish goals, while a second agent plays the
user. Protocol-level checks (schema errors, a tool call that 500s) are useful
sanity checks, but they aren't the real test — the real test is whether an
agent can actually get a task done through your MCP, end to end.

📖 **Docs wiki:** https://sajjadgg.github.io/Rehearsal/ · 🤖 **For agents:** [`llms.txt`](llms.txt) · 🛠 **Contributing:** [`CONTRIBUTING.md`](CONTRIBUTING.md)

## Quickstart

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e .            # add '.[ui]' for the web UI, '.[apps]' for widget rendering

ghostlab create
```

That's the whole flow. `ghostlab create` walks you through everything, end to end:

1. **Name + target** — the only two prompts. A target is an MCP URL, or a path
   to a target JSON / standard `mcpServers` config.
2. **Discover** — connects to the target, lints its contract (schema errors,
   risk labels), and probes any MCP Apps `ui://` widgets.
3. **Configure semantic testing** — if `codex` is on your `$PATH`, offers to
   wire it up as the **agent-under-test**: a real coding-agent session with
   your MCP's tools available, so the dual-agent conversations below have
   something to actually drive. (No codex? The wizard still finishes —
   protocol-level suites still run, semantic/security just skip until a host
   is configured; see [Runner Configs](#runner-configs).)
4. **Generate a test plan** — personas × scenarios for the semantic/security
   suites, plus deterministic coverage for every discovered tool
   (`test-plan.yaml`), all editable afterward.
5. **Pick which suites to run** — defaults to everything; narrow it to just
   `semantic` while you're iterating, or the full set for a release check.
6. **Run + review** — executes the plan against your configured host(s),
   writes a colored pass/fail summary plus a dashboard, and prints the
   readiness/gate verdict.

Everything the wizard does is one of `discover` / `plan` / `test` / `review`
under the hood — run any of them standalone afterward to iterate without
repeating the whole wizard:

```bash
ghostlab discover --job <name>    # re-inspect after the target changes
ghostlab plan --job <name>        # regenerate/curate the test plan
ghostlab test --job <name>        # rerun (add --suite semantic to narrow it)
ghostlab review --job <name>      # the readiness/gate report on its own
```

A job is a self-contained folder: `jobs/<name>/job.yaml` (target, hosts,
generation/test defaults, gates — all editable), `test-plan.yaml`, `workspace/`
(discover/generated/test artifacts + a local sqlite db), and `runs/`.

## What you get

| Stage | What it produces |
| --- | --- |
| **Discover** | A deterministic `contract.json` (schema lint, risk labels, MCP Apps metadata checks) and a refreshed `capabilities:` section in `job.yaml` |
| **Plan** | A coverage-driven `test-plan.yaml`: deterministic protocol cases for every tool, plus generated persona/scenario cases for the semantic/security suites |
| **Test** | Multi-host execution results (`results.json`/`results.md`), a standalone HTML dashboard, and — for conversational cases — full dual-agent transcripts with structured tool-call capture |
| **Review** | A readiness report: pass/fail gate verdict, failure clusters, and prioritized repairs |

## Goal

Build a repeatable, sandboxed tester that can:

- Run any target MCP app in an isolated environment.
- Launch one coding-agent session as the **agent-under-test** (with target MCP injected).
- Launch another coding-agent session as the **user emulator** (persona + goal driven).
- Drive multi-turn interactions between them.
- Capture full traces, tool activity, failures, and outcomes.

This lets you test with your existing Codex/Claude usage path, instead of wiring a separate LLM provider deployment just for E2E testing.

## Scope

Rehearsal is intentionally **app-agnostic**:

- Works with any MCP server reachable by stdio/SSE/streamable HTTP.
- Supports local or remote MCP endpoints.
- Supports multiple coding-agent runners (Codex, Claude Code, and future adapters).

No Cortex-specific assumptions are required in the core harness.

## Reference

Everything below is the individual-command reference and advanced usage —
useful once you're past the first `ghostlab create` run, or scripting CI.

### Job folder layout

```text
jobs/<name>/
  job.yaml          # target, setup, hosts, generation, test, prompts, gates
  test-plan.yaml    # produced by `ghostlab plan`
  workspace/        # discover/, generated/, test/ artifacts + ghostlab.sqlite3
  runs/             # dual-agent run output
```

### Core dual-harness architecture

1. **AUT Harness (Agent Under Test)** — starts a coding-agent session (Codex or
   Claude Code), injects the target MCP server config into it, and exposes a
   controlled I/O bridge so it can receive user messages and return
   replies/tool results.
2. **User Emulator Harness** — starts a second coding-agent session, gives it a
   scenario file (persona, goals, constraints, success criteria), and asks it
   to act like a realistic user, sending messages turn-by-turn to the AUT.
3. **Orchestrator** — coordinates turn-taking, timeouts, retries, and stop
   conditions; logs every message/event in structured format; produces a run
   report with bug candidates and reproduction context.

### Target configuration model

Each test run points to a target definition:

- `target.id`: unique name (`filesystem-mcp-local`, `my-app-staging`)
- `transport`: `stdio` | `sse` | `streamable-http`
- `connection`: command+args+env (stdio) or URL+headers (network transports)
- `capabilities`: optional expected tools/resources/prompts
- `startup`: optional health checks and boot timeout

### Commands

The package installs two equivalent console scripts: `ghostlab` and `rehearsal`.

- `ghostlab create` — the end-to-end wizard described above.
- `ghostlab init` — create a `job.yaml`/`ghostlab.yaml` from an existing target JSON, without the wizard prompts.
- `ghostlab discover` — inspect the job's target, lint its contract, refresh capabilities.
- `ghostlab plan` — generate (or curate) the coverage-driven test plan.
- `ghostlab test` — execute the test plan across the job's host adapters.
- `ghostlab review` — readiness report over discover + plan + test artifacts (release gate).
- `ghostlab inspect` — connect to a target MCP and capture what it exposes (no job needed).
- `ghostlab profile` — turn an `inspect.json` into a capability profile (codex).
- `ghostlab generate-scenarios` / `generate-personas` / `generate-dataset` — build reusable persona×scenario datasets outside the job model.
- `ghostlab review-dataset` / `run-dataset` — curate and run a standalone dataset.
- `ghostlab run` — run one dual-agent E2E scenario directly.
- `ghostlab evaluate` — score a run into a pass/fail verdict (codex judge).
- `ghostlab compare` — diff two dataset runs for regressions.
- `ghostlab apps-probe` / `apps-render` — probe/render MCP Apps `ui://` widgets.
- `ghostlab doctor` — check codex and validate runner presets.
- `ghostlab dashboard` — build a standalone HTML dashboard for a `ghostlab test` run.
- `ghostlab ui` — launch the Streamlit pipeline UI.
- `ghostlab db` — manage the SQLite persistence database.

### The UI: `ghostlab ui`

Run the whole pipeline from a browser instead of the CLI:

```bash
pip install 'ghostlab[ui]'       # installs streamlit
ghostlab ui                      # opens http://localhost:8501
```

The app mirrors the same job-based flow as `ghostlab create`: pick or create a
job, discover its target, configure the agent-under-test host, generate and
curate the test plan, run selected suites, and review colored results and full
conversation traces — the same `job.yaml`/`test-plan.yaml`/`results.json` a CLI
run of the same job would produce.

### Understand a new MCP: `inspect`

Point it at a target and it introspects the server without any coding-agent
credits or manual `curl`:

```bash
ghostlab inspect --target targets/cortex-local.json
```

This connects over the configured transport (stdio / streamable-HTTP / SSE),
runs the `initialize` handshake, and pages through `tools/list`,
`resources/list`, `resources/templates/list`, and `prompts/list`. It writes
`runs/<id>-inspect/inspect.json` (raw) and `inspect.md` (readable), and **lints**
tool/resource descriptions for references to tools the server does not actually
expose. This capability dump is the input to capability profiling and scenario
generation.

### Profile a new MCP: `profile`

Turn the raw `inspect.json` into a structured **capability profile** — the
bridge between Understand and Generate. Deterministic structure (tool taxonomy
by name family, read/write state surfaces, gaps) is computed locally; a domain
summary and inferred multi-step workflows are generated by codex:

```bash
ghostlab profile \
  --inspect runs/<id>-inspect/inspect.json
```

It writes `capabilities.json` + `capabilities.md` next to the `inspect.json`.
Generated workflow steps are filtered to real tool names, so the profile never
references hallucinated or non-exposed tools.

### Generate scenarios: `generate-scenarios`

Generate grounded use-case scenarios the MCP supports, derived from the
capability profile:

```bash
ghostlab generate-scenarios \
  --profile runs/<id>-inspect/capabilities.json \
  --n 3 \
  --output-dir scenarios
```

Scenarios are spread across intents (`happy_path` / `edge_case` / `adversarial`)
and each declares an `exercises` list of the tools it should drive the assistant
to use. Tool references are filtered to real tool names.

### Build a persona library: `generate-personas`

Personas are reusable **user profiles** decoupled from scenarios, so the same
persona can be paired with many scenarios (the basis for the dataset matrix).
Generate a domain-relevant library from a capability profile:

```bash
ghostlab generate-personas \
  --profile runs/<id>-inspect/capabilities.json \
  --n 4 \
  --output-dir personas
```

Each persona has a `summary`, behavioral `traits` (terse, impatient, easily
confused, non-native, ...), and a domain `context` map (native_language,
target_exam, level, ...). Pass one to a run with `--persona`:

```bash
ghostlab run ... --persona personas/ielts-power-user.json
```

### Build a dataset: `generate-dataset`

A dataset is a **persona x scenario matrix** — different users, and different
scenarios tailored to each of them. For every persona, codex generates
persona-specific scenarios, and the pairs become runnable cases:

```bash
ghostlab generate-dataset \
  --profile runs/<id>-inspect/capabilities.json \
  --personas 3 --scenarios-per-persona 3 --seed 7 \
  --name cortex
```

This writes a self-contained dataset directory:

```text
datasets/cortex/
  dataset.json          manifest: mcp, seed, cases[]
  personas/<id>.json
  scenarios/<id>.json    persona-namespaced; inline `persona` is a situational note
```

### Review & curate a dataset: `review-dataset`

Before spending agent credits, check that the dataset makes sense:

```bash
ghostlab review-dataset \
  --dataset datasets/cortex \
  --profile runs/<id>-inspect/capabilities.json
```

This writes `review.md` + `review.json` with a tool-coverage matrix, per-case
previews, and flags (near-duplicate cases, scenarios exercising non-exposed
tools, personas with no scenarios). Curation is file-first — each case gets a
`status` in `dataset.json` (`pending` / `approved` / `rejected` / `needs-edit`):

```bash
ghostlab review-dataset --dataset datasets/cortex \
  --approve case-a case-b --reject case-c
```

### Run a dataset: `run-dataset`

```bash
ghostlab run-dataset \
  --dataset datasets/cortex \
  --target targets/cortex-local.json \
  --aut-runner runners/codex-cortex-aut.json \
  --user-runner runners/codex-user-emulator.json \
  --limit 2
```

Each case runs through the orchestrator (with its persona) into its own run
directory, and a dataset-level `summary.md` + `results.json` capture per-case
status and turn counts.

### Tool-call capture & output hygiene

Every run captures structured MCP tool calls from the agent host. The codex AUT
runners set `"parser": "codex-json"` and run `codex exec --json`, so the
orchestrator parses the JSONL stream and records each `mcp_tool_call` with its
arguments, result, error, and status into `events.jsonl`, with a per-turn table
in `report.md`. stdout and stderr are kept separate: only stdout (with known
host noise redacted) becomes the conversational message handed to the other
agent, while raw stderr is logged for debugging.

### Evaluate a run: `evaluate`

```bash
ghostlab evaluate --run runs/<id> --capabilities runs/<id>-inspect/capabilities.json
```

Combines deterministic checks over captured tool calls with a codex LLM-judge
that scores each `success_criterion`/`failure_signal` from the transcript.
Writes `verdict.json` + `verdict.md`; exits non-zero unless the verdict is
`pass`.

### Compare two runs: `compare`

```bash
ghostlab compare --base runs/<base>-summary --candidate runs/<cand>-summary \
  --output comparison.md
```

Diffs case-by-case on verdict, listing regressions first, then fixes, then
other changes. Exits non-zero when there are regressions, so it can gate CI.

### MCP Apps: `apps-probe` / `apps-render`

Some MCPs ship **MCP Apps UI** resources — a tool's `_meta.ui.resourceUri`
points to a `ui://…` HTML widget a compatible host is expected to render.

`apps-probe` connects to a target, finds every UI-producing tool, fetches each
`ui://` resource, and reports render-readiness and CSP diagnostics:

```bash
ghostlab apps-probe --target targets/cortex-local.json
```

`apps-render` actually renders a `ui://` widget in headless Chrome, proving a
user can see and use it — it implements the MCP Apps host bridge, mounts the
widget in a sandboxed iframe, completes the `ui/initialize` handshake, feeds it
real tool input/result, and can drive a sequence of UI intents:

```bash
pip install 'ghostlab[apps]' && playwright install chrome    # one-time
ghostlab apps-render --target targets/cortex-local.json \
  --tool views_generate_sentence_scramble \
  --arguments '{"target_sentence":"The cat sat on the mat","shuffled_elements":["mat","The","on","sat","cat","the"]}' \
  --intent '{"type":"reorder","value":["The","cat","sat","on","the","mat"]}' \
  --intent '{"type":"reveal"}'
```

It writes `apps-render.json` + `apps-render.md`, a `widget.png` of the initial
render, and `widget-final.png` after the intents run. Exit status is non-zero
if the render errored or any assertion failed.

### Session runner (one live agent across turns)

By default each turn spawns a fresh agent process and the orchestrator replays
the transcript. The session runner (`"kind": "codex-session"`) instead keeps
one codex session alive: turn 1 records the `thread_id`, and later turns run
`codex exec resume <thread_id>` so codex retains context — fewer tokens, no
repeated cold-start noise.

```bash
ghostlab run --target targets/cortex-local.json --scenario <scenario.json> \
  --aut-runner runners/codex-cortex-local-session.json --user-runner <user.json>
```

### Validate your setup: `doctor`

```bash
ghostlab doctor               # validates runners/*.json
ghostlab doctor --runners runners/codex-cortex-local-session.json
```

Reports the codex binary + version and validates each runner's kind, command,
and parser.

### Default agent backend

`codex` is the default coding-agent backend for generation, the
agent-under-test, and judging. `inspect` needs no agent — it is a direct MCP
client. The codex binary is auto-detected from `$PATH`, then the macOS app
bundle (`/Applications/Codex.app/Contents/Resources/codex`); override with
`$REHEARSAL_CODEX_BIN` or `--codex-bin`.

### Colored output

CLI output is colored automatically on a TTY (dual-agent transcripts,
pass/fail/skip verdicts, gate failures). Set `NO_COLOR=1` (or
`GHOSTLAB_COLOR=0`) to disable it, `GHOSTLAB_COLOR=1` to force it on (e.g.
piping into a pager that groks ANSI).

## Runner Configs

Mock runner (no agent, free):

```json
{ "kind": "mock" }
```

Process runner:

```json
{
  "kind": "process",
  "command": ["codex", "exec", "-"],
  "env": {},
  "timeout_seconds": 300,
  "prompt_mode": "stdin"
}
```

The process runner starts one fresh process per turn. `prompt_mode` can be
`stdin`, `append-arg`, or `replace-placeholder`. `ghostlab create` synthesizes
one of these automatically for the agent-under-test host (wiring the target
MCP in via codex's `-c mcp_servers.<id>...` overrides, including
`bearer_token_env_var` for `Bearer ${VAR}`-style auth headers) — see
`jobs/<name>/runners/aut.json` after running it. To use Claude Code or another
agent as the AUT instead, hand-write a runner JSON (see
`runners/claude-process.example.json`) and pass it via `--aut-runner` to
`ghostlab create`/`ghostlab plan`, or add it directly under `hosts:` in
`job.yaml`.

## Install from PyPI

```bash
pip install ghostlab            # add [ui] and/or [apps] for those extras
ghostlab --help
```

## Packaging & Release

Build and validate distributions locally:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

CI runs tests on Python 3.10 through 3.13 and verifies that the package builds.
Releases are automated: the **`publish.yml`** workflow builds the sdist + wheel,
publishes them to PyPI via **Trusted Publishing**, and attaches them to the
GitHub Release — triggered when you **publish a GitHub Release** (or run the
workflow manually). Cut a release like:

```bash
# bump rehearsal/__init__.py __version__ first, then:
gh release create v0.1.0 --generate-notes
```

To enable publishing, create the PyPI project **`ghostlab`** and add a Trusted
Publisher for this repository, workflow `.github/workflows/publish.yml`,
environment `pypi`. No PyPI username or token is committed.

The Pages workflow builds the docs wiki with MkDocs and deploys it to GitHub
Pages on pushes to `main`, `v*.*.*` release tags, and manual workflow runs. In
the GitHub repository settings, set Pages to use GitHub Actions as the source.
