"""The insights window resolver: explicit --window wins; otherwise a trailing --days
window (so a scheduled job can ask for a rolling window without hardcoding dates)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from ytmetrics.cli import _insights_window
from ytmetrics.timeutil import trailing_window


def test_explicit_window_wins():
    args = SimpleNamespace(window="2026-06-01:2026-06-30", days=90)
    assert _insights_window(args) == (date(2026, 6, 1), date(2026, 6, 30))


def test_trailing_days_when_no_window():
    args = SimpleNamespace(window=None, days=14)
    assert _insights_window(args) == trailing_window(14)


def test_bad_window_is_a_friendly_error():
    import pytest

    args = SimpleNamespace(window="not-a-range", days=90)
    with pytest.raises(SystemExit):
        _insights_window(args)
