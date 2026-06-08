"""Date/time helpers, pinned to Pacific Time.

YouTube Analytics defines a "day" in America/Los_Angeles. All date math in ytmetrics goes
through here so the trailing window and the ``today - 2`` default never drift by a day
because of the host's local timezone or UTC.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

# YouTube finalizes recent days over ~2-3 days; default the end of a pull this far back.
DEFAULT_LAG_DAYS = 2

ISO_DATE = "%Y-%m-%d"


def now_pt() -> datetime:
    """Current time in Pacific."""
    return datetime.now(PACIFIC)


def today_pt() -> date:
    """Today's date in Pacific."""
    return now_pt().date()


def default_end_date() -> date:
    """Default end of a pull window: today (PT) minus the data-lag."""
    return today_pt() - timedelta(days=DEFAULT_LAG_DAYS)


def parse_date(s: str) -> date:
    """Parse a YYYY-MM-DD string into a date."""
    return datetime.strptime(s, ISO_DATE).date()


def fmt_date(d: date) -> str:
    """Format a date as YYYY-MM-DD."""
    return d.strftime(ISO_DATE)


def iso_now_pt() -> str:
    """ISO-8601 timestamp in Pacific, for last_updated / pulled_at columns."""
    return now_pt().isoformat(timespec="seconds")


def trailing_window(days: int, end: date | None = None) -> tuple[date, date]:
    """Return (start, end) for a trailing window of ``days`` ending at ``end``.

    ``end`` defaults to today - DEFAULT_LAG_DAYS. The window is inclusive and spans
    ``days`` calendar days, so days=7 ending 2026-06-06 starts 2026-05-31.
    """
    if end is None:
        end = default_end_date()
    start = end - timedelta(days=days - 1)
    return start, end


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split an inclusive [start, end] range into per-calendar-month [s, e] chunks.

    Used to keep the per-video-daily backfill under per-response row limits.
    """
    if start > end:
        return []
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        if cur.month == 12:
            month_end = date(cur.year, 12, 31)
        else:
            month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
        chunk_end = min(month_end, end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks
