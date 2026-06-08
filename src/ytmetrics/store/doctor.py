"""Preflight checks: config / secrets / db / clock (always) and auth+API (when --live).

Each check returns a status so the CLI can print a precise fix. Offline checks need no
network or credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..config import Config
from ..timeutil import PACIFIC, parse_date, today_pt

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str


def _writable_dir(path) -> bool:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    return os.access(parent, os.W_OK)


def run_doctor(config: Config, *, live: bool = False) -> list[CheckResult]:
    results: list[CheckResult] = []

    results.append(CheckResult("config", OK, f"{len(config.channels)} channel(s) configured"))

    # Clock / timezone
    try:
        now = today_pt()
        if now.year < 2000:
            results.append(CheckResult("clock", FAIL, f"system clock looks wrong: {now}"))
        else:
            results.append(
                CheckResult("timezone", OK, f"Pacific time OK ({PACIFIC.key}, today {now})")
            )
    except Exception as exc:  # pragma: no cover
        results.append(CheckResult("timezone", FAIL, f"zoneinfo unavailable: {exc}"))

    # DB writability
    if _writable_dir(config.db_path):
        results.append(CheckResult("db", OK, f"writable: {config.db_path}"))
    else:
        results.append(CheckResult("db", FAIL, f"not writable: {config.db_path.parent}"))

    # Per-channel secrets (offline view)
    for ch in config.channels:
        if ch.secret_backend == "file" and not ch.client_secret.is_file():
            results.append(CheckResult(
                f"client_secret[{ch.name}]", FAIL,
                f"missing {ch.client_secret} — download a Desktop OAuth client",
            ))
        else:
            results.append(CheckResult(f"client_secret[{ch.name}]", OK, "present"))

        from ..auth import secret_store_for

        try:
            token = secret_store_for(ch).load()
            if token:
                results.append(
                    CheckResult(f"token[{ch.name}]", OK, f"stored ({ch.secret_backend})")
                )
            else:
                results.append(CheckResult(
                    f"token[{ch.name}]", WARN,
                    "no stored token — run `ytmetrics list-channels` to authorize",
                ))
        except Exception as exc:
            results.append(CheckResult(f"token[{ch.name}]", FAIL, str(exc)))

    # Freshness (needs an existing db)
    results.extend(_freshness_checks(config))

    if live:
        results.extend(_live_checks(config))

    return results


def _freshness_checks(config: Config) -> list[CheckResult]:
    if not config.db_path.is_file():
        return [CheckResult("freshness", WARN, "no database yet — run a pull")]
    from .sqlite_store import SqliteStore

    out: list[CheckResult] = []
    today = today_pt()
    with SqliteStore(config.db_path) as store:
        rows = store.channels()
    if not rows:
        return [CheckResult("freshness", WARN, "database has no channel data yet")]
    for r in rows:
        through = r["data_through"]
        if not through:
            out.append(CheckResult(f"freshness[{r['channel_id']}]", WARN, "no data_through"))
            continue
        gap = (today - parse_date(through)).days
        if gap > config.freshness_warn_days:
            out.append(CheckResult(
                f"freshness[{r['channel_id']}]", WARN,
                f"stale: data_through={through} ({gap}d behind today)",
            ))
        else:
            out.append(CheckResult(
                f"freshness[{r['channel_id']}]", OK, f"data_through={through} ({gap}d behind)"
            ))
    return out


def _live_checks(config: Config) -> list[CheckResult]:
    from ..sources.live import LiveSource

    out: list[CheckResult] = []
    src = LiveSource(config.max_api_calls_per_run, interactive=False)
    for ch in config.channels:
        try:
            info = src.resolve_channel(ch)
            scopes = "analytics+youtube" + (",monetary" if ch.include_revenue else "")
            out.append(CheckResult(
                f"api[{ch.name}]", OK,
                f"resolved {info['channel_id']} ({info.get('title')}); scopes {scopes}",
            ))
        except Exception as exc:
            out.append(CheckResult(f"api[{ch.name}]", FAIL, str(exc)))
    return out
