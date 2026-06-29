"""ytmetrics command-line interface.

Commands: pull, insights, list-channels, doctor, info, referrers, backups, restore.
Only `list-channels` is interactive (it can open a browser to mint the first token);
everything else runs headless from the stored refresh token.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .logging_setup import get_logger, setup_logging
from .status import Heartbeat
from .timeutil import default_end_date, fmt_date, month_chunks, parse_date, trailing_window

DEFAULT_CONFIG = "config.toml"


# --------------------------------------------------------------------------- locking
class _RunLock:
    """Single-instance lock so a manual run and the scheduled run can't overlap."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> _RunLock:
        self.fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            raise SystemExit(
                f"another ytmetrics run holds the lock ({self.path}): {exc}"
            ) from exc
        return self

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)


# --------------------------------------------------------------------------- helpers
def _load(args) -> Config:
    try:
        return load_config(args.config)
    except ConfigError as exc:
        raise SystemExit(f"config error: {exc}") from exc


def _select_channels(config: Config, name: str | None):
    if name is None:
        return config.channels
    return [config.channel(name)]


def _window(args) -> tuple[date, date]:
    if args.start:
        start = parse_date(args.start)
        end = parse_date(args.end) if args.end else default_end_date()
        return start, end
    return trailing_window(args.days)


def _insights_window(args) -> tuple[date, date]:
    """An explicit ``--window START:END`` wins; otherwise a trailing ``--days`` window
    (so a scheduled job can ask for a rolling window without hardcoding dates)."""
    if getattr(args, "window", None):
        try:
            start_s, end_s = args.window.split(":", 1)
        except ValueError as exc:
            raise SystemExit(
                "--window must be START:END, e.g. 2026-05-01:2026-05-31"
            ) from exc
        return parse_date(start_s), parse_date(end_s)
    return trailing_window(args.days)


# --------------------------------------------------------------------------- commands
def cmd_pull(args) -> int:
    config = _load(args)
    setup_logging(config.log_dir, config.log_level, args.verbose)
    log = get_logger()
    channels = _select_channels(config, args.channel)
    start, end = _window(args)

    if args.dry_run:
        return _print_pull_plan(config, channels, start, end)

    # Build source
    from .sources.base import Source

    source: Source
    if args.source == "replay":
        from .sources.replay import ReplaySource

        source = ReplaySource(config.base_dir / "fixtures")
    else:
        from .sources.live import LiveSource

        source = LiveSource(config.max_api_calls_per_run, interactive=args.interactive, logger=log)

    from . import pipeline
    from .store import backup, notify
    from .store.sqlite_store import SqliteStore

    lock_path = config.db_path.with_suffix(config.db_path.suffix + ".lock")
    with _RunLock(lock_path):
        if config.backup_before_pull and args.source != "replay":
            snap = backup.daily_snapshot(config.db_path, config.backup_dir)
            if snap:
                log.info("pre-pull snapshot: %s", snap)
            backup.prune(config.backup_dir, config.backup_retention_days)

        with SqliteStore(config.db_path) as store:
            with Heartbeat(config.heartbeat_seconds, "starting pull") as hb:
                summary = pipeline.run_pull(store, source, channels, start, end,
                                            heartbeat=hb, logger=log)

    _print_pull_summary(summary, start, end)

    if summary.any_failed:
        failed = [c.name for c in summary.channels if not c.ok]
        msg = f"ytmetrics pull had failures: {failed} (window {start}..{end})"
        snap_hint = backup.list_backups(config.backup_dir)
        if snap_hint:
            msg += f". Roll back with `ytmetrics restore --latest` ({snap_hint[-1].path})."
        notify.notify_failure(config.on_failure, msg, log)
        print(msg, file=sys.stderr)
        return 1

    _warn_if_stale(config, summary)
    return 0


def cmd_list_channels(args) -> int:
    config = _load(args)
    setup_logging(config.log_dir, config.log_level, args.verbose)
    from .sources.live import LiveSource

    source = LiveSource(config.max_api_calls_per_run, interactive=True, logger=get_logger())
    seen: set[str] = set()
    for ch in config.channels:
        for owned in source.list_owned_channels(ch):
            if owned["channel_id"] in seen:
                continue
            seen.add(owned["channel_id"])
            print(f"{owned['channel_id']}\t{owned['title']}")
    if not seen:
        print("no channels found for the authorized account", file=sys.stderr)
    return 0


def cmd_doctor(args) -> int:
    config = _load(args)
    setup_logging(config.log_dir, config.log_level, args.verbose)
    from .store.doctor import FAIL, WARN, run_doctor

    results = run_doctor(config, live=args.live)
    symbol = {"ok": "✓", "warn": "⚠", "fail": "✗"}
    worst = 0
    for r in results:
        print(f"  {symbol.get(r.status, '?')} {r.name}: {r.detail}")
        if r.status == FAIL:
            worst = max(worst, 2)
        elif r.status == WARN:
            worst = max(worst, 1)
    return 2 if worst == 2 else 0


