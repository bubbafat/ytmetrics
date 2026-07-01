"""Weekly channel-intelligence briefing — a brand-styled multi-page PDF generated from
the local db. Repeatable: `ytmetrics briefing --out report.pdf` (the weekly job can run it).

Internal-momentum only — every number comes from ytmetrics.db; no external/web calls.
matplotlib is an optional dependency (the `briefing` extra); imported lazily so the core
tool stays slim.
"""

from __future__ import annotations

import sqlite3
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

# --- Brand kit (Rough Cut "Studio") ------------------------------------------------
NAVY = "#14304A"     # background
SURFACE = "#1C3E5C"  # panels
CREAM = "#F4EFE3"    # primary text
CORAL = "#F0824A"    # the data
SKY = "#6FB7DC"      # totals / accent
MUTED = "#9DB2C4"    # secondary text on navy
SAND = "#D2925A"     # sparing secondary
RED = "#E0604A"
GREEN = "#7FB069"

PAGE = (13.333, 7.5)  # 16:9 slide


# --- fonts (use brand fonts if cached on the Mac; else graceful fallback) ----------
def _fonts():
    import matplotlib.font_manager as fm

    cache = Path.home() / ".cache" / "brand-fonts"
    disp = body = None
    if cache.is_dir():
        for f in cache.glob("*.[to]tf"):
            name = f.name.lower()
            try:
                fm.fontManager.addfont(str(f))
                fam = fm.FontProperties(fname=str(f)).get_name()
            except Exception:
                continue
            if "bebas" in name:
                disp = fam
            elif "montserrat" in name:
                body = fam
    return disp or "DejaVu Sans", body or "DejaVu Sans"


# --- small query helpers -----------------------------------------------------------
def _rows(c, sql, p=()):
    return c.execute(sql, p).fetchall()


def _val(c, sql, p=(), default=0):
    r = c.execute(sql, p).fetchone()
    return (r[0] if r and r[0] is not None else default)


def _window(c):
    mx = _val(c, "SELECT max(date) FROM channel_daily", default=None)
    mon = _val(c, "SELECT min(date) FROM channel_revenue_daily", default=None)
    return mx, mon


def _agg(c, lo, hi):
    r = _rows(
        c,
        "SELECT coalesce(sum(views),0) v, coalesce(sum(estimated_minutes_watched),0) m, "
        "coalesce(sum(subscribers_gained)-sum(subscribers_lost),0) net "
        "FROM channel_daily WHERE date BETWEEN ? AND ?",
        (lo, hi),
    )[0]
    rev = _val(
        c, "SELECT sum(estimated_revenue) FROM channel_revenue_daily WHERE date BETWEEN ? AND ?",
        (lo, hi),
    )
    return {"views": r["v"], "mins": r["m"], "net": r["net"], "rev": rev or 0.0}


def _latest_window_end(c, table):
    return _val(c, f"SELECT max(window_end) FROM {table}", default=None)


# --- page primitives ---------------------------------------------------------------
class Deck:
    def __init__(self, pdf, disp, body):
        self.pdf, self.disp, self.body = pdf, disp, body

    def page(self):
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=PAGE)
        fig.patch.set_facecolor(NAVY)
        return fig

    def save(self, fig):
        self.pdf.savefig(fig, facecolor=NAVY)
        import matplotlib.pyplot as plt

        plt.close(fig)

    def header(self, fig, eyebrow, title):
        fig.text(0.06, 0.90, eyebrow.upper(), color=SKY, fontsize=13,
                 family=self.body, fontweight="bold")
        fig.text(0.06, 0.82, title, color=CREAM, fontsize=34, family=self.disp)
        fig.lines = []

    def footer(self, fig, idx, total, sub, name):
        fig.text(0.06, 0.05, f"{name.upper()} · WEEKLY BRIEFING", color=MUTED,
                 fontsize=9, family=self.body)
        fig.text(0.94, 0.05, f"{sub}   ·   {idx}/{total}", color=MUTED, fontsize=9,
                 family=self.body, ha="right")

    def ax(self, fig, rect):
        ax = fig.add_axes(rect)
        ax.set_facecolor(NAVY)
        for s in ax.spines.values():
            s.set_color(MUTED)
        ax.tick_params(colors=MUTED, labelsize=9)
        for lbl in ax.get_xticklabels() + ax.get_yticklabels():
            lbl.set_color(CREAM)
        return ax


