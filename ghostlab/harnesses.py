"""One agent definition, translated into each harness's headless command line.

A :class:`Harness` knows four things about its CLI: how to build the command
(model, MCPs, options), how to resume a session, how its output is parsed,
and which host files it must find inside a sandbox. Everything a harness
cannot load natively — instruction files and skills, for all but OpenCode —
is rendered into a preamble ahead of the task, so the same definition means
the same thing everywhere.

Harness-specific settings live under ``options`` in the agent config; every
harness accepts ``bin`` (host binary) and ``extra_args`` (appended verbatim).
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from .parsers import PARSERS, Parsed

if TYPE_CHECKING:
    from .agent import MCP, Agent

PathMap = Callable[[Any], str]

# Marks coding-agent sessions as synthetic so rollout collectors can skip them.
CODEX_ORIGINATOR_ENV = "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"
GHOSTLAB_COPILOT_SESSION_PREFIX = "67686f73-746c-"  # the bytes "ghostl"


def new_copilot_session_id() -> str:
    value = bytearray(uuid.uuid4().bytes)
    value[:6] = b"ghostl"
    return str(uuid.UUID(bytes=bytes(value)))


@dataclass
class Invocation:
    """A fully resolved harness command, before the box runs it."""

    argv: list[str]
    stdin: bool = True  # False: the prompt is appended as the last argument
    env: dict[str, str] = field(default_factory=dict)
    parser: str = "text"


def _strings(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    return [str(value)] if isinstance(value, str) else [str(v) for v in value]


def _read(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def _expand(mapping: Any) -> dict[str, str]:
    return {str(k): os.path.expandvars(str(v)) for k, v in dict(mapping or {}).items()}


def mcp_servers(mcps: Sequence["MCP"], inside: PathMap, *, stdio_type: str,
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ``{"mcpServers": ...}`` document Claude Code and Copilot both read."""
    servers: dict[str, Any] = {}
    for mcp in mcps:
        if mcp.transport == "stdio":
            command = [inside(part) for part in mcp.command]
            entry: dict[str, Any] = {"type": stdio_type, "command": command[0], "args": command[1:]}
            if mcp.env:
                entry["env"] = _expand(mcp.env)
        else:
            entry = {"type": mcp.transport, "url": mcp.url}
            if mcp.headers:
                entry["headers"] = _expand(mcp.headers)
        servers[mcp.name] = {**entry, **(extra or {})}
    return {"mcpServers": servers}


class Harness:
    name = ""
    binary = ""
    native_context = False  # loads instruction files and skills itself

    def prompt(self, agent: "Agent", task: str) -> str:
        sections = [agent.instructions.strip()] if agent.instructions.strip() else []
        if not self.native_context:
            sections += [_read(path) for path in agent.instruction_files]
            sections += [f"## Skill: {p.parent.name}\n\n{_read(p)}" for p in agent.skills]
        if not sections:
            return task
        preamble = "\n\n".join(sections)
        return (
            "You are operating as a configured agent. Follow these instructions and "
            f"use these skills when they apply.\n\n{preamble}\n\n---\n\nTask:\n{task}"
        )

    def bin(self, agent: "Agent", sandboxed: bool) -> str:
        # Inside a sandbox the image provides the CLI; a host path means nothing there.
        return self.binary if sandboxed else str(agent.options.get("bin") or self.binary)

    def invocation(self, agent: "Agent", *, workdir: str, inside: PathMap,
                   sandboxed: bool) -> Invocation:
        raise NotImplementedError

    def resume(self, invocation: Invocation, session_id: str, sandboxed: bool) -> Invocation:
        return invocation

    def parse(self, invocation: Invocation, stdout: str, agent: "Agent") -> Parsed:
        return PARSERS[invocation.parser](stdout, [m.name for m in agent.mcps])

    def agent_files(self, agent: "Agent") -> list[Path]:
        """Host files the harness reads by path, so they must be in the sandbox."""
        return []

    def egress(self, agent: "Agent") -> tuple[list[str], list[str]] | None:
        """(hosts, binaries) for a generated egress policy, when none is configured."""
        return None


