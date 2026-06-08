"""Exponential backoff for transient API errors (HTTP 429 / 5xx and network blips)."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

from googleapiclient.errors import HttpError

T = TypeVar("T")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRYABLE_NETWORK = (ConnectionError, TimeoutError, OSError)


def _status_of(exc: BaseException) -> int | None:
    if isinstance(exc, HttpError):
        status = getattr(getattr(exc, "resp", None), "status", None)
        try:
            return int(status) if status is not None else None
        except (TypeError, ValueError):
            return None
    return None


def is_retryable(exc: BaseException) -> bool:
    status = _status_of(exc)
    if status is not None:
        return status in _RETRYABLE_STATUS
    return isinstance(exc, _RETRYABLE_NETWORK)


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    label: str = "api call",
    logger: logging.Logger | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` with exponential backoff + jitter on retryable errors."""
    log = logger or logging.getLogger("ytmetrics")
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - decide via is_retryable
            last = exc
            if attempt >= attempts or not is_retryable(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay / 2)
            log.warning(
                "retry %d/%d for %s after error: %s (sleep %.1fs)",
                attempt, attempts, label, exc, delay,
            )
            sleep(delay)
    assert last is not None
    raise last
