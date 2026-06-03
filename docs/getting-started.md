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
