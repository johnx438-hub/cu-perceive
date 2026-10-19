"""Low-level mouse/keyboard. Unicode type, drag, wheel. IME switching lives in ime.py."""
from __future__ import annotations

import atexit
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

from .keys import VK, UnknownKeyError, check_key_spec, key_names, key_names_summary, normalize_key  # noqa: F401

# None off Windows so the pure helpers (lParam packing, densify) stay importable for tests.
user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
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

# HWND PostMessage mouse (S1 method=post). No SetCursorPos / SendInput.
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010



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


class POINT(ctypes.Structure):
    _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))


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


def pack_lparam(cx: int, cy: int) -> int:
    """Pack client coords into WM_* mouse lParam (low word = x, high word = y)."""
    return ((int(cy) & 0xFFFF) << 16) | (int(cx) & 0xFFFF)


def screen_to_client(hwnd: int, sx: int, sy: int) -> tuple[int, int]:
    """Screen pixels -> client-relative for hwnd. Raises on failure."""
    hwnd = int(hwnd)
    if user32 is None:
        raise OSError("ScreenToClient needs Windows")
    if not user32.IsWindow(hwnd):
        raise OSError(f"hwnd {hwnd} is not a window")
    pt = POINT(int(sx), int(sy))
    ok = user32.ScreenToClient(hwnd, ctypes.byref(pt))
    if not ok:
        raise OSError(f"ScreenToClient failed for hwnd={hwnd} screen=({sx},{sy})")
    return int(pt.x), int(pt.y)


def _post_msg(hwnd: int, msg: int, wparam: int, lparam: int) -> None:
    """PostMessageW; fail-closed (no SendInput fallback)."""
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        raise OSError(f"hwnd {hwnd} is not a window")
    # BOOL PostMessageW(HWND, UINT, WPARAM, LPARAM)
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    ok = user32.PostMessageW(hwnd, int(msg), int(wparam), int(lparam))
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(f"PostMessage failed hwnd={hwnd} msg=0x{msg:04x} last_error={err}")


def _button_msgs(button: str) -> tuple[int, int, int]:
    btn = (button or "left").lower()
    if btn == "right":
        return WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON
    if btn == "middle":
        return WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON
    if btn != "left":
        raise ValueError(f"post click button must be left|right|middle, got {button!r}")
    return WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON


def post_click_messages(cx: int, cy: int, button: str = "left", double: bool = False) -> list[dict]:
    """Describe the PostMessage sequence (for dry-run / tests). Client coords."""
    down_msg, up_msg, down_flag = _button_msgs(button)
    lp = pack_lparam(cx, cy)
    seq = [
        {"msg": "WM_MOUSEMOVE", "msg_id": WM_MOUSEMOVE, "wparam": 0, "lparam": lp, "client": [int(cx), int(cy)]},
        {"msg": "DOWN", "msg_id": down_msg, "wparam": down_flag, "lparam": lp, "client": [int(cx), int(cy)]},
        {"msg": "UP", "msg_id": up_msg, "wparam": 0, "lparam": lp, "client": [int(cx), int(cy)]},
    ]
    if double and (button or "left").lower() == "left":
        seq.extend([
            {"msg": "DOWN", "msg_id": down_msg, "wparam": down_flag, "lparam": lp, "client": [int(cx), int(cy)]},
            {"msg": "UP", "msg_id": up_msg, "wparam": 0, "lparam": lp, "client": [int(cx), int(cy)]},
        ])
    return seq


