# Publishing

## Package Release

Build and validate distributions locally:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m build
.venv/bin/twine check dist/*
```

The `Release` workflow publishes to PyPI when a `v*.*.*` tag is pushed or the workflow is run manually.

PyPI publishing uses Trusted Publishing. Create or claim the `mcp-ghostlab` project on PyPI and add a trusted publisher for:

- Repository: `sajjadGG/Rehearsal`
- Workflow: `.github/workflows/release.yml`
- Environment: `pypi`

No PyPI username or token needs to be committed.

## Wiki Release

The `Pages` workflow builds this wiki with MkDocs and deploys it to GitHub Pages.

It runs on:

- Pushes to `main`.
- Version tags matching `v*.*.*`.
- Manual workflow dispatch.

The workflow uses GitHub's Pages artifact flow, so set the repository Pages source to GitHub Actions.

## Local Wiki Preview

```bash
.venv/bin/mkdocs serve
```

Build strictly before pushing:

```bash
.venv/bin/mkdocs build --strict
```
