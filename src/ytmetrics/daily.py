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
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

from .channel import identity as channel_identity
from .freshness import stale_html_banner, stale_text_banner
from .timeutil import today_pt

# Anomaly thresholds (locked) ------------------------------------------------------
VIEWS_THRESHOLD = 0.20   # |latest/avg7 - 1| >= this -> flag views
REV_THRESHOLD = 0.20     # same, on revenue's own 7-day avg
TRAFFIC_MULTIPLE = 2.0   # a source's latest >= this × its 7-day avg -> a shift
TRAFFIC_MIN_VIEWS = 25   # …and at least this many views, so noise doesn't fire
REVISION_DAYS = 2        # the freshest day + this many behind it are still YouTube estimates
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


def sparkline(values: Sequence[float]) -> str:
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


def _channel_link(c) -> dict | None:
    """The channel's name / @handle / URL for the subject + footer (see channel.identity)."""
    return channel_identity(c)


def _channel_name(digest: dict) -> str:
    ch = digest.get("channel")
    return ch["name"] if ch and ch.get("name") else "Your channel"


def _series(c, days: list[str]) -> tuple[list[int], list[float]]:
    """Per-day (views, revenue) for the given ordered dates; 0 where missing."""
    views, rev = [], []
    for d in days:
        s = _day_stats(c, d)
        views.append(s["views"])
        rev.append(s["rev"] or 0.0)
    return views, rev


# --- the computation ---------------------------------------------------------------
def compute(db_path: str | Path, *, today=None, warn_days: int = 3) -> dict:
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        latest = _val(c, "SELECT max(date) FROM channel_daily")
        if not latest:
            # Day-1 reality: a brand-new channel (or a fresh install) has no analytics yet —
            # YouTube's Analytics API lags ~2-3 days. Degrade to a calm "warming up" digest
            # instead of crashing the scheduled email job.
            return {
                "no_data": True,
                "channel": _channel_link(c),
                "last_pull": _val(c, "SELECT max(last_successful_pull) FROM channels"),
            }
        latest_d = datetime.strptime(latest, "%Y-%m-%d").date()
        today_d = today or today_pt()
        days_behind = (today_d - latest_d).days
        prev = (latest_d - timedelta(days=1)).isoformat()

        def _est(day_iso: str) -> bool:
            # YouTube keeps revising the most-recent few days it has *published*, and the
            # Analytics API itself runs ~3 days behind. So anchor the "still an estimate"
            # window to the freshest day we actually have (latest_d), NOT to today — a
            # today-anchored window never catches real data and would mark everything
            # "finalized" (the very days that this morning's pull revised).
            return (latest_d - datetime.strptime(day_iso, "%Y-%m-%d").date()).days <= REVISION_DAYS

        # 7-day baseline = the 7 days ending at prev (latest-7 .. latest-1), averaged.
        base_days = [(latest_d - timedelta(days=n)).isoformat() for n in range(7, 0, -1)]
        # Only count days that actually have data (handles <8 days gracefully).
        ph = ",".join("?" * len(base_days))
        have = {r[0] for r in c.execute(
            f"SELECT DISTINCT date FROM channel_daily WHERE date IN ({ph})", base_days)}
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

        # --- this week / this month, vs the prior period ---------------------------
        week = _week(c, latest_d)
        month = _month(c, latest_d)

        # --- when the data was last fetched ----------------------------------------
        last_pull = _val(c, "SELECT max(last_successful_pull) FROM channels")

        # --- channel identity (for the footer link) --------------------------------
        channel = _channel_link(c)

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
            "days_behind": days_behind,
            "stale": days_behind > warn_days,
            "last_pull": last_pull,
            "channel": channel,
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
            "week": week,
            "month": month,
            "spark_views": sparkline(sv),
            "spark_views_vals": sv,
            "spark_rev": sparkline(sr),
            "spark_rev_vals": sr,
            "status": status,
        }
        digest["headline"] = _headline(digest)
        digest["alert_tone"] = _alert_tone(digest)
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
    ph = ",".join("?" * len(present))
    avgs = {r["t"]: r["v"] / len(present) for r in c.execute(
        "SELECT traffic_source_type t, coalesce(sum(views),0) v "
        f"FROM channel_traffic_sources_daily WHERE date IN ({ph}) GROUP BY t", present)}
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
    vs_typical = _vs_typical(c, vid, pub, days_since, "VIDEO_ON_DEMAND", total)
    return {
        "title": v["title"], "day_views": day_views, "prev_views": prev_views,
        "total": total, "avp": avp or 0.0, "days_since": days_since,
        "trend": trend, "breakout": breakout, "vs_typical": vs_typical,
    }