def _delta(now, prev):
    if not prev:
        return "", MUTED
    pct = (now - prev) / abs(prev) * 100
    arrow = "▲" if pct >= 0 else "▼"
    col = GREEN if pct >= 0 else RED
    return f"{arrow} {abs(pct):.0f}%", col


# --- the pages ---------------------------------------------------------------------
def generate(db_path: str | Path, out_path: str | Path, weeks: int = 1) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.backends.backend_pdf import PdfPages

    disp, body = _fonts()
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    mx, mon = _window(c)
    if not mx:
        raise RuntimeError("no channel_daily data in db — run `ytmetrics pull` first")
    mx_d = datetime.strptime(mx, "%Y-%m-%d").date()
    span = 7 * weeks
    this_lo = (mx_d - timedelta(days=span - 1)).isoformat()
    prev_hi = (mx_d - timedelta(days=span)).isoformat()
    prev_lo = (mx_d - timedelta(days=2 * span - 1)).isoformat()
    cur, prev = _agg(c, this_lo, mx), _agg(c, prev_lo, prev_hi)

    from .channel import identity
    name = (identity(c) or {}).get("name") or "Your channel"

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sub = f"{this_lo} → {mx}"

    with PdfPages(str(out_path)) as pdf:
        d = Deck(pdf, disp, body)
        pages = [
            _p_cover, _p_scoreboard, _p_latest_video, _p_working,
            _p_audience, _p_monetization, _p_discovery, _p_actions,
        ]
        ctx = dict(c=c, mx=mx, mon=mon, cur=cur, prev=prev, this_lo=this_lo, sub=sub, name=name)
        for i, fn in enumerate(pages, 1):
            fig = d.page()
            fn(d, fig, ctx)
            d.footer(fig, i, len(pages), sub, name)
            d.save(fig)
    c.close()
    return out_path


def _tile(d, fig, x, y, w, h, label, value, delta=""):
    fig.patches.append(_panel(fig, x, y, w, h))
    fig.text(x + 0.02, y + h - 0.05, label.upper(), color=MUTED, fontsize=10, family=d.body)
    fig.text(x + 0.02, y + 0.05, value, color=CREAM, fontsize=30, family=d.disp)
    if delta:
        txt, col = delta
        fig.text(x + w - 0.02, y + h - 0.05, txt, color=col, fontsize=13,
                 family=d.body, fontweight="bold", ha="right")


def _panel(fig, x, y, w, h):
    from matplotlib.patches import FancyBboxPatch

    return FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.004,rounding_size=0.012",
                          transform=fig.transFigure, facecolor=SURFACE, edgecolor="none",
                          mutation_aspect=PAGE[0] / PAGE[1])


def _p_cover(d, fig, ctx):
    cur, prev = ctx["cur"], ctx["prev"]
    fig.text(0.06, 0.86, ctx["name"].upper(), color=SKY, fontsize=15, family=d.body,
             fontweight="bold")
    fig.text(0.06, 0.78, "Weekly Channel\nBriefing", color=CREAM, fontsize=52,
             family=d.disp, linespacing=0.95, va="top")
    fig.text(0.06, 0.52, ctx["sub"], color=MUTED, fontsize=14, family=d.body)
    # three headline tiles
    _tile(d, fig, 0.06, 0.24, 0.26, 0.20, "Views (7d)", f"{cur['views']:,}",
          _delta(cur["views"], prev["views"]))
    _tile(d, fig, 0.37, 0.24, 0.26, 0.20, "Net subscribers", f"+{cur['net']}",
          _delta(cur["net"], prev["net"]))
    _tile(d, fig, 0.68, 0.24, 0.26, 0.20, "Revenue (7d)", f"${cur['rev']:.2f}",
          _delta(cur["rev"], prev["rev"]))
    # auto takeaway
    sub_dir = "accelerating" if cur["net"] >= prev["net"] else "cooling"
    view_dir = "up" if cur["views"] >= prev["views"] else "down"
    tl = f"Subs {sub_dir} (+{cur['net']} vs +{prev['net']}); views {view_dir} week over week."
    fig.text(0.06, 0.15, "THE TAKEAWAY", color=CORAL, fontsize=12, family=d.body,
             fontweight="bold")
    fig.text(0.06, 0.105, tl, color=CREAM, fontsize=14, family=d.body, wrap=True)


