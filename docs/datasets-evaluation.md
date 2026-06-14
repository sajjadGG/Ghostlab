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
- Tool efficiency: total calls, unique tools, redundant calls (same tool with
  identical arguments), and per-call latency when the capture provides it.
- Expected-tool coverage from `exercises`.
- Success criteria met or unmet.
- Failure signals triggered or avoided.
- Claimed tools not exposed by the server, when capabilities are supplied.
- Golden assertions from a scenario's optional `expected_outcome`.

The command writes `verdict.json` and `verdict.md`.

### Golden assertions

For scenarios with a known-correct answer, add an `expected_outcome` block to the
scenario JSON for objective, judge-independent grading. A mismatch is a hard gate
that forces an overall `fail`:

```json
"expected_outcome": {
  "must_include": ["band score", "7.5"],
  "must_not_include": ["error"],
  "expected_tool_args": [
    { "tool": "student_get_status", "arguments": { "id": "u1" } }
  ]
}
```

`must_include` / `must_not_include` are case-insensitive substrings checked
against the final assistant turn. Each `expected_tool_args` entry passes when the
run contains a call to that tool whose arguments include the given key/value pairs
(a subset match). The LLM judge still scores the open-ended `success_criteria`.

## Evaluate A Dataset

```bash
ghostlab run-dataset --dataset datasets/cortex \
  --target targets/cortex-local.json \
  --aut-runner runners/codex-cortex-local-session.json \
  --evaluate --capabilities runs/<id>-inspect/capabilities.json
```

Per-case verdicts are written into each run directory and aggregated into the dataset summary.

## Scorecard A Dataset Run

Roll a whole dataset run up into one MCP validation report:

```bash
ghostlab scorecard --results runs/<id>-summary
```

It reads each case's run directory (verdict, critique, and tool calls when
present) and aggregates server-level signals — pass rate, average tool coverage,
average tool-ergonomics score, per-tool failure rates, hallucinated-tool and
golden-mismatch counts, efficiency, and recurring tool-design recommendations —
into `scorecard.json` and `scorecard.md`. Run `evaluate` and `critique` on the
cases first for the richest report.

## Compare Dataset Runs

```bash
ghostlab compare --base runs/<base>-summary --candidate runs/<candidate>-summary \
  --output comparison.md
```

Comparison reports regressions first, then fixes, then other verdict or status changes. It exits non-zero when regressions are found so it can gate CI.
