"""Datasets: persona x scenario matrices, plus a dataset runner.

`build_dataset` generates a persona library from a capability profile and, for
each persona, a set of persona-specific scenarios — realizing "different users
and different scenarios for each of them". The result is a self-contained,
reviewable dataset directory:

    datasets/<name>/
      dataset.json          manifest: mcp, seed, cases[]
      personas/<id>.json
      scenarios/<id>.json

`run_dataset` executes every case through the orchestrator and writes a
dataset-level summary alongside the per-case run directories.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_backend import CodexBackend
from .config import load_persona, load_runner, load_scenario, load_target
from .generate import generate_scenarios
from .personas import generate_personas
from .orchestrator import run_scenario
from .types import utc_now


@dataclass(frozen=True)
class DatasetCase:
    id: str
    persona: str
    scenario: str
    intent: str
    exercises: list[str]
    max_turns: int


def assemble_cases(
    personas: list[dict[str, Any]],
    scenarios_by_persona: dict[str, list[dict[str, Any]]],
    seed: int,
) -> list[dict[str, Any]]:
    """Pair personas with their scenarios into ordered, seeded cases.

    Pure and deterministic given its inputs: the seed only governs the stable
    ordering of cases, so the same inputs + seed always yield the same manifest.
    """
    cases: list[dict[str, Any]] = []
    for persona in personas:
        persona_id = persona["id"]
        for scenario in scenarios_by_persona.get(persona_id, []):
            cases.append(
                {
                    # Scenario ids are already persona-namespaced by build_dataset,
                    # so they double as a readable, unique case id.
                    "id": scenario["id"],
                    "persona": persona_id,
                    "scenario": scenario["id"],
                    "intent": scenario.get("intent", ""),
                    "exercises": list(scenario.get("exercises", [])),
                    "max_turns": scenario.get("max_turns", 4),
                }
            )
    rng = random.Random(seed)
    rng.shuffle(cases)
    return cases


def build_dataset(
    profile: dict[str, Any],
    backend: CodexBackend,
    *,
    n_personas: int,
    scenarios_per_persona: int,
    seed: int,
    name: str,
) -> dict[str, Any]:
    """Generate personas + per-persona scenarios and assemble a dataset manifest.

    Returns a dict with keys: manifest, personas, scenarios (the latter two are
    lists of dicts ready to be written to disk).
    """
    personas = generate_personas(profile, backend, n_personas)

    all_scenarios: list[dict[str, Any]] = []
    scenarios_by_persona: dict[str, list[dict[str, Any]]] = {}
    seen_scenario_ids: set[str] = set()
    for persona in personas:
        scenarios = generate_scenarios(profile, backend, scenarios_per_persona, persona=persona)
        prefixed: list[dict[str, Any]] = []
        for scenario in scenarios:
            # Namespace scenario ids by persona so files never collide.
            base_id = f"{persona['id']}--{scenario['id']}"
            scenario_id = base_id
            suffix = 2
            while scenario_id in seen_scenario_ids:
                scenario_id = f"{base_id}-{suffix}"
                suffix += 1
            seen_scenario_ids.add(scenario_id)
            scenario = {**scenario, "id": scenario_id}
            prefixed.append(scenario)
            all_scenarios.append(scenario)
        scenarios_by_persona[persona["id"]] = prefixed

    cases = assemble_cases(personas, scenarios_by_persona, seed)
    manifest = {
        "name": name,
        "mcp": profile.get("mcp", "?"),
        "seed": seed,
        "created": utc_now(),
        "n_personas": len(personas),
        "scenarios_per_persona": scenarios_per_persona,
        "cases": cases,
    }
    return {"manifest": manifest, "personas": personas, "scenarios": all_scenarios}


def write_dataset(dataset: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    personas_dir = out_dir / "personas"
    scenarios_dir = out_dir / "scenarios"
    personas_dir.mkdir(exist_ok=True)
    scenarios_dir.mkdir(exist_ok=True)

    for persona in dataset["personas"]:
        (personas_dir / f"{persona['id']}.json").write_text(
            json.dumps(persona, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    for scenario in dataset["scenarios"]:
        (scenarios_dir / f"{scenario['id']}.json").write_text(
            json.dumps(scenario, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    manifest_path = out_dir / "dataset.json"
    manifest_path.write_text(
        json.dumps(dataset["manifest"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest_path


# --------------------------------------------------------------------------- #
# Running a dataset
# --------------------------------------------------------------------------- #
def run_dataset(
    dataset_dir: Path,
    *,
    target_path: Path,
    aut_runner_path: Path | None,
    user_runner_path: Path | None,
    output_dir: Path,
    limit: int | None = None,
) -> Path:
    manifest = json.loads((dataset_dir / "dataset.json").read_text(encoding="utf-8"))
    target = load_target(target_path)
    aut_runner = load_runner(aut_runner_path)
    user_runner = load_runner(user_runner_path)

    cases = manifest.get("cases", [])
    if limit is not None:
        cases = cases[:limit]

    results: list[dict[str, Any]] = []
    for index, case in enumerate(cases, start=1):
        persona = load_persona(dataset_dir / "personas" / f"{case['persona']}.json")
        scenario = load_scenario(dataset_dir / "scenarios" / f"{case['scenario']}.json")
        print(f"[{index}/{len(cases)}] running case {case['id']} ({case.get('intent', '?')})...")
        run = run_scenario(
            target=target,
            scenario=scenario,
            aut_runner_config=aut_runner,
            user_runner_config=user_runner,
            output_dir=output_dir,
            persona=persona,
        )
        results.append(
            {
                "case": case["id"],
                "persona": case["persona"],
                "scenario": case["scenario"],
                "intent": case.get("intent", ""),
                "status": run.status,
                "turns": run.turns,
                "run_dir": str(run.run_dir),
            }
        )
        print(f"    -> {run.status} ({run.turns} turns)")

    summary_dir = output_dir / f"{_summary_stamp()}-{manifest.get('name', 'dataset')}-summary"
    summary_dir.mkdir(parents=True, exist_ok=True)
    (summary_dir / "results.json").write_text(
        json.dumps({"dataset": manifest.get("name"), "results": results}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    summary_md = summary_dir / "summary.md"
    summary_md.write_text(_render_summary(manifest, results), encoding="utf-8")
    return summary_md


def _summary_stamp() -> str:
    return utc_now().replace("+00:00", "Z").replace(":", "")


def _render_summary(manifest: dict[str, Any], results: list[dict[str, Any]]) -> str:
    by_status: dict[str, int] = {}
    for row in results:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    lines = [
        f"# Dataset Run: {manifest.get('name', '?')}",
        "",
        f"- MCP: `{manifest.get('mcp', '?')}`",
        f"- Seed: `{manifest.get('seed', '?')}`",
        f"- Cases run: {len(results)}",
        "- Status counts: " + (", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "none"),
        "",
        "## Cases",
        "",
        "| case | intent | status | turns |",
        "| --- | --- | --- | --- |",
    ]
    for row in results:
        lines.append(
            f"| {row['case']} | {row['intent']} | {row['status']} | {row['turns']} |"
        )
    lines.append("")
    return "\n".join(lines)
