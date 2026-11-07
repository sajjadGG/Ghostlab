# CLI

```text
ghostlab exec    run one agent on one task
ghostlab run     run or resume a pipeline
ghostlab status  summarize a pipeline run
```

Exit codes: `0` success, `1` the task or some items failed, `2` invalid
configuration.

## `ghostlab exec`

```bash
ghostlab exec --agent agent.yaml "Draft release notes into RELEASE.md" --collect RELEASE.md
echo "Summarize this" | ghostlab exec --agent agent.yaml -
```

| Flag | Meaning |
| --- | --- |
| `--agent PATH` | agent config (required) |
| `TASK` / `-` / `--prompt-file PATH` | the task, inline, from stdin, or from a file |
| `--harness`, `--model`, `--timeout`, `--workspace` | override the config |
| `--sandbox openshell\|local` | override the execution boundary |
| `--provider NAME` | attach an OpenShell provider (repeatable) |
| `--collect PATH` | copy a workspace path into the run dir (repeatable) |
| `--run-dir DIR` | where to keep the trace (default `runs/<timestamp>-<agent>`) |
| `--json` | print the full result instead of the answer |

The answer goes to stdout, and the status line and run directory go to stderr.

## `ghostlab run`

```bash
ghostlab run examples/mcp-eval/pipeline.py --input examples/mcp-eval/targets.jsonl
ghostlab run examples/mcp-eval/pipeline.py                   # resume
ghostlab run pipeline.py:nightly --params params.yaml --param max_turns=6
```

| Flag | Meaning |
| --- | --- |
| `FILE.py[:NAME]` | the module holding the `Pipeline` (`NAME` when it defines several) |
| `--input PATH` | items as `.jsonl`, JSON, or YAML (repeatable); omit to reuse the recorded inputs |
| `--param KEY=VALUE` | a parameter; `VALUE` is parsed as JSON when it can be, otherwise kept as a string |
| `--params PATH` | parameters from a YAML/JSON mapping |
| `--run-dir DIR` | run directory (default `runs/<pipeline name>`, so re-running resumes) |

An agent parameter can be a config path (`--param judge=agents/judge.yaml`) or
inline JSON (`--param 'judge={"harness": "claude", "model": "opus"}'`).

## `ghostlab status`

```bash
ghostlab status runs/mcp-eval
```

Prints done, cached, and failed counts per stage and lists the failed items.
Exits `1` when the run is incomplete or has failures.
