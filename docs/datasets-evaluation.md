# Datasets and Evaluation

## Generate A Dataset

```bash
ghostlab generate-dataset \
  --profile runs/<id>-inspect/capabilities.json \
  --personas 3 \
  --scenarios-per-persona 3 \
  --seed 7 \
  --name cortex
```

This writes a self-contained directory:

```text
datasets/cortex/
  dataset.json
  personas/<id>.json
  scenarios/<id>.json
```

The manifest records cases, seed, MCP identity, and status. The seed governs case ordering for reproducibility.

## Curate Before Running

```bash
ghostlab review-dataset \
  --dataset datasets/cortex \
  --profile runs/<id>-inspect/capabilities.json
```

The review command writes `review.md` and `review.json` with coverage, previews, and flags. You can edit `dataset.json` directly or use `--approve`, `--reject`, and `--needs-edit`.

## Evaluate Runs

```bash
ghostlab evaluate --run runs/<id> --capabilities runs/<id>-inspect/capabilities.json
```

Evaluation combines deterministic checks with a Codex LLM judge:

- Failed tool calls.
- Expected-tool coverage from `exercises`.
- Success criteria met or unmet.
- Failure signals triggered or avoided.
- Claimed tools not exposed by the server, when capabilities are supplied.

The command writes `verdict.json` and `verdict.md`.

## Evaluate A Dataset

```bash
ghostlab run-dataset --dataset datasets/cortex \
  --target targets/cortex-local.json \
  --aut-runner runners/codex-cortex-local-session.json \
  --evaluate --capabilities runs/<id>-inspect/capabilities.json
```

Per-case verdicts are written into each run directory and aggregated into the dataset summary.

## Compare Dataset Runs

```bash
ghostlab compare --base runs/<base>-summary --candidate runs/<candidate>-summary \
  --output comparison.md
```

Comparison reports regressions first, then fixes, then other verdict or status changes. It exits non-zero when regressions are found so it can gate CI.
