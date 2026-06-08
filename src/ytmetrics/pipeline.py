"""Orchestration: pull each channel through source → normalize → store.

Per-channel transaction boundary: a channel's writes commit or roll back atomically, and
one channel failing never stops the others (isolation). The store's merge-upsert provides
the retention guarantee; this module just wires sources to it and aggregates a summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from .config import ChannelConfig
from .sources.base import PullBatch, Source
from .status import Heartbeat
from .store.sqlite_store import SqliteStore, UpsertResult
from .timeutil import iso_now_pt


@dataclass
class ChannelSummary:
    name: str
    channel_id: str
    ok: bool
    error: str | None = None
    upserts: dict[str, UpsertResult] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    data_through: str | None = None

    @property
    def revisions(self) -> int:
        return sum(u.revisions for u in self.upserts.values())


@dataclass
class RunSummary:
    channels: list[ChannelSummary] = field(default_factory=list)
    api_calls: int = 0

    @property
    def any_failed(self) -> bool:
        return any(not c.ok for c in self.channels)


def _store_batch(
    store: SqliteStore, channel: ChannelConfig, batch: PullBatch
) -> ChannelSummary:
    cid = batch.channel_id
    pulled_at = iso_now_pt()
    store.begin()
    try:
        if batch.channel_title or batch.uploads_playlist_id:
            store.upsert(
                "channels",
                [
                    {
                        "channel_id": cid,
                        "title": batch.channel_title,
                        "uploads_playlist_id": batch.uploads_playlist_id,
                    }
                ],
                pulled_at=pulled_at,
            )
        if batch.videos:
            store.upsert("videos", batch.videos, pulled_at=pulled_at)

        upserts: dict[str, UpsertResult] = {}
        for table, rows in batch.tables.items():
            upserts[table] = store.upsert(
                table, rows, track_revisions=channel.track_revisions, pulled_at=pulled_at
            )

        _, data_through = store.channel_date_coverage(cid)
        store.set_channel_freshness(cid, pulled_at, data_through)
        store.commit()
    except Exception:
        store.rollback()
        raise
    return ChannelSummary(
        name=channel.name,
        channel_id=cid,
        ok=True,
        upserts=upserts,
        degraded=batch.degraded,
        data_through=data_through,
    )


def pull_channel(
    store: SqliteStore,
    source: Source,
    channel: ChannelConfig,
    start: date,
    end: date,
    *,
    heartbeat: Heartbeat | None = None,
) -> ChannelSummary:
    batch = source.fetch_reports(
        channel, start, end, include_revenue=channel.include_revenue, heartbeat=heartbeat
    )
    return _store_batch(store, channel, batch)


def run_pull(
    store: SqliteStore,
    source: Source,
    channels: list[ChannelConfig],
    start: date,
    end: date,
    *,
    heartbeat: Heartbeat | None = None,
    logger: logging.Logger | None = None,
) -> RunSummary:
    log = logger or logging.getLogger("ytmetrics")
    summary = RunSummary()
    for channel in channels:
        if heartbeat:
            heartbeat.update(f"channel={channel.name} pulling {start}..{end}")
        log.info("pull start: channel=%s window=%s..%s", channel.name, start, end)
        try:
            cs = pull_channel(store, source, channel, start, end, heartbeat=heartbeat)
            log.info(
                "pull done: channel=%s rows=%s revisions=%d degraded=%s",
                channel.name,
                {t: (u.inserted + u.updated) for t, u in cs.upserts.items()},
                cs.revisions,
                cs.degraded,
            )
        except Exception as exc:  # isolation: log and continue with other channels
            log.exception("pull FAILED: channel=%s: %s", channel.name, exc)
            cs = ChannelSummary(name=channel.name, channel_id=channel.channel_id, ok=False,
                                error=str(exc))
        summary.channels.append(cs)

    counter = getattr(source, "counter", None)
    summary.api_calls = getattr(counter, "count", 0)
    return summary
