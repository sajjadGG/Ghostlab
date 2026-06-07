"""Redact secrets out of target connection config before persistence.

Connection dicts look like one of:

    {"url": "...", "headers": {"Authorization": "Bearer xyz", ...}}
    {"command": "python", "args": [...], "env": {"API_KEY": "xyz", ...}}

We keep the shape and key names (so a run stays reproducible/inspectable) but
blank out every value inside ``headers`` / ``env`` and any value whose key looks
like a secret. Nothing sensitive should reach SQLite or the exported artifacts.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "***redacted***"

# Containers whose values are always secret-bearing.
_SECRET_CONTAINERS = {"headers", "env"}
# Key names that are secrets wherever they appear.
_SECRET_KEY_RE = re.compile(
    r"(token|password|passwd|secret|api[_-]?key|authorization|auth|bearer|credential)",
    re.IGNORECASE,
)


def _redact(value: Any, *, in_secret_container: bool) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            if in_secret_container or _SECRET_KEY_RE.search(str(key)):
                out[key] = REDACTED if val not in (None, "") else val
            elif str(key).lower() in _SECRET_CONTAINERS:
                out[key] = _redact(val, in_secret_container=True)
            else:
                out[key] = _redact(val, in_secret_container=False)
        return out
    if isinstance(value, list):
        return [_redact(item, in_secret_container=in_secret_container) for item in value]
    return value


def redact_connection(connection: dict[str, Any]) -> dict[str, Any]:
    """Return a redacted copy of a connection dict (input is not mutated)."""
    return _redact(connection, in_secret_container=False)
