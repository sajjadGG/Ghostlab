"""SQLite persistence for Ghostlab.

SQLite is the system of record for new pipeline writes; the existing
.md/.json/.jsonl artifacts keep being written as exports and are indexed by path.
See ``specs/sqlite-persistence.spec`` for the full design.
"""
from __future__ import annotations

from .db import connect, get_connection, integrity_check, migrate, resolve_db_path
from .repository import GhostlabStore

__all__ = [
    "GhostlabStore",
    "connect",
    "get_connection",
    "integrity_check",
    "migrate",
    "resolve_db_path",
]
