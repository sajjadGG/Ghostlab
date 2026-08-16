---
name: ghostlab
description: Test an MCP server or a configured AI agent end to end with Ghostlab — a scripted user role-plays against the agent, real tool calls are captured, and an LLM judge scores the outcome inside an OpenShell sandbox. Use when asked to evaluate, test, validate, or find problems in an MCP server, an agent configuration, or a skill; when asked whether an MCP "actually works" with a real agent; or to generate personas/scenarios, judge a run, or produce an evaluation report.
---

# Ghostlab

Ghostlab answers a question unit tests cannot: **can a real agent get a real
task done with this thing?** One agent session plays the user, another holds the
capability under test, they talk for several turns, every tool call is captured,
and a judge scores the transcript against the scenario's criteria.

Protocol checks (schema lint, does a call 500) run too, but they are the cheap
half. The expensive, useful half is the conversation.

## Pick the right entry point

| The thing under test | Command |
| --- | --- |
| An MCP server | `ghostlab create` |
| A configured agent (model + instructions + skills + MCPs + code) | `ghostlab lab`, or an `agent.json` (below) |
| A skill folder | `ghostlab create --skill ./skills/x` |

**Prefer the file-driven path when working inside a coding harness.**
`ghostlab lab` is an interactive wizard; a written `agent.json` plus
`create --yes` is scriptable, reviewable, and reproducible.

## Before anything else

```bash
ghostlab doctor --probe
```

`--probe` sends one live request to each LLM backend. A plain `doctor` only
checks that binaries exist, which does not catch an expired quota or a CLI too
old for the model an account is pinned to — the failure then surfaces much later
as `generation skipped`. If the selected backend is unusable, doctor names the
flag to switch:

```bash
--llm-backend opencode --model github-copilot/claude-sonnet-4.5
```

OpenShell must report `Connected`. If it does not, Docker is usually stopped:
`open -a Docker && brew services restart openshell`.

## Evaluating an MCP server

```bash
ghostlab create --name my-mcp --target ./mcp.json --no-discover --yes
ghostlab discover --job my-mcp --sample safe
ghostlab plan     --job my-mcp --llm-backend opencode --model github-copilot/claude-sonnet-4.5
ghostlab test     --job my-mcp --llm-backend opencode --model github-copilot/claude-sonnet-4.5 --pdf
ghostlab review   --job my-mcp
```

`--target` accepts the standard `mcpServers` config you already give Claude
Desktop, Cursor, or Codex — use `--server <name>` when it defines several.

**`--sandbox local` is required, not optional, for MCPs that need host-only
resources** — a server driving a macOS app (Safari, Mail) via AppleScript, or
one needing your logged-in browser profile, can never work in a Linux container.
It is a per-invocation flag, so repeat it on `discover` *and* `test`.

To keep cost down while iterating, run the deterministic suites alone — they use
no model at all:

```bash
ghostlab test --job my-mcp --hosts direct-mcp --suite smoke --suite edge
```

## Evaluating a configured agent

Write the agent as a file, then create the job from it:

```json
{
  "id": "release-bot",
  "description": "Drafts release notes from the changelog. Must never claim a release was published.",
  "runtime": {
    "backend": "opencode",
    "model": "github-copilot/claude-sonnet-4.5",
    "instructions": ["AGENTS.md"],
    "skills": { "paths": ["skills/release-notes"] },
    "permission": { "bash": "deny", "edit": "allow" }
  },
  "workspace": "repo",
  "inputs": { "mcps": [{ "config_ref": "mcp.json", "server": "github" }] },
  "sandbox": {
    "backend": "openshell",
    "image": "docker/agent-sandbox.Dockerfile",
    "credentials": { "opencode_auth": true }
  }
}
```

```bash
ghostlab create --name release-bot --agent ./agent.json --no-discover --yes
ghostlab discover --job release-bot
ghostlab plan --job release-bot --llm-backend opencode --model github-copilot/claude-sonnet-4.5
ghostlab test --job release-bot --llm-backend opencode --model github-copilot/claude-sonnet-4.5 --pdf
ghostlab review --job release-bot
```

