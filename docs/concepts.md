# Concepts

## Spec (`ghostlab.yaml`)

The spec is the canonical, human-editable description of one MCP or skill under test:
how to connect (`target`), how to set it up (`setup`), which agent hosts should
exercise it (`hosts`), what it exposes (`capabilities`, refreshed by
`ghostlab discover`), and what quality bar it must clear (`review.gates`).
`ghostlab init` creates it from a target JSON; discovery artifacts (inventory,
contract lint, MCP Apps probes) accumulate under its workspace directory
(default `.ghostlab/`). One spec per MCP is the intended unit of QA.

## Contract

The contract (`contract.json`) is the deterministic judgment of the spec's
discovered surface: schema-quality findings, risk labels per tool (read-only /
mutates-state / destructive / credential-bearing / ui-producing, from MCP tool
`annotations` first and name heuristics second), and MCP Apps metadata
compatibility checks. It is regenerated on every `discover` and is the input
for coverage-driven test planning.

## Target

A target describes either an MCP server or a local skill under test:

- `id`: stable target identifier.
- `transport`: `stdio`, `sse`, `streamable-http`, or `skill`.
- `connection`: command and environment for stdio, or URL and headers for network transports.
- `capabilities`: optional expected tools/resources/prompts.
- `startup`: optional boot and health-check settings.

For a skill, `target.kind` and `transport` are `skill`, while
`connection.path` points to `SKILL.md`. Skill evaluation uses the conversational
harness and judge; direct MCP protocol checks do not apply.

## Scenario

A scenario is the task the user emulator tries to accomplish. It includes a goal, success criteria, failure signals, stop conditions, and an optional `exercises` list that names tools the assistant should be driven to use.

## Persona

A persona is the user identity used during a run. Persona files hold durable traits and context, while a scenario's inline persona text should stay situational.

## Dataset

A dataset is a persona x scenario matrix. It contains a manifest, generated personas, generated scenarios, and runnable cases with curation status values like `pending`, `approved`, `rejected`, and `needs-edit`.

## Runner

A runner controls how Ghostlab talks to an agent host. Mock runners are deterministic. Process runners start a fresh command per turn. Codex session runners keep one live Codex thread across turns by resuming the thread id captured from JSONL output.

## Permissions and approvals

There are two separate layers. Host-level approval mode (for example Codex
`-a never` and its sandbox) determines whether the AUT process may execute a
tool at all; the user emulator cannot override that layer. An assistant may
also ask the user a conversational permission question. The emulator answers
that question in persona: it normally approves reversible actions needed for
the goal, but asks or refuses when an action is destructive, exposes private
data or credentials, costs money, or conflicts with the persona. It never
discusses host flags or the test harness. Host auto-denials and conversational
user refusals are logged separately as `permission_denied` tool failures versus
ordinary user messages.

## Outputs

Each run captures:

- Full user and assistant transcript.
- Structured MCP tool-call events when available.
- Raw stderr for debugging host warnings.
- Clean assistant stdout for conversational handoff.
- Markdown reports and dataset summaries.