def _vs_typical(c, video_id: str, pub_date: str, days_since: int, content_type: str,
                target_total: int) -> dict | None:
    """Benchmark this video against the channel's *typical* video at the same age: the median
    cumulative views other same-type videos had by day ``days_since``. Only videos that
    actually reached that age count (so a 3-day-old video is compared to others' day-3 totals,
    not their lifetimes). Returns None if fewer than 3 comparable videos — too thin to trust."""
    if days_since < 1:
        return None
    rows = c.execute(
        "SELECT vd.video_id vid, "
        "CAST(julianday(vd.date) - julianday(substr(v.published_at,1,10)) AS INTEGER) age, "
        "coalesce(vd.views,0) views "
        "FROM video_daily vd JOIN videos v USING(video_id) "
        "WHERE v.content_type=? AND vd.video_id<>?", (content_type, video_id)).fetchall()
    cum: dict[str, int] = defaultdict(int)
    maxage: dict[str, int] = defaultdict(int)
    for r in rows:
        age = r["age"]
        if age is None:
            continue
        maxage[r["vid"]] = max(maxage[r["vid"]], age)
        if 0 <= age <= days_since:
            cum[r["vid"]] += r["views"]
    reached = sorted(cum[v] for v in cum if maxage[v] >= days_since)
    if len(reached) < 3:
        return None
    med = median(reached)
    return {"median": med, "ahead": target_total >= med, "cohort": len(reached)}


def _window_totals(c, days: list[str]) -> dict:
    """Sum the four scoreboard metrics over a list of ISO dates (revenue None if none seen)."""
    v = w = s = 0
    rev = 0.0
    has_rev = False
    for d in days:
        st = _day_stats(c, d)
        v += st["views"]
        w += st["mins"]
        s += st["net"]
        if st["rev"] is not None:
            rev += st["rev"]
            has_rev = True
    return {"views": v, "watch_hours": w / 60.0, "net_subs": s,
            "revenue": rev if has_rev else None}


def _compare(this_t: dict, last_t: dict) -> dict:
    """Per-metric {this, last, delta%} for two period totals (delta None if no base)."""
    out = {}
    for m in ("views", "watch_hours", "net_subs", "revenue"):
        a, b = this_t[m], last_t[m]
        out[m] = {"this": a, "last": b, "wow": _pct(a, b) if (a is not None and b) else None}
    return out


def _week(c, latest_d) -> dict:
    """Trailing 7 days vs the prior 7 (this = latest-6..latest, last = latest-13..latest-7)."""
    this_days = [(latest_d - timedelta(days=n)).isoformat() for n in range(6, -1, -1)]
    last_days = [(latest_d - timedelta(days=n)).isoformat() for n in range(13, 6, -1)]
    return _compare(_window_totals(c, this_days), _window_totals(c, last_days))


def _month(c, latest_d) -> dict:
    """Month-to-date vs the *same point* last month (a fair pace comparison): the 1st through
    ``latest_d`` vs the 1st through the same day-count of the prior month (clamped if the prior
    month is shorter)."""
    first_this = latest_d.replace(day=1)
    n = (latest_d - first_this).days                       # days after the 1st
    this_days = [(first_this + timedelta(days=i)).isoformat() for i in range(n + 1)]
    prev_last = first_this - timedelta(days=1)              # last day of previous month
    first_last = prev_last.replace(day=1)
    last_end = min(first_last + timedelta(days=n), prev_last)
    cnt = (last_end - first_last).days + 1
    last_days = [(first_last + timedelta(days=i)).isoformat() for i in range(cnt)]
    return _compare(_window_totals(c, this_days), _window_totals(c, last_days))


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


