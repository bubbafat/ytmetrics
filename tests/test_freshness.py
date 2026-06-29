"""Staleness detection + the 'big red banner' the emails show when the pull has stalled."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from ytmetrics import freshness
from ytmetrics.store.sqlite_store import SqliteStore


def _db_with_latest(tmp_path: Path, latest_iso: str) -> Path:
    db = tmp_path / "t.db"
    with SqliteStore(db):   # opening creates the schema
        pass
    c = sqlite3.connect(db)
    c.execute(
        "INSERT INTO channel_daily (channel_id, date, creator_content_type, views) "
        "VALUES (?, ?, ?, ?)",
        ("UC", latest_iso, "VIDEO_ON_DEMAND", 10),
    )
    c.commit()
    c.close()
    return db


def test_days_behind(tmp_path):
    db = _db_with_latest(tmp_path, "2026-06-20")
    latest, n = freshness.days_behind(db, today=date(2026, 6, 27))
    assert latest == "2026-06-20"
    assert n == 7


def test_is_stale_threshold(tmp_path):
    db = _db_with_latest(tmp_path, "2026-06-20")
    assert freshness.is_stale(db, 3, today=date(2026, 6, 27))[0] is True    # 7 > 3
    assert freshness.is_stale(db, 10, today=date(2026, 6, 27))[0] is False  # 7 < 10


def test_banners_are_obvious():
    assert "STALE DATA" in freshness.stale_text_banner("2026-06-20", 7)
    html = freshness.stale_html_banner("2026-06-20", 7)
    assert "STALE DATA" in html
    assert "background:#C5221F" in html   # the red box
