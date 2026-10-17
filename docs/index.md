# Ghostlab Wiki

Ghostlab lets you define an agent once — model, instructions, skills, MCP
servers, workspace — and run it in any coding-agent harness: Claude Code,
Codex, GitHub Copilot CLI, or OpenCode. Everything runs inside an NVIDIA
OpenShell sandbox by default, so an agent can be given real tools and real
permissions without touching your machine.

With that one definition you can:

- **Run it** — `ghostlab exec` hands the agent a task and returns its answer,
  tool trace, and any files it produced. See [Run an Agent](run-an-agent.md).
- **Evaluate it** — `ghostlab lab` and `ghostlab create` generate realistic
  scenarios, drive the agent with a simulated user over multiple turns, judge
  the outcomes, and gate releases. See the
  [Configured Agent Lab](configured-agent-lab.md).
- **Build eval sets for an MCP server or a skill** — persona × scenario
  datasets generated from a server's tools or a `SKILL.md`, curated as files
  and rerun in CI. See [Datasets and Evaluation](datasets-evaluation.md).

## What Ghostlab Gives You

- One harness-agnostic agent definition, with MCP-only and skill-only shorthands.
- OpenShell isolation for the agent CLI, its stdio MCP servers, and its workspace.
- Direct MCP inspection without spending agent credits.
- Capability profiles derived from real exposed tools, resources, and prompts.
- Scenario, persona, and dataset generation for repeatable test coverage.
- Dual-agent scenario runs with structured event logs and markdown reports.
- Tool-call capture for Claude Code, Codex, Copilot, and OpenCode output.
- LLM-judge evaluation, scorecards, dataset comparisons, and benchmark scoring.

## Common Flow

```bash
openshell status
ghostlab doctor

# Run
ghostlab exec --agent examples/agent-lab/task-agent.json "Draft the release notes"

# Evaluate
ghostlab create --name my-agent --agent examples/agent.json --yes
ghostlab test --job my-agent
ghostlab review --job my-agent
```

For trusted host execution, opt out explicitly with `--sandbox local`; Ghostlab
never falls back automatically. Start with [Getting Started](getting-started.md),
then use the [CLI Reference](cli.md) for the full command map.
