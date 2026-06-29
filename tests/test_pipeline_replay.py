from __future__ import annotations

import json
import shutil
from datetime import date

from ytmetrics.pipeline import run_pull
from ytmetrics.sources.replay import ReplaySource
from ytmetrics.store.sqlite_store import SqliteStore

from .conftest import make_channel

WINDOW = (date(2026, 6, 1), date(2026, 6, 3))


def test_full_offline_pull(tmp_path, fixtures_dir):
    ch = make_channel(tmp_path)
    src = ReplaySource(fixtures_dir)
    with SqliteStore(tmp_path / "t.db") as store:
        summary = run_pull(store, src, [ch], *WINDOW)
        c = summary.channels[0]
        assert c.ok and not c.degraded
        assert c.data_through == "2026-06-03"
        counts = store.table_counts()
        assert counts["channel_daily"] == 6
        assert counts["video_daily"] == 6
        assert counts["channel_revenue_daily"] == 3
        # Per-video facts populated from the new fixtures.
        assert counts["video_revenue_daily"] == 6
        assert counts["video_traffic_sources_daily"] == 7
        assert counts["video_discovery_daily"] == 6
        # Shorts vs long split is preserved.
        buckets = {
            r["bucket"]: r["views"]
            for r in store.query(
                "SELECT bucket, SUM(views) views FROM v_shorts_vs_long GROUP BY bucket"
            )
        }
        assert buckets["shorts"] > buckets["long_form"]


def test_video_content_type_tagged(tmp_path, fixtures_dir):
    ch = make_channel(tmp_path)
    with SqliteStore(tmp_path / "t.db") as store:
        run_pull(store, ReplaySource(fixtures_dir), [ch], *WINDOW)
        types = dict(store.query("SELECT video_id, content_type FROM videos"))
        assert types["shrtBBBBBBB"] == "SHORTS"
        assert types["vodAAAAAAAA"] == "VIDEO_ON_DEMAND"


def test_revenue_degrades_when_missing(tmp_path, fixtures_dir):
    # Copy fixtures, remove revenue.json -> revenue report should degrade, not fail.
    fx = tmp_path / "fixtures"
    shutil.copytree(fixtures_dir, fx)
    (fx / "main" / "revenue.json").unlink()
    ch = make_channel(tmp_path)
    with SqliteStore(tmp_path / "t.db") as store:
        summary = run_pull(store, ReplaySource(fx), [ch], *WINDOW)
        c = summary.channels[0]
        assert c.ok
        assert "revenue" in c.degraded
        assert store.table_counts()["channel_revenue_daily"] == 0
        assert store.table_counts()["channel_daily"] == 6  # core unaffected


def test_revenue_attribution_residual(tmp_path, fixtures_dir):
    ch = make_channel(tmp_path)
    with SqliteStore(tmp_path / "t.db") as store:
        run_pull(store, ReplaySource(fixtures_dir), [ch], *WINDOW)
        rows = {
            r["date"]: r
            for r in store.query(
                "SELECT date, channel_revenue, attributed_revenue, unattributed_revenue "
                "FROM v_revenue_attribution ORDER BY date"
            )
        }
        # Channel total legitimately exceeds the sum of per-video revenue; the residual
        # is surfaced as unattributed_revenue (2.15 channel vs 1.00+0.50 attributed).
        r1 = rows["2026-06-01"]
        assert round(r1["channel_revenue"], 2) == 2.15
        assert round(r1["attributed_revenue"], 2) == 1.50
        assert round(r1["unattributed_revenue"], 2) == 0.65


def test_video_revenue_lifetime(tmp_path, fixtures_dir):
    ch = make_channel(tmp_path)
    with SqliteStore(tmp_path / "t.db") as store:
        run_pull(store, ReplaySource(fixtures_dir), [ch], *WINDOW)
        rows = {
            r["video_id"]: r
            for r in store.query(
                "SELECT video_id, title, content_type, estimated_revenue "
                "FROM v_video_revenue_lifetime"
            )
        }
        vod = rows["vodAAAAAAAA"]
        assert vod["content_type"] == "VIDEO_ON_DEMAND"
        assert vod["title"] == "Long-form: Getting Started"
        # 1.00 + 0.80 + 1.50 over the window.
        assert round(vod["estimated_revenue"], 2) == 3.30


def test_revision_logged_across_pulls(tmp_path, fixtures_dir):
    fx = tmp_path / "fixtures"
    shutil.copytree(fixtures_dir, fx)
    ch = make_channel(tmp_path)
    db = tmp_path / "t.db"
    with SqliteStore(db) as store:
        run_pull(store, ReplaySource(fx), [ch], *WINDOW)

    # Bump a value in the fixture and re-pull.
    cd_path = fx / "main" / "channel_daily.json"
    data = json.loads(cd_path.read_text())
    data["rows"][0][2] = 999  # views on the first row
    cd_path.write_text(json.dumps(data))

    with SqliteStore(db) as store:
        run_pull(store, ReplaySource(fx), [ch], *WINDOW)
        revs = store.query("SELECT column_name, new_value FROM revision_log")
        assert any(r["column_name"] == "views" and r["new_value"] == "999" for r in revs)
