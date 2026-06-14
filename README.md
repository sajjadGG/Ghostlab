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
user. Ghostlab understands a target MCP, generates persona × scenario datasets,
runs the dual-agent loop, scores each run, and compares runs for regressions.

📖 **Docs wiki:** https://sajjadgg.github.io/Rehearsal/ · 🤖 **For agents:** [`llms.txt`](llms.txt) · 🛠 **Contributing:** [`CONTRIBUTING.md`](CONTRIBUTING.md)

## Quickstart

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e .            # add '.[ui]' for the web UI, '.[apps]' for widget rendering

# 1. Understand a target MCP
ghostlab inspect --target targets/cortex-local.json

# 2. Drive it with two agents (one under test, one emulating a user)
ghostlab run --target targets/cortex-local.json --scenario scenarios/cortex-onboarding-status.json

# 3. Render and click through an MCP Apps ui:// widget (needs '.[apps]')
ghostlab apps-render --target targets/cortex-local.json --tool views_generate_sentence_scramble \
  --arguments '{"target_sentence":"The cat sat on the mat","shuffled_elements":["mat","The","on","sat","cat","the"]}' \
  --intent '{"type":"reorder","value":["The","cat","sat","on","the","mat"]}'
```

## What it does

| Stage | Commands | What you get |
| --- | --- | --- |
| **Understand** | `inspect`, `profile` | Tool/resource/prompt dump + a capability profile, with lint findings |
| **Generate** | `generate-scenarios`, `generate-personas`, `generate-dataset`, `review-dataset` | Reusable persona × scenario datasets you can curate |
| **Run** | `run`, `run-dataset` | Multi-turn dual-agent transcripts with structured tool-call capture |
| **Evaluate** | `evaluate`, `compare` | Pass/fail verdicts (codex judge) and regression diffs between runs |
| **MCP Apps** | `apps-probe`, `apps-render` | Fetch/diagnose `ui://` widgets, then render + interact with them in headless Chrome |
| **Persist & explore** | `db`, `ui` | SQLite run history + a Streamlit UI over the whole pipeline |

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

## Core Idea

Rehearsal uses a **dual-harness architecture**:

1. **AUT Harness (Agent Under Test)**
- Starts a coding-agent session (Codex or Claude Code).
- Injects target MCP server config into that session.
- Exposes a controlled I/O bridge so it can receive user messages and return replies/tool results.

2. **User Emulator Harness**
- Starts a second coding-agent session.
- Gives it a scenario file (persona, goals, constraints, success criteria).
- Asks it to act like a realistic user and send messages turn-by-turn to the AUT.

3. **Orchestrator**
- Coordinates turn-taking, timeouts, retries, and stop conditions.
- Logs every message and event in structured format.
- Produces a run report with bug candidates and reproduction context.

## First Implementation Plan

### Phase 1: Local Loop

- Define scenario schema (JSON).
- Define target schema for MCP connection config (stdio/SSE/HTTP).
- Build a Python orchestrator that runs:
  - `codex`/`claude` process A as AUT
  - `codex`/`claude` process B as emulator
- Relay turns through a strict protocol.
- Write JSONL logs + markdown summary.

### Phase 2: Sandboxed Execution

- Add Docker Compose profiles for generic MCP target services.
- Keep orchestrator on host or in sidecar container.
- Stamp each run with target ID + build SHA/version + scenario ID + timestamp.

### Phase 3: Regression + CI

- Add deterministic scenario packs.
- Add pass/fail gates (timeouts, tool misuse, policy violations, hallucinated capabilities, schema errors).
- Publish comparison reports between runs.

## Target Configuration Model

Each test run points to a target definition, for example:

- `target.id`: unique name (`filesystem-mcp-local`, `my-app-staging`)
- `transport`: `stdio` | `sse` | `streamable-http`
- `connection`: command+args+env (stdio) or URL+headers (network transports)
- `capabilities`: optional expected tools/resources/prompts
- `startup`: optional health checks and boot timeout

This model makes the same harness reusable across different MCP apps.

## What We’ll Log

Per run:

- Target metadata (id, transport, endpoint/command fingerprint).
- Scenario metadata (id, persona, goal).
- Full AUT/emulator transcripts.
- MCP tool call envelopes (request/response/error).
- Timing (latency per turn, total runtime).
- Exit states (success, timeout, crash, policy breach).
- Repro bundle pointers.

## Success Criteria

Rehearsal is useful when you can:

- Start one command and run multiple scenarios against any MCP target.
- Reproduce failures with the same target+scenario seed/config.
- Compare two runs and quickly see regressions.
- Debug from logs without rerunning blindly.

## Current Folder Layout

```text
mcp-rehearsal/
  README.md
  __main__.py
  rehearsal/
  targets/
  scenarios/
  runners/
  runs/
  docker/
```

## Commands

Install locally from this checkout:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

The package installs two equivalent console scripts:

```bash
ghostlab --help
rehearsal --help
```