class Claude(Harness):
    name = binary = "claude"

    def invocation(self, agent, *, workdir, inside, sandboxed):
        o = agent.options
        argv = [self.bin(agent, sandboxed), "-p", "--output-format", "stream-json", "--verbose"]
        if agent.model:
            argv += ["--model", agent.model]
        if o.get("permission_mode"):
            argv += ["--permission-mode", str(o["permission_mode"])]
        if o.get("max_turns"):
            argv += ["--max-turns", str(o["max_turns"])]
        # Headless Claude Code denies anything needing approval, so the agent's
        # own MCPs are pre-approved; built-ins follow allow_tools.
        allowed = [f"mcp__{m.name}" for m in agent.mcps] + _strings(o.get("allow_tools"))
        if allowed:
            argv += ["--allowedTools", ",".join(dict.fromkeys(allowed))]
        if o.get("deny_tools"):
            argv += ["--disallowedTools", ",".join(_strings(o["deny_tools"]))]
        if agent.mcps:
            config = mcp_servers(agent.mcps, inside, stdio_type="stdio")
            argv += ["--mcp-config", json.dumps(config, separators=(",", ":")), "--strict-mcp-config"]
        return Invocation([*argv, *_strings(o.get("extra_args"))], parser="claude")

    def resume(self, invocation, session_id, sandboxed):
        return replace(invocation, argv=[*invocation.argv, "--resume", session_id])


_BEARER_ENV = re.compile(r"Bearer\s+\$\{?(\w+)\}?")
_ENV_REF = re.compile(r"\$\{?(\w+)\}?")


def codex_mcp_overrides(mcps: Sequence["MCP"], inside: PathMap) -> tuple[list[str], dict[str, str]]:
    """``-c mcp_servers.<name>...`` overrides, plus env vars codex must read.

    Codex wants ``command`` as a string and ``args`` as a list. A ``Bearer
    ${VAR}`` header maps to ``bearer_token_env_var`` and a header that is only
    ``${VAR}`` to ``env_http_headers``, so secrets are read inside codex rather
    than baked into its argv; the referenced variables travel in the env.
    """
    args: list[str] = []
    env: dict[str, str] = {}

    def forward(variable: str) -> None:
        if variable in os.environ:
            env[variable] = os.environ[variable]

    for mcp in mcps:
        prefix = f"mcp_servers.{mcp.name}"
        if mcp.transport == "stdio":
            command = [inside(part) for part in mcp.command]
            args += ["-c", f"{prefix}.command={json.dumps(command[0])}",
                     "-c", f"{prefix}.args={json.dumps(command[1:])}"]
            for key, value in mcp.env.items():
                args += ["-c", f"{prefix}.env.{key}={json.dumps(os.path.expandvars(value))}"]
            continue
        args += ["-c", f"{prefix}.url={json.dumps(mcp.url)}"]
        for name, value in mcp.headers.items():
            bearer = _BEARER_ENV.search(value)
            only_env = _ENV_REF.fullmatch(value.strip())
            if bearer and name.lower() == "authorization":
                args += ["-c", f"{prefix}.bearer_token_env_var={json.dumps(bearer.group(1))}"]
                forward(bearer.group(1))
            elif only_env:
                args += ["-c", f"{prefix}.env_http_headers.{name}={json.dumps(only_env.group(1))}"]
                forward(only_env.group(1))
            else:
                args += ["-c", f"{prefix}.http_headers.{name}={json.dumps(os.path.expandvars(value))}"]
    return args, env


class Codex(Harness):
    name = binary = "codex"

    def invocation(self, agent, *, workdir, inside, sandboxed):
        o = agent.options
        argv = [self.bin(agent, sandboxed),
                "--sandbox", str(o.get("sandbox_mode") or "workspace-write"),
                "-a", str(o.get("approval") or "never")]
        if agent.model:
            argv += ["-m", agent.model]
        if o.get("reasoning_effort"):
            argv += ["-c", f"model_reasoning_effort={json.dumps(str(o['reasoning_effort']))}"]
        overrides, env = codex_mcp_overrides(agent.mcps, inside)
        argv += [*overrides, "-C", workdir, *_strings(o.get("extra_args")),
                 "exec", "--json", "--skip-git-repo-check", "-"]
        return Invocation(argv, env={CODEX_ORIGINATOR_ENV: "ghostlab", **env}, parser="codex")

    def resume(self, invocation, session_id, sandboxed):
        argv = list(invocation.argv)
        at = argv.index("exec") + 1
        argv[at:at] = ["resume", session_id]
        return replace(invocation, argv=argv)


