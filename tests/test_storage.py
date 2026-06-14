from __future__ import annotations

import sqlite3

import pytest

from rehearsal.storage import GhostlabStore, connect, migrate, resolve_db_path
from rehearsal.storage.redact import REDACTED, redact_connection
from rehearsal.types import Event


@pytest.fixture
def store(tmp_path):
    s = GhostlabStore.open(tmp_path / "ghostlab.sqlite3")
    yield s
    s.close()


def _inspect_result(tool_names=("alpha_one", "beta_two"), version="0.1.0"):
    return {
        "target_id": "demo-mcp",
        "transport": "streamable-http",
        "server_info": {"name": "demo", "version": version, "protocolVersion": "2025-06-18"},
        "capabilities": {"tools": {}},
        "instructions": "Be helpful.",
        "tools": [
            {
                "name": name,
                "title": name.title(),
                "description": f"does {name}",
                "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}},
                "annotations": {"readOnlyHint": True},
                "_meta": {"ui": {"resourceUri": f"ui://{name}"}},
            }
            for name in tool_names
        ],
        "resources": [{"uri": "res://a", "name": "A", "mimeType": "text/plain"}],
        "resource_templates": [],
        "prompts": [{"name": "p1", "description": "prompt one", "arguments": []}],
        "lint": [{"kind": "missing_tool_reference", "referenced": "kb_find", "in": "tool:alpha_one"}],
    }


def _target(url="http://localhost:8000/mcp", token="super-secret"):
    return {
        "id": "demo-mcp",
        "transport": "streamable-http",
        "connection": {"url": url, "headers": {"Authorization": f"Bearer {token}"}},
        "capabilities": {},
        "startup": {},
    }


# --------------------------------------------------------------------------- #
# Migrations / schema
# --------------------------------------------------------------------------- #
def test_migrations_apply_and_are_idempotent(tmp_path):
    path = tmp_path / "db.sqlite3"
    conn = connect(path)
    first = migrate(conn)
    assert first == [1]
    again = migrate(conn)
    assert again == []  # nothing new applied
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in ("targets", "inspections", "runs", "run_events", "tool_calls", "judgments"):
        assert expected in tables
    conn.close()


