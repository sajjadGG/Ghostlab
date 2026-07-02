# MCP GhostLab Vision Gap Analysis and Implementation Roadmap

Date: 2026-07-02

## Executive Summary

MCP GhostLab is already pointed at the right thesis: MCP quality cannot be judged only by protocol conformance or deterministic unit tests. A real MCP or MCP App has two probabilistic surfaces:

- The host/model surface: will Codex, Claude Code, ChatGPT, or another MCP host discover the right tools, understand their descriptions, call them with valid arguments, handle errors, and recover?
- The user surface: will realistic users phrase goals ambiguously, change their mind, provide partial context, click UI widgets, and expose edge cases that a scripted unit test would miss?

The current repository has a meaningful first version of that loop:

- `inspect` discovers tools, resources, prompts, server info, and basic lint findings.
- `profile` turns inspection into a capability profile.
- `generate-scenarios`, `generate-personas`, and `generate-dataset` create a persona x scenario matrix.
- `run` and `run-dataset` coordinate a dual-agent conversation between an agent-under-test and a user emulator.
- `evaluate`, `critique`, `compare`, and `scorecard` provide verdicts, regression views, and tool usability feedback.
- `apps-probe` and `apps-render` can fetch and render MCP Apps `ui://` widgets in a headless browser.
- The Streamlit UI exposes the broad pipeline.

The gap is that GhostLab is still a collection of useful stages, not yet the "sole plane" for MCP QA. It lacks a canonical MCP/app spec artifact, a setup/provisioning layer, host adapters that model real host behavior, integrated visual/user interaction loops, a strong review-to-repair workflow, security/adversarial test generation, and CI-grade reproducibility.

The best path is not to throw it away. The current architecture is a strong skeleton. The plan should be to turn it into a spec-driven test runtime where every target becomes:

1. A discovered and curated `ghostlab.yaml`.
2. A generated test plan that covers protocol, tool semantics, host selection, user behavior, visual widgets, security, and regressions.
3. A set of executable runs across one or more real host adapters.
4. A review bundle that explains failures and recommends concrete changes to tool descriptions, schemas, resources, app UI, setup, and docs.

## Product Vision

GhostLab should become the local QA plane for MCP servers and MCP Apps:

- Accept any MCP target: stdio, SSE, streamable HTTP, local process, Docker service, remote staging URL, authenticated connector, or ChatGPT/Codex/Claude-compatible config.
- Discover what it exposes: tools, schemas, resources, prompts, instructions, UI resources, authentication needs, rate limits, state surfaces, and likely workflows.
- Generate a durable spec: a human-editable YAML file describing how to start it, how to connect, what capabilities exist, what hosts should be tested, what fixtures are needed, and what risks matter.
- Generate scenario suites: happy paths, edge cases, adversarial probes, visual app flows, error recovery paths, permission/auth paths, multi-server trust-boundary paths, and regression canaries.
- Run host-realistic tests: use the user’s existing subscriptions and local clients where possible, such as Codex CLI/session, Claude Code, ChatGPT developer mode, Cursor, VS Code, Goose, MCP Inspector/MCPJam, or direct protocol runners.
- Observe everything: transcripts, prompts, tool calls, tool results, UI screenshots, DOM snapshots, host bridge messages, console/network errors, timing, auth/session metadata, model/host versions, and setup logs.
- Review results: determine scenario success, protocol correctness, tool ergonomics, UI correctness, host compatibility, security risk, and regression status.
- Recommend repairs: produce specific edits to tool names, descriptions, schemas, output shapes, `_meta` fields, CSP, app bridge behavior, error messages, setup docs, and test fixtures.

## Current Implementation Inventory

### Commands and Stages

The command surface in `rehearsal/cli.py` is already broad:

- Understand: `inspect`, `profile`
- Generate: `generate-scenarios`, `generate-personas`, `generate-dataset`, `review-dataset`
- Run: `run`, `run-dataset`
- Evaluate: `evaluate`, `critique`, `compare`, `scorecard`
- MCP Apps: `apps-probe`, `apps-render`
- Persistence and UI: `db`, `ui`
- Setup diagnostics: `doctor`

This maps well to the desired lifecycle, but the lifecycle is not yet represented as one explicit project/spec object.

### Discovery

`rehearsal/inspect.py` connects to a target, initializes the MCP server, lists tools/resources/resource templates/prompts, captures instructions, and lints backticked tool references that do not correspond to exposed tools.

