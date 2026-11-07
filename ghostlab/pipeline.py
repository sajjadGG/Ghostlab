"""Composable, checkpointed, resumable jobs built from agent stages.

A :class:`Pipeline` is an ordered list of stages. Items flow through it: the
inputs feed the first stage, and each stage's outputs feed the next.

* ``@stage`` — map: one output per item (return ``None`` to drop the item).
* ``@stage(fan_out=True)`` — return a list; every element becomes an item.
* ``@stage(gather=True)`` — receive every surviving item at once (reports).

A stage function is ``fn(ctx, item)``. The :class:`Context` runs agents
(``ctx.run``, ``ctx.session``), resolves agent configs passed as parameters
(``ctx.agent("judge")``), and gives the item a private directory.

Checkpointing happens at two levels, both under the run directory:

* every item's outcome is written to ``<NN-stage>/<key>/item.json`` as soon as
  it finishes; a re-run skips items already done with the same input;
* every ``ctx.run`` call inside an item is cached in ``calls/NNN``, so an item
  that failed half-way reuses the agent calls that already completed.

Re-running the same command resumes: completed work is reused, failed items
are retried, and changing an item's input (or bumping a stage's ``version``)
recomputes just what depends on it.
"""
from __future__ import annotations

import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ._util import canonical, digest, load_items, read_json, slug, utc_now, write_json, write_text
from .agent import Agent, ConfigError
from .session import AgentError, Result, Session

DONE = "done"
FAILED = "failed"


@dataclass(frozen=True)
class Stage:
    fn: Callable[..., Any]
    name: str
    fan_out: bool = False
    gather: bool = False
    concurrency: int = 1
    retries: int = 0
    version: str = ""

    def __post_init__(self) -> None:
        if self.fan_out and self.gather:
            raise ConfigError(f"stage {self.name!r} cannot both fan_out and gather")
        if self.concurrency < 1 or self.retries < 0:
            raise ConfigError(f"stage {self.name!r}: concurrency >= 1 and retries >= 0")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.fn(*args, **kwargs)


def stage(
    fn: Callable[..., Any] | None = None, *, name: str = "", fan_out: bool = False,
    gather: bool = False, concurrency: int = 1, retries: int = 0, version: str = "",
) -> Any:
    """Declare a stage: ``@stage`` or ``@stage(fan_out=True, concurrency=4)``.

    ``version`` is part of every item's checkpoint key; bump it after changing
    the stage's logic to recompute its items on the next run.
    """
    def wrap(func: Callable[..., Any]) -> Stage:
        return Stage(func, name or func.__name__, fan_out, gather, concurrency, retries, version)

    return wrap(fn) if fn is not None else wrap


@dataclass
class Item:
    key: str
    value: Any


def _keyed(values: Iterable[Any], prefix: str = "") -> list[Item]:
    """Stable, filesystem-safe keys: the item's ``id`` when it has one, else a hash."""
    items: list[Item] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(values):
        if isinstance(value, Mapping) and value.get("id") not in (None, ""):
            base = slug(value["id"])
        else:
            base = str(index) if prefix else digest(value, 10)
        key = f"{prefix}.{base}" if prefix else base
        seen[key] = seen.get(key, 0) + 1
        items.append(Item(key if seen[key] == 1 else f"{key}-{seen[key]}", value))
    return items


