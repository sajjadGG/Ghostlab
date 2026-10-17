# Ghostlab Vision And Roadmap

_Current implementation snapshot: October 2026._

This page describes the product that exists now and the remaining direction.
It replaces the original MCP-only gap analysis, whose proposed spec, setup,
planning, host-adapter, Apps, resume, and readiness layers have since been
implemented.

## Product Vision

Ghostlab is a sandboxed runtime and QA plane for configured AI agents. An agent
is defined once — a harness and model plus instructions, MCPs, skills,
workspace files, and assets — and that one definition can be:

- **run** on any task in Claude Code, Codex, GitHub Copilot CLI, or OpenCode
  inside NVIDIA OpenShell (`ghostlab exec`), to get an answer or an artifact;
- **evaluated** against realistic, generated user goals (`lab`, `create`,
  `test`, `review`);
- used as the subject of **eval sets** built from an MCP server or a skill.

The central evaluation question is outcome-oriented: can this configured agent
reliably complete realistic user goals without violating its security or
permission boundaries?

Ghostlab should provide:

- A canonical, human-editable agent evaluation definition.
- Deterministic contract checks and generated behavioral cases.
- Realistic dual-agent conversations with structured traces.
- Safe-by-default execution for untrusted runners and local MCP processes.
- Repeatable review gates, resume behavior, variance reporting, and CI outputs.
- MCP Apps probing, rendering, and interaction when the agent exposes UI.

## Current Architecture

### Agent As The Evaluation Boundary

The canonical `agent` definition contains:

- `runner`: a process or supported coding-agent session.
- `instructions`: agent-level behavior and policy.
- `inputs.mcps`: zero or more MCP definitions or standard config references.
- `inputs.skills`: zero or more `SKILL.md` inputs.
- `workspace` and `inputs.assets`: files staged for the run.
- `tests`: optional user-authored cases seeded directly into the plan.

`--target` and `--skill` remain convenient shorthands. Ghostlab normalizes them
into the same agent-centric model so future orchestration does not need separate
MCP, skill, and composed-agent pipelines.

### OpenShell-First Runtime

