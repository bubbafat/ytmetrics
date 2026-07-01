"""The daily digest computes from a populated db and renders calm plain-text email."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from ytmetrics import daily, mailer, pipeline
from ytmetrics.config import EmailConfig
from ytmetrics.sources.replay import ReplaySource
from ytmetrics.store.sqlite_store import SqliteStore

from .conftest import make_channel


def _populated_db(tmp_path: Path, fixtures_dir: Path) -> Path:
    ch = make_channel(tmp_path)  # include_revenue=True by default
    src = ReplaySource(fixtures_dir)
    db = tmp_path / "t.db"
    with SqliteStore(db) as store:
        pipeline.run_pull(store, src, [ch], date(2026, 6, 1), date(2026, 6, 3))
    return db


def test_compute_returns_expected_shape(tmp_path, fixtures_dir):
    d = daily.compute(_populated_db(tmp_path, fixtures_dir))
    for key in ("latest_date", "latest_estimated", "recent_days", "prev_date", "views",
                "net_subs", "status", "headline", "top_videos", "spark_views", "spark_rev"):
        assert key in d
    assert 1 <= len(d["recent_days"]) <= 5
    assert all("estimated" in rd for rd in d["recent_days"])
    assert d["status"] in {"normal", "alert"}
    assert isinstance(d["views"], int)
    # deltas are numeric or None (None pre-baseline)
    for k in ("views_dd", "views_v7", "rev_dd", "rev_v7"):
        assert d[k] is None or isinstance(d[k], float)
    # sparklines are 7 days long (latest day plus the prior 6)
    assert len(d["spark_views"]) == 7
    assert len(d["spark_rev"]) == 7
    assert len(d["spark_views_vals"]) == 7


def test_render_text_subject_and_body(tmp_path, fixtures_dir):
    # today close to the fixture's latest day -> not stale, normal verdict path
    subject, body = daily.render_text(
        daily.compute(_populated_db(tmp_path, fixtures_dir), today=date(2026, 6, 5)))
    assert subject.startswith("Empty Besters")
    assert any(i in subject for i in ("✅", "⚠️", "🟢"))   # 🟢 = good surge
    assert "LATEST DAY" in body
    assert "RECENT DAYS" in body


def test_chart_png_returns_png_bytes(tmp_path, fixtures_dir):
    pytest.importorskip("matplotlib")  # only this test needs the briefing extra
    d = daily.compute(_populated_db(tmp_path, fixtures_dir))
    png = daily.chart_png(d)
    assert isinstance(png, bytes)
    assert png.startswith(b"\x89PNG")


def test_render_html_is_native_with_sections_and_chart(tmp_path, fixtures_dir):
    d = daily.compute(_populated_db(tmp_path, fixtures_dir), today=date(2026, 6, 5))
    html = daily.render_html(d, img_cid="trend")
    assert html.startswith("<div")          # native HTML container, not a <pre>
    assert "<pre" not in html
    assert "cid:trend" in html              # inline chart still embedded
    assert "Last 7 days" in html            # week section
    assert "Month to date" in html          # month section
    assert "Latest day" in html             # day section label
    assert "Latest video" in html           # video section
    assert "Views" in html and "Revenue" in html   # scoreboard metrics


def test_render_html_shows_week_and_vs_typical(tmp_path):
    # a db with two videos: an old one (long history) + a brand-new one to benchmark
    import sqlite3

    from ytmetrics.store.sqlite_store import SqliteStore
    db = tmp_path / "t.db"
    with SqliteStore(db):
        pass
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO videos (video_id, channel_id, title, published_at, content_type) "
                 "VALUES (?,?,?,?,?)", ("OLD", "UC", "Old vid", "2026-05-01T00:00:00Z",
                                       "VIDEO_ON_DEMAND"))
    conn.execute("INSERT INTO videos (video_id, channel_id, title, published_at, content_type) "
                 "VALUES (?,?,?,?,?)", ("NEW", "UC", "New vid", "2026-06-24T00:00:00Z",
                                       "VIDEO_ON_DEMAND"))
    for n in range(21):                      # 3 weeks of channel + old-video days
        day = (date(2026, 6, 3) + timedelta(days=n)).isoformat()
        conn.execute("INSERT INTO channel_daily (channel_id, date, creator_content_type, views, "
                     "estimated_minutes_watched, subscribers_gained, subscribers_lost) "
                     "VALUES (?,?,?,?,?,?,?)", ("UC", day, "VIDEO_ON_DEMAND", 100, 300, 2, 1))
        conn.execute("INSERT INTO video_daily (channel_id, video_id, date, content_type, "
                     "views, average_view_percentage) VALUES (?,?,?,?,?,?)",
                     ("UC", "OLD", day, "VIDEO_ON_DEMAND", 40, 45.0))
    for n in range(3):                       # the new video's first 3 days
        day = (date(2026, 6, 24) + timedelta(days=n)).isoformat()
        conn.execute("INSERT INTO video_daily (channel_id, video_id, date, content_type, "
                     "views, average_view_percentage) VALUES (?,?,?,?,?,?)",
                     ("UC", "NEW", day, "VIDEO_ON_DEMAND", 30, 50.0))
    conn.commit()
    conn.close()
    d = daily.compute(db, today=date(2026, 6, 26))
    assert set(d["week"]["views"]) == {"this", "last", "wow"}
    assert d["latest_video"]["vs_typical"] is None or "median" in d["latest_video"]["vs_typical"]
    html = daily.render_html(d, img_cid=None)
    assert "prev 7d" in html and "last mo" in html   # week + month framing present


def test_latest_day_is_always_estimated(tmp_path, fixtures_dir):
    # The freshest day we have is what YouTube is still revising, so it's an estimate
    # regardless of how far behind "today" the API runs (the whole point of the fix).
    db = _populated_db(tmp_path, fixtures_dir)   # latest day = 2026-06-03
    assert daily.compute(db, today=date(2026, 6, 4))["latest_estimated"] is True
    assert daily.compute(db, today=date(2026, 6, 29))["latest_estimated"] is True


def test_est_window_anchored_to_freshest_day(tmp_path):
    # 6 consecutive days; the newest REVISION_DAYS+1 (=3) are est, older ones finalized —
    # anchored to the latest day in the db, not to today.
    import sqlite3

    from ytmetrics.store.sqlite_store import SqliteStore
    db = tmp_path / "t.db"
    with SqliteStore(db):
        pass
    conn = sqlite3.connect(db)
    for day in ("2026-06-20", "2026-06-21", "2026-06-22", "2026-06-23", "2026-06-24",
                "2026-06-25"):
        conn.execute("INSERT INTO channel_daily (channel_id, date, creator_content_type, "
                     "views) VALUES (?,?,?,?)", ("UC", day, "VIDEO_ON_DEMAND", 100))
    conn.commit()
    conn.close()
    d = daily.compute(db, today=date(2026, 6, 28))   # data ends 06-25; "today" far ahead
    est = {rd["date"]: rd["estimated"] for rd in d["recent_days"]}
    assert est["2026-06-25"] is True and est["2026-06-24"] is True and est["2026-06-23"] is True
    assert est["2026-06-22"] is False and est["2026-06-21"] is False


def test_stale_banner_and_subject(tmp_path, fixtures_dir):
    db = _populated_db(tmp_path, fixtures_dir)   # latest day = 2026-06-03
    d = daily.compute(db, today=date(2026, 6, 20), warn_days=3)   # 17 days behind
    assert d["stale"] is True
    subject, body = daily.render_text(d)
    assert "🔴" in subject and "STALE" in subject
    assert "STALE DATA" in body
    html = daily.render_html(d, img_cid=None)
    assert "STALE DATA" in html and "background:#C5221F" in html


def test_alert_tone_good_vs_bad():
    base = {"views_anomaly": False, "views_v7": None, "rev_anomaly": False, "rev_v7": None,
            "sub_loss": False, "sub_spike": False, "latest_video": None, "traffic": None}
    assert daily._alert_tone({**base, "views_anomaly": True, "views_v7": 0.3}) == "good"
    assert daily._alert_tone({**base, "views_anomaly": True, "views_v7": -0.3}) == "bad"
    assert daily._alert_tone({**base, "sub_loss": True}) == "bad"
    assert daily._alert_tone({**base, "sub_spike": True}) == "good"


# --- pure helpers ------------------------------------------------------------------
def test_pct_helper():
    assert daily._pct(140, 100) == 0.40
    assert daily._pct(80, 100) == -0.20
    assert daily._pct(100, 0) is None
    assert daily._pct(100, None) is None


def test_anomaly_classifier_flags_spike_and_ignores_flat():
    # a 40% spike vs a 7-day avg of 100 -> flagged
    assert daily._is_anomaly(140, 100.0, daily.VIEWS_THRESHOLD) is True
    # a 40% drop -> also flagged (absolute deviation)
    assert daily._is_anomaly(60, 100.0, daily.VIEWS_THRESHOLD) is True
    # flat / small wobble -> not flagged
    assert daily._is_anomaly(100, 100.0, daily.VIEWS_THRESHOLD) is False
    assert daily._is_anomaly(110, 100.0, daily.VIEWS_THRESHOLD) is False
    # no baseline -> never flagged
    assert daily._is_anomaly(140, None, daily.VIEWS_THRESHOLD) is False


def test_sparkline_length_and_chars():
    s = daily.sparkline([1, 2, 3, 4, 5, 6, 7])
    assert len(s) == 7
    assert s[0] == daily.SPARK[0] and s[-1] == daily.SPARK[-1]
    # flat series -> all the same low char
    assert daily.sparkline([3, 3, 3]) == daily.SPARK[0] * 3


# --- mailer.send_text --------------------------------------------------------------
class _FakeSMTP:
    instances: list[_FakeSMTP] = []

    def __init__(self, host, port):
        self.host, self.port, self.sent, self.logged_in = host, port, None, False
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, pw):
        self.logged_in = (user, pw)

    def send_message(self, msg):
        self.sent = msg


def test_send_text_logs_in_and_sends_plain_text(tmp_path, monkeypatch):
    monkeypatch.setenv("YTM_TEST_SMTP_PW", "app-pass-123")
    cfg = EmailConfig(
        smtp_host="smtp.example.com", smtp_port=587,
        username="me@example.com", sender="me@example.com",
        recipients=["me@example.com"],
        password_env="YTM_TEST_SMTP_PW", password_file=tmp_path / "nope",
    )
    _FakeSMTP.instances.clear()
    rcpts = mailer.send_text(cfg, "the subject", "the body", smtp_factory=_FakeSMTP)
    assert rcpts == ["me@example.com"]
    smtp = _FakeSMTP.instances[0]
    assert smtp.logged_in == ("me@example.com", "app-pass-123")
    assert smtp.sent is not None
    assert smtp.sent["Subject"] == "the subject"
    # plain text — no attachments
    assert list(smtp.sent.iter_attachments()) == []
