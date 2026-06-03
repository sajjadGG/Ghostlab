# MCP Rehearsal

A local end-to-end testing harness for **any MCP-exposed app** where coding agents role-play real users.

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

- `rehearsal inspect` — connect to a target MCP and capture what it exposes.
- `rehearsal profile` — turn an `inspect.json` into a capability profile (codex).
- `rehearsal generate-scenarios` — generate scenarios from a profile (codex).
- `rehearsal generate-personas` — generate a reusable persona library (codex).
- `rehearsal generate-dataset` — build a persona x scenario dataset (codex).
- `rehearsal review-dataset` — review & curate a dataset (coverage, flags, approve/reject).
- `rehearsal run-dataset` — run every case in a dataset.
- `rehearsal run` — run a dual-agent E2E scenario.

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
`mcp-ghostlab` and add a trusted publisher for this repository, workflow
`.github/workflows/release.yml`, environment `pypi`. No PyPI username or token
needs to be committed.

### Understand a new MCP: `inspect`

Point it at a target and it introspects the server without any coding-agent
credits or manual `curl`:

```bash
python3 -m rehearsal.cli inspect --target targets/cortex-local.json
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
python3 -m rehearsal.cli profile \
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
python3 -m rehearsal.cli generate-scenarios \
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
python3 -m rehearsal.cli generate-personas \
  --profile runs/<id>-inspect/capabilities.json \
  --n 4 \
  --output-dir personas
```

Each persona has a `summary`, behavioral `traits` (terse, impatient, easily
confused, non-native, ...), and a domain `context` map (native_language,
target_exam, level, ...). Pass one to a run with `--persona`:

```bash
python3 -m rehearsal.cli run ... --persona personas/ielts-power-user.json
```

The user-emulator prompt is composed from the persona's summary + traits +
context. Scenarios with an inline `persona` string still work unchanged; when a
persona is supplied, the scenario's inline note refines it.

### Build a dataset: `generate-dataset`

A dataset is a **persona x scenario matrix** — different users, and different
scenarios tailored to each of them. For every persona, codex generates
persona-specific scenarios, and the pairs become runnable cases:

```bash
python3 -m rehearsal.cli generate-dataset \
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
python3 -m rehearsal.cli review-dataset \
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
python3 -m rehearsal.cli review-dataset --dataset datasets/cortex \
  --approve case-a case-b --reject case-c
```

Then run only the approved cases:

```bash
python3 -m rehearsal.cli run-dataset --dataset datasets/cortex \
  --target targets/cortex-local.json --approved-only
```

### Run a dataset: `run-dataset`

Execute every case (use `--limit` for small dev runs):

```bash
python3 -m rehearsal.cli run-dataset \
  --dataset datasets/cortex \
  --target targets/cortex-local.json \
  --aut-runner runners/codex-cortex-aut.json \
  --user-runner runners/codex-user-emulator.json \
  --limit 2
```

Each case runs through the orchestrator (with its persona) into its own run
directory, and a dataset-level `summary.md` + `results.json` capture per-case
status and turn counts.

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
python3 -m rehearsal.cli run \
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
