"""Low-level mouse/keyboard. Unicode type, drag, wheel. No IME."""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
try:
    user32.SetProcessDPIAware()
except Exception:
    pass

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
WHEEL_DELTA = 120
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

VK = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "space": 0x20,
    "delete": 0x2E,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "v": 0x56,
}


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class INPUT_UNION(ctypes.Union):
    _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("union", INPUT_UNION))


def _send(inp: INPUT) -> None:
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def _virtual_screen() -> tuple[int, int, int, int]:
    left = int(user32.GetSystemMetrics(76))
    top = int(user32.GetSystemMetrics(77))
    w = int(user32.GetSystemMetrics(78))
    h = int(user32.GetSystemMetrics(79))
    return left, top, max(w, 1), max(h, 1)


def _abs_xy(x: int, y: int) -> tuple[int, int]:
    left, top, w, h = _virtual_screen()
    ax = int(round((int(x) - left) * 65535 / max(w - 1, 1)))
    ay = int(round((int(y) - top) * 65535 / max(h - 1, 1)))
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def _mouse(flags: int, x: int = 0, y: int = 0) -> None:
    if flags & MOUSEEVENTF_ABSOLUTE:
        ax, ay = _abs_xy(x, y)
        mi = MOUSEINPUT(ax, ay, 0, flags, 0, 0)
    else:
        mi = MOUSEINPUT(0, 0, 0, flags, 0, 0)
    _send(INPUT(type=INPUT_MOUSE, union=INPUT_UNION(mi=mi)))


def move(x: int, y: int) -> None:
    """Teleport cursor and also emit an absolute MOVE so apps see WM_MOUSEMOVE."""
    user32.SetCursorPos(int(x), int(y))
    _mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, int(x), int(y))





def type_text(text: str, gap: float = 0.012) -> dict:
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            tap_key("enter")
            continue
        down = INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)))
        up = INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)))
        _send(down)
        _send(up)
        if gap:
            time.sleep(gap)
    return {"ok": True, "typed": text, "n": len(text)}


def tap_key(name: str) -> dict:
    if "+" in name:
        return tap_combo(name)
    vk = VK.get(name.lower())
    if vk is None:
        raise KeyError(f"unknown key {name}")
    down = INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(vk, 0, 0, 0, 0)))
    up = INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)))
    _send(down)
    _send(up)
    return {"ok": True, "key": name.lower()}


def key_down(name: str) -> dict:
    vk = VK.get(name.lower())
    if vk is None:
        raise KeyError(f"unknown key {name}")
    _vk_down(vk)
    return {"ok": True, "key": name.lower(), "down": True}


def key_up(name: str) -> dict:
    vk = VK.get(name.lower())
    if vk is None:
        raise KeyError(f"unknown key {name}")
    _vk_up(vk)
    return {"ok": True, "key": name.lower(), "up": True}


def _densify(points: list[tuple[int, int]], step_px: int = 4) -> list[tuple[int, int]]:
    if len(points) < 2:
        return list(points)
    step = max(1, int(step_px))
    out = [points[0]]
    for i in range(1, len(points)):
        x1, y1 = points[i - 1]
        x2, y2 = points[i]
        dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        n = max(1, int(dist / step))
        for k in range(1, n + 1):
            out.append((int(x1 + (x2 - x1) * k / n), int(y1 + (y2 - y1) * k / n)))
    return out


def drag_path(
    points,
    step_px: int = 4,
    hold: float = 0.06,
    gap: float = 0.004,
) -> dict:
    """Left-down at first point, walk a densified polyline, left-up. Screen pixels."""
    pts = [(int(p[0]), int(p[1])) for p in points]
    if len(pts) < 2:
        raise ValueError("drag needs at least two points")
    dense = _densify(pts, step_px=step_px)
    x0, y0 = dense[0]
    move(x0, y0)
    time.sleep(0.06)
    flags_abs = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    _mouse(MOUSEEVENTF_LEFTDOWN | flags_abs, x0, y0)
    time.sleep(max(0.08, hold))
    for x, y in dense[1:]:
        _mouse(flags_abs, x, y)
        if gap:
            time.sleep(gap)
    time.sleep(0.04)
    x1, y1 = dense[-1]
    _mouse(MOUSEEVENTF_LEFTUP | flags_abs, x1, y1)
    return {
        "ok": True,
        "from": list(pts[0]),
        "to": list(pts[-1]),
        "waypoints": [list(p) for p in pts],
        "samples": len(dense),
        "step_px": int(step_px),
    }


