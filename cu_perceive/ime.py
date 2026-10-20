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


WM_IME_CONTROL = 0x0283
IMC_GETCONVERSIONMODE = 0x0001
IMC_GETOPENSTATUS = 0x0005
IMC_SETOPENSTATUS = 0x0006
IME_CMODE_NATIVE = 0x0001  # set = native (e.g. Chinese) input; clear = alphanumeric
GA_ROOT = 2


def focus_target(hwnd: int, user32) -> tuple[int, int, int]:
    """(focus_hwnd, focus_thread, top_thread) for top-level ``hwnd``.

    The focused child can live on another thread than the top-level window (WPS: the
    document view). GetGUIThreadInfo on the top-level thread, else on the foreground
    thread when its focus is inside ``hwnd``. Falls back to (hwnd, top_thread, top_thread).
    """
    import ctypes

    _bind(user32, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
    top = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
    focus = 0
    for tid in (top, 0):
        f = _gui_focus_hwnd(user32, tid) if (tid or top) else 0
        if not f:
            continue
        if tid == 0:
            try:
                _bind(user32, "GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND)
                if int(user32.GetAncestor(f, GA_ROOT) or 0) != int(hwnd):
                    continue
            except Exception:
                continue
        focus = f
        break
    if not focus:
        return int(hwnd), top, top
    try:
        ftid = int(user32.GetWindowThreadProcessId(focus, None) or 0) or top
    except Exception:
        ftid = top
    return int(focus), ftid, top


def _ime_window_state(user32, imm32, focus: int) -> tuple[bool | None, int | None]:
    """(open, conversion) via ImmGetDefaultIMEWnd + WM_IME_CONTROL. Works across threads,
    where ImmGetContext on another thread's window returns NULL."""
    if imm32 is None:
        return None, None
    import ctypes

    try:
        _bind(imm32, "ImmGetDefaultIMEWnd", [wintypes.HWND], wintypes.HWND)
        ime_wnd = int(imm32.ImmGetDefaultIMEWnd(focus) or 0)
        if not ime_wnd:
            return None, None
        _bind(user32, "SendMessageTimeoutW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                              wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)],
              wintypes.LPARAM)
        vals = []
        for cmd in (IMC_GETOPENSTATUS, IMC_GETCONVERSIONMODE):
            res = ctypes.c_size_t(0)
            ok = user32.SendMessageTimeoutW(ime_wnd, WM_IME_CONTROL, cmd, 0, 0x0002, 200, ctypes.byref(res))
            vals.append(int(res.value) if ok else None)
        return (None if vals[0] is None else bool(vals[0])), vals[1]
    except Exception:
        return None, None


def set_ime_open(hwnd_or_focus: int, open_: bool, *, user32=None, imm32=None) -> bool:
    """IMC_SETOPENSTATUS via the focus window's default IME window; ImmSetOpenStatus as fallback."""
    import ctypes

    user32 = user32 or _load_user32()
    imm32 = imm32 or _load_imm32()
    if imm32 is None or user32 is None:
        return False
    try:
        _bind(imm32, "ImmGetDefaultIMEWnd", [wintypes.HWND], wintypes.HWND)
        ime_wnd = int(imm32.ImmGetDefaultIMEWnd(int(hwnd_or_focus)) or 0)
        if ime_wnd:
            _bind(user32, "SendMessageTimeoutW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                                  wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)],
                  wintypes.LPARAM)
            res = ctypes.c_size_t(0)
            if user32.SendMessageTimeoutW(ime_wnd, WM_IME_CONTROL, IMC_SETOPENSTATUS, 1 if open_ else 0,
                                          0x0002, 200, ctypes.byref(res)):
                return True
    except Exception:
        pass
    try:
        _bind(imm32, "ImmGetContext", [wintypes.HWND], wintypes.HANDLE)
        _bind(imm32, "ImmSetOpenStatus", [wintypes.HANDLE, wintypes.BOOL], wintypes.BOOL)
        _bind(imm32, "ImmReleaseContext", [wintypes.HWND, wintypes.HANDLE], wintypes.BOOL)
        himc = imm32.ImmGetContext(int(hwnd_or_focus))
        if himc:
            try:
                return bool(imm32.ImmSetOpenStatus(himc, bool(open_)))
            finally:
                imm32.ImmReleaseContext(int(hwnd_or_focus), himc)
    except Exception:
        pass
    return False


def probe_ime(hwnd: int, *, user32=None, imm32=None) -> dict:
    """Layout and IME state of the thread that owns ``hwnd``'s focused child. Never raises.

    Returns hwnd, focus_hwnd, thread_id (the focus thread), top_thread_id, hkl, hkl_hex,
    lang_id, ime_open, ime_conversion (IME_CMODE_* bits; 0x1 = native input), ime_active, ok.
    """
    hwnd = int(hwnd or 0)
    out = {
        "ok": False, "hwnd": hwnd, "focus_hwnd": None, "thread_id": None, "top_thread_id": None,
        "hkl": None, "hkl_hex": None, "lang_id": None, "ime_open": None, "ime_conversion": None,
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
        try:
            focus, tid, top = focus_target(hwnd, user32)
        except Exception:
            _bind(user32, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
            top = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
            focus, tid = hwnd, top
        _bind(user32, "GetKeyboardLayout", [wintypes.DWORD], wintypes.HANDLE)
        hkl = int(user32.GetKeyboardLayout(tid) or 0)
    except Exception as e:
        out["error"] = f"GetKeyboardLayout failed: {e}"
        return out
    ime_open = None
    conv = None
    if imm32 is not None:
        try:
            _bind(imm32, "ImmGetContext", [wintypes.HWND], wintypes.HANDLE)
            _bind(imm32, "ImmGetOpenStatus", [wintypes.HANDLE], wintypes.BOOL)
            _bind(imm32, "ImmReleaseContext", [wintypes.HWND, wintypes.HANDLE], wintypes.BOOL)
            himc = imm32.ImmGetContext(focus)
            if himc:
                try:
                    ime_open = bool(imm32.ImmGetOpenStatus(himc))
                    try:
                        _bind(imm32, "ImmGetConversionStatus",
                              [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)],
                              wintypes.BOOL)
                        c, sm = wintypes.DWORD(0), wintypes.DWORD(0)
                        if imm32.ImmGetConversionStatus(himc, ctypes.byref(c), ctypes.byref(sm)):
                            conv = int(c.value)
                    except Exception:
                        pass
                finally:
                    imm32.ImmReleaseContext(focus, himc)
        except Exception:
            ime_open = None
        if ime_open is None:
            # other-thread focus window: ImmGetContext is NULL there; ask its IME window instead
            ime_open, conv2 = _ime_window_state(user32, imm32, focus)
            conv = conv if conv is not None else conv2
    out.update({
        "ok": True, "focus_hwnd": int(focus), "thread_id": tid, "top_thread_id": top,
        "hkl": hkl, "hkl_hex": hkl_hex(hkl), "lang_id": f"0x{hkl & 0xFFFF:04x}",
        "ime_open": ime_open, "ime_conversion": conv,
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


IME_VERIFY_BUDGET_S = 0.75  # total verify budget across every layout candidate (and the IME-close fallback)


def suspend_ime(hwnd: int, *, user32=None, imm32=None, layouts=None, probe=None, sleep=None,
                verify_s: float = IME_VERIFY_BUDGET_S, mode: str = "off") -> dict:
    """Make ``hwnd``'s focused input thread send ASCII for this act, then verify.

    1. Each installed English layout, then other Latin ones, is requested on the focused child
       and the top-level window (WM_INPUTLANGCHANGEREQUEST) and verified on the focus thread.
       ``verify_s`` is the TOTAL budget, split evenly over the candidates.
    2. If no layout sticks but the IME is open, it is closed (IMC_SETOPENSTATUS 0); a confirmed
       close counts as suspended and verified (ime_closed=True).
    Never calls LoadKeyboardLayout. Never raises for an unverified result (see ``error``);
    raises ImeError only when Windows APIs are unavailable.
    """
    import time as _time

    sleep = sleep or _time.sleep
    probe = probe or (lambda h: probe_ime(h, user32=user32, imm32=imm32))
    info = probe(hwnd)
    if user32 is None and imm32 is None:
        user32 = _load_user32()
        imm32 = _load_imm32()
    if user32 is None:
        raise ImeError(f"ime={mode} needs Windows to switch the target window's layout")
    hwnd = int(hwnd)
    before_hkl = info.get("hkl")
    if layouts is None:
        layouts = installed_layouts(user32)
    candidates = latin_candidates(layouts)
    latin = candidates[0] if candidates else None
    focus = int(info.get("focus_hwnd") or 0) or hwnd
    state = {
        "hwnd": hwnd, "focus_hwnd": focus, "thread_id": info.get("thread_id"),
        "hkl": before_hkl, "ime_open": info.get("ime_open"), "ime_conversion": info.get("ime_conversion"),
        "english_hkl": int(latin) if latin else None, "installed": [hkl_hex(h) for h in layouts or []],
        "switch_requested": False, "layout_switched": False, "closed": False, "verified": False,
        "already_latin": bool(info.get("ok") and layout_ok(before_hkl, latin, info.get("ime_open"))),
        "tried": [], "verify_budget_ms": int(round(verify_s * 1000)), "mode": mode,
        "_user32": user32, "_imm32": imm32, "_probe": probe, "_sleep": sleep,
    }
    after = info
    if state["already_latin"]:
        state["verified"] = True
        state["after"] = info
        return state
    errs: list[str] = []
    targets = [h for h in dict.fromkeys([focus, hwnd]) if h]
    state["targets"] = targets
    step = 0.05
    per = verify_s / max(1, len(candidates))
    waited = 0.0
    for cand in candidates:
        errs += _request_layout(user32, targets, cand)
        state["switch_requested"] = True
        state["tried"].append(hkl_hex(cand))
        spent = 0.0
        while True:
            after = probe(hwnd)
            if after.get("ok") and layout_ok(after.get("hkl"), cand, after.get("ime_open")):
                state["verified"] = True
                state["english_hkl"] = int(cand)
                break
            if spent + 1e-9 >= per:
                break
            sleep(step)
            spent += step
        waited += spent
        if state["verified"]:
            break
    if not state["verified"]:
        if state["switch_requested"]:
            # undo the unverified request right away so the window is left as it was
            _request_layout(user32, targets, int(before_hkl)) if before_hkl else None
        now = probe(hwnd) if candidates else after
        if now.get("ime_open") and imm32 is not None:
            if set_ime_open(focus, False, user32=user32, imm32=imm32):
                after = probe(hwnd)
                if after.get("ime_open") is False:
                    state["closed"] = True
                    state["verified"] = True
                    state["closed_only"] = True
        else:
            after = now
    state["after"] = after
    state["after_hkl_hex"] = after.get("hkl_hex")
    state["verify_ms"] = int(round(waited * 1000))
    changed = bool(after.get("hkl") and before_hkl and int(after["hkl"]) != int(before_hkl))
    state["layout_switched"] = bool(state["verified"] and changed and not state.get("closed_only"))
    if not state["verified"]:
        why = "no English or Latin keyboard layout is installed" if not latin else (
            f"requested {', '.join(state['tried'])} on focus hwnd {focus} (thread {after.get('thread_id')}) "
            f"but it still reports {after.get('hkl_hex')}")
        state["error"] = (f'ime="{mode}" could not switch hwnd {hwnd} ({why}, ime_open={after.get("ime_open")}, '
                          f'ime_conversion={after.get("ime_conversion")})'
                          + (f"; {'; '.join(errs)}" if errs else ""))
    return state


def restore_ime(state: dict | None) -> dict:
    """Undo what ``suspend_ime`` actually changed. Best effort.

    restored=True only when a verified layout switch or an IME close happened and was undone
    (read back when a probe is available). Nothing changed -> {"restored": False, "changed": False}.
    """
    if not state or not (state.get("layout_switched") or state.get("closed")):
        return {"ok": True, "restored": False, "changed": False}
    user32 = state.get("_user32") or _load_user32()
    imm32 = state.get("_imm32")
    if imm32 is None and state.get("closed"):
        imm32 = _load_imm32()
    hwnd = int(state.get("hwnd") or 0)
    focus = int(state.get("focus_hwnd") or 0) or hwnd
    errors: list[str] = []
    if user32 is not None and hwnd and state.get("hkl") and state.get("layout_switched"):
        errors += _request_layout(user32, state.get("targets") or [hwnd], int(state["hkl"]))
    if hwnd and state.get("closed"):
        if not set_ime_open(focus, bool(state.get("ime_open")), user32=user32, imm32=imm32):
            errors.append("ime: could not reopen the IME")
    if errors:
        return {"ok": False, "restored": False, "changed": True, "error": "; ".join(errors)}
    probe, sleep = state.get("_probe"), state.get("_sleep")
    if probe and sleep:
        want = int(state.get("hkl") or 0) & 0xFFFF
        now: dict = {}
        for _ in range(7):
            try:
                now = probe(hwnd)
            except Exception:
                return {"ok": True, "restored": True, "changed": True, "verified": None}
            lay_ok = not state.get("layout_switched") or (int(now.get("hkl") or 0) & 0xFFFF) == want
            open_ok = not state.get("closed") or now.get("ime_open") in (None, bool(state.get("ime_open")))
            if now.get("ok") and lay_ok and open_ok:
                return {"ok": True, "restored": True, "changed": True, "verified": True, "after": now}
            sleep(0.05)
        return {"ok": False, "restored": False, "changed": True,
                "error": f"still {now.get('hkl_hex')} / ime_open={now.get('ime_open')} after restore"}
    return {"ok": True, "restored": True, "changed": True}


def normalize_ime(raw) -> str | None:
    """None = detect and warn. 'off' = switch the target window to English for this act, and
    warn + continue if the switch cannot be verified. 'strict' = same, but abort (ime_failed)."""
    if raw in (None, ""):
        return None
    s = str(raw).strip().lower()
    if s in ("off", "strict"):
        return s
    raise ValueError('ime must be "off", "strict" or omitted (got %r)' % (raw,))
