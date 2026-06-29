"""The weekly briefing generator produces a real multi-page PDF from a populated db."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("matplotlib")  # briefing is an optional extra

from ytmetrics import briefing, pipeline
from ytmetrics.sources.replay import ReplaySource
from ytmetrics.store.sqlite_store import SqliteStore

from .conftest import make_channel


def _populated_db(tmp_path: Path, fixtures_dir: Path) -> Path:
    ch = make_channel(tmp_path)  # include_revenue=True by default
    src = ReplaySource(fixtures_dir)
    db = tmp_path / "t.db"
    with SqliteStore(db) as store:
        pipeline.run_pull(store, src, [ch], date(2026, 6, 1), date(2026, 6, 3))
        pipeline.run_insights(store, src, [ch], date(2026, 6, 1), date(2026, 6, 3))
    return db


def test_briefing_generates_pdf(tmp_path, fixtures_dir):
    db = _populated_db(tmp_path, fixtures_dir)
    out = briefing.generate(db, tmp_path / "b.pdf", weeks=1)
    data = Path(out).read_bytes()
    assert data[:5] == b"%PDF-"          # a real PDF
    assert len(data) > 5000              # a multi-page deck, not an empty stub


def test_briefing_errors_without_data(tmp_path):
    with SqliteStore(tmp_path / "empty.db"):
        pass  # schema only, no rows
    with pytest.raises(RuntimeError, match="run `ytmetrics pull`"):
        briefing.generate(tmp_path / "empty.db", tmp_path / "x.pdf")
