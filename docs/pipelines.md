# Pipelines

A pipeline is an ordered list of stages. Inputs feed the first stage, and each
stage's outputs feed the next. A stage is a plain function `fn(ctx, item)`.

```python
from ghostlab import Agent, Pipeline, stage

@stage
def describe(ctx, target): ...                 # map: one output per item

@stage(fan_out=True)
def scenarios(ctx, target): ...                # returns a list; each element is an item

@stage(concurrency=4, retries=1, version="2")
def evaluate(ctx, case): ...                   # parallel, retried, versioned

@stage(gather=True)
def report(ctx, results): ...                  # receives every surviving item at once

pipeline = Pipeline("mcp-eval", [describe, scenarios, evaluate, report],
                    params={"assistant": Agent.load("agents/assistant.yaml"), "max_turns": 4})

report = pipeline.run("targets.jsonl", run_dir="runs/mcp-eval")
report.ok, report.results, report.failures
```

A map stage that returns `None` drops the item, so filtering is just a stage.
An exception fails only that item: it is recorded, the other items carry on,
and nothing downstream sees it.

## Stage options

| Option | Default | Meaning |
| --- | --- | --- |
| `fan_out` | `False` | the function returns a list; each element becomes an item |
| `gather` | `False` | the function receives the list of all items and returns one |
| `concurrency` | `1` | items run in parallel threads (agent calls are subprocesses) |
| `retries` | `0` | extra attempts within a run before an item is marked failed |
| `version` | `""` | part of every item's checkpoint key; bump it after changing the logic |
| `name` | function name | the stage directory name |

## The context

| Member | Use |
| --- | --- |
| `ctx.run(agent, prompt, check=True, collect=())` | one agent task, cached (see below); raises `AgentError` unless it completes, when `check` is set |
| `ctx.session(agent)` | a multi-turn `Session`, as a context manager |
| `ctx.agent(name, **overrides)` | the agent passed as parameter `name`: an `Agent`, a config path, or an inline mapping |
| `ctx.params` | the pipeline parameters (defaults merged with run-time ones) |
| `ctx.dir` | this item's directory, for any artifacts the stage writes |
| `ctx.key`, `ctx.stage`, `ctx.log(msg)` | identity and logging |

Passing agents as parameters keeps a pipeline independent of any one model or
harness. The same pipeline runs with a different agent by changing one
parameter:

```bash
ghostlab run pipeline.py --param assistant=agents/codex.yaml
ghostlab run pipeline.py --param 'judge={"harness": "claude", "model": "opus", "sandbox": "local"}'
```

## Checkpointing and resume

Everything lives under the run directory:

```text
runs/mcp-eval/
  inputs.jsonl          the inputs, recorded by the first run
  job.json              parameters and per-stage counts
  results.jsonl         the last stage's outputs
  01-describe/
    outputs.jsonl       this stage's outputs, keyed
    tiny/
      item.json         status, input, output or error, attempts, timing
      calls/001/        one cached agent call: call.json + turn-1/ trace
      sessions/001/     one conversation: turn-1/, turn-2/, ...
      attempts/1/       traces of a failed attempt, kept for debugging
  02-scenarios/ ...
```

There are two levels of checkpoint:

1. **Items.** An item's `item.json` is written as soon as it finishes. On the
   next run, an item that is `done` with the same checkpoint key is skipped.
   The key is the stage `version`, the item's input, and the parameters.
2. **Agent calls.** Each `ctx.run` inside an item is stored in `calls/NNN`
   with a fingerprint of the agent and prompt. When the item runs again, a
   completed call with the same fingerprint is reused instead of re-running
   the agent.

The rules that follow:

- Re-running the same command **resumes**: done items are skipped and failed
  items are retried. `pipeline.run(run_dir=...)` with no inputs reuses the
  recorded ones.
- Changing an input, a parameter, or a stage's `version` **recomputes** the
  affected items. Agent calls whose agent and prompt did not change are still
  served from the cache, so recomputing is cheap.
- When an **agent call fails** (`AgentError`), the calls that completed stay
  cached, and only the failed call is moved to `attempts/N/`.
- When the **stage rejects an answer** (any other exception, such as invalid
  JSON or a label outside the allowed set), all of that attempt's calls move to
  `attempts/N/`, so the retry asks the agent again rather than replaying the
  answer it rejected.
- **Sessions are never cached.** A conversation is re-run from the start on a
  retry, because its sandbox and harness state are gone.

Item keys are the item's `id` field (slugged) when it has one, otherwise a hash
of the item. A fan-out child is keyed `<parent>.<id or index>`, and a gather
item is keyed `all`.

## Examples

- [`examples/mcp-eval/pipeline.py`](https://github.com/sajjadGG/Ghostlab/blob/main/examples/mcp-eval/pipeline.py):
  describe each MCP with an agent that has it attached, generate scenarios
  (fan-out), run emulated-user conversations against the agent under test and
  judge them, then gather a report.
- [`examples/batch-label/pipeline.py`](https://github.com/sajjadGG/Ghostlab/blob/main/examples/batch-label/pipeline.py):
  label tickets eight at a time and validate each answer, so a bad label fails
  the item and is retried.