def post_click_xy(
    hwnd: int,
    x: int,
    y: int,
    *,
    button: str = "left",
    double: bool = False,
    screen: bool = True,
) -> dict:
    """Non-invasive click via HWND PostMessage.

    If screen=True (default), (x,y) are screen pixels and converted with ScreenToClient.
    If screen=False, (x,y) are already client-relative.
    Does NOT SetCursorPos or SendInput. Fail-closed on PostMessage error.
    """
    hwnd = int(hwnd)
    if screen:
        cx, cy = screen_to_client(hwnd, int(x), int(y))
        sx, sy = int(x), int(y)
    else:
        cx, cy = int(x), int(y)
        sx, sy = None, None
    down_msg, up_msg, down_flag = _button_msgs(button)
    lp = pack_lparam(cx, cy)
    count = 2 if (double and (button or "left").lower() == "left") else 1
    for _ in range(count):
        _post_msg(hwnd, WM_MOUSEMOVE, 0, lp)
        _post_msg(hwnd, down_msg, down_flag, lp)
        time.sleep(0.035)
        _post_msg(hwnd, up_msg, 0, lp)
        time.sleep(0.050)
    out = {
        "ok": True,
        "method": "post",
        "hwnd": hwnd,
        "client": [cx, cy],
        "lparam": lp,
        "button": (button or "left").lower(),
        "double": bool(double),
    }
    if sx is not None:
        out["screen"] = [sx, sy]
    return out


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
    vk = VK[normalize_key(name)]
    down = INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(vk, 0, 0, 0, 0)))
    up = INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0)))
    _send(down)
    _send(up)
    return {"ok": True, "key": name.lower()}


def key_down(name: str) -> dict:
    vk = VK[normalize_key(name)]
    _vk_down(vk)
    return {"ok": True, "key": name.lower(), "down": True}


def key_up(name: str) -> dict:
    vk = VK[normalize_key(name)]
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


class ClipboardError(RuntimeError):
    """A clipboard read/write failed. Callers turn this into ok=false; it must not kill the process."""

    def __init__(self, message: str, *, reason: str = "clipboard_failed"):
        super().__init__(message)
        self.reason = reason


CLIPBOARD_OPEN_TRIES = 8
CLIPBOARD_OPEN_BASE_S = 0.02
CLIPBOARD_OPEN_MAX_S = 0.20
# pywin32 311 SetClipboardData(CF_UNICODETEXT, str) treats a digit-only str as a HANDLE.
# "4" heap-corrupts the process (0xC0000374). Never pass str; pass UTF-16-LE + NUL.


def unicode_clipboard_bytes(text: str) -> bytes:
    """CF_UNICODETEXT bytes: UTF-16-LE and a terminating NUL wchar."""
    return str(text).encode("utf-16-le") + b"\x00\x00"


def _clipboard_backoff(attempt: int) -> None:
    time.sleep(min(CLIPBOARD_OPEN_MAX_S, CLIPBOARD_OPEN_BASE_S * (2 ** max(0, int(attempt)))))


def _close_clipboard(win32clipboard) -> None:
    try:
        win32clipboard.CloseClipboard()
    except Exception:
        pass


def _set_unicode_clipboard(win32clipboard, win32con, text: str) -> None:
    """Place Unicode text on an open clipboard. str is never given to SetClipboardData."""
    payload = unicode_clipboard_bytes(text)
    try:
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, payload)
    except TypeError:
        setter = getattr(win32clipboard, "SetClipboardText", None)
        if setter is None:
            raise
        setter(str(text), win32con.CF_UNICODETEXT)


def _with_open_clipboard(fn, *, tries: int = CLIPBOARD_OPEN_TRIES):
    """OpenClipboard with backoff. Retries access-denied races. Raises ClipboardError."""
    import win32clipboard

    last: Exception | None = None
    attempts = max(1, int(tries))
    for i in range(attempts):
        opened = False
        try:
            win32clipboard.OpenClipboard(None)
            opened = True
            return fn(win32clipboard)
        except ClipboardError:
            raise
        except Exception as e:
            last = e
            if i + 1 >= attempts:
                break
        finally:
            if opened:
                _close_clipboard(win32clipboard)
        _clipboard_backoff(i)
    raise ClipboardError(f"clipboard operation failed: {last}")


def clipboard_empty_inprocess() -> None:
    """Empty the clipboard. Raises ClipboardError. Used so a paste does not leave a secret behind."""

    def clear(cb) -> None:
        cb.EmptyClipboard()

    _with_open_clipboard(clear)


def clipboard_set_inprocess(text: str) -> None:
    """Set CF_UNICODETEXT in this process. Raises ClipboardError. Used by the helper child and tests."""
    import win32con

    def write(cb) -> None:
        cb.EmptyClipboard()
        _set_unicode_clipboard(cb, win32con, "" if text is None else str(text))

    _with_open_clipboard(write)