def _p_scoreboard(d, fig, ctx):
    d.header(fig, "the week vs last", "Scoreboard")
    cur, prev = ctx["cur"], ctx["prev"]
    rpm = (cur["rev"] / cur["views"] * 1000) if cur["views"] else 0
    rpm_p = (prev["rev"] / prev["views"] * 1000) if prev["views"] else 0
    tiles = [
        ("Views", f"{cur['views']:,}", _delta(cur["views"], prev["views"])),
        ("Watch hrs", f"{cur['mins'] / 60:,.0f}", _delta(cur["mins"], prev["mins"])),
        ("Net subs", f"+{cur['net']}", _delta(cur["net"], prev["net"])),
        ("Revenue", f"${cur['rev']:.2f}", _delta(cur["rev"], prev["rev"])),
        ("RPM", f"${rpm:.2f}", _delta(rpm, rpm_p)),
    ]
    x = 0.06
    for label, value, dl in tiles:
        _tile(d, fig, x, 0.45, 0.165, 0.22, label, value, dl)
        x += 0.178
    fig.text(0.06, 0.34, "Versus the prior 7 days. RPM uses revenue ÷ views ×1000 over the "
             "monetized window.", color=MUTED, fontsize=12, family=d.body)


def _p_latest_video(d, fig, ctx):
    c = ctx["c"]
    d.header(fig, "newest upload", "Latest video")
    v = _rows(c, "SELECT video_id, title, published_at FROM videos "
                 "WHERE content_type='VIDEO_ON_DEMAND' ORDER BY published_at DESC LIMIT 1")
    if not v:
        fig.text(0.06, 0.5, "No videos found.", color=CREAM, fontsize=16, family=d.body)
        return
    v = v[0]
    vid = v["video_id"]
    s = _rows(c, "SELECT coalesce(sum(views),0) v, coalesce(sum(subscribers_gained),0) sg, "
                 "avg(average_view_percentage) avp FROM video_daily WHERE video_id=?", (vid,))[0]
    spk = (1000.0 * s["sg"] / s["v"]) if s["v"] else 0
    # channel medians for benchmarking
    conv = [r[0] for r in _rows(
        c, "SELECT 1000.0*sum(subscribers_gained)/sum(views) FROM video_daily "
           "GROUP BY video_id HAVING sum(views)>200") if r[0] is not None]
    ret = [r[0] for r in _rows(
        c, "SELECT avg(average_view_percentage) FROM video_daily GROUP BY video_id") if r[0]]
    med_spk = median(conv) if conv else 0
    med_avp = median(ret) if ret else 0
    fig.text(0.06, 0.70, v["title"][:80], color=CREAM, fontsize=20, family=d.disp)
    fig.text(0.06, 0.645, f"published {v['published_at'][:10]}", color=MUTED, fontsize=12,
             family=d.body)
    _tile(d, fig, 0.06, 0.38, 0.20, 0.20, "Views so far", f"{s['v']:,}")
    _tile(d, fig, 0.28, 0.38, 0.20, 0.20, "Avg % viewed",
          f"{(s['avp'] or 0):.0f}%")
    _tile(d, fig, 0.50, 0.38, 0.20, 0.20, "Subs gained", f"{s['sg']}")
    _tile(d, fig, 0.72, 0.38, 0.22, 0.20, "Subs / 1k views", f"{spk:.1f}")
    # verdict
    verdicts = []
    verdicts.append(("converts above" if spk >= med_spk else "converts below")
                    + f" your median ({med_spk:.1f}/1k)")
    verdicts.append(("holds attention better" if (s["avp"] or 0) >= med_avp
                     else "holds attention worse")
                    + f" than median ({med_avp:.0f}%)")
    fig.text(0.06, 0.27, "VERDICT", color=CORAL, fontsize=12, family=d.body, fontweight="bold")
    fig.text(0.06, 0.225, "This video " + verdicts[0] + "; it " + verdicts[1] + ".",
             color=CREAM, fontsize=14, family=d.body)


