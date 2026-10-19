"""Stamp names: YYYYMMDD-HHMMSS-mmm, with -N appended when that name is taken.

A stamp is reserved by creating ``<dest>/<stamp>.reserve`` exclusively (O_EXCL), so
two captures in the same millisecond, even from different processes and even a
perceive and a ``windows`` map, never share a stamp. The final ``<stamp><suffix>`` only appears once it is complete:
``commit`` writes ``<final>.tmp`` and renames it into place with ``os.replace``.
Readers globbing ``*.json`` therefore never see an empty or half-written stamp.
Call ``release`` if the capture fails before ``commit``.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime
from pathlib import Path

STAMP_RE = re.compile(r"^(?P<sec>\d{8}-\d{6})(?:-(?P<ms>\d{3}))?(?:-(?P<n>\d+))?$")
RESERVE_SUFFIX = ".reserve"
TMP_SUFFIX = ".tmp"
_MAX_TRIES = 1000


def base_stamp(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond // 1000:03d}"


def reserve_path(final: Path) -> Path:
    final = Path(final)
    return final.with_name(final.name.split(".", 1)[0] + RESERVE_SUFFIX)


def _taken(dest: Path, stamp: str) -> bool:
    return any(dest.glob(f"{stamp}.*"))


def tmp_path(final: Path) -> Path:
    final = Path(final)
    return final.with_name(final.name + TMP_SUFFIX)


def allocate(dest: Path, suffix: str = ".json", now: datetime | None = None) -> tuple[str, Path]:
    """Reserve a unique stamp in ``dest``. Returns (stamp, final_path).

    ``final_path`` does not exist yet; write it with ``commit``.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    base = base_stamp(now)
    for n in range(1, _MAX_TRIES + 1):
        stamp = base if n == 1 else f"{base}-{n}"
        final = dest / f"{stamp}{suffix}"
        if _taken(dest, stamp):
            continue
        try:
            fd = os.open(reserve_path(final), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        if final.exists():
            _unlink(reserve_path(final))
            continue
        return stamp, final
    raise RuntimeError(f"could not allocate a unique stamp in {dest} after {_MAX_TRIES} tries")


def commit(final: Path, text: str) -> Path:
    """Atomically publish ``text`` at ``final`` and drop the reservation."""
    final = Path(final)
    tmp = tmp_path(final)
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
    except BaseException:
        _unlink(tmp)
        raise
    _unlink(reserve_path(final))
    return final


def release(final: Path) -> None:
    """Drop a reservation that was never committed."""
    final = Path(final)
    _unlink(reserve_path(final))
    _unlink(tmp_path(final))


LATEST_DIR = ".latest"


def _latest_pointer(dest: Path, hwnd: int) -> Path:
    return Path(dest) / LATEST_DIR / f"hwnd-{int(hwnd)}"


def note_latest(dest: Path, hwnd: int, final: Path) -> None:
    """Remember ``final`` as the newest stamp of ``hwnd`` in ``dest`` (best effort)."""
    try:
        ptr = _latest_pointer(dest, hwnd)
    except (TypeError, ValueError):
        return
    tmp = ptr.with_name(f"{ptr.name}.{os.getpid()}.{threading.get_ident()}{TMP_SUFFIX}")
    try:
        ptr.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(Path(final).name, encoding="utf-8")
        os.replace(tmp, ptr)
    except OSError:
        _unlink(tmp)


def read_latest(dest: Path, hwnd: int) -> Path | None:
    """The stamp json note_latest last recorded for ``hwnd``, if it still exists."""
    try:
        name = _latest_pointer(dest, hwnd).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or "/" in name or "\\" in name:
        return None
    path = Path(dest) / name
    return path if path.is_file() else None


def _unlink(p: Path) -> None:
    try:
        Path(p).unlink()
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
