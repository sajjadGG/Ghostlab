# Ghostlab

Ghostlab runs coding agents headlessly and composes them into checkpointed,
resumable jobs.

- **One agent definition, any harness.** Claude Code, Codex, GitHub Copilot
  CLI, OpenCode, or any CLI you point it at. Instructions, skills, MCP servers,
  and a workspace mean the same thing in each.
- **Sandboxed by default.** The agent CLI, its stdio MCPs, and a copy of its
  workspace run inside an NVIDIA OpenShell sandbox that is deleted afterwards.
  Running on the host (`sandbox: local`) must be asked for.
- **Pipelines in plain Python.** Stages map, fan out, or gather items; each
  stage calls agents through a context that caches every call. Re-running a
  pipeline resumes it.

Ghostlab is the infrastructure layer. Evaluations, labeling runs, test
generation, and migrations are pipelines you write on top of it. See
[the MCP evaluation example](https://github.com/sajjadGG/Ghostlab/blob/main/examples/mcp-eval/pipeline.py).

| Page | Read it for |
| --- | --- |
| [Getting started](getting-started.md) | install, run one agent, run one pipeline |
| [Agents](agents.md) | the agent config, every harness's options, the sandbox |
| [Pipelines](pipelines.md) | stages, the context, checkpointing and resume |
| [CLI](cli.md) | `ghostlab exec`, `run`, `status` |
| [Publishing](publishing.md) | releasing the package and this wiki |
