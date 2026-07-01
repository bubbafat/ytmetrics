"""Source interface and the normalized batch a source returns for one channel/window."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..config import ChannelConfig
from ..status import Heartbeat


class ApiBudgetExceeded(RuntimeError):
    """Raised when a run exceeds max_api_calls_per_run (runaway guard)."""


class CallCounter:
    """Shared across a whole run (all channels). Aborts when the cap is hit."""

    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self.count = 0

    def tick(self, label: str = "") -> None:
        self.count += 1
        if self.max_calls and self.count > self.max_calls:
            raise ApiBudgetExceeded(
                f"exceeded max_api_calls_per_run={self.max_calls} (at {label or 'api call'})"
            )


@dataclass
class PullBatch:
    """Normalized rows for one channel + window, ready for the store."""

    channel_id: str  # resolved UC… id (config "mine" is resolved by the source)
    channel_title: str | None = None
    uploads_playlist_id: str | None = None
    subscriber_count: int | None = None  # current total from Data API channels.list (W7)
    channel_handle: str | None = None  # @handle (Data API snippet.customUrl)
    videos: list[dict[str, Any]] = field(default_factory=list)
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)  # reports skipped/degraded


class Source(ABC):
    """A source produces normalized rows; only LiveSource touches the network."""

    @abstractmethod
    def fetch_reports(
        self,
        channel: ChannelConfig,
        start: date,
        end: date,
        *,
        include_revenue: bool,
        heartbeat: Heartbeat | None = None,
    ) -> PullBatch:
        """Return the normalized batch for ``channel`` over the inclusive [start, end]."""

    @abstractmethod
    def fetch_insights(
        self,
        channel: ChannelConfig,
        start: date,
        end: date,
        *,
        include_demographics: bool = True,
        heartbeat: Heartbeat | None = None,
    ) -> PullBatch:
        """Return the windowed-insight batch (W1–W4, W6) for ``channel`` over [start, end]."""