def clipboard_get_inprocess() -> str | None:
    """CF_UNICODETEXT, or None when that format is not on the clipboard. Raises ClipboardError."""
    import win32con

    def read(cb):
        if not cb.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return None
        data = cb.GetClipboardData(win32con.CF_UNICODETEXT)
        if data is None:
            return None
        if isinstance(data, bytes):
            return data.decode("utf-16-le", "replace").rstrip("\x00")
        return str(data)

    return _with_open_clipboard(read)


# Formats that restore_staged can put back: the text itself and its synthesized siblings.
_TEXT_FORMATS = frozenset({1, 7, 13, 16})  # CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE
# Registered formats that OLE / .NET add to a plain-text copy. They carry no data of their own.
_TEXT_COMPANION_NAMES = frozenset({
    "dataobject", "ole private data", "system.string", "oleclipboardpersistonflush",
})


def _is_text_companion(cb, fmt: int) -> bool:
    if int(fmt) < 0xC000:
        return False
    try:
        name = cb.GetClipboardFormatName(int(fmt))
    except Exception:
        return False
    return str(name or "").strip().lower() in _TEXT_COMPANION_NAMES


def clipboard_foreign_formats_inprocess() -> list[int]:
    """Clipboard formats other than plain text (bitmaps, files, HTML, RTF...). Raises ClipboardError."""

    def scan(cb) -> list[int]:
        found: list[int] = []
        fmt = 0
        for _ in range(512):
            fmt = cb.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if int(fmt) not in _TEXT_FORMATS and not _is_text_companion(cb, fmt):
                found.append(int(fmt))
        return found

    return _with_open_clipboard(scan)


def _clipboard_inprocess(op: str, text: str | None) -> dict:
    try:
        if op == "get":
            foreign = clipboard_foreign_formats_inprocess()
            return {"ok": True, "text": clipboard_get_inprocess(), "foreign_formats": len(foreign)}
        if op == "set":
            clipboard_set_inprocess("" if text is None else str(text))
            return {"ok": True}
        if op == "empty":
            clipboard_empty_inprocess()
            return {"ok": True}
        return {"ok": False, "reason": "clipboard_failed", "error": f"bad clipboard op {op!r}"}
    except ClipboardError as e:
        return {"ok": False, "reason": e.reason, "error": str(e)}
    except Exception as e:
        return {"ok": False, "reason": "clipboard_failed", "error": f"{type(e).__name__}: {e}"}


def _parse_clipboard_stdout(raw: bytes) -> dict | None:
    text = (raw or b"").decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        data = json.loads(text.splitlines()[-1])
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _worker_failure(returncode, stdout: bytes = b"", stderr: bytes = b"") -> dict:
    """Map a dead clipboard helper to ok=false. A native crash must not escape."""
    parsed = _parse_clipboard_stdout(stdout or b"")
    if parsed is not None and not parsed.get("ok"):
        parsed.setdefault("reason", "clipboard_failed")
        return parsed
    err = (stderr or b"").decode("utf-8", "replace").strip()[-400:]
    detail = f"clipboard helper exited {returncode}"
    if err:
        detail = f"{detail}: {err}"
    return {"ok": False, "reason": "clipboard_failed", "error": detail}


_CLIPBOARD_WORKER_LOCK = threading.Lock()
_clipboard_worker: dict = {"proc": None}


def _kill_clipboard_worker(proc) -> None:
    if proc is None:
        return
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _shutdown_clipboard_worker() -> None:
    with _CLIPBOARD_WORKER_LOCK:
        proc = _clipboard_worker.get("proc")
        _clipboard_worker["proc"] = None
    _kill_clipboard_worker(proc)


atexit.register(_shutdown_clipboard_worker)


def _start_clipboard_worker():
    """One long-lived helper. Spawning a process per get/set cost about 1.5s per paste."""
    import subprocess

    env = os.environ.copy()
    env["CU_PERCEIVE_CLIPBOARD_CHILD"] = "1"
    kwargs: dict = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.Popen(
        [sys.executable, "-m", "cu_perceive.clipboard_worker", "--serve"],
        **kwargs,
    )