[NVIDIA OpenShell](https://docs.nvidia.com/openshell/latest/) is the default
execution backend. Ghostlab creates separate sandboxes for the agent under test
and user emulator, stages declared inputs, filters the parent environment,
attaches named providers, runs each turn through `openshell sandbox exec`,
retains logs, and cleans up afterward.

Local stdio MCP processes launched by Ghostlab use the same boundary. Sandbox
setup, gateway, policy, image, upload, download, and timeout failures are
classified as harness failures rather than agent or target failures.

`--sandbox local` is an explicit compatibility option for trusted host
execution. OpenShell errors never trigger an automatic local fallback.

### Job Pipeline

The recommended workflow is:

```text
create → discover → plan → test → review
```

- `create` builds a self-contained job from an agent, MCP, or skill.
- `discover` inventories the primary capability, lints its contract, samples
  safe tools when requested, and probes MCP Apps resources.
- `plan` combines deterministic coverage cases, inline agent tests, and
  persona-grounded generated scenarios.
- `test` executes cases across direct-MCP, runner, and Apps hosts, checkpoints
  results, and optionally judges conversational outcomes.
- `review` applies readiness gates and prioritizes repair work.

Jobs persist YAML/JSON/Markdown artifacts plus optional SQLite history. Partial
runs can resume without discarding completed cases, and harness outages are
kept out of the target pass rate.

### Evaluation And Reporting

Ghostlab currently supports:

- Deterministic protocol, schema, edge, and golden assertions.
- Codex-backed open-ended judging and tool-usability critique.
- Structured MCP tool-call capture and failure-cause classification.
- Persona × scenario datasets, curation, scorecards, and regression comparison.
- Repeat runs with flaky-case and variance reporting.
- Standalone HTML dashboards and readable Markdown artifacts.
- MCP Apps discovery, CSP diagnostics, browser rendering, UI intents, and
  screenshots.

### Permissions

Host-level execution permission and conversational permission are separate.
OpenShell and the coding-agent host decide what a process may actually do. When
the assistant asks the emulated user for confirmation, the emulator answers in
persona and treats destructive, costly, credential-sensitive, or private-data
actions cautiously. Reports distinguish permission denial, client timeout,
client cancellation, backend cancellation, and server stream failure.

## Remaining Product Gaps

### 1. Richer Multi-Input Discovery

Execution receives the complete agent composition, but automatic discovery and
generation still use the first MCP or skill as the primary capability. The next
step is a merged capability graph across every MCP, skill, instruction source,
and declared asset, including conflicts and cross-capability workflows.

### 2. One Runtime Path For Run And Evaluate

`ghostlab exec` resolves an agent definition into a sandboxed invocation for
every harness, while the job pipeline still materializes its own per-harness
runner files and only puts the *whole* agent in the sandbox for OpenCode. The
next step is to route the evaluation pipeline's agent-under-test and
`artifact-run` through the same resolver, so a definition that runs under
`exec` is exactly what gets evaluated and benchmarked. Two concrete gaps found
while building `exec`:

- `artifact-run` still requires a raw `runner.command` and does not attach the
  definition's MCPs and skills.
- The job pipeline's OpenShell Copilot runners always pass `--session-id` and
  `--no-remote`, which the Copilot CLI in OpenShell's `base` image (1.0.16)
  rejects; `exec` already omits them inside the sandbox.

### 3. Portable Sandbox Profiles

OpenShell configuration should gain reusable project profiles for:

- Read-only versus writable uploads.
- Deterministic fixtures and state reset.
- Provider and egress-policy templates.
- CPU, memory, and optional GPU budgets.
- CI gateway configuration and version pinning.

OpenShell is alpha software, so Ghostlab should keep CLI compatibility tests and
surface supported runtime versions clearly.

### 4. Stronger Agent-Definition Validation

Agent configs should receive a published schema, richer diagnostics, and a
dry-run command that shows the resolved runner, staged paths, providers,
environment allowlist, MCP composition, and test inventory before creating a
sandbox.

### 5. Broader Host Fidelity

The host-adapter layer should continue to model product-specific behavior:

- Codex and Claude configuration semantics.
- Additional agent runtimes and session protocols.
- Host-specific MCP Apps bridges and metadata aliases.
- Approval, cancellation, retry, and context-compaction behavior.

### 6. Better Coverage Intelligence

Planning should reason over state transitions and workflow graphs, not only tool
families. Useful additions include pairwise capability coverage, mutation/reset
coverage, credential-boundary cases, long-horizon workflows, and historical
failure seeding across jobs.

### 7. Evaluation Calibration

LLM judging needs ongoing calibration against human-reviewed fixtures. The
highest-value work is disagreement reporting, judge-version fingerprints,
multi-judge policies for release gates, and confidence intervals that separate
model variance from actual product regressions.

### 8. CI And Fleet Operation

The current artifacts are CI-friendly, but larger deployments need shared
OpenShell gateways, concurrency controls, sandbox quotas, artifact retention,
provider rotation, sharding, and a stable machine-readable summary contract.

## Near-Term Priority Order

1. Route the evaluation pipeline and `artifact-run` through the `exec` resolver.
2. Publish and validate the composed-agent schema and dry-run output.
3. Merge discovery across all configured MCPs and skills.
4. Add OpenShell version-compatibility integration tests and reusable policies.
5. Expand deterministic state/reset and security-boundary coverage.
6. Calibrate judges against maintained human-reviewed fixtures.
7. Add CI sharding and shared-gateway operational guidance.

## Definition Of Done

Ghostlab reaches the intended product boundary when a maintainer can provide an
arbitrary agent configuration and test cases, run them safely and repeatedly in
OpenShell, inspect complete evidence across every configured capability, resume
after infrastructure failure, and gate a release without writing a custom eval
harness.

The default workflow must remain safe and explicit: declared inputs only,
allowlisted environment only, policy-controlled network and credentials,
classified harness failures, retained evidence, and no silent host fallback.
