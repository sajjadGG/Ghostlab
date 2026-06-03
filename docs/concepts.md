# Concepts

## Target

A target describes the MCP server under test:

- `id`: stable target identifier.
- `transport`: `stdio`, `sse`, or `streamable-http`.
- `connection`: command and environment for stdio, or URL and headers for network transports.
- `capabilities`: optional expected tools/resources/prompts.
- `startup`: optional boot and health-check settings.

## Scenario

A scenario is the task the user emulator tries to accomplish. It includes a goal, success criteria, failure signals, stop conditions, and an optional `exercises` list that names tools the assistant should be driven to use.

## Persona

A persona is the user identity used during a run. Persona files hold durable traits and context, while a scenario's inline persona text should stay situational.

## Dataset

A dataset is a persona x scenario matrix. It contains a manifest, generated personas, generated scenarios, and runnable cases with curation status values like `pending`, `approved`, `rejected`, and `needs-edit`.

## Runner

A runner controls how Ghostlab talks to an agent host. Mock runners are deterministic. Process runners start a fresh command per turn. Codex session runners keep one live Codex thread across turns by resuming the thread id captured from JSONL output.

## Outputs

Each run captures:

- Full user and assistant transcript.
- Structured MCP tool-call events when available.
- Raw stderr for debugging host warnings.
- Clean assistant stdout for conversational handoff.
- Markdown reports and dataset summaries.
