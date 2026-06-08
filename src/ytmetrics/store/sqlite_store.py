"""The SQLite system of record: schema bootstrap, non-destructive merge-upsert with
revision logging, and small query helpers.

Upsert semantics (the retention guarantee):
- ``INSERT … ON CONFLICT(pk) DO UPDATE SET col = COALESCE(excluded.col, table.col)`` —
  a NULL incoming value never clobbers an existing non-NULL value, so a guarded/degraded
  report or a partial pull cannot erase data already captured.
- Keys absent from the incoming batch are left entirely untouched.
- When tracking is on, a changed value (old non-NULL, differs from new) is appended to
  ``revision_log`` before the update.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from ..timeutil import iso_now_pt
from . import migrations
from .schema import TABLES_BY_NAME, TableSpec

_CHUNK = 400


@dataclass
class UpsertResult:
    table: str
    inserted: int
    updated: int
    revisions: int


def _row_key(spec: TableSpec, row: Mapping[str, Any]) -> str:
    return "|".join(f"{c}={row.get(c)}" for c in spec.pk)


class SqliteStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------------
    def connect(self) -> SqliteStore:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA busy_timeout=5000;")
        self.conn = conn
        return self

    def ensure_schema(self) -> int:
        return migrations.migrate(self._conn)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> SqliteStore:
        self.connect()
        self.ensure_schema()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def _conn(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("SqliteStore is not connected")
        return self.conn

    # -- transactions ------------------------------------------------------------
    def begin(self) -> None:
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    # -- meta + channel freshness -----------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def set_channel_freshness(
        self, channel_id: str, last_successful_pull: str, data_through: str | None
    ) -> None:
        self._conn.execute(
            "INSERT INTO channels (channel_id, last_successful_pull, data_through) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET "
            "  last_successful_pull = excluded.last_successful_pull, "
            "  data_through = COALESCE(excluded.data_through, channels.data_through)",
            (channel_id, last_successful_pull, data_through),
        )

    # -- the merge-upsert --------------------------------------------------------
    def _fetch_existing(
        self, spec: TableSpec, pk_tuples: list[tuple]
    ) -> dict[tuple, sqlite3.Row]:
        if not pk_tuples:
            return {}
        left = spec.pk[0] if len(spec.pk) == 1 else "(" + ", ".join(spec.pk) + ")"
        out: dict[tuple, sqlite3.Row] = {}
        for i in range(0, len(pk_tuples), _CHUNK):
            chunk = pk_tuples[i : i + _CHUNK]
            row_ph = "(" + ", ".join(["?"] * len(spec.pk)) + ")"
            values_ph = ", ".join([row_ph] * len(chunk))
            sql = (
                f"SELECT {', '.join(spec.col_names)} FROM {spec.name} "
                f"WHERE {left} IN (VALUES {values_ph})"
            )
            params: list[Any] = [v for tup in chunk for v in tup]
            for r in self._conn.execute(sql, params):
                out[tuple(r[c] for c in spec.pk)] = r
        return out

    def upsert(
        self,
        table_name: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        track_revisions: bool = False,
        pulled_at: str | None = None,
    ) -> UpsertResult:
        spec = TABLES_BY_NAME[table_name]
        if not rows:
            return UpsertResult(table_name, 0, 0, 0)
        ts = pulled_at or iso_now_pt()
        track = track_revisions and spec.revisioned

        # Normalize: every column present; timestamp set; missing values -> None.
        norm: list[dict[str, Any]] = []
        for row in rows:
            rec = {c: row.get(c) for c in spec.col_names}
            if spec.ts_col:
                rec[spec.ts_col] = ts
            norm.append(rec)

        pk_tuples = [tuple(rec[c] for c in spec.pk) for rec in norm]
        existing = self._fetch_existing(spec, pk_tuples)

        revisions: list[tuple] = []
        inserted = updated = 0
        for rec, key in zip(norm, pk_tuples, strict=True):
            ex = existing.get(key)
            if ex is None:
                inserted += 1
                continue
            updated += 1
            if track:
                for col in spec.value_cols:
                    new_val = rec[col]
                    old_val = ex[col]
                    if new_val is not None and old_val is not None and old_val != new_val:
                        revisions.append(
                            (spec.name, _row_key(spec, rec), col, str(old_val), str(new_val), ts)
                        )

        cols = spec.col_names
        col_list = ", ".join(cols)
        placeholders = ", ".join(["?"] * len(cols))
        set_parts = [
            f"{c} = COALESCE(excluded.{c}, {spec.name}.{c})" for c in spec.value_cols
        ]
        if spec.ts_col:
            set_parts.append(f"{spec.ts_col} = excluded.{spec.ts_col}")
        conflict = ", ".join(spec.pk)
        sql = (
            f"INSERT INTO {spec.name} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({conflict}) DO UPDATE SET {', '.join(set_parts)}"
        )
        self._conn.executemany(sql, [[rec[c] for c in cols] for rec in norm])

        if revisions:
            self._conn.executemany(
                "INSERT INTO revision_log "
                "(table_name, row_key, column_name, old_value, new_value, pulled_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                revisions,
            )

        return UpsertResult(table_name, inserted, updated, len(revisions))

    # -- read helpers ------------------------------------------------------------
    def table_counts(self) -> dict[str, int]:
        names = list(TABLES_BY_NAME) + ["revision_log"]
        return {
            n: self._conn.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0] for n in names
        }

    def channel_date_coverage(self, channel_id: str) -> tuple[str | None, str | None]:
        row = self._conn.execute(
            "SELECT MIN(date), MAX(date) FROM channel_daily WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def channels(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM channels ORDER BY channel_id"))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, list(params)))
