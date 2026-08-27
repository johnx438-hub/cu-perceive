"""Window list/find and hwnd capture. Harvested from Enikk Win32 helpers, not the game layer."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ENIKK_ROOT = Path(r"C:\Users\jawn\src\enikk")
if str(ENIKK_ROOT) not in sys.path:
    sys.path.insert(0, str(ENIKK_ROOT))

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


def find_window(title: str = "", exe: str = "", hwnd: int | None = None) -> dict | None:
    """Find one window. hwnd wins. title and exe are AND if both set."""
    wins = list_windows(include_minimized=True)
    if hwnd is not None:
        hwnd = int(hwnd)
        for w in wins:
            if int(w["hwnd"]) == hwnd:
                return w
        from enikk.game.window import WindowService
        if WindowService().is_valid(hwnd):
            return {
                "hwnd": hwnd, "title": "", "exe": "", "pid": 0,
                "exe_path": "", "rect": [0, 0, 0, 0], "minimized": False,
            }
        return None

    title_l = (title or "").lower()
    exe_l = (exe or "").lower()
    if not title_l and not exe_l:
        return None

    matches = []
    for w in wins:
        if title_l and title_l not in (w.get("title") or "").lower():
            continue
        exe_hay = ((w.get("exe") or "") + " " + (w.get("exe_path") or "")).lower()
        if exe_l and exe_l not in exe_hay:
            continue
        matches.append(w)
    if not matches:
        return None

    def score(w):
        t = (w.get("title") or "").lower()
        exact = 1 if title_l and t == title_l else 0
        title_hit = 1 if title_l and title_l in t else 0
        exe_in_title = 1 if exe_l and exe_l in t else 0
        left, top, right, bottom = w["rect"]
        area = max(0, right - left) * max(0, bottom - top)
        if w.get("minimized"):
            area = 0
        return (exact, title_hit, exe_in_title, 0 if w.get("minimized") else 1, area)

    return max(matches, key=score)



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


def refresh_geom(hwnd: int) -> tuple[dict | None, dict | None]:
    """Client origin + window row after restore. Call AFTER capture, not before."""
    import time
    import win32gui
    hwnd = int(hwnd)
    origin = client_origin(hwnd)
    was_iconic = bool(win32gui.IsIconic(hwnd))
    if was_iconic or is_sentinel_origin(origin):
        from enikk.game.window import WindowService
        WindowService().force_foreground(hwnd)
        time.sleep(RESTORE_SETTLE_S if was_iconic else 0.15)
        origin = client_origin(hwnd)
    return origin, find_window(hwnd=hwnd)


def client_origin(hwnd: int) -> dict | None:
    from enikk.game.window import WindowService
    region = WindowService().get_client_region(int(hwnd))
    if region is None:
        return None
    return {
        "left": region.left,
        "top": region.top,
        "width": region.width,
        "height": region.height,
    }


def capture_hwnd(hwnd: int, activate: bool = False):
    from enikk.game.capture import CaptureService
    import time
    import win32gui
    hwnd = int(hwnd)
    was_iconic = bool(win32gui.IsIconic(hwnd))
    if not (was_iconic or activate):
        return CaptureService().capture(hwnd, activate=False)
    # Restore / pin-to-front: first frame can be wallpaper, the window
    # sitting on top (e.g. Chrome over Steam CEF), or a PrintWindow empty.
    # Always discard it. Do not gate on capture_looks_implausible.
    CaptureService().capture(hwnd, activate=True)
    time.sleep(RESTORE_SETTLE_S)
    again = CaptureService().capture(hwnd, activate=False)
    return again


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
