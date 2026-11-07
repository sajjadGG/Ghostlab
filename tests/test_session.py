from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghostlab import Agent, AgentError, Result, Sandbox
from conftest import FAKE_AGENT


def test_one_task_leaves_a_complete_trace(local_agent: Agent, tmp_path: Path) -> None:
    result = local_agent.with_(instructions="Be brief.").run("say hi json", tmp_path / "run")
    assert result.ok and result.turn == 1
    assert result.json()["echo"] == "say hi json"
    turn = tmp_path / "run" / "turn-1"
    assert result.dir == turn.resolve()
    assert "Be brief." in (turn / "prompt.txt").read_text()
    assert (turn / "answer.md").read_text().strip() == result.answer
    assert Result.load(turn) == result


def test_the_workspace_is_a_private_copy(local_agent: Agent, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello")
    agent = local_agent.with_(workspace=repo)
    result = agent.run("write:NOTES.md json", tmp_path / "run", collect=("NOTES.md", "README.md"))
    assert Path(result.json()["cwd"]).resolve() == (tmp_path / "run/workspace").resolve()
    assert not (repo / "NOTES.md").exists()
    assert result.files == ["NOTES.md", "README.md"]
    assert (result.dir / "files/NOTES.md").read_text() == "written\n"


@pytest.mark.parametrize("task, status, error", [
    ("exit:3", "agent_error", "boom"),
    ("quota", "model_unavailable", "rate limit"),
    ("sleep:5", "timed_out", "timeout"),
])
def test_failures_are_classified(local_agent: Agent, tmp_path: Path, task, status, error) -> None:
    result = local_agent.with_(timeout=1).run(task, tmp_path / "run")
    assert result.status == status and error in result.error
    with pytest.raises(AgentError, match=status):
        result.check()


def test_a_missing_binary_is_a_harness_error(tmp_path: Path) -> None:
    agent = Agent(harness="claude", options={"bin": str(tmp_path / "nope")},
                  sandbox=Sandbox(backend="local"))
    assert agent.run("hi", tmp_path / "run").status == "harness_error"


def test_session_resumes_the_harness_conversation(fake_claude: Path, tmp_path: Path) -> None:
    agent = Agent(harness="claude", instructions="Persona: tester.",
                  options={"bin": str(fake_claude)}, sandbox=Sandbox(backend="local"))
    with agent.session(tmp_path / "chat") as chat:
        first = chat.send("hello")
        second = chat.send("again")
    assert first.answer == "echo: hello" and first.session_id == "sess-1"
    assert first.tool_calls[0]["tool"] == "echo"
    assert second.answer == "echo: again (resumed sess-1)" and second.turn == 2
    assert "Persona" not in (tmp_path / "chat/turn-2/prompt.txt").read_text()


def test_openshell_runs_inside_the_sandbox_and_cleans_up(openshell, local_agent: Agent,
                                                         tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    agent = local_agent.with_(workspace=repo, sandbox=Sandbox(bin=openshell.bin, image="base"))
    result = agent.run("write:out.txt json", tmp_path / "run", collect=("out.txt",))
    assert result.ok, result.error
    assert result.json()["cwd"].endswith("/sandbox/repo")
    assert (result.dir / "files/out.txt").read_text() == "written\n"
    create = next(c for c in openshell.calls() if c[:2] == ["sandbox", "create"])
    assert "--no-auto-providers" in create and f"{repo}:/sandbox" in create
    assert f"{FAKE_AGENT.parent}:/sandbox/agent" in create  # the program travels with it
    assert any(c[:2] == ["sandbox", "delete"] for c in openshell.calls())
    assert openshell.boxes() == []
    assert (tmp_path / "run/openshell.log").read_text().strip() == "fake openshell logs"


def test_openshell_failures_are_sandbox_errors(openshell, local_agent: Agent, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL_CREATE", "1")
    result = local_agent.with_(sandbox=Sandbox(bin=openshell.bin)).run("hi", tmp_path / "run")
    assert result.status == "sandbox_error"
    assert "sandbox_gateway_unavailable" in result.error


def test_openshell_copies_secret_files_outside_the_workspace(openshell, local_agent: Agent,
                                                             tmp_path: Path) -> None:
    secret = tmp_path / "auth.json"
    secret.write_text(json.dumps({"token": "x"}))
    sandbox = Sandbox(bin=openshell.bin, files={"/opt/agent/share/auth.json": secret})
    assert local_agent.with_(sandbox=sandbox).run("hi", tmp_path / "run").ok
    uploads = [c for c in openshell.calls() if c[:2] == ["sandbox", "upload"]]
    assert uploads[0][3:5] == [str(secret), "/opt/agent/share/auth.json"]
    assert ["sandbox", "exec"] == next(c for c in openshell.calls() if "/bin/chmod" in c)[:2]
