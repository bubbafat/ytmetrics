from datetime import date

from ytmetrics.timeutil import month_chunks, parse_date, trailing_window


def test_trailing_window_inclusive():
    start, end = trailing_window(7, end=date(2026, 6, 7))
    assert start == date(2026, 6, 1)
    assert end == date(2026, 6, 7)


def test_month_chunks_spans_month_boundaries():
    chunks = month_chunks(date(2026, 1, 15), date(2026, 3, 10))
    assert chunks == [
        (date(2026, 1, 15), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 3, 10)),
    ]


def test_month_chunks_single_month():
    assert month_chunks(date(2026, 6, 1), date(2026, 6, 3)) == [
        (date(2026, 6, 1), date(2026, 6, 3))
    ]


def test_month_chunks_empty_when_reversed():
    assert month_chunks(date(2026, 6, 3), date(2026, 6, 1)) == []


def test_parse_date():
    assert parse_date("2026-06-01") == date(2026, 6, 1)
