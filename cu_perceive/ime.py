"""Target-window keyboard layout / IME probe, and an opt-in English switch.

Microsoft Pinyin (HKL 0x08040804) swallows ASCII keys while each tap still
reports success. Probe uses the target thread's GetKeyboardLayout and
ImmGetOpenStatus. ``ime=off`` sends WM_INPUTLANGCHANGEREQUEST to that window
(not the server's thread) for an already-installed Latin layout, closes its
IME, then restores both. It does not call LoadKeyboardLayout.
"""
from __future__ import annotations

import sys
from ctypes import wintypes

WM_INPUTLANGCHANGEREQUEST = 0x0050
# LANG_CHINESE, LANG_JAPANESE, LANG_KOREAN — layouts that compose ASCII.
_IME_PRIMARY = frozenset({0x04, 0x11, 0x12})


class ImeError(OSError):
    """ime=off could not switch the target window. reason=ime_failed."""

    def __init__(self, message: str):
        super().__init__(message)
        self.reason = "ime_failed"


def layout_ime_active(hkl: int, ime_open: bool | None) -> bool:
    """True when an IME is open or the layout's primary language is CJK."""
    if ime_open:
        return True
    primary = (int(hkl) & 0xFFFF) & 0x3FF
    return primary in _IME_PRIMARY


def hkl_hex(hkl: int) -> str:
    return f"0x{int(hkl) & 0xFFFFFFFF:08x}"


def _load_user32():
    if sys.platform != "win32":
        return None
    import ctypes

    return ctypes.WinDLL("user32", use_last_error=True)


def _load_imm32():
    if sys.platform != "win32":
        return None
    import ctypes

    try:
        return ctypes.WinDLL("imm32", use_last_error=True)
    except OSError:
        return None


def _bind(dll, name: str, args, restype) -> None:
    fn = getattr(dll, name)
    fn.argtypes = args
    fn.restype = restype


