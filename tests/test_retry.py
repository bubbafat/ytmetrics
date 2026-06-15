"""Backoff / retry behavior — the 'resilient against API blips' guarantee.

These lock down two things a maintainer needs to trust before changing the
pull pipeline: transient errors (429/5xx, network blips) are retried, and
permanent errors (4xx, programming bugs) fail fast instead of looping.
No real time passes — `sleep` is injected and recorded.
"""

from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

from ytmetrics.retry import is_retryable, with_retries


class _Resp:
    reason = "error"

    def __init__(self, status: int) -> None:
        self.status = status


def _http_error(status: int) -> HttpError:
    return HttpError(_Resp(status), b'{"error": {"message": "boom"}}')


# --- is_retryable -----------------------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_http_errors_are_retryable(status):
    assert is_retryable(_http_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_http_errors_are_not_retryable(status):
    # e.g. the expired-token 4xx must fail fast, not retry 5 times.
    assert is_retryable(_http_error(status)) is False


@pytest.mark.parametrize("exc", [ConnectionError(), TimeoutError(), OSError()])
def test_network_blips_are_retryable(exc):
    assert is_retryable(exc) is True


@pytest.mark.parametrize("exc", [ValueError(), KeyError(), RuntimeError()])
def test_programming_errors_are_not_retryable(exc):
    assert is_retryable(exc) is False


# --- with_retries -----------------------------------------------------------

def test_returns_immediately_on_success():
    calls = []
    sleeps: list[float] = []
    result = with_retries(
        lambda: calls.append(1) or "ok",
        sleep=sleeps.append,
    )
    assert result == "ok"
    assert len(calls) == 1
    assert sleeps == []  # no backoff on the happy path


def test_retries_then_succeeds():
    state = {"n": 0}
    sleeps: list[float] = []

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise _http_error(503)
        return "recovered"

    result = with_retries(flaky, base_delay=1.0, max_delay=30.0, sleep=sleeps.append)
    assert result == "recovered"
    assert state["n"] == 3
    assert len(sleeps) == 2  # slept before attempts 2 and 3


def test_gives_up_after_attempts_and_raises_last():
    sleeps: list[float] = []

    def always_503():
        raise _http_error(503)

    with pytest.raises(HttpError):
        with_retries(always_503, attempts=4, sleep=sleeps.append)
    # 4 attempts => slept 3 times between them.
    assert len(sleeps) == 3


def test_non_retryable_fails_fast_without_sleeping():
    calls = []
    sleeps: list[float] = []

    def boom():
        calls.append(1)
        raise _http_error(403)

    with pytest.raises(HttpError):
        with_retries(boom, attempts=5, sleep=sleeps.append)
    assert len(calls) == 1  # no retry loop
    assert sleeps == []


def test_backoff_is_bounded_by_max_delay():
    sleeps: list[float] = []

    def always_500():
        raise _http_error(500)

    with pytest.raises(HttpError):
        with_retries(
            always_500, attempts=8, base_delay=1.0, max_delay=5.0, sleep=sleeps.append
        )
    # delay = min(max_delay, base*2^n) + jitter up to delay/2, so never exceeds 1.5x cap.
    assert sleeps, "expected at least one backoff sleep"
    assert all(s <= 5.0 * 1.5 for s in sleeps)
