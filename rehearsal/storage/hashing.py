"""Content hashing for immutable-snapshot dedup.

Snapshots (target revisions, profiles, personas, scenarios) are content-addressed
so re-recording identical content reuses the existing row instead of duplicating.
The hash is over canonical JSON: sorted keys, compact separators, UTF-8.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_sha256(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
