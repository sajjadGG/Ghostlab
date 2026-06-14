"""Stable, sortable public identifiers for stored entities.

Python 3.9 has no ``uuid7``, so we compose a lexicographically sortable id from
a microsecond UTC timestamp plus random hex. The result sorts in creation order
(useful for "latest first" listing) while staying globally unique in practice.
"""
from __future__ import annotations

import os
import re
import time

_PREFIXES = {
    "target",
    "target_revision",
    "inspection",
    "profile",
    "dataset",
    "persona",
    "scenario",
    "case",
    "run_batch",
    "run",
    "run_event",
    "tool_call",
    "judgment",
    "artifact",
}


def public_id(prefix: str) -> str:
    """Return a sortable unique id like ``run_000620f1a3c4-9f2b1c``.

    The middle segment is a zero-padded microsecond timestamp (hex) so ids sort
    in creation order; the suffix is random to avoid collisions within the same
    microsecond.
    """
    micros = time.time_ns() // 1000
    return f"{prefix}_{micros:014x}-{os.urandom(3).hex()}"


def slugify(text: str, fallback: str = "item") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or fallback
