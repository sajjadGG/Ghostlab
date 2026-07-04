# Getting Started

## Install From A Checkout

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

This installs the package in editable mode and adds the `ghostlab` and `rehearsal` console scripts. Prefer `ghostlab` in new docs and scripts.

```bash
ghostlab --help
ghostlab --version
```

## Create A Job (Recommended Starting Point)

A **job** is one MCP evaluation, and everything about it lives in one folder. Use the guided wizard to set one up:

```bash
ghostlab create
# ? Job name: cortex-eval
# ? Target MCP URL (or path to a target JSON): http://localhost:8000/mcp
# ? Transport [streamable-http]:
# ? Personas to generate [2]:
# ? Scenarios per persona [2]:
# ? Min pass rate (release gate) [0.9]:
```

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
  --target targets/example-stdio.json \
  --scenario scenarios/basic-discovery.json \
  --aut-runner runners/mock-aut.json \
  --user-runner runners/mock-user.json
```

Run output is written under `runs/<run-id>/`:

- `events.jsonl`: structured event log.
- `report.md`: readable run summary.
- `target.mcp.json`: generated MCP server config for the target.

## Inspect A Real MCP Target

```bash
ghostlab inspect --target targets/cortex-local.json
```

`inspect` connects directly to the MCP server, runs the initialize handshake, lists tools/resources/prompts, and writes an `inspect.json` plus readable `inspect.md`. It does not need Codex or another agent.

## Build A Capability Profile

```bash
ghostlab profile --inspect runs/<id>-inspect/inspect.json
```

The profile combines deterministic taxonomy with a Codex-generated domain summary and workflow map. Scenario and dataset generation use this profile as their source of truth.
