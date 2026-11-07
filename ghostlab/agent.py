"""The agent definition: one config that every harness can run.

An agent is a harness (Claude Code, Codex, Copilot CLI, OpenCode, or any
command) plus what it is given to work with: a model, instructions, skills,
MCP servers, a workspace, environment, and the sandbox it runs in. Load it
from YAML/JSON or build it in Python; either way it is immutable, and
``with_`` derives variants for a stage that needs a different model or an
extra MCP.

```yaml
name: reviewer
harness: claude
model: sonnet
instructions: Be concise.
skills: [skills/review]
mcps:
  docs: {command: node, args: [docs-mcp.js]}
  hub:  {url: "https://hub.example/mcp", headers: {Authorization: "Bearer ${HUB_TOKEN}"}}
workspace: repo
sandbox: {backend: openshell, providers: [anthropic]}
```
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from ._util import load_file
from .sandbox import Sandbox

if TYPE_CHECKING:
    from .session import Result, Session

HARNESSES = ("claude", "codex", "copilot", "opencode", "command")


class ConfigError(ValueError):
    """An agent, sandbox, or pipeline config is invalid."""


def _resolve(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


@dataclass(frozen=True)
class MCP:
    """One MCP server, in the ``mcpServers`` shape every client already uses."""

    name: str
    command: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    url: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    transport: str = "stdio"  # stdio | http | sse

    @classmethod
    def parse(cls, name: str, spec: Mapping[str, Any], base: Path | None = None) -> "MCP":
        if not isinstance(spec, Mapping):
            raise ConfigError(f"mcps.{name} must be a mapping")
        if spec.get("url"):
            kind = str(spec.get("type") or spec.get("transport") or "http").lower()
            return cls(
                name=name, url=str(spec["url"]),
                headers={str(k): str(v) for k, v in dict(spec.get("headers") or {}).items()},
                transport="sse" if kind == "sse" else "http",
            )
        raw = spec.get("command")
        if not raw:
            raise ConfigError(f"mcps.{name} needs a 'command' (stdio) or a 'url' (http/sse)")
        parts = [raw] if isinstance(raw, str) else list(raw)
        parts += list(spec.get("args") or [])
        if base is not None:
            # `node server.js` beside the config means that file, wherever we run from.
            parts = [str(base / p) if (base / str(p)).is_file() and not Path(str(p)).is_absolute()
                     else str(p) for p in parts]
        return cls(
            name=name, command=tuple(str(p) for p in parts),
            env={str(k): str(v) for k, v in dict(spec.get("env") or {}).items()},
        )

    def to_dict(self) -> dict[str, Any]:
        if self.transport == "stdio":
            return {"command": list(self.command), "env": dict(self.env)}
        return {"url": self.url, "headers": dict(self.headers), "type": self.transport}


def _parse_mcps(raw: Any, base: Path) -> tuple[MCP, ...]:
    if isinstance(raw, (list, tuple)) and all(isinstance(m, MCP) for m in raw):
        return tuple(raw)
    if not isinstance(raw, Mapping):
        raise ConfigError("mcps must be a mapping of server name to server config")
    servers = raw.get("mcpServers", raw)
    return tuple(MCP.parse(str(name), spec, base) for name, spec in servers.items())


def _skill_path(value: Any, base: Path) -> Path:
    path = _resolve(value, base)
    if path.is_dir():
        path = path / "SKILL.md"
    if not path.is_file():
        raise ConfigError(f"skill not found: {path}")
    return path


@dataclass(frozen=True)
class Agent:
    harness: str
    name: str = "agent"
    model: str = ""
    instructions: str = ""
    instruction_files: tuple[Path, ...] = ()
    skills: tuple[Path, ...] = ()
    mcps: tuple[MCP, ...] = ()
    workspace: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)
    timeout: int = 900
    sandbox: Sandbox = field(default_factory=Sandbox)

    def __post_init__(self) -> None:
        # Accept plain values from Python callers; config files go through from_dict.
        cwd = Path.cwd()
        normalized = {
            "mcps": _parse_mcps(self.mcps, cwd) if self.mcps else (),
            "skills": tuple(Path(p) for p in self.skills),
            "instruction_files": tuple(Path(p) for p in self.instruction_files),
            "workspace": _resolve(self.workspace, cwd) if self.workspace else None,
            "sandbox": self.sandbox if isinstance(self.sandbox, Sandbox)
            else Sandbox.from_dict(self.sandbox, cwd),
        }
        for name, value in normalized.items():
            object.__setattr__(self, name, value)
        if self.harness not in HARNESSES:
            raise ConfigError(
                f"unknown harness {self.harness!r}; choose one of {', '.join(HARNESSES)}"
            )
        if self.harness == "command" and not self.options.get("command"):
            raise ConfigError("harness 'command' needs options.command (the argv to run)")
        if self.workspace is not None and not self.workspace.is_dir():
            raise ConfigError(f"workspace directory not found: {self.workspace}")

    # -- construction ------------------------------------------------------ #
    @classmethod
    def load(cls, path: str | Path, **overrides: Any) -> "Agent":
        """Load an agent from YAML/JSON; relative paths resolve against the file."""
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise ConfigError(f"agent config not found: {path}")
        data = load_file(path)
        if not isinstance(data, Mapping):
            raise ConfigError(f"{path} must contain a mapping")
        return cls.from_dict({"name": path.stem, **data, **overrides}, base=path.parent)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], base: Path | None = None) -> "Agent":
        base = Path(base or Path.cwd()).resolve()
        data = dict(data)
        known = {f.name for f in dataclasses.fields(cls)} | {"mcp_config"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(f"unknown agent field(s): {', '.join(unknown)}")
        if "harness" not in data:
            raise ConfigError(f"agent config needs a harness: {', '.join(HARNESSES)}")
        mcps: tuple[MCP, ...] = ()
        if data.get("mcp_config"):
            ref = _resolve(data.pop("mcp_config"), base)
            mcps += _parse_mcps(load_file(ref), ref.parent)
        data.pop("mcp_config", None)
        if data.get("mcps"):
            mcps += _parse_mcps(data["mcps"], base)
        data["mcps"] = mcps
        files = data.get("instruction_files") or []
        data["instruction_files"] = tuple(
            _resolve(item, base) for item in ([files] if isinstance(files, str) else files)
        )
        data["skills"] = tuple(_skill_path(item, base) for item in data.get("skills") or [])
        if data.get("workspace"):
            data["workspace"] = _resolve(data["workspace"], base)
        if "sandbox" in data and not isinstance(data["sandbox"], Sandbox):
            data["sandbox"] = Sandbox.from_dict(data["sandbox"], base)
        return cls(**data)

    def with_(self, **changes: Any) -> "Agent":
        """A copy with fields replaced, e.g. ``agent.with_(model="opus")``."""
        return dataclasses.replace(self, **changes)

    def with_mcp(self, name: str, spec: Mapping[str, Any] | MCP) -> "Agent":
        """A copy with one more MCP server attached (replacing one of the same name)."""
        mcp = spec if isinstance(spec, MCP) else MCP.parse(name, spec, Path.cwd())
        return dataclasses.replace(
            self, mcps=tuple(m for m in self.mcps if m.name != name) + (mcp,)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "harness": self.harness, "model": self.model,
            "instructions": self.instructions,
            "instruction_files": [str(p) for p in self.instruction_files],
            "skills": [str(p) for p in self.skills],
            "mcps": {m.name: m.to_dict() for m in self.mcps},
            "workspace": str(self.workspace) if self.workspace else None,
            "env": dict(self.env), "options": dict(self.options),
            "timeout": self.timeout, "sandbox": self.sandbox.to_dict(),
        }

    # -- running ----------------------------------------------------------- #
    def session(self, run_dir: str | Path | None = None) -> "Session":
        """A multi-turn conversation that keeps the harness session and sandbox alive."""
        from .session import Session

        return Session(self, Path(run_dir) if run_dir else _default_run_dir(self))

    def run(
        self, prompt: str, run_dir: str | Path | None = None, *, collect: tuple[str, ...] = ()
    ) -> "Result":
        """Run one task headlessly and return the agent's answer and trace.

        ``collect`` names workspace-relative paths to copy into the run dir
        before the sandbox is torn down.
        """
        with self.session(run_dir) as session:
            return session.send(prompt, collect=collect)


def _default_run_dir(agent: Agent) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return Path("runs") / f"{stamp}-{agent.name}"
