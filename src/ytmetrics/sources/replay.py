"""ReplaySource — feed recorded fixtures through the same normalization as live.

Layout: ``<fixtures_root>/<channel_name>/`` containing
``meta.json`` ({channel_id, title, uploads_playlist_id}), ``videos.json`` (list of
videos-dim rows), and raw analytics responses ``channel_daily.json``,
``traffic_sources.json``, ``video_daily.json``, and optionally ``discovery.json`` /
``revenue.json``. A missing optional file simulates a degraded/unavailable report.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ..config import ChannelConfig
from ..status import Heartbeat
from ..timeutil import fmt_date
from . import normalize
from .base import PullBatch, Source


def _load(path: Path) -> Any | None:
    if not path.is_file():
        return None
    with path.open("rb") as fh:
        return json.load(fh)


def _in_window(rows: list[dict], start: str, end: str) -> list[dict]:
    return [r for r in rows if "date" not in r or start <= r["date"] <= end]


class ReplaySource(Source):
    def __init__(self, fixtures_root: str | Path):
        self.root = Path(fixtures_root)

    def fetch_reports(
        self,
        channel: ChannelConfig,
        start: date,
        end: date,
        *,
        include_revenue: bool,
        heartbeat: Heartbeat | None = None,
    ) -> PullBatch:
        cdir = self.root / channel.name
        if not cdir.is_dir():
            raise FileNotFoundError(f"no replay fixtures for channel {channel.name!r} at {cdir}")
        s, e = fmt_date(start), fmt_date(end)
        if heartbeat:
            heartbeat.update(f"channel={channel.name} replaying fixtures")

        meta = _load(cdir / "meta.json") or {}
        channel_id = meta.get("channel_id") or channel.channel_id
        batch = PullBatch(
            channel_id=channel_id,
            channel_title=meta.get("title"),
            uploads_playlist_id=meta.get("uploads_playlist_id"),
        )

        cd = _load(cdir / "channel_daily.json")
        if cd is not None:
            batch.tables["channel_daily"] = _in_window(
                normalize.channel_daily_rows(cd, channel_id), s, e
            )

        ts = _load(cdir / "traffic_sources.json")
        if ts is not None:
            batch.tables["traffic_sources_daily"] = _in_window(
                normalize.traffic_sources_rows(ts, channel_id), s, e
            )

        content_types: dict[str, str] = {}
        vd = _load(cdir / "video_daily.json")
        if vd is not None:
            rows, content_types = normalize.video_daily_rows(vd, channel_id)
            batch.tables["video_daily"] = _in_window(rows, s, e)

        disc = _load(cdir / "discovery.json")
        if disc is not None:
            batch.tables["discovery_daily"] = _in_window(
                normalize.discovery_rows(disc, channel_id), s, e
            )
        else:
            batch.degraded.append("discovery")

        if include_revenue:
            rev = _load(cdir / "revenue.json")
            if rev is not None:
                batch.tables["revenue_daily"] = _in_window(
                    normalize.revenue_rows(rev, channel_id), s, e
                )
            else:
                batch.degraded.append("revenue")

        videos = _load(cdir / "videos.json") or []
        for v in videos:
            v["channel_id"] = channel_id
            if v.get("video_id") in content_types:
                v["content_type"] = content_types[v["video_id"]]
        batch.videos = videos

        return batch
