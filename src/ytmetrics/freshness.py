"""Staleness check + 'big red banner' builders for the EMAILS (daily digest + the weekly
briefing's email body). If the pull has stopped — e.g. the OAuth token died — the latest
data falls behind and these make that impossible to miss.

The threshold is the existing ``freshness_warn_days`` config (default 3); data is "stale"
when the newest ``channel_daily`` date is more than that many days behind today.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

from .timeutil import today_pt


def days_behind(db_path: str | Path, *, today: date | None = None) -> tuple[str | None, int | None]:
    """Return (latest_channel_daily_date, days behind today). (None, None) if no data."""
    c = sqlite3.connect(str(db_path))
    try:
        row = c.execute("SELECT max(date) FROM channel_daily").fetchone()
    except sqlite3.Error:
        return None, None
    finally:
        c.close()
    latest = row[0] if row and row[0] else None
    if not latest:
        return None, None
    t = today or today_pt()
    return latest, (t - datetime.strptime(latest, "%Y-%m-%d").date()).days


def is_stale(db_path: str | Path, warn_days: int, *, today: date | None = None
             ) -> tuple[bool, str | None, int | None]:
    """(stale?, latest_date, days_behind). Stale when data is > ``warn_days`` behind."""
    latest, n = days_behind(db_path, today=today)
    return (n is not None and n > warn_days), latest, n


def stale_text_banner(latest: str | None, n: int | None) -> str:
    bar = "█" * 56
    return (
        f"{bar}\n"
        f"  🔴 STALE DATA — latest is {latest} ({n} days behind).\n"
        f"  The pull may have stopped — check the OAuth token / pull job.\n"
        f"{bar}"
    )


def stale_html_banner(latest: str | None, n: int | None) -> str:
    return (
        '<div style="background:#C5221F;color:#ffffff;font-weight:700;'
        "padding:14px 16px;border-radius:8px;margin:0 0 14px;"
        "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
        'font-size:15px;line-height:1.45;">'
        f"🔴 STALE DATA — latest is {latest} ({n} days behind). "
        "The pull may have stopped — check the OAuth token / pull job.</div>"
    )
