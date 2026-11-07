# Ghostlab as a skill

`skills/ghostlab/` packages Ghostlab so a coding harness (Claude Code, or any
agent that loads `SKILL.md` files) can run agents headlessly and write
pipelines without being told the API each time.

## Install

Skills are loaded from a directory, so a symlink keeps the skill current with
the repo:

```bash
# For every project you work on
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/ghostlab" ~/.claude/skills/ghostlab

# Or just this project
mkdir -p .claude/skills
ln -s "../../skills/ghostlab" .claude/skills/ghostlab
```

Ghostlab itself must be installed (`pip install ghostlab`), along with at least
one agent CLI and, for sandboxed runs, OpenShell.

## Verify it

```bash
ghostlab exec --agent examples/agents/claude.yaml --sandbox local "Say hello in one word."
```
