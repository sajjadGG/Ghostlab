# CLI Reference

Ghostlab installs two equivalent console scripts, `ghostlab` and `rehearsal`. New examples use `ghostlab`.

## create

Create an end-to-end evaluation job for a composed agent, an MCP target, or a
local skill:

```bash
ghostlab create --name api-eval --target https://example.com/mcp --yes
ghostlab create --name notes-skill --skill ./skills/notes --yes
ghostlab create --name full-agent --agent ./agent.yaml --yes
ghostlab create --name full-agent --agent ./agent.yaml \
  --image base --provider openai --model gpt-5.2-codex \
  --user-model gpt-5.2-codex --generation-model gpt-5.2-codex \
  --judge-model gpt-5.2-codex --yes
ghostlab create --name copilot-agent --agent ./copilot-agent.json \
  --aut-backend copilot --user-backend copilot \
  --aut-agent release-reviewer --user-agent realistic-user \
  --model gpt-5.4 --user-model gpt-5-mini --sandbox local --yes
```

`--agent`, `--skill`, and `--target` are mutually exclusive. `--agent` accepts
JSON/YAML combining a runner, MCPs, skills, workspace, and assets. `--skill`
accepts a `SKILL.md` file or directory. Skill jobs use conversational semantic/security evaluation and omit
MCP-only protocol and Apps cases.

Generated jobs use `sandbox.backend: openshell`. `--sandbox local` is the only
CLI opt-out; there is no `--local` shorthand and no automatic fallback.
Use repeatable `--provider NAME` flags and `--image IMAGE` to configure
OpenShell without editing the generated job.

Without `--yes`, the Questionary/Rich creator asks for the subject, sandbox,
OpenShell image/providers, separate AUT and user runner backends,
AUT/user/generation/judge models, runner lifecycle and timeout, custom agent
names plus Copilot reasoning/context settings, Codex policy, scenario size, and
release gate. Arrow-key choices and a checkbox suite picker replace ambiguous
text prompts. It previews the effective configuration and prints stable `[1/5]`
through `[5/5]` progress. `--yes` keeps the non-interactive automation path.

### GitHub Copilot and VS Code custom agents

Use `--aut-backend copilot` and/or `--user-backend copilot` to drive a role
with GitHub Copilot CLI. Copilot custom agents are shared with VS Code:
`--aut-agent release-reviewer` selects the
`.github/agents/release-reviewer.agent.md` definition that VS Code shows as
`release-reviewer`.

Ghostlab does not automate the VS Code window or extension UI. It uses the
supported headless Copilot CLI session API, which is reproducible in CI while
still exercising the same custom-agent definition.

```bash
ghostlab create --name release-eval --target https://example.com/mcp \
  --aut-backend copilot --user-backend copilot \
  --model gpt-5.4 --user-model gpt-5-mini \
  --aut-agent release-reviewer --user-agent realistic-user \
  --aut-reasoning-effort high --aut-context long_context \
  --copilot-bin copilot \
  --aut-copilot-arg=--no-custom-instructions \
  --aut-runner-env COPILOT_GITHUB_TOKEN='$COPILOT_GITHUB_TOKEN' \
  --sandbox local --no-discover --yes
```

The creator writes two independent files:
`jobs/<name>/runners/aut.json` includes the target MCP, while
`jobs/<name>/runners/user.json` excludes it and explicitly disables the target
server name. `copilot-session` reuses one `--session-id` across turns and the
`copilot-json` parser captures the final message, MCP arguments/results/errors,
and built-in tool calls.

Quote `$NAME` environment references as shown above. The placeholder is stored
in the job instead of the secret and expanded only when the runner starts.
For OpenShell jobs, explicitly configured runner environment names are also
added to the sandbox environment allowlist.

The generated `agent.runtime` and `test.user_runtime` mappings are the complete
declarative configuration. Supported keys include:

- identity and model: `copilot_bin`, `model`, `agent`, `reasoning_effort`,
  `context`, `mode`, `kind`, `working_directory`, and `timeout_seconds`;
- permissions: `allow_all`, `allow_all_tools`, `allow_all_paths`,
  `allow_all_urls`, `allow_tools`, `deny_tools`, `available_tools`,
  `excluded_tools`, `allow_urls`, `deny_urls`, `add_dirs`, and
  `disallow_temp_dir`;