def _p_working(d, fig, ctx):
    c = ctx["c"]
    d.header(fig, "winners & weak spots", "What's working")
    top = _rows(c, "SELECT substr(v.title,1,46) t, "
                   "1000.0*sum(vd.subscribers_gained)/sum(vd.views) spk "
                   "FROM video_daily vd JOIN videos v USING(video_id) GROUP BY vd.video_id "
                   "HAVING sum(vd.views)>200 ORDER BY spk DESC LIMIT 6")
    ax = d.ax(fig, [0.06, 0.18, 0.55, 0.56])
    labels = [r["t"] for r in top][::-1]
    vals = [r["spk"] for r in top][::-1]
    ypos = list(range(len(vals)))
    ax.barh(ypos, vals, color=CORAL, height=0.74)
    vmax = max(vals) if vals else 1
    ax.set_xlim(0, vmax * 1.12)
    ax.set_yticks([])
    ax.set_xticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)
    pad = vmax * 0.02
    for yi, (lab, v) in enumerate(zip(labels, vals, strict=False)):
        ax.text(pad, yi, lab, va="center", ha="left", color=NAVY, fontsize=11,
                family=d.body)                                   # title inside the bar
        ax.text(v + pad, yi, f"{v:.1f}", va="center", ha="left", color=CREAM, fontsize=11,
                family=d.body, fontweight="bold")                # value at the tip
    ax.set_title("Top subscriber converters — subs / 1k views", color=CREAM, fontsize=12,
                 family=d.body, loc="left", pad=10)
    # retention cliff callout
    we = _latest_window_end(c, "video_retention")
    drop_txt = "run `insights` for retention"
    if we:
        a0 = _val(c, "SELECT avg(audience_watch_ratio) FROM video_retention "
                     "WHERE window_end=? AND abs(elapsed_ratio-0.0)<0.03", (we,), default=1.0)
        a10 = _val(c, "SELECT avg(audience_watch_ratio) FROM video_retention "
                      "WHERE window_end=? AND abs(elapsed_ratio-0.1)<0.03", (we,), default=a0)
        drop_txt = f"{(a0 - a10) * 100:.0f}% of viewers leave in the first 10%"
    fig.patches.append(_panel(fig, 0.64, 0.46, 0.30, 0.26))
    fig.text(0.66, 0.685, "THE WEAK SPOT", color=CORAL, fontsize=12, family=d.body,
             fontweight="bold")
    fig.text(0.66, 0.63, "Intro retention", color=CREAM, fontsize=20, family=d.disp)
    fig.text(0.66, 0.575,
             textwrap.fill(drop_txt + " — the single highest-leverage fix.", 38),
             color=CREAM, fontsize=12.5, family=d.body, va="top", linespacing=1.3)
    fig.patches.append(_panel(fig, 0.64, 0.20, 0.30, 0.22))
    fig.text(0.66, 0.36, "FORMAT REALITY", color=CORAL, fontsize=12, family=d.body,
             fontweight="bold")
    fig.text(0.66, 0.27, "Long-form pays; Shorts ~$0.\nLean into long-form.",
             color=CREAM, fontsize=14, family=d.body)


def _p_audience(d, fig, ctx):
    c = ctx["c"]
    d.header(fig, "who's actually watching", "Audience")
    # age bars
    we = _latest_window_end(c, "channel_demographics")
    ax1 = d.ax(fig, [0.07, 0.20, 0.40, 0.50])
    if we:
        age = _rows(c, "SELECT age_group, sum(viewer_percentage) p FROM channel_demographics "
                       "WHERE window_end=? GROUP BY age_group ORDER BY age_group", (we,))
        labels = [r["age_group"].replace("age", "") for r in age]
        vals = [r["p"] for r in age]
        ax1.bar(labels, vals, color=CORAL, width=0.7)
        ax1.set_title("Age (% of viewers)", color=CREAM, fontsize=12, family=d.body,
                      loc="left", pad=8)
        for sp in ("top", "right"):
            ax1.spines[sp].set_visible(False)
    else:
        ax1.text(0.5, 0.5, "run insights", color=MUTED, ha="center")
    # device donut
    wd = _latest_window_end(c, "audience_devices")
    ax2 = d.ax(fig, [0.52, 0.24, 0.20, 0.42])
    if wd:
        dev = _rows(c, "SELECT device_type, sum(views) v FROM audience_devices "
                       "WHERE window_end=? GROUP BY device_type ORDER BY v DESC", (wd,))
        ax2.pie([r["v"] for r in dev], labels=[r["device_type"].title() for r in dev],
                colors=[CORAL, SKY, SAND, MUTED, CREAM], textprops={"color": CREAM, "fontsize": 9},
                wedgeprops={"width": 0.42}, startangle=90)
        ax2.set_title("Devices", color=CREAM, fontsize=12, family=d.body)
    # geography
    wg = _latest_window_end(c, "audience_geography")
    ax3 = d.ax(fig, [0.78, 0.20, 0.17, 0.50])
    if wg:
        geo = _rows(c, "SELECT country, sum(views) v FROM audience_geography "
                       "WHERE window_end=? GROUP BY country ORDER BY v DESC LIMIT 5", (wg,))
        labels = [r["country"] for r in geo][::-1]
        vals = [r["v"] for r in geo][::-1]
        ax3.barh(labels, vals, color=SKY, height=0.6)
        ax3.set_title("Top countries", color=CREAM, fontsize=12, family=d.body, loc="left", pad=8)
        for sp in ("top", "right", "bottom"):
            ax3.spines[sp].set_visible(False)
        ax3.tick_params(length=0)
    fig.text(0.07, 0.13, "Older and more TV-bound than the stated 30–65 target — bigger "
             "on-screen text, lean-back pacing.", color=MUTED, fontsize=12, family=d.body)