def cmd_info(args) -> int:
    config = _load(args)
    from .store.doctor import _freshness_checks
    from .store.sqlite_store import SqliteStore

    if not config.db_path.is_file():
        print(f"no database yet at {config.db_path}; run a pull first")
        return 0
    with SqliteStore(config.db_path) as store:
        print(f"db: {config.db_path}  (schema v{store.get_meta('schema_version')})")
        print("\nrow counts:")
        for name, count in store.table_counts().items():
            print(f"  {name:24} {count:>8}")
        print("\nchannels:")
        for r in store.channels():
            lo, hi = store.channel_date_coverage(r["channel_id"])
            print(f"  {r['channel_id']}  {r['title']}")
            print(f"      coverage {lo}..{hi}  last_pull={r['last_successful_pull']}")
    print()
    for chk in _freshness_checks(config):
        if chk.status != "ok":
            print(f"  ⚠ {chk.name}: {chk.detail}")
    return 0


def cmd_referrers(args) -> int:
    config = _load(args)
    setup_logging(config.log_dir, config.log_level, args.verbose)
    log = get_logger()
    channel = config.channel(args.channel) if args.channel else config.channels[0]
    try:
        start_s, end_s = args.window.split(":", 1)
        start, end = parse_date(start_s), parse_date(end_s)
    except ValueError as exc:
        raise SystemExit("--window must be START:END, e.g. 2026-05-01:2026-05-31") from exc

    from .sources.live import LiveSource
    from .store.sqlite_store import SqliteStore

    source = LiveSource(config.max_api_calls_per_run, interactive=args.interactive, logger=log)
    rows = source.fetch_referrers(channel, args.video, start, end)
    with SqliteStore(config.db_path) as store:
        store.begin()
        res = store.upsert("video_referrers", rows)
        store.commit()
    print(f"stored {res.inserted + res.updated} referrer rows for {args.video} "
          f"({fmt_date(start)}..{fmt_date(end)})")
    for r in rows:
        print(f"  {r['traffic_source_type']:16} {r.get('referrer_video_id')}  "
              f"views={r.get('views')}")
    return 0


def cmd_insights(args) -> int:
    config = _load(args)
    setup_logging(config.log_dir, config.log_level, args.verbose)
    log = get_logger()
    channels = _select_channels(config, args.channel)
    start, end = _insights_window(args)

    from . import pipeline
    from .sources.live import LiveSource
    from .store.sqlite_store import SqliteStore

    source = LiveSource(config.max_api_calls_per_run, interactive=args.interactive, logger=log)
    with SqliteStore(config.db_path) as store:
        with Heartbeat(config.heartbeat_seconds, "starting insights") as hb:
            summary = pipeline.run_insights(
                store, source, channels, start, end,
                include_demographics=not args.no_demographics, heartbeat=hb, logger=log,
            )
        pruned = store.prune_insight_snapshots(config.insights_retention_weeks)
        if pruned:
            total = sum(pruned.values())
            log.info("pruned %d old insight-snapshot rows (>%dw): %s",
                     total, config.insights_retention_weeks, pruned)

    print(f"\ninsights {fmt_date(start)}..{fmt_date(end)}  ({summary.api_calls} api calls)")
    for c in summary.channels:
        if not c.ok:
            print(f"  ✗ {c.name}: FAILED — {c.error}")
            continue
        rows = {t: u.inserted + u.updated for t, u in c.upserts.items()}
        deg = f"  degraded={c.degraded}" if c.degraded else ""
        print(f"  ✓ {c.name}: {rows}{deg}")
    return 1 if summary.any_failed else 0


def cmd_backups(args) -> int:
    config = _load(args)
    from .store import backup

    backups = backup.list_backups(config.backup_dir)
    if not backups:
        print(f"no snapshots in {config.backup_dir}")
        return 0
    for b in backups:
        print(f"  {b.date}  {b.size_bytes:>10} bytes  {b.path}")
    return 0


def cmd_restore(args) -> int:
    config = _load(args)
    from .store import backup

    try:
        snap = backup.resolve_snapshot(
            config.backup_dir, date=args.date, latest=args.latest, file=args.file
        )
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    pre = backup.restore(config.db_path, snap)
    print(f"restored {config.db_path} from {snap}\n  previous db saved to {pre}")
    return 0


