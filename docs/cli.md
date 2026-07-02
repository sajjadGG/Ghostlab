# CLI Reference

Ghostlab installs two equivalent console scripts, `ghostlab` and `rehearsal`. New examples use `ghostlab`.

## init

Create a `ghostlab.yaml` spec — the canonical, human-editable description of the
MCP under test — from an existing target JSON. The spec is the entry point for
the project flow (`init` → `discover` → future `plan`/`test`/`review` stages);
every other command keeps accepting raw target/scenario JSON as before.

```bash
ghostlab init --target targets/cortex-local.json          # writes ghostlab.yaml
ghostlab init --target targets/cortex-local.json --out cortex.ghostlab.json
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

## plan

Generate a coverage-driven `test-plan.yaml` from the latest discover
artifacts. Unlike `generate-scenarios` (model-imagined stories), the plan is
deterministic: every case exists for a stated `reason` (tool coverage,
workflow coverage, UI coverage, risk coverage, or a specific finding), so the
plan doubles as a coverage report and lists untested tools/widgets as gaps.

```bash
ghostlab plan --spec ghostlab.yaml            # generate / regenerate
ghostlab plan --spec ghostlab.yaml --approve  # curate: approve all cases
ghostlab plan --spec ghostlab.yaml --reject security-resource-injection
```

Suites follow the roadmap taxonomy: `smoke` (protocol discovery + one minimal
call per read-only tool + first-widget render — executable without a model),
`semantic` (one conversational seed per tool family, marked
`needs_generation` for scenario generation), `edge` (missing-required and
invalid-enum probes derived from schemas), `error-recovery` (seeded from
sampling failures), `apps` (render + interact per `ui://` resource),
`security` (hallucinated-tool, destructive-approval, credential-handling, and
resource-injection probes derived from contract risk labels), `host-compat`
(smoke slice per configured host when the spec declares several), and
`regression` (reserved for run-history failures).

Case ids are deterministic, so `status` curation (`proposed` / `approved` /
`rejected`) survives regeneration after a re-discover. A `test-plan.md`
companion and the spec's `test_plan` summary are refreshed on every run.

## test

Execute the test plan across the spec's host adapters and write a results
bundle under `<workspace>/test/<timestamp>-<id>/` (`results.json` +
`results.md`, host/version fingerprints included).

```bash
ghostlab test --spec ghostlab.yaml
ghostlab test --spec ghostlab.yaml --suite smoke --suite edge   # CI-able subset
ghostlab test --spec ghostlab.yaml --hosts direct-mcp --approved-only --strict
```

Every case runs on each capable host (one result per case × host). The
built-in `direct-mcp` host executes protocol cases deterministically — no
model, no variance: `discovery` must list tools, `tool_call` with
`expect.no_error` must succeed without `isError`, and `expect.graceful_error`
passes only when the server rejects bad input *in protocol* (JSON-RPC error or
`isError: true`) rather than crashing. `app_render` cases render through the
Playwright apps host when `ghostlab[apps]` is installed (skip with a reason
otherwise). Runner-backed hosts (`codex-session`, `process` kinds in
`hosts`) execute conversational cases that carry a concrete
`execution.scenario`; generation seeds skip with instructions. Cases no host
can execute surface as explicit skips, never silence.

The spec's `setup` section runs before and tears down after, exactly as in
`discover`. With `--strict`, `review.gates.min_pass_rate` fails the run when
the executed pass rate drops below it.

## inspect

Introspect a target MCP server.

```bash
ghostlab inspect --target targets/cortex-local.json
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

Run one scenario.

```bash
ghostlab run \
  --target targets/example-stdio.json \
  --scenario scenarios/basic-discovery.json \
  --aut-runner runners/mock-aut.json \
  --user-runner runners/mock-user.json
```

## run-dataset

Run every case in a dataset. Use `--limit` for small development runs and `--approved-only` to skip unreviewed cases.

```bash
ghostlab run-dataset \
  --dataset datasets/cortex \
  --target targets/cortex-local.json \
  --aut-runner runners/codex-cortex-aut.json \
  --user-runner runners/codex-user-emulator.json \
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

Aggregate a whole dataset run into one MCP validation report (pass rate, per-tool
reliability, hallucination/golden-mismatch counts, efficiency, and recurring
tool-design recommendations). No model calls — it reads the per-case artifacts.

```bash
ghostlab scorecard --results runs/<id>-summary
```

Writes `scorecard.json` and `scorecard.md` into the summary directory.

## doctor

Validate local agent and runner setup.

```bash
ghostlab doctor
ghostlab doctor --runners runners/codex-cortex-local-session.json
```
