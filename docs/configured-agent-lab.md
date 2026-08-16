# Configured Agent Lab

Status: proposed · Owner: TBD · Supersedes nothing; extends the job model.

## Problem

Ghostlab evaluates an **MCP server**. The agent that exercises it is a thin,
largely fixed wrapper — one CLI process with one MCP injected and whatever
model default the CLI happens to carry.

That is not the thing people ship. A real agent is a *configuration*: a model, a
system prompt, instruction files, skills, a set of MCPs, tool permissions,
subagents, custom commands, and a working directory of code it operates on.
Ghostlab cannot express any of that, so it cannot answer the question users
actually have — **"does the agent I configured behave correctly?"**

Three concrete gaps:

1. **No agent configuration surface.** `agent.runner.command` is a raw argv
   list. Setting a model means hand-editing CLI flags; skills, instructions,
   permissions, subagents, and multi-MCP composition have no representation.
2. **Generation is tool-shaped.** Profile, persona, and scenario generation read
   `inspect.json` — the MCP tool inventory. An agent whose purpose lives in its
   instructions and skills (a release assistant, a support triager) produces
   scenarios about tool families instead of about its job.
3. **The agent runs on the host.** Only the MCP is sandboxed. An agent
   configured with `bash` or `edit` permissions acts on the developer's machine
   — precisely the configuration most worth testing, and the least safe to run.

## Goals

1. Express the **full** agent configuration the harness accepts (OpenCode
   first), including the code path the agent runs against.
2. Infer the agent's **purpose** from that configuration — optionally guided by
   a user-supplied description — and drive profile, persona, and scenario
   generation from it.
3. An **interactive, LLM-guided** setup that walks the user from "here is my
   agent" to "here are the scenarios I want to test it with".
4. Run **everything in the sandbox**: the agent CLI, its MCPs, and its code.
   Nothing touches the host machine.
5. Judge the result, and emit a **complete rollout document** (including PDF)
   covering configuration, generation, transcript, and verdict.

## Non-goals

- Codex parity for the full configuration surface. Codex keeps today's
  behaviour; the rich surface targets OpenCode, whose config is declarative.
- Giving the **user emulator** tools or MCPs. It plays a human and stays
  deliberately tool-free — that separation is load-bearing.
- Replacing the MCP-only and skill-only job types. They remain shorthands that
  normalize into the same agent model.

## Design

### 1. Agent configuration

The full OpenCode configuration surface is declarative
(`https://opencode.ai/config.json`), so Ghostlab mirrors it rather than
inventing a parallel vocabulary. `agent.runtime` gains an OpenCode block whose
keys map 1:1 onto that schema:

```yaml
agent:
  id: release-bot
  name: Release Bot
  description: |          # optional; the user's own words about the purpose
    Helps maintainers cut a release: reads the changelog, checks CI, drafts
    release notes. Must never push or tag without explicit confirmation.
  runtime:
    backend: opencode
    model: github-copilot/claude-sonnet-4.5
    small_model: github-copilot/claude-haiku-4.5
    default_agent: release
    subagent_depth: 1
    instructions: [AGENTS.md, docs/release-policy.md]
    skills:
      paths: [./skills/release-notes]
      urls: []
    agents:                       # OpenCode `agent` map
      release:
        description: Cuts releases
        prompt: ./prompts/release.md
        model: github-copilot/claude-sonnet-4.5
        temperature: 0.2
        steps: 30
        permission: { bash: ask, edit: allow }
    tools: { webfetch: false }
    permission: { bash: deny, edit: allow, external_directory: deny }
    commands: { changelog: { ... } }
    provider: { ... }
    plugins: []
  workspace: ./release-repo       # code the agent operates on
  inputs:
    mcps:
      - config_ref: ./mcp.json
        server: github
      - config_ref: ./mcp.json
        server: filesystem
    skills: [./skills/release-notes]
    assets: [./fixtures/changelog.md]
```

Rules:

- **Validated against a vendored subset of the OpenCode schema.** An unknown or
  misspelled key fails at load with the offending path, rather than being
  silently dropped into a config OpenCode ignores.
- **Every referenced path is an input.** Instruction files, prompt files, skill
  folders, and the workspace are resolved relative to the agent config and
  become sandbox uploads.
- **Multi-MCP is native.** `inputs.mcps` already accepts a list; the OpenCode
  project config gets one entry per server.
- The existing shorthands (`--target` for MCP-only, `--skill` for skill-only)
  keep working and normalize into this shape.

### 2. Purpose inference

New module `rehearsal/agent_profile.py`. Instead of profiling a tool inventory,
it profiles the *agent*, reading:

- the user's `description`, when given (authoritative — never overridden);
- the contents of `instructions` files and each skill's `SKILL.md`;
- subagent prompts and the tool/permission posture;
- the MCP tool inventory from `discover`, when MCPs are attached;
- the workspace's shape (top-level layout, README title) — not its full contents.

It produces `agent-profile.json`:

```json
{
  "purpose": "...", "audience": "...",
  "workflows": [{ "name": "...", "steps": ["..."], "capabilities_used": ["..."] }],
  "risk_surface": [{ "risk": "...", "why": "...", "capability": "..." }],
  "out_of_scope": ["..."],
  "evidence": { "from_description": true, "instructions": ["AGENTS.md"], "skills": ["..."] }
}
```