def _p_monetization(d, fig, ctx):
    c = ctx["c"]
    d.header(fig, "the money", "Monetization")
    rows = _rows(c, "SELECT date, estimated_revenue r FROM channel_revenue_daily ORDER BY date")
    ax = d.ax(fig, [0.08, 0.24, 0.54, 0.48])
    xs = [datetime.strptime(r["date"], "%Y-%m-%d").date() for r in rows]
    ys = [r["r"] for r in rows]
    ax.plot(xs, ys, color=CORAL, lw=2)
    ax.fill_between(xs, ys, color=CORAL, alpha=0.12)
    ax.set_title("Daily estimated revenue", color=CREAM, fontsize=12, family=d.body,
                 loc="left", pad=8)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    import matplotlib.dates as mdates

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    # concentration + total
    tot = _val(c, "SELECT sum(estimated_revenue) FROM channel_revenue_daily")
    earners = [r[0] for r in _rows(
        c, "SELECT sum(estimated_revenue) FROM video_revenue_daily GROUP BY video_id "
           "ORDER BY 1 DESC")]
    top3 = sum(earners[:3]) / sum(earners) * 100 if earners else 0
    _tile(d, fig, 0.66, 0.50, 0.28, 0.20, "Total since monetizing", f"${tot:.2f}")
    _tile(d, fig, 0.66, 0.26, 0.28, 0.20, "Top-3 video share", f"{top3:.0f}%")
    fig.text(0.08, 0.16, "Revenue is concentrated in a few videos and entirely long-form — "
             "every new long-form upload compounds the catalog.", color=MUTED, fontsize=12,
             family=d.body)


def _p_discovery(d, fig, ctx):
    c = ctx["c"]
    d.header(fig, "how they find you + ideas", "Discovery & topic radar")
    # traffic mix donut (last 28d)
    mx = ctx["mx"]
    lo28 = (datetime.strptime(mx, "%Y-%m-%d").date() - timedelta(days=27)).isoformat()
    mix = _rows(c, "SELECT traffic_source_type t, sum(views) v FROM channel_traffic_sources_daily "
                   "WHERE date>=? GROUP BY t ORDER BY v DESC LIMIT 5", (lo28,))
    ax = d.ax(fig, [0.07, 0.24, 0.22, 0.44])
    if mix:
        ax.pie([r["v"] for r in mix], labels=[r["t"].replace("_", " ").title()[:12] for r in mix],
               colors=[CORAL, SKY, SAND, MUTED, CREAM], textprops={"color": CREAM, "fontsize": 8},
               wedgeprops={"width": 0.42}, startangle=90)
        ax.set_title("Traffic mix (28d)", color=CREAM, fontsize=12, family=d.body)
    # top search terms
    we = _latest_window_end(c, "traffic_source_detail")
    terms = _rows(
        c, "SELECT detail, views FROM traffic_source_detail WHERE traffic_source_type="
        "'YT_SEARCH' AND window_end=? ORDER BY views DESC LIMIT 8", (we,)) if we else []
    fig.text(0.36, 0.66, "TOP SEARCHES THAT FIND YOU", color=CORAL, fontsize=12, family=d.body,
             fontweight="bold")
    y = 0.60
    for r in terms:
        fig.text(0.36, y, f"“{r['detail']}”", color=CREAM, fontsize=13, family=d.body)
        fig.text(0.62, y, f"{r['views']}", color=SKY, fontsize=13, family=d.body)
        y -= 0.045
    # rising videos (last 7d vs prior 7d)
    mxd = datetime.strptime(mx, "%Y-%m-%d").date()
    a_lo = (mxd - timedelta(days=6)).isoformat()
    b_hi = (mxd - timedelta(days=7)).isoformat()
    b_lo = (mxd - timedelta(days=13)).isoformat()
    rising = _rows(c,
        "SELECT substr(v.title,1,30) t, "
        " coalesce(sum(case when vd.date>=? then vd.views end),0) a, "
        " coalesce(sum(case when vd.date between ? and ? then vd.views end),0) b "
        "FROM video_daily vd JOIN videos v USING(video_id) GROUP BY vd.video_id "
        "HAVING a>20 AND a>b ORDER BY (a-b) DESC LIMIT 4", (a_lo, b_lo, b_hi))
    fig.text(0.70, 0.66, "GAINING STEAM", color=CORAL, fontsize=12, family=d.body,
             fontweight="bold")
    y = 0.60
    for r in rising:
        fig.text(0.70, y, f"{r['t']}", color=CREAM, fontsize=12, family=d.body)
        fig.text(0.70, y - 0.025, f"+{r['a'] - r['b']} views w/w", color=GREEN, fontsize=10,
                 family=d.body)
        y -= 0.075
    fig.text(0.07, 0.14, "Title to the specific terms people are already searching for — "
             "that's where the demand is.", color=MUTED, fontsize=12,
             family=d.body)