def _alert_tone(d: dict) -> str:
    """'good' (a positive surge — show green) or 'bad' (concerning — show yellow) for the
    chosen headline. Mirrors ``_headline``'s priority so the icon matches the words."""
    if d["views_anomaly"]:
        return "good" if (d["views_v7"] or 0) > 0 else "bad"
    if d["rev_anomaly"]:
        return "good" if (d["rev_v7"] or 0) > 0 else "bad"
    if d["sub_loss"]:
        return "bad"
    if d["sub_spike"]:
        return "good"
    if d["latest_video"] and d["latest_video"].get("breakout"):
        return "good"
    if d["traffic"]:
        return "good"
    return "good"


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
    if digest.get("no_data") or not digest.get("spark_views_vals"):
        return None
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
    if alert:
        icon = "🟢" if d.get("alert_tone") == "good" else "⚠️"
        head.append(f"{icon} " + d["headline"])
    else:
        head.append("✅ Normal day — nothing needs you.")
    freshness = ("estimated — these newest days still revise as YouTube finalizes"
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

    # --- THIS WEEK / THIS MONTH (vs the prior period) --------------------------
    def _period_block(heading: str, p: dict, period: str) -> list[str]:
        def line(lbl: str, m: dict, val: str, last: str) -> str:
            return f"  {lbl:<9} {val}  ({_fmt_pct(m['wow'])} vs {last} {period})"
        out = [heading,
               line("Views", p["views"], f"{p['views']['this']:,}", f"{p['views']['last']:,}"),
               line("Watch", p["watch_hours"], f"{p['watch_hours']['this']:,.1f} hrs",
                    f"{p['watch_hours']['last']:,.1f}"),
               line("Subs", p["net_subs"], f"{p['net_subs']['this']:+d}",
                    f"{p['net_subs']['last']:+d}")]
        if p["revenue"]["this"] is not None:
            out.append(line("Revenue", p["revenue"], _fmt_money(p["revenue"]["this"]),
                            _fmt_money(p["revenue"]["last"])))
        return out
    blocks.append(_period_block("LAST 7 DAYS", d["week"], "prev 7d"))
    blocks.append(_period_block("MONTH TO DATE", d["month"], "last mo"))

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
        vt = lv.get("vs_typical")
        if vt:
            word = "ahead of" if vt["ahead"] else "behind"
            lvb.append(f"  {word} your typical video at day {lv['days_since']} "
                       f"(median {vt['median']:,.0f})")
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
        ["— ytmetrics daily digest. Recent days are YouTube estimates and will revise.",
         _footer_text_tail(d) + "."]
    )
    return blocks


def _subject(d: dict) -> str:
    """The verdict, readable from the lock screen. 🔴 stale · 🟢 good surge · ⚠️ concerning ·
    ✅ normal. The channel name is dynamic (from the stored channel record)."""
    name = _channel_name(d)
    if d.get("no_data"):
        return f"{name} ✅ warming up — no analytics yet"
    if d.get("stale"):
        return f"{name} 🔴 STALE ({d['days_behind']}d behind) — data not updating"
    day = _fmt_date(d["latest_date"])
    if d["status"] == "alert":
        icon = "🟢" if d.get("alert_tone") == "good" else "⚠️"
        return f"{name} {icon} {day}: {d['headline']}"
    return f"{name} ✅ {day}: {d['views']:,} views, {_fmt_money(d['revenue'])}"


def _no_data_body_lines() -> list[str]:
    return [
        "No analytics yet — the channel is still warming up.",
        "",
        "YouTube's Analytics API lags about 2–3 days, so the first numbers land here within",
        "a few days of activity. Nothing needed from you — this email will fill in on its own.",
    ]


def render_text(digest: dict) -> tuple[str, str]:
    if digest.get("no_data"):
        body = "\n".join(_no_data_body_lines())
        body += "\n\n— ytmetrics daily digest.\n" + _footer_text_tail(digest) + "."
        return _subject(digest), body
    blocks = _body_blocks(digest)
    body = "\n\n".join("\n".join(block) for block in blocks)
    if digest.get("stale"):
        body = (stale_text_banner(digest["latest_date"], digest["days_behind"])
                + "\n\n" + body)
    return _subject(digest), body


