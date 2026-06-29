"""Windowed insight snapshots are pruned to a rolling `insights_retention_weeks` window;
the daily fact tables are never touched."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ytmetrics.config import load_config
from ytmetrics.store.sqlite_store import SqliteStore


def _ret_row(video_id: str, window_end: str) -> dict:
    return {
        "channel_id": "UC",
        "video_id": video_id,
        "audience_type": "ORGANIC",
        "elapsed_ratio": 0.5,
        "audience_watch_ratio": 0.8,
        "relative_retention_performance": 0.5,
        "window_start": "2026-01-01",
        "window_end": window_end,
    }


def test_prune_drops_snapshots_older_than_retention(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        s.upsert("video_retention", [
            _ret_row("vOld", "2026-04-30"),   # > 4 weeks before today -> dropped
            _ret_row("vNew", "2026-06-27"),   # recent -> kept
        ])
        removed = s.prune_insight_snapshots(4, today=date(2026, 6, 29))
        assert removed.get("video_retention") == 1
        ends = {r["window_end"] for r in s.query("SELECT window_end FROM video_retention")}
        assert ends == {"2026-06-27"}


def test_retention_zero_disables_pruning(tmp_path):
    with SqliteStore(tmp_path / "t.db") as s:
        s.upsert("video_retention", [_ret_row("vAncient", "2020-01-01")])
        assert s.prune_insight_snapshots(0, today=date(2026, 6, 29)) == {}
        assert len(s.query("SELECT 1 FROM video_retention")) == 1


def test_prune_leaves_daily_facts_alone(tmp_path):
    """A daily fact table has no window_end and must be ignored by the prune."""
    with SqliteStore(tmp_path / "t.db") as s:
        s.upsert("video_daily", [{
            "channel_id": "UC", "video_id": "v1", "date": "2020-01-01",
            "content_type": "VIDEO_ON_DEMAND", "views": 5,
        }])
        s.prune_insight_snapshots(4, today=date(2026, 6, 29))
        assert len(s.query("SELECT 1 FROM video_daily")) == 1  # untouched


def test_config_default_retention_weeks(tmp_path):
    cfg_path = Path(tmp_path) / "config.toml"
    cfg_path.write_text('[[channels]]\nname = "main"\nchannel_id = "mine"\n')
    assert load_config(cfg_path).insights_retention_weeks == 26
