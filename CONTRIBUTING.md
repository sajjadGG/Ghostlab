# Contributing to Ghostlab

Thanks for helping improve Ghostlab! This guide is written to be friendly to both
humans and coding agents. For a machine-readable map of the project, see
[`llms.txt`](llms.txt).

## Project shape

- Python package: `rehearsal` · installed CLI: `ghostlab` (alias `rehearsal`).
- Pipeline: **understand → generate → run → evaluate**, plus optional SQLite
  persistence, a Streamlit UI, and an MCP Apps render layer.
- The MCP client uses only the standard library; coding-agent CLIs (Codex /
  Claude) are the agent backends.

## Setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'      # add ',ui' and/or ',apps' for those features
.venv/bin/pytest                       # run the test suite
```

Optional extras:

- `.[ui]` — the Streamlit pipeline UI (`ghostlab ui`).
- `.[apps]` — Playwright, for rendering MCP Apps `ui://` widgets
  (`ghostlab apps-render`). After install: `playwright install chrome`.

## Conventions

- **Python 3.9-syntax-safe.** CI targets 3.10–3.13, but contributors run 3.9
  locally — use `from __future__ import annotations` and avoid 3.10+ syntax
  (e.g. `match`). Built-in generics in annotations are fine because they are
  stringified.
- **Test your change.** Most logic is pure and unit-tested with `unittest` under
  `tests/`. Browser/network-dependent paths should degrade gracefully and be
  guarded so the suite passes without them.
- **Keep artifacts hybrid.** Commands write human-readable `.md` + machine
  `.json`; SQLite is the system of record but never required for a run to work.
- Match the surrounding style: small modules, docstrings that explain *why*, and
  no new heavyweight dependencies in the core (use an optional extra instead).

## Making a change

1. Branch off `main` (e.g. `feat/…`, `fix/…`, `docs/…`).
2. Make the change with tests; run `.venv/bin/pytest` (and `python -m build` if
   you touched packaging).
3. Open a PR against `main` with a clear description of what and why. Reference
   the issue it addresses (`Closes #NN`).

## Where to start

Browse the [open issues](https://github.com/sajjadGG/Ghostlab/issues) — they are
labeled by pipeline stage (`pipeline:understand`, `pipeline:run`,
`pipeline:evaluate`, …). Good first contributions: a new target/scenario/runner
preset, an additional lint or assertion, or a per-widget MCP Apps assertion.
