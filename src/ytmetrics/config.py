"""Load and validate ``config.toml`` (stdlib tomllib).

Relative paths in the config are resolved against the config file's directory, so the
tool behaves the same no matter what working directory a scheduler launches it from.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

VALID_SECRET_BACKENDS = {"file", "keychain", "env"}


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class ChannelConfig:
    name: str
    channel_id: str  # a UC… id, or "mine" for the account's default channel
    client_secret: Path
    secret_backend: str
    token_file: Path
    include_revenue: bool
    track_revisions: bool

    @property
    def analytics_ids(self) -> str:
        """The ids= value for an Analytics query."""
        if self.channel_id.lower() == "mine":
            return "channel==MINE"
        return f"channel=={self.channel_id}"


@dataclass(frozen=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    username: str
    sender: str
    recipients: list[str]
    password_env: str       # env var holding the SMTP password / Gmail app password
    password_file: Path | None  # fallback file (gitignored) if the env var is unset


@dataclass(frozen=True)
class Config:
    base_dir: Path
    db_path: Path
    backup_before_pull: bool
    backup_dir: Path
    backup_retention_days: int
    max_api_calls_per_run: int
    log_dir: Path
    log_level: str
    heartbeat_seconds: int
    freshness_warn_days: int
    insights_retention_weeks: int
    on_failure: dict[str, str] | None
    email: EmailConfig | None = None
    channels: list[ChannelConfig] = field(default_factory=list)

    def channel(self, name: str) -> ChannelConfig:
        for ch in self.channels:
            if ch.name == name:
                return ch
        raise ConfigError(f"no channel named {name!r} in config")


def _resolve(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def load_config(path: str | Path) -> Config:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(
            f"config file not found: {config_path}\n"
            "Copy config.example.toml to config.toml and fill it in."
        )
    base = config_path.parent
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    raw_channels = raw.get("channels")
    if not raw_channels:
        raise ConfigError("config must define at least one [[channels]] entry")

    channels: list[ChannelConfig] = []
    seen_names: set[str] = set()
    for i, c in enumerate(raw_channels):
        name = c.get("name")
        if not name:
            raise ConfigError(f"channels[{i}] is missing 'name'")
        if name in seen_names:
            raise ConfigError(f"duplicate channel name {name!r}")
        seen_names.add(name)

        backend = c.get("secret_backend", "file")
        if backend not in VALID_SECRET_BACKENDS:
            raise ConfigError(
                f"channel {name!r}: invalid secret_backend {backend!r} "
                f"(expected one of {sorted(VALID_SECRET_BACKENDS)})"
            )
        if "channel_id" not in c:
            raise ConfigError(f"channel {name!r} is missing 'channel_id' (use 'mine' for default)")

        channels.append(
            ChannelConfig(
                name=name,
                channel_id=str(c["channel_id"]),
                client_secret=_resolve(base, c.get("client_secret", "secrets/client_secret.json")),
                secret_backend=backend,
                token_file=_resolve(base, c.get("token_file", f"secrets/token_{name}.json")),
                include_revenue=bool(c.get("include_revenue", False)),
                track_revisions=bool(c.get("track_revisions", True)),
            )
        )

    on_failure = raw.get("on_failure")
    if on_failure is not None:
        keys = {k for k in ("command", "webhook") if on_failure.get(k)}
        if len(keys) != 1:
            raise ConfigError("[on_failure] must set exactly one of 'command' or 'webhook'")

    email_cfg: EmailConfig | None = None
    em = raw.get("email")
    if em:
        if "username" not in em:
            raise ConfigError("[email] requires 'username'")
        to = em.get("to", em["username"])
        recipients = [to] if isinstance(to, str) else [str(x) for x in to]
        email_cfg = EmailConfig(
            smtp_host=str(em.get("smtp_host", "smtp.gmail.com")),
            smtp_port=int(em.get("smtp_port", 587)),
            username=str(em["username"]),
            sender=str(em.get("from", em["username"])),
            recipients=recipients,
            password_env=str(em.get("password_env", "YTMETRICS_SMTP_PASSWORD")),
            password_file=(_resolve(base, em["password_file"]) if em.get("password_file")
                           else base / "secrets" / "smtp_password"),
        )

    return Config(
        base_dir=base,
        db_path=_resolve(base, raw.get("db_path", "ytmetrics.db")),
        backup_before_pull=bool(raw.get("backup_before_pull", True)),
        backup_dir=_resolve(base, raw.get("backup_dir", "backups")),
        backup_retention_days=int(raw.get("backup_retention_days", 14)),
        max_api_calls_per_run=int(raw.get("max_api_calls_per_run", 2000)),
        log_dir=_resolve(base, raw.get("log_dir", "logs")),
        log_level=str(raw.get("log_level", "INFO")).upper(),
        heartbeat_seconds=int(raw.get("heartbeat_seconds", 3)),
        freshness_warn_days=int(raw.get("freshness_warn_days", 3)),
        insights_retention_weeks=int(raw.get("insights_retention_weeks", 26)),
        on_failure=on_failure,
        email=email_cfg,
        channels=channels,
    )
