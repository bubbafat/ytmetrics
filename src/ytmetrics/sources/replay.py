"""ReplaySource — feed recorded fixtures through the same normalization as live.

Layout: ``<fixtures_root>/<channel_name>/`` containing
``meta.json`` ({channel_id, title, uploads_playlist_id}), ``videos.json`` (list of
videos-dim rows), and raw analytics responses ``channel_daily.json``,
``traffic_sources.json``, ``video_daily.json``, and optionally ``discovery.json`` /
``revenue.json`` (channel grain) plus ``video_revenue.json`` /
``video_traffic_sources.json`` / ``video_discovery.json`` (video grain), and
``subscribed_status.json`` (W5, daily). Windowed insights (W1–W4, W6) come from
``retention.json`` / ``demographics.json`` / ``geography.json`` / ``devices.json`` /
``search_terms.json`` via ``fetch_insights``. A missing optional file simulates a
degraded/unavailable report.
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
            subscriber_count=meta.get("subscriber_count"),
            channel_handle=meta.get("handle"),
        )

        cd = _load(cdir / "channel_daily.json")
        if cd is not None:
            batch.tables["channel_daily"] = _in_window(
                normalize.channel_daily_rows(cd, channel_id), s, e
            )

        ts = _load(cdir / "traffic_sources.json")
        if ts is not None:
            batch.tables["channel_traffic_sources_daily"] = _in_window(
                normalize.traffic_sources_rows(ts, channel_id), s, e
            )

        # W5: subscribed-status daily (optional ⇒ degrade if missing).
        ss = _load(cdir / "subscribed_status.json")
        if ss is not None:
            batch.tables["subscribed_status_daily"] = _in_window(
                normalize.subscribed_status_rows(ss, channel_id), s, e
            )
        else:
            batch.degraded.append("subscribed_status")

        content_types: dict[str, str] = {}
        vd = _load(cdir / "video_daily.json")
        if vd is not None:
            rows, content_types = normalize.video_daily_rows(vd, channel_id)
            batch.tables["video_daily"] = _in_window(rows, s, e)

        disc = _load(cdir / "discovery.json")
        if disc is not None:
            batch.tables["channel_discovery_daily"] = _in_window(
                normalize.discovery_rows(disc, channel_id), s, e
            )
        else:
            batch.degraded.append("discovery")

        if include_revenue:
            rev = _load(cdir / "revenue.json")
            if rev is not None:
                batch.tables["channel_revenue_daily"] = _in_window(
                    normalize.revenue_rows(rev, channel_id), s, e
                )
            else:
                batch.degraded.append("revenue")

        # Per-video facts (optional fixtures; a missing file simulates a degraded report).
        vts = _load(cdir / "video_traffic_sources.json")
        if vts is not None:
            batch.tables["video_traffic_sources_daily"] = _in_window(
                normalize.video_traffic_sources_rows(vts, channel_id), s, e
            )
        else:
            batch.degraded.append("video_traffic_sources")

        vdisc = _load(cdir / "video_discovery.json")
        if vdisc is not None:
            batch.tables["video_discovery_daily"] = _in_window(
                normalize.video_discovery_rows(vdisc, channel_id), s, e
            )
        else:
            batch.degraded.append("video_discovery")

        if include_revenue:
            vrev = _load(cdir / "video_revenue.json")
            if vrev is not None:
                batch.tables["video_revenue_daily"] = _in_window(
                    normalize.video_revenue_rows(vrev, channel_id), s, e
                )
            else:
                batch.degraded.append("video_revenue")

        videos = _load(cdir / "videos.json") or []
        for v in videos:
            v["channel_id"] = channel_id
            if v.get("video_id") in content_types:
                v["content_type"] = content_types[v["video_id"]]
        batch.videos = videos

        return batch

    def fetch_insights(
        self,
        channel: ChannelConfig,
        start: date,
        end: date,
        *,
        include_demographics: bool = True,
        heartbeat: Heartbeat | None = None,
    ) -> PullBatch:
        """Replay the windowed-insight fixtures (W1–W4, W6), tagging the requested window.

        Each fixture is optional; a missing file simulates a degraded/unavailable report.
        Fixtures hold raw analytics responses (no window column) — the window is applied
        here from the requested [start, end], mirroring the live path.
        """
        cdir = self.root / channel.name
        if not cdir.is_dir():
            raise FileNotFoundError(f"no replay fixtures for channel {channel.name!r} at {cdir}")
        ws, we = fmt_date(start), fmt_date(end)
        if heartbeat:
            heartbeat.update(f"channel={channel.name} replaying insight fixtures")

        meta = _load(cdir / "meta.json") or {}
        channel_id = meta.get("channel_id") or channel.channel_id
        batch = PullBatch(
            channel_id=channel_id,
            channel_title=meta.get("title"),
            uploads_playlist_id=meta.get("uploads_playlist_id"),
            subscriber_count=meta.get("subscriber_count"),
            channel_handle=meta.get("handle"),
        )

        if include_demographics:
            demo = _load(cdir / "demographics.json")
            if demo is not None:
                batch.tables["channel_demographics"] = normalize.demographics_rows(
                    demo, channel_id, ws, we
                )
            else:
                batch.degraded.append("demographics")

        geo = _load(cdir / "geography.json")
        if geo is not None:
            batch.tables["audience_geography"] = normalize.geography_rows(
                geo, channel_id, ws, we
            )
        else:
            batch.degraded.append("geography")

        dev = _load(cdir / "devices.json")
        if dev is not None:
            batch.tables["audience_devices"] = normalize.devices_rows(dev, channel_id, ws, we)
        else:
            batch.degraded.append("devices")

        # W6 search terms / traffic-source detail (channel-level, video_id='').
        terms = _load(cdir / "search_terms.json")
        if terms is not None:
            batch.tables["traffic_source_detail"] = normalize.traffic_source_detail_rows(
                terms, channel_id, "", "YT_SEARCH", ws, we
            )
        else:
            batch.degraded.append("traffic_source_detail")

        # W1 retention: one fixture holding rows for one or more videos. The fixture carries
        # ``video`` + ``audienceType`` columns (like the live response), so each row tags
        # itself; window/channel come from this call.
        ret = _load(cdir / "retention.json")
        if ret is not None:
            rows = normalize.normalize_rows(
                ret, {"channel_id": channel_id, "window_start": ws, "window_end": we}
            )
            for r in rows:
                r.setdefault("audience_type", "ORGANIC")
            batch.tables["video_retention"] = rows
        else:
            batch.degraded.append("video_retention")

        return batch