class Context:
    """What a stage function gets besides its item."""

    def __init__(self, stage: Stage, key: str, directory: Path, params: Mapping[str, Any],
                 log: Callable[[str], None]) -> None:
        self.stage = stage.name
        self.key = key
        self.dir = directory
        self.params = params
        self._log = log
        self._calls = 0
        self._sessions = 0
        self._lock = threading.Lock()

    def log(self, message: str) -> None:
        self._log(f"[{self.stage}] {self.key}: {message}")

    def agent(self, name: str, **overrides: Any) -> Agent:
        """The agent passed as parameter ``name``: an :class:`Agent`, a config
        path, or an inline mapping. Keyword overrides apply on top."""
        if name not in self.params:
            raise ConfigError(f"stage {self.stage!r} needs parameter {name!r} (an agent config)")
        value = self.params[name]
        if isinstance(value, Agent):
            agent = value
        elif isinstance(value, (str, Path)):
            agent = Agent.load(value)
        elif isinstance(value, Mapping):
            agent = Agent.from_dict({"name": name, **value})
        else:
            raise ConfigError(f"parameter {name!r} is not an agent config: {value!r}")
        return agent.with_(**overrides) if overrides else agent

    def run(self, agent: Agent, prompt: str, *, check: bool = True,
            collect: Sequence[str] = ()) -> Result:
        """Run one agent task, reusing the cached result from an earlier attempt.

        With ``check`` (the default) a turn that did not complete raises
        :class:`~ghostlab.AgentError`, which fails — and later retries — the item.
        """
        with self._lock:
            self._calls += 1
            call_dir = self.dir / "calls" / f"{self._calls:03d}"
        fingerprint = digest([agent.to_dict(), prompt, list(collect)], 16)
        marker = call_dir / "call.json"
        if marker.is_file() and read_json(marker).get("fingerprint") == fingerprint:
            cached = Result.load(call_dir / "turn-1")
            if cached.ok:
                return cached
        shutil.rmtree(call_dir, ignore_errors=True)
        result = agent.run(prompt, call_dir, collect=tuple(collect))
        write_json(marker, {"fingerprint": fingerprint, "status": result.status})
        return result.check() if check else result

    def session(self, agent: Agent) -> Session:
        """A fresh multi-turn session (sessions are re-run, not cached, on resume)."""
        with self._lock:
            self._sessions += 1
            directory = self.dir / "sessions" / f"{self._sessions:03d}"
        shutil.rmtree(directory, ignore_errors=True)
        return Session(agent, directory)


@dataclass
class RunReport:
    run_dir: Path
    stages: list[dict[str, Any]]
    results: list[Any]
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _jsonable(params: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v.to_dict() if isinstance(v, Agent) else v for k, v in params.items()}


class Pipeline:
    def __init__(self, name: str, stages: Sequence[Stage], params: Mapping[str, Any] | None = None):
        if not stages:
            raise ConfigError("a pipeline needs at least one stage")
        names = [s.name for s in stages]
        if len(set(names)) != len(names):
            raise ConfigError(f"stage names must be unique: {names}")
        self.name = name
        self.stages = list(stages)
        self.params = dict(params or {})

    def run(
        self, inputs: Iterable[Any] | str | Path | None = None, *, run_dir: str | Path,
        params: Mapping[str, Any] | None = None, log: Callable[[str], None] | None = None,
    ) -> RunReport:
        """Run (or resume) the pipeline in ``run_dir``.

        ``inputs`` is a list of items or a ``.jsonl``/JSON/YAML file. Omit it to
        resume with the inputs recorded by the first run.
        """
        run_dir = Path(run_dir).expanduser().resolve()
        log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        recorded = run_dir / "inputs.jsonl"
        if inputs is None:
            if not recorded.is_file():
                raise ConfigError(f"no inputs given and none recorded in {run_dir}")
            values = load_items(recorded)
        else:
            values = load_items(Path(inputs)) if isinstance(inputs, (str, Path)) else list(inputs)
            write_text(recorded, "".join(_line(v) for v in values))
        merged = {**self.params, **dict(params or {})}
        job = {"pipeline": self.name, "params": _jsonable(merged), "started_at": utc_now(),
               "stages": []}
        items = _keyed(values)
        failures: list[dict[str, Any]] = []
        for index, current in enumerate(self.stages, start=1):
            stage_dir = run_dir / f"{index:02d}-{current.name}"
            work = [Item("all", [i.value for i in items])] if current.gather else items
            records = self._run_stage(current, work, stage_dir, merged, log)
            items = []
            counts = {DONE: 0, FAILED: 0, "cached": 0}
            for item, record in zip(work, records):
                counts[record["status"]] += 1
                counts["cached"] += bool(record.get("cached"))
                if record["status"] == FAILED:
                    failures.append({"stage": current.name, "key": item.key,
                                     "error": _last_line(record["error"])})
                    continue
                output = record["output"]
                if current.fan_out:
                    items += _keyed(output or [], prefix=item.key)
                elif output is not None:
                    items.append(Item(item.key, output))
            write_text(stage_dir / "outputs.jsonl", "".join(
                _line({"key": i.key, "output": i.value}) for i in items))
            job["stages"].append({"name": current.name, **counts, "outputs": len(items)})
            log(f"[{current.name}] {counts[DONE]} done ({counts['cached']} cached), "
                f"{counts[FAILED]} failed -> {len(items)} items")
            write_json(run_dir / "job.json", {**job, "updated_at": utc_now()})
        results = [i.value for i in items]
        write_text(run_dir / "results.jsonl", "".join(_line(v) for v in results))
        write_json(run_dir / "job.json", {**job, "finished_at": utc_now(),
                                          "failures": failures})
        return RunReport(run_dir, job["stages"], results, failures)

    def _run_stage(self, current: Stage, work: list[Item], stage_dir: Path,
                   params: Mapping[str, Any], log: Callable[[str], None]) -> list[dict[str, Any]]:
        def one(item: Item) -> dict[str, Any]:
            return _run_item(current, item, stage_dir / item.key, params, log)

        if current.concurrency == 1 or len(work) <= 1:
            return [one(item) for item in work]
        with ThreadPoolExecutor(max_workers=current.concurrency) as pool:
            return list(pool.map(one, work))


