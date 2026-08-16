# Ghostlab as a skill

`skills/ghostlab/` packages Ghostlab so a coding harness (Claude Code, or any
agent that loads `SKILL.md` files) can drive it: set up an evaluation, run it,
and read the results without being told the CLI surface each time.

## Install

Skills are loaded from a directory, so a symlink keeps it current with the repo:

```bash
# For every project you work on
mkdir -p ~/.claude/skills
ln -s "$PWD/skills/ghostlab" ~/.claude/skills/ghostlab

# Or just this project
mkdir -p .claude/skills
ln -s "../../skills/ghostlab" .claude/skills/ghostlab
```

Copy the directory instead of symlinking if you would rather pin a version.

Ghostlab itself must be installed and on `PATH` (`pip install -e .`), along with
OpenShell and at least one agent CLI — the skill's first instruction is to run
`ghostlab doctor --probe`, which reports exactly what is missing.

## What it covers

- choosing between `create` (an MCP server) and `lab` / `agent.json` (a
  configured agent)
- the scriptable file-driven path, which is what a harness should use — the
  wizard is interactive and awkward to drive
- reading `readiness.md`, `verdict.json`, and `critique.md`, including when the
  judge's narrative and the deterministic tool-call record disagree
- the failure modes that look like harness bugs but are not

## Verify it

`examples/agent-lab/` holds a complete, runnable configured agent. From a
checkout:

```bash
ghostlab create --name demo --agent examples/agent-lab/agent.json --no-discover --yes
ghostlab discover --job demo
```
