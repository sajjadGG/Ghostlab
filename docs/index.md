# Ghostlab Wiki

Ghostlab is a local end-to-end testing harness for MCP-exposed apps. It runs one coding-agent session as the assistant under test and another as a user emulator, then captures the transcript, MCP tool calls, reports, verdicts, and dataset summaries that make failures reproducible.

Use it when you want to test an MCP server through the same Codex or Claude Code path real users rely on, without building a separate LLM-provider harness.

## What Ghostlab Gives You

- Direct MCP inspection without spending agent credits.
- Capability profiles derived from real exposed tools, resources, and prompts.
- Scenario, persona, and dataset generation for repeatable test coverage.
- Dual-agent scenario runs with structured event logs and markdown reports.
- Tool-call capture for Codex JSONL output.
- Optional LLM-judge evaluation and dataset comparisons.

## Common Flow

```bash
ghostlab inspect --target target.json
ghostlab profile --inspect runs/<id>-inspect/inspect.json
ghostlab generate-dataset --profile runs/<id>-inspect/capabilities.json --name cortex
ghostlab review-dataset --dataset datasets/cortex --profile runs/<id>-inspect/capabilities.json
ghostlab run-dataset --dataset datasets/cortex --target target.json --approved-only
```

Start with [Getting Started](getting-started.md), then use the [CLI Reference](cli.md) for the full command map.
