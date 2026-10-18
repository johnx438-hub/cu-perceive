"""Window list/find, identity and hwnd capture. Win32 helpers come from vendored Enikk."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _window_service():
    from ._vendor.enikk.window import WindowService

    return WindowService()


_SKIP_CLASS = frozenset({
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "TopLevelWindowForOverflowX",
    "NotifyIconOverflowWindow",
})
_SKIP_TITLE = (
    "nvidia geforce overlay",
    "windows input experience",
)


def list_windows(include_minimized: bool = True) -> list[dict]:
    import psutil
    import win32gui
    import win32process

    rows: list[dict] = []

    def callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            cls = win32gui.GetClassName(hwnd)
            if cls in _SKIP_CLASS:
                return True
            if any(p in title.lower() for p in _SKIP_TITLE):
                return True
            iconic = bool(win32gui.IsIconic(hwnd))
            if iconic and not include_minimized:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            w, h = right - left, bottom - top
            if not iconic and (w < 80 or h < 80):
                return True
            try:
                proc = psutil.Process(pid)
                exe = proc.name()
                exe_path = proc.exe()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                exe, exe_path = "", ""
            rows.append({
                "hwnd": int(hwnd),
                "title": title,
                "pid": int(pid),
                "ctime": _ctime_str(int(pid)),
                "exe": exe,
                "exe_path": exe_path,
                "class": cls,
                "rect": [left, top, right, bottom],
                "minimized": iconic,
            })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(callback, None)
    return rows


PICK_RULE = (
    "When title/exe match multiple windows: prefer non-minimized, then EnumWindows "
    "z-order (topmost first), then larger area. Not guaranteed to be GetForegroundWindow. "
    "Pass hwnd to pin one. Callers should use the windows[] array when several match."
)


def _match_windows(title: str = "", exe: str = "") -> list[dict]:
    """All list_windows rows matching title/exe (AND if both set). Preserves EnumWindows order."""
    title_l = (title or "").lower()
    exe_l = (exe or "").lower()
    if not title_l and not exe_l:
        return []
    matches: list[dict] = []
    for w in list_windows(include_minimized=True):
        if title_l and title_l not in (w.get("title") or "").lower():
            continue
        exe_hay = ((w.get("exe") or "") + " " + (w.get("exe_path") or "")).lower()
        if exe_l and exe_l not in exe_hay:
            continue
        matches.append(w)
    return matches


def _score_match(w: dict, title_l: str, exe_l: str, z_index: int) -> tuple:
    """Higher is better. z_index: 0 = topmost in EnumWindows among matches."""
    t = (w.get("title") or "").lower()
    exact = 1 if title_l and t == title_l else 0
    title_hit = 1 if title_l and title_l in t else 0
    exe_in_title = 1 if exe_l and exe_l in t else 0
    left, top, right, bottom = w["rect"]
    area = max(0, right - left) * max(0, bottom - top)
    if w.get("minimized"):
        area = 0
    return (exact, title_hit, exe_in_title, 0 if w.get("minimized") else 1, -int(z_index), area)


def find_windows(title: str = "", exe: str = "", hwnd: int | None = None) -> list[dict]:
    """All matches for title/exe, sorted best-first by PICK_RULE. hwnd => 0 or 1 row."""
    if hwnd is not None:
        one = find_window(hwnd=hwnd)
        return [one] if one else []
    title_l = (title or "").lower()
    exe_l = (exe or "").lower()
    matches = _match_windows(title=title, exe=exe)
    ranked = sorted(
        enumerate(matches),
        key=lambda pair: _score_match(pair[1], title_l, exe_l, pair[0]),
        reverse=True,
    )
    return [w for _, w in ranked]


def find_window(title: str = "", exe: str = "", hwnd: int | None = None) -> dict | None:
    """Find one window. hwnd wins. title and exe are AND if both set.

    Multi-match pick rule (see PICK_RULE): non-minimized, then EnumWindows z-top,
    then larger area — NOT foreground-window guaranteed. Prefer find_windows() /
    windows[] when several share an exe (browser).
    """
    wins = list_windows(include_minimized=True)
    if hwnd is not None:
        hwnd = int(hwnd)
        for w in wins:
            if int(w["hwnd"]) == hwnd:
                return w
        if _window_service().is_valid(hwnd):
            from . import _win32
            pid = _win32.window_pid(hwnd)
            return {
                "hwnd": hwnd, "title": "", "exe": "", "pid": pid,
                "ctime": _ctime_str(pid),
                "exe_path": "", "rect": [0, 0, 0, 0], "minimized": False,
                "minimized_unknown": True,
            }
        return None

    ranked = find_windows(title=title, exe=exe)
    return ranked[0] if ranked else None


def _ctime_str(pid: int) -> str | None:
    """Process creation FILETIME as a decimal string (exceeds JS safe-int range)."""
    if not pid:
        return None
    try:
        from . import _win32
        ct = _win32.process_creation_time(int(pid))
    except Exception:
        return None
    return str(ct) if ct else None


def window_identity(hwnd: int) -> dict:
    """{hwnd, pid, ctime} for a live hwnd. pid/ctime None when they can't be read."""
    from . import _win32

    hwnd = int(hwnd)
    pid = _win32.window_pid(hwnd)
    return {"hwnd": hwnd, "pid": pid or None, "ctime": _ctime_str(pid)}


