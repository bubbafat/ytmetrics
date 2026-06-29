"""Daily digest — a calm, mobile-first PLAIN-TEXT email the owner reads on his phone so he
can stop compulsively opening YouTube Studio. The verdict goes in the subject line so he can
triage from the lock screen.

Pure + dependency-light: matplotlib optional — used only for the inline trend chart;
degrades to a text sparkline. Everything comes from the local db. `compute(db)`
returns a dict; `render_text(digest)` turns it into (subject, body).

Framing baked in: report the *freshest* day available (max(date) in `channel_daily`),
Studio-style — but YouTube revises roughly the last 2-3 days, so any such day is marked
"(est.)" and a RECENT DAYS table shows which days are estimates vs settled. Default to calm:
most days read "✅ normal day — nothing needs you," and we only escalate when an anomaly
clears a fixed threshold.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .timeutil import today_pt

# Anomaly thresholds (locked) ------------------------------------------------------
VIEWS_THRESHOLD = 0.20   # |latest/avg7 - 1| >= this -> flag views
REV_THRESHOLD = 0.20     # same, on revenue's own 7-day avg
TRAFFIC_MULTIPLE = 2.0   # a source's latest >= this × its 7-day avg -> a shift
TRAFFIC_MIN_VIEWS = 25   # …and at least this many views, so noise doesn't fire
REVISION_DAYS = 2        # days within this many of "today" are still YouTube estimates
SPARK = "▁▂▃▄▅▆▇█"       # 8-step block ramp


# --- small pure helpers ------------------------------------------------------------
def _pct(now: float, base: float | None) -> float | None:
    """Percent change of ``now`` vs ``base`` (e.g. 0.40 for a 40% rise). None if no base."""
    if not base:
        return None
    return (now - base) / abs(base)


def _is_anomaly(now: float, avg: float | None, threshold: float) -> bool:
    """True when ``now`` deviates from the (stabler) 7-day ``avg`` by >= ``threshold``."""
    p = _pct(now, avg)
    return p is not None and abs(p) >= threshold


def _direction(p: float | None) -> str:
    """'up' / 'down' / 'flat' for a fractional change."""
    if p is None or abs(p) < 1e-9:
        return "flat"
    return "up" if p > 0 else "down"


def sparkline(values: list[float]) -> str:
    """Map a series onto the 8-char block ramp (min→▁, max→█). Flat series → all mid."""
    nums = [float(v or 0) for v in values]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    if hi - lo < 1e-9:
        return SPARK[0] * len(nums)
    span = hi - lo
    return "".join(SPARK[min(len(SPARK) - 1, int((v - lo) / span * (len(SPARK) - 1) + 0.5))]
                   for v in nums)


def _fmt_money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _fmt_pct(p: float | None) -> str:
    if p is None:
        return "n/a"
    arrow = "▲" if p >= 0 else "▼"
    return f"{arrow}{abs(p) * 100:.0f}%"


# --- query helpers -----------------------------------------------------------------
def _val(c, sql, p=(), default=None):
    r = c.execute(sql, p).fetchone()
    return r[0] if r and r[0] is not None else default


def _day_stats(c, day: str) -> dict:
    """Channel totals for a single day (summed across content types)."""
    r = c.execute(
        "SELECT coalesce(sum(views),0) v, coalesce(sum(estimated_minutes_watched),0) m, "
        "coalesce(sum(subscribers_gained),0) sg, coalesce(sum(subscribers_lost),0) sl "
        "FROM channel_daily WHERE date=?",
        (day,),
    ).fetchone()
    rev = _val(c, "SELECT sum(estimated_revenue) FROM channel_revenue_daily WHERE date=?", (day,))
    return {"views": r[0], "mins": r[1], "net": r[2] - r[3], "rev": rev}


def _series(c, days: list[str]) -> tuple[list[int], list[float]]:
    """Per-day (views, revenue) for the given ordered dates; 0 where missing."""
    views, rev = [], []
    for d in days:
        s = _day_stats(c, d)
        views.append(s["views"])
        rev.append(s["rev"] or 0.0)
    return views, rev


# --- the computation ---------------------------------------------------------------
def compute(db_path: str | Path, *, today=None) -> dict:
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        latest = _val(c, "SELECT max(date) FROM channel_daily")
        if not latest:
            raise RuntimeError("no channel_daily data in db — run `ytmetrics pull` first")
        latest_d = datetime.strptime(latest, "%Y-%m-%d").date()
        today_d = today or today_pt()
        prev = (latest_d - timedelta(days=1)).isoformat()

        def _est(day_iso: str) -> bool:
            return (today_d - datetime.strptime(day_iso, "%Y-%m-%d").date()).days <= REVISION_DAYS

        # 7-day baseline = the 7 days ending at prev (latest-7 .. latest-1), averaged.
        base_days = [(latest_d - timedelta(days=n)).isoformat() for n in range(7, 0, -1)]
        # Only count days that actually have data (handles <8 days gracefully).
        have = {r[0] for r in c.execute(
            "SELECT DISTINCT date FROM channel_daily WHERE date IN (%s)"
            % ",".join("?" * len(base_days)), base_days)}
        present = [d for d in base_days if d in have]

        cur = _day_stats(c, latest)
        prv = _day_stats(c, prev)

        base_views = [_day_stats(c, d)["views"] for d in present]
        base_rev = [r for r in (_day_stats(c, d)["rev"] for d in present) if r is not None]
        base_net = [_day_stats(c, d)["net"] for d in present]
        avg7_views = sum(base_views) / len(base_views) if base_views else None
        avg7_rev = sum(base_rev) / len(base_rev) if base_rev else None
        avg7_net = sum(base_net) / len(base_net) if base_net else 0.0

        # --- views: two deltas, flag on the stabler 7-day baseline -----------------
        views_dd = _pct(cur["views"], prv["views"])
        views_v7 = _pct(cur["views"], avg7_views)
        views_anom = _is_anomaly(cur["views"], avg7_views, VIEWS_THRESHOLD)

        # --- revenue (skip entirely if pre-monetization / no revenue this day) -----
        rev_dd = _pct(cur["rev"], prv["rev"]) if cur["rev"] is not None else None
        rev_v7 = _pct(cur["rev"], avg7_rev) if cur["rev"] is not None else None
        rev_anom = (cur["rev"] is not None
                    and _is_anomaly(cur["rev"], avg7_rev, REV_THRESHOLD))

        # --- subscribers -----------------------------------------------------------
        net = cur["net"]
        sub_spike_floor = max(5, 2 * avg7_net)
        sub_loss = net < 0
        sub_spike = net >= sub_spike_floor and net > 0
        sub_flag = sub_loss or sub_spike

        # --- traffic-source shift --------------------------------------------------
        traffic = _traffic_shift(c, latest, present)

        # --- what moved it: top 3 videos by views on the latest day ----------------
        top_videos = _top_videos(c, latest, cur["views"])

        # --- latest video ----------------------------------------------------------
        latest_video = _latest_video(c, latest, prev)

        # --- 7-day sparklines ending at latest -------------------------------------
        spark_days = [(latest_d - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
        sv, sr = _series(c, spark_days)

        # --- recent days (Studio-like), with estimate markers ----------------------
        recent_dates = [r[0] for r in c.execute(
            "SELECT date FROM channel_daily WHERE date<=? GROUP BY date "
            "ORDER BY date DESC LIMIT 5", (latest,))][::-1]
        recent_days = [{"date": dd, "views": _day_stats(c, dd)["views"],
                        "rev": _day_stats(c, dd)["rev"], "estimated": _est(dd)}
                       for dd in recent_dates]

        breakout = bool(latest_video and latest_video.get("breakout"))
        status = "alert" if any(
            [views_anom, rev_anom, sub_flag, bool(traffic), breakout]
        ) else "normal"

        digest = {
            "latest_date": latest,
            "latest_estimated": _est(latest),
            "recent_days": recent_days,
            "prev_date": prev,
            "baseline_days": len(present),
            "views": cur["views"],
            "watch_hours": cur["mins"] / 60.0,
            "net_subs": net,
            "revenue": cur["rev"],
            "views_dd": views_dd,
            "views_v7": views_v7,
            "views_anomaly": views_anom,
            "avg7_views": avg7_views,
            "rev_dd": rev_dd,
            "rev_v7": rev_v7,
            "rev_anomaly": rev_anom,
            "avg7_rev": avg7_rev,
            "sub_loss": sub_loss,
            "sub_spike": sub_spike,
            "avg7_net": avg7_net,
            "traffic": traffic,
            "top_videos": top_videos,
            "latest_video": latest_video,
            "spark_views": sparkline(sv),
            "spark_views_vals": sv,
            "spark_rev": sparkline(sr),
            "spark_rev_vals": sr,
            "status": status,
        }
        digest["headline"] = _headline(digest)
        return digest
    finally:
        c.close()


def _traffic_shift(c, latest: str, present: list[str]) -> dict | None:
    """Biggest riser whose latest-day views >= 2× its 7-day avg AND latest >= 25 views."""
    if not present:
        return None
    rows = c.execute(
        "SELECT traffic_source_type t, coalesce(sum(views),0) v "
        "FROM channel_traffic_sources_daily WHERE date=? GROUP BY t", (latest,)).fetchall()
    avgs = {r["t"]: r["v"] / len(present) for r in c.execute(
        "SELECT traffic_source_type t, coalesce(sum(views),0) v "
        "FROM channel_traffic_sources_daily WHERE date IN (%s) GROUP BY t"
        % ",".join("?" * len(present)), present)}
    best = None
    for r in rows:
        latest_v, avg = r["v"], avgs.get(r["t"], 0.0)
        if latest_v < TRAFFIC_MIN_VIEWS or avg <= 0:
            continue
        mult = latest_v / avg
        if mult >= TRAFFIC_MULTIPLE and (best is None or mult > best["multiple"]):
            best = {"source": r["t"], "latest": latest_v, "avg7": avg, "multiple": mult}
    return best


def _top_videos(c, latest: str, day_views: int) -> list[dict]:
    rows = c.execute(
        "SELECT v.title t, coalesce(sum(vd.views),0) v FROM video_daily vd "
        "JOIN videos v USING(video_id) WHERE vd.date=? "
        "GROUP BY vd.video_id ORDER BY v DESC LIMIT 3", (latest,)).fetchall()
    out = []
    for r in rows:
        share = (r["v"] / day_views) if day_views else 0.0
        out.append({"title": r["t"], "views": r["v"], "share": share})
    return out


def _latest_video(c, latest: str, prev: str) -> dict | None:
    v = c.execute(
        "SELECT video_id, title, published_at FROM videos "
        "WHERE content_type='VIDEO_ON_DEMAND' ORDER BY published_at DESC LIMIT 1").fetchone()
    if not v:
        return None
    vid = v["video_id"]
    day_views = _val(c, "SELECT views FROM video_daily WHERE video_id=? AND date=?",
                     (vid, latest), default=0)
    prev_views = _val(c, "SELECT views FROM video_daily WHERE video_id=? AND date=?",
                      (vid, prev), default=0)
    total = _val(c, "SELECT coalesce(sum(views),0) FROM video_daily WHERE video_id=?",
                 (vid,), default=0)
    avp = _val(c, "SELECT avg(average_view_percentage) FROM video_daily WHERE video_id=?",
               (vid,), default=0.0)
    pub = v["published_at"][:10]
    pub_d = datetime.strptime(pub, "%Y-%m-%d").date()
    latest_d = datetime.strptime(latest, "%Y-%m-%d").date()
    days_since = (latest_d - pub_d).days
    if day_views > prev_views:
        trend = "up"
    elif day_views < prev_views:
        trend = "fading"
    else:
        trend = "flat"
    # A breakout: a meaningfully young video whose latest day clearly outpaces the prior day.
    breakout = days_since <= 14 and day_views >= max(25, 1.5 * prev_views) and prev_views > 0
    return {
        "title": v["title"], "day_views": day_views, "prev_views": prev_views,
        "total": total, "avp": avp or 0.0, "days_since": days_since,
        "trend": trend, "breakout": breakout,
    }


def _headline(d: dict) -> str:
    """The single biggest signal, as a short phrase for the subject line / verdict."""
    if d["views_anomaly"]:
        dir_ = _direction(d["views_v7"])
        word = "surged" if dir_ == "up" else "dropped"
        return f"Views {word} {_fmt_pct(d['views_v7'])} vs 7-day norm"
    if d["rev_anomaly"]:
        dir_ = _direction(d["rev_v7"])
        word = "up" if dir_ == "up" else "down"
        return f"Revenue {word} {_fmt_pct(d['rev_v7'])} vs 7-day norm"
    if d["sub_loss"]:
        return f"Net subscriber loss ({d['net_subs']:+d})"
    if d["sub_spike"]:
        return f"Subscriber spike (+{d['net_subs']})"
    if d["latest_video"] and d["latest_video"].get("breakout"):
        return f"Latest video breaking out (+{d['latest_video']['day_views']} views)"
    if d["traffic"]:
        t = d["traffic"]
        return f"{_src_label(t['source'])} up {t['multiple']:.1f}× vs norm"
    return "normal day"


# --- rendering ---------------------------------------------------------------------
def _src_label(src: str) -> str:
    pretty = {
        "YT_SEARCH": "Search", "RELATED_VIDEO": "Suggested", "PLAYLIST": "Playlists",
        "SUBSCRIBER": "Browse/Home", "NO_LINK_OTHER": "Direct", "EXT_URL": "External",
        "ADVERTISING": "Ads", "YT_CHANNEL": "Channel page", "SHORTS": "Shorts feed",
        "NOTIFICATION": "Notifications", "END_SCREEN": "End screens",
    }
    return pretty.get(src, src.replace("_", " ").title())


def _fmt_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").date().strftime("%b %d")


def _trend_dates(digest: dict) -> list[str]:
    """The 7 short dates ('Jun 22'…) matching ``spark_views_vals`` / ``spark_rev_vals``,
    ending at ``latest_date``. Mirrors how ``compute`` builds the 7-day series."""
    latest_d = datetime.strptime(digest["latest_date"], "%Y-%m-%d").date()
    iso = [(latest_d - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
    return [_fmt_date(d) for d in iso]


def _trend_lines(d: dict) -> list[str]:
    """The body lines of the TREND (7d) section (sparklines + ranges). Isolated so the
    HTML renderer can swap them for an inline chart image."""
    lines: list[str] = []
    sv = d["spark_views_vals"]
    lines.append(f"  Views   {d['spark_views']}  ({min(sv):,}–{max(sv):,})")
    if d["revenue"] is not None and any(d["spark_rev_vals"]):
        sr = d["spark_rev_vals"]
        lines.append(f"  Revenue {d['spark_rev']}  (${min(sr):,.2f}–${max(sr):,.2f})")
    return lines


def chart_png(digest: dict) -> bytes | None:
    """Render the 7-day trend (views=coral line, revenue=sky line on a 2nd axis) as a
    compact, mobile-friendly PNG on a navy background. Returns PNG bytes, or ``None`` if
    matplotlib isn't installed (daily must work without the ``briefing`` extra)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    from io import BytesIO

    from .briefing import CORAL, CREAM, MUTED, NAVY, SKY

    dates = _trend_dates(digest)
    views = [float(v or 0) for v in digest["spark_views_vals"]]
    rev = [float(v or 0) for v in digest["spark_rev_vals"]]

    fig, ax = plt.subplots(figsize=(6.4, 2.2), dpi=150)
    fig.patch.set_facecolor(NAVY)
    ax.set_facecolor(NAVY)

    ax.plot(range(len(views)), views, color=CORAL, marker="o", markersize=4, linewidth=2)
    ax.tick_params(axis="x", colors=CREAM, labelsize=8)
    ax.tick_params(axis="y", colors=CORAL, labelsize=8)
    ax.set_xticks(range(len(dates)))
    ax.set_xticklabels(dates)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)

    ax2 = ax.twinx()
    ax2.set_facecolor("none")
    ax2.plot(range(len(rev)), rev, color=SKY, marker="o", markersize=4, linewidth=2)
    ax2.tick_params(axis="y", colors=SKY, labelsize=8)
    for side in ("top", "left", "bottom"):
        ax2.spines[side].set_visible(False)
    ax2.spines["right"].set_color(MUTED)

    ax.set_title("7-day trend — views (coral) · revenue (sky)",
                 color=CREAM, fontsize=9, pad=6)

    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=NAVY, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# Marker token identifying the TREND block among the ordered section blocks, so the HTML
