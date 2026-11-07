from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghostlab import MCP, Agent, ConfigError, Sandbox


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_load_yaml_resolves_paths_against_the_config(tmp_path: Path) -> None:
    write(tmp_path / "skills/notes/SKILL.md", "Write notes.")
    write(tmp_path / "AGENTS.md", "Be kind.")
    write(tmp_path / "server.js", "")
    (tmp_path / "repo").mkdir()
    config = write(tmp_path / "helper.yaml", """
harness: claude
model: sonnet
instruction_files: AGENTS.md
skills: [skills/notes]
workspace: repo
mcps:
  tiny: {command: node, args: [server.js, -y], env: {LEVEL: "1"}}
  hub: {url: "https://hub.example/mcp", type: sse, headers: {X-Team: qa}}
sandbox: {backend: local}
""")
    agent = Agent.load(config)
    assert agent.name == "helper"
    assert agent.instruction_files == (tmp_path / "AGENTS.md",)
    assert agent.skills == (tmp_path / "skills/notes/SKILL.md",)
    assert agent.workspace == tmp_path / "repo"
    tiny, hub = agent.mcps
    assert tiny.command == ("node", str(tmp_path / "server.js"), "-y")
    assert tiny.env == {"LEVEL": "1"}
    assert (hub.transport, hub.url, hub.headers) == ("sse", "https://hub.example/mcp", {"X-Team": "qa"})
    assert agent.sandbox.backend == "local"


def test_mcp_config_file_in_mcp_servers_shape(tmp_path: Path) -> None:
    write(tmp_path / "mcp.json", json.dumps({"mcpServers": {"a": {"url": "https://a"}}}))
    agent = Agent.from_dict({"harness": "codex", "mcp_config": "mcp.json"}, base=tmp_path)
    assert [m.name for m in agent.mcps] == ["a"]
    assert agent.mcps[0].transport == "http"


@pytest.mark.parametrize("data, message", [
    ({"model": "x"}, "needs a harness"),
    ({"harness": "gemini"}, "unknown harness"),
    ({"harness": "command"}, "needs options.command"),
    ({"harness": "claude", "modle": "x"}, "unknown agent field"),
    ({"harness": "claude", "skills": ["missing"]}, "skill not found"),
    ({"harness": "claude", "mcps": {"x": {"args": []}}}, "needs a 'command'"),
])
def test_invalid_configs_fail_clearly(tmp_path: Path, data: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Agent.from_dict(data, base=tmp_path)


def test_variants_are_copies() -> None:
    base = Agent(harness="claude", model="sonnet")
    tuned = base.with_(model="opus").with_mcp("tiny", {"command": ["node", "x.js"]})
    assert base.model == "sonnet" and base.mcps == ()
    assert tuned.model == "opus"
    assert tuned.mcps == (MCP(name="tiny", command=("node", "x.js")),)
    replaced = tuned.with_mcp("tiny", {"url": "https://t"})
    assert [m.transport for m in replaced.mcps] == ["http"]
    assert base.with_(sandbox={"backend": "local"}).sandbox == Sandbox(backend="local")


def test_sandbox_rejects_unsafe_settings(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported sandbox"):
        Sandbox(backend="docker")
    with pytest.raises(ValueError, match="under /sandbox"):
        Sandbox.from_dict({"uploads": [{"source": ".", "target": "/etc"}]}, tmp_path)
    with pytest.raises(ValueError, match="unknown sandbox field"):
        Sandbox.from_dict({"netwrk": "x"}, tmp_path)


def test_python_construction_accepts_plain_values(tmp_path: Path) -> None:
    agent = Agent(harness="claude", sandbox="local", workspace=str(tmp_path),
                  mcps={"t": {"url": "https://t"}})
    assert agent.sandbox == Sandbox(backend="local")
    assert agent.workspace == tmp_path.resolve()
    assert agent.mcps[0].name == "t"
