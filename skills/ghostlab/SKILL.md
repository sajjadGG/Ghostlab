---
name: ghostlab
description: Run a configured coding agent (Claude Code, Codex, Copilot CLI, OpenCode) headlessly on a task — locally or sandboxed in NVIDIA OpenShell — or write a Ghostlab pipeline that chains agent calls into a checkpointed, resumable job (evaluations such as MCP or skill evals, dataset labeling, test generation, any batch agent work). Use when asked to run an agent on a task in a sandbox, to run many agent tasks over a dataset, to build an agentic eval, or to resume or inspect a Ghostlab run.
---

# Ghostlab

Ghostlab is infrastructure for headless agent work:

- an **agent** (YAML/JSON or Python) = harness + model + instructions + skills
  + MCPs + workspace + sandbox;
- a **run** of it = `Result` with `status`, `answer`, `tool_calls`, `.json()`;
- a **pipeline** = stages that map, fan out, or gather items, where every item
  and every agent call is checkpointed, so re-running resumes.

## One task

```bash
ghostlab exec --agent agent.yaml "task" [--sandbox local] [--collect OUT.md] [--json]
```

A minimal agent:

```yaml
harness: claude        # claude | codex | copilot | opencode | command
model: sonnet
instructions: Be concise.
skills: [skills/x]
mcps: {docs: {command: node, args: [docs-mcp.js]}}
workspace: repo        # always a private copy
sandbox: {backend: openshell, providers: [anthropic]}   # or: sandbox: local
```

The answer goes to stdout. The trace is in `runs/<stamp>-<name>/turn-1/`
(`prompt.txt`, `stdout.txt`, `stderr.txt`, `answer.md`, `result.json`).

## Many tasks: write a pipeline

```python
from ghostlab import Agent, Pipeline, stage

@stage(concurrency=4, retries=1)
def work(ctx, item):
    reply = ctx.run(ctx.agent("worker"), f"... {item['text']} ... Reply with JSON.")
    data = reply.json()                 # raise if the answer is unusable -> retried
    return {**item, **data}

pipeline = Pipeline("job", [work], params={"worker": Agent.load("worker.yaml")})
```

```bash
ghostlab run job.py --input items.jsonl         # results: runs/job/results.jsonl
ghostlab run job.py                             # resume after a crash or a failure
ghostlab status runs/job
```

Building blocks:

- `@stage(fan_out=True)` returns a list, and each element becomes an item;
  `@stage(gather=True)` receives all items at once (for reports); returning
  `None` from a map stage drops the item.
- `ctx.session(agent)` holds a multi-turn conversation that resumes the
  harness session — use it for emulated users.
- `agent.with_(model=..., instructions=...)` and `agent.with_mcp(name, spec)`
  derive variants. Pass agents as params so `--param worker=other.yaml` swaps
  them without code changes.
- Bump `@stage(version="2")` after changing a stage's logic to recompute it.

`examples/mcp-eval/pipeline.py` is a full evaluation pipeline: describe the
MCP → generate scenarios → run emulated-user conversations and judge them →
report.

## Reading failures

`Result.status` separates the causes:

| Status | Meaning — what to do |
| --- | --- |
| `agent_error` | the harness exited non-zero; read `turn-N/stderr.txt` |
| `model_unavailable` | quota, auth, or an unsupported model; change the model or the credentials |
| `timed_out` | raise the agent's `timeout` |
| `sandbox_error` | OpenShell failed (gateway, image, policy); check `openshell status` and `openshell.log` |
| `harness_error` | the CLI binary was not found |

A failed pipeline item keeps its traceback in `<NN-stage>/<key>/item.json` and
the traces of failed attempts under `attempts/N/`.
