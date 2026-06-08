from __future__ import annotations

from pathlib import Path

import pytest

from ytmetrics.config import ChannelConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


def make_channel(tmp_path: Path, *, name: str = "main", include_revenue: bool = True,
                 track_revisions: bool = True) -> ChannelConfig:
    return ChannelConfig(
        name=name,
        channel_id="mine",
        client_secret=tmp_path / "client_secret.json",
        secret_backend="file",
        token_file=tmp_path / "token.json",
        include_revenue=include_revenue,
        track_revisions=track_revisions,
    )