def _clipboard_worker_call(op: str, text: str | None, *, timeout: float = 8.0) -> dict:
    """One JSON line in, one JSON line out. A dead helper is restarted on the next call."""
    payload = (json.dumps({"op": op, "text": text}, ensure_ascii=False) + "\n").encode("utf-8")
    with _CLIPBOARD_WORKER_LOCK:
        proc = _clipboard_worker.get("proc")
        if proc is None or proc.poll() is not None:
            try:
                proc = _start_clipboard_worker()
            except Exception as e:
                _clipboard_worker["proc"] = None
                return {
                    "ok": False,
                    "reason": "clipboard_failed",
                    "error": f"clipboard helper failed to start: {e}",
                }
            _clipboard_worker["proc"] = proc
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except Exception as e:
            _clipboard_worker["proc"] = None
            _kill_clipboard_worker(proc)
            return {"ok": False, "reason": "clipboard_failed", "error": f"clipboard helper failed: {e}"}
        box: dict = {}

        def _read() -> None:
            try:
                box["line"] = proc.stdout.readline()
            except Exception as e:
                box["err"] = e

        reader = threading.Thread(target=_read, name="cu-perceive-clipboard-read", daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            _clipboard_worker["proc"] = None
            _kill_clipboard_worker(proc)
            return {"ok": False, "reason": "clipboard_failed", "error": "clipboard helper timed out"}
        if box.get("err") is not None or not box.get("line"):
            code = proc.poll()
            _clipboard_worker["proc"] = None
            _kill_clipboard_worker(proc)
            if box.get("err") is not None:
                return {"ok": False, "reason": "clipboard_failed", "error": f"clipboard helper failed: {box['err']}"}
            return _worker_failure(code if code is not None else "unknown", b"")
        parsed = _parse_clipboard_stdout(box["line"])
        if parsed is None:
            return {"ok": False, "reason": "clipboard_failed", "error": "clipboard helper returned nothing"}
        if not parsed.get("ok"):
            parsed.setdefault("reason", "clipboard_failed")
        else:
            parsed.pop("reason", None)
        return parsed


def clipboard_rpc(op: str, text: str | None = None) -> dict:
    """Clipboard get/set/empty that always returns {ok: bool}. A native crash stays in the helper."""
    child = os.environ.get("CU_PERCEIVE_CLIPBOARD_CHILD") == "1"
    inproc = child or os.environ.get("CU_PERCEIVE_CLIPBOARD_INPROCESS") == "1"
    if inproc:
        return _clipboard_inprocess(op, text)
    return _clipboard_worker_call(op, text)


def clipboard_get() -> str | None:
    """Best-effort Unicode clipboard read. Raises ClipboardError on failure (does not crash)."""
    got = clipboard_rpc("get")
    if not got.get("ok"):
        raise ClipboardError(got.get("error") or "clipboard_get failed", reason=got.get("reason") or "clipboard_failed")
    return got.get("text")


def clipboard_set(text: str) -> None:
    """Best-effort Unicode clipboard write. Raises ClipboardError on failure (does not crash)."""
    put = clipboard_rpc("set", "" if text is None else str(text))
    if not put.get("ok"):
        raise ClipboardError(put.get("error") or "clipboard_set failed", reason=put.get("reason") or "clipboard_failed")


def _ctrl_v() -> None:
    _vk_down(VK["ctrl"])
    time.sleep(0.02)
    _send(INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(0x56, 0, 0, 0, 0))))
    _send(INPUT(type=INPUT_KEYBOARD, union=INPUT_UNION(ki=KEYBDINPUT(0x56, 0, KEYEVENTF_KEYUP, 0, 0))))
    time.sleep(0.02)
    _vk_up(VK["ctrl"])
    time.sleep(0.08)


def _restore_staged(staged: dict) -> tuple[bool, str | None]:
    """Put the previous Unicode text back. An empty clipboard is emptied again."""
    if not staged or not staged.get("save_ok"):
        return False, None
    saved = staged.get("saved")
    if saved is None:
        back = clipboard_rpc("empty")
    else:
        back = clipboard_rpc("set", "" if saved is None else str(saved))
    if back.get("ok"):
        return True, None
    return False, back.get("error") or "could not restore the clipboard"