# --- HTML rendering (native, mobile-first, brand palette) --------------------------
# Palette duplicated from briefing.py on purpose: importing briefing would pull matplotlib
# at module load, and the daily digest must work without the `briefing` extra.
_NAVY = "#14304A"; _CREAM = "#F4EFE3"; _CORAL = "#F0824A"     # noqa: E702
_MUTED = "#77899A"; _GOOD = "#1E8E5A"; _BAD = "#C5221F"       # noqa: E702
_AMBER = "#9A6400"; _CARD = "#FFFFFF"; _LINE = "#E6DFCF"      # noqa: E702
_FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _html_delta(pct: float | None, *, prefix: str = "") -> str:
    """A small coloured ▲/▼ percentage span (green up, red down, muted flat/none)."""
    if pct is None:
        return f'<span style="color:{_MUTED};">{prefix}–</span>'
    color = _GOOD if pct > 0 else _BAD if pct < 0 else _MUTED
    arrow = "▲" if pct > 0 else "▼" if pct < 0 else "→"
    return (f'<span style="color:{color};font-weight:600;white-space:nowrap;">'
            f"{prefix}{arrow}{abs(pct) * 100:.0f}%</span>")


def _stat_cell(label: str, value: str, secondary: str) -> str:
    return (
        f'<td width="50%" style="padding:10px 12px;vertical-align:top;">'
        f'<div style="color:{_MUTED};font-size:11px;letter-spacing:.06em;'
        f'text-transform:uppercase;">{label}</div>'
        f'<div style="color:{_NAVY};font-size:26px;font-weight:700;line-height:1.1;'
        f'margin:2px 0;">{value}</div>'
        f'<div style="font-size:12px;color:{_MUTED};">{secondary}</div></td>'
    )


def _callout(digest: dict) -> str:
    """The verdict/action box: red when stale, green for a good surge, amber for a concern,
    a calm green line when nothing needs the owner."""
    if digest.get("stale"):
        return stale_html_banner(digest["latest_date"], digest["days_behind"])
    if digest["status"] == "alert":
        good = digest.get("alert_tone") == "good"
        bg, border, icon = ((f"{_GOOD}14", _GOOD, "🟢") if good
                            else (f"{_AMBER}14", _AMBER, "⚠️"))
        from html import escape
        return (
            f'<div style="background:{bg};border-left:4px solid {border};'
            f'border-radius:6px;padding:12px 14px;margin:0 0 14px;">'
            f'<span style="font-size:15px;font-weight:700;color:{_NAVY};">'
            f"{icon} {escape(digest['headline'])}</span></div>"
        )
    return (
        f'<div style="background:{_GOOD}12;border-left:4px solid {_GOOD};'
        f'border-radius:6px;padding:12px 14px;margin:0 0 14px;color:{_NAVY};'
        f'font-size:15px;font-weight:600;">✅ Nothing needs you today.</div>'
    )


def _section_label(text: str) -> str:
    return (f'<div style="color:{_CORAL};font-size:12px;font-weight:700;'
            f'letter-spacing:.06em;text-transform:uppercase;margin:0 0 8px;">{text}</div>')


def _period_lines(p: dict, period: str) -> str:
    """The per-metric 'this X ▲n% vs last Y' lines shared by the week and month sections."""
    def line(label: str, val: str, last: str, delta: str) -> str:
        return (f'<div style="margin:3px 0;color:{_NAVY};font-size:14px;">'
                f'<span style="display:inline-block;min-width:78px;color:{_MUTED};">'
                f'{label}</span><span style="font-weight:600;">{val}</span> {delta} '
                f'<span style="color:{_MUTED};font-size:13px;">vs {last} {period}</span></div>')
    out = [
        line("Views", f'{p["views"]["this"]:,}', f'{p["views"]["last"]:,}',
             _html_delta(p["views"]["wow"])),
        line("Watch hrs", f'{p["watch_hours"]["this"]:,.1f}', f'{p["watch_hours"]["last"]:,.1f}',
             _html_delta(p["watch_hours"]["wow"])),
        line("Net subs", f'{p["net_subs"]["this"]:+d}', f'{p["net_subs"]["last"]:+d}',
             _html_delta(p["net_subs"]["wow"])),
    ]
    if p["revenue"]["this"] is not None:
        out.append(line("Revenue", _fmt_money(p["revenue"]["this"]),
                        _fmt_money(p["revenue"]["last"]), _html_delta(p["revenue"]["wow"])))
    return "".join(out)


