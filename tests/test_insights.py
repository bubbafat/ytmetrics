from __future__ import annotations

import shutil
import sqlite3
from datetime import date

from ytmetrics.pipeline import run_insights, run_pull
from ytmetrics.sources.replay import ReplaySource
from ytmetrics.store import migrations
from ytmetrics.store.sqlite_store import SqliteStore

from .conftest import make_channel

WINDOW = (date(2026, 6, 1), date(2026, 6, 3))


def test_run_insights_populates_windowed_tables(tmp_path, fixtures_dir):
    ch = make_channel(tmp_path)
    src = ReplaySource(fixtures_dir)
    with SqliteStore(tmp_path / "t.db") as store:
        summary = run_insights(store, src, [ch], *WINDOW)
        c = summary.channels[0]
        assert c.ok and not c.degraded
        counts = store.table_counts()
        assert counts["channel_demographics"] == 6
        assert counts["audience_geography"] == 4
        assert counts["audience_devices"] == 4
        assert counts["traffic_source_detail"] == 4
        assert counts["video_retention"] == 8

        # Window is tagged from the requested [start, end].
        row = store.query(
            "SELECT window_start, window_end, subscribed_status FROM channel_demographics LIMIT 1"
        )[0]
        assert row["window_start"] == "2026-06-01"
        assert row["window_end"] == "2026-06-03"
        assert row["subscribed_status"] == "ALL"

        # Retention rows carry the elapsed-ratio curve per video, audience_type tagged.
        ret = store.query(
            "SELECT DISTINCT video_id, audience_type FROM video_retention ORDER BY video_id"
        )
        assert [(r["video_id"], r["audience_type"]) for r in ret] == [
            ("shrtBBBBBBB", "ORGANIC"),
            ("vodAAAAAAAA", "ORGANIC"),
        ]

        # Search terms land in traffic_source_detail (channel-level video_id='').
        terms = store.query(
            "SELECT detail, views FROM traffic_source_detail "
            "WHERE traffic_source_type = 'YT_SEARCH' ORDER BY views DESC"
        )
        assert terms[0]["detail"] == "how to get started"
        vids = store.query("SELECT DISTINCT video_id FROM traffic_source_detail")
        assert [r["video_id"] for r in vids] == [""]


def test_run_insights_does_not_touch_freshness(tmp_path, fixtures_dir):
    ch = make_channel(tmp_path)
    with SqliteStore(tmp_path / "t.db") as store:
        run_insights(store, ReplaySource(fixtures_dir), [ch], *WINDOW)
        # No daily data was pulled, so data_through stays unset.
        rows = store.query("SELECT data_through FROM channels")
        assert rows and rows[0]["data_through"] is None


def test_insights_degrade_when_fixtures_missing(tmp_path, fixtures_dir):
    fx = tmp_path / "fixtures"
    shutil.copytree(fixtures_dir, fx)
    for name in ("demographics.json", "geography.json", "devices.json",
                 "search_terms.json", "retention.json"):
        (fx / "main" / name).unlink()
    ch = make_channel(tmp_path)
    with SqliteStore(tmp_path / "t.db") as store:
        summary = run_insights(store, ReplaySource(fx), [ch], *WINDOW)
        c = summary.channels[0]
        assert c.ok
        assert set(c.degraded) == {
            "demographics", "geography", "devices", "traffic_source_detail", "video_retention",
        }
        assert store.table_counts()["audience_geography"] == 0


def test_subscribed_status_rides_the_daily_pull(tmp_path, fixtures_dir):
    ch = make_channel(tmp_path)
    with SqliteStore(tmp_path / "t.db") as store:
        summary = run_pull(store, ReplaySource(fixtures_dir), [ch], *WINDOW)
        assert summary.channels[0].ok and not summary.channels[0].degraded
        assert store.table_counts()["subscribed_status_daily"] == 6
        sub = store.query(
            "SELECT SUM(views) v FROM subscribed_status_daily WHERE subscribed_status='SUBSCRIBED'"
        )[0]
        assert sub["v"] == 320 + 280 + 410


def test_subscriber_count_threaded_onto_channels(tmp_path, fixtures_dir):
    fx = tmp_path / "fixtures"
    shutil.copytree(fixtures_dir, fx)
    import json
    meta_path = fx / "main" / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["subscriber_count"] = 12345
    meta_path.write_text(json.dumps(meta))
    ch = make_channel(tmp_path)
    with SqliteStore(tmp_path / "t.db") as store:
        run_pull(store, ReplaySource(fx), [ch], *WINDOW)
        row = store.query("SELECT subscriber_count FROM channels")[0]
        assert row["subscriber_count"] == 12345


def test_v3_alter_adds_subscriber_count_to_existing_db(tmp_path):
    """An existing v2 db (channels table without subscriber_count) gets the column via the
    v3 ALTER, not silently skipped by CREATE TABLE IF NOT EXISTS."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);")
    conn.execute(
        "CREATE TABLE channels (channel_id TEXT PRIMARY KEY, title TEXT, "
        "uploads_playlist_id TEXT, last_successful_pull TEXT, data_through TEXT);"
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
    conn.execute("INSERT INTO channels (channel_id, title) VALUES ('UCold', 'Old')")
    conn.commit()
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(channels)")}
    assert "subscriber_count" not in cols_before

    new_version = migrations.migrate(conn)
    assert new_version == migrations.CURRENT_SCHEMA_VERSION == 3
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(channels)")}
    assert "subscriber_count" in cols_after
    # Existing row preserved; new column NULL.
    row = conn.execute("SELECT title, subscriber_count FROM channels").fetchone()
    assert row[0] == "Old" and row[1] is None
    conn.close()
