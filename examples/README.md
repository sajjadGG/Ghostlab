# Examples

Run these from the repository root.

| Directory | What it is |
| --- | --- |
| [`agents/`](agents) | a starting agent config for each harness |
| [`release-notes/`](release-notes) | one agent with instructions, a skill, and a workspace: `ghostlab exec --agent examples/release-notes/agent.yaml "Draft release notes into RELEASE.md" --collect RELEASE.md` |
| [`batch-label/`](batch-label) | a one-stage batch job: `ghostlab run examples/batch-label/pipeline.py --input examples/batch-label/tickets.jsonl` |
| [`mcp-eval/`](mcp-eval) | a four-stage MCP evaluation: `ghostlab run examples/mcp-eval/pipeline.py --input examples/mcp-eval/targets.jsonl` |

The agent configs default to OpenShell with an `anthropic` provider. To run on
this machine instead, override a pipeline's agent parameter or `exec`'s
boundary:

```bash
ghostlab exec --agent examples/agents/claude.yaml --sandbox local "Hello"
ghostlab run examples/batch-label/pipeline.py --input examples/batch-label/tickets.jsonl \
  --param 'labeler={"harness": "claude", "model": "haiku", "sandbox": "local"}'
```
