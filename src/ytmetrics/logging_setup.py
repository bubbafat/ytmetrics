"""Logging setup with a secret-redaction filter.

Two layers of defense so secrets never reach a log file:
1. We never deliberately log credential objects (the code passes only metadata).
2. Every handler runs a redaction filter that scrubs known secret values plus anything
   matching OAuth-token / Authorization / JSON-secret patterns — even at DEBUG.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_REDACTED = "***REDACTED***"

# Patterns that look like secrets regardless of whether we registered them.
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ya29\.[A-Za-z0-9_\-]+"),            # OAuth access tokens
    re.compile(r"1//[A-Za-z0-9_\-]+"),               # OAuth refresh tokens
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),    # Authorization: Bearer …
    # JSON-ish "secret_key": "value"
    re.compile(
        r'(?i)"(access_token|refresh_token|client_secret|token|private_key)"\s*:\s*"[^"]*"'
    ),
]

# Exact secret strings registered at runtime (e.g. the loaded refresh token).
_known_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register an exact secret value to be scrubbed from all future log records."""
    if value and len(value) >= 6:
        _known_secrets.add(value)


def redact(text: str) -> str:
    for secret in _known_secrets:
        if secret in text:
            text = text.replace(secret, _REDACTED)
    for pat in _PATTERNS:
        text = pat.sub(_REDACTED, text)
    return text


class RedactionFilter(logging.Filter):
    """Scrubs secrets from a record by materializing and rewriting its message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        return True


def setup_logging(log_dir: Path, level: str = "INFO", verbose: bool = False) -> logging.Logger:
    """Configure the ``ytmetrics`` logger with rotating-file + console handlers.

    The console handler stays quiet (WARNING+) so normal runs aren't noisy; the heartbeat
    and command output are printed separately. The file captures everything at ``level``.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    effective = "DEBUG" if verbose else level.upper()

    logger = logging.getLogger("ytmetrics")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    redaction = RedactionFilter()
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_dir / "ytmetrics.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(getattr(logging, effective, logging.INFO))
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redaction)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    console.addFilter(redaction)
    logger.addHandler(console)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("ytmetrics")