Persona and scenario generation switch to agent-aware prompt templates that take
this profile as ground truth. `risk_surface` seeds the adversarial/security
scenarios directly, which is a strict improvement over today's name heuristics.
Existing MCP-only jobs keep the current tool-shaped path.

### 3. Interactive setup: `ghostlab lab`

A guided, resumable, LLM-assisted flow. Each step shows a proposal, and the user
accepts, edits, or regenerates it. Every answer is written to `job.yaml`, so the
end state is a reproducible file rather than a transcript.

| Step | What happens |
| --- | --- |
| 1 Source | Import an existing `opencode.json`/project, an `agent.yaml`, or start from scratch |
| 2 Purpose | User describes the agent, or accepts an inferred draft |
| 3 Model | Pick provider/model from what is actually authenticated |
| 4 MCPs | Import from a standard `mcpServers` config; enable per server; `discover` runs for each |
| 5 Skills & instructions | Attach folders/files; each is previewed |
| 6 Permissions & tools | Safe defaults shown, with the blast radius of each spelled out |
| 7 Sandbox | Image, network policy, and the explicit credential opt-in |
| 8 Profile | Review the inferred purpose, workflows, and risk surface |
| 9 Personas | Generated from the profile; add, edit, drop, regenerate |
| 10 Scenarios | Per persona; each is editable, with success criteria and failure signals |
| 11 Gates | Pass rate and release gates |
| 12 Preview & run | Full resolved config, then execute |

`--yes` plus flags drives the same flow non-interactively for CI. `--resume`
continues a partially configured lab.

### 4. Full sandboxing

Today only the MCP process is sandboxed. This closes the gap.

- **Image.** Ghostlab ships `docker/agent-sandbox.Dockerfile` (Linux, Node, the
  OpenCode CLI). `sandbox.image` accepts a community name, an image reference,
  or a path to a Dockerfile — OpenShell's `--from` builds it. The host's own
  OpenCode binary is *not* uploaded: it is a platform-specific binary and will
  not run in the container.
- **Agent execution.** The runner command becomes the SSH-wrapped
  `opencode run …` inside the sandbox, reusing the transport already proven for
  stdio MCPs. The agent's MCPs are launched by OpenCode *inside* the same
  container, so they are sandboxed by construction.
- **Code.** `workspace` uploads to `/sandbox/workspace` and becomes the agent's
  working directory, so `edit`/`bash` permissions act on a copy, never on the
  user's checkout.
- **Credentials.** Running a real model from inside the sandbox needs a
  provider credential in the sandbox. This is an explicit opt-in:
  `sandbox.credentials.opencode_auth: true` uploads the OpenCode auth file to
  the container's data dir. Without it the run fails fast and says why. The
  value is never logged, never printed, and is redacted from every report.
- **Network.** Default deny. Ghostlab generates a policy allowing only the
  provider endpoints the chosen model needs, plus any declared remote MCP
  hosts. Anything else the agent reaches for is denied and shows up in the
  sandbox log — which is itself a finding.

### 5. Rollout report and PDF

Runs already emit `events.jsonl`, `report.md`, `verdict.json`, and
`critique.md`. This adds a single document that assembles the whole rollout:

1. Resolved agent configuration (secrets redacted) and sandbox provenance
2. Inferred purpose profile, with its evidence
3. Personas and scenarios, with why each was generated
4. Per-turn transcript, with every tool call, arguments, result, and latency
5. Judge verdict with per-criterion evidence, plus deterministic checks
6. Tool-usability critique
7. Gates and the readiness verdict

`rehearsal/report_pdf.py` renders it to a self-contained HTML document and then
to PDF through the Chrome/Playwright path the MCP Apps host already uses
(`ghostlab[apps]`). Without that extra it writes the HTML and says so, rather
than failing. Exposed as `ghostlab report --job <name> --pdf out.pdf` and as
`--pdf` on `ghostlab test`.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Provider credentials inside a container | Explicit opt-in, never logged, redacted in reports, documented blast radius |
| An agent with `bash`/`edit` doing damage | That is the point of the sandbox; the workspace is a copy and network is default-deny |
| Image build time on first run | Docker layer cache; the image is built once and reused |
| OpenCode config drift | Vendored schema subset plus a test that fails when a config key we emit is not in the published schema |
| LLM-guided setup producing plausible nonsense | Every generated artifact is shown and editable before it is used; the user's own description always wins |

## Milestones

- **M1** Agent configuration schema, validation, and OpenCode project emission
- **M2** Purpose inference feeding profile/persona/scenario generation
- **M3** In-sandbox agent execution: Dockerfile, credentials, network policy
- **M4** Interactive `ghostlab lab` wizard
- **M5** Rollout report and PDF
- **M6** Documentation and an end-to-end verified example

## Acceptance

A user can point Ghostlab at an agent configuration with a model, instructions,
a skill, two MCPs, and a code workspace; answer a guided setup; and get a run
where the agent, its MCPs, and its code all executed inside OpenShell, judged,
with a PDF containing the complete rollout — and confirm from the sandbox log
that nothing touched the host.
