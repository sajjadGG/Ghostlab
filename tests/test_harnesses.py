from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghostlab import Agent
from ghostlab.harnesses import (
    GHOSTLAB_COPILOT_SESSION_PREFIX,
    HARNESSES,
    codex_mcp_overrides,
)

MCPS = {
    "tiny": {"command": "node", "args": ["/srv/tiny.js"], "env": {"LEVEL": "1"}},
    "hub": {"url": "https://hub.example/mcp", "headers": {"Authorization": "Bearer ${HUB_TOKEN}"}},
}


def identity(value):
    return str(value)


def build(agent: Agent, *, sandboxed: bool = False, inside=identity):
    return HARNESSES[agent.harness].invocation(agent, workdir="/work", inside=inside,
                                               sandboxed=sandboxed)


def agent(harness: str, **extra) -> Agent:
    return Agent.from_dict({"harness": harness, "model": "m-1", "mcps": MCPS, **extra})


def test_prompt_preamble_carries_instructions_files_and_skills(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Never publish.")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes/SKILL.md").write_text("Group by Added/Fixed.")
    configured = Agent.from_dict({"harness": "claude", "instructions": "Be concise.",
                                  "instruction_files": ["AGENTS.md"], "skills": ["notes"]},
                                 base=tmp_path)
    prompt = HARNESSES["claude"].prompt(configured, "Draft notes")
    for text in ("Be concise.", "Never publish.", "## Skill: notes", "Task:\nDraft notes"):
        assert text in prompt
    # OpenCode loads files and skills natively, so only free text travels.
    native = HARNESSES["opencode"].prompt(configured.with_(harness="opencode"), "Draft notes")
    assert "Never publish." not in native and "Be concise." in native
    assert HARNESSES["claude"].prompt(Agent(harness="claude"), "hi") == "hi"


def test_claude_command_and_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_TOKEN", "secret")
    invocation = build(agent("claude", options={"allow_tools": ["Read"], "max_turns": 5}))
    argv = invocation.argv
    assert argv[:2] == ["claude", "-p"] and invocation.stdin
    assert argv[argv.index("--model") + 1] == "m-1"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__tiny,mcp__hub,Read"
    config = json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
    assert config["tiny"] == {"type": "stdio", "command": "node", "args": ["/srv/tiny.js"],
                              "env": {"LEVEL": "1"}}
    assert config["hub"]["headers"] == {"Authorization": "Bearer secret"}
    resumed = HARNESSES["claude"].resume(invocation, "s-1", False)
    assert resumed.argv[-2:] == ["--resume", "s-1"]


def test_codex_maps_mcps_and_resumes_after_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUB_TOKEN", "secret")
    invocation = build(agent("codex", options={"reasoning_effort": "high"}))
    argv = invocation.argv
    assert 'mcp_servers.tiny.command="node"' in argv
    assert 'mcp_servers.tiny.args=["/srv/tiny.js"]' in argv
    assert 'mcp_servers.hub.bearer_token_env_var="HUB_TOKEN"' in argv
    assert invocation.env["HUB_TOKEN"] == "secret"  # read inside codex, not baked into argv
    assert invocation.env["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"] == "ghostlab"
    assert argv[-4:] == ["exec", "--json", "--skip-git-repo-check", "-"]
    resumed = HARNESSES["codex"].resume(invocation, "th-1", False).argv
    assert resumed[resumed.index("exec"):][:3] == ["exec", "resume", "th-1"]


def test_codex_header_shapes() -> None:
    hub = Agent.from_dict({"harness": "codex", "mcps": {
        "k": {"url": "u", "headers": {"X-Key": "${API_KEY}", "X-Team": "qa"}}}})
    args, _ = codex_mcp_overrides(hub.mcps, identity)
    assert 'mcp_servers.k.env_http_headers.X-Key="API_KEY"' in args
    assert 'mcp_servers.k.http_headers.X-Team="qa"' in args


def test_copilot_tags_sessions_locally_and_continues_in_a_sandbox() -> None:
    local = build(agent("copilot", options={"agent": "notes", "deny_tools": ["shell"],
                                            "reasoning_effort": "low"}))
    argv = local.argv
    assert not local.stdin and argv[-1] == "--prompt"
    assert argv[argv.index("--session-id") + 1].startswith(GHOSTLAB_COPILOT_SESSION_PREFIX)
    assert argv[argv.index("--reasoning-effort") + 1] == "low"
    assert ["--agent", "notes"] == argv[argv.index("--agent"):argv.index("--agent") + 2]
    assert ["--deny-tool", "shell"] == argv[argv.index("--deny-tool"):argv.index("--deny-tool") + 2]
    assert json.loads(argv[argv.index("--additional-mcp-config") + 1])["mcpServers"]["tiny"]["type"] == "local"
    assert HARNESSES["copilot"].resume(local, "x", False) is local
    boxed = build(agent("copilot"), sandboxed=True)
    assert "--session-id" not in boxed.argv and "--no-remote" not in boxed.argv
    resumed = HARNESSES["copilot"].resume(boxed, "x", True).argv
    assert resumed[-2:] == ["--continue", "--prompt"]


def test_opencode_config_travels_inline_with_sandbox_paths(tmp_path: Path) -> None:
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes/SKILL.md").write_text("x")
    configured = Agent.from_dict({
        "harness": "opencode", "model": "anthropic/claude", "skills": ["notes"], "mcps": MCPS,
        "options": {"config": {"permission": {"edit": "allow"}}},
    }, base=tmp_path)
    invocation = build(configured, sandboxed=True,
                       inside=lambda p: str(p).replace(str(tmp_path), "/sandbox/agent"))
    config = json.loads(invocation.env["OPENCODE_CONFIG_CONTENT"])
    assert config["permission"]["edit"] == "allow" and config["permission"]["bash"] == "deny"
    assert config["skills"]["paths"] == ["/sandbox/agent/notes"]
    assert config["mcp"]["tiny"]["command"] == ["node", "/srv/tiny.js"]
    assert config["mcp"]["hub"]["type"] == "remote"
    assert invocation.env["XDG_DATA_HOME"] == "/opt/agent/share"
    assert HARNESSES["opencode"].egress(configured)[0][-1] == "api.anthropic.com"


def test_command_harness_prompt_modes() -> None:
    by_arg = Agent(harness="command", options={"command": ["my-agent", "--go"], "prompt": "arg"})
    assert not build(by_arg).stdin
    with pytest.raises(ValueError, match="options.output"):
        build(by_arg.with_(options={"command": ["x"], "output": "xml"}))
