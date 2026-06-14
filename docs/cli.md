# CLI Reference

Ghostlab installs two equivalent console scripts, `ghostlab` and `rehearsal`. New examples use `ghostlab`.

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

## doctor

Validate local agent and runner setup.

```bash
ghostlab doctor
ghostlab doctor --runners runners/codex-cortex-local-session.json
```
