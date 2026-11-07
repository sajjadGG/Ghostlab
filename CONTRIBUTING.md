# Contributing to Ghostlab

Thanks for helping improve Ghostlab! This guide is written for both humans and
coding agents. For a machine-readable map of the project, see
[`llms.txt`](llms.txt).

## Project shape

- Package and CLI: `ghostlab`. Its only runtime dependency is PyYAML.
- Layers, bottom up: `sandbox` (local / OpenShell boxes) → `harnesses` +
  `parsers` (one class per agent CLI) → `session` (a run, single or
  multi-turn) → `pipeline` (stages, checkpoints, resume) → `cli`.
- Ghostlab is infrastructure. Job-specific logic belongs in pipelines under
  `examples/`, not in the package.

## Setup

```bash
python3 -m venv .venv                   # Python 3.10+
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Sandboxed runs need [NVIDIA OpenShell](https://docs.nvidia.com/openshell/latest/)
(`openshell status`). The test suite does not: `tests/fakes/` provides a fake
agent CLI and a fake `openshell` CLI.

## Making a change

1. Branch off `main` (`feat/…`, `fix/…`, `docs/…`).
2. Make the change with tests.
3. Open a PR against `main` that says what changed and why.

To add a harness, subclass `Harness` in `ghostlab/harnesses.py`, add its parser
to `ghostlab/parsers.py`, register both, and document its options in
`docs/agents.md`.

When you add or change a CLI command, update `ghostlab/cli.py`, `tests/`,
`README.md`, and `docs/cli.md`.

## Pull request checklist

```bash
.venv/bin/pytest                        # always
.venv/bin/mkdocs build --strict         # if you touched docs/ or mkdocs.yml
.venv/bin/python -m build               # if you touched packaging
.venv/bin/twine check dist/*            # "
```

## Releases

Publishing to PyPI is automated by `.github/workflows/publish.yml` via PyPI
Trusted Publishing. It runs when a GitHub Release is published.

1. Bump `__version__` in `ghostlab/__init__.py`.
2. Validate: `.venv/bin/pytest && .venv/bin/python -m build && .venv/bin/twine check dist/*`.
3. `gh release create v<x.y.z> --generate-notes`.
