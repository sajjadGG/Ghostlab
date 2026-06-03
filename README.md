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

Rehearsal exposes subcommands (the bare `--target ... --scenario ...` form still
works and is treated as `run`):

- `rehearsal inspect` — connect to a target MCP and capture what it exposes.
- `rehearsal run` — run a dual-agent E2E scenario.

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

### Default agent backend

`codex` is the default coding-agent backend for the generation and run stages.
The `inspect` command needs no agent — it is a direct MCP client.

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
