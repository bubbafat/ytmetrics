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
    for index_sql in schema.INDEXES:
        conn.execute(index_sql)
    _create_views(conn)


def _create_views(conn: sqlite3.Connection) -> None:
    for name, ddl in schema.VIEWS.items():
        conn.execute(f"DROP VIEW IF EXISTS {name};")
        conn.execute(ddl)


def _v2_drop_renamed_channel_tables(conn: sqlite3.Connection) -> None:
    """v2 renamed the three channel-grain facts with a ``channel_`` prefix. The old data
    is disposable (the owner re-pulls), so drop the old-named tables outright."""
    for old in ("revenue_daily", "discovery_daily", "traffic_sources_daily"):
        conn.execute(f"DROP TABLE IF EXISTS {old};")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _v3_add_subscriber_count(conn: sqlite3.Connection) -> None:
    """v3 adds ``subscriber_count`` to the channels dim. New windowed/daily insight tables
    are created by _create_all (additive, IF NOT EXISTS); only the existing ``channels``
    table needs an ALTER since IF NOT EXISTS won't add a column to it."""
    if not _column_exists(conn, "channels", "subscriber_count"):
        conn.execute("ALTER TABLE channels ADD COLUMN subscriber_count INTEGER")


# Upgrade steps keyed by the version they produce. Empty for v1 (handled by _create_all).
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _v2_drop_renamed_channel_tables,
    3: _v3_add_subscriber_count,
}


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
