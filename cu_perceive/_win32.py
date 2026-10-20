"""ctypes Win32 helpers: window identity, DPI-aware geometry, PrintWindow / DWM-thumbnail grabs.

Import-safe on any OS; every call raises Win32Unavailable off Windows. Nothing here
activates, restores or raises a window.

The DWM-thumbnail and PrintWindow grabs follow the window-preview fallback chain in
Piik (https://github.com/TNTcraftHIM/Piik, native/capture/windows/capture_target.cpp
@ 6cb0ca1, MIT, Copyright (c) 2026 TNTcraft and Piik contributors), rewritten in
Python, without Piik's 320x180 preview scaling. See THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0
SRCCOPY = 0x00CC0020
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SWP_NOACTIVATE = 0x0010
SWP_NOSENDCHANGING = 0x0400
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
OFFSCREEN = -32768
DWM_TNP_RECTDESTINATION = 0x00000001
DWM_TNP_VISIBLE = 0x00000008
DWM_TNP_SOURCECLIENTAREAONLY = 0x00000010
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
DWM_SETTLE_S = 0.012
GA_ROOT = 2
GW_OWNER = 4


class Win32Unavailable(OSError):
    """Raised when a Win32 call is attempted on a non-Windows host."""


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


class BITMAPINFO(ctypes.Structure):
    _fields_ = (("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3))


class DWM_THUMBNAIL_PROPERTIES(ctypes.Structure):
    _fields_ = (
        ("dwFlags", wintypes.DWORD),
        ("rcDestination", wintypes.RECT),
        ("rcSource", wintypes.RECT),
        ("opacity", ctypes.c_ubyte),
        ("fVisible", wintypes.BOOL),
        ("fSourceClientAreaOnly", wintypes.BOOL),
    )


_DLLS: dict[str, object] = {}


def _declare(name: str, dll) -> None:
    H, HWND, HDC = wintypes.HANDLE, wintypes.HWND, wintypes.HDC
    sigs: dict[str, tuple[list, object]] = {}
    if name == "user32":
        sigs = {
            "IsWindow": ([HWND], wintypes.BOOL),
            "IsWindowVisible": ([HWND], wintypes.BOOL),
            "IsChild": ([HWND, HWND], wintypes.BOOL),
            "IsIconic": ([HWND], wintypes.BOOL),
            "GetForegroundWindow": ([], HWND),
            "GetAncestor": ([HWND, wintypes.UINT], HWND),
            "WindowFromPoint": ([wintypes.POINT], HWND),
            "GetCursorPos": ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            "GetWindow": ([HWND, wintypes.UINT], HWND),
            "GetClassNameW": ([HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowTextW": ([HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetWindowThreadProcessId": ([HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetWindowRect": ([HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetClientRect": ([HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetDC": ([HWND], HDC),
            "ReleaseDC": ([HWND, HDC], ctypes.c_int),
            "PrintWindow": ([HWND, HDC, wintypes.UINT], wintypes.BOOL),
            "CreateWindowExW": (
                [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                 ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                 HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID],
                HWND,
            ),
            "DestroyWindow": ([HWND], wintypes.BOOL),
            "ShowWindow": ([HWND, ctypes.c_int], wintypes.BOOL),
            "UpdateWindow": ([HWND], wintypes.BOOL),
            "SetWindowPos": (
                [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT],
                wintypes.BOOL,
            ),
            "SetProcessDPIAware": ([], wintypes.BOOL),
        }
    elif name == "kernel32":
        sigs = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], H),
            "CloseHandle": ([H], wintypes.BOOL),
            "GetProcessTimes": ([H] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
        }
    elif name == "gdi32":
        sigs = {
            "CreateCompatibleDC": ([HDC], HDC),
            "DeleteDC": ([HDC], wintypes.BOOL),
            "CreateDIBSection": (
                [HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                 ctypes.POINTER(ctypes.c_void_p), H, wintypes.DWORD],
                wintypes.HBITMAP,
            ),
            "SelectObject": ([HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
            "DeleteObject": ([wintypes.HGDIOBJ], wintypes.BOOL),
            "BitBlt": (
                [HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                 HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD],
                wintypes.BOOL,
            ),
            "GdiFlush": ([], wintypes.BOOL),
        }
    elif name == "dwmapi":
        sigs = {
            "DwmGetWindowAttribute": ([HWND, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD], ctypes.c_long),
            "DwmRegisterThumbnail": ([HWND, HWND, ctypes.POINTER(H)], ctypes.c_long),
            "DwmUnregisterThumbnail": ([H], ctypes.c_long),
            "DwmQueryThumbnailSourceSize": ([H, ctypes.POINTER(wintypes.SIZE)], ctypes.c_long),
            "DwmUpdateThumbnailProperties": ([H, ctypes.POINTER(DWM_THUMBNAIL_PROPERTIES)], ctypes.c_long),
            "DwmFlush": ([], ctypes.c_long),
        }
    for fn, (args, res) in sigs.items():
        f = getattr(dll, fn)
        f.argtypes = args
        f.restype = res


def _dll(name: str):
    if not IS_WINDOWS:
        raise Win32Unavailable(f"{name}.dll is only available on Windows")
    dll = _DLLS.get(name)
    if dll is None:
        dll = ctypes.WinDLL(name, use_last_error=True)
        _declare(name, dll)
        _DLLS[name] = dll
    return dll


_dpi_done = False


def ensure_dpi_aware() -> None:
    """System DPI awareness, same as the vendored Enikk window module and input.py."""
    global _dpi_done
    if _dpi_done:
        return
    try:
        _dll("user32").SetProcessDPIAware()
    except Exception:
        pass
    _dpi_done = True


class per_monitor_dpi:
    """Run a block with PER_MONITOR_AWARE_V2 thread DPI so rects are physical pixels
    (what WGC / DWM / PrintWindow produce). No-op before Windows 10 1607."""

    def __enter__(self):
        self._prev = None
        try:
            fn = _dll("user32").SetThreadDpiAwarenessContext
        except (Win32Unavailable, AttributeError):
            return self
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_void_p
        self._fn = fn
        self._prev = fn(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2))
        return self

    def __exit__(self, *exc):
        if self._prev:
            self._fn(ctypes.c_void_p(self._prev))
        return False


def is_window(hwnd: int) -> bool:
    return bool(_dll("user32").IsWindow(int(hwnd)))


def is_iconic(hwnd: int) -> bool:
    return bool(_dll("user32").IsIconic(int(hwnd)))


def foreground_window() -> int:
    """HWND that receives keyboard input right now, 0 if none."""
    return int(_dll("user32").GetForegroundWindow() or 0)


def focus_hwnd() -> int | None:
    """hwndFocus of the foreground thread.

    None when GetGUIThreadInfo fails. 0 when the call succeeds and nothing has focus.
    Raises Win32Unavailable off Windows.
    """

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
    user32 = _dll("user32")
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL
    if not user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        return None
    return int(info.hwndFocus or 0)


def root_window(hwnd: int) -> int:
    """Top-level ancestor of hwnd via the parent chain (GA_ROOT; owners are not followed)."""
    if not hwnd:
        return 0
    return int(_dll("user32").GetAncestor(int(hwnd), GA_ROOT) or 0)


def window_from_point(x: int, y: int) -> int:
    """Window that would receive a mouse event at screen point (x, y), 0 if none."""
    return int(_dll("user32").WindowFromPoint(wintypes.POINT(int(x), int(y))) or 0)


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    if not _dll("user32").GetCursorPos(ctypes.byref(pt)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(pt.x), int(pt.y)


def owner_window(hwnd: int) -> int:
    return int(_dll("user32").GetWindow(int(hwnd), GW_OWNER) or 0) if hwnd else 0


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    n = _dll("user32").GetClassNameW(int(hwnd), buf, 256)
    return buf.value[:n] if n > 0 else ""


def window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    n = _dll("user32").GetWindowTextW(int(hwnd), buf, 512)
    return buf.value[:n] if n > 0 else ""


def is_window_visible(hwnd: int) -> bool:
    return bool(_dll("user32").IsWindowVisible(int(hwnd))) if hwnd else False


def is_child(parent: int, child: int) -> bool:
    """IsChild(parent, child): child is a descendant (WS_CHILD chain) of parent."""
    if not parent or not child:
        return False
    return bool(_dll("user32").IsChild(int(parent), int(child)))


def is_cloaked(hwnd: int) -> bool:
    """DWMWA_CLOAKED != 0 (a hidden UWP/virtual-desktop/cloaked window still IsWindowVisible)."""
    if not hwnd:
        return False
    try:
        val = wintypes.DWORD(0)
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(int(hwnd)), wintypes.DWORD(14), ctypes.byref(val), ctypes.sizeof(val)
        )
        return hr == 0 and bool(val.value)
    except Exception:
        return False


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25


def process_integrity_rid(pid: int) -> tuple[int | None, str | None]:
    """Mandatory integrity RID of a process (0x2000 medium, 0x3000 high, 0x4000 system).

    Returns (rid, None) or (None, error). Opening an elevated process's token from a
    medium process normally works (query-only); access denied is reported as an error.
    """
    if not pid:
        return None, "no pid"
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]
    adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    adv.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    hproc = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not hproc:
        return None, f"OpenProcess failed (winerror {ctypes.get_last_error()})"
    tok = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(hproc, _TOKEN_QUERY, ctypes.byref(tok)):
            return None, f"OpenProcessToken failed (winerror {ctypes.get_last_error()})"
        try:
            need = wintypes.DWORD(0)
            adv.GetTokenInformation(tok, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(need))
            buf = ctypes.create_string_buffer(max(int(need.value), 64))
            if not adv.GetTokenInformation(tok, _TOKEN_INTEGRITY_LEVEL, buf, len(buf), ctypes.byref(need)):
                return None, f"GetTokenInformation failed (winerror {ctypes.get_last_error()})"
            sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_MANDATORY_LABEL.Label.Sid
            count = adv.GetSidSubAuthorityCount(sid)[0]
            return int(adv.GetSidSubAuthority(sid, count - 1)[0]), None
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(hproc)


def window_thread(hwnd: int) -> int:
    """Thread that created hwnd, 0 if the window is gone."""
    return int(_dll("user32").GetWindowThreadProcessId(int(hwnd), None) or 0)


def window_pid(hwnd: int) -> int:
    """Owning PID of hwnd, 0 if the window is gone."""
    pid = wintypes.DWORD(0)
    tid = _dll("user32").GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    return int(pid.value) if tid else 0


def process_creation_time(pid: int) -> int | None:
    """Process creation FILETIME (100 ns since 1601) or None if it can't be read."""
    if not pid or int(pid) <= 0:
        return None
    k32 = _dll("kernel32")
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return value or None
    finally:
        k32.CloseHandle(handle)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    if not _dll("user32").GetWindowRect(int(hwnd), ctypes.byref(r)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def client_size(hwnd: int) -> tuple[int, int]:
    r = wintypes.RECT()
    if not _dll("user32").GetClientRect(int(hwnd), ctypes.byref(r)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(r.right - r.left), int(r.bottom - r.top)


def extended_frame_bounds(hwnd: int) -> tuple[int, int, int, int] | None:
    """Visible frame without invisible resize borders (always physical pixels)."""
    r = wintypes.RECT()
    hr = _dll("dwmapi").DwmGetWindowAttribute(
        int(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)
    )
    if hr != 0:
        return None
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def physical_geometry(hwnd: int) -> dict:
    """Window / visible frame / client rects in physical pixels (one coordinate space)."""
    from .capture import heuristic_client_rect

    with per_monitor_dpi():
        win = window_rect(hwnd)
        cw, ch = client_size(hwnd)
        frame = extended_frame_bounds(hwnd)
    return {"window": win, "frame": frame, "client": heuristic_client_rect(win, cw, ch)}


def _bitmapinfo(width: int, height: int) -> BITMAPINFO:
    bmi = BITMAPINFO()
    h = bmi.bmiHeader
    h.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    h.biWidth = int(width)
    h.biHeight = -int(height)
    h.biPlanes = 1
    h.biBitCount = 32
    h.biCompression = BI_RGB
    return bmi


def _grab_dc(draw, width: int, height: int):
    """Allocate a top-down 32bpp DIB, run draw(mem_dc) -> bool, return BGR ndarray."""
    import numpy as np

    u32, g32 = _dll("user32"), _dll("gdi32")
    screen = u32.GetDC(None)
    if not screen:
        raise ctypes.WinError(ctypes.get_last_error())
    mem = g32.CreateCompatibleDC(screen)
    bits = ctypes.c_void_p()
    bmp = g32.CreateDIBSection(screen, ctypes.byref(_bitmapinfo(width, height)), DIB_RGB_COLORS,
                               ctypes.byref(bits), None, 0)
    if not mem or not bmp or not bits.value:
        if bmp:
            g32.DeleteObject(bmp)
        if mem:
            g32.DeleteDC(mem)
        u32.ReleaseDC(None, screen)
        raise ctypes.WinError(ctypes.get_last_error())
    old = g32.SelectObject(mem, bmp)
    try:
        if not draw(mem):
            raise OSError("draw into DIB failed")
        g32.GdiFlush()
        raw = ctypes.string_at(bits.value, int(width) * int(height) * 4)
        arr = np.frombuffer(raw, dtype=np.uint8).reshape(int(height), int(width), 4)
        return np.ascontiguousarray(arr[:, :, :3])
    finally:
        g32.SelectObject(mem, old)
        g32.DeleteObject(bmp)
        g32.DeleteDC(mem)
        u32.ReleaseDC(None, screen)


def printwindow_capture(hwnd: int):
    """PrintWindow(PW_RENDERFULLCONTENT), then flag 0. Returns (bgr, window_rect_physical)."""
    u32 = _dll("user32")
    with per_monitor_dpi():
        rect = window_rect(hwnd)
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w <= 0 or h <= 0:
            raise OSError(f"empty window rect {rect}")

        def draw(mem):
            return bool(u32.PrintWindow(int(hwnd), mem, PW_RENDERFULLCONTENT)) or bool(
                u32.PrintWindow(int(hwnd), mem, 0)
            )

        return _grab_dc(draw, w, h), rect


def dwm_thumbnail_capture(hwnd: int):
    """Render the window's DWM thumbnail into an off-screen no-activate tool window and read it.

    Experimental: DWM composes the thumbnail into the desktop surface, not into the
    host window's bitmap, so this BitBlt reads black on Windows 11. Not in capture=auto.
    Returns (bgr, None); the caller matches the frame size against window/frame rects.
    """
    u32, g32, dwm = _dll("user32"), _dll("gdi32"), _dll("dwmapi")
    with per_monitor_dpi():
        dest = u32.CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, "Static", "", WS_POPUP,
            OFFSCREEN, OFFSCREEN, 1, 1, None, None, None, None,
        )
        if not dest:
            raise ctypes.WinError(ctypes.get_last_error())
        thumb = wintypes.HANDLE()
        try:
            hr = dwm.DwmRegisterThumbnail(dest, int(hwnd), ctypes.byref(thumb))
            if hr != 0 or not thumb:
                raise OSError(f"DwmRegisterThumbnail hr=0x{hr & 0xFFFFFFFF:08X}")
            size = wintypes.SIZE()
            hr = dwm.DwmQueryThumbnailSourceSize(thumb, ctypes.byref(size))
            w, h = int(size.cx), int(size.cy)
            if hr != 0 or w <= 0 or h <= 0:
                raise OSError(f"DwmQueryThumbnailSourceSize hr=0x{hr & 0xFFFFFFFF:08X} size={w}x{h}")
            props = DWM_THUMBNAIL_PROPERTIES()
            props.dwFlags = DWM_TNP_VISIBLE | DWM_TNP_RECTDESTINATION | DWM_TNP_SOURCECLIENTAREAONLY
            props.fVisible = True
            props.fSourceClientAreaOnly = False
            props.rcDestination = wintypes.RECT(0, 0, w, h)
            hr = dwm.DwmUpdateThumbnailProperties(thumb, ctypes.byref(props))
            if hr != 0:
                raise OSError(f"DwmUpdateThumbnailProperties hr=0x{hr & 0xFFFFFFFF:08X}")
            u32.SetWindowPos(dest, HWND_TOPMOST, OFFSCREEN, OFFSCREEN, w, h,
                             SWP_NOACTIVATE | SWP_NOSENDCHANGING)
            u32.ShowWindow(dest, SW_SHOWNOACTIVATE)
            u32.UpdateWindow(dest)
            dwm.DwmFlush()
            from .settings import get as _cfg

            time.sleep(float(_cfg("capture.dwm_settle_s")))
            src = u32.GetDC(dest)
            if not src:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                img = _grab_dc(lambda mem: bool(g32.BitBlt(mem, 0, 0, w, h, src, 0, 0, SRCCOPY)), w, h)
            finally:
                u32.ReleaseDC(dest, src)
            return img, None
        finally:
            if thumb:
                dwm.DwmUnregisterThumbnail(thumb)
            u32.ShowWindow(dest, SW_HIDE)
            u32.DestroyWindow(dest)
