from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ghostlab import Agent, ConfigError, Pipeline, stage
from ghostlab._util import extract_json


def quiet(_: str) -> None:
    pass


@stage
def double(ctx, item):
    return {**item, "n": item["n"] * 2}


@stage(fan_out=True)
def split(ctx, item):
    return [{"id": f"{item['id']}-{i}", "n": item["n"] + i} for i in range(2)]


@stage
def drop_odd(ctx, item):
    return item if item["n"] % 2 == 0 else None


@stage(gather=True)
def total(ctx, items):
    (ctx.dir / "report.md").write_text(f"{len(items)} items")
    return {"count": len(items), "sum": sum(i["n"] for i in items)}


def test_map_fan_out_filter_and_gather(tmp_path: Path) -> None:
    pipeline = Pipeline("demo", [double, split, drop_odd, total])
    report = pipeline.run([{"id": "a", "n": 1}, {"id": "b", "n": 2}], run_dir=tmp_path, log=quiet)
    # a: 2 -> 2, 3 -> keep 2 ; b: 4 -> 4, 5 -> keep 4
    assert report.ok and report.results == [{"count": 2, "sum": 6}]
    assert [s["outputs"] for s in report.stages] == [2, 4, 2, 1]
    assert (tmp_path / "02-split/a/item.json").is_file()
    keys = [json.loads(line)["key"] for line in (tmp_path / "02-split/outputs.jsonl").read_text().splitlines()]
    assert keys == ["a.a-0", "a.a-1", "b.b-0", "b.b-1"]
    assert (tmp_path / "04-total/all/report.md").read_text() == "2 items"
    assert json.loads((tmp_path / "results.jsonl").read_text()) == {"count": 2, "sum": 6}


def test_resume_skips_done_items_and_retries_failures(tmp_path: Path) -> None:
    seen: list[str] = []
    broken = {"b"}

    @stage
    def flaky(ctx, item):
        seen.append(item["id"])
        if item["id"] in broken:
            raise RuntimeError("transient outage")
        return item

    pipeline = Pipeline("resume", [flaky])
    inputs = [{"id": "a"}, {"id": "b"}]
    first = pipeline.run(inputs, run_dir=tmp_path, log=quiet)
    assert not first.ok and first.failures[0]["key"] == "b"
    assert "transient outage" in first.failures[0]["error"]
    broken.clear()
    second = pipeline.run(run_dir=tmp_path, log=quiet)  # inputs come from the first run
    assert second.ok and seen == ["a", "b", "b"]
    assert second.stages[0]["cached"] == 1
    assert json.loads((tmp_path / "01-flaky/b/item.json").read_text())["attempts"] == 2


def test_changed_inputs_and_versions_recompute(tmp_path: Path) -> None:
    runs: list[int] = []

    def build(version: str) -> Pipeline:
        @stage(name="count", version=version)
        def count(ctx, item):
            runs.append(item["n"])
            return item

        return Pipeline("versions", [count])

    build("1").run([{"id": "a", "n": 1}], run_dir=tmp_path, log=quiet)
    build("1").run([{"id": "a", "n": 1}], run_dir=tmp_path, log=quiet)
    build("1").run([{"id": "a", "n": 2}], run_dir=tmp_path, log=quiet)
    build("2").run([{"id": "a", "n": 2}], run_dir=tmp_path, log=quiet)
    assert runs == [1, 2, 2]


def test_in_run_retries(tmp_path: Path) -> None:
    attempts = []

    @stage(retries=2)
    def wobbly(ctx, item):
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("not yet")
        return "ok"

    assert Pipeline("retry", [wobbly]).run([1], run_dir=tmp_path, log=quiet).results == ["ok"]
    assert len(attempts) == 3


def test_concurrency_runs_items_in_parallel_and_keeps_order(tmp_path: Path) -> None:
    active, peak, lock = [0], [0], threading.Lock()

    @stage(concurrency=4)
    def slow(ctx, item):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return item

    report = Pipeline("par", [slow]).run(list(range(8)), run_dir=tmp_path, log=quiet)
    assert report.results == list(range(8)) and peak[0] > 1