- MCP/plugins: `disable_builtin_mcps`, `disable_mcp_servers`,
  `additional_mcp_configs`, `allow_all_mcp_server_instructions`,
  `add_github_mcp_tools`, `add_github_mcp_toolsets`,
  `enable_all_github_mcp_tools`, and `plugin_dirs`;
- session/process: `no_custom_instructions`, `no_ask_user`, `enable_memory`,
  `enable_reasoning_summaries`, `max_ai_credits`,
  `max_autopilot_continues`, `secret_env_vars`, `bash_env`, `env`, and
  `extra_args`.

`extra_args` is the forward-compatible escape hatch for new Copilot CLI
options. Ghostlab rejects only protocol-owned flags (`--prompt`,
`--session-id`, `--output-format`, and `--stream`) because overriding those
would break turn delivery or JSONL capture. For a fully hand-written command,
use `--aut-runner` and `--user-runner`.

The full creator requires a real semantic result. It exits non-zero instead of
claiming completion when generation only produced inert placeholders or every
semantic/security case skipped. The job and diagnostic plan remain on disk so
you can fix credentials/providers or runner settings and resume.

`ghostlab create --name NAME --resume` continues an existing job without asking
for the target again. It reuses completed discovery and cached generation
artifacts, then resumes per-case testing where possible.

## config

Show every effective agent setting, including the exact AUT and user runner
commands and their sources, backend/model/custom-agent selection,
AUT/user/generation/judge models, runner lifecycle, timeout, parser, policy,
composed MCPs/skills/assets, and OpenShell providers/uploads/policy:

```bash
ghostlab config --job my-agent
ghostlab config --job my-agent --json
```

The readable form is syntax-colored; `--json` is stable machine-readable
output. When `-m` is omitted, Ghostlab reads only the top-level `model` from
Codex's `config.toml` and reports that inherited model plus its source instead
of the ambiguous label “CLI default.” The dashboard Overview exposes the same
resolved view. Its Configure tab edits Codex settings directly and provides
full AUT/user runtime JSON editors for Copilot before rematerializing both
runner files.

## init

Create a `ghostlab.yaml` spec from an existing target JSON. This is the advanced
single-file entry point for the implemented `init` → `discover` → `plan` →
`test` → `review` flow; most users should use `ghostlab create` and a job.

```bash
ghostlab init --target target.json          # writes ghostlab.yaml
ghostlab init --target target.json --out cortex.ghostlab.json
```

Options: `--name` (display name), `--workspace` (artifact directory, default
`.ghostlab/` next to the spec), `--force` (overwrite an existing spec). Specs
can be YAML or JSON by extension; the built-in YAML reader covers everything
ghostlab emits, and an installed PyYAML is picked up automatically for full
YAML syntax in hand-edited specs.

## discover

Connect to the spec's target, capture the inventory (`inspect.json`), lint the
contract into `contract.json`/`contract.md`, probe `ui://` widget resources
when the server exposes MCP Apps tools, and refresh the spec's `capabilities`
section (tool risk labels, UI resources, artifact provenance). Artifacts land
under `<workspace>/discover/<timestamp>-<id>/`.

```bash
ghostlab discover --spec ghostlab.yaml
ghostlab discover --spec ghostlab.yaml --strict          # exit 1 when review gates fail
ghostlab discover --spec ghostlab.yaml --sample safe     # also call read-only tools once
ghostlab discover --spec ghostlab.yaml --sandbox local   # trusted legacy opt-out
```

If the spec declares a `setup` section, discover executes it first and tears it
down afterwards: `setup.commands` run in order (`background: true` for the
server process itself — it is terminated at teardown), `setup.health` probes
(`http`, `tcp`, or `command`) are polled until they pass or time out, and
`setup.teardown` always runs. Logs land in `setup.log`, and `setup.json`
records per-step status plus a version fingerprint (ghostlab/python/platform/
server versions). `--skip-setup` bypasses all of it when the target is already
running.

`--sample` calls tools for real, under an explicit safety model:

