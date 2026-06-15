"""Auth fallback behavior: a dead refresh token must not crash the pipeline.

A Google project still in "Testing" publishing status expires refresh tokens
after ~7 days, so `creds.refresh()` raises RefreshError. get_credentials must
fall through to a fresh interactive authorization (interactive) or raise a clear
AuthError (non-interactive) — never propagate the raw RefreshError traceback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError

import ytmetrics.auth as auth
from ytmetrics.config import ChannelConfig


def _channel(tmp_path: Path) -> ChannelConfig:
    return ChannelConfig(
        name="main",
        channel_id="mine",
        client_secret=tmp_path / "client_secret.json",
        secret_backend="file",
        token_file=tmp_path / "token.json",
        include_revenue=True,
        track_revisions=True,
    )


class _DeadCreds:
    """Stored creds whose refresh fails (expired/revoked refresh token)."""

    valid = False
    expired = True
    refresh_token = "1//dead-refresh-token"
    token = None

    def refresh(self, request):  # noqa: ANN001
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    def to_json(self) -> str:
        return json.dumps({"refresh_token": self.refresh_token})


class _FreshCreds:
    valid = True
    expired = False
    refresh_token = "1//fresh-refresh-token"
    token = None

    def to_json(self) -> str:
        return json.dumps({"refresh_token": self.refresh_token})


class _Flow:
    def __init__(self) -> None:
        self.ran = False

    def run_local_server(self, port: int = 0) -> _FreshCreds:
        self.ran = True
        return _FreshCreds()


def _stub_dead_token(monkeypatch) -> None:
    monkeypatch.setattr(
        auth.Credentials,
        "from_authorized_user_info",
        classmethod(lambda cls, info, scopes: _DeadCreds()),
    )


def test_refresh_failure_falls_through_to_browser(tmp_path, monkeypatch):
    ch = _channel(tmp_path)
    ch.client_secret.write_text("{}")
    ch.token_file.write_text(json.dumps({"refresh_token": "1//dead-refresh-token"}))
    _stub_dead_token(monkeypatch)

    flow = _Flow()
    monkeypatch.setattr(
        auth.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(lambda cls, path, scopes: flow),
    )

    creds = auth.get_credentials(ch, interactive=True)

    assert isinstance(creds, _FreshCreds)
    assert flow.ran, "expected fall-through to the interactive browser flow"
    # The freshly minted token must be persisted over the dead one.
    assert "fresh-refresh-token" in ch.token_file.read_text()


def test_refresh_failure_noninteractive_raises_autherror(tmp_path, monkeypatch):
    ch = _channel(tmp_path)
    ch.token_file.write_text(json.dumps({"refresh_token": "1//dead-refresh-token"}))
    _stub_dead_token(monkeypatch)

    def _should_not_run(cls, path, scopes):  # noqa: ANN001
        raise AssertionError("non-interactive must not open a browser flow")

    monkeypatch.setattr(
        auth.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(_should_not_run),
    )

    with pytest.raises(auth.AuthError) as excinfo:
        auth.get_credentials(ch, interactive=False)

    # A clear, actionable error — not a raw RefreshError traceback.
    assert "could not be refreshed" in str(excinfo.value)