# --------------------------------------------------------------------------- output
def _print_pull_plan(config: Config, channels, start: date, end: date) -> int:
    chunks = len(month_chunks(start, end))
    print(f"DRY RUN — window {start}..{end}")
    for ch in channels:
        est = 4 + (1 if ch.include_revenue else 0) + chunks  # rough analytics-call estimate
        print(f"  channel={ch.name} ({ch.channel_id})  ~{est} analytics calls "
              f"(video_daily over {chunks} month-chunk(s)), revenue={ch.include_revenue}")
    print("no data written.")
    return 0


def _print_pull_summary(summary, start: date, end: date) -> None:
    print(f"\npull {start}..{end}  ({summary.api_calls} api calls)")
    for c in summary.channels:
        if not c.ok:
            print(f"  ✗ {c.name}: FAILED — {c.error}")
            continue
        rows = {t: u.inserted + u.updated for t, u in c.upserts.items()}
        deg = f"  degraded={c.degraded}" if c.degraded else ""
        print(f"  ✓ {c.name}: {rows}  revisions={c.revisions}  through={c.data_through}{deg}")


def _warn_if_stale(config: Config, summary) -> None:
    from .timeutil import today_pt

    today = today_pt()
    for c in summary.channels:
        if c.ok and c.data_through:
            gap = (today - parse_date(c.data_through)).days
            if gap > config.freshness_warn_days:
                print(f"  ⚠ {c.name}: data_through={c.data_through} is {gap}d behind today",
                      file=sys.stderr)


# --------------------------------------------------------------------------- parser
def _add_common(parser: argparse.ArgumentParser, *, top: bool) -> None:
    """Add --config/--verbose so they work both before and after the subcommand.

    On the top parser they carry the real defaults; on each subparser they use SUPPRESS
    so an absent flag there doesn't clobber a value given before the subcommand.
    """
    config_default = DEFAULT_CONFIG if top else argparse.SUPPRESS
    verbose_default = False if top else argparse.SUPPRESS
    parser.add_argument("--config", default=config_default, help="path to config.toml")
    parser.add_argument("--verbose", action="store_true", default=verbose_default,
                        help="DEBUG logging to console + file")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ytmetrics", description=__doc__)
    _add_common(p, top=True)
    p.add_argument("--version", action="version", version=f"ytmetrics {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_sub(name: str, **kw) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, **kw)
        _add_common(sp, top=False)
        return sp

    pull = add_sub("pull", help="pull analytics into the SQLite store")
    pull.add_argument("--channel", help="only this channel (default: all configured)")
    pull.add_argument("--days", type=int, default=7, help="trailing window length (default 7)")
    pull.add_argument("--start", help="explicit start date YYYY-MM-DD (backfill)")
    pull.add_argument("--end", help="explicit end date YYYY-MM-DD (default today-2 PT)")
    pull.add_argument("--source", choices=["live", "replay"], default="live")
    pull.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    pull.add_argument("--interactive", action="store_true",
                      help="allow a browser auth prompt (live only)")
    pull.set_defaults(func=cmd_pull)

    lc = add_sub("list-channels", help="list channels owned by the authorized account")
    lc.set_defaults(func=cmd_list_channels)

    doc = add_sub("doctor", help="preflight checks")
    doc.add_argument("--live", action="store_true", help="also check auth + API reachability")
    doc.set_defaults(func=cmd_doctor)

    info = add_sub("info", help="row counts, coverage, and freshness")
    info.set_defaults(func=cmd_info)

    ref = add_sub("referrers", help="snapshot cross-video referral attribution")
    ref.add_argument("--video", required=True, help="destination video id")
    ref.add_argument("--window", required=True, help="START:END (YYYY-MM-DD:YYYY-MM-DD)")
    ref.add_argument("--channel", help="channel name (default: first configured)")
    ref.add_argument("--interactive", action="store_true")
    ref.set_defaults(func=cmd_referrers)

    ins = add_sub("insights", help="pull windowed audience/retention/search insights")
    ins.add_argument("--window", help="START:END (YYYY-MM-DD:YYYY-MM-DD); overrides --days")
    ins.add_argument("--days", type=int, default=90,
                     help="trailing-window size in days when --window is omitted (default 90)")
    ins.add_argument("--channel", help="only this channel (default: all configured)")
    ins.add_argument("--no-demographics", action="store_true",
                     help="skip the demographics report")
    ins.add_argument("--interactive", action="store_true")
    ins.set_defaults(func=cmd_insights)

    bk = add_sub("backups", help="list db snapshots")
    bk.set_defaults(func=cmd_backups)

    rs = add_sub("restore", help="restore the db from a snapshot (reversible)")
    g = rs.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="snapshot date YYYY-MM-DD")
    g.add_argument("--latest", action="store_true", help="most recent snapshot")
    g.add_argument("--file", help="explicit snapshot path")
    rs.set_defaults(func=cmd_restore)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