_ET = "America/New_York"


def _rendered_stamp() -> str:
    """When this email was generated, in Eastern time (falls back to naive local)."""
    try:
        from zoneinfo import ZoneInfo
        now, tz = datetime.now(ZoneInfo(_ET)), " ET"
    except Exception:
        now, tz = datetime.now(), ""
    return now.strftime("%b %d, %Y %I:%M %p") + tz


def _et_fmt(iso: str | None) -> str | None:
    """Format a stored ISO pull timestamp (which carries a UTC offset) in Eastern time."""
    if not iso:
        return None
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(iso).astimezone(ZoneInfo(_ET)).strftime(
            "%b %d, %Y %I:%M %p") + " ET"
    except Exception:
        return iso


def _footer_text_tail(d: dict) -> str:
    """'Rendered … · data last pulled … for <channel> (<url>)' — plain-text footer line."""
    tail = f"Rendered {_rendered_stamp()}"
    pulled = _et_fmt(d.get("last_pull"))
    if pulled:
        tail += f" · data last pulled {pulled}"
    ch = d.get("channel")
    if ch:
        tail += f" for {ch['display']} ({ch['url']})"
    return tail


def _footer_html_tail(d: dict) -> str:
    """Same footer line for HTML, with the channel as a muted underlined link."""
    from html import escape
    tail = f"Rendered {_rendered_stamp()}"
    pulled = _et_fmt(d.get("last_pull"))
    if pulled:
        tail += f" · data last pulled {pulled}"
    ch = d.get("channel")
    if ch:
        link = (f'<a href="{escape(ch["url"])}" style="color:{_MUTED};'
                f'text-decoration:underline;">{escape(ch["display"])}</a>')
        tail += f" for {link}"
    return tail


def _no_data_html(d: dict) -> str:
    """The day-1 'warming up' HTML digest (no analytics rows yet)."""
    callout = (f'<div style="background:{_GOOD}12;border-left:4px solid {_GOOD};'
               f'border-radius:6px;padding:12px 14px;margin:0 0 14px;color:{_NAVY};'
               f'font-size:15px;font-weight:600;">✅ Warming up — no analytics yet.</div>')
    msg = ("<p style=\"margin:0 0 8px;\">YouTube's Analytics API lags about 2–3 days, so the "
           "first numbers land here within a few days of activity.</p>"
           "<p style=\"margin:0;\">Nothing needed from you — this email will fill in on its "
           "own.</p>")
    footer = (f'<div style="color:{_MUTED};font-size:11px;margin:18px 0 0;'
              f'border-top:1px solid {_LINE};padding-top:10px;">{_footer_html_tail(d)}.</div>')
    return (
        f'<div style="background:{_CREAM};padding:16px 0;font-family:{_FONT};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f'<tr><td align="center" style="padding:0 12px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px;text-align:left;"><tr><td>'
        f'{callout}<div style="color:{_NAVY};font-size:14px;">{msg}</div>{footer}'
        f'</td></tr></table></td></tr></table></div>'
    )


