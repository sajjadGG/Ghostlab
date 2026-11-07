# Getting started

## Install

```bash
pip install ghostlab          # Python 3.10+
```

You also need the CLI of at least one harness (`claude`, `codex`, `copilot`,
or `opencode`). For sandboxed runs, install
[NVIDIA OpenShell](https://docs.nvidia.com/openshell/latest/) and check it
with `openshell status`.

## Run one agent

```yaml
# helper.yaml
harness: claude
model: sonnet
instructions: Be concise.
sandbox: local                 # or {backend: openshell, providers: [anthropic]}
```

```bash
ghostlab exec --agent helper.yaml "What is 17 * 23? Reply with the number only."
```

The answer goes to stdout. The full trace (prompt, raw stream, stderr,
`result.json`) is kept in `runs/<timestamp>-helper/turn-1/`.

The same from Python:

```python
from ghostlab import Agent

helper = Agent.load("helper.yaml")
result = helper.run("What is 17 * 23?")
print(result.status, result.answer)
```

## Run one pipeline

```python
# label.py
from ghostlab import Agent, Pipeline, stage

@stage(concurrency=8, retries=2)
def label(ctx, ticket):
    reply = ctx.run(ctx.agent("labeler"),
                    f"Label as bug, billing, or other. JSON {{\"label\": ...}}.\n\n{ticket['text']}")
    return {**ticket, **reply.json()}

pipeline = Pipeline("label", [label], params={"labeler": Agent.load("helper.yaml")})
```

```bash
ghostlab run label.py --input tickets.jsonl     # writes runs/label/results.jsonl
ghostlab run label.py                           # resume: done items are skipped
ghostlab status runs/label
```

Next: the [agent reference](agents.md) and the [pipeline guide](pipelines.md).
