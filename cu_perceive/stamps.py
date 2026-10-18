"""Stamp names: YYYYMMDD-HHMMSS-mmm, with -N appended when that name is taken.

A stamp is reserved by creating ``<dest>/<stamp><suffix>`` exclusively (O_EXCL), so
two perceives in the same millisecond, even from different processes, never share
files. The caller overwrites the empty reservation with the real JSON, or calls
``release`` if it fails first.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

STAMP_RE = re.compile(r"^(?P<sec>\d{8}-\d{6})(?:-(?P<ms>\d{3}))?(?:-(?P<n>\d+))?$")
_MAX_TRIES = 1000


def base_stamp(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"


def allocate(dest: Path, suffix: str = ".json", now: datetime | None = None) -> tuple[str, Path]:
    """Reserve a unique stamp in ``dest``. Returns (stamp, reserved_path)."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    base = base_stamp(now)
    for n in range(1, _MAX_TRIES + 1):
        stamp = base if n == 1 else f"{base}-{n}"
        path = dest / f"{stamp}{suffix}"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return stamp, path
    raise RuntimeError(f"could not allocate a unique stamp in {dest} after {_MAX_TRIES} tries")


def release(path: Path) -> None:
    """Drop a reservation that was never filled (empty file only)."""
    try:
        p = Path(path)
        if p.is_file() and p.stat().st_size == 0:
            p.unlink()
    except OSError:
        pass


def parse_time(stamp: str) -> datetime | None:
    """Wall-clock time encoded in a stamp (old second-resolution names too)."""
    m = STAMP_RE.match(str(stamp or "").strip())
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group("sec"), "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    if m.group("ms"):
        dt = dt.replace(microsecond=int(m.group("ms")) * 1000)
    return dt