def prepare_paste(text: str) -> dict:
    """Save the current clipboard and set ``text``. Does not send Ctrl+V and does not clear a field.

    Call this before replace clears the edit, so a clipboard failure leaves the field alone.
    """
    text = "" if text is None else str(text)
    got = clipboard_rpc("get")
    if got.get("ok") and int(got.get("foreign_formats") or 0) > 0:
        # Only text can be saved and put back. Overwriting an image, file list or
        # rich text would destroy it, so do not touch the clipboard at all.
        return {
            "ok": False,
            "via": "clipboard",
            "reason": "clipboard_nontext",
            "error": "the clipboard holds non-text data that cannot be restored after a paste",
            "n": len(text),
            "restored": False,
            "staged": False,
            "fallback": "keys",
        }
    save_ok = bool(got.get("ok"))
    saved = got.get("text") if save_ok else None
    save_error = None if save_ok else (got.get("error") or "could not read the clipboard")
    put = clipboard_rpc("set", text)
    if not put.get("ok"):
        return {
            "ok": False,
            "via": "clipboard",
            "reason": put.get("reason") or "clipboard_failed",
            "error": put.get("error") or "clipboard_set failed",
            "n": len(text),
            "restored": False,
            "staged": False,
            "save_ok": save_ok,
            "saved": saved,
            "save_error": save_error,
        }
    return {
        "ok": True,
        "via": "clipboard",
        "staged": True,
        "text": text,
        "n": len(text),
        "save_ok": save_ok,
        "saved": saved,
        "save_error": save_error,
    }


def discard_paste(staged: dict | None) -> dict:
    """Restore the clipboard when a later step (clear, focus) refuses to type."""
    if not staged or not staged.get("staged"):
        return {"ok": True, "restored": False}
    restored, err = _restore_staged(staged)
    out = {"ok": True, "restored": restored}
    if err:
        out["clipboard_restore_error"] = err
    return out


def finish_paste(staged: dict) -> dict:
    """Ctrl+V the text already on the clipboard, then restore what prepare_paste saved."""
    if not staged or not staged.get("ok"):
        failed = staged or {"ok": False, "reason": "clipboard_failed", "error": "clipboard was not staged"}
        failed.setdefault("reason", "clipboard_failed")
        return failed
    send_error = None
    try:
        time.sleep(0.05)
        _ctrl_v()
    except Exception as e:
        send_error = e
    restored, restore_error = _restore_staged(staged)
    out = {
        "ok": send_error is None,
        "via": "clipboard",
        "n": staged.get("n", 0),
        "restored": restored,
    }
    if send_error is not None:
        out["reason"] = "clipboard_failed"
        out["error"] = f"paste failed: {send_error}"
    if staged.get("save_error"):
        out["clipboard_save_error"] = staged["save_error"]
    if restore_error:
        out["clipboard_restore_error"] = restore_error
    return out


def paste_text(text: str, restore: bool = True) -> dict:
    """Clipboard + Ctrl+V. Saves and restores the previous Unicode text, best effort.

    An empty clipboard is restored to empty so the pasted text does not linger.
    Every failure returns ok=false and reason=clipboard_failed. A native clipboard fault
    is contained in the helper process and comes back as that result, not a dead server.
    """
    text = "" if text is None else str(text)
    if not restore:
        put = clipboard_rpc("set", text)
        if not put.get("ok"):
            return {
                "ok": False,
                "via": "clipboard",
                "reason": put.get("reason") or "clipboard_failed",
                "error": put.get("error") or "clipboard_set failed",
                "n": len(text),
                "restored": False,
            }
        send_error = None
        try:
            time.sleep(0.05)
            _ctrl_v()
        except Exception as e:
            send_error = e
        out = {"ok": send_error is None, "via": "clipboard", "n": len(text), "restored": False}
        if send_error is not None:
            out["reason"] = "clipboard_failed"
            out["error"] = f"paste failed: {send_error}"
        return out
    staged = prepare_paste(text)
    if not staged.get("ok"):
        return staged
    return finish_paste(staged)


def type_keys(text: str, gap: float = 0.012) -> dict:
    return type_text(text, gap=gap)


def enter_text(text: str, via: str = "paste") -> dict:
    """Paste (default) or per-character SendInput unicode (via='keys'). Never raises for clipboard errors."""
    if via == "keys":
        try:
            result = type_text(text)
        except Exception as e:
            return {"ok": False, "reason": "input_failed", "error": str(e), "via": "keys"}
        result["via"] = "keys"
        return result
    return paste_text(text)


