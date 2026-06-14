"""`rehearsal review-dataset` — inspect and curate a generated dataset.

Renders a coverage matrix, per-case previews, and flags (near-duplicate cases,
scenarios exercising non-existent tools, personas with no scenarios) so a human
can decide whether the dataset makes sense before spending agent credits.
Curation is file-first: case `status` lives in dataset.json and can be edited by
hand or via --approve/--reject; `run-dataset --approved-only` honors it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

VALID_STATUSES = ("pending", "approved", "rejected", "needs-edit")
_DUP_THRESHOLD = 0.8


def _family(name: str) -> str:
    return re.split(r"[._]", name)[0] if name else "other"


def _load_dir(path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not path.is_dir():
        return out
    for file in sorted(path.glob("*.json")):
        data = json.loads(file.read_text(encoding="utf-8"))
        out[data.get("id", file.stem)] = data
    return out


def load_dataset(dataset_dir: Path) -> dict[str, Any]:
    manifest = json.loads((dataset_dir / "dataset.json").read_text(encoding="utf-8"))
    personas = _load_dir(dataset_dir / "personas")
    scenarios = _load_dir(dataset_dir / "scenarios")
    return {"manifest": manifest, "personas": personas, "scenarios": scenarios}


def ensure_statuses(manifest: dict[str, Any]) -> bool:
    """Initialize a `status` field on every case. Returns True if anything changed."""
    changed = False
    for case in manifest.get("cases", []):
        if "status" not in case:
            case["status"] = "pending"
            changed = True
    return changed


def set_statuses(manifest: dict[str, Any], case_ids: set[str], status: str) -> list[str]:
    """Set status on the named cases (or all when case_ids is empty). Returns updated ids."""
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status} (use one of {', '.join(VALID_STATUSES)})")
    updated: list[str] = []
    for case in manifest.get("cases", []):
        if not case_ids or case["id"] in case_ids:
            case["status"] = status
            updated.append(case["id"])
    return updated


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _near_duplicate_flags(scenarios: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    items = list(scenarios.values())
    sigs = [
        (s.get("id", "?"), _tokens(s.get("goal", "") + " " + s.get("opening_message", "")))
        for s in items
    ]
    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            score = _jaccard(sigs[i][1], sigs[j][1])
            if score >= _DUP_THRESHOLD:
                flags.append(
                    {
                        "kind": "near_duplicate",
                        "detail": f"{sigs[i][0]} ~ {sigs[j][0]} (similarity {score:.2f})",
                    }
                )
    return flags


def build_review(dataset: dict[str, Any], profile: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = dataset["manifest"]
    personas = dataset["personas"]
    scenarios = dataset["scenarios"]
    cases = manifest.get("cases", [])

    by_intent: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for case in cases:
        by_intent[case.get("intent", "")] = by_intent.get(case.get("intent", ""), 0) + 1
        by_status[case.get("status", "pending")] = by_status.get(case.get("status", "pending"), 0) + 1

    flags: list[dict[str, Any]] = list(_near_duplicate_flags(scenarios))

    # Personas referenced by no case.
    used_personas = {c.get("persona") for c in cases}
    for persona_id in personas:
        if persona_id not in used_personas:
            flags.append({"kind": "persona_without_scenarios", "detail": persona_id})

    coverage: dict[str, Any] = {}
    if profile is not None:
        all_tools: set[str] = set()
        for names in profile.get("taxonomy", {}).values():
            all_tools.update(names)
        exercised: set[str] = set()
        for scenario in scenarios.values():
            for tool in scenario.get("exercises", []):
                if tool in all_tools:
                    exercised.add(tool)
                else:
                    flags.append(
                        {
                            "kind": "unknown_tool_exercised",
                            "detail": f"{scenario.get('id', '?')} exercises non-exposed `{tool}`",
                        }
                    )
        by_category: dict[str, dict[str, int]] = {}
        for family, names in profile.get("taxonomy", {}).items():
            covered = sum(1 for n in names if n in exercised)
            by_category[family] = {"total": len(names), "exercised": covered}
        coverage = {
            "exercised_tools": sorted(exercised),
            "unexercised_tools": sorted(all_tools - exercised),
            "by_category": by_category,
        }

    case_previews: list[dict[str, Any]] = []
    for case in cases:
        scenario = scenarios.get(case.get("scenario"), {})
        persona = personas.get(case.get("persona"), {})
        case_previews.append(
            {
                "id": case["id"],
                "status": case.get("status", "pending"),
                "intent": case.get("intent", ""),
                "persona": case.get("persona"),
                "scenario": case.get("scenario"),
                "persona_summary": persona.get("summary", ""),
                "persona_traits": persona.get("traits", []),
                "scenario_title": scenario.get("title", ""),
                "goal": scenario.get("goal", ""),
                "situation": scenario.get("persona", ""),
                "opening_message": scenario.get("opening_message", ""),
                "success_criteria": scenario.get("success_criteria", []),
                "failure_signals": scenario.get("failure_signals", []),
                "exercises": scenario.get("exercises", []),
                "max_turns": scenario.get("max_turns", case.get("max_turns")),
            }
        )

    return {
        "dataset": manifest.get("name", "?"),
        "mcp": manifest.get("mcp", "?"),
        "seed": manifest.get("seed"),
        "totals": {
            "personas": len(personas),
            "scenarios": len(scenarios),
            "cases": len(cases),
            "by_intent": by_intent,
            "by_status": by_status,
        },
        "coverage": coverage,
        "flags": flags,
        "cases": case_previews,
    }


def render_review_md(review: dict[str, Any]) -> str:
    totals = review["totals"]
    lines = [
        f"# Dataset Review: {review['dataset']}",
        "",
        f"- MCP: `{review['mcp']}`",
        f"- Seed: `{review['seed']}`",
        f"- Personas: {totals['personas']} | Scenarios: {totals['scenarios']} | Cases: {totals['cases']}",
        "- Intents: " + (", ".join(f"{k}={v}" for k, v in sorted(totals["by_intent"].items())) or "none"),
        "- Statuses: " + (", ".join(f"{k}={v}" for k, v in sorted(totals["by_status"].items())) or "none"),
        "",
    ]

    coverage = review.get("coverage") or {}
    if coverage:
        lines += ["## Tool coverage", ""]
        for family, stats in coverage.get("by_category", {}).items():
            lines.append(f"- `{family}`: {stats['exercised']}/{stats['total']} tools exercised")
        unexercised = coverage.get("unexercised_tools", [])
        if unexercised:
            lines.append("")
            lines.append("Never exercised: " + ", ".join(f"`{t}`" for t in unexercised))
        lines.append("")

    lines += ["## Flags", ""]
    if not review["flags"]:
        lines.append("None.")
    else:
        for flag in review["flags"]:
            lines.append(f"- **{flag['kind']}**: {flag['detail']}")
    lines.append("")

    lines += ["## Cases", ""]
    for case in review["cases"]:
        lines.append(f"### [{case['status']}] {case['id']}  _({case['intent']})_")
        lines.append(f"- persona: **{case['persona']}** — {case['persona_summary']}")
        if case["persona_traits"]:
            lines.append(f"- traits: {', '.join(case['persona_traits'])}")
        if case["situation"]:
            lines.append(f"- situation: {case['situation']}")
        lines.append(f"- goal: {case['goal']}")
        lines.append(f"- opening: \"{case['opening_message']}\"")
        if case["success_criteria"]:
            lines.append("- success criteria:")
            lines += [f"  - {c}" for c in case["success_criteria"]]
        if case["failure_signals"]:
            lines.append("- failure signals:")
            lines += [f"  - {c}" for c in case["failure_signals"]]
        lines.append(f"- exercises: {', '.join(f'`{t}`' for t in case['exercises']) or '(none)'}")
        lines.append("")

    return "\n".join(lines)


def write_review_artifacts(review: dict[str, Any], dataset_dir: Path) -> tuple[Path, Path]:
    json_path = dataset_dir / "review.json"
    md_path = dataset_dir / "review.md"
    json_path.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_review_md(review), encoding="utf-8")
    return json_path, md_path


def save_manifest(dataset_dir: Path, manifest: dict[str, Any]) -> None:
    (dataset_dir / "dataset.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
