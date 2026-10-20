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


def latin_candidates(layouts) -> list[int]:
    """Every installed English layout (en-US first, then en-GB 0809, ...), then other Latin ones.

    GetKeyboardLayoutList can include an HKL that is loaded but not in the user's language list
    (a stray 0x04090409 on an en-GB + zh-Hans machine); some apps (WPS / Qt) refuse that one, so
    suspend_ime tries each candidate in turn and keeps the first one that verifies.
    """
    out: list[int] = []
    first = pick_installed_latin(layouts)
    if first:
        out.append(first)
    seen = set(out)
    ints = []
    for raw in layouts or []:
        try:
            ints.append(int(raw))
        except (TypeError, ValueError):
            continue
    for want_english in (True, False):
        for h in ints:
            if not h or h in seen:
                continue
            pl = primary_lang(h)
            if (pl == 0x09) if want_english else (pl in _LATIN_PRIMARY and pl != 0x09):
                out.append(h)
                seen.add(h)
    return out


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


def _gui_focus_hwnd(user32, tid: int) -> int:
    """The focused child on ``tid`` (GetGUIThreadInfo). 0 when unknown."""
    import ctypes

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD), ("hwndActive", wintypes.HWND),
                    ("hwndFocus", wintypes.HWND), ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND), ("rcCaret", wintypes.RECT)]

    try:
        gi = GUITHREADINFO()
        gi.cbSize = ctypes.sizeof(GUITHREADINFO)
        _bind(user32, "GetGUIThreadInfo", [wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL)
        if user32.GetGUIThreadInfo(int(tid), ctypes.byref(gi)):
            return int(gi.hwndFocus or 0)
    except Exception:
        pass
    return 0


def layout_ok(hkl: int | None, want: int | None, ime_open: bool | None) -> bool:
    """Switch verified: IME not open and the thread layout is not a CJK IME.

    Compares language ids (low word), not full HKLs: 0809:00020409 (en-GB with a US
    keyboard) reports a substitute high word, so a full-HKL compare failed on such machines.
    Any non-IME layout is fine for sending ASCII, even if it is not exactly ``want``.
    """
    if not hkl or ime_open:
        return False
    return not layout_ime_active(int(hkl), ime_open)


def _request_layout(user32, targets, hkl: int) -> list[str]:
    """WM_INPUTLANGCHANGEREQUEST to each target. Posted (handled on the target's own thread,
    which is what DefWindowProc needs to call ActivateKeyboardLayout there), sent as fallback."""
    errs: list[str] = []
    _bind(user32, "PostMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL)
    _bind(user32, "SendMessageTimeoutW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                          wintypes.UINT, wintypes.UINT, wintypes.LPVOID], wintypes.LPARAM)
    for h in targets:
        try:
            if not user32.PostMessageW(int(h), WM_INPUTLANGCHANGEREQUEST, 0, int(hkl)):
                user32.SendMessageTimeoutW(int(h), WM_INPUTLANGCHANGEREQUEST, 0, int(hkl), 0x0002, 200, None)
        except Exception as e:
            errs.append(f"{h}: {e}")
    return errs


def suspend_ime(hwnd: int, *, user32=None, imm32=None, layouts=None, probe=None, sleep=None,
                verify_s: float = 0.5) -> dict:
    """Close the target window's IME and request an installed Latin layout, then verify.

    Never calls LoadKeyboardLayout. The request goes to the focused child and the
    top-level window of the target thread; the result is polled for up to ``verify_s``
    (the switch is asynchronous). Never raises for an unverified switch: the result has
    layout_switched (verified), switch_requested, verified and, when not verified, error.
    Raises ImeError only when Windows APIs are unavailable.
    """
    import time as _time

    sleep = sleep or _time.sleep
    probe = probe or (lambda h: probe_ime(h, user32=user32, imm32=imm32))
    info = probe(hwnd)
    if user32 is None and imm32 is None:
        user32 = _load_user32()
        imm32 = _load_imm32()
    if user32 is None:
        raise ImeError("ime=off needs Windows to switch the target window's layout")
    hwnd = int(hwnd)
    before_hkl = info.get("hkl")
    if layouts is None:
        layouts = installed_layouts(user32)
    candidates = latin_candidates(layouts)
    latin = candidates[0] if candidates else None
    state = {
        "hwnd": hwnd, "hkl": before_hkl, "ime_open": info.get("ime_open"),
        "english_hkl": int(latin) if latin else None, "installed": [hkl_hex(h) for h in layouts or []],
        "switch_requested": False, "layout_switched": False, "closed": False, "verified": False,
        "already_latin": bool(info.get("ok") and layout_ok(before_hkl, latin, info.get("ime_open"))),
        "tried": [], "_user32": user32, "_imm32": imm32, "_probe": probe, "_sleep": sleep,
    }
    if state["already_latin"]:
        state["verified"] = True
        return state
    errs: list[str] = []
    if imm32 is not None and info.get("ime_open"):
        try:
            _bind(imm32, "ImmGetContext", [wintypes.HWND], wintypes.HANDLE)
            _bind(imm32, "ImmSetOpenStatus", [wintypes.HANDLE, wintypes.BOOL], wintypes.BOOL)
            _bind(imm32, "ImmReleaseContext", [wintypes.HWND, wintypes.HANDLE], wintypes.BOOL)
            himc = imm32.ImmGetContext(hwnd)
            if himc:
                try:
                    state["closed"] = bool(imm32.ImmSetOpenStatus(himc, False))
                finally:
                    imm32.ImmReleaseContext(hwnd, himc)
        except Exception as e:
            errs.append(f"ImmSetOpenStatus: {e}")
    focus = _gui_focus_hwnd(user32, info.get("thread_id") or 0) if (candidates and info.get("thread_id")) else 0
    targets = [h for h in dict.fromkeys([focus, hwnd]) if h]
    after = info
    waited = 0.0
    step = 0.05
    per = verify_s if len(candidates) <= 1 else max(0.2, verify_s / 2)
    for cand in candidates or [None]:
        if cand is not None:
            errs += _request_layout(user32, targets, cand)
            state["switch_requested"] = True
            state["targets"] = targets
            state["tried"].append(hkl_hex(cand))
        spent = 0.0
        while True:
            after = probe(hwnd)
            if after.get("ok") and layout_ok(after.get("hkl"), cand, after.get("ime_open")):
                state["verified"] = True
                break
            if cand is None and state["closed"] and after.get("ok") and after.get("ime_open") is False:
                state["verified"] = True  # no Latin layout: a closed IME passes ASCII through
                state["closed_only"] = True
                break
            if spent >= per:
                break
            sleep(step)
            spent += step
        waited += spent
        if state["verified"]:
            if cand is not None:
                state["english_hkl"] = int(cand)
            break
    state["after_hkl_hex"] = after.get("hkl_hex")
    state["verify_ms"] = int(round(waited * 1000))
    changed = bool(after.get("hkl") and before_hkl and int(after["hkl"]) != int(before_hkl))
    state["layout_switched"] = bool(state["verified"] and changed)
    if not state["verified"]:
        why = "no English or Latin keyboard layout is installed" if not latin else (
            f"requested {', '.join(state['tried'])} but the window still reports {after.get('hkl_hex')}")
        state["error"] = (f"ime=off did not switch hwnd {hwnd} ({why}, ime_open={after.get('ime_open')})"
                          + (f"; {'; '.join(errs)}" if errs else ""))
    return state


def restore_ime(state: dict | None) -> dict:
    """Put back the layout / IME open-state that ``suspend_ime`` changed. Best effort.

    restored=True only when something had been switched or closed and was put back.
    """
    if not state or not (state.get("switch_requested") or state.get("closed")):
        return {"ok": True, "restored": False}
    user32 = state.get("_user32") or _load_user32()
    imm32 = state.get("_imm32")
    if imm32 is None and state.get("closed"):
        imm32 = _load_imm32()
    hwnd = int(state.get("hwnd") or 0)
    errors: list[str] = []
    if user32 is not None and hwnd and state.get("hkl") and state.get("switch_requested"):
        errors += _request_layout(user32, state.get("targets") or [hwnd], int(state["hkl"]))
    if imm32 is not None and hwnd and state.get("closed"):
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
    probe, sleep = state.get("_probe"), state.get("_sleep")
    if probe and sleep and state.get("layout_switched") and state.get("hkl"):
        # the request is posted: confirm the original language is back (usually < 50 ms)
        want = int(state["hkl"]) & 0xFFFF
        now: dict = {}
        for _ in range(7):
            try:
                now = probe(hwnd)
            except Exception:
                return {"ok": True, "restored": True, "verified": None}  # cannot read back
            if now.get("ok") and (int(now.get("hkl") or 0) & 0xFFFF) == want:
                return {"ok": True, "restored": True, "verified": True}
            sleep(0.05)
        return {"ok": False, "restored": False, "error": f"layout still {now.get('hkl_hex')} after restore"}
    return {"ok": True, "restored": True}


def normalize_ime(raw) -> str | None:
    """None = detect and warn. 'off' = switch the target window to English for this act, and
    warn + continue if the switch cannot be verified. 'strict' = same, but abort (ime_failed)."""
    if raw in (None, ""):
        return None
    s = str(raw).strip().lower()
    if s in ("off", "strict"):
        return s
    raise ValueError('ime must be "off", "strict" or omitted (got %r)' % (raw,))
