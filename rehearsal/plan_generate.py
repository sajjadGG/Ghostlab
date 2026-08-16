"""Real, persona-grounded scenario generation for the test plan (roadmap A5 seam).

`rehearsal/plan.py`'s semantic/security suites start as *inert seeds*: one
placeholder per tool family, marked ``needs_generation`` and skipped by every
host. GhostLab already has a full generation engine — it just wasn't
connected to the plan. This module is that connection: it calls the same
Codex-backed pipeline `ghostlab generate-dataset` uses (capability profile →
personas → per-persona scenarios) and turns the result into real, runnable
plan cases.

Scenario ``intent`` routes the case to a suite: ``happy_path``/``edge_case``
land in ``semantic`` (does the assistant actually get the user's goal done?),
``adversarial`` lands in ``security`` (a persona pushing on a risk the
contract already flagged). Generated personas/scenarios are written to disk
once and reused across `plan` regenerations unless the caller asks to refresh
them — each persona/scenario is a real LLM call, so silent re-generation on
every `plan` invocation would be a surprise cost.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from .codex_backend import CodexBackend
from .dataset import build_dataset, write_dataset
from .profile import build_capability_profile
from .types import utc_now

DEFAULT_N_PERSONAS = 2
DEFAULT_SCENARIOS_PER_PERSONA = 2

ProgressFn = Callable[[dict[str, Any]], None]


def generate_conversational_dataset(
    inspect_data: dict[str, Any],
    backend: CodexBackend,
    *,
    spec_id: str,
    n_personas: int = DEFAULT_N_PERSONAS,
    scenarios_per_persona: int = DEFAULT_SCENARIOS_PER_PERSONA,
    seed: int = 0,
    progress: Optional[ProgressFn] = None,
    agent: "Optional[dict[str, Any]]" = None,
) -> dict[str, Any]:
    """Profile the target, then generate personas + per-persona scenarios.

    With ``agent``, the profile describes what the *configured agent* is for —
    its instructions, skills, subagents, and permissions — instead of only what
    its tools can do. An agent whose purpose lives in its prompt would otherwise
    get scenarios about tool families rather than about its job.

    Returns the same shape as :func:`rehearsal.dataset.build_dataset`
    (``manifest``/``personas``/``scenarios``), plus the profile used, so
    callers that also want the domain summary don't have to regenerate it.
    """
    if progress is not None:
        progress({"phase": "profile", "completed": 0, "total": 1,
                  "message": "Inferring agent purpose" if agent else
                             "Inferring capability profile"})
    if agent:
        from .agent_profile import as_capability_profile, build_agent_profile

        agent_profile = build_agent_profile(agent, backend, inspect_data)
        profile = as_capability_profile(agent_profile, inspect_data)
    else:
        profile = build_capability_profile(inspect_data, backend)
    if progress is not None:
        progress({"phase": "profile", "completed": 1, "total": 1,
                  "message": f"Profiled {profile.get('mcp', '?')}"})

    dataset = build_dataset(
        profile,
        backend,
        n_personas=n_personas,
        scenarios_per_persona=scenarios_per_persona,
        seed=seed,
        name=spec_id,
        progress=progress,
    )
    dataset["profile"] = profile
    return dataset


def write_conversational_dataset(dataset: dict[str, Any], out_dir: Path) -> Path:
    """Persist personas/scenarios/dataset.json under ``out_dir`` (see build_dataset)."""
    manifest_path = write_dataset(dataset, out_dir)
    if "profile" in dataset:
        profile = dataset["profile"]
        (out_dir / "profile.json").write_text(
            json.dumps(profile, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # The inferred purpose is the reviewable artifact for a configured
        # agent, so it is kept beside the adapted capability profile.
        if profile.get("agent_profile"):
            from .agent_profile import write_agent_profile

            write_agent_profile(profile["agent_profile"], out_dir)
    return manifest_path


# Scenario intent -> plan suite. `adversarial` scenarios are personas pushing
# on a risk (impatience, over-trust, ambiguity) rather than a clean goal, so
# they read as security-suite probes even though they're not contract-derived.
_INTENT_SUITE = {"happy_path": "semantic", "edge_case": "semantic", "adversarial": "security"}


def generated_dataset_to_cases(
    dataset_dir: Path, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Turn a written dataset's cases into plan-case dicts (see plan._case).

    Paths in ``execution`` are relative to ``dataset_dir`` so the plan stays
    portable if the workspace moves with the repo.
    """
    cases: list[dict[str, Any]] = []
    for case in manifest.get("cases", []):
        intent = str(case.get("intent", "happy_path"))
        suite = _INTENT_SUITE.get(intent, "semantic")
        exercises = case.get("exercises") or []
        cases.append({
            "id": f"{suite}-gen-{case['id']}",
            "suite": suite,
            "kind": "conversational",
            "title": f"Generated {intent} scenario: {case['scenario']}",
            "reason": f"generated_scenario:{intent}:{case['persona']}",
            "tools": list(exercises),
            "status": "proposed",
            "execution": {
                "type": "scenario",
                "generated": True,
                "scenario": str((dataset_dir / "scenarios" / f"{case['scenario']}.json")),
                "persona": str((dataset_dir / "personas" / f"{case['persona']}.json")),
            },
        })
    return cases


def load_generated_cases(dataset_dir: Path) -> list[dict[str, Any]]:
    """Reload plan cases from a previously written dataset dir (no Codex calls)."""
    manifest_path = dataset_dir / "dataset.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return generated_dataset_to_cases(dataset_dir, manifest)


def generation_dir_name() -> str:
    return utc_now().replace("+00:00", "Z").replace(":", "")
