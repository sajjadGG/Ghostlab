# Examples

Ready-to-run sample configs so you can try Ghostlab without writing any JSON
first. They point at the public Hugging Face MCP (`https://huggingface.co/mcp`),
which can be **introspected without a token**, so the read-only commands below
work with zero setup.

| File | Shape | Used by |
| --- | --- | --- |
| `target.json` | Native Ghostlab target (`id` + `transport` + `connection`) | `ghostlab inspect`, `ghostlab run` |
| `scenario.json` | A dual-agent scenario (persona, goal, success/failure criteria) | `ghostlab run` |
| `mcp-config.json` | Standard `mcpServers` client config | `ghostlab create`, `ghostlab inspect` |

## Try it

Introspect the target with no auth (writes `inspect.json` + `inspect.md`):

```bash
ghostlab inspect --target examples/target.json
```

Run the mock dual-agent scenario end to end — no coding-agent credits, no
network to the target (the mock runners don't call it):

```bash
ghostlab run \
  --target examples/target.json \
  --scenario examples/scenario.json \
  --aut-runner runners/mock-aut.json \
  --user-runner runners/mock-user.json
```

Scaffold a job straight from a standard `mcpServers` config:

```bash
ghostlab create --target examples/mcp-config.json
```

## Adding auth

Most Hugging Face tools (beyond introspection) need a token. Add a bearer header
to either config and export the referenced env var — the value is an env-var
*reference*, never a literal secret in the file:

```json
"headers": { "Authorization": "Bearer ${HF_TOKEN}" }
```

```bash
export HF_TOKEN=hf_...      # your token, kept out of the config file
```