Rehearsal exposes subcommands (the bare `--target ... --scenario ...` form still
works and is treated as `run`):

- `ghostlab inspect` — connect to a target MCP and capture what it exposes.
- `ghostlab profile` — turn an `inspect.json` into a capability profile (codex).
- `ghostlab generate-scenarios` — generate scenarios from a profile (codex).
- `ghostlab generate-personas` — generate a reusable persona library (codex).
- `ghostlab generate-dataset` — build a persona x scenario dataset (codex).
- `ghostlab review-dataset` — review & curate a dataset (coverage, flags, approve/reject).
- `ghostlab run-dataset` — run every case in a dataset.
- `ghostlab run` — run a dual-agent E2E scenario.
- `ghostlab evaluate` — score a run into a pass/fail verdict (codex judge).
- `ghostlab compare` — diff two dataset runs for regressions.
- `ghostlab doctor` — check codex and validate runner presets.
- `ghostlab ui` — launch the Streamlit pipeline UI.

### The UI: `ghostlab ui`

Run the whole pipeline from a browser instead of the CLI:

```bash
pip install 'ghostlab[ui]'       # installs streamlit
ghostlab ui                      # opens http://localhost:8501
```

The app walks an MCP through the pipeline as tabs, each step feeding the next:

1. **Connect & Inspect** — enter the MCP URL (or stdio command) and introspect
   it (tools, resources, lint findings).
2. **Profile** — build the capability profile (codex).
3. **Generate Dataset** — set the parameters (number of personas, scenarios per
   persona, seed, name) and generate the persona × scenario matrix.
4. **Review & Curate** — coverage matrix, flags, and per-case approve/reject.
5. **Run & Evaluate** — run the cases (persistent session runner, optional mock
   user, codex judge) and watch live progress.
6. **Trace Viewer** — browse any run: the multi-turn transcript, every tool call
   with its arguments and results, and the judge's verdict with per-criterion
   evidence.

The sidebar sets the **workspace dir**, the **codex binary**, and the **codex
model** (applied to every codex-backed stage — generation, the AUT/user runners,
and the judge — and shown wherever it is used). Each stage has a **🔍 View
prompt** expander so you can see the exact prompt sent to codex (profile,
persona/scenario generation, the agent-under-test and user-emulator prompts, and
the judge). Steps gate on their prerequisites and the run step shows live
per-case progress.

Artifacts are written under the workspace directory (default
`ghostlab_workspace/`) so runs persist and can also be opened with the CLI.

## Packaging & Release

Build and validate distributions locally:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

CI runs tests on Python 3.10 through 3.13 and verifies that the package builds.
The release workflow publishes to PyPI when a `v*.*.*` tag is pushed, using
PyPI Trusted Publishing. To enable it, create a PyPI project named
`ghostlab` and add a trusted publisher for this repository, workflow
`.github/workflows/release.yml`, environment `pypi`. No PyPI username or token
needs to be committed.

The Pages workflow builds the docs wiki with MkDocs and deploys it to GitHub
Pages on pushes to `main`, `v*.*.*` release tags, and manual workflow runs. In
the GitHub repository settings, set Pages to use GitHub Actions as the source.

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
expose (e.g. Cortex descriptions mention `kb_find` / `kb_read` / `kb_read_skill`,
which are not in `tools/list`). This capability dump is the input to capability
profiling and scenario generation.

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
references hallucinated or non-exposed tools. This profile is the input scenario
generation consumes.

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
to use. Tool references are filtered to real tool names, so scenarios never
depend on hallucinated or non-exposed tools. Each scenario is written as a
`ScenarioConfig`-shaped JSON file ready for `run`.

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

The user-emulator prompt is composed from the persona's summary + traits +
context. Scenarios with an inline `persona` string still work unchanged; when a
persona is supplied, the scenario's inline note refines it.

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

The persona is the authoritative identity at run time; each scenario's inline
`persona` carries only a short situational note ("has 45 minutes before work"),
so the two never conflict. The `--seed` governs case ordering for reproducible
manifests.

### Review & curate a dataset: `review-dataset`

Before spending agent credits, check that the dataset makes sense:

```bash
ghostlab review-dataset \
  --dataset datasets/cortex \
  --profile runs/<id>-inspect/capabilities.json
```

This writes `review.md` + `review.json` with a **tool-coverage matrix** (which
tool categories are exercised, which tools are never touched), **per-case
previews** (persona traits, situation, goal, opening message, success/failure
criteria, exercises), and **flags**: near-duplicate cases, scenarios exercising
non-exposed tools, and personas with no scenarios.

Curation is **file-first** — each case gets a `status` in `dataset.json`
(`pending` / `approved` / `rejected` / `needs-edit`). Edit it by hand, or use:

```bash
# approve/reject by case id (no ids = all cases)
ghostlab review-dataset --dataset datasets/cortex \
  --approve case-a case-b --reject case-c
```

Then run only the approved cases:

