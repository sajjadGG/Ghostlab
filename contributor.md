# Contributor Guide

Thanks for contributing to Ghostlab. This project is a local E2E testing harness for MCP-exposed apps, with a Python CLI, optional Streamlit UI, generated datasets, structured run logs, and GitHub Pages documentation.

## Quick Start

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

For UI work:

```bash
.venv/bin/pip install -e ".[ui]"
ghostlab ui
```

For documentation work:

```bash
.venv/bin/mkdocs serve
.venv/bin/mkdocs build --strict
```

## Branches And Commits

- Use short, descriptive branch names.
- Keep commits focused on one logical change.
- Include tests or docs with behavior changes.
- Do not include generated `runs/`, `datasets/`, `dist/`, `site/`, cache directories, or `.venv`.

## Pull Request Checklist

Before opening a PR, run the checks that match your change:

```bash
.venv/bin/python -m pytest
```

```bash
.venv/bin/python -m mkdocs build --strict
```

```bash
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

Use the full set for release, packaging, workflow, or public CLI changes.

## CLI And Docs

Use `ghostlab` in public examples:

```bash
ghostlab inspect --target target.json
ghostlab run --target target.json --scenario scenario.json
```

If you add or change a command, update:

- `rehearsal/cli.py`
- tests under `tests/`
- `README.md`
- `docs/cli.md`

## Documentation Site

The wiki lives in `docs/` and is built with MkDocs. GitHub Pages deploys it from the `Pages` workflow on pushes to `main`, release tags, and manual workflow runs.

Public docs: `https://sajjadgg.github.io/Ghostlab/`

## Releases

PyPI release publishing is handled by `.github/workflows/release.yml` using Trusted Publishing. Version tags should use `v*.*.*`.

Local release validation:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m build
.venv/bin/twine check dist/*
```