def probe_ime(hwnd: int, *, user32=None, imm32=None) -> dict:
    """Layout and IME-open state for ``hwnd``'s thread. Never raises.

    Returns hwnd, thread_id, hkl, hkl_hex, lang_id, ime_open, ime_active, ok.
    """
    hwnd = int(hwnd or 0)
    out = {
        "ok": False,
        "hwnd": hwnd,
        "thread_id": None,
        "hkl": None,
        "hkl_hex": None,
        "lang_id": None,
        "ime_open": None,
        "ime_active": False,
    }
    if not hwnd:
        out["error"] = "no hwnd"
        return out
    if user32 is None and imm32 is None:
        user32 = _load_user32()
        imm32 = _load_imm32()
    if user32 is None:
        out["error"] = "keyboard layout needs Windows"
        return out
    import ctypes

    try:
        _bind(user32, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
        _bind(user32, "GetKeyboardLayout", [wintypes.DWORD], wintypes.HANDLE)
        tid = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
        hkl = int(user32.GetKeyboardLayout(tid) or 0)
    except Exception as e:
        out["error"] = f"GetKeyboardLayout failed: {e}"
        return out
    ime_open = None
    if imm32 is not None:
        try:
            _bind(imm32, "ImmGetContext", [wintypes.HWND], wintypes.HANDLE)
            _bind(imm32, "ImmGetOpenStatus", [wintypes.HANDLE], wintypes.BOOL)
            _bind(imm32, "ImmReleaseContext", [wintypes.HWND, wintypes.HANDLE], wintypes.BOOL)
            himc = imm32.ImmGetContext(hwnd)
            if himc:
                try:
                    ime_open = bool(imm32.ImmGetOpenStatus(himc))
                finally:
                    imm32.ImmReleaseContext(hwnd, himc)
        except Exception:
            ime_open = None
    out.update({
        "ok": True,
        "thread_id": tid,
        "hkl": hkl,
        "hkl_hex": hkl_hex(hkl),
        "lang_id": f"0x{hkl & 0xFFFF:04x}",
        "ime_open": ime_open,
        "ime_active": layout_ime_active(hkl, ime_open),
    })
    return out


# Primary language ids that use a Latin keyboard. English is preferred.
_LATIN_PRIMARY = frozenset({
    0x09,  # en
    0x07,  # de
    0x0C,  # fr
    0x0A,  # es
    0x16,  # pt
    0x10,  # it
    0x13,  # nl
    0x1D,  # sv
    0x06,  # da
    0x14,  # no
    0x0B,  # fi
    0x15,  # pl
    0x05,  # cs
    0x0E,  # hu
    0x18,  # ro
    0x1F,  # tr
    0x2A,  # vi
    0x21,  # id
    0x3E,  # ms
    0x0F,  # is
    0x1B,  # sk
    0x24,  # sl
    0x25,  # et
    0x26,  # lv
    0x27,  # lt
})


def primary_lang(hkl: int) -> int:
    return (int(hkl) & 0xFFFF) & 0x3FF


def pick_installed_latin(layouts) -> int | None:
    """An already-installed English layout, else another Latin one. Never a new HKL."""
    installed = []
    for raw in layouts or []:
        try:
            hkl = int(raw)
        except (TypeError, ValueError):
            continue
        if hkl:
            installed.append(hkl)
    english = [h for h in installed if primary_lang(h) == 0x09]
    for hkl in english:
        if (hkl & 0xFFFF) == 0x0409:
            return hkl
    if english:
        return english[0]
    for hkl in installed:
        if primary_lang(hkl) in _LATIN_PRIMARY:
            return hkl
    return None


def installed_layouts(user32) -> list[int]:
    """HKLs from GetKeyboardLayoutList. Empty when the list cannot be read."""
    import ctypes

    try:
        _bind(user32, "GetKeyboardLayoutList", [ctypes.c_int, ctypes.c_void_p], ctypes.c_int)
        count = int(user32.GetKeyboardLayoutList(0, None) or 0)
    except Exception:
        return []
    if count <= 0:
        return []
    buf = (ctypes.c_void_p * count)()
    try:
        got = int(user32.GetKeyboardLayoutList(count, ctypes.cast(buf, ctypes.c_void_p)) or 0)
    except Exception:
        return []
    out: list[int] = []
    for i in range(max(0, got)):
        try:
            hkl = int(buf[i] or 0)
        except (TypeError, ValueError):
            continue
        if hkl:
            out.append(hkl)
    return out


def suspend_ime(hwnd: int, *, user32=None, imm32=None, layouts=None) -> dict:
    """Close the target window's IME and, when one is installed, request a Latin layout.

    Uses GetKeyboardLayoutList. Never calls LoadKeyboardLayout, so a missing
    en-US layout is not added to the user's language list. With no Latin layout
    installed, only the IME is closed.

    Raises ImeError when the requested switch itself fails. Does not change the
    server process layout.
    """
    info = probe_ime(hwnd, user32=user32, imm32=imm32)
    if user32 is None and imm32 is None:
        user32 = _load_user32()
        imm32 = _load_imm32()
    if user32 is None:
        raise ImeError("ime=off needs Windows to switch the target window's layout")
    hwnd = int(hwnd)
    if layouts is None:
        layouts = installed_layouts(user32)
    latin = pick_installed_latin(layouts)
    layout_switched = False
    if latin:
        try:
            _bind(user32, "SendMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.LPARAM)
            user32.SendMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, int(latin))
            layout_switched = True
        except Exception as e:
            raise ImeError(f"could not switch hwnd {hwnd} to an installed Latin layout: {e}") from e

    closed = False
    if imm32 is not None and info.get("ime_open"):
        try:
            _bind(imm32, "ImmGetContext", [wintypes.HWND], wintypes.HANDLE)
            _bind(imm32, "ImmSetOpenStatus", [wintypes.HANDLE, wintypes.BOOL], wintypes.BOOL)
            _bind(imm32, "ImmReleaseContext", [wintypes.HWND, wintypes.HANDLE], wintypes.BOOL)
            himc = imm32.ImmGetContext(hwnd)
            if himc:
                try:
                    imm32.ImmSetOpenStatus(himc, False)
                    closed = True
                finally:
                    imm32.ImmReleaseContext(hwnd, himc)
        except Exception as e:
            raise ImeError(f"could not close the IME on hwnd {hwnd}: {e}") from e
    if not layout_switched and not closed and not info.get("ime_open"):
        # Nothing to close and no Latin layout. Still a successful no-op switch
        # only when the IME was already closed; the caller warns about the layout.
        pass

    return {
        "hwnd": hwnd,
        "hkl": info.get("hkl"),
        "ime_open": info.get("ime_open"),
        "english_hkl": int(latin) if latin else None,
        "layout_switched": layout_switched,
        "closed_only": not layout_switched,
        "closed": closed,
        "_user32": user32,
        "_imm32": imm32,
    }


def restore_ime(state: dict | None) -> dict:
    """Put the layout and IME open-state from ``suspend_ime`` back. Best effort."""
    if not state:
        return {"ok": True, "restored": False}
    user32 = state.get("_user32") or _load_user32()
    imm32 = state.get("_imm32")
    if imm32 is None and state.get("ime_open"):
        imm32 = _load_imm32()
    hwnd = int(state.get("hwnd") or 0)
    errors: list[str] = []
    if user32 is not None and hwnd and state.get("hkl"):
        try:
            _bind(
                user32, "SendMessageW",
                [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
                wintypes.LPARAM,
            )
            user32.SendMessageW(hwnd, WM_INPUTLANGCHANGEREQUEST, 0, int(state["hkl"]))
        except Exception as e:
            errors.append(f"layout: {e}")
    if imm32 is not None and hwnd and state.get("ime_open") is not None and state.get("closed"):
        try:
            _bind(imm32, "ImmGetContext", [wintypes.HWND], wintypes.HANDLE)
            _bind(imm32, "ImmSetOpenStatus", [wintypes.HANDLE, wintypes.BOOL], wintypes.BOOL)
            _bind(imm32, "ImmReleaseContext", [wintypes.HWND, wintypes.HANDLE], wintypes.BOOL)
            himc = imm32.ImmGetContext(hwnd)
            if himc:
                try:
                    imm32.ImmSetOpenStatus(himc, bool(state.get("ime_open")))
                finally:
                    imm32.ImmReleaseContext(hwnd, himc)
        except Exception as e:
            errors.append(f"ime: {e}")
    if errors:
        return {"ok": False, "restored": False, "error": "; ".join(errors)}
    return {"ok": True, "restored": True}


def normalize_ime(raw) -> str | None:
    """None = detect and warn. 'off' = switch the target window to English for this act."""
    if raw in (None, ""):
        return None
    s = str(raw).strip().lower()
    if s == "off":
        return "off"
    raise ValueError('ime must be "off" or omitted (got %r)' % (raw,))
