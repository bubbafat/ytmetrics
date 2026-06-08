from __future__ import annotations

from ytmetrics.store import backup
from ytmetrics.store.sqlite_store import SqliteStore


def test_snapshot_reused_same_day(tmp_path):
    db = tmp_path / "ytmetrics.db"
    bdir = tmp_path / "backups"
    with SqliteStore(db) as s:
        s.upsert("channels", [{"channel_id": "UC1", "title": "v1"}])
        s.commit()
    first = backup.daily_snapshot(db, bdir, day="2026-06-08")
    second = backup.daily_snapshot(db, bdir, day="2026-06-08")
    assert first == second
    assert len(backup.list_backups(bdir)) == 1


def test_restore_reverts_and_is_reversible(tmp_path):
    db = tmp_path / "ytmetrics.db"
    bdir = tmp_path / "backups"
    with SqliteStore(db) as s:
        s.upsert("channels", [{"channel_id": "UC1", "title": "orig"}])
        s.commit()
    backup.daily_snapshot(db, bdir, day="2026-06-08")
    with SqliteStore(db) as s:
        s.upsert("channels", [{"channel_id": "UC1", "title": "changed"}])
        s.commit()

    pre = backup.restore(db, backup.resolve_snapshot(bdir, latest=True))
    with SqliteStore(db) as s:
        assert s.channels()[0]["title"] == "orig"
    assert pre.exists()  # previous db preserved for un-restore


def test_prune_removes_old(tmp_path):
    db = tmp_path / "ytmetrics.db"
    bdir = tmp_path / "backups"
    with SqliteStore(db) as s:
        s.upsert("channels", [{"channel_id": "UC1"}])
        s.commit()
    backup.daily_snapshot(db, bdir, day="2026-05-01")
    backup.daily_snapshot(db, bdir, day="2026-06-08")
    removed = backup.prune(bdir, retention_days=14, today="2026-06-08")
    remaining = [b.date for b in backup.list_backups(bdir)]
    assert "2026-05-01" not in remaining
    assert "2026-06-08" in remaining
    assert len(removed) == 1
