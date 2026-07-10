# Ghostlab Wiki

Ghostlab is an end-to-end testing harness for configured AI agents. An evaluated
agent can combine instructions, a runner, MCP servers, skills, workspace files,
and assets. Ghostlab runs one session as the assistant under test and another as
a user emulator, then captures the transcript, tool calls, sandbox logs,
reports, verdicts, and dataset summaries that make failures reproducible.

NVIDIA OpenShell is the default execution backend. Use Ghostlab when you want
to test an agent through the same Codex, Claude Code, or process-runner path
real users rely on without granting the evaluated code broad host access.

## What Ghostlab Gives You

- Agent-centric evaluation with MCP-only and skill-only compatibility shorthands.
- OpenShell isolation for agent runners and local stdio MCP processes.
- Direct MCP inspection without spending agent credits.
- Capability profiles derived from real exposed tools, resources, and prompts.
- Scenario, persona, and dataset generation for repeatable test coverage.
- Dual-agent scenario runs with structured event logs and markdown reports.
- Tool-call capture for Codex JSONL output.
- Optional LLM-judge evaluation and dataset comparisons.

## Common Flow

```bash
openshell status
ghostlab doctor
ghostlab create --name my-agent --agent examples/agent.json --yes
ghostlab test --job my-agent
ghostlab review --job my-agent
```

For trusted host execution, opt out explicitly with `--sandbox local`; Ghostlab
never falls back automatically. Start with [Getting Started](getting-started.md),
then use the [CLI Reference](cli.md) for the full command map.
