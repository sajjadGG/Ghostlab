"""SQLite connection, PRAGMAs, and migration runner for Ghostlab persistence.

One short-lived connection per CLI operation. The Streamlit UI may keep a
connection factory but must not share a single connection across threads.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

from ..types import utc_now

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_DEFAULT_DB_NAME = "ghostlab.sqlite3"

_INIT_PRAGMAS = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA temp_store = MEMORY;",
)


def resolve_db_path(
    db: Optional[Union[str, Path]] = None,
    *,
    workspace: Optional[Union[str, Path]] = None,
) -> Path:
    """Resolve the database file path.

    Precedence: explicit ``db`` arg -> ``GHOSTLAB_DB`` env ->
    ``<workspace>/ghostlab.sqlite3`` -> ``./ghostlab.sqlite3``.
    """
    if db:
        return Path(db)
    env = os.environ.get("GHOSTLAB_DB")
    if env:
        return Path(env)
    if workspace:
        return Path(workspace) / _DEFAULT_DB_NAME
    return Path(_DEFAULT_DB_NAME)


def connect(db_path: Union[str, Path]) -> sqlite3.Connection:
    """Open a connection with row access by name and the standard PRAGMAs.

    Foreign keys are enabled per connection (a SQLite requirement).
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    for pragma in _INIT_PRAGMAS:
        conn.execute(pragma)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction: commit on success, rollback on error."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {int(row[0]) for row in rows}


def _migration_files() -> list[tuple[int, str, Path]]:
    """Return ordered (version, name, path) for each ``NNNN_name.sql`` file."""
    files: list[tuple[int, str, Path]] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        version_str, _, name = path.stem.partition("_")
        files.append((int(version_str), name or path.stem, path))
    return files


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied now."""
    _ensure_migrations_table(conn)
    applied = _applied_versions(conn)
    newly: list[int] = []
    for version, name, path in _migration_files():
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, utc_now()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        newly.append(version)
    return newly


def get_connection(
    db: Optional[Union[str, Path]] = None,
    *,
    workspace: Optional[Union[str, Path]] = None,
) -> sqlite3.Connection:
    """Resolve the path, connect, and apply pending migrations."""
    conn = connect(resolve_db_path(db, workspace=workspace))
    migrate(conn)
    return conn


def integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row[0] if row else "unknown"
