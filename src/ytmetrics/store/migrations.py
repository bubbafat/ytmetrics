"""Schema creation and a tiny version-keyed migration runner.

A fresh database is created at CURRENT_SCHEMA_VERSION. Future schema changes append a
step to ``_MIGRATIONS`` keyed by the version they upgrade *to*; the runner applies any
steps newer than the db's stored version. No ORM / migration framework.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from . import schema
from .schema import CURRENT_SCHEMA_VERSION


def _create_all(conn: sqlite3.Connection) -> None:
    conn.execute(schema.META_DDL)
    conn.execute(schema.REVISION_LOG_DDL)
    for spec in schema.UPSERT_TABLES:
        conn.execute(spec.create_sql())
    _create_views(conn)


def _create_views(conn: sqlite3.Connection) -> None:
    for name, ddl in schema.VIEWS.items():
        conn.execute(f"DROP VIEW IF EXISTS {name};")
        conn.execute(ddl)


# Upgrade steps keyed by the version they produce. Empty for v1 (handled by _create_all).
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


def get_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database schema up to CURRENT_SCHEMA_VERSION. Returns the new version."""
    fresh = not _table_exists(conn, "meta")
    _create_all(conn)  # idempotent (IF NOT EXISTS); also refreshes views

    if fresh:
        _set_version(conn, CURRENT_SCHEMA_VERSION)
        conn.commit()
        return CURRENT_SCHEMA_VERSION

    version = get_version(conn)
    for target in sorted(_MIGRATIONS):
        if target > version:
            _MIGRATIONS[target](conn)
            _set_version(conn, target)
            version = target
    if version < CURRENT_SCHEMA_VERSION:
        _set_version(conn, CURRENT_SCHEMA_VERSION)
        version = CURRENT_SCHEMA_VERSION
    conn.commit()
    return version