```bash
ghostlab run-dataset --dataset datasets/cortex \
  --target targets/cortex-local.json --approved-only
```

### Run a dataset: `run-dataset`

Execute every case (use `--limit` for small dev runs):

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
**arguments, result, error, and status** — plus the clean assistant message —
into `events.jsonl`, with a per-turn table in `report.md`. (Runners without the
codex-json parser fall back to scraping codex's plain-text
`mcp: <server>/<tool> started|(completed)|(failed)` lines for tool name +
status.) When a scenario declares `exercises`, the report also shows a
tool-coverage line (expected vs. actually called).

stdout and stderr are kept **separate**: only stdout (with known host noise
redacted) becomes the conversational message handed to the other agent, while
raw stderr is logged for debugging. This prevents the emulator from reacting to
agent-host warnings instead of the assistant's actual reply.

### Evaluate a run: `evaluate`

Turn a run into a structured pass/fail verdict:

```bash
ghostlab evaluate --run runs/<id> --capabilities runs/<id>-inspect/capabilities.json
```

It combines **deterministic checks** over the captured tool calls (failed calls,
expected-tool coverage from the scenario's `exercises`) with a **codex
LLM-judge** that scores each `success_criterion` (met?) and `failure_signal`
(triggered?) from the transcript and tool calls. Hard gates force an overall
`fail`: the run crashed, a failure signal triggered, or — when `--capabilities`
is supplied — the assistant claimed a tool the server does not expose. Writes
`verdict.json` + `verdict.md`; exits non-zero unless the verdict is `pass`
(`partial` exits 0 unless `--strict`), so datasets can gate CI.

### Score a whole dataset

`run-dataset --evaluate` runs the codex judge on each case and records the
verdict in the per-case run dir and in the summary's `results.json` (stamped with
the ghostlab version + dataset seed for provenance):

```bash
ghostlab run-dataset --dataset datasets/cortex \
  --target targets/cortex-local.json \
  --aut-runner runners/codex-cortex-local-session.json \
  --evaluate --capabilities runs/<id>-inspect/capabilities.json
```

### Compare two runs: `compare`

After editing a prompt or tool description, re-run the same dataset and diff the
results to see what got better or worse:

```bash
ghostlab compare --base runs/<base>-summary --candidate runs/<cand>-summary \
  --output comparison.md
```

It diffs case-by-case on verdict (falling back to run status), listing
**regressions** (newly failing) first, then **fixes** (newly passing), then other
changes. Exits non-zero when there are regressions, so it can gate CI.

### Session runner (one live agent across turns)

By default each turn spawns a fresh agent process and the orchestrator replays
the transcript. The **session runner** (`"kind": "codex-session"`) instead keeps
one codex session alive: turn 1 records the `thread_id` from the JSONL
`thread.started` event, and later turns run `codex exec resume <thread_id>` so
codex retains context — the orchestrator then sends only the new user message
instead of the whole transcript (fewer tokens, no repeated cold-start noise). The
shared `session_id` is logged per turn in `events.jsonl` for auditability.

```bash
ghostlab run --target targets/cortex-local.json --scenario <scenario.json> \
  --aut-runner runners/codex-cortex-local-session.json --user-runner <user.json>
```

### Validate your setup: `doctor`

Check that codex is reachable and that runner presets are well-formed before a
run:

```bash
ghostlab doctor               # validates runners/*.json
ghostlab doctor --runners runners/codex-cortex-local-session.json
```

It reports the codex binary + version and validates each runner's kind, command,
and parser (e.g. a `codex-session` command must contain `exec`).

### Default agent backend

`codex` is the default coding-agent backend for the generation and run stages.
The `inspect` command needs no agent — it is a direct MCP client. The codex
binary is auto-detected from `$PATH`, then the macOS app bundle
(`/Applications/Codex.app/Contents/Resources/codex`); override with
`$REHEARSAL_CODEX_BIN` or `--codex-bin`.

## Quick Start

Run a mock scenario without spending any coding-agent credits:

```bash
cd mcp-rehearsal
ghostlab run \
  --target targets/example-stdio.json \
  --scenario scenarios/basic-discovery.json \
  --aut-runner runners/mock-aut.json \
  --user-runner runners/mock-user.json
```

The run output is written under `runs/<run-id>/`:

- `events.jsonl`: structured event log
- `report.md`: readable run summary
- `target.mcp.json`: generated `mcpServers` config for the target

## Runner Configs

Mock runner:

```json
{
  "kind": "mock"
}
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

The process runner starts one fresh process per turn. `prompt_mode` can be `stdin`, `append-arg`, or `replace-placeholder`. Rehearsal also sets `REHEARSAL_TARGET_ID` and `REHEARSAL_MCP_CONFIG` for the AUT process so runner commands can inject the generated MCP config into Codex, Claude Code, or another agent host.

## Next Step

Wire the process runner to real Codex and Claude Code MCP config injection, then add a Docker Compose sandbox for target MCP apps.
