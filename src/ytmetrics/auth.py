"""OAuth (installed-app flow) + pluggable secret backends for the refresh token.

Scopes are read-only; the refresh token is the sensitive secret and is registered with
the logging redaction filter as soon as it's loaded. The Sheets scope is intentionally
NOT requested here — it only appears if/when an export command is added.
"""

from __future__ import annotations

import json
import os
import stat
from abc import ABC, abstractmethod
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import ChannelConfig
from .logging_setup import get_logger, register_secret

SCOPE_ANALYTICS_RO = "https://www.googleapis.com/auth/yt-analytics.readonly"
SCOPE_YOUTUBE_RO = "https://www.googleapis.com/auth/youtube.readonly"
SCOPE_MONETARY_RO = "https://www.googleapis.com/auth/yt-analytics-monetary.readonly"


class AuthError(RuntimeError):
    pass


def scopes_for(include_revenue: bool) -> list[str]:
    scopes = [SCOPE_ANALYTICS_RO, SCOPE_YOUTUBE_RO]
    if include_revenue:
        scopes.append(SCOPE_MONETARY_RO)
    return scopes


def _env_var_name(channel_name: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in channel_name).upper()
    return f"YTMETRICS_TOKEN_{safe}"


class SecretStore(ABC):
    @abstractmethod
    def load(self) -> str | None: ...

    @abstractmethod
    def save(self, token_json: str) -> None: ...


class FileSecretStore(SecretStore):
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> str | None:
        if not self.path.is_file():
            return None
        return self.path.read_text(encoding="utf-8")

    def save(self, token_json: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, stat.S_IRWXU)  # 0700
        except OSError:
            pass
        # Write with restrictive perms from the start.
        fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token_json)
        os.chmod(self.path, 0o600)


class KeychainSecretStore(SecretStore):
    SERVICE = "ytmetrics"

    def __init__(self, account: str):
        self.account = account
        try:
            import keyring  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise AuthError(
                "secret_backend='keychain' requires the 'keychain' extra: "
                "uv sync --extra keychain"
            ) from exc

    def load(self) -> str | None:
        import keyring

        return keyring.get_password(self.SERVICE, self.account)

    def save(self, token_json: str) -> None:
        import keyring

        keyring.set_password(self.SERVICE, self.account, token_json)


class EnvSecretStore(SecretStore):
    """Read-only token from an env var (for headless/cloud deploys)."""

    def __init__(self, var: str):
        self.var = var

    def load(self) -> str | None:
        return os.environ.get(self.var)

    def save(self, token_json: str) -> None:
        get_logger().debug("env secret backend is read-only; not persisting token (%s)", self.var)


def secret_store_for(channel: ChannelConfig) -> SecretStore:
    if channel.secret_backend == "file":
        return FileSecretStore(channel.token_file)
    if channel.secret_backend == "keychain":
        return KeychainSecretStore(account=channel.name)
    if channel.secret_backend == "env":
        return EnvSecretStore(_env_var_name(channel.name))
    raise AuthError(f"unknown secret_backend {channel.secret_backend!r}")


def _register_token_secret(creds: Credentials) -> None:
    register_secret(getattr(creds, "refresh_token", None))
    register_secret(getattr(creds, "token", None))


def get_credentials(
    channel: ChannelConfig, *, interactive: bool = True
) -> Credentials:
    """Load (and refresh) stored credentials, or run the browser flow on first use."""
    scopes = scopes_for(channel.include_revenue)
    store = secret_store_for(channel)

    raw = store.load()
    creds: Credentials | None = None
    if raw:
        creds = Credentials.from_authorized_user_info(json.loads(raw), scopes)
        _register_token_secret(creds)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _register_token_secret(creds)
            store.save(creds.to_json())
            return creds
        except RefreshError as exc:
            # Refresh token expired or revoked (e.g. Google project still in
            # "Testing" expires refresh tokens after 7 days). Fall through to a
            # fresh interactive authorization instead of crashing.
            if not interactive:
                raise AuthError(
                    f"stored credentials for channel {channel.name!r} could not be "
                    f"refreshed ({exc}). Run `ytmetrics list-channels` to re-authorize."
                ) from exc
            get_logger().warning(
                "refresh failed for channel %r (%s); re-authorizing in browser",
                channel.name,
                exc,
            )
            creds = None

    if not interactive:
        raise AuthError(
            f"no valid credentials for channel {channel.name!r} and not interactive. "
            f"Run `ytmetrics list-channels` once to authorize."
        )

    if not channel.client_secret.is_file():
        raise AuthError(
            f"client secret not found: {channel.client_secret}. Download an OAuth "
            f"'Desktop app' client from Google Cloud and point client_secret at it."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(channel.client_secret), scopes)
    creds = flow.run_local_server(port=0)
    _register_token_secret(creds)
    store.save(creds.to_json())
    return creds
