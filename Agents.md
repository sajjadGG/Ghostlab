# Agent Instructions

This repository is Ghostlab, a Python CLI and optional Streamlit UI for testing MCP-exposed apps with dual coding-agent sessions.

## Project Shape

- Core package: `rehearsal/`
- Compatibility package: `ghostlab/`
- Tests: `tests/`
- Example configs: `targets/`, `runners/`, `scenarios/`, `personas/`
- Documentation wiki: `docs/` with `mkdocs.yml`
- CI and release pipelines: `.github/workflows/`

Prefer the public CLI name `ghostlab` in documentation and examples. The `rehearsal` console script remains supported for compatibility.

## Development Setup

Use the local virtualenv when it exists:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m mkdocs build --strict
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

If the venv is missing:

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

Install UI dependencies only when working on the Streamlit app:

```bash
.venv/bin/pip install -e ".[ui]"
```

## Coding Guidance

- Keep edits scoped to the requested behavior.
- Preserve public CLI compatibility unless a change explicitly removes it.
- Prefer standard-library solutions unless the project already depends on a package for the job.
- Keep runner config and MCP target examples JSON-compatible and easy to inspect.
- Do not commit generated run outputs, datasets, virtualenvs, `site/`, `dist/`, or cache directories.
- When adding a CLI command, update tests, README examples, and the MkDocs CLI reference.
- When changing packaging, run build and Twine checks.
- When changing docs, run `mkdocs build --strict`.

## Test Expectations

Run focused tests for small changes and the full test suite before release-facing changes:

```bash
.venv/bin/python -m pytest
```

For docs or pipeline changes, also run:

```bash
.venv/bin/python -m mkdocs build --strict
```

For packaging or PyPI-facing changes, also run:

```bash
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

## Git Safety

The working tree may contain user or parallel-agent changes. Do not revert changes you did not make. If unrelated files are dirty, leave them alone and stage only the files relevant to your task.

## Release Notes

- PyPI publishing uses GitHub Actions Trusted Publishing from `.github/workflows/release.yml`.
- GitHub Pages documentation deploys from `.github/workflows/pages.yml`.
- The public docs URL is `https://sajjadgg.github.io/Rehearsal/`.