- `safe` — only tools classified **read-only** (MCP `annotations` first,
  heuristics second), with arguments generated from each required parameter's
  schema (`default` → `examples` → `enum` head → type zero-value). Tools whose
  arguments can't be generated are skipped with a reason, never guessed.
- `fixture` — additionally calls tools listed in `setup.fixtures`
  (`- {tool: name, arguments: {...}}`) with those arguments. A mutating
  fixture still requires `--approve-mutations`; a destructive one requires
  `--approve-destructive`. After mutating samples, `setup.reset` hooks
  (`tool` or `command`) restore state.

Sample outcomes are written to `samples.json` and folded into the contract as
findings (failed calls, `isError` results, declared `outputSchema` without
`structuredContent`, UI tools that return nothing model-visible).

Contract findings are deterministic (no model calls): schema quality
(undocumented/untyped params, `required` names that don't exist, `$ref`-heavy
schemas hosts translate poorly), risk classification (read-only vs mutating vs
destructive, credential-bearing params, UI-producing — MCP tool `annotations`
take precedence over name heuristics), and MCP Apps metadata compatibility
(standard `_meta.ui.resourceUri` vs the `openai/outputTemplate` alias, dangling
`ui://` references). With `--strict`, the spec's `review.gates` get teeth:
`no_tool_schema_errors: true` fails the run when any error-severity finding
exists.

For local stdio MCPs, discovery launches the persistent MCP process through
`openshell sandbox exec`. Declared uploads are staged before startup, environment
variables are allowlisted, and `openshell-*.log` is retained with artifacts.

## plan

Generate a coverage-driven `test-plan.yaml` from the latest discover
artifacts. Deterministic suites (smoke/edge/apps/security-from-contract) exist
for a stated `reason` (tool coverage, workflow coverage, UI coverage, risk
coverage), so the plan doubles as a coverage report and lists untested
tools/widgets as gaps. The semantic/security suites also get **real,
persona-grounded conversational scenarios**, generated by the same engine
`ghostlab generate-dataset` uses: `ghostlab plan` infers a capability profile,
proposes personas relevant to the MCP's domain, and generates goal-oriented
scenarios per persona — for a language-learning MCP that's things like "a
beginner French learner asks for a writing exercise" or "an impatient user
pushes for placement testing before onboarding is done".

```bash
ghostlab plan --spec ghostlab.yaml                     # generate/regenerate (personas on by default)
ghostlab plan --spec ghostlab.yaml --no-generate       # fast, free, deterministic-only plan
ghostlab plan --spec ghostlab.yaml --personas 3 --scenarios-per-persona 3
ghostlab plan --spec ghostlab.yaml --regenerate        # force fresh personas/scenarios
ghostlab plan --spec ghostlab.yaml --require-semantic  # fail on placeholder-only plans
ghostlab plan --spec ghostlab.yaml --approve           # curate: approve all cases
ghostlab plan --spec ghostlab.yaml --reject security-resource-injection
```

Each persona and scenario is a real codex call, so generation defaults to a
small size (2 personas × 2 scenarios) and is **cached**: a `plan` re-run
reuses the previously generated dataset (tracked in the spec's
`test_plan.generated_dataset`) instead of calling codex again — pass
`--regenerate` to refresh it, or `--no-generate` to skip generation entirely.
Generated scenario `intent` routes the case: `happy_path`/`edge_case` land in
`semantic`, `adversarial` lands in `security` (a persona pushing on a risk).
Personas/scenarios are written under `<workspace>/generated/<timestamp>-<id>/`
alongside a `profile.json` domain summary.

Suites follow the roadmap taxonomy: `smoke` (protocol discovery + one minimal
call per read-only tool + first-widget render — executable without a model),
`semantic` (real dual-agent scenarios once generated, otherwise inert
per-tool-family seeds), `edge` (missing-required and invalid-enum probes
derived from schemas), `error-recovery` (seeded from sampling failures),
`apps` (render + interact per `ui://` resource), `security` (contract-driven
hallucinated-tool/destructive/credential/injection probes, plus generated
adversarial-persona scenarios), `host-compat` (smoke slice per configured host
when the spec declares several), and `regression` (reserved for run-history
failures).