Strengths:

- Works with the project’s internal `TargetConfig`.
- Captures core MCP inventory.
- Detects one important class of tool-description drift.
- Produces both JSON and Markdown artifacts.

Gaps:

- Does not produce a canonical target spec.
- Does not probe tool calls unless manually invoked elsewhere.
- Does not validate JSON Schema quality deeply.
- Does not map auth, permissions, setup, health checks, fixtures, state reset, or teardown.
- Does not classify tools by risk, state mutation, idempotency, required credentials, destructive behavior, or host visibility.
- Does not inspect resource contents beyond listing resources except in the app-specific probe path.

### Capability Profiling and Generation

`rehearsal/profile.py`, `rehearsal/generate.py`, `rehearsal/personas.py`, and `rehearsal/dataset.py` use Codex-backed generation to infer domain summaries, workflows, personas, scenarios, and case matrices.

Strengths:

- The idea is correct: let an agent reason from the discovered tool inventory.
- Scenarios are constrained to real tool names.
- Cases cover happy path, edge case, and adversarial intent labels.
- Dataset review includes duplicate detection and tool coverage.

Gaps:

- Profiles are too shallow for complex MCPs. They group by name family and read/write hints, but do not infer data model, resource graph, auth model, UI surfaces, state lifecycle, error taxonomy, or host compatibility.
- Scenario generation is not coverage-driven enough. It does not guarantee per-tool, per-workflow, per-risk, per-auth, per-UI, per-error, or per-regression coverage.
- There is no generated "test plan" artifact that explains why each case exists.
- Personas are user-centric, but host personas are missing: Codex, Claude Code, ChatGPT, and IDE hosts behave differently.
- Generated scenarios are JSON-only and relatively flat; they do not include setup steps, fixtures, expected tool trajectories, app UI intents, visual assertions, seed/state reset, or negative controls.

### Dual-Agent Orchestration

`rehearsal/orchestrator.py` runs an agent-under-test and a user emulator turn by turn. It supports stateful Codex session runners and logs events to JSONL/SQLite.

Strengths:

- The dual-agent loop is the right core abstraction.
- Stateful Codex sessions are supported.
- Prompts are logged.
- Tool calls can be recovered from Codex JSONL.
- Runs write reproducible artifacts.

Gaps:

- Runners are process-oriented, not host-oriented. A host adapter should understand setup, MCP config injection, session lifecycle, approvals, UI/browser state, and artifact capture for a named host.
- The user emulator currently emits only text. It does not see screenshots or app state, and it cannot decide UI actions from visual evidence.
- The orchestration loop does not branch into app-render flows when a tool result produces a widget.
- It does not support multi-host test matrices, repeated runs, model variance sampling, or host compatibility comparison.
- It has limited controls for secrets, sandboxes, state reset, deterministic fixtures, or destructive tool approvals.
- It does not represent the host as part of the test result identity.

### Evaluation, Critique, and Reporting

`rehearsal/evaluate.py` combines deterministic checks with a Codex judge. `rehearsal/critique.py` reviews exercised tool ergonomics. `rehearsal/scorecard.py` aggregates dataset results.

Strengths:

- Combines deterministic and model-judged evidence.
- Tracks expected tools, failed tool calls, golden assertions, and hallucinated tools.
- Provides a repair-oriented tool critique.
- Scorecards and comparisons point toward regression workflows.

Gaps:

- Evaluation is scenario-level, not system-readiness-level.
- The judge does not receive visual proof unless manually attached through app-render artifacts.
- There is no rubric for host compatibility, UI correctness, setup reliability, auth, security, or tool schema maturity.
- The critique only covers tools used in a run, not the whole discovered contract.
- There is no issue-generation or patch-suggestion output.
- Failures are not automatically clustered across runs into root causes.

### MCP Apps and Visual Testing

`rehearsal/mcp_apps.py` detects `_meta.ui.resourceUri`, fetches resources, diagnoses CSP/resource issues, and defines a UI intent schema. `rehearsal/apps_host/renderer.py` mounts widgets in a sandboxed iframe with a host bridge and captures screenshots, body text, console errors, network failures, bridge transcript, and intent logs.

Strengths:

- This is a very important differentiator.
- It follows the current MCP Apps direction: standard `_meta.ui.resourceUri`, iframe bridge, and `ui/*` JSON-RPC over `postMessage`.
- It captures useful render proof.
- Assertions exist for generic and some widget-specific checks.