def test_foreign_keys_enforced(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO target_revisions(public_id, target_id, revision, transport, "
            "connection_json, content_sha256, created_at) VALUES "
            "('x', 999, 1, 'http', '{}', 'h', '2026-01-01')"
        )
        store.conn.commit()


def test_resolve_db_path_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("GHOSTLAB_DB", raising=False)
    assert resolve_db_path("/explicit/x.sqlite3").name == "x.sqlite3"
    monkeypatch.setenv("GHOSTLAB_DB", str(tmp_path / "env.sqlite3"))
    assert resolve_db_path().name == "env.sqlite3"
    monkeypatch.delenv("GHOSTLAB_DB")
    assert resolve_db_path(workspace=tmp_path).name == "ghostlab.sqlite3"


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_redaction_blanks_secrets_but_keeps_shape():
    conn = {
        "url": "http://localhost:8000/mcp",
        "headers": {"Authorization": "Bearer abc", "X-Trace": "ok"},
        "api_key": "leak",
    }
    red = redact_connection(conn)
    assert red["url"] == "http://localhost:8000/mcp"
    assert red["headers"]["Authorization"] == REDACTED
    assert red["headers"]["X-Trace"] == REDACTED  # any header value is secret-bearing
    assert red["api_key"] == REDACTED
    # input not mutated
    assert conn["headers"]["Authorization"] == "Bearer abc"


def test_inspection_secret_never_persisted(store):
    store.record_inspection(_target(token="TOPSECRET"), _inspect_result())
    rows = store.conn.execute("SELECT connection_json FROM target_revisions").fetchall()
    assert rows
    for row in rows:
        assert "TOPSECRET" not in row[0]
        assert REDACTED in row[0]


# --------------------------------------------------------------------------- #
# Inspections: versioning + revision dedup
# --------------------------------------------------------------------------- #
def test_inspection_versions_increment_and_revision_reused(store):
    a = store.record_inspection(_target(), _inspect_result())
    assert a["version"] == 1
    # same connection -> reuse revision; new inspection version
    b = store.record_inspection(_target(), _inspect_result(version="0.2.0"))
    assert b["version"] == 2
    assert a["target_revision_public_id"] == b["target_revision_public_id"]
    revs = store.conn.execute("SELECT COUNT(*) FROM target_revisions").fetchone()[0]
    assert revs == 1
    # changed connection -> new revision, version keeps climbing per target
    c = store.record_inspection(_target(url="http://other:9000/mcp"), _inspect_result())
    assert c["version"] == 3
    assert c["target_revision_public_id"] != a["target_revision_public_id"]
    assert store.conn.execute("SELECT COUNT(*) FROM target_revisions").fetchone()[0] == 2


def test_inspection_children_and_reads(store):
    info = store.record_inspection(_target(), _inspect_result())
    insp = store.get_inspection(info["inspection_public_id"])
    assert insp["tool_count"] == 2
    assert {t["name"] for t in insp["tools"]} == {"alpha_one", "beta_two"}
    assert insp["findings"][0]["referenced"] == "kb_find"
    targets = store.list_targets()
    assert targets[0]["slug"] == "demo-mcp"
    assert targets[0]["inspection_count"] == 1
    inspections = store.list_inspections(info["target_public_id"])
    assert [i["version"] for i in inspections] == [1]


# --------------------------------------------------------------------------- #
# Profile + dataset
# --------------------------------------------------------------------------- #
def _profile():
    return {
        "mcp": "demo@0.1.0",
        "domain_summary": "A demo MCP.",
        "taxonomy": {"alpha": ["alpha_one"], "beta": ["beta_two"]},
        "categories": [],
        "workflows": [],
    }


def _dataset():
    return {
        "manifest": {
            "name": "demo",
            "mcp": "demo@0.1.0",
            "seed": 7,
            "n_personas": 1,
            "scenarios_per_persona": 1,
            "cases": [
                {
                    "id": "p1--s1",
                    "persona": "p1",
                    "scenario": "p1--s1",
                    "intent": "happy_path",
                    "exercises": ["alpha_one"],
                    "max_turns": 3,
                    "status": "pending",
                }
            ],
        },
        "personas": [{"id": "p1", "name": "Pat", "summary": "tester", "traits": ["terse"], "context": {"level": "B1"}}],
        "scenarios": [
            {
                "id": "p1--s1",
                "title": "Do the thing",
                "intent": "happy_path",
                "persona": "in a hurry",
                "goal": "accomplish X",
                "opening_message": "hi",
                "max_turns": 3,
                "success_criteria": ["uses alpha_one"],
                "failure_signals": ["claims fake tool"],
                "exercises": ["alpha_one"],
            }
        ],
    }


def test_profile_dedup_and_dataset_cases(store):
    info = store.record_inspection(_target(), _inspect_result())
    p1 = store.record_profile(info["inspection_public_id"], _profile(), model="m", prompt_text="prompt")
    p2 = store.record_profile(info["inspection_public_id"], _profile(), model="m", prompt_text="prompt")
    assert p2["reused"] is True
    assert p1["profile_public_id"] == p2["profile_public_id"]

    ds = store.record_dataset(_dataset(), profile_public_id=p1["profile_public_id"])
    cases = store.get_dataset_cases(ds["dataset_public_id"])
    assert len(cases) == 1
    assert cases[0]["scenario_title"] == "Do the thing"
    assert cases[0]["curation_status"] == "pending"

    n = store.set_curation_status(ds["dataset_public_id"], {"p1--s1": "approved"})
    assert n == 1
    approved = store.get_dataset_cases(ds["dataset_public_id"], curation_status="approved")
    assert len(approved) == 1


# --------------------------------------------------------------------------- #
# Runs: event sink, tool-call projection, trace reconstruction, judgment
# --------------------------------------------------------------------------- #
def _run_events(run_id="2026-demo-run"):
    target = _target()
    scenario = _dataset()["scenarios"][0]
    return [
        Event.create("run_started", run_id=run_id, target=target, scenario=scenario,
                     models={"agent_under_test": "m-a", "user_emulator": "m-u"}),
        Event.create("user_message", turn=1, content="hi"),
        Event.create("aut_prompt", turn=1, prompt="PROMPT", stateful_resume=False),
        Event.create("aut_result", turn=1, exit_code=0, timed_out=False, output="done",
                     stderr="", tool_calls=[
                         {"index": 1, "server": "demo", "tool": "alpha_one",
                          "status": "completed", "arguments": {"x": "1"}, "result": {"ok": True}, "error": None}
                     ]),
        Event.create("user_emulator_prompt", turn=1, prompt="UPROMPT"),
        Event.create("user_emulator_result", turn=1, exit_code=0, timed_out=False, output="REHEARSAL_DONE", stderr=""),
        Event.create("run_finished", status="completed", transcript=[
            {"role": "user", "content": "hi"}, {"role": "assistant", "content": "done"}], tool_call_summary={}),
    ]


def _persist_run(store, run_public="2026-demo-run", status="completed"):
    target = _target()
    scenario = _dataset()["scenarios"][0]
    run_id = store.start_run(
        run_public, target=target, scenario=scenario, aut_model="m-a", user_model="m-u", max_turns=3,
    )
    for event in _run_events(run_public):
        store.append_event(run_id, event)
    store.finish_run(run_id, status=status, turns_completed=1)
    return run_id


def test_run_persistence_projection_and_trace(store):
    _persist_run(store)
    run = store.get_run("2026-demo-run")
    assert run["status"] == "completed"
    assert run["tool_call_count"] == 1
    assert run["turns_completed"] == 1

    calls = store.conn.execute("SELECT tool_name, status FROM tool_calls").fetchall()
    assert [(c[0], c[1]) for c in calls] == [("alpha_one", "completed")]

    trace = store.get_run_trace("2026-demo-run")
    assert trace["status"] == "completed"
    assert trace["models"]["agent_under_test"] == "m-a"
    assert len(trace["tool_calls"]) == 1
    assistant_turns = [t for t in trace["trace"] if t.get("role") == "assistant"]
    assert assistant_turns and assistant_turns[0]["tool_calls"][0]["tool"] == "alpha_one"
    assert any(p["type"] == "aut_prompt" for p in trace["prompts"])


def test_event_sequence_is_authoritative(store):
    run_id = _persist_run(store)
    seqs = [r[0] for r in store.conn.execute(
        "SELECT sequence FROM run_events WHERE run_id=? ORDER BY sequence", (run_id,))]
    assert seqs == list(range(1, len(seqs) + 1))


def test_judgment_attempts_increment(store):
    _persist_run(store)
    verdict = {
        "run_status": "completed",
        "verdict": "pass",
        "gates": [],
        "deterministic": {"coverage": "1/1"},
        "judge": {"summary": "good", "hallucinated_tools": [], "criteria": [], "failure_signals": []},
    }
    j1 = store.record_judgment("2026-demo-run", verdict, model="judge-m", prompt_text="JP")
    assert j1["attempt"] == 1
    j2 = store.record_judgment("2026-demo-run", verdict, model="judge-m", prompt_text="JP")
    assert j2["attempt"] == 2
    latest = store.get_latest_judgment("2026-demo-run")
    assert latest["attempt"] == 2
    assert latest["verdict"] == "pass"


def test_list_runs_filters(store):
    _persist_run(store, run_public="run-a", status="completed")
    _persist_run(store, run_public="run-b", status="aut_failed")
    store.record_judgment("run-a", {"run_status": "completed", "verdict": "pass", "judge": {}}, model="m")

    everything = store.list_runs()
    assert len(everything) == 2
    assert store.list_runs({"status": "aut_failed"})[0]["public_id"] == "run-b"
    assert store.list_runs({"verdict": "pass"})[0]["public_id"] == "run-a"
    assert store.list_runs({"verdict": "not evaluated"})[0]["public_id"] == "run-b"
    assert store.list_runs({"target": "demo-mcp"})
    assert store.list_runs({"search": "run-a"})[0]["public_id"] == "run-a"


def test_artifact_indexing(tmp_path, store):
    store.record_inspection(_target(), _inspect_result())
    info = store.list_inspections(store.list_targets()[0]["public_id"])[0]
    art = tmp_path / "inspect.json"
    art.write_text('{"hello": 1}', encoding="utf-8")
    pid = store.index_artifact("inspection", info["public_id"], "inspect.json", art)
    assert pid is not None
    listed = store.list_artifacts("inspection", info["public_id"])
    assert listed[0]["kind"] == "inspect.json"
    assert listed[0]["byte_size"] == len('{"hello": 1}')
    # missing file -> skipped
    assert store.index_artifact("inspection", info["public_id"], "missing", tmp_path / "nope") is None
