# Getting Started

## Install From A Checkout

```bash
python3 -m venv .venv          # Python 3.10+
.venv/bin/pip install -e .
```

This installs the package in editable mode and adds the `ghostlab` and `rehearsal` console scripts. Prefer `ghostlab` in new docs and scripts. (Contributors who also want the test/build/docs toolchain use `pip install -r requirements-dev.txt` instead — see `CONTRIBUTING.md`.)

```bash
ghostlab --help
ghostlab --version
```

## Install A Coding-Agent CLI

Ghostlab needs one agent CLI to generate scenarios, play the agent under test,
and judge runs. Either works:

```bash
codex --version                 # uses your ChatGPT/Codex plan
opencode --version              # uses GitHub Copilot, Azure, etc.
```

For opencode, authenticate a provider once (`opencode auth login`) and select it
per command with `--llm-backend opencode --model github-copilot/claude-sonnet-4.5`,
or per job via `generation.backend` in `job.yaml`. See the README's
"Coding-agent backends" section for precedence and model selection.

## Install And Verify OpenShell

Install [NVIDIA OpenShell](https://docs.nvidia.com/openshell/latest/about/installation)
and a supported compute driver. Docker Desktop is the simplest local driver on
macOS; start it before the OpenShell gateway. Then verify both Ghostlab and the
gateway:

```bash
openshell status
ghostlab doctor
```

`ghostlab doctor` reports the sandbox, both LLM backends, and your runner
presets. Add `--probe` to send one tiny live request per backend, which is the
only way to catch an exhausted quota or a CLI too old for your account's model:

```bash
ghostlab doctor --probe
```

`openshell status` must report `Connected`. A CLI binary alone is not enough:
the gateway also needs a running compute driver. For a Homebrew installation,
use this recovery sequence when the gateway refuses connections:

```bash
open -a Docker                       # macOS only; wait until Docker is ready
docker info                          # must include a Server section
brew services restart openshell
openshell status
```

Ghostlab-generated jobs and direct `ghostlab run` calls default to OpenShell.
It creates separate sandboxes for the AUT and user emulator, uploads only
declared inputs, forwards only allowlisted environment variables, captures
`openshell-*.log`, and removes the sandboxes after the run. Local stdio MCPs
launched by Ghostlab use the same boundary.

There is no `--local` shorthand. Use `--sandbox local` to opt into direct,
unsandboxed host execution for trusted code:

```bash
ghostlab create --name trusted --agent ./agent.yaml --sandbox local --yes
ghostlab discover --job trusted --sandbox local
ghostlab test --job trusted --sandbox local
ghostlab run --target target.json --scenario scenario.json \
  --aut-runner aut.json --user-runner user.json --sandbox local
```

Ghostlab never switches to local mode automatically. A missing CLI, stopped
gateway, unavailable image, bad policy, or denied upload is reported as a
sandbox/harness error so it cannot be mistaken for an agent failure.

### Local stdio MCPs and the sandbox boundary

A stdio MCP is launched by host path (`node /path/to/server/index.js`). Under
OpenShell that path must exist *inside* the container, so Ghostlab uploads the
server's own program directory to `/sandbox/mcp/<dirname>` and rewrites the
command to match. Uploads skip `.gitignore` filtering by default, because the
files a server needs at runtime (`node_modules`, `.venv`) are usually ignored;
set `sandbox.respect_git_ignore: true` in `job.yaml` to opt back in.

Before starting the server, Ghostlab checks that its program actually resolves
inside the sandbox. If it does not, you get a `sandbox_command_missing` error
naming the missing file, rather than a timeout that looks like the MCP refused
to answer.

The session itself is carried over OpenShell's SSH channel, not
`openshell sandbox exec`. `exec` buffers stdin until EOF, so it can run a
one-shot command but cannot sustain the request/response loop a stdio MCP
needs — it would deadlock on the first `initialize`. This is why `ssh` must be
available on the host.

Some MCPs cannot be sandboxed at all: they exist to reach host-only resources.
A server that drives a macOS app (Safari, Mail, Finder) via AppleScript, or one
that needs your logged-in browser profile, will never work inside a Linux
container. Run those with `--sandbox local` and treat the code as trusted:

```bash
ghostlab discover --job safari --sandbox local
ghostlab test --job safari --sandbox local
```

Check the server's own prerequisites too — they are enforced by the host, not
by Ghostlab. `safari-mcp`, for example, needs Safari's *Develop → Allow
JavaScript from Apple Events* enabled; without it most of its tools return
`isError: true` and the smoke suite fails for reasons that have nothing to do
with the server's contract.

OpenShell currently labels itself alpha software; pin and validate the runtime
version in CI, and treat gateway/policy upgrades as infrastructure changes.

## Create A Job (Recommended Starting Point)

A **job** is one configured-agent evaluation, and everything about it lives in
one folder. `ghostlab create` accepts a complete agent definition, an MCP target,
or a skill; MCP-only and skill-only inputs are normalized into the same agent
model. In interactive MCP mode it asks only for what it cannot infer—a name and
target—then inspects the target immediately:

```bash
ghostlab create
# Job name: release-agent
# Evaluate [agent/mcp/skill] (agent): agent
# Agent config path (JSON/YAML): examples/agent.json
# Execution [openshell/local] (openshell): openshell
# → configuration preview → OpenShell preflight → [1/5] … [5/5]
```

The guided creator configures persona/scenario counts, the release gate,
OpenShell image/providers, AUT/user/generation/judge models, runner lifecycle,
timeout, Codex policy, and whether to run immediately. It previews the resolved
job before writing. For automation, pass `--yes` plus flags such as `--model`,
`--user-model`, `--generation-model`, `--judge-model`, `--runner-kind`,
`--runner-timeout`, `--approval-mode`, `--codex-sandbox`, `--personas`,
`--image`, or `--provider`. Add `--no-discover` to scaffold only.

Inspect the final effective values at any time:

```bash
ghostlab config --job release-agent
ghostlab config --job release-agent --json
```

The complete creator succeeds only after a semantic/security conversation
actually runs. If no OpenShell provider/model is usable, scenario generation
fails, or the plan contains only placeholders, it exits non-zero and leaves a
diagnostic plan instead of reporting a successful evaluation. After fixing the
configuration, run `ghostlab create --name release-agent --resume --yes`.

### Evaluate an agent skill

Pass a `SKILL.md` file or its containing directory instead of `--target`:

```bash
ghostlab create --name release-notes --skill ./skills/release-notes --yes
```

Ghostlab reads the skill instructions, generates semantic and adversarial user
scenarios, runs them through the dual-agent harness, and judges whether the AUT
followed the skill. Protocol, tool-schema, and MCP Apps suites do not apply to
skill targets.

### Evaluate a composed agent

An agent config can combine any runner with MCP, skill, workspace, and asset inputs:

```bash
ghostlab create --name my-agent --agent examples/agent.json --yes
```

```yaml
id: my-agent
instructions: Use the available capabilities and cite evidence.
runner:
  kind: process
  command: [codex, --sandbox, read-only, -a, never, exec, --json, --skip-git-repo-check, -]
  parser: codex-json
workspace: ./agent-workspace
inputs:
  mcps:
    - config_ref: ./mcp.json
      server: notes
  skills:
    - ./skills/research
tests:
  - id: summarize-evidence
    goal: Produce a concise evidence-backed summary.
    opening_message: Summarize what changed and cite the source.
    success_criteria: [Names the change, cites supporting evidence]
    failure_signals: [Invents a source]
sandbox:
  backend: openshell
  image: base
  network: disabled
  providers: [openai]
  env_allowlist: []
```

Relative references resolve from the agent config. Absolute paths under the
declared workspace are rewritten to its staged OpenShell workdir.
Inline `tests` are materialized as ordinary scenario files and seeded into
`test-plan.yaml`; generated cases can be added alongside them later.
The example assumes an OpenShell provider named `openai` already exists. Check
providers with `openshell provider list`. Provider attachment is the preferred
way to make model credentials and matching egress policy available without
copying secrets into the agent config. Remove `providers: [openai]` for a
credential-free runner, or deliberately allowlist a required environment
variable under `env_allowlist`.

It scaffolds a self-contained directory:

```
jobs/cortex-eval/
  job.yaml         # the whole config: target, hosts, generation, test, prompts, gates
  test-plan.yaml   # produced by `ghostlab plan`
  workspace/       # discover/generated/test artifacts + ghostlab.sqlite3
  runs/            # dual-agent run output
```

`job.yaml` is populated with **editable defaults** for every knob — persona/scenario counts, suites, judge, gates, and a `prompts:` section where you can override any built-in prompt (each entry is blank = use the built-in; the file header lists the `{placeholders}` each prompt accepts). An explicit CLI flag still wins over a `job.yaml` setting, which wins over the code default.

Non-interactive (for scripts/CI):

```bash
ghostlab create --name cortex-eval --target http://localhost:8000/mcp \
  --aut-runner runners/codex-cortex-local-aut.json --yes
```

### Bring your existing MCP config

`--target` also accepts the **standard `mcpServers` config** you already give Codex, Claude Desktop, Cursor, or VS Code — GhostLab normalizes it into a target (issue #32). This covers local **stdio** servers and remote **HTTP/SSE** servers alike:

```jsonc
// mcp.json
{
  "mcpServers": {
    "obsidian": { "command": "npx", "args": ["-y", "obsidian-mcp"], "env": { "VAULT": "/notes" } },
    "github":   { "url": "https://api.githubcopilot.com/mcp/",
                  "headers": { "Authorization": "Bearer ${GITHUB_TOKEN}" } }
  }
}
```

```bash
ghostlab create  --name gh --target ./mcp.json --server github   # pick a server by name
ghostlab inspect --target ./mcp.json --server obsidian           # inspect works the same way
```

When a config has a single server, `--server` is optional; with several, GhostLab lists them and asks you to choose.

**Auth without leaking secrets:** header/env values may reference environment variables (`${GITHUB_TOKEN}`), which are expanded at connection time — so the token stays in your shell, not in the tracked `job.yaml`. If you have only a URL, the wizard can add the header for you:

```bash
export GITHUB_TOKEN=ghp_xxx
ghostlab create --name gh --target https://api.githubcopilot.com/mcp/ \
  --header 'Authorization: Bearer ${GITHUB_TOKEN}' --yes
```

A `401` from `discover`/`inspect` means the server got no (or a wrong) auth header — check that the header is present in the target and that the referenced env var is exported.

Then run the loop against the job by name (`--job`), no paths to juggle:

```bash
ghostlab discover --job cortex-eval    # inspect + lint + refresh capabilities
ghostlab plan     --job cortex-eval    # coverage-driven test-plan.yaml
ghostlab test     --job cortex-eval    # execute across host adapters
ghostlab test     --job cortex-eval --resume  # resume completed case/host pairs
ghostlab review   --job cortex-eval    # readiness report / release gate
```

Inside a job directory you can drop `--job` entirely — the commands auto-detect `job.yaml` in the current folder.

## Run A Mock Scenario

Mock runners let you exercise the orchestrator without spending coding-agent credits.

```bash
ghostlab run \
  --target examples/target.json \
  --scenario examples/scenario.json \
  --aut-runner runners/mock-aut.json \
  --user-runner runners/mock-user.json
```

Run output is written under `runs/<run-id>/`:

- `events.jsonl`: structured event log.
- `report.md`: readable run summary.
- `target.mcp.json`: generated MCP server config for the target.

## Inspect A Real MCP Target

```bash
ghostlab inspect --target examples/target.json
# or a standard MCP client config:
ghostlab inspect --target ./mcp.json --server obsidian
```

`inspect` connects directly to the MCP server, runs the initialize handshake, lists tools/resources/prompts, and writes an `inspect.json` plus readable `inspect.md`. It does not need Codex or another agent. `--target` accepts either a GhostLab target JSON or a standard `mcpServers` config (use `--server` to pick one when it defines several).

## Build A Capability Profile

```bash
ghostlab profile --inspect runs/<id>-inspect/inspect.json
```

The profile combines deterministic taxonomy with a Codex-generated domain summary and workflow map. Scenario and dataset generation use this profile as their source of truth.
