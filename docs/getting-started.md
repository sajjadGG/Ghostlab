# Getting Started

## Install From A Checkout

```bash
python3 -m venv .venv          # Python 3.10+
.venv/bin/pip install -e .
```

This installs the package in editable mode and adds the `ghostlab` and `rehearsal` console scripts. Prefer `ghostlab` in new docs and scripts. (Contributors who also want the test/build/docs toolchain use `pip install -r requirements-dev.txt` instead — see `CONTRIBUTING.md`.)

```bash
ghostlab --help
ghostlab --version
```

## Create A Job (Recommended Starting Point)

A **job** is one MCP evaluation, and everything about it lives in one folder. `ghostlab create` asks only for what it can't infer — a name and a target — then **inspects the target immediately** so the job is validated and its capabilities are populated in one step:

```bash
ghostlab create
# ? Job name: cortex-eval
# ? Target MCP URL or config path: http://localhost:8000/mcp
# → Created job 'cortex-eval' … then runs discover and prints the tool inventory
```

Everything else (persona/scenario counts, gates, prompts) uses documented defaults you edit in `job.yaml` — pass `--personas`, `--scenarios-per-persona`, `--min-pass-rate`, or `--aut-runner` to set them up front. Add `--no-discover` to just scaffold without inspecting.

It scaffolds a self-contained directory:

```
jobs/cortex-eval/
  job.yaml         # the whole config: target, hosts, generation, test, prompts, gates
  test-plan.yaml   # produced by `ghostlab plan`
  workspace/       # discover/generated/test artifacts + ghostlab.sqlite3
  runs/            # dual-agent run output
```

`job.yaml` is populated with **editable defaults** for every knob — persona/scenario counts, suites, judge, gates, and a `prompts:` section where you can override any built-in prompt (each entry is blank = use the built-in; the file header lists the `{placeholders}` each prompt accepts). An explicit CLI flag still wins over a `job.yaml` setting, which wins over the code default.

Non-interactive (for scripts/CI):

```bash
ghostlab create --name cortex-eval --target http://localhost:8000/mcp \
  --aut-runner runners/codex-cortex-local-aut.json --yes
```

### Bring your existing MCP config

`--target` also accepts the **standard `mcpServers` config** you already give Codex, Claude Desktop, Cursor, or VS Code — GhostLab normalizes it into a target (issue #32). This covers local **stdio** servers and remote **HTTP/SSE** servers alike:

```jsonc
// mcp.json
{
  "mcpServers": {
    "obsidian": { "command": "npx", "args": ["-y", "obsidian-mcp"], "env": { "VAULT": "/notes" } },
    "github":   { "url": "https://api.githubcopilot.com/mcp/",
                  "headers": { "Authorization": "Bearer ${GITHUB_TOKEN}" } }
  }
}
```

```bash
ghostlab create  --name gh --target ./mcp.json --server github   # pick a server by name
ghostlab inspect --target ./mcp.json --server obsidian           # inspect works the same way
```

When a config has a single server, `--server` is optional; with several, GhostLab lists them and asks you to choose.

**Auth without leaking secrets:** header/env values may reference environment variables (`${GITHUB_TOKEN}`), which are expanded at connection time — so the token stays in your shell, not in the tracked `job.yaml`. If you have only a URL, the wizard can add the header for you:

```bash
export GITHUB_TOKEN=ghp_xxx
ghostlab create --name gh --target https://api.githubcopilot.com/mcp/ \
  --header 'Authorization: Bearer ${GITHUB_TOKEN}' --yes
```

A `401` from `discover`/`inspect` means the server got no (or a wrong) auth header — check that the header is present in the target and that the referenced env var is exported.

Then run the loop against the job by name (`--job`), no paths to juggle:

```bash
ghostlab discover --job cortex-eval    # inspect + lint + refresh capabilities
ghostlab plan     --job cortex-eval    # coverage-driven test-plan.yaml
ghostlab test     --job cortex-eval    # execute across host adapters
ghostlab review   --job cortex-eval    # readiness report / release gate
```

Inside a job directory you can drop `--job` entirely — the commands auto-detect `job.yaml` in the current folder.

## Run A Mock Scenario

Mock runners let you exercise the orchestrator without spending coding-agent credits.

```bash
ghostlab run \
  --target examples/target.json \
  --scenario examples/scenario.json \
  --aut-runner runners/mock-aut.json \
  --user-runner runners/mock-user.json
```

Run output is written under `runs/<run-id>/`:

- `events.jsonl`: structured event log.
- `report.md`: readable run summary.
- `target.mcp.json`: generated MCP server config for the target.

## Inspect A Real MCP Target

```bash
ghostlab inspect --target examples/target.json
# or a standard MCP client config:
ghostlab inspect --target ./mcp.json --server obsidian
```

`inspect` connects directly to the MCP server, runs the initialize handshake, lists tools/resources/prompts, and writes an `inspect.json` plus readable `inspect.md`. It does not need Codex or another agent. `--target` accepts either a GhostLab target JSON or a standard `mcpServers` config (use `--server` to pick one when it defines several).

## Build A Capability Profile

```bash
ghostlab profile --inspect runs/<id>-inspect/inspect.json
```

The profile combines deterministic taxonomy with a Codex-generated domain summary and workflow map. Scenario and dataset generation use this profile as their source of truth.
