# Agent Instructions

This repository is Ghostlab: a headless runner for coding agents (Claude Code, Codex, Copilot CLI, OpenCode) sandboxed in NVIDIA OpenShell, plus a Python SDK for checkpointed, resumable agent pipelines.

## Project Shape

- Package: `ghostlab/` (`agent`, `harnesses`, `parsers`, `sandbox`, `session`, `pipeline`, `cli`)
- Tests: `tests/`, with fake agent and fake OpenShell CLIs in `tests/fakes/`
- Example agents and pipelines: `examples/`
- Documentation wiki: `docs/` with `mkdocs.yml`
- CI and release pipelines: `.github/workflows/`

Ghostlab is infrastructure: keep job-specific logic (MCP evaluation, labeling, ...) in example pipelines, not in the package.

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

## Coding Guidance

- Keep edits scoped to the requested behavior.
- Preserve public CLI compatibility unless a change explicitly removes it.
- Prefer standard-library solutions unless the project already depends on a package for the job.
- Keep agent configs YAML/JSON and easy to inspect.
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

- PyPI publishing uses GitHub Actions Trusted Publishing from `.github/workflows/publish.yml`.
- GitHub Pages documentation deploys from `.github/workflows/pages.yml`.
- The public docs URL is `https://sajjadgg.github.io/Ghostlab/`.
