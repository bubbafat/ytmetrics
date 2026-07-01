"""The mailer builds a correct PDF-attached message and sends it over a (faked) SMTP."""

from __future__ import annotations

from pathlib import Path

import pytest

from ytmetrics import mailer
from ytmetrics.config import EmailConfig


def _cfg(tmp_path: Path) -> EmailConfig:
    return EmailConfig(
        smtp_host="smtp.example.com", smtp_port=587,
        username="me@example.com", sender="me@example.com",
        recipients=["me@example.com", "you@example.com"],
        password_env="YTM_TEST_SMTP_PW", password_file=tmp_path / "nope",
    )


def _pdf(tmp_path: Path) -> Path:
    p = tmp_path / "b.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def test_build_message_attaches_pdf(tmp_path):
    msg = mailer.build_message(_cfg(tmp_path), "subj", "body", _pdf(tmp_path))
    assert msg["Subject"] == "subj"
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "me@example.com, you@example.com"
    atts = [a for a in msg.iter_attachments()]
    assert len(atts) == 1
    assert atts[0].get_filename() == "b.pdf"
    assert atts[0].get_content_type() == "application/pdf"


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


def test_send_pdf_uses_password_env_and_sends(tmp_path, monkeypatch):
    monkeypatch.setenv("YTM_TEST_SMTP_PW", "app-pass-123")
    _FakeSMTP.instances.clear()
    rcpts = mailer.send_pdf(_cfg(tmp_path), "s", "b", _pdf(tmp_path), smtp_factory=_FakeSMTP)
    assert rcpts == ["me@example.com", "you@example.com"]
    smtp = _FakeSMTP.instances[0]
    assert smtp.logged_in == ("me@example.com", "app-pass-123")
    assert smtp.sent is not None and smtp.sent["Subject"] == "s"


def test_send_html_multipart_with_inline_image(tmp_path, monkeypatch):
    monkeypatch.setenv("YTM_TEST_SMTP_PW", "app-pass-123")
    _FakeSMTP.instances.clear()
    png = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    rcpts = mailer.send_html(
        _cfg(tmp_path), "subj", "the text body", "<pre>the html body</pre>",
        images={"trend": png}, smtp_factory=_FakeSMTP,
    )
    assert rcpts == ["me@example.com", "you@example.com"]
    smtp = _FakeSMTP.instances[0]
    assert smtp.logged_in == ("me@example.com", "app-pass-123")
    msg = smtp.sent
    assert msg is not None
    assert msg.is_multipart()

    # collect the leaf parts by content type
    types = {p.get_content_type() for p in msg.walk()
             if p.get_content_maintype() != "multipart"}
    assert "text/plain" in types
    assert "text/html" in types

    images = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(images) == 1
    img = images[0]
    assert img["Content-ID"] == "<trend>"
    assert img.get_payload(decode=True) == png


def test_send_pdf_with_html_body_is_multipart_with_banner(tmp_path, monkeypatch):
    monkeypatch.setenv("YTM_TEST_SMTP_PW", "app-pass-123")
    _FakeSMTP.instances.clear()
    rcpts = mailer.send_pdf(
        _cfg(tmp_path), "s", "plain body", _pdf(tmp_path),
        html_body="<div>🔴 STALE DATA</div>", smtp_factory=_FakeSMTP,
    )
    assert rcpts == ["me@example.com", "you@example.com"]
    msg = _FakeSMTP.instances[0].sent
    types = {p.get_content_type() for p in msg.walk()
             if p.get_content_maintype() != "multipart"}
    assert "text/plain" in types
    assert "text/html" in types
    assert "application/pdf" in types


def test_send_pdf_password_file_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("YTM_TEST_SMTP_PW", raising=False)
    pwfile = tmp_path / "pw"
    pwfile.write_text("file-pass\n")
    cfg = EmailConfig(
        smtp_host="h", smtp_port=587, username="u", sender="u", recipients=["u"],
        password_env="YTM_TEST_SMTP_PW", password_file=pwfile,
    )
    _FakeSMTP.instances.clear()
    mailer.send_pdf(cfg, "s", "b", _pdf(tmp_path), smtp_factory=_FakeSMTP)
    assert _FakeSMTP.instances[0].logged_in == ("u", "file-pass")


def test_send_pdf_errors_without_password(tmp_path, monkeypatch):
    monkeypatch.delenv("YTM_TEST_SMTP_PW", raising=False)
    with pytest.raises(RuntimeError, match="no SMTP password"):
        mailer.send_pdf(_cfg(tmp_path), "s", "b", _pdf(tmp_path), smtp_factory=_FakeSMTP)
