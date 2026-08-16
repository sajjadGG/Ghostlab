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
- NVIDIA OpenShell is the default execution backend for real runner sessions
  and local stdio MCPs; `--sandbox local` is the explicit trusted-host opt-out.

## Setup

```bash
python3 -m venv .venv                   # Python 3.10+
.venv/bin/pip install -e '.[dev]'       # add ',ui' and/or ',apps' for those features
.venv/bin/pytest                        # run the test suite
```

Optional extras:

- `.[ui]` — the Streamlit pipeline UI (`ghostlab ui`).
- `.[apps]` — Playwright, for rendering MCP Apps `ui://` widgets
  (`ghostlab apps-render`). After install: `playwright install chrome`.

OpenShell integration work also needs a supported compute driver (Docker
Desktop is the simplest local option) and a connected gateway:

```bash
openshell status
ghostlab doctor
```

For documentation work:

```bash
.venv/bin/mkdocs serve                  # live-preview the wiki locally
.venv/bin/mkdocs build --strict         # what CI enforces
```

## Conventions

- **Support Python 3.10–3.13.** Keep syntax and dependencies compatible with
  the declared `requires-python` range and CI matrix.
- **Test your change.** Most logic is pure and unit-tested with `unittest` under
  `tests/`. Browser/network-dependent paths should degrade gracefully and be
  guarded so the suite passes without them.
- **Keep artifacts hybrid.** Commands write human-readable `.md` + machine
  `.json`; SQLite is the system of record but never required for a run to work.
- **Keep commits focused** on one logical change, with tests or docs alongside
  behavior changes. Don't commit generated output (`jobs/`, `runs/`, `datasets/`,
  `dist/`, `site/`), caches, or `.venv` — they're gitignored.
- Match the surrounding style: small modules, docstrings that explain *why*, and
  no new heavyweight dependencies in the core (use an optional extra instead).

## Making a change

1. Branch off `main` (e.g. `feat/…`, `fix/…`, `docs/…`).
2. Make the change with tests.
3. Open a PR against `main` with a clear description of what and why. Reference
   the issue it addresses (`Closes #NN`).

When you **add or change a CLI command**, update all of:

- `rehearsal/cli.py` (the command)
- `tests/` (coverage)
- `README.md` (the command reference)
- `docs/cli.md` (the wiki page)

Use `ghostlab` (not `rehearsal`) in public examples.

## Pull request checklist

Run the checks that match your change:

```bash
.venv/bin/pytest                        # always
.venv/bin/mkdocs build --strict         # if you touched docs/ or mkdocs.yml
.venv/bin/python -m build               # if you touched packaging
.venv/bin/twine check dist/*            # "
```

Run the full set for release, packaging, workflow, or public-CLI changes.

## Documentation site

The wiki lives in `docs/` and is built with MkDocs. The **Pages** workflow
(`.github/workflows/pages.yml`) deploys it to GitHub Pages on pushes to `main`,
release tags, and manual runs. Public docs: <https://sajjadgg.github.io/Ghostlab/>.

## Releases

Publishing to PyPI is automated by `.github/workflows/publish.yml` via
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) (no tokens).
It runs when you **publish a GitHub Release** (or dispatch the workflow manually).
Version tags use `v*.*.*`.

To cut a release:

1. Bump `__version__` in `rehearsal/__init__.py`.
2. Validate locally: `.venv/bin/pytest && .venv/bin/python -m build && .venv/bin/twine check dist/*`.
3. `gh release create v<x.y.z> --generate-notes` — this triggers `publish.yml`.

## Where to start

Browse the [open issues](https://github.com/sajjadGG/Ghostlab/issues) — they are
labeled by pipeline stage (`pipeline:understand`, `pipeline:run`,
`pipeline:evaluate`, …). Good first contributions: a new target/scenario/runner
preset, an additional lint or assertion, or a per-widget MCP Apps assertion.