Case ids are deterministic, so `status` curation (`proposed` / `approved` /
`rejected`) survives regeneration after a re-discover. A `test-plan.md`
companion and the spec's `test_plan` summary are refreshed on every run.

## test

Execute the test plan across the spec's host adapters and write a results
bundle under `<workspace>/test/<timestamp>-<id>/` (`results.json` +
`results.md`, host/version fingerprints included). **Progress prints live**,
per case, as it runs — this matters most for conversational cases, which are
a real multi-turn LLM conversation and can take real wall-clock time.

```bash
ghostlab test --spec ghostlab.yaml
ghostlab test --spec ghostlab.yaml --suite smoke --suite edge   # CI-able subset
ghostlab test --spec ghostlab.yaml --hosts direct-mcp --approved-only --strict
ghostlab test --spec ghostlab.yaml --no-judge                   # skip the codex judge/critique
ghostlab test --spec ghostlab.yaml --resume                     # resume latest partial run
ghostlab test --spec ghostlab.yaml --require-semantic            # require a real conversation
ghostlab test --spec ghostlab.yaml --sandbox local               # explicit unsandboxed opt-out
```

Every case runs on each capable host (one result per case × host). The
built-in `direct-mcp` host executes protocol cases deterministically — no
model, no variance: `discovery` must list tools, `tool_call` with
`expect.no_error` must succeed without `isError`, and `expect.graceful_error`
passes only when the server rejects bad input *in protocol* (JSON-RPC error or
`isError: true`) rather than crashing. `app_render` cases render through the
Playwright apps host when `ghostlab[apps]` is installed (skip with a reason
otherwise).

Runner-backed hosts (`codex-session`/`process` kinds in `hosts`) execute
conversational cases that carry a concrete `execution.scenario` — this is the
actual dual-agent role-play: a user-emulator session (driven by the case's
generated persona and goal) and an agent-under-test session with the target
MCP attached go back and forth turn by turn, and every turn — user message,
assistant reply, tool calls — prints live. Once the conversation ends,
`--judge` (on by default) scores it with the codex judge: **pass/fail is the
judge's verdict, not just "did the conversation finish"** — a session can
complete without the user's goal actually being met. A tool-usability
critique also runs and its `critique.json` feeds `ghostlab review`'s
aggregated MCP feedback. `--no-judge` skips both and falls back to
finished-or-not. Seeds still marked `needs_generation` (no scenario attached
yet) skip with instructions to run `ghostlab plan --generate`.

Cases no host can execute surface as explicit skips, never silence.
With `--require-semantic`, skips, harness errors, and placeholder cases do not
count as execution and the command exits non-zero unless a conversation trace
was produced.

The AUT and user-emulator runner sessions use the job's sandbox configuration.
OpenShell setup/runtime/policy failures are classified as retryable harness
errors rather than target failures.

Each case result is checkpointed while the run is active. `--resume` reuses the
latest matching run directory, skips completed case/host pairs, and retries
unfinished or `harness_error` cases. Backend quota, timeout, authentication,
and judge outages are reported as `harness_error` and excluded from the target
pass rate; `--resume` currently requires `--repeat 1`.

Failed tool calls record a specific cause (`client_timeout`,
`permission_denied`, `client_cancelled`, `backend_cancelled`, or
`server_stream_error`) in `events.jsonl` and `report.md`. In particular, a
closed event stream with `INTERNAL_ERROR` is a server-stream failure, not a
human cancellation.

The spec's `setup` section runs before and tears down after, exactly as in
`discover`. With `--strict`, `review.gates.min_pass_rate` fails the run when
the executed pass rate drops below it.

`--repeat N` runs the plan N times and writes `variance.json`: per-case status
distribution across attempts, with **flaky** cases (passed some attempts,
failed others) called out separately from broken ones — the difference matters
once model-backed hosts join the matrix. `--profile` bundles CI presets:
`smoke` (smoke+edge suites), `nightly` (all suites), `release` (all suites,
`--repeat 3`, strict gates). Explicit flags override the preset.

A minimal GitHub Actions job:

```yaml
- name: MCP smoke tests
  run: |
    pip install ghostlab
    ghostlab discover --spec ghostlab.yaml --strict
    ghostlab test --spec ghostlab.yaml --profile smoke --strict
    ghostlab review --spec ghostlab.yaml --strict
```

