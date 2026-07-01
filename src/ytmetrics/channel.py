"""Channel identity — name, @handle, and public URL — read from the stored ``channels`` dim.

Shared by the daily digest (footer link + subject) and the weekly briefing (PDF cover +
email) so none of them hardcode a channel name. Degrades gracefully: prefers the @handle
(Data API ``customUrl``) for the link, falls back to the title + ``/channel/<id>`` URL until
a pull captures the handle.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def identity(conn_or_path: sqlite3.Connection | str | Path) -> dict | None:
    """Return ``{"name", "display", "url"}`` for the primary channel, or None if there's no
    channel row yet. ``name`` is the human title (subject lines); ``display`` is the link
    text (the @handle when known); ``url`` is the channel's public URL."""
    if isinstance(conn_or_path, (str, Path)):
        conn = sqlite3.connect(str(conn_or_path))
        own = True
    else:
        conn = conn_or_path
        own = False
    try:
        row = conn.execute(
            "SELECT channel_id, title, handle FROM channels "
            "ORDER BY (subscriber_count IS NULL), subscriber_count DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        if own:
            conn.close()
    if not row:
        return None
    cid, title, handle = row[0], row[1], row[2]   # positional: don't assume a row_factory
    if handle:
        display = handle if handle.startswith("@") else f"@{handle}"
        url = f"https://www.youtube.com/{display}"
    else:
        display = title or cid
        url = f"https://www.youtube.com/channel/{cid}"
    return {"name": title or display, "display": display, "url": url}