def verify_window_identity(hwnd, pid, ctime) -> str | None:
    """Fail-closed HWND + PID + process-creation-time check. None = same window; else why not.

    Catches a closed window whose HWND was reused by another process, and a PID
    reused by a restarted process.
    """
    from . import _win32

    try:
        hwnd_i = int(hwnd)
    except (TypeError, ValueError):
        return f"bad hwnd {hwnd!r}; re-perceive"
    try:
        pid_i = int(pid) if pid not in (None, "") else 0
        ctime_i = int(ctime) if ctime not in (None, "") else 0
    except (TypeError, ValueError):
        return f"bad stamp identity pid={pid!r} ctime={ctime!r}; re-perceive"
    if pid_i <= 0 or ctime_i <= 0:
        return "stamp has no process identity (pid + creation time); re-perceive this window"
    try:
        if not _win32.is_window(hwnd_i):
            return f"hwnd {hwnd_i} no longer exists; re-perceive"
        live_pid = _win32.window_pid(hwnd_i)
        if live_pid != pid_i:
            return (
                f"hwnd {hwnd_i} now belongs to pid {live_pid}, stamp was pid {pid_i} "
                "(HWND reused by another process); re-perceive"
            )
        live_ct = _win32.process_creation_time(live_pid)
    except Exception as e:
        return f"identity check failed: {e}; re-perceive"
    if not live_ct:
        return f"cannot read creation time of pid {pid_i}; identity unverified (fail closed) — re-perceive"
    if int(live_ct) != ctime_i:
        return (
            f"pid {pid_i} is a different process than at perceive time "
            "(creation time changed; PID reused); re-perceive"
        )
    return None



# GetWindowPlacement showCmd values that mean iconic (win32con).
_SW_SHOWMINIMIZED = 2
_SW_MINIMIZE = 6
_SW_SHOWMINNOACTIVE = 7
_SW_FORCEMINIMIZE = 11
_MINIMIZED_SHOW_CMDS = frozenset({
    _SW_SHOWMINIMIZED,
    _SW_MINIMIZE,
    _SW_SHOWMINNOACTIVE,
    _SW_FORCEMINIMIZE,
})