def test_agent_calls_are_checkpointed_inside_an_item(local_agent: Agent, calls, tmp_path: Path) -> None:
    fail_judge = [True]

    @stage
    def two_calls(ctx, item):
        draft = ctx.run(ctx.agent("worker"), f"draft {item['id']}").answer
        if fail_judge[0]:
            ctx.run(ctx.agent("worker"), "exit:2")
        return {"draft": draft, "verdict": ctx.run(ctx.agent("worker"), "judge json").json()}

    pipeline = Pipeline("calls", [two_calls], params={"worker": local_agent})
    first = pipeline.run([{"id": "x"}], run_dir=tmp_path, log=quiet)
    assert "agent_error" in first.failures[0]["error"]
    fail_judge[0] = False
    second = pipeline.run(run_dir=tmp_path, log=quiet)
    assert second.results[0]["draft"] == "echo: draft x"
    # The draft was reused from the checkpoint; only the failed call was redone.
    assert calls() == ["draft x", "exit:2", "judge json"]
    assert (tmp_path / "01-two_calls/x/attempts/1/calls/002/turn-1/stderr.txt").is_file()


def test_rejected_answers_are_asked_again(local_agent: Agent, calls, tmp_path: Path) -> None:
    @stage(retries=1)
    def strict(ctx, item):
        answer = ctx.run(ctx.agent("worker"), "label it").answer
        if len(calls()) == 1:
            raise ValueError(f"bad label {answer!r}")
        return answer

    report = Pipeline("strict", [strict], params={"worker": local_agent}).run(
        [1], run_dir=tmp_path, log=quiet)
    assert report.ok and calls() == ["label it", "label it"]


def test_agent_params_accept_paths_mappings_and_overrides(tmp_path: Path) -> None:
    config = tmp_path / "w.yaml"
    config.write_text("harness: claude\nmodel: sonnet\n")
    seen = {}

    @stage
    def inspect(ctx, item):
        seen["path"] = ctx.agent("from_path")
        seen["inline"] = ctx.agent("inline", model="opus")
        return item

    Pipeline("params", [inspect], params={"from_path": str(config),
                                          "inline": {"harness": "codex"}}).run(
        [1], run_dir=tmp_path / "run", log=quiet)
    assert (seen["path"].name, seen["path"].model) == ("w", "sonnet")
    assert (seen["inline"].harness, seen["inline"].model) == ("codex", "opus")

    @stage
    def needs(ctx, item):
        return ctx.agent("missing")

    report = Pipeline("p", [needs]).run([1], run_dir=tmp_path / "r2", log=quiet)
    assert "needs parameter 'missing'" in report.failures[0]["error"]


def test_inputs_without_ids_get_stable_hash_keys(tmp_path: Path) -> None:
    @stage
    def keep(ctx, item):
        return ctx.key

    first = Pipeline("k", [keep]).run(["x", "y", "x"], run_dir=tmp_path, log=quiet).results
    assert len(set(first)) == 3 and first[2] == f"{first[0]}-2"


def test_invalid_pipelines() -> None:
    with pytest.raises(ConfigError, match="unique"):
        Pipeline("dup", [double, double])
    with pytest.raises(ConfigError, match="both"):
        stage(fan_out=True, gather=True)(lambda ctx, item: item)
    with pytest.raises(ConfigError, match="no inputs"):
        Pipeline("empty", [double]).run(run_dir="/nonexistent/ghostlab-run", log=quiet)


@pytest.mark.parametrize("reply, value", [
    ('{"a": 1}', {"a": 1}),
    ('Sure!\n```json\n{"a": 2}\n```\nDone.', {"a": 2}),
    ('Result: [1, 2] and then {"final": true} ok', {"final": True}),
])
def test_extract_json_tolerates_prose(reply: str, value) -> None:
    assert extract_json(reply) == value


def test_fan_out_must_return_a_list(tmp_path: Path) -> None:
    @stage(fan_out=True)
    def bad(ctx, item):
        return {"not": "a list"}

    report = Pipeline("bad", [bad]).run([1], run_dir=tmp_path, log=quiet)
    assert "must return a list" in report.failures[0]["error"]
