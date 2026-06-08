"""Pre-modification daily snapshots + reversible restore.

Snapshots use SQLite's online backup API (WAL-consistent), one per Pacific day, reused by
later same-day runs. Restore is explicit and itself reversible (the current db is copied
to ``<db>.prerestore`` first).
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from ..timeutil import parse_date, today_pt

_PREFIX = "ytmetrics-"
_SUFFIX = ".db"


@dataclass
class BackupInfo:
    date: str
    path: Path
    size_bytes: int


def _snapshot_path(backup_dir: Path, day: str) -> Path:
    return backup_dir / f"{_PREFIX}{day}{_SUFFIX}"


def _online_backup(src_path: Path, dest_path: Path) -> None:
    src = sqlite3.connect(str(src_path))
    try:
        dest = sqlite3.connect(str(dest_path))
        try:
            with dest:
                src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()


def daily_snapshot(db_path: Path, backup_dir: Path, day: str | None = None) -> Path | None:
    """Ensure today's snapshot exists; return its path (None if the db doesn't exist yet)."""
    if not db_path.is_file():
        return None
    day = day or today_pt().isoformat()
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = _snapshot_path(backup_dir, day)
    if dest.is_file():
        return dest
    _online_backup(db_path, dest)
    return dest


def prune(backup_dir: Path, retention_days: int, *, today: str | None = None) -> list[Path]:
    if not backup_dir.is_dir():
        return []
    cutoff = (parse_date(today) if today else today_pt()) - timedelta(days=retention_days)
    removed: list[Path] = []
    for p in backup_dir.glob(f"{_PREFIX}*{_SUFFIX}"):
        stamp = p.name[len(_PREFIX) : -len(_SUFFIX)]
        try:
            d = parse_date(stamp)
        except ValueError:
            continue
        if d < cutoff:
            p.unlink()
            removed.append(p)
    return removed


def list_backups(backup_dir: Path) -> list[BackupInfo]:
    if not backup_dir.is_dir():
        return []
    out: list[BackupInfo] = []
    for p in sorted(backup_dir.glob(f"{_PREFIX}*{_SUFFIX}")):
        stamp = p.name[len(_PREFIX) : -len(_SUFFIX)]
        out.append(BackupInfo(date=stamp, path=p, size_bytes=p.stat().st_size))
    return out


def resolve_snapshot(
    backup_dir: Path, *, date: str | None = None, latest: bool = False, file: str | None = None
) -> Path:
    if file:
        p = Path(file)
        if not p.is_file():
            raise FileNotFoundError(f"snapshot not found: {p}")
        return p
    backups = list_backups(backup_dir)
    if not backups:
        raise FileNotFoundError(f"no snapshots in {backup_dir}")
    if latest:
        return backups[-1].path
    if date:
        for b in backups:
            if b.date == date:
                return b.path
        raise FileNotFoundError(f"no snapshot for {date} in {backup_dir}")
    raise ValueError("specify one of date / latest / file")


def restore(db_path: Path, snapshot_path: Path) -> Path:
    """Copy current db to ``<db>.prerestore`` then swap in the snapshot. Returns the backup."""
    prerestore = db_path.with_suffix(db_path.suffix + ".prerestore")
    if db_path.is_file():
        shutil.copy2(db_path, prerestore)
    # Drop stale WAL sidecars so the restored file is authoritative.
    for sidecar in (db_path.with_name(db_path.name + "-wal"),
                    db_path.with_name(db_path.name + "-shm")):
        if sidecar.exists():
            sidecar.unlink()
    shutil.copy2(snapshot_path, db_path)
    return prerestore
