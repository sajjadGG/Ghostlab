# Run an Agent

`ghostlab exec` takes one agent definition and one task, runs the agent in the
harness you choose, and gives you its answer. Nothing about the agent is
specific to a harness: the same file runs in Claude Code, Codex, GitHub Copilot
CLI, or OpenCode, and by default all of it runs inside a throwaway NVIDIA
OpenShell sandbox.

```bash
ghostlab exec --agent agent.json "Summarize the open TODOs in this repo"
```

## The agent definition

```json
{
  "id": "release-helper",
  "description": "Drafts release notes. Never claims a release was published.",
  "instructions": "Be concise. Ask before anything irreversible.",
  "runtime": {
    "backend": "claude",
    "model": "sonnet",
    "timeout_seconds": 600
  },
  "workspace": "repo",
  "inputs": {
    "mcps": [
      { "config_ref": "mcp.json", "server": "tiny" },
      { "id": "hub", "transport": "streamable-http",
        "connection": { "url": "https://hub.example/mcp",
                        "headers": { "Authorization": "Bearer ${HUB_TOKEN}" } } }
    ],
    "skills": ["skills/release-notes"]
  },
  "sandbox": {
    "backend": "openshell",
    "image": "base",
    "providers": ["anthropic"]
  }
}
```

Every relative path resolves against the agent file. YAML works too.

| Field | Meaning |
| --- | --- |
| `runtime.backend` | `claude`, `codex`, `copilot`, or `opencode`. Omit it and give a `runner.command` to drive any other CLI. |
| `runtime.model` | Model for that harness (`sonnet`, `gpt-5-mini`, `github-copilot/claude-sonnet-4.5`, ...). |
| `instructions` | Free-text behavior and policy, applied in every harness. |
| `runtime.instructions` | Instruction files (for example `AGENTS.md`), applied in every harness. |
| `inputs.mcps` | Any number of MCP servers: inline (`id`, `transport`, `connection`) or a `config_ref` to a standard `mcpServers` file. |
| `inputs.skills` | Skill folders or `SKILL.md` files. |
| `workspace` | The directory the agent works on. |
| `sandbox` | The OpenShell boundary: `image`, `providers`, `policy`, `env_allowlist`, `credentials`, resources. |

## How each harness receives the definition

| Backend | Command | MCPs | Skills and instruction files | Answer parsed from |
| --- | --- | --- | --- | --- |
| `claude` | `claude -p --output-format stream-json` | `--mcp-config` with `--strict-mcp-config`; the agent's MCP tools are pre-approved | Prompt preamble | Final `result` frame |
| `codex` | `codex exec --json` | `-c mcp_servers.<id>.*` overrides | Prompt preamble | `agent_message` items |
| `copilot` | `copilot --output-format json --prompt` | `--additional-mcp-config` | Prompt preamble | Last assistant message |
| `opencode` | `opencode run --format json` | Generated `opencode.json` | Native (`instructions`, `skills.paths`) | Text parts |
| `runner` | Your `runner.command` | Your command | Prompt preamble | Its `parser` (default: stdout) |

Harness-specific settings live in `runtime` and are passed through:

- **claude** — `permission_mode`, `allow_tools`, `deny_tools`, `max_turns`, `extra_args`.
- **codex** — `codex_sandbox` (default `workspace-write`), `approval_mode`, `reasoning_effort`, `extra_args`.
- **copilot** — every key `ghostlab create` accepts for Copilot (`agent`, `reasoning_effort`, `allow_tools`, `deny_tools`, `allow_all`, ...).
- **opencode** — the full OpenCode runtime: `permission`, `agents`, `default_agent`, `instructions`, `skills`.

Only the MCPs and skills in the definition are attached. Claude Code runs with
`--strict-mcp-config` so servers from your own configuration do not leak in.

## The sandbox

`--sandbox openshell` is the default, and nothing falls back to the host:

1. A sandbox is created from `sandbox.image` (default `base`, which ships the
   Claude Code, Codex, Copilot, and OpenCode CLIs) with the declared providers
   and policy.
2. The workspace is uploaded to `/sandbox/<name>`. Stdio MCP programs and
   OpenCode instruction and skill files are uploaded beside it, and every path
   in the harness configuration is rewritten to its in-sandbox location.
3. The harness runs inside the sandbox, so the MCP servers do too, and edits
   land in the uploaded copy.
4. `--export REMOTE[=NAME]` files are downloaded, the OpenShell log is saved to
   the run directory, and the sandbox is deleted (`--keep-sandbox` keeps it).

Model credentials come from OpenShell providers. Attach them in the agent file
(`sandbox.providers`) or per run with `--provider`. For OpenCode,
`sandbox.credentials.opencode_auth: true` copies your OpenCode login into the
sandbox outside the workspace, and Ghostlab generates a least-privilege egress
policy for the model's provider.

`--sandbox local` runs the harness on your machine in the workspace directory
itself, and with your own harness configuration and credentials. Use it only
for agents you trust.

## Outputs

```text
runs/<timestamp>-exec-<agent>/
  answer.md            the agent's final message (also printed to stdout)
  exec.json            status, backend, model, sandbox, MCP and built-in tool calls, timing
  prompt.txt           exactly what the harness received
  stdout.txt           the harness's raw stream
  stderr.txt
  openshell-agent.log  sandbox log (OpenShell runs)
  <exports>            files requested with --export
```

`ghostlab exec` exits `0` only when the status is `completed`. Other statuses
keep "the harness broke" apart from "the agent failed": `agent_error`,
`model_unavailable`, `timed_out`, `sandbox_error`, `harness_error`.

## Examples

```bash
# Switch harness and model without touching the file
ghostlab exec --agent agent.json --backend copilot --model gpt-5-mini --provider copilot "..."
ghostlab exec --agent agent.json --backend opencode --model github-copilot/gpt-5-mini "..."

# Long task from a file; keep only the answer
ghostlab exec --agent agent.json --prompt-file task.md > answer.md

# Pipe a task in, get machine-readable output
echo "List the failing tests" | ghostlab exec --agent agent.json - --json

# Work on a different checkout and bring back a file it writes
ghostlab exec --agent agent.json --workspace ../service \
  --export /sandbox/service/REPORT.md "Audit the error handling and write REPORT.md"
```

`examples/agent-lab/task-agent.json` is a complete working definition with a
stdio MCP server, a skill, and a workspace.

## From running to evaluating

The file `ghostlab exec` runs is the same agent definition the evaluation
pipeline takes. When you want to know how *well* the agent does its job rather
than just getting one answer, hand it to the [Configured Agent Lab](configured-agent-lab.md)
or `ghostlab create --agent agent.json`, which generate realistic scenarios
from it, drive it with a simulated user, and judge the outcomes.