# renderer can locate it and substitute the inline chart image.
_TREND_HEADING = "TREND (7d)"


def _body_blocks(digest: dict) -> list[list[str]]:
    """The plain-text body as an ordered list of section blocks (each a list of lines).
    ``render_text`` joins them; ``render_html`` reuses them, swapping the trend block."""
    d = digest
    alert = d["status"] == "alert"
    blocks: list[list[str]] = []

    # --- verdict + freshness ---------------------------------------------------
    head: list[str] = []
    head.append("⚠️ " + d["headline"] if alert else "✅ Normal day — nothing needs you.")
    freshness = ("estimated — YouTube revises the last ~2 days"
                 if d["latest_estimated"] else "finalized")
    head.append(f"(as of {_fmt_date(d['latest_date'])} · {freshness})")
    blocks.append(head)

    # --- LATEST DAY ------------------------------------------------------------
    ld = ["LATEST DAY" + (" (est.)" if d["latest_estimated"] else "")]
    ld.append(f"  Views   {d['views']:,}  (d/d {_fmt_pct(d['views_dd'])}, "
              f"7d {_fmt_pct(d['views_v7'])})")
    ld.append(f"  Watch   {d['watch_hours']:,.1f} hrs")
    ld.append(f"  Subs    {d['net_subs']:+d} net")
    if d["revenue"] is None:
        ld.append("  Revenue —  (pre-monetization)")
    else:
        ld.append(f"  Revenue {_fmt_money(d['revenue'])}  (d/d {_fmt_pct(d['rev_dd'])}, "
                  f"7d {_fmt_pct(d['rev_v7'])})")
    blocks.append(ld)

    # --- RECENT DAYS (Studio-like) ---------------------------------------------
    rdb = ["RECENT DAYS"]
    for rd in reversed(d["recent_days"]):          # newest first
        rev = _fmt_money(rd["rev"]) if rd["rev"] is not None else "—"
        mark = "  est" if rd["estimated"] else ""
        rdb.append(f"  {_fmt_date(rd['date'])}  {rd['views']:>5,}  {rev:>8}{mark}")
    blocks.append(rdb)

    # --- WHAT MOVED IT ---------------------------------------------------------
    wm = ["WHAT MOVED IT"]
    if d["top_videos"]:
        for v in d["top_videos"]:
            title = v["title"] if len(v["title"]) <= 44 else v["title"][:43] + "…"
            wm.append(f"  • {title} — {v['views']:,} ({v['share'] * 100:.0f}%)")
    else:
        wm.append("  No per-video views recorded.")
    blocks.append(wm)

    # --- LATEST VIDEO ----------------------------------------------------------
    lvb = ["LATEST VIDEO"]
    lv = d["latest_video"]
    if lv:
        title = lv["title"] if len(lv["title"]) <= 44 else lv["title"][:43] + "…"
        trend = {"up": "accelerating", "fading": "fading", "flat": "flat"}[lv["trend"]]
        lvb.append(f"  {title}")
        lvb.append(f"  {lv['day_views']:,} views that day · {lv['total']:,} total · "
                   f"{lv['avp']:.0f}% avg viewed · day {lv['days_since']} · {trend}")
    else:
        lvb.append("  No videos found.")
    blocks.append(lvb)

    # --- SIGNALS ---------------------------------------------------------------
    sb = ["SIGNALS"]
    signals = []
    if d["sub_loss"]:
        signals.append(f"📉 Net subscriber loss ({d['net_subs']:+d}).")
    elif d["sub_spike"]:
        signals.append(f"📈 Subscriber spike (+{d['net_subs']}, "
                       f"vs ~{d['avg7_net']:.0f}/day norm).")
    if d["traffic"]:
        t = d["traffic"]
        signals.append(f"📈 {_src_label(t['source'])} up {t['multiple']:.1f}× vs norm "
                       f"({t['latest']} views) — possible algorithm pickup.")
    if lv and lv.get("breakout"):
        signals.append(f"📈 Latest video accelerating (+{lv['day_views']} views, day "
                       f"{lv['days_since']}).")
    if signals:
        for s in signals:
            sb.append(f"  {s}")
    else:
        sb.append("  Nothing unusual.")
    blocks.append(sb)

    # --- TREND (7d) ------------------------------------------------------------
    blocks.append([_TREND_HEADING] + _trend_lines(d))

    # --- footer ----------------------------------------------------------------
    blocks.append(
        ["— ytmetrics daily digest. Recent days are YouTube estimates and will revise."]
    )
    return blocks