# Classic WinForms TextBox ignores Ctrl+A. Clear with ValuePattern, EM_SETSEL, or a key chord.
EM_SETSEL = 0x00B1
WM_CLEAR = 0x0303
WM_GETTEXTLENGTH = 0x000E
_EDIT_CLASS_MARKERS = ("edit", "richedit")


def _class_is_edit(class_name: str | None) -> bool:
    name = (class_name or "").lower()
    return any(mark in name for mark in _EDIT_CLASS_MARKERS)


def _send_message_w(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
    if user32 is None:
        raise OSError("SendMessage needs Windows")
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    return int(user32.SendMessageW(int(hwnd), int(msg), int(wparam), int(lparam)))


def _gettext_length(hwnd: int) -> int | None:
    try:
        return int(_send_message_w(int(hwnd), WM_GETTEXTLENGTH, 0, 0))
    except Exception:
        return None


def _window_class_name(hwnd: int) -> str:
    if user32 is None or not hwnd:
        return ""
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    buf = ctypes.create_unicode_buffer(256)
    n = user32.GetClassNameW(int(hwnd), buf, 256)
    return buf.value if n else ""


def _foreground_focus_hwnd() -> int:
    """hwndFocus of the foreground thread (GetGUIThreadInfo). 0 when it cannot be read."""
    if user32 is None:
        return 0

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        )

    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    if not user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        return 0
    return int(info.hwndFocus or 0)


def _legacy_text(control) -> str:
    """Legacy accessible description/help/name, for mask detection. Never raises."""
    if control is None:
        return ""
    parts: list[str] = []
    try:
        import uiautomation as auto

        leg_id = getattr(auto.PatternId, "LegacyIAccessiblePattern", None)
        leg = control.GetPattern(leg_id) if leg_id is not None else None
    except Exception:
        leg = None
    for src in (leg, control):
        if src is None:
            continue
        for name in ("Description", "Help", "Name", "AutomationId", "ClassName"):
            try:
                val = getattr(src, name, None)
            except Exception:
                continue
            if callable(val):
                try:
                    val = val()
                except Exception:
                    continue
            if val:
                parts.append(str(val))
    return " ".join(parts)


def _discover_focused_edit() -> tuple:
    """(ValuePattern or None, edit hwnd, class name, control type, control). Best effort; never raises."""
    pattern = None
    hwnd = 0
    class_name = ""
    control_type = ""
    ctrl = None
    try:
        import uiautomation as auto

        ctrl = auto.GetFocusedControl()
        if ctrl is not None:
            try:
                hwnd = int(getattr(ctrl, "NativeWindowHandle", 0) or 0)
            except Exception:
                hwnd = 0
            try:
                pattern = ctrl.GetPattern(auto.PatternId.ValuePattern)
            except Exception:
                pattern = None
            try:
                control_type = str(getattr(ctrl, "ControlTypeName", "") or "")
            except Exception:
                control_type = ""
            try:
                class_name = str(getattr(ctrl, "ClassName", "") or "")
            except Exception:
                class_name = ""
    except Exception:
        pattern = None
        ctrl = None
    if not hwnd:
        try:
            hwnd = _foreground_focus_hwnd()
        except Exception:
            hwnd = 0
    if hwnd and not class_name:
        try:
            class_name = _window_class_name(hwnd)
        except Exception:
            class_name = ""
    return pattern, hwnd, class_name, control_type, ctrl


# Edit control style. WinForms PasswordChar sets this; UIA IsPassword is the other signal.
ES_PASSWORD = 0x0020
GWL_STYLE = -16
_UIA_IS_PASSWORD = 30019


def edit_is_password(*, uia_password: bool | None = None, style: int | None = None) -> bool:
    """True when UIA says IsPassword or the edit style has ES_PASSWORD."""
    if uia_password is True:
        return True
    if style is not None and (int(style) & ES_PASSWORD):
        return True
    return False


def _edit_style(hwnd: int) -> int | None:
    if user32 is None or not hwnd:
        return None
    try:
        user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        return int(user32.GetWindowLongW(int(hwnd), GWL_STYLE))
    except Exception:
        return None


