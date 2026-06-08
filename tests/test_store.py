from __future__ import annotations

from ytmetrics.store.sqlite_store import SqliteStore


def _row(**over):
    base = dict(
        channel_id="UC1", date="2026-06-01", creator_content_type="VIDEO_ON_DEMAND",
        views=100, engaged_views=90, red_views=5, estimated_minutes_watched=300,
        subscribers_gained=3, subscribers_lost=1, likes=10, comments=2, shares=1,
    )
    base.update(over)
    return base


def test_insert_then_idempotent(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        r1 = s.upsert("channel_daily", [_row()], track_revisions=True)
        s.commit()
        assert (r1.inserted, r1.updated) == (1, 0)
        r2 = s.upsert("channel_daily", [_row()], track_revisions=True)
        s.commit()
        assert (r2.inserted, r2.updated, r2.revisions) == (0, 1, 0)
        assert s.table_counts()["channel_daily"] == 1


def test_null_does_not_clobber(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        s.upsert("channel_daily", [_row(engaged_views=90)], track_revisions=True)
        s.commit()
        s.upsert("channel_daily", [_row(views=150, engaged_views=None)], track_revisions=True)
        s.commit()
        row = s.query("SELECT views, engaged_views FROM channel_daily")[0]
        assert row["views"] == 150  # provided value updates
        assert row["engaged_views"] == 90  # NULL incoming retained old value


def test_revision_logged_on_change(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        s.upsert("channel_daily", [_row(views=100)], track_revisions=True)
        s.commit()
        s.upsert("channel_daily", [_row(views=150)], track_revisions=True)
        s.commit()
        revs = s.query("SELECT column_name, old_value, new_value FROM revision_log")
        assert [(r["column_name"], r["old_value"], r["new_value"]) for r in revs] == [
            ("views", "100", "150")
        ]


def test_revision_not_logged_when_disabled(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        s.upsert("channel_daily", [_row(views=100)], track_revisions=False)
        s.commit()
        s.upsert("channel_daily", [_row(views=150)], track_revisions=False)
        s.commit()
        assert s.table_counts()["revision_log"] == 0


def test_absent_keys_retained(tmp_path):
    """A pull that doesn't mention an existing key must not delete it (retention)."""
    with SqliteStore(tmp_path / "t.db") as s:
        s.upsert("channel_daily", [_row(date="2026-05-01")], track_revisions=True)
        s.commit()
        # A later pull for a different date leaves the old row untouched.
        s.upsert("channel_daily", [_row(date="2026-06-09")], track_revisions=True)
        s.commit()
        dates = [r["date"] for r in s.query("SELECT date FROM channel_daily ORDER BY date")]
        assert dates == ["2026-05-01", "2026-06-09"]