# Copilot options: agent-config key -> CLI flag.
_COPILOT_VALUES = {
    "agent": "--agent", "reasoning_effort": "--reasoning-effort", "mode": "--mode",
    "max_autopilot_continues": "--max-autopilot-continues", "log_level": "--log-level",
}
_COPILOT_FLAGS = {
    "allow_all": "--allow-all", "allow_all_paths": "--allow-all-paths",
    "allow_all_urls": "--allow-all-urls", "enable_memory": "--enable-memory",
    "no_custom_instructions": "--no-custom-instructions", "experimental": "--experimental",
}
_COPILOT_REPEATED = {
    "add_dirs": "--add-dir", "allow_tools": "--allow-tool", "deny_tools": "--deny-tool",
    "allow_urls": "--allow-url", "deny_urls": "--deny-url", "plugin_dirs": "--plugin-dir",
}


class Copilot(Harness):
    name = binary = "copilot"

    def invocation(self, agent, *, workdir, inside, sandboxed):
        o = agent.options
        argv = [self.bin(agent, sandboxed), "--output-format", "json", "--stream", "off",
                "--no-color", "--no-auto-update", "--no-ask-user", "--disable-builtin-mcps"]
        if not sandboxed:
            # The CLI pinned in OpenShell's base image predates --no-remote and
            # --session-id; each sandbox holds one session, so --continue resumes it.
            argv += ["--no-remote", "--session-id", new_copilot_session_id()]
        if agent.model:
            argv += ["--model", agent.model]
        for key, flag in _COPILOT_VALUES.items():
            if o.get(key) not in (None, ""):
                argv += [flag, str(o[key])]
        argv += [flag for key, flag in _COPILOT_FLAGS.items() if o.get(key)]
        if not o.get("allow_all") and o.get("allow_all_tools", True):
            argv.append("--allow-all-tools")
        for key, flag in _COPILOT_REPEATED.items():
            for value in _strings(o.get(key)):
                argv += [flag, value]
        if agent.mcps:
            config = mcp_servers(agent.mcps, inside, stdio_type="local", extra={"tools": ["*"]})
            argv += ["--additional-mcp-config", json.dumps(config, separators=(",", ":"))]
        argv += [*_strings(o.get("extra_args")), "--prompt"]
        return Invocation(argv, stdin=False, parser="copilot")

    def resume(self, invocation, session_id, sandboxed):
        if not sandboxed:
            return invocation  # the same --session-id continues the conversation
        argv = list(invocation.argv)
        argv.insert(argv.index("--prompt"), "--continue")
        return replace(invocation, argv=argv)

    def parse(self, invocation, stdout, agent):
        parsed = super().parse(invocation, stdout, agent)
        if "--session-id" in invocation.argv:
            parsed.session_id = invocation.argv[invocation.argv.index("--session-id") + 1]
        return parsed


# OpenCode resolves `provider/model` against a catalog it fetches at startup.
OPENCODE_CATALOG = ["models.dev", "api.models.dev", "opencode.ai"]
PROVIDER_ENDPOINTS = {
    "github-copilot": ["api.github.com", "api.githubcopilot.com",
                       "proxy.individual.githubcopilot.com", "copilot-proxy.githubusercontent.com"],
    "anthropic": ["api.anthropic.com"],
    "openai": ["api.openai.com"],
    "azure": ["management.azure.com"],
}
# Matches docker/agent-sandbox.Dockerfile: OpenCode state lives outside the
# workspace, so an `edit`-permitted agent cannot read or rewrite its own auth.
OPENCODE_SANDBOX_ENV = {
    "XDG_DATA_HOME": "/opt/agent/share",
    "XDG_CONFIG_HOME": "/opt/agent/config",
    "XDG_CACHE_HOME": "/opt/agent/cache",
}
# An unconfigured agent cannot reach around the capabilities it was given.
OPENCODE_DEFAULT_PERMISSION = {"bash": "deny", "edit": "deny", "webfetch": "deny",
                               "external_directory": "deny"}


