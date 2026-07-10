# Concepts

## Spec (`ghostlab.yaml`)

The spec is the canonical, human-editable description of one agent under test:
its composed inputs (`agent`), primary discovery target (`target`), isolation
boundary (`sandbox`), setup, and agent hosts that
exercise it (`hosts`), what it exposes (`capabilities`, refreshed by
`ghostlab discover`), and what quality bar it must clear (`review.gates`).
`ghostlab init` creates it from a target JSON; discovery artifacts (inventory,
contract lint, MCP Apps probes) accumulate under its workspace directory
(default `.ghostlab/`). One configured agent is the intended unit of QA.

## Agent

`agent` is the canonical evaluation subject:

- `runner`: any process/coding-agent command Ghostlab can drive turn by turn.
- `instructions`: agent-level behavior and policy.
- `inputs.mcps`: zero or more native MCP definitions or normalized config references.
- `inputs.skills`: zero or more `SKILL.md` inputs.
- `inputs.assets` and `workspace`: files staged into the sandbox.

MCP-only and skill-only jobs are normalized into this shape. Discovery and
automatic scenario generation currently use the first MCP or skill as the
primary capability, while execution receives the complete composition.

## Sandbox

NVIDIA OpenShell is the default runtime. Ghostlab creates one policy-enforced
sandbox per runner session, stages declared files, executes turns with stdin
and exit-code propagation, captures OpenShell logs, and deletes the sandbox at
the end unless `keep: true` is configured.

The lifecycle is:

1. Normalize the agent's sandbox declaration and resolve relative paths.
2. Create a named OpenShell sandbox from `image` with the policy, resource
   limits, providers, and uploads fixed at creation time.
3. Execute each runner turn with `openshell sandbox exec --no-tty`, the declared
   `workdir`, and the filtered environment.
4. Download requested artifacts, retain `openshell-<role>.log`, and delete the
   sandbox unless debugging requested `keep: true`.

The agent under test and user emulator receive different sandboxes. A local
stdio MCP spawned by the job pipeline is also rewritten through OpenShell; a remote
HTTP/SSE MCP remains remote and is reached only when the policy/provider permits
that egress.

`network: disabled` adds no user-authored egress rules; OpenShell remains
default-deny except for rules contributed by attached providers. Set
`network: policy` with an OpenShell policy file for explicit additional egress.
Only variables named in `env_allowlist` are
copied from the parent environment; internal `GHOSTLAB_`/`REHEARSAL_` values are
added by the harness. `backend: local` is an explicit unsandboxed compatibility
mode and is never an automatic fallback.

For managed credentials, list existing OpenShell provider names under
`sandbox.providers`; Ghostlab attaches them during sandbox creation. With no
providers it uses non-interactive `--no-auto-providers`, so credentials must be
supplied through the explicit environment allowlist or configured inference.

```yaml
sandbox:
  backend: openshell
  image: base
  workdir: /sandbox
  network: disabled
  providers: []
  env_allowlist: []
  uploads: []
  keep: false
```

`backend: local` runs the process directly on the host. Select it with
`--sandbox local` for trusted compatibility work. It is never selected as a
fallback after an OpenShell error.

The standalone `ghostlab inspect` command is intentionally a low-level direct
MCP client and does not consume a job sandbox declaration. Use
`ghostlab create`/`ghostlab discover` for untrusted local stdio MCP code.

## Contract

The contract (`contract.json`) is the deterministic judgment of the spec's
discovered surface: schema-quality findings, risk labels per tool (read-only /
mutates-state / destructive / credential-bearing / ui-producing, from MCP tool
`annotations` first and name heuristics second), and MCP Apps metadata
compatibility checks. It is regenerated on every `discover` and is the input
for coverage-driven test planning.

## Target

A target describes the primary MCP or skill used for discovery and planning:

- `id`: stable target identifier.
- `transport`: `stdio`, `sse`, `streamable-http`, or `skill`.
- `connection`: command and environment for stdio, or URL and headers for network transports.
- `capabilities`: optional expected tools/resources/prompts.
- `startup`: optional boot and health-check settings.

For a skill, `target.kind` and `transport` are `skill`, while
`connection.path` points to `SKILL.md`. Skill evaluation uses the conversational
harness and judge; direct MCP protocol checks do not apply.

## Scenario

A scenario is the task the user emulator tries to accomplish. It includes a goal, success criteria, failure signals, stop conditions, and an optional `exercises` list that names tools the assistant should be driven to use.

## Persona

A persona is the user identity used during a run. Persona files hold durable traits and context, while a scenario's inline persona text should stay situational.

## Dataset

A dataset is a persona x scenario matrix. It contains a manifest, generated personas, generated scenarios, and runnable cases with curation status values like `pending`, `approved`, `rejected`, and `needs-edit`.

## Runner

A runner controls how Ghostlab talks to an agent host. Mock runners are deterministic. Process runners start a fresh command per turn. Codex session runners keep one live Codex thread across turns by resuming the thread id captured from JSONL output.

## Permissions and approvals

There are two separate layers. Host-level approval mode (for example Codex
`-a never` and its sandbox) determines whether the AUT process may execute a
tool at all; the user emulator cannot override that layer. An assistant may
also ask the user a conversational permission question. The emulator answers
that question in persona: it normally approves reversible actions needed for
the goal, but asks or refuses when an action is destructive, exposes private
data or credentials, costs money, or conflicts with the persona. It never
discusses host flags or the test harness. Host auto-denials and conversational
user refusals are logged separately as `permission_denied` tool failures versus
ordinary user messages.

## Outputs

Each run captures:

- Full user and assistant transcript.
- Structured MCP tool-call events when available.
- Raw stderr for debugging host warnings.
- Clean assistant stdout for conversational handoff.
- Markdown reports and dataset summaries.