def _uia_is_password(control) -> bool | None:
    if control is None:
        return None
    flag = getattr(control, "IsPassword", None)
    if callable(flag):
        try:
            flag = flag()
        except Exception:
            flag = None
    if isinstance(flag, bool):
        return flag
    try:
        import uiautomation as auto

        pid = getattr(getattr(auto, "PropertyId", None), "IsPasswordProperty", _UIA_IS_PASSWORD)
        raw = control.GetPropertyValue(pid)
    except Exception:
        return None
    if isinstance(raw, bool):
        return raw
    if raw in (0, 1):
        return bool(raw)
    return None


def _hwnd_root(hwnd: int) -> int | None:
    """GA_ROOT of hwnd, or None when it cannot be read (including fake test hwnds)."""
    if not hwnd:
        return None
    try:
        from . import _win32

        if not _win32.is_window(int(hwnd)):
            return None
        return int(_win32.root_window(int(hwnd)) or int(hwnd))
    except Exception:
        return None


def read_focused_value() -> str | None:
    """ValuePattern of the focused control, or None when it cannot be read."""
    return focused_readback(0).get("value")


def focused_readback(target_hwnd: int = 0) -> dict:
    """Value of the focused edit, and whether that edit belongs to ``target_hwnd``.

    ``in_target`` is False when the focused control's top-level window is not
    ``target_hwnd`` (a modal dialog's Filter box). Its value must not be treated
    as the stamp window's. ``password`` skips comparison: password edits read
    back as empty. ``value`` is None when it cannot be read or it is not the target's.
    """
    pattern, hwnd, _class_name, _ctype, control = _discover_focused_edit()
    root = _hwnd_root(int(hwnd or 0))
    in_target: bool | None
    if not target_hwnd or root is None:
        in_target = None
    else:
        in_target = int(root) == int(target_hwnd)
    password = edit_is_password(uia_password=_uia_is_password(control), style=_edit_style(int(hwnd or 0)))
    value = None
    if in_target is not False and not password:
        value = _pattern_value(pattern) if pattern is not None else None
    return {
        "value": value,
        "password": password,
        "in_target": in_target,
        "hwnd": int(hwnd or 0),
        "root": root,
    }


def typed_matches(expected: str, actual: str | None) -> bool | None:
    """True/False when the focused value can be compared; None when it cannot.

    Mask literals are ignored (typed ``20261019`` matches ``2026-10-19``).
    A longer value is not a match, so an appended ``WEB-02WEB-02`` fails.
    """
    if actual is None:
        return None
    exp = "" if expected is None else str(expected)
    act = str(actual)
    if act.strip() == exp.strip():
        return True

    def alnum(s: str) -> str:
        return "".join(ch for ch in s if ch.isalnum())

    exp_n, act_n = alnum(exp), alnum(act)
    if exp_n and exp_n == act_n:
        return True
    return False


def _pattern_value(pattern) -> str | None:
    try:
        value = getattr(pattern, "Value", None)
    except Exception:
        return None
    if value is None:
        return None
    return str(value)


def _looks_masked(class_name: str | None, control_type: str | None = None, legacy_text: str | None = None) -> bool:
    """WinForms MaskedTextBox's window class is WindowsForms10.EDIT..., not 'masked'.

    Also accept UIA/legacy description, help, name, or automation id that says masked.
    """
    blob = f"{class_name or ''} {control_type or ''} {legacy_text or ''}".lower()
    return "masked" in blob or "maskedit" in blob


def value_is_blank(current: str | None) -> bool | None:
    """True for '' or a mask template (``____-__-__``). None when there is no value.

    Prompt and separator characters are not content. A date still in the box
    has digits and is not blank.
    """
    if current is None:
        return None
    if current == "":
        return True
    return not any(ch.isalnum() for ch in current)


def _looks_edit(class_name: str | None, control_type: str | None = None) -> bool:
    if _looks_masked(class_name, control_type):
        return True
    if _class_is_edit(class_name):
        return True
    return "edit" in (control_type or "").lower()