def is_hwnd_minimized(hwnd: int) -> bool:
    """Live iconic state. Uses IsIconic + showCmd; never rcNormalPosition / restore rect."""
    import win32gui

    hwnd = int(hwnd)
    if bool(win32gui.IsIconic(hwnd)):
        return True
    try:
        placement = win32gui.GetWindowPlacement(hwnd)
    except Exception:
        return False
    # pywin32: (flags, showCmd, ptMin, ptMax, rcNormal)
    try:
        show_cmd = int(placement[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return False
    return show_cmd in _MINIMIZED_SHOW_CMDS


def is_sentinel_origin(origin: dict | None) -> bool:
    """Minimized GetWindowRect is left/top -32000 and a stub size (~144x20)."""
    if not origin:
        return True
    try:
        left = int(origin.get('left', 0))
        top = int(origin.get('top', 0))
        w = int(origin.get('width', 0))
        h = int(origin.get('height', 0))
    except (TypeError, ValueError):
        return True
    if left <= -10000 or top <= -10000:
        return True
    if w < 80 or h < 80:
        return True
    return False


RESTORE_SETTLE_S = 0.30


def desktop_capture_sizes() -> set[tuple[int, int]]:
    """Virtual-screen and primary-monitor sizes; a hwnd grab matching these is implausible."""
    sizes: set[tuple[int, int]] = set()
    try:
        _l, _t, vw, vh = virtual_screen()
        if vw > 0 and vh > 0:
            sizes.add((int(vw), int(vh)))
    except Exception:
        pass
    try:
        import win32api
        pw, ph = int(win32api.GetSystemMetrics(0)), int(win32api.GetSystemMetrics(1))
        if pw > 0 and ph > 0:
            sizes.add((pw, ph))
    except Exception:
        pass
    return sizes


def capture_looks_implausible(arr, origin: dict | None = None) -> bool:
    """True if a hwnd grab is still sentinel, stub-sized (~144x20), or a full-desktop fallback."""
    if arr is None:
        return True
    try:
        h, w = arr.shape[:2]
    except Exception:
        return True
    if is_sentinel_origin(origin):
        return True
    if w < 80 or h < 80:
        return True
    if (int(w), int(h)) in desktop_capture_sizes():
        return True
    return False


def live_geom(hwnd: int) -> tuple[dict | None, dict | None]:
    """Client origin + window row as they are now. Never restores / force_foreground.

    Stamp validation and default (no-focus) perceive use this.
    """
    hwnd = int(hwnd)
    return client_origin(hwnd), find_window(hwnd=hwnd)


def refresh_geom(hwnd: int) -> tuple[dict | None, dict | None]:
    """Client origin + window row after restore. Only for perceive focus=true.

    Iconic / sentinel hwnds are force_foreground'd so the next grab is live.
    Stamp validation must use live_geom + is_hwnd_minimized (no restore).
    """
    import time
    import win32gui
    hwnd = int(hwnd)
    origin = client_origin(hwnd)
    was_iconic = bool(win32gui.IsIconic(hwnd))
    if was_iconic or is_sentinel_origin(origin):
        _window_service().force_foreground(hwnd)
        time.sleep(RESTORE_SETTLE_S if was_iconic else 0.15)
        origin = client_origin(hwnd)
    return origin, find_window(hwnd=hwnd)


def force_foreground(hwnd: int) -> bool:
    """Restore + raise + focus. Only on explicit request (perceive focus=true)."""
    return bool(_window_service().force_foreground(int(hwnd)))


def client_origin(hwnd: int) -> dict | None:
    region = _window_service().get_client_region(int(hwnd))
    if region is None:
        return None
    return {
        "left": region.left,
        "top": region.top,
        "width": region.width,
        "height": region.height,
    }


def capture_window_image(hwnd: int, origin: dict, *, capture: str = "auto", focused: bool = False):
    """Client-area grab of hwnd sized to ``origin``. Never focuses or restores.

    capture='auto' runs WGC -> DWM thumbnail -> PrintWindow. When ``focused`` (the
    caller already brought the window to front) the legacy screen-region grab is
    a last resort. Returns capture.CaptureResult; raises capture.CaptureError /
    WindowMinimizedError.
    """
    from .capture import CaptureError, capture_screen_region, capture_window, parse_capture_choice

    order = parse_capture_choice(capture)
    if order == ("screen",):
        if not focused:
            raise CaptureError(
                "capture=screen grabs whatever is on screen at the window rect; "
                "it needs focus=true (window brought to front)"
            )
        return capture_screen_region(origin)
    out_size = (int(origin["width"]), int(origin["height"]))
    try:
        return capture_window(int(hwnd), out_size, order=order)
    except CaptureError as e:
        if not focused or type(e) is not CaptureError or capture not in (None, "", "auto"):
            raise
        res = capture_screen_region(origin)
        res.attempts = e.attempts + res.attempts
        return res


def capture_hwnd(hwnd: int, activate: bool = False):
    """Back-compat: BGR ndarray of hwnd's client area, or None. activate=True focuses first."""
    import time
    hwnd = int(hwnd)
    if activate:
        force_foreground(hwnd)
        time.sleep(RESTORE_SETTLE_S)
    origin = client_origin(hwnd)
    if origin is None:
        return None
    try:
        return capture_window_image(hwnd, origin, focused=activate).image
    except Exception:
        return None


def virtual_screen() -> tuple[int, int, int, int]:
    import win32api
    left = int(win32api.GetSystemMetrics(76))
    top = int(win32api.GetSystemMetrics(77))
    w = int(win32api.GetSystemMetrics(78))
    h = int(win32api.GetSystemMetrics(79))
    return left, top, w, h


def windows_map(out_dir: str | None = None, rows: list[dict] | None = None) -> dict:
    """Desktop grab + 0-1000 grid + numbered window boxes. No OCR."""
    import cv2
    import numpy as np
    from PIL import ImageGrab
    from .core import DEFAULT_OUT, draw_grid

    dest = Path(out_dir) if out_dir else DEFAULT_OUT
    dest.mkdir(parents=True, exist_ok=True)
    if rows is None:
        rows = list_windows(include_minimized=True)
    left, top, vw, vh = virtual_screen()
    pil = ImageGrab.grab(bbox=(left, top, left + vw, top + vh), all_screens=True)
    bgr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    mapped = []
    for i, w in enumerate(rows, start=1):
        item = {
            "id": i,
            "hwnd": w.get("hwnd"),
            "title": w.get("title"),
            "exe": w.get("exe"),
            "pid": w.get("pid"),
            "rect": w.get("rect"),
            "minimized": w.get("minimized"),
        }
        mapped.append(item)
        if w.get("minimized"):
            continue
        rl, rt, rr, rb = w["rect"]
        x1 = int(rl - left)
        y1 = int(rt - top)
        x2 = int(rr - left)
        y2 = int(rb - top)
        if x2 < 0 or y2 < 0 or x1 > vw or y1 > vh:
            continue
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(vw - 1, x2), min(vh - 1, y2)
        color = (0, 180, 255)
        cv2.rectangle(bgr, (x1, y1), (x2, y2), color, 2)
        label = f"#{i} {w.get('hwnd')}"
        cv2.putText(bgr, label, (x1 + 4, max(16, y1 + 16)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    stamped = draw_grid(bgr)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    mapp = dest / f"{stamp}.windows.map.png"
    js = dest / f"{stamp}.windows.json"
    cv2.imwrite(str(mapp), stamped)
    payload = {
        "stamp": stamp,
        "width": vw,
        "height": vh,
        "origin": {"left": left, "top": top},
        "coord_space": "screen",
        "windows": mapped,
        "map": str(mapp),
        "json": str(js),
    }
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
