"""LiveSource — the only component that touches the network.

Builds the YouTube Analytics + Data API services from stored credentials, runs the
reports with backoff, guards optional metrics/dimensions so the core pull never fails,
and chunks per-video-daily by month to stay under per-response row limits.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .. import auth
from ..config import ChannelConfig
from ..retry import with_retries
from ..status import Heartbeat, progress
from ..timeutil import fmt_date, month_chunks
from . import normalize
from .base import CallCounter, PullBatch, Source

_VIDEO_ID_CHUNK = 500  # max ids per filters=video== clause
_META_CHUNK = 50  # max ids per videos.list / playlistItems page

_CHANNEL_METRICS = [
    "views", "engagedViews", "redViews", "estimatedMinutesWatched",
    "estimatedRedMinutesWatched", "averageViewDuration", "averageViewPercentage",
    "subscribersGained", "subscribersLost", "likes", "dislikes", "comments", "shares",
]
_CHANNEL_METRICS_FALLBACK = [
    "views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
    "subscribersGained", "subscribersLost", "likes", "dislikes", "comments", "shares",
]
_VIDEO_METRICS = [
    "views", "engagedViews", "estimatedMinutesWatched", "averageViewDuration",
    "averageViewPercentage", "likes", "dislikes", "comments", "shares", "subscribersGained",
]
_VIDEO_METRICS_FALLBACK = [
    "views", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
    "likes", "dislikes", "comments", "shares", "subscribersGained",
]
# Discovery is split: card metrics are broadly available; the newer thumbnail-impression
# metrics are often not served by the API, so they're attempted separately and merged.
_CARD_METRICS = ["cardImpressions", "cardClickRate"]
_THUMBNAIL_METRICS = ["videoThumbnailImpressions", "videoThumbnailImpressionsClickRate"]
_REVENUE_METRICS = [
    "estimatedRevenue", "estimatedAdRevenue", "estimatedRedPartnerRevenue",
    "grossRevenue", "cpm", "playbackBasedCpm", "monetizedPlaybacks", "adImpressions",
]
_REFERRER_SOURCE_TYPES = ["RELATED_VIDEO", "END_SCREEN", "VIDEO_REMIXES"]

_DURATION_RE = re.compile(
    r"P(?:(?P<days>\d+)D)?T?(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?"
)


def _duration_seconds(iso: str | None) -> int | None:
    if not iso:
        return None
    m = _DURATION_RE.fullmatch(iso)
    if not m:
        return None
    days = int(m.group("days") or 0)
    h = int(m.group("h") or 0)
    mins = int(m.group("m") or 0)
    s = int(m.group("s") or 0)
    return days * 86400 + h * 3600 + mins * 60 + s


def _status(exc: HttpError) -> int | None:
    s = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(s) if s is not None else None
    except (TypeError, ValueError):
        return None


class LiveSource(Source):
    def __init__(
        self,
        max_api_calls: int,
        *,
        interactive: bool = False,
        logger: logging.Logger | None = None,
    ):
        self.counter = CallCounter(max_api_calls)
        self.interactive = interactive
        self.log = logger or logging.getLogger("ytmetrics")
        self._services: dict[str, tuple] = {}

    # -- service construction ----------------------------------------------------
    def services(self, channel: ChannelConfig):
        if channel.name not in self._services:
            creds = auth.get_credentials(channel, interactive=self.interactive)
            analytics = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
            data = build("youtube", "v3", credentials=creds, cache_discovery=False)
            self._services[channel.name] = (analytics, data)
        return self._services[channel.name]

    # -- low-level query ---------------------------------------------------------
    def _query(self, analytics, *, ids: str, start: date, end: date, metrics: list[str],
               dimensions: str | None = None, filters: str | None = None,
               sort: str | None = None, max_results: int | None = None, label: str = "query"):
        self.counter.tick(label)
        params: dict[str, object] = {
            "ids": ids,
            "startDate": fmt_date(start),
            "endDate": fmt_date(end),
            "metrics": ",".join(metrics),
        }
        if dimensions:
            params["dimensions"] = dimensions
        if filters:
            params["filters"] = filters
        if sort:
            params["sort"] = sort
        if max_results:
            params["maxResults"] = max_results
        return with_retries(
            lambda: analytics.reports().query(**params).execute(),
            label=label, logger=self.log,
        )

    # -- channel / video discovery ----------------------------------------------
    def list_owned_channels(self, channel: ChannelConfig) -> list[dict]:
        _, data = self.services(channel)
        self.counter.tick("channels.list(mine)")
        resp = with_retries(
            lambda: data.channels().list(part="snippet,contentDetails", mine=True).execute(),
            label="channels.list", logger=self.log,
        )
        out = []
        for item in resp.get("items", []):
            out.append({
                "channel_id": item["id"],
                "title": item.get("snippet", {}).get("title"),
                "uploads_playlist_id":
                    item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
            })
        return out

    def resolve_channel(self, channel: ChannelConfig) -> dict:
        _, data = self.services(channel)
        self.counter.tick("channels.list")
        if channel.channel_id.lower() == "mine":
            req = data.channels().list(part="snippet,contentDetails", mine=True)
        else:
            req = data.channels().list(part="snippet,contentDetails", id=channel.channel_id)
        resp = with_retries(lambda: req.execute(), label="channels.list", logger=self.log)
        items = resp.get("items", [])
        if not items:
            raise RuntimeError(f"channel not found for {channel.name!r} ({channel.channel_id})")
        item = items[0]
        return {
            "channel_id": item["id"],
            "title": item.get("snippet", {}).get("title"),
            "uploads_playlist_id":
                item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
        }

    def _list_video_ids(self, channel: ChannelConfig, uploads_playlist_id: str) -> list[str]:
        _, data = self.services(channel)
        ids: list[str] = []
        page = None
        while True:
            self.counter.tick("playlistItems.list")
            req = data.playlistItems().list(
                part="contentDetails", playlistId=uploads_playlist_id,
                maxResults=_META_CHUNK, pageToken=page,
            )
            resp = with_retries(req.execute, label="playlistItems.list", logger=self.log)
            for it in resp.get("items", []):
                vid = it.get("contentDetails", {}).get("videoId")
                if vid:
                    ids.append(vid)
            page = resp.get("nextPageToken")
            if not page:
                break
        return ids

    def _video_metadata(self, channel: ChannelConfig, ids: list[str]) -> list[dict]:
        _, data = self.services(channel)
        rows: list[dict] = []
        for i in range(0, len(ids), _META_CHUNK):
            chunk = ids[i : i + _META_CHUNK]
            self.counter.tick("videos.list")
            req = data.videos().list(
                part="snippet,contentDetails,status", id=",".join(chunk), maxResults=_META_CHUNK,
            )
            resp = with_retries(req.execute, label="videos.list", logger=self.log)
            for it in resp.get("items", []):
                rows.append({
                    "video_id": it["id"],
                    "title": it.get("snippet", {}).get("title"),
                    "published_at": it.get("snippet", {}).get("publishedAt"),
                    "duration_seconds":
                        _duration_seconds(it.get("contentDetails", {}).get("duration")),
                    "privacy_status": it.get("status", {}).get("privacyStatus"),
                })
        return rows

    # -- the reports -------------------------------------------------------------
    def fetch_reports(
        self,
        channel: ChannelConfig,
        start: date,
        end: date,
        *,
        include_revenue: bool,
        heartbeat: Heartbeat | None = None,
    ) -> PullBatch:
        analytics, _ = self.services(channel)
        info = self.resolve_channel(channel)
        cid = info["channel_id"]
        ids = f"channel=={cid}"
        batch = PullBatch(
            channel_id=cid,
            channel_title=info["title"],
            uploads_playlist_id=info["uploads_playlist_id"],
        )

        def hb(msg: str) -> None:
            if heartbeat:
                heartbeat.update(f"channel={channel.name} {msg}")

        # 1. channel_daily (guarded: drop creatorContentType + engagedViews/redViews on 400)
        hb("channel_daily")
        try:
            resp = self._query(analytics, ids=ids, start=start, end=end,
                               metrics=_CHANNEL_METRICS, dimensions="day,creatorContentType",
                               label="channel_daily")
        except HttpError as exc:
            if _status(exc) == 400:
                self.log.warning("channel_daily degraded (dropping content-type split): %s", exc)
                resp = self._query(analytics, ids=ids, start=start, end=end,
                                   metrics=_CHANNEL_METRICS_FALLBACK, dimensions="day",
                                   label="channel_daily.fallback")
                batch.degraded.append("channel_daily.creator_content_type")
            else:
                raise
        batch.tables["channel_daily"] = normalize.channel_daily_rows(resp, cid)

        # 2. traffic_sources
        hb("traffic_sources")
        resp = self._query(analytics, ids=ids, start=start, end=end,
                           metrics=["views", "estimatedMinutesWatched"],
                           dimensions="day,insightTrafficSourceType", label="traffic_sources")
        batch.tables["traffic_sources_daily"] = normalize.traffic_sources_rows(resp, cid)

        # 3. video_daily (uploads -> ids -> chunked by month and by <=500 ids)
        hb("listing videos")
        video_ids = self._list_video_ids(channel, info["uploads_playlist_id"]) \
            if info["uploads_playlist_id"] else []
        batch.videos = self._video_metadata(channel, video_ids) if video_ids else []
        vd_rows, content_types = self._fetch_video_daily(
            analytics, cid, ids, video_ids, start, end, heartbeat
        )
        batch.tables["video_daily"] = vd_rows
        for v in batch.videos:
            if v["video_id"] in content_types:
                v["content_type"] = content_types[v["video_id"]]

        # 4. discovery (best-effort; card metrics usually work, thumbnail metrics often
        #    are not served by the API — query them separately and merge by date).
        hb("discovery")
        merged: dict[str, dict] = {}
        card_ok = self._merge_discovery(analytics, ids, cid, start, end,
                                        _CARD_METRICS, "discovery.card", merged)
        self._merge_discovery(analytics, ids, cid, start, end,
                              _THUMBNAIL_METRICS, "discovery.thumbnail", merged)
        if merged:
            batch.tables["discovery_daily"] = list(merged.values())
        if not card_ok:  # the reliable half failed -> surface a degradation
            batch.degraded.append("discovery")

        # 5. revenue (optional, graceful-degrade)
        if include_revenue:
            hb("revenue")
            try:
                resp = self._query(analytics, ids=ids, start=start, end=end,
                                   metrics=_REVENUE_METRICS, dimensions="day", label="revenue")
                batch.tables["revenue_daily"] = normalize.revenue_rows(resp, cid)
            except HttpError as exc:
                self.log.warning("revenue report unavailable (monetary scope / not in YPP?): %s",
                                 exc)
                batch.degraded.append("revenue")

        return batch

    def _merge_discovery(self, analytics, ids, cid, start, end, metrics, label, merged) -> bool:
        """Query a discovery metric group and merge rows into ``merged`` keyed by date.

        Returns True on success, False if the API doesn't support the group (logged, not
        fatal) — lets card metrics land even when thumbnail metrics are unavailable.
        """
        try:
            resp = self._query(analytics, ids=ids, start=start, end=end,
                               metrics=metrics, dimensions="day", label=label)
        except HttpError as exc:
            self.log.info("%s unavailable, skipping (status %s)", label, _status(exc))
            return False
        for r in normalize.discovery_rows(resp, cid):
            merged.setdefault(r["date"], {"channel_id": cid, "date": r["date"]}).update(r)
        return True

    def _fetch_video_daily(self, analytics, cid, ids, video_ids, start, end, heartbeat):
        rows: list[dict] = []
        content_types: dict[str, str] = {}
        if not video_ids:
            return rows, content_types
        chunks = month_chunks(start, end)
        total = len(chunks)
        for idx, (cs, ce) in enumerate(chunks, start=1):
            if heartbeat:
                heartbeat.update(f"video_daily month {idx}/{total} ({cs:%Y-%m})")
            for j in range(0, len(video_ids), _VIDEO_ID_CHUNK):
                id_chunk = video_ids[j : j + _VIDEO_ID_CHUNK]
                vfilter = "video==" + ",".join(id_chunk)
                try:
                    resp = self._query(
                        analytics, ids=ids, start=cs, end=ce, metrics=_VIDEO_METRICS,
                        dimensions="video,day,creatorContentType", filters=vfilter,
                        label="video_daily",
                    )
                except HttpError as exc:
                    if _status(exc) == 400:
                        resp = self._query(
                            analytics, ids=ids, start=cs, end=ce, metrics=_VIDEO_METRICS_FALLBACK,
                            dimensions="video,day", filters=vfilter, label="video_daily.fallback",
                        )
                    else:
                        raise
                chunk_rows, cts = normalize.video_daily_rows(resp, cid)
                rows.extend(chunk_rows)
                content_types.update(cts)
            progress(f"[backfill] {cs:%Y-%m} video_daily: {len(rows)} rows ({idx}/{total})")
        return rows, content_types

    # -- referrers (on-demand attribution) --------------------------------------
    def fetch_referrers(
        self, channel: ChannelConfig, dest_video_id: str, start: date, end: date
    ) -> list[dict]:
        analytics, _ = self.services(channel)
        info = self.resolve_channel(channel)
        cid = info["channel_id"]
        ids = f"channel=={cid}"
        out: list[dict] = []
        for source_type in _REFERRER_SOURCE_TYPES:
            vfilter = f"video=={dest_video_id};insightTrafficSourceType=={source_type}"
            try:
                resp = self._query(
                    analytics, ids=ids, start=start, end=end,
                    metrics=["views", "estimatedMinutesWatched"],
                    dimensions="insightTrafficSourceDetail", filters=vfilter,
                    sort="-views", max_results=25, label=f"referrers.{source_type}",
                )
            except HttpError as exc:
                self.log.warning("referrers %s unavailable: %s", source_type, exc)
                continue
            out.extend(
                normalize.referrer_rows(
                    resp, cid, dest_video_id, source_type, fmt_date(start), fmt_date(end)
                )
            )
        return out
