"""Local skill targets: discovery and conversion to the common inspect shape."""
from __future__ import annotations

import re
from pathlib import Path

from .config import ConfigError
from .inspect import InspectResult


def resolve_skill_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_dir():
        path = path / "SKILL.md"
    if not path.exists() or not path.is_file():
        raise ConfigError(f"Skill not found: {path} (expected a SKILL.md file or directory)")
    return path


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    values: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, sep, value = line.partition(":")
        if sep and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key.strip()):
            values[key.strip()] = value.strip().strip("\"'")
    return values


def inspect_skill(path: Path, target_id: str) -> InspectResult:
    """Read a SKILL.md into the artifact shape used by profiling/generation."""
    skill_path = resolve_skill_path(path)
    text = skill_path.read_text(encoding="utf-8")
    meta = _frontmatter(text)
    name = meta.get("name") or skill_path.parent.name
    description = meta.get("description", "")
    return InspectResult(
        target_id=target_id,
        transport="skill",
        server_info={"name": name, "version": "local", "target_type": "skill"},
        capabilities={"target_type": "skill", "description": description},
        instructions=text,
    )