def render_html(digest: dict, *, img_cid: str | None) -> str:
    """Native, mobile-first HTML digest on the brand palette. Answers, top to bottom:
    the verdict/action callout, a scoreboard (yesterday), the week (chart + WoW), and the
    latest video (pace, retention, vs. a typical video at the same age). The plain-text
    ``render_text`` remains the fallback alternative."""
    from html import escape

    d = digest
    if d.get("no_data"):
        return _no_data_html(d)
    fresh = "est." if d["latest_estimated"] else "final"

    # --- latest day (scoreboard) ----------------------------------------------------
    day_note = (f'<div style="color:{_MUTED};font-size:12px;margin:-4px 0 8px;">'
                f'{_fmt_date(d["latest_date"])} · {fresh}</div>')
    views_sec = f'd/d {_html_delta(d["views_dd"])} · 7d {_html_delta(d["views_v7"])}'
    wk = d["week"]
    watch_sec = f'wk {_html_delta(wk["watch_hours"]["wow"])}'
    subs_wk = wk["net_subs"]["this"]
    subs_sec = f'<span style="color:{_MUTED};">wk {subs_wk:+d}</span>'
    if d["revenue"] is None:
        rev_val = "—"
        rev_sec = f'<span style="color:{_MUTED};">pre-monetization</span>'
    else:
        rev_val = _fmt_money(d["revenue"])
        rev_sec = f'd/d {_html_delta(d["rev_dd"])} · 7d {_html_delta(d["rev_v7"])}'
    scoreboard = (
        _section_label("Latest day") + day_note
        + f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="border:1px solid {_LINE};border-radius:8px;background:{_CARD};'
        f'margin:0 0 16px;"><tr>'
        + _stat_cell("Views", f'{d["views"]:,}', views_sec)
        + _stat_cell("Watch hrs", f'{d["watch_hours"]:,.1f}', watch_sec)
        + "</tr><tr>"
        + _stat_cell("Net subs", f'{d["net_subs"]:+d}', subs_sec)
        + _stat_cell("Revenue", rev_val, rev_sec)
        + "</tr></table>"
    )

    # --- this week (chart + WoW lines) ---------------------------------------------
    chart = ""
    if img_cid:
        chart = (f'<img src="cid:{escape(img_cid)}" alt="7-day trend" '
                 f'style="width:100%;max-width:100%;height:auto;display:block;'
                 f'border-radius:6px;margin:0 0 10px;">')

    week_html = _section_label("Last 7 days") + chart + _period_lines(wk, "prev 7d")

    # --- month to date (same layout, no chart) -------------------------------------
    month_note = (f'<div style="color:{_MUTED};font-size:12px;margin:-4px 0 6px;">'
                  f'vs the same point last month</div>')
    month_html = _section_label("Month to date") + month_note + _period_lines(d["month"], "last mo")

    # --- latest video ---------------------------------------------------------------
    lv = d["latest_video"]
    if lv:
        trend_word = {"up": "climbing", "fading": "fading", "flat": "steady"}[lv["trend"]]
        meta = (f'Day {lv["days_since"]} · {lv["total"]:,} views total · {trend_word}')
        quality = f'Avg viewed {lv["avp"]:.0f}%'
        vt = lv.get("vs_typical")
        if vt:
            word = "ahead of" if vt["ahead"] else "behind"
            color = _GOOD if vt["ahead"] else _AMBER
            quality += (f' · <span style="color:{color};font-weight:600;">{word}</span> '
                        f'your typical video at day {lv["days_since"]} '
                        f'(median {vt["median"]:,.0f})')
        video_html = (
            _section_label("Latest video")
            + f'<div style="color:{_NAVY};font-size:15px;font-weight:700;margin:0 0 3px;">'
            f'{escape(lv["title"])}</div>'
            + f'<div style="color:{_MUTED};font-size:13px;margin:0 0 4px;">{meta}</div>'
            + f'<div style="color:{_NAVY};font-size:14px;">{quality}</div>'
        )
    else:
        video_html = _section_label("Latest video") + (
            f'<div style="color:{_MUTED};font-size:14px;">No videos found.</div>')

    footer = (f'<div style="color:{_MUTED};font-size:11px;margin:18px 0 0;'
              f'border-top:1px solid {_LINE};padding-top:10px;">'
              f'Recent days are YouTube estimates and will revise.<br>'
              f'{_footer_html_tail(d)}.</div>')

    return (
        f'<div style="background:{_CREAM};padding:16px 0;font-family:{_FONT};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f'<tr><td align="center" style="padding:0 12px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px;text-align:left;">'
        f'<tr><td>{_callout(d)}{scoreboard}{week_html}'
        f'<div style="height:16px;"></div>{month_html}'
        f'<div style="height:16px;"></div>{video_html}{footer}</td></tr>'
        f'</table></td></tr></table></div>'
    )