def _p_actions(d, fig, ctx):
    c = ctx["c"]
    d.header(fig, "do these next", "Action plan — top 3")
    # derive supporting numbers
    we = _latest_window_end(c, "video_retention")
    cliff = None
    if we:
        a0 = _val(c, "SELECT avg(audience_watch_ratio) FROM video_retention "
                     "WHERE window_end=? AND abs(elapsed_ratio-0.0)<0.03", (we,), default=1.0)
        a10 = _val(c, "SELECT avg(audience_watch_ratio) FROM video_retention "
                      "WHERE window_end=? AND abs(elapsed_ratio-0.1)<0.03", (we,), default=a0)
        cliff = (a0 - a10) * 100
    top = _rows(c, "SELECT substr(v.title,1,34) t, "
                   "1000.0*sum(vd.subscribers_gained)/sum(vd.views) spk "
                   "FROM video_daily vd JOIN videos v USING(video_id) GROUP BY vd.video_id "
                   "HAVING sum(vd.views)>200 ORDER BY spk DESC LIMIT 1")
    wse = _latest_window_end(c, "traffic_source_detail")
    terms = _rows(
        c, "SELECT detail FROM traffic_source_detail WHERE traffic_source_type="
        "'YT_SEARCH' AND window_end=? ORDER BY views DESC LIMIT 3", (wse,)) if wse else []
    term_str = ", ".join(f"“{r['detail']}”" for r in terms) or "your top search terms"

    actions = []
    if cliff is not None:
        actions.append(("Tighten the first 15 seconds",
                        f"~{cliff:.0f}% of viewers leave in the first 10% of the video. A "
                        f"sharper cold-open hook is the highest-leverage change available."))
    if top:
        actions.append((f"Make another like “{top[0]['t']}…”",
                        f"It converts {top[0]['spk']:.1f} subscribers per 1k views — your best. "
                        f"More of this format/topic compounds growth."))
    actions.append(("Title to the searches that already find you",
                    f"Search is a top source. Lead titles with the specific terms people use: "
                    f"{term_str}."))
    if len(actions) < 3:
        actions.append(("Keep a consistent publish cadence",
                        "Regular uploads compound: they give the algorithm more to recommend "
                        "and keep your audience coming back."))
    actions = actions[:3]

    top, H, gap = 0.72, 0.18, 0.025
    for i, (head, body) in enumerate(actions, 1):
        cy = top - (i - 1) * (H + gap)   # card top edge
        mid = cy - H / 2                 # vertical centre of the card
        fig.patches.append(_panel(fig, 0.06, cy - H, 0.88, H))
        fig.text(0.105, mid, str(i), color=CORAL, fontsize=42, family=d.disp,
                 ha="center", va="center")
        fig.text(0.16, mid + 0.035, head, color=CREAM, fontsize=18, family=d.disp,
                 va="center")
        fig.text(0.16, mid - 0.02, textwrap.fill(body, 95), color=MUTED,
                 fontsize=12.5, family=d.body, va="top", linespacing=1.3)