Gaps:

- App rendering is a separate command, not part of the normal dual-agent run.
- The user emulator cannot inspect screenshots or DOM state before deciding what to do.
- UI intents are generic but brittle. Real apps need a richer action model: accessible tree, roles, selectors, coordinates, file uploads, scrolling, drag/drop, keyboard, hover, modal handling, and host tool calls from the widget.
- Host-specific compatibility is shallow. ChatGPT, Claude, VS Code, Postman, Goose, MCPJam, and legacy MCP-UI hosts expose different bridge/adapters.
- There is no visual diffing, responsive viewport matrix, accessibility scan, or app-state assertion DSL.
- It does not yet verify `_meta.ui.visibility`, standard vs OpenAI compatibility metadata, `structuredContent`/`content`/`_meta` split, or widget hydration semantics deeply.

## External Landscape

### MCP Inspector

The official [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is a visual developer tool for testing and debugging MCP servers. Its architecture uses a React web UI plus a Node proxy that connects to stdio, SSE, and streamable HTTP servers. It can export server configurations for clients such as Cursor and Claude Code, and the OpenAI Apps SDK docs recommend it during development because it can list tools, call tools, inspect raw requests/responses, and render components inline.

Implication for GhostLab:

- Do not compete as only a manual inspector.
- Reuse or interoperate with Inspector-style config formats.
- Consider launching Inspector/MCPJam as optional host adapters for manual replay and artifact capture.
- Match its protocol coverage and exceed it with automated scenario generation, user emulation, host-model testing, and review.

### MCP-UI and MCP Apps

[MCP-UI](https://github.com/MCP-UI-Org/mcp-ui) provides UI-over-MCP libraries and host adapters. It documents host actions, adapters for ChatGPT Apps SDK (`window.openai`), and supported host categories. OpenAI’s Apps SDK docs state that ChatGPT supports the MCP Apps open standard, runs UIs inside iframes, and communicates through `ui/*` JSON-RPC over `postMessage`. The docs recommend standard `_meta.ui.resourceUri` and `ui/*` bridge methods first, with ChatGPT-specific `window.openai` extensions only when needed. The OpenAI reference also documents tool descriptor `_meta` fields, resource `_meta.ui.csp`, `_meta.ui.domain`, and the separation between model-visible `structuredContent`/`content` and component-only `_meta`.

Implication for GhostLab:

- Treat MCP Apps as first-class, not an optional appendix.
- Test both standard MCP Apps metadata and ChatGPT compatibility aliases.
- Verify iframe bridge behavior, CSP, widget hydration, and host-specific degradation.
- Let the user emulator and judge see visual evidence.

### MCP-Bench

[MCP-Bench](https://github.com/Accenture/mcp-bench) evaluates LLMs on MCP tool-use tasks across many servers. It emphasizes discovery, tool selection, multi-step execution, multi-server tasks, task synthesis, and LLM-as-judge evaluation.

Implication for GhostLab:

- Borrow coverage concepts: fuzzy user tasks, multi-server tasks, trajectory-level planning, and task completion metrics.
- Differentiate by focusing on a developer’s own MCP/app, setup/reproducibility, real host adapters, and repair-oriented reports.

### MCPEval

[MCPEval](https://github.com/SalesforceAIResearch/MCPEval) is an MCP-based agent evaluation framework with automated task generation, evaluation, multi-turn simulation, persona support, SQLite persistence, web UI, replay, model comparison, and statistical testing.

Implication for GhostLab:

- GhostLab should add statistical confidence and repeated-run variance analysis.
- Preserve its distinctive use of local subscriptions/hosts rather than only provider API models.
- Build stronger replay and dashboard views for host/model comparisons.

## Primary Product Gap

GhostLab’s deepest missing piece is a canonical project/spec layer. Today the user has to manually move between target JSON, inspect artifacts, profiles, datasets, runner configs, app render commands, and run summaries. The vision needs one artifact that describes the MCP under test and its QA universe.

Recommended artifact: `ghostlab.yaml`

Example shape:

```yaml
schema_version: 1
id: cortex-local
name: Cortex Language MCP

target:
  transport: streamable-http
  url: http://127.0.0.1:8020/mcp
  headers_env:
    Authorization: CORTEX_TEST_AUTH

setup:
  commands:
    - id: start-server
      command: npm run start -w @cortex/server
      env:
        PORT: "8020"
  health:
    - type: http
      url: http://127.0.0.1:8020/health
      timeout_seconds: 30
  reset:
    - type: tool
      name: test_reset_state
      optional: true

hosts:
  - id: codex-cli
    kind: codex-session
    config_ref: runners/codex-cortex-aut.json
    roles: [agent_under_test, judge]
  - id: claude-code
    kind: claude-code
    config_ref: runners/claude-process.example.json
    roles: [agent_under_test]
  - id: ghostlab-protocol
    kind: direct-mcp
    roles: [contract, deterministic]

capabilities:
  generated_from: runs/20260702-cortex-inspect/inspect.json
  tools:
    - name: views_generate_sentence_scramble
      category: ui_generation
      mutates_state: false
      produces_ui: true
      risk: low
  ui_resources:
    - uri: ui://sentence-scramble/v1.html
      hosts: [chatgpt, claude, mcpjam, ghostlab-browser]

test_plan:
  suites:
    - id: smoke
      goals: [startup, discovery, one_call_per_core_tool]
    - id: semantic
      goals: [happy_path, edge_case, error_recovery]
    - id: apps
      goals: [render, interact, bridge, responsive, visual_judge]
    - id: adversarial
      goals: [prompt_injection, hallucinated_tool, auth_boundary]
    - id: regression
      goals: [goldens, canaries, previous_failures]

review:
  gates:
    min_pass_rate: 0.9
    no_tool_schema_errors: true
    no_ui_console_errors: true
    no_high_security_findings: true
```

This artifact becomes the center of the system. All commands can still exist, but a new top-level flow should be:

```bash
ghostlab init --target targets/cortex-local.json --out ghostlab.yaml
ghostlab discover --spec ghostlab.yaml
ghostlab plan --spec ghostlab.yaml
ghostlab test --spec ghostlab.yaml --suite all --hosts codex-cli,claude-code
ghostlab review --spec ghostlab.yaml --latest
```

## Detailed Missing Parts

### 1. Setup and Environment Discovery

What exists:

- `TargetConfig` supports transport, connection, capabilities, startup.
- Runner configs can pass env.
- `doctor` validates local agent/runner setup.

What is missing:

- Automatic generation of setup requirements from repo/docs/user hints.
- Health checks, readiness checks, teardown, and state reset as first-class primitives.
- Secret declaration without leaking values.
- Fixture loading and test data isolation.
- Docker/compose/local process orchestration.
- Version fingerprinting: target build SHA, package version, MCP protocol version, host version, model version.

Implementation steps:

- Add `rehearsal/spec.py` with a typed `GhostlabSpec`.
- Add `ghostlab init` to convert existing target JSON into `ghostlab.yaml`.
- Add `ghostlab discover-setup` that inspects common files (`package.json`, `pyproject.toml`, Dockerfiles, README, `.mcp.json`) and proposes setup commands.
- Add `setup.commands`, `health`, `reset`, `teardown`, `fixtures`, and `secrets` to the spec.
- Store setup logs and fingerprints in every run.

### 2. Stronger MCP Contract Inspection

What exists:

- Basic list tools/resources/prompts.
- Basic missing-tool-reference lint.

What is missing:

- JSON Schema validation quality.
- Output schema discovery and validation where available.
- Tool result sampling.
- Resource fetch validation.
- Prompt template validation.
- Risk classification.
- Auth and permission behavior.
- Host metadata compatibility checks.

Implementation steps:

- Add `contract inspect` internals that produce `contract.json`.
- Validate every tool schema: missing descriptions, weak enum docs, ambiguous names, unsupported schema features by host, required fields without descriptions, nullable/optional ambiguity, large object risks.
- Add tool sampling modes:
  - `dry`: schema-only.
  - `example`: generated safe arguments.
  - `fixture`: user-provided arguments.
  - `destructive`: requires explicit approval.
- Add host compatibility lint:
  - standard `_meta.ui.resourceUri`
  - OpenAI alias `_meta["openai/outputTemplate"]`
  - `_meta.ui.visibility`
  - `_meta.ui.csp`
  - `_meta.ui.domain`
  - `structuredContent` matches output schema
  - component-only `_meta` does not contain data the model needs
- Add risk labels:
  - read-only
  - write/mutate
  - destructive
  - external network
  - credential-bearing
  - personal-data-bearing
  - UI-producing
  - model-hidden data

### 3. Test Plan Generation

What exists:

- Capability profile.
- Scenario/persona generation.
- Dataset review.

What is missing:

- A traceable test plan with coverage goals.
- Suite taxonomy.
- Host matrix.
- Scenario provenance.
- Objective goldens.
- UI/app flows.
- Security probes.

Implementation steps:

- Add `ghostlab plan`.
- Generate `test-plan.yaml` from `ghostlab.yaml` and `contract.json`.
- Include the following suites:
  - `smoke`: initialize, list tools/resources/prompts, call safe examples, render first UI widget.
  - `semantic`: user goals mapped to workflows.
  - `edge`: missing fields, ambiguous user requests, stale/empty state, large inputs, invalid enum values.
  - `error-recovery`: expected tool failures and model recovery.
  - `apps`: render, interact, bridge, responsive, accessibility, visual correctness.
  - `host-compat`: run same scenario across Codex/Claude/ChatGPT/direct protocol.
  - `security`: prompt injection, tool poisoning, cross-server exfiltration, hidden `_meta` leakage, OAuth/auth failures.
  - `regression`: known failures, golden cases, canaries.
- Assign each case a reason:
  - tool coverage
  - workflow coverage
  - risk coverage
  - UI coverage
  - host compatibility
  - previous failure

### 4. Host Adapter Layer

What exists:

- Mock runner.
- Process runner.
- Codex session runner.
- Parser for Codex JSONL tool calls.

What is missing:

- A host abstraction separate from raw process execution.
- Real adapters for Claude Code, ChatGPT developer mode, browser-based hosts, MCP Inspector/MCPJam, Cursor/VS Code/Goose where feasible.
- Host capability declarations.
- Host-specific setup/injection, approvals, and artifact capture.
- Host compatibility reports.

Implementation steps:

- Replace or wrap `RunnerConfig` with `HostAdapter`.
- Define adapter interface:

```python
class HostAdapter:
    id: str
    capabilities: HostCapabilities

    def prepare(self, spec, run_dir): ...
    def start_session(self, role, scenario, persona): ...
    def send_user_message(self, session, text): ...
    def observe_assistant(self, session): ...
    def capture_tool_events(self, session): ...
    def capture_ui_events(self, session): ...
    def close(self, session): ...
```

- Keep current process/codex runners as adapters.
- Add adapters in priority order:
  1. `direct-mcp`: deterministic protocol calls without a model.
  2. `codex-cli`: current Codex session runner, hardened.
  3. `claude-code`: process/session runner with MCP config injection.
  4. `browser-host`: Playwright-controlled host surfaces, starting with GhostLab’s own app host.
  5. `mcp-inspector` or `mcpjam`: launch and drive as manual/debug host where possible.
  6. `chatgpt-dev`: browser-driven or app-supported workflow if credentials and policies allow.
- Add host capability flags:
  - supports stdio/SSE/HTTP
  - supports MCP Apps
  - exposes tool trace
  - exposes UI screenshot
  - supports app bridge messages
  - supports approvals
  - supports session resume
  - supports file upload
  - supports OAuth

### 5. Visual and MCP Apps Testing

What exists:

- `apps-probe` and `apps-render`.
- Sandbox iframe renderer.
- Basic bridge transcript.
- Generic UI intents and assertions.

What is missing:

- Integration into normal scenario runs.
- Vision-based user emulator.
- Visual judge.
- Accessibility and responsive tests.
- Host-specific bridge compatibility.
- Model-visible vs component-hidden data checks.
- Rich UI action execution.

Implementation steps:

- Make UI events part of `run_scenario`.
- When a tool call result references a UI resource, automatically:
  - fetch resource
  - render widget
  - capture screenshot/DOM/accessibility tree
  - feed visual state to user emulator
  - execute emulator-chosen UI actions
  - capture final screenshot/state
  - feed evidence to the judge
- Replace `UiIntent` with richer `AppAction`:

```yaml
actions:
  - type: click
    target:
      role: button
      name: Check
  - type: fill
    target:
      role: textbox
      name: Answer
    value: "photosynthesis"
  - type: drag
    from_text: "cat"
    to_position: 2
  - type: screenshot_assert
    assertion: "The selected words appear in the answer row in the target order."
```

- Add evidence artifacts:
  - `widget-initial.png`
  - `widget-final.png`
  - `dom.json`
  - `accessibility-tree.json`
  - `bridge.jsonl`
  - `console.jsonl`
  - `network.jsonl`
  - `visual-judge.json`
- Add app assertions:
  - renders non-empty body
  - no console errors
  - bridge initialized
  - tool input delivered
  - tool result delivered
  - declared CSP sufficient
  - visible state matches tool result
  - UI action changed state
  - app can call server tool if allowed
  - app can send follow-up if allowed
  - mobile and desktop viewports render without overflow
  - app degrades without ChatGPT-specific extensions

### 6. User Emulator Upgrade

What exists:

- User emulator prompt creates text replies and can stop with `REHEARSAL_DONE`.

What is missing:

- Structured user state.
- Visual observation.
- Ability to act in UI.
- Ability to intentionally create ambiguity, corrections, impatience, or adversarial pressure.
- Deterministic modes for CI.

Implementation steps:

- Add `UserModel` output schema:

```json
{
  "message_to_assistant": "string",
  "ui_actions": [],
  "user_state": {
    "satisfaction": "low|medium|high",
    "goal_progress": "blocked|partial|complete",
    "new_information": {}
  },
  "stop": false,
  "reasoning_summary": "short"
}
```

- Provide observations:
  - conversation transcript
  - assistant response
  - tool trace summary
  - UI screenshot
  - visible text
  - accessibility tree
  - scenario goal and persona
- Add persona stressors:
  - vague
  - typo-prone
  - impatient
  - overtrusting
  - adversarial
  - accessibility needs
  - mobile-only
  - multilingual
- Add deterministic scripted user mode for smoke/CI.

### 7. Review and Repair Loop

What exists:

- `evaluate` writes verdicts.
- `critique` suggests tool ergonomics changes.
- `scorecard` aggregates results.

What is missing:

- Root-cause clustering.
- Readiness gates.
- Repair suggestions that are directly actionable.
- Issue/PR output.
- Spec update suggestions.
- Re-run recommendations.

Implementation steps:

- Add `ghostlab review`.
- Inputs:
  - `contract.json`
  - `test-plan.yaml`
  - all run artifacts
  - app render artifacts
  - host compatibility matrix
- Outputs:
  - `readiness.md`
  - `readiness.json`
  - `failures.md`
  - `recommended-patches.md`
  - optional GitHub issue markdown
- Classify failures:
  - setup
  - transport/protocol
  - schema
  - tool selection
  - tool arguments
  - tool runtime
  - missing read-back
  - state pollution
  - host incompatibility
  - UI render
  - UI interaction
  - auth
  - security
  - flaky/model variance
- Gate readiness:
  - smoke pass rate
  - semantic pass rate
  - no critical contract findings
  - no high security findings
  - no UI render blockers
  - minimum host compatibility score
  - no unresolved previous regressions

### 8. Security and Trust-Boundary Testing

What exists:

- Adversarial scenarios exist as an intent type.
- Hallucinated tools and failed calls are detected.

What is missing:

- Purpose-built MCP security tests.
- Tool poisoning checks.
- Prompt injection canaries.
- Cross-server data propagation tests.
- Hidden `_meta` leakage checks.
- Permission/approval checks.
- Destructive-action safeguards.

Implementation steps:

- Add `security` suite generator.
- Include probes:
  - tool description injection attempts
  - resource content injection
  - prompt template injection
  - user asks model to invent unavailable tools
  - user asks model to reveal hidden `_meta`
  - server returns malicious UI text
  - UI attempts unauthorized `tools/call`
  - cross-server canary propagation
  - auth expired/missing/insufficient scopes
  - destructive tool requires approval or dry-run
- Add canary infrastructure:
  - seed secrets
  - taint markers
  - expected non-propagation assertions
- Add `risk_acceptance` section to `ghostlab.yaml` for known/approved behaviors.

### 9. CI, Reproducibility, and Variance

What exists:

- Runs write event logs and reports.
- Dataset comparison exists.
- SQLite persistence exists.

What is missing:

- Repeat runs and statistical confidence.
- Host/model version tracking.
- Flake classification.
- CI-friendly deterministic mode.
- Artifact bundle standard.

Implementation steps:

- Add `ghostlab test --repeat N`.
- Add `run_bundle.json` with:
  - spec hash
  - target fingerprint
  - host versions
  - model names
  - seed
  - scenario IDs
  - fixture IDs
  - artifact paths
- Add `variance.json`:
  - pass rate by case
  - pass rate by host
  - tool trajectory variance
  - average turns/calls
  - flaky cases
- Add CI profiles:
  - `smoke`: deterministic/direct protocol plus one model run.
  - `nightly`: full host matrix.
  - `release`: repeat and regression gates.

## Two Implementation Plans

## Plan A: Incremental Evolution of the Current Codebase

This is the recommended plan. It preserves the existing code, keeps the public CLI compatible, and turns the current stages into a coherent product.

### Phase A1: Canonical Spec and Project Flow

Goal: make `ghostlab.yaml` the source of truth.

Deliverables:

- Add `rehearsal/spec.py`.
- Add `ghostlab init --target ...`.
- Add `ghostlab discover --spec ghostlab.yaml`.
- Convert `inspect` output into `contract.json`.
- Store all artifacts under `.ghostlab/` or configurable workspace dir.
- Keep existing target JSON support for compatibility.

Acceptance criteria:

- A user can point GhostLab at an existing target JSON and get a complete starter `ghostlab.yaml`.
- Existing `inspect`, `profile`, and `run` commands still work.
- `ghostlab discover --spec` produces inspect/profile/app-probe artifacts and updates spec references.

### Phase A2: Contract Lint and Setup Runtime

Goal: make setup, health, fixtures, and MCP contract checks reliable.

Deliverables:

- Setup command runner with logs, health checks, teardown, reset hooks.
- Schema lint suite.
- Tool metadata/risk classifier.
- UI metadata compatibility lint.
- Safe tool sampling with generated/example/fixture args.

Acceptance criteria:

- `ghostlab discover --spec` reports setup status, health status, schema findings, UI metadata findings, and risk map.
- Unsafe/destructive tools are never called without explicit approval.
- Contract findings appear in Markdown and JSON.

### Phase A3: Test Plan Generator

Goal: generate a coverage-driven plan rather than loose scenarios.

Deliverables:

- `ghostlab plan --spec`.
- `test-plan.yaml`.
- Suite taxonomy: smoke, semantic, edge, error-recovery, apps, host-compat, security, regression.
- Coverage map showing tools/workflows/resources/UI/security risks covered.
- Upgrade dataset review to plan review.

Acceptance criteria:

- Every generated case has a coverage reason.
- The plan highlights untested tools, workflows, UI widgets, and risks.
- Users can approve/reject/edit cases before running.

### Phase A4: Host Adapter Abstraction

Goal: make Codex, Claude Code, direct MCP, and browser hosts first-class.

Deliverables:

- `rehearsal/hosts/` package.
- Adapter interface and capability metadata.
- Port current `ProcessRunner` and `CodexSessionRunner` into adapters.
- Add direct protocol host.
- Add Claude Code adapter if local command semantics are available.
- Add host matrix runner.

Acceptance criteria:

- `ghostlab test --spec --hosts codex-cli,direct-mcp` runs the same plan across hosts.
- Reports show host-specific pass/fail, tool traces, setup logs, and model/host versions.

### Phase A5: Integrated MCP Apps Loop

Goal: make visual app testing part of normal runs.

Deliverables:

- Automatic widget render when a tool result produces UI.
- Screenshot/DOM/accessibility artifacts included in run events.
- Structured UI actions from user emulator.
- Visual judge over screenshots and DOM.
- Responsive viewport matrix.
- Host bridge compatibility checks.

Acceptance criteria:

- A scenario can say "complete this exercise in the widget" and GhostLab can render, act, and judge the widget.
- Reports include initial/final screenshots, bridge transcript, action log, console/network errors, and visual verdict.

### Phase A6: Review, Repair, and Readiness

Goal: close the loop from test results to implementation guidance.

Deliverables:

- `ghostlab review --spec --runs ...`.
- Root-cause clustering.
- Readiness score.
- Repair recommendations.
- GitHub issue markdown output.
- Re-run recommendations.

Acceptance criteria:

- A failed dataset produces a report that explains what to fix first.
- The report separates MCP contract issues, host/model behavior, user-emulation findings, UI issues, setup failures, and security risks.
- Reports are stable enough to use as release gates.

### Phase A7: CI and Variance

Goal: make GhostLab reliable for release workflows.

Deliverables:

- `ghostlab test --profile smoke|nightly|release`.
- Repeat runs and variance reports.
- Flake detection.
- GitHub Actions examples.
- Artifact bundle format.

Acceptance criteria:

- CI can run smoke tests without manual intervention.
- Release tests can fail on readiness gates.
- Re-running with the same spec and seed is diagnosable even when model behavior varies.

## Plan B: Re-Architecture Around a Test Runtime

This plan is more ambitious. It is appropriate if GhostLab should become a platform rather than a CLI-first tool.

### Architecture

Split GhostLab into five packages:

- `ghostlab-core`: spec schema, artifacts, contract model, test plan model, verdict model.
- `ghostlab-runtime`: setup runner, host adapters, orchestration engine, event bus.
- `ghostlab-hosts`: Codex, Claude Code, direct MCP, browser, ChatGPT, Inspector/MCPJam adapters.
- `ghostlab-apps`: MCP Apps renderer, bridge emulator, visual assertions, accessibility, responsive checks.
- `ghostlab-ui`: web dashboard, replay, plan editor, readiness reports.

Use an internal event model:

```yaml
events:
  - setup.started
  - setup.finished
  - mcp.initialize
  - mcp.tools.listed
  - host.session.started
  - user.message
  - assistant.message
  - tool.call.started
  - tool.call.finished
  - app.render.started
  - app.screenshot.captured
  - app.action.performed
  - judge.verdict
  - review.finding
```

### Runtime Model

Each test case becomes a state machine:

1. Prepare setup and fixtures.
2. Start target.
3. Start host session.
4. Send user message or scripted event.
5. Observe assistant/tool/UI events.
6. If UI appears, route through app runtime.
7. Ask user emulator for next text/UI action.
8. Stop on success, failure, max turns, or explicit done.
9. Judge.
10. Reset state.

### Advantages

- Cleaner long-term architecture.
- Easier host/plugin ecosystem.
- Better web UI and replay.
- Better multi-host and visual testing support.
- Stronger artifact schema.

### Costs

- Higher implementation risk.
- More migration work.
- Existing CLI commands need compatibility wrappers.
- More design decisions before users see value.

### Recommended Use

Use Plan B only if the project goal is to become a durable product/platform with plugin-host ecosystem, hosted dashboard, or team collaboration. For the current repo, Plan A gets the product much closer to the vision faster.

## Recommended Priority Order

1. Add `ghostlab.yaml` and `ghostlab init/discover/plan/test/review`.
2. Harden contract inspection and setup/runtime reproducibility.
3. Introduce host adapters, starting with current Codex and direct MCP.
4. Integrate MCP Apps rendering into normal runs.
5. Add visual user emulator and visual judge.
6. Add review/readiness reports.
7. Add security suites and variance/CI.

## Definition of Done for the Vision

GhostLab reaches the vision when a developer can run:

```bash
ghostlab init --target ./mcp.json
ghostlab discover
ghostlab plan
ghostlab test --hosts codex-cli,claude-code,ghostlab-browser --suite all
ghostlab review
```

And receive:

- A curated MCP/app spec.
- A runnable setup plan.
- A coverage-driven test plan.
- Text and visual scenarios.
- Multi-host execution traces.
- Tool-call and widget-interaction evidence.
- Pass/fail verdicts with deterministic and judged evidence.
- Security and host compatibility findings.
- A readiness score.
- Concrete repair recommendations.

At that point, GhostLab is no longer just an MCP test harness. It is a QA lab for the new MCP reality: model uncertainty, user uncertainty, tool contracts, and embedded app surfaces all tested together.

## Sources Consulted

- Local repository: `README.md`, `docs/cli.md`, `docs/concepts.md`, `rehearsal/cli.py`, `rehearsal/inspect.py`, `rehearsal/profile.py`, `rehearsal/generate.py`, `rehearsal/orchestrator.py`, `rehearsal/runners.py`, `rehearsal/evaluate.py`, `rehearsal/critique.py`, `rehearsal/mcp_apps.py`, `rehearsal/apps_host/*`, and related tests.
- [MCP Inspector](https://github.com/modelcontextprotocol/inspector)
- [MCP-UI](https://github.com/MCP-UI-Org/mcp-ui)
- [OpenAI Apps SDK: MCP Apps compatibility in ChatGPT](https://developers.openai.com/apps-sdk/mcp-apps-in-chatgpt)
- [OpenAI Apps SDK: Test your integration](https://developers.openai.com/apps-sdk/deploy/testing)
- [OpenAI Apps SDK reference](https://developers.openai.com/apps-sdk/reference)
- [MCP-Bench](https://github.com/Accenture/mcp-bench)
- [MCPEval](https://github.com/SalesforceAIResearch/MCPEval)