def _field_empty(pattern, hwnd: int, text_length, *, masked: bool) -> bool | None:
    """True/False when the edit can be read; None when it cannot.

    A value with no letters or digits (``____-__-__``) is empty even when
    WM_GETTEXTLENGTH counts the mask characters. Digits still in a masked
    control are not empty. Otherwise length 0 means empty.
    """
    current = _pattern_value(pattern) if pattern is not None else None
    blank = value_is_blank(current)
    if blank is True:
        return True
    if masked and blank is False:
        return False
    n = None
    if text_length is not None and hwnd:
        try:
            n = text_length(int(hwnd))
        except Exception:
            n = None
    if n is not None:
        return n == 0
    return blank


def _keyboard_clear_selection() -> None:
    """End, Shift+Home, Delete. Selects a WinForms mask; Ctrl+A and SetValue('') do not."""
    tap_key("end")
    tap_combo("shift+home")
    tap_key("delete")


def clear_field_for_replace(
    *,
    pattern=None,
    hwnd: int = 0,
    class_name: str | None = None,
    control_type: str | None = None,
    legacy_text: str | None = None,
    send_message=None,
    text_length=None,
    allow_keyboard: bool | None = None,
    discover: bool | None = None,
) -> dict:
    """Empty the focused edit before a replace-type.

    Edit and masked edits (WinForms MaskedTextBox is an Edit class) are cleared
    with End, Shift+Home, Delete. SetValue('') on a mask only blanks the display
    and leaves the old date in place, so it is not used for those controls.
    EM_SETSEL+WM_CLEAR is the fallback for a normal Edit. When the field can be
    read back and is still non-empty, ok=false and reason=replace_failed.
    """
    notes: list[str] = []
    if discover is None:
        discover = (
            pattern is None and not hwnd and send_message is None
            and text_length is None and class_name is None and control_type is None
            and legacy_text is None
        )
    if discover:
        pattern, hwnd, class_name, control_type, ctrl = _discover_focused_edit()
        if legacy_text is None:
            legacy_text = _legacy_text(ctrl)
        if send_message is None and user32 is not None:
            send_message = _send_message_w
        if text_length is None and user32 is not None:
            text_length = _gettext_length
    if allow_keyboard is None:
        allow_keyboard = user32 is not None
    masked = _looks_masked(class_name, control_type, legacy_text)
    edit = _looks_edit(class_name, control_type)

    def _done(method: str):
        empty = _field_empty(pattern, int(hwnd or 0), text_length, masked=masked)
        if empty is True:
            return {"ok": True, "method": method, "empty": True, "verified": True, "notes": notes}
        return None

    if edit or masked:
        if allow_keyboard:
            try:
                _keyboard_clear_selection()
            except Exception as e:
                notes.append(f"keyboard clear failed: {e}")
            done = _done("end_shift_home")
            if done:
                return done
        if hwnd and send_message is not None and _class_is_edit(class_name) and not masked:
            try:
                send_message(int(hwnd), EM_SETSEL, 0, -1)
                send_message(int(hwnd), WM_CLEAR, 0, 0)
            except Exception as e:
                notes.append(f"EM_SETSEL failed: {e}")
            done = _done("em_setsel")
            if done:
                return done
    else:
        if pattern is not None:
            try:
                pattern.SetValue("")
            except Exception as e:
                notes.append(f"ValuePattern.SetValue failed: {e}")
            done = _done("value_pattern")
            if done:
                return done
        if allow_keyboard:
            try:
                _keyboard_clear_selection()
            except Exception as e:
                notes.append(f"keyboard clear failed: {e}")
            done = _done("end_shift_home")
            if done:
                return done

    empty = _field_empty(pattern, int(hwnd or 0), text_length, masked=masked)
    if empty is False:
        return {
            "ok": False,
            "reason": "replace_failed",
            "error": "field still has text after End+Shift+Home+Delete"
            + ("" if masked or edit else " and ValuePattern.SetValue"),
            "method": "end_shift_home" if edit or masked else "value_pattern",
            "empty": False,
            "verified": True,
            "notes": notes,
        }
    if not allow_keyboard and not edit:
        return {"ok": True, "method": "unavailable", "empty": None, "verified": False, "notes": notes}
    return {
        "ok": True,
        "method": "end_shift_home" if (edit or masked) else "unavailable",
        "empty": None,
        "verified": False,
        "notes": notes,
    }

MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MODS = frozenset({"ctrl", "alt", "shift", "win"})



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
    parts = [p for p in check_key_spec(spec).split("+") if p]
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