def _last_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[-1] if lines else ""


def _line(value: Any) -> str:
    return canonical(value) + "\n"


def _archive(directory: Path, attempt: int, exc: Exception) -> None:
    """Move a failed attempt's traces aside so the next attempt starts right.

    Sessions are never reused. After an agent failure the calls that did
    complete stay cached and only the failed one is set aside; after any other
    failure (the stage rejected what the agent said) every call is redone.
    """
    target = directory / "attempts" / str(attempt)
    moves = [directory / "sessions"]
    calls = directory / "calls"
    if not isinstance(exc, AgentError):
        moves.append(calls)
    elif exc.result.dir.parent.parent == calls:
        moves.append(exc.result.dir.parent)
    for source in moves:
        if source.exists():
            destination = target / source.relative_to(directory)
            shutil.rmtree(destination, ignore_errors=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))


def _run_item(current: Stage, item: Item, directory: Path, params: Mapping[str, Any],
              log: Callable[[str], None]) -> dict[str, Any]:
    checkpoint = directory / "item.json"
    # Params are part of the key: changing one recomputes items, while unchanged
    # agent calls inside them still come from the per-call cache.
    input_hash = digest([current.version, item.value, _jsonable(params)], 16)
    previous: dict[str, Any] = read_json(checkpoint) if checkpoint.is_file() else {}
    if previous.get("status") == DONE and previous.get("input_hash") == input_hash:
        return {**previous, "cached": True}
    attempts = int(previous.get("attempts") or 0) if previous.get("input_hash") == input_hash else 0
    record: dict[str, Any] = {"stage": current.name, "key": item.key, "input_hash": input_hash,
                              "input": item.value, "started_at": utc_now()}
    clock = time.monotonic()
    directory.mkdir(parents=True, exist_ok=True)
    for _ in range(current.retries + 1):
        attempts += 1
        ctx = Context(current, item.key, directory, params, log)
        try:
            output = current.fn(ctx, item.value)
            if current.fan_out and not isinstance(output, (list, tuple)):
                raise TypeError(f"fan_out stage {current.name!r} must return a list, "
                                f"not {type(output).__name__}")
        except Exception as exc:  # noqa: BLE001 — any stage failure is recorded, not fatal
            record.update(status=FAILED, output=None, error=traceback.format_exc()[-4000:])
            _archive(directory, attempts, exc)
        else:
            record.update(status=DONE, output=output, error="")
            break
    record.update(attempts=attempts, finished_at=utc_now(),
                  duration=round(time.monotonic() - clock, 2))
    write_json(checkpoint, record)
    tail = _last_line(record["error"])
    log(f"[{current.name}] {item.key}: {record['status']} in {record['duration']}s"
        + (f" — {tail}" if tail else ""))
    return record
