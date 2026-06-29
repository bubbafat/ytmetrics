"""Send the briefing PDF over SMTP (e.g. Gmail with an app password).

The password is never stored in config: it comes from an environment variable, or a
gitignored fallback file (``secrets/smtp_password``). It's registered with the logging
redaction filter as soon as it's read.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Callable

from .config import EmailConfig
from .logging_setup import register_secret


def _password(cfg: EmailConfig) -> str:
    pw = os.environ.get(cfg.password_env)
    if not pw and cfg.password_file and Path(cfg.password_file).is_file():
        pw = Path(cfg.password_file).read_text().strip()
    if not pw:
        raise RuntimeError(
            f"no SMTP password — set ${cfg.password_env} or write it to {cfg.password_file}"
        )
    register_secret(pw)
    return pw


def build_message(cfg: EmailConfig, subject: str, body: str, pdf_path: str | Path,
                  *, html_body: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    if html_body:
        # text + HTML alternative; the PDF attach below wraps both in multipart/mixed.
        msg.add_alternative(html_body, subtype="html")
    p = Path(pdf_path)
    msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    return msg


def _send(cfg: EmailConfig, msg: EmailMessage,
          smtp_factory: Callable[..., smtplib.SMTP]) -> list[str]:
    """Open SMTP (starttls/login), send ``msg``, return the recipients. Shared by both
    senders. ``smtp_factory`` is injectable so the send path is unit-testable offline."""
    pw = _password(cfg)
    with smtp_factory(cfg.smtp_host, cfg.smtp_port) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(cfg.username, pw)
        s.send_message(msg)
    return list(cfg.recipients)


def send_pdf(
    cfg: EmailConfig,
    subject: str,
    body: str,
    pdf_path: str | Path,
    *,
    html_body: str | None = None,
    smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
) -> list[str]:
    """Send ``pdf_path`` as an attachment to ``cfg.recipients``. When ``html_body`` is given
    the email carries a text+HTML alternative (so the body can show e.g. a stale-data
    banner) alongside the PDF attachment. Returns the recipients."""
    msg = build_message(cfg, subject, body, pdf_path, html_body=html_body)
    return _send(cfg, msg, smtp_factory)


def build_text_message(cfg: EmailConfig, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def send_text(
    cfg: EmailConfig,
    subject: str,
    body: str,
    *,
    smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
) -> list[str]:
    """Send a plain-text email (no attachment) to ``cfg.recipients``. Returns the recipients."""
    msg = build_text_message(cfg, subject, body)
    return _send(cfg, msg, smtp_factory)


def build_html_message(
    cfg: EmailConfig,
    subject: str,
    text_body: str,
    html_body: str,
    *,
    images: dict[str, bytes] | None = None,
) -> EmailMessage:
    """A ``multipart/alternative`` message: plain text first, then HTML. When ``images``
    is given, the HTML part is wrapped in a ``multipart/related`` so each image is
    attached inline with its ``Content-ID`` (``<cid>``)."""
    msg = EmailMessage()
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg["Subject"] = subject
    # Plain-text first (the fallback), then HTML — that's `multipart/alternative` order.
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if images:
        # The HTML alternative is the last payload; attach inline images *to it* so they
        # form a `multipart/related` and resolve the `cid:` references.
        html_part = msg.get_payload()[-1]
        for cid, data in images.items():
            html_part.add_related(
                data, maintype="image", subtype="png",
                cid=f"<{cid}>", disposition="inline",
            )
    return msg


def send_html(
    cfg: EmailConfig,
    subject: str,
    text_body: str,
    html_body: str,
    *,
    images: dict[str, bytes] | None = None,
    smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
) -> list[str]:
    """Send a multipart text+HTML email (with optional inline images) to ``cfg.recipients``.
    Returns the recipients."""
    msg = build_html_message(cfg, subject, text_body, html_body, images=images)
    return _send(cfg, msg, smtp_factory)