## review

Readiness report over everything the pipeline produced — the release-gate
answer to "is this MCP ready, and if not, what do I fix first?".

```bash
ghostlab review --spec ghostlab.yaml            # uses the latest test results
ghostlab review --spec ghostlab.yaml --strict   # exit 1 unless verdict is 'ready'
```

Writes `readiness.json` / `readiness.md` next to the test results:

- **Gates** — the spec's `review.gates` evaluated against evidence
  (`min_pass_rate` vs executed results, `no_tool_schema_errors` vs contract
  findings, `no_ui_console_errors` vs apps cases, `no_high_security_findings`
  vs security cases), each pass / fail / not-evaluated with a reason.
- **Failure clusters** — failed cases grouped by category (ui-render,
  input-validation, tool-runtime, transport-protocol, host-compatibility,
  security) and detail signature, so repeats of one root cause read as one
  problem.
- **Repairs** — prioritized, concrete recommendations mapped from finding
  kinds (P1 "fix `inputSchema.required`" before P4 "add param descriptions"),
  with the tools they apply to.
- **MCP feedback** — every conversational case's tool-usability critique
  (`ghostlab test`'s judge pass), rolled up: average tool-ergonomics score,
  deduplicated top recommendations across all runs, and a per-tool table
  (name clarity, suggestions). This is literally "ask the agent that used the
  tool how it felt, and aggregate the answers."
- **Verdict** — `not-ready` (a gate failed), `needs-work` (failures, error
  findings, coverage gaps, or planned suites nothing executed), or `ready`.

## inspect

Introspect a target MCP server.

`inspect` is a low-level direct protocol command. It does not load a job's
sandbox/uploads. For an untrusted local stdio server, create a job and run
`ghostlab discover`, which defaults to OpenShell. Remote HTTP/SSE inspection
does not launch target code locally.

```bash
ghostlab inspect --target examples/target.json
```

## profile

Create a capability profile from an `inspect.json`.

```bash
ghostlab profile --inspect runs/<id>-inspect/inspect.json
```

## generate-scenarios

Generate grounded scenarios from a capability profile.

```bash
ghostlab generate-scenarios \
  --profile runs/<id>-inspect/capabilities.json \
  --n 3 \
  --output-dir scenarios
```

## generate-personas

Generate reusable domain personas from a capability profile.

```bash
ghostlab generate-personas \
  --profile runs/<id>-inspect/capabilities.json \
  --n 4 \
  --output-dir personas
```

## generate-dataset

Generate a persona x scenario dataset.

```bash
ghostlab generate-dataset \
  --profile runs/<id>-inspect/capabilities.json \
  --personas 3 \
  --scenarios-per-persona 3 \
  --seed 7 \
  --name cortex
```

## review-dataset

Review, flag, approve, or reject dataset cases before spending agent credits.

```bash
ghostlab review-dataset \
  --dataset datasets/cortex \
  --profile runs/<id>-inspect/capabilities.json
```

```bash
ghostlab review-dataset --dataset datasets/cortex \
  --approve case-a case-b --reject case-c
```

## run

Run one scenario. Real runner sessions use OpenShell by default; mock runners
remain in-process deterministic fixtures and do not exercise a sandbox.

```bash
ghostlab run \
  --target examples/target.json \
  --scenario examples/scenario.json \
  --aut-runner runners/mock-aut.json \
  --user-runner runners/mock-user.json
```

For trusted direct-host execution:

```bash
ghostlab run --target target.json --scenario scenario.json \
  --aut-runner aut.json --user-runner user.json --sandbox local
```

## run-dataset

Run every case in a dataset. AUT, user-emulator, and optional judge execution
use OpenShell by default. Use `--limit` for small development runs,
`--approved-only` to skip unreviewed cases, or `--sandbox local` for an
explicit trusted-host opt-out. Attach configured credential/egress providers to
runner sessions and the optional judge with repeatable `--provider <name>`
flags; without the flag, each runner file's own sandbox providers are preserved.

```bash
ghostlab run-dataset \
  --dataset datasets/cortex \
  --target target.json \
  --aut-runner runners/codex-cortex-aut.json \
  --user-runner runners/codex-user-emulator.json \
  --provider openai \
  --limit 2
```

## evaluate

Score a completed run into a pass, partial, or fail verdict.

```bash
ghostlab evaluate --run runs/<id> --capabilities runs/<id>-inspect/capabilities.json
```

## critique

Critique the MCP server's tool usability from a completed run. Where `evaluate`
asks "did the scenario pass?", `critique` asks "how do I improve this MCP?": it
grades the naming, descriptions, parameter clarity, and error quality of the
tools the agent actually exercised, with concrete suggestions. Pass `--inspect`
so the judge can see the real tool definitions.

```bash
ghostlab critique --run runs/<id> --inspect runs/<id>-inspect/inspect.json
```

Writes `critique.json` and `critique.md` into the run directory.

## compare

Diff two dataset result sets for regressions.

```bash
ghostlab compare --base runs/<base>-summary --candidate runs/<candidate>-summary \
  --output comparison.md
```

## scorecard

Aggregate a whole dataset run into one agent/capability validation report (pass
rate, per-tool reliability, hallucination/golden-mismatch counts, efficiency,
and recurring tool-design recommendations). No model calls—it reads the
per-case artifacts.

```bash
ghostlab scorecard --results runs/<id>-summary
```

Writes `scorecard.json` and `scorecard.md` into the summary directory.

## doctor

Validate the agent, runner, and OpenShell setup. The default check resolves the
OpenShell CLI and connects to its gateway; `--sandbox local` deliberately skips
that runtime check. Both LLM backends are reported, with the selected one
marked.

```bash
ghostlab doctor
ghostlab doctor --probe
ghostlab doctor --runners runners/codex-cortex-local-session.json
ghostlab doctor --sandbox local
```

Without `--probe`, a backend is reported as *installed, not verified* — the
honest limit of a `--version` check. `--probe` sends one tiny live generation
request to each backend, which is what catches an exhausted quota or a CLI too
old for the model your account is pinned to. When the selected backend fails
but another works, doctor names the flag to switch:

```
[!!] codex (selected): ... The 'gpt-5.6-sol' model requires a newer version of Codex.
[ok] opencode: /Users/me/.opencode/bin/opencode (1.4.3) — answered a live generation probe
note: the selected backend 'codex' is unusable; rerun with --llm-backend opencode
```

## apps-probe

Fetch MCP Apps `ui://` resources and report UI-tool metadata and CSP issues:

```bash
ghostlab apps-probe --target target.json
ghostlab apps-probe --target target.json --tool calendar_create_event
```

Like standalone `inspect`, this is a direct protocol utility. Prefer the job
pipeline for an untrusted local stdio MCP.

## apps-render

Render an MCP Apps widget in headless Chrome, optionally call its tool, and
execute one or more JSON UI intents:

```bash
pip install 'ghostlab[apps]'
playwright install chrome
ghostlab apps-render --target target.json --tool calendar_create_event \
  --arguments '{"title":"Demo"}' \
  --intent '{"type":"submit"}'
```

Artifacts include JSON/Markdown diagnostics and initial/final screenshots.
Use `--no-call` to render from tool input without invoking the MCP tool.

## dashboard

Build a standalone HTML dashboard from a `ghostlab test` result directory:

```bash
ghostlab dashboard jobs/my-agent/workspace/test/<run-id>
ghostlab dashboard jobs/my-agent/workspace/test/<run-id> --open
```

The self-contained report includes evaluation health, pass/fail/tool-call and
conversation metrics, status/suite filters, full-text case search, judge
evidence, transcripts, tool payloads, and widget interactions. It works offline
and adapts to light/dark mode and mobile widths.

## ui

Launch the optional Streamlit interface over the same job artifacts:

```bash
pip install 'ghostlab[ui]'
ghostlab ui --port 8501 --server-address localhost
```

The UI can create agent-, MCP-, and skill-based jobs, configure OpenShell and
generation defaults, show pipeline completion, stream long-running command
output, curate cases, filter results, inspect traces, and export the standalone
HTML dashboard.

## db

Initialize or verify the optional SQLite system of record:

```bash
ghostlab db init --db ghostlab.sqlite3
ghostlab db verify --db ghostlab.sqlite3
```