def _subject(d: dict) -> str:
    """The verdict, readable from the lock screen."""
    day = _fmt_date(d["latest_date"])
    if d["status"] == "alert":
        return f"Empty Besters ⚠️ {day}: {d['headline']}"
    return f"Empty Besters ✅ {day}: {d['views']:,} views, {_fmt_money(d['revenue'])}"


def render_text(digest: dict) -> tuple[str, str]:
    blocks = _body_blocks(digest)
    body = "\n\n".join("\n".join(block) for block in blocks)
    return _subject(digest), body


def render_html(digest: dict, *, img_cid: str | None) -> str:
    """The same plain-text body, wrapped in a dark-themed ``<pre>``. For the TREND (7d)
    section only: if ``img_cid`` is given, swap the sparkline lines for an inline
    ``<img src="cid:...">``; otherwise keep the text sparklines."""
    from html import escape

    parts: list[str] = []
    for block in _body_blocks(digest):
        if img_cid and block and block[0] == _TREND_HEADING:
            heading = escape(block[0])
            img = (f'<img src="cid:{escape(img_cid)}" alt="7-day trend chart" '
                   'style="max-width:100%;height:auto;margin-top:6px;display:block;'
                   'border-radius:6px;">')
            parts.append(heading + "\n" + img)
        else:
            parts.append(escape("\n".join(block)))
    inner = "\n\n".join(parts)

    return (
        '<pre style="background:#14304A;color:#F4EFE3;'
        "font-family:'SF Mono',Menlo,Consolas,'Liberation Mono',monospace;"
        'font-size:13px;line-height:1.5;padding:16px;border-radius:8px;'
        'white-space:pre-wrap;word-break:break-word;margin:0;">'
        + inner
        + "</pre>"
    )
