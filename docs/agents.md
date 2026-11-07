# Agents

An agent is a harness plus what it is given to work with. Load one from YAML
or JSON with `Agent.load(path)`, or build it in Python with `Agent(...)`.
Relative paths in a config file resolve against that file.

```yaml
name: reviewer                       # default: the file name
harness: claude                      # claude | codex | copilot | opencode | command
model: sonnet                        # passed to the harness as-is
instructions: Be concise.            # free text
instruction_files: [AGENTS.md]       # files whose contents are instructions
skills: [skills/review]              # SKILL.md files or their folders
mcps:                                # the `mcpServers` shape every client uses
  docs: {command: node, args: [docs-mcp.js], env: {LEVEL: "1"}}
  hub:  {url: "https://hub.example/mcp", headers: {Authorization: "Bearer ${HUB_TOKEN}"}}
mcp_config: .mcp.json                # or load servers from an existing config file
workspace: repo                      # a directory the agent works on (always a copy)
env: {API_BASE: "${API_BASE}"}       # extra environment; ${VAR} expands on the host
timeout: 900                         # seconds per turn
options: {}                          # harness-specific settings, below
sandbox: {backend: openshell}        # where it runs, below
```

Agents are immutable. Derive variants with `with_` and `with_mcp`:

```python
judge = helper.with_(model="opus", instructions="You are a strict grader.")
tester = assistant.with_mcp("tiny", {"command": "node", "args": ["tiny-mcp.js"]})
```

## Running

| Call | Does |
| --- | --- |
| `agent.run(prompt, run_dir=None, collect=())` | one task; returns a `Result` |
| `agent.session(run_dir=None)` | a multi-turn `Session`; `session.send(prompt)` per turn |

A session keeps one sandbox and one harness conversation alive. Later turns
resume it with `--resume` (Claude Code), `exec resume` (Codex),
`--session-id`/`--continue` (Copilot), or `--session` (OpenCode). Instructions
and skills are sent on the first turn only.

`collect` copies workspace paths into the turn directory before the sandbox is
torn down; they are listed in `result.files`.

### Result

| Field | Meaning |
| --- | --- |
| `status` | `completed`, `agent_error`, `timed_out`, `model_unavailable`, `sandbox_error`, `harness_error` |
| `ok` | `status == "completed"` |
| `answer` | the agent's final message |
| `json()` | the JSON value in the answer (tolerates prose and code fences) |
| `check()` | returns the result, or raises `AgentError` if it did not complete |
| `tool_calls` / `builtin_calls` | MCP calls and built-in tool calls: server, tool, status, arguments, result, error |
| `session_id`, `turn`, `duration`, `exit_code`, `error`, `files`, `dir` | the rest of the trace |

Each turn writes `prompt.txt`, `stdout.txt`, `stderr.txt`, `answer.md`,
`result.json`, and `files/` under `<run_dir>/turn-N/`.

## Harnesses

Instruction files and skills are rendered into a preamble ahead of the task
for every harness except OpenCode, which loads them natively. Every harness
accepts `options.bin` (host binary) and `options.extra_args` (appended
verbatim).

| Harness | Command | MCPs via | Options |
| --- | --- | --- | --- |
| `claude` | `claude -p --output-format stream-json` | `--mcp-config` (pre-approved) | `permission_mode`, `max_turns`, `allow_tools`, `deny_tools` |
| `codex` | `codex exec --json` | `-c mcp_servers.<name>.*` | `sandbox_mode`, `approval`, `reasoning_effort` |
| `copilot` | `copilot --output-format json --prompt` | `--additional-mcp-config` | `agent`, `reasoning_effort`, `mode`, `allow_all`, `allow_all_tools`, `allow_tools`, `deny_tools`, `add_dirs`, `allow_urls`, `deny_urls`, `plugin_dirs`, ... |
| `opencode` | `opencode run --format json` | its own config (`OPENCODE_CONFIG_CONTENT`) | `default_agent`, `config` (merged into `opencode.json`) |
| `command` | `options.command` | — | `command` (argv), `prompt` (`stdin` or `arg`), `output` (`text`, or a harness name to parse its stream) |

Codex reads `Bearer ${VAR}` and `${VAR}` headers from the environment itself,
so those secrets never appear in its argv. OpenCode denies `bash`, `edit`, and
`webfetch` unless `options.config.permission` allows them.

Coding-agent sessions are tagged as synthetic: Codex gets
`originator: ghostlab`, and Copilot session ids start with Ghostlab's reserved
UUID prefix, so rollout collectors can exclude them.

## Sandbox

```yaml
sandbox:
  backend: openshell                 # or local
  image: base                        # an OpenShell image or Dockerfile
  providers: [anthropic]             # OpenShell providers holding credentials
  policy: egress.yaml                # an OpenShell policy file
  env_allowlist: [GH_TOKEN]          # host variables forwarded into the sandbox
  uploads: [{source: data, target: /sandbox/data}]
  files: {/opt/agent/share/opencode/auth.json: ~/.local/share/opencode/auth.json}
  cpu: "2"
  memory: 4Gi
  keep: false                        # leave the sandbox running for debugging
```

`sandbox: local` is shorthand for `{backend: local}`.

**OpenShell** (the default) creates one sandbox per session. It uploads the
workspace to `/sandbox/<name>`, every stdio MCP's program to `/sandbox/mcp`,
and any files the harness reads by path to `/sandbox/agent`. Paths in the
command are rewritten to those locations. `files` are copied in after creation
with mode 600, so credentials can live outside the workspace. With no
providers the sandbox is created with `--no-auto-providers`. For OpenCode,
when no `policy` is given, Ghostlab generates a least-privilege egress policy
for the model's provider. The sandbox log is saved as `openshell.log`, and the
sandbox is deleted unless `keep` is set. OpenShell failures are reported as
`sandbox_error` and never fall back to local execution.

**Local** runs the harness on the host, in a private copy of the workspace,
with the full host environment. The copy sits in a neutral temporary directory
while the agent runs — harnesses tell the model their working directory, and a
run directory's path names the job and item — and is moved to
`<run_dir>/workspace` afterwards. `uploads`, `files`,
and the other OpenShell settings do not apply. Use it only for agents and MCPs
you trust.

The image in [`docker/agent-sandbox.Dockerfile`](https://github.com/sajjadGG/Ghostlab/blob/main/docker/agent-sandbox.Dockerfile)
pins OpenCode and Copilot CLI builds and keeps their state outside the workspace.