class OpenCode(Harness):
    """OpenCode reads instructions, skills, and MCPs from its own config,
    which travels inline as ``OPENCODE_CONFIG_CONTENT``. ``options.config``
    is merged into it verbatim (agents, permission, provider, ...)."""

    name = binary = "opencode"
    native_context = True

    def project_config(self, agent: "Agent", inside: PathMap) -> dict[str, Any]:
        extra = dict(agent.options.get("config") or {})
        config: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,  # never spend a turn upgrading itself
            **extra,
            "permission": {**OPENCODE_DEFAULT_PERMISSION, **dict(extra.get("permission") or {})},
        }
        if agent.instruction_files:
            config["instructions"] = [inside(p) for p in agent.instruction_files]
        if agent.skills:
            skills = dict(config.get("skills") or {})
            skills["paths"] = [*skills.get("paths", []), *(inside(p.parent) for p in agent.skills)]
            config["skills"] = skills
        mcp: dict[str, Any] = dict(config.get("mcp") or {})
        for server in agent.mcps:
            if server.transport == "stdio":
                entry: dict[str, Any] = {"type": "local", "enabled": True,
                                         "command": [inside(p) for p in server.command]}
                if server.env:
                    entry["environment"] = _expand(server.env)
            else:
                entry = {"type": "remote", "enabled": True, "url": server.url}
                if server.headers:
                    entry["headers"] = _expand(server.headers)
            mcp[server.name] = entry
        if mcp:
            config["mcp"] = mcp
        return config

    def invocation(self, agent, *, workdir, inside, sandboxed):
        argv = [self.bin(agent, sandboxed), "run", "--format", "json", "--log-level", "ERROR"]
        if agent.model:
            argv += ["--model", agent.model]
        if agent.options.get("default_agent"):
            argv += ["--agent", str(agent.options["default_agent"])]
        argv += ["--dir", workdir, *_strings(agent.options.get("extra_args"))]
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps(self.project_config(agent, inside))}
        if sandboxed:
            env.update(OPENCODE_SANDBOX_ENV)
        return Invocation(argv, env=env, parser="opencode")

    def resume(self, invocation, session_id, sandboxed):
        return replace(invocation, argv=[*invocation.argv, "--session", session_id])

    def agent_files(self, agent):
        return [*agent.instruction_files, *(p.parent for p in agent.skills)]

    def egress(self, agent):
        provider = agent.model.split("/", 1)[0]
        if provider not in PROVIDER_ENDPOINTS:
            from .sandbox import SandboxError

            raise SandboxError(
                "sandbox_policy_missing",
                f"no known egress endpoints for OpenCode provider {provider or '(unset)'!r}; "
                "set sandbox.policy to an OpenShell policy file, or use a model from: "
                + ", ".join(sorted(PROVIDER_ENDPOINTS)),
            )
        return OPENCODE_CATALOG + PROVIDER_ENDPOINTS[provider], ["/usr/bin/node", "/usr/bin/opencode"]


class Command(Harness):
    """Any headless CLI: ``options.command`` is the argv, ``options.prompt`` is
    ``stdin`` (default) or ``arg``, and ``options.output`` names the parser
    (``text`` by default, or another harness's stream format)."""

    name = "command"

    def invocation(self, agent, *, workdir, inside, sandboxed):
        o = agent.options
        mode = str(o.get("prompt") or "stdin")
        if mode not in ("stdin", "arg"):
            raise ValueError(f"options.prompt must be 'stdin' or 'arg', not {mode!r}")
        output = str(o.get("output") or "text")
        if output not in PARSERS:
            raise ValueError(f"options.output must be one of {', '.join(PARSERS)}")
        return Invocation([inside(p) for p in _strings(o["command"])], stdin=mode == "stdin",
                          parser=output)

    def agent_files(self, agent):
        from .sandbox import program_uploads

        return program_uploads(_strings(agent.options["command"]))


HARNESSES: dict[str, Harness] = {
    h.name: h for h in (Claude(), Codex(), Copilot(), OpenCode(), Command())
}