Paths are resolved relative to the agent file. `runtime` mirrors OpenCode's own
config schema, so anything settable on a real agent is settable here (model,
`small_model`, `instructions`, `skills`, `agents` for subagents, `tools`,
`permission`, `commands`, `default_agent`, `subagent_depth`). A misspelled key
is rejected by name rather than silently ignored.

`description` matters: it is treated as **authoritative** by purpose inference,
which reads it plus the instruction files, skills, subagent prompts, permissions
and MCP inventory to work out what the agent is for. Personas and scenarios come
from that, so they are about the agent's job rather than its tool families. The
inferred profile lands in `workspace/agent-profile.json` — read it, and if the
purpose is wrong, fix `description` and re-run `plan --regenerate`.

With a configured agent, the CLI, its MCPs, and its code all run inside the
sandbox, and the workspace is an uploaded copy — so `edit`/`bash` cannot reach
the real checkout. `credentials.opencode_auth` is required for the agent to call
its model; the file is uploaded outside the workspace and redacted from reports.

## Reading the results

Everything lands under `jobs/<name>/workspace/`:

| File | What to read it for |
| --- | --- |
| `test/<ts>/results.json` | per-case status, pass rate, failure detail |
| `test/<ts>/readiness.md` | gate verdict, failure **clusters**, ranked repairs |
| `test/<ts>/<run>/report.md` | the conversation with an inline tool-call table |
| `test/<ts>/<run>/verdict.json` | judge per-criterion evidence **and** deterministic checks |
| `test/<ts>/<run>/critique.md` | tool ergonomics score and concrete API suggestions |
| `test/<ts>/<run>/rollout.pdf` | the whole run as one shareable document (`--pdf`) |
| `discover/<ts>/contract.json` | schema lint, risk labels, undocumented params |
| `generated/<ts>/personas/`, `scenarios/` | what was generated, and why |

Start at `readiness.md` — it clusters failures, so 19 separate red cases become
one finding like *"input-validation ×19: invalid input accepted without error"*.

**Read `verdict.json`, not just the summary line.** It carries the judge's
per-criterion evidence *next to* deterministic facts (`exercises_called`,
`coverage`, `efficiency`). When they disagree, the deterministic side is the
reliable one — an agent can narrate a tool call it never made, and the tool-call
table proves it.

A `fail` is usually a real finding about the target, not a harness problem.
Distinguish:

- `fail` — the judge scored the conversation against its criteria. Genuine.
- `error` / `harness_error` — Ghostlab could not run the case. Fix the setup.
- `skip` — no capable host; usually no agent-under-test host is configured.

## Common failures and what they mean

| Symptom | Cause |
| --- | --- |
| `sandbox_command_missing` | The MCP's program is not in the sandbox. Use `--sandbox local`, or add `sandbox.uploads`. |
| `generation skipped (...)` | The LLM backend is unusable. Run `doctor --probe`. |
| Every tool returns `403` in the sandbox | Default-deny egress. The MCP calls an external API; add a `sandbox.policy` allowing that host. |
| `sandbox_policy_invalid` | Policy YAML rejected by OpenShell — the message quotes the offending field. |
| Conversational cases all `skip` | No agent-under-test host configured on the job. |
| `Model not found` inside the sandbox | The policy is missing OpenCode's model catalog (`models.dev`). |

## Rules that matter

- **Never present a `fail` as a Ghostlab bug without checking `verdict.json`.**
  Most are real findings about the target.
- **The user emulator must never get the target's MCP.** It plays a human; giving
  it tools collapses the dual-agent premise into one agent talking to itself.
- Conversational runs cost real tokens and take real time. Default to
  `--personas 1 --scenarios-per-persona 1` while iterating; scale up for a
  release gate.
- `plan` caches its generated dataset. Use `--regenerate` after changing the
  agent's description or capabilities, or you will re-run stale scenarios.
- `--sandbox` is per-invocation and does not persist to `job.yaml`. Repeat it.

## Reference

- `docs/configured-agent-lab.md` — the agent-lab design in full
- `docs/cli.md` — every command and flag
- `ghostlab <command> --help` — authoritative, and cheaper than guessing
