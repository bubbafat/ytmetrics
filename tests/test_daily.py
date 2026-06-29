"""The daily digest computes from a populated db and renders calm plain-text email."""

from __future__ import annotations

from datetime import date
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
    subject, body = daily.render_text(daily.compute(_populated_db(tmp_path, fixtures_dir)))
    assert subject.startswith("Empty Besters")
    assert ("✅" in subject) or ("⚠️" in subject)
    assert "LATEST DAY" in body
    assert "RECENT DAYS" in body


def test_chart_png_returns_png_bytes(tmp_path, fixtures_dir):
    pytest.importorskip("matplotlib")  # only this test needs the briefing extra
    d = daily.compute(_populated_db(tmp_path, fixtures_dir))
    png = daily.chart_png(d)
    assert isinstance(png, bytes)
    assert png.startswith(b"\x89PNG")


def test_render_html_embeds_chart_and_escaped_text(tmp_path, fixtures_dir):
    d = daily.compute(_populated_db(tmp_path, fixtures_dir))
    html = daily.render_html(d, img_cid="trend")
    assert "cid:trend" in html
    assert "RECENT DAYS" in html          # the escaped digest text is present
    assert html.startswith("<pre")


def test_estimated_flag_tracks_today(tmp_path, fixtures_dir):
    db = _populated_db(tmp_path, fixtures_dir)   # latest day = 2026-06-03
    assert daily.compute(db, today=date(2026, 6, 4))["latest_estimated"] is True
    assert daily.compute(db, today=date(2026, 6, 10))["latest_estimated"] is False


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
    instances: list["_FakeSMTP"] = []

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
