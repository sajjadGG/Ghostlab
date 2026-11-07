"""Small helpers shared by every layer: file I/O, hashing, and JSON extraction."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_file(path: Path) -> Any:
    """Parse a JSON or YAML file (YAML for any suffix other than ``.json``)."""
    text = Path(path).read_text(encoding="utf-8")
    if Path(path).suffix.lower() == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def load_items(path: Path) -> list[Any]:
    """Read a list of items from ``.jsonl`` (one per line), JSON, or YAML."""
    path = Path(path)
    if path.suffix.lower() == ".jsonl":
        lines = path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    data = load_file(path)
    return data if isinstance(data, list) else [data]


def write_text(path: Path, text: str) -> None:
    """Write atomically, so a crash never leaves a half-written checkpoint."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def digest(value: Any, length: int = 12) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()[:length]


def slug(value: Any, limit: int = 48) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return text[:limit].rstrip("-") or "item"


def extract_json(text: str) -> Any:
    """The JSON value an agent replied with, tolerating prose and code fences.

    Tries, in order: the whole reply, the last fenced block, then the last
    top-level object or array that decodes cleanly.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for block in reversed(re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)):
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            continue
    decoder = json.JSONDecoder()
    found: Any = None
    index = 0
    while index < len(text):
        if text[index] in "{[":
            try:
                found, end = decoder.raw_decode(text, index)
                index = end
                continue
            except json.JSONDecodeError:
                pass
        index += 1
    if found is None:
        raise ValueError(f"no JSON value in agent reply: {text[:200]!r}")
    return found