def drag_xy(x1: int, y1: int, x2: int, y2: int, steps: int = 12, hold: float = 0.08) -> dict:
    dist = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    step_px = max(1, int(dist / max(2, int(steps))))
    return drag_path([(x1, y1), (x2, y2)], step_px=step_px, hold=hold)


def scroll_xy(notches: int, x: int | None = None, y: int | None = None) -> dict:
    if x is not None and y is not None:
        move(x, y)
        time.sleep(0.04)
    data = int(notches) * WHEEL_DELTA
    # mouseData is unsigned DWORD; negative notches need wrap
    data_u = ctypes.c_uint32(data).value
    wheel = INPUT(type=INPUT_MOUSE, union=INPUT_UNION(mi=MOUSEINPUT(0, 0, data_u, MOUSEEVENTF_WHEEL, 0, 0)))
    _send(wheel)
    return {"ok": True, "notches": int(notches), "xy": [x, y] if x is not None else None}

def _vk_down(vk: int) -> None:
    _send(INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(vk, 0, 0, 0, 0))))


def _vk_up(vk: int) -> None:
    _send(INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0))))


def clipboard_get() -> str | None:
    import win32clipboard
    import win32con
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    finally:
        win32clipboard.CloseClipboard()


def clipboard_set(text: str) -> None:
    import win32clipboard
    import win32con
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def paste_text(text: str, restore: bool = True) -> dict:
    """Put text on clipboard, Ctrl+V, optionally put the old clip back."""
    old = clipboard_get() if restore else None
    clipboard_set(text)
    time.sleep(0.05)
    _vk_down(VK["ctrl"])
    time.sleep(0.02)
    tap_key("v") if False else None
    _send(INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(0x56, 0, 0, 0, 0))))
    _send(INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(0x56, 0, KEYEVENTF_KEYUP, 0, 0))))
    time.sleep(0.02)
    _vk_up(VK["ctrl"])
    time.sleep(0.08)
    if restore and old is not None:
        clipboard_set(old)
    return {"ok": True, "via": "clipboard", "n": len(text), "restored": bool(restore and old is not None)}


def type_keys(text: str, gap: float = 0.012) -> dict:
    return type_text(text, gap=gap)


def enter_text(text: str, via: str = "paste") -> dict:
    if via == "keys":
        return type_text(text)
    return paste_text(text)

MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MODS = frozenset({"ctrl", "alt", "shift", "win"})

for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    VK.setdefault(_c, 0x41 + _i)
for _i in range(1, 13):
    VK.setdefault(f"f{_i}", 0x70 + _i - 1)
VK.setdefault("win", 0x5B)
VK.setdefault("up", 0x26)
VK.setdefault("down", 0x28)
VK.setdefault("left", 0x25)
VK.setdefault("right", 0x27)
VK.setdefault("home", 0x24)
VK.setdefault("end", 0x23)


def click_xy(x, y, double=False, button: str = "left", hold: float = 0):
    move(x, y)
    time.sleep(0.05)
    btn = (button or "left").lower()
    if btn == "right":
        down_f, up_f = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    elif btn == "middle":
        down_f, up_f = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
    else:
        down_f, up_f = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    down = INPUT(type=INPUT_MOUSE, union=INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, down_f, 0, 0)))
    up = INPUT(type=INPUT_MOUSE, union=INPUT_UNION(mi=MOUSEINPUT(0, 0, 0, up_f, 0, 0)))
    _send(down)
    held = float(hold or 0)
    if held > 0:
        time.sleep(held)
    _send(up)
    if double and btn == "left" and held <= 0:
        time.sleep(0.08)
        _send(down)
        _send(up)
    return {"ok": True, "x": int(x), "y": int(y), "double": bool(double), "button": btn, "hold": held}


def hover_xy(x, y, linger: float = 0.15) -> dict:
    move(x, y)
    time.sleep(linger)
    return {"ok": True, "x": int(x), "y": int(y), "via": "hover"}


def tap_combo(spec: str) -> dict:
    parts = [p for p in spec.lower().replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("empty key")
    mods = [p for p in parts if p in MODS]
    keys = [p for p in parts if p not in MODS]
    if not keys:
        keys = [mods[-1]]
        mods = mods[:-1]
    for m in mods:
        _vk_down(VK[m])
        time.sleep(0.015)
    for k in keys:
        tap_key(k)
        time.sleep(0.015)
    for m in reversed(mods):
        _vk_up(VK[m])
    return {"ok": True, "key": spec.lower()}
