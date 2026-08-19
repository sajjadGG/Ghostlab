"""Local skill targets: discovery and conversion to the common inspect shape."""
from __future__ import annotations

import re
from pathlib import Path

from .config import ConfigError
from .inspect import InspectResult

_SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}
_SCRIPT_SUFFIXES = {".sh", ".bash", ".zsh", ".ps1", ".py", ".js", ".mjs", ".ts"}
_BINARY_SUFFIXES = {
    ".gz", ".tgz", ".zip", ".tar", ".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2",
}


def resolve_skill_path(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_dir():
        path = path / "SKILL.md"
    if not path.exists() or not path.is_file():
        raise ConfigError(f"Skill not found: {path} (expected a SKILL.md file or directory)")
    return path


def skill_root(path: Path) -> Path:
    """Directory that owns SKILL.md and any companion scripts/assets."""
    return resolve_skill_path(path).parent


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


def _file_kind(path: Path) -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name == "skill.md":
        return "skill"
    if name.startswith("license"):
        return "license"
    if suffix in _SCRIPT_SUFFIXES:
        return "script"
    if suffix in _BINARY_SUFFIXES:
        return "asset"
    return "file"


def list_skill_files(root: Path) -> list[dict[str, object]]:
    """Inventory every companion file a skill ships besides empty directories."""
    files: list[dict[str, object]] = []
    if not root.is_dir():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        files.append({
            "path": str(path.relative_to(root)),
            "size": path.stat().st_size,
            "kind": _file_kind(path),
        })
    return files


def skill_requires_shell(files: list[dict[str, object]]) -> bool:
    return any(entry.get("kind") == "script" for entry in files)


def inspect_skill(path: Path, target_id: str) -> InspectResult:
    """Read a skill folder into the artifact shape used by profiling/generation."""
    skill_file = resolve_skill_path(path)
    root = skill_file.parent
    text = skill_file.read_text(encoding="utf-8")
    meta = _frontmatter(text)
    name = meta.get("name") or root.name
    description = meta.get("description", "")
    files = list_skill_files(root)
    scripts = [str(entry["path"]) for entry in files if entry.get("kind") == "script"]
    resources = [
        {
            "uri": f"skill://{entry['path']}",
            "name": entry["path"],
            "mimeType": "application/octet-stream" if entry.get("kind") == "asset" else "text/plain",
            "description": f"{entry['kind']} ({entry['size']} bytes)",
        }
        for entry in files
        if entry.get("kind") != "skill"
    ]
    return InspectResult(
        target_id=target_id,
        transport="skill",
        server_info={"name": name, "version": "local", "target_type": "skill"},
        capabilities={
            "target_type": "skill",
            "description": description,
            "root": str(root),
            "files": files,
            "scripts": scripts,
            "requires_shell": skill_requires_shell(files),
        },
        instructions=text,
        resources=resources,
    )
