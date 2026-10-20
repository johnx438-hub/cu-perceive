"""Per-window capture that works while the window is behind others. Never focuses.

Chain (default ``auto``): Windows.Graphics.Capture -> PrintWindow.
Each grab is cropped to the client area and resized to the client size the stamp
origin reports, so the 0-1000 map and origin+xy click math are unchanged. A
near-black grab counts as a miss (sampled like Piik's BitmapHasContent) and the
next backend is tried. Minimized windows are refused, never restored.

``screen`` is the legacy screen-region grab; only valid when the window was
explicitly brought to the front (perceive focus=true).

``dwm`` (DWM thumbnail into an off-screen host window, read back with BitBlt) is
experimental and not in ``auto``: DWM composes thumbnails into the desktop surface,
not into the host window's own bitmap, so the readback is all black (seen 3/3 on
Windows 11, occluded and not). It stays selectable for experiments only.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from .settings import get as _cfg

BACKEND_ORDER: tuple[str, ...] = ("wgc", "printwindow")
EXPERIMENTAL_BACKENDS: tuple[str, ...] = ("dwm",)
CAPTURE_CHOICES: tuple[str, ...] = ("auto", "wgc", "dwm", "printwindow", "screen")
DWM_EXPERIMENTAL_WARNING = (
    "capture=dwm is experimental: its readback is all black on Windows 11, so it is not "
    "part of capture=auto; use auto, wgc or printwindow"
)
CONTENT_THRESHOLD = 8
CONTENT_STEP = 4
FRAME_SLOP_PX = 2
WGC_TIMEOUT_S = 1.5


class CaptureError(RuntimeError):
    def __init__(self, message: str, attempts: list[dict] | None = None, reason: str | None = None,
                 hint: str | None = None):
        super().__init__(message)
        self.attempts = list(attempts or [])
        self.reason = reason
        self.hint = hint


class WindowMinimizedError(CaptureError):
    """Target is minimized: there is no live composited frame. Not restored."""


class BackendUnavailable(OSError):
    """Backend can't run here (package missing, OS too old, not Windows)."""


@dataclass
class CaptureResult:
    image: np.ndarray
    backend: str
    attempts: list[dict] = field(default_factory=list)
    blank: bool = False

    def meta(self) -> dict:
        out = {"backend": self.backend, "attempts": self.attempts}
        if self.blank:
            out["blank"] = True
        return out


def parse_capture_choice(value: str | None) -> tuple[str, ...]:
    """'auto' -> wgc then printwindow; 'wgc'|'dwm'|'printwindow'|'screen' -> that one only."""
    v = (value or "auto").strip().lower()
    if v not in CAPTURE_CHOICES:
        raise ValueError(f"capture must be one of {'|'.join(CAPTURE_CHOICES)} (got {value!r})")
    return BACKEND_ORDER if v == "auto" else (v,)


def frame_has_content(img: np.ndarray | None, threshold: int | None = None, step: int | None = None) -> bool:
    """True if any sampled pixel channel exceeds ``threshold`` (all-black == failed grab)."""
    if threshold is None:
        threshold = int(_cfg("capture.content_threshold"))
    if step is None:
        step = int(_cfg("capture.content_step"))
    if img is None or getattr(img, "size", 0) == 0:
        return False
    chans = img.shape[2] if img.ndim == 3 else 1
    flat = img.reshape(-1, chans)[::max(1, int(step)), : min(3, chans)]
    return bool((flat > threshold).any())


def heuristic_client_rect(window_rect, client_w: int, client_h: int) -> tuple[int, int, int, int] | None:
    """Client rect from window rect + client size, same rule as Enikk get_client_region
    (which produces the stamp origin), so crops line up with origin + xy clicks."""
    left, top, right, bottom = (int(v) for v in window_rect)
    width, height = right - left, bottom - top
    if client_w <= 0 or client_h <= 0:
        return None
    border_w = (width - int(client_w)) // 2
    border_h = height - int(client_h) - border_w
    cl, ct = left + border_w, top + border_h
    return cl, ct, cl + int(client_w), ct + int(client_h)


def _size(rect) -> tuple[int, int]:
    return int(rect[2]) - int(rect[0]), int(rect[3]) - int(rect[1])


def resolve_frame_rect(frame_w: int, frame_h: int, candidates, slop: int | None = None):
    """Pick which screen rect a captured frame covers.

    Returns (rect, sx, sy): the first candidate whose size matches the frame within
    ``slop`` px (scale 1), else the first candidate scaled to the frame size.
    """
    if slop is None:
        slop = int(_cfg("capture.frame_slop_px"))
    cands = [tuple(int(v) for v in c) for c in candidates if c]
    if not cands:
        raise CaptureError("no window rect to place the captured frame")
    for c in cands:
        cw, ch = _size(c)
        if abs(cw - frame_w) <= slop and abs(ch - frame_h) <= slop:
            return c, 1.0, 1.0
    c = cands[0]
    cw, ch = _size(c)
    if cw <= 0 or ch <= 0:
        raise CaptureError(f"degenerate window rect {c}")
    return c, frame_w / cw, frame_h / ch


def crop_to_client(img: np.ndarray, candidates, client_rect, out_size: tuple[int, int]) -> np.ndarray:
    """Cut the client area out of a window frame; pad (black) if it overhangs; resize to out_size."""
    import cv2

    if client_rect is None:
        raise CaptureError("window has no client area")
    fh, fw = img.shape[:2]
    rect, sx, sy = resolve_frame_rect(fw, fh, candidates)
    x1 = int(round((client_rect[0] - rect[0]) * sx))
    y1 = int(round((client_rect[1] - rect[1]) * sy))
    x2 = int(round((client_rect[2] - rect[0]) * sx))
    y2 = int(round((client_rect[3] - rect[1]) * sy))
    if x2 <= x1 or y2 <= y1:
        raise CaptureError(f"empty client crop {client_rect} in frame {rect}")
    if x2 <= 0 or y2 <= 0 or x1 >= fw or y1 >= fh:
        raise CaptureError(f"client rect {client_rect} outside captured frame {rect}")
    out = np.zeros((y2 - y1, x2 - x1, img.shape[2]), dtype=img.dtype)
    sx1, sy1, sx2, sy2 = max(0, x1), max(0, y1), min(fw, x2), min(fh, y2)
    out[sy1 - y1 : sy2 - y1, sx1 - x1 : sx2 - x1] = img[sy1:sy2, sx1:sx2]
    ow, oh = int(out_size[0]), int(out_size[1])
    if ow <= 0 or oh <= 0:
        raise CaptureError(f"bad output size {out_size}")
    if (out.shape[1], out.shape[0]) != (ow, oh):
        shrink = ow < out.shape[1] or oh < out.shape[0]
        out = cv2.resize(out, (ow, oh), interpolation=cv2.INTER_AREA if shrink else cv2.INTER_LINEAR)
    return np.ascontiguousarray(out)


WGC_UNSAFE_MODULES: tuple[str, ...] = ("onnxruntime",)
WGC_GUARD_HINT = (
    "use capture=printwindow (or auto, which falls back to it), or restart the server "
    "without CU_PERCEIVE_DETECT_INPROC=1 so OCR/YOLO run in the worker process"
)


def wgc_blocker() -> str | None:
    """Why WGC must not run in this process, or None.

    After onnxruntime is loaded, the next windows_capture start_free_threaded crashes
    the whole process (access violation), so WGC is skipped; PrintWindow is unaffected.
    perceive keeps OCR/YOLO in a child process (detect_worker), so this only triggers
    with CU_PERCEIVE_DETECT_INPROC=1 or when something else imported onnxruntime.
    """
    for mod in WGC_UNSAFE_MODULES:
        if mod in sys.modules:
            return (
                f"{mod} is loaded in this process and WGC would crash it; skipped "
                "(restart the process to use WGC again)"
            )
    return None


def _wgc_once(WindowsCapture, hwnd: int, draw_border, timeout: float):
    done = threading.Event()
    box: dict = {}
    cap = WindowsCapture(cursor_capture=False, draw_border=draw_border, window_hwnd=int(hwnd))

    def on_frame_arrived(frame, capture_control):
        if "img" not in box:
            box["img"] = np.array(frame.frame_buffer[:, :, :3], copy=True)
        capture_control.stop()
        done.set()

    def on_closed():
        done.set()

    cap.event(on_frame_arrived)
    cap.event(on_closed)
    control = cap.start_free_threaded()
    try:
        if not done.wait(timeout):
            raise TimeoutError(f"no WGC frame within {timeout:g}s")
    finally:
        try:
            control.stop()
        except Exception:
            pass
    if "img" not in box:
        raise CaptureError("WGC capture item closed before the first frame")
    return box["img"]


def wgc_capture(hwnd: int, timeout: float | None = None):
    """One full-resolution frame via Windows.Graphics.Capture (windows-capture wheel).

    Borderless is requested (Win11 / build 20348+); older builds may still show the
    yellow capture border for the ~frame it takes.
    """
    if timeout is None:
        timeout = float(_cfg("capture.wgc_timeout_s"))
    try:
        from windows_capture import WindowsCapture
    except ImportError as e:
        raise BackendUnavailable(f"windows-capture not importable: {e}") from e
    try:
        img = _wgc_once(WindowsCapture, hwnd, False, timeout)
    except TimeoutError:
        raise
    except Exception:
        img = _wgc_once(WindowsCapture, hwnd, None, timeout)
    return img, None


def _dwm(hwnd: int):
    from ._win32 import dwm_thumbnail_capture

    return dwm_thumbnail_capture(hwnd)


def _printwindow(hwnd: int):
    from ._win32 import printwindow_capture

    return printwindow_capture(hwnd)


BACKENDS = {"wgc": wgc_capture, "dwm": _dwm, "printwindow": _printwindow}

# Which rects a backend's frame usually covers, most likely first.
_FRAME_HINTS = {
    "wgc": ("frame", "window"),
    "dwm": ("window", "frame"),
    "printwindow": ("window", "frame"),
}


def _geometry(hwnd: int) -> dict:
    from ._win32 import physical_geometry

    return physical_geometry(hwnd)


def capture_window(
    hwnd: int,
    out_size: tuple[int, int],
    *,
    order: tuple[str, ...] = BACKEND_ORDER,
    is_minimized=None,
    geometry=None,
    backends: dict | None = None,
) -> CaptureResult:
    """Grab hwnd's client area without touching z-order or focus.

    ``out_size`` is the (width, height) of the stamp origin. Raises
    WindowMinimizedError for iconic windows and CaptureError when every backend
    fails; a frame that is blank on every backend is returned with blank=True.
    """
    if is_minimized is None:
        from .windows import is_hwnd_minimized as is_minimized
    if is_minimized(int(hwnd)):
        raise WindowMinimizedError(
            f"hwnd {int(hwnd)} is minimized; not restored (pass focus=true to restore it)"
        )
    geom = (geometry or _geometry)(int(hwnd))
    table = backends if backends is not None else BACKENDS
    attempts: list[dict] = []
    blank: tuple[str, np.ndarray] | None = None
    for name in order:
        fn = table.get(name)
        t0 = time.perf_counter()
        rec: dict = {"backend": name}
        if fn is None:
            rec.update(ok=False, error="unknown backend")
            attempts.append(rec)
            continue
        blocked = wgc_blocker() if name == "wgc" else None
        if blocked:
            rec.update(ok=False, skipped="unsafe", error=blocked)
            attempts.append(rec)
            continue
        try:
            raw, hint = fn(int(hwnd))
            cands = [hint] + [geom.get(k) for k in _FRAME_HINTS.get(name, ("window",))]
            img = crop_to_client(raw, cands, geom.get("client"), out_size)
            rec["frame"] = [int(raw.shape[1]), int(raw.shape[0])]
        except Exception as e:
            rec.update(ok=False, error=f"{type(e).__name__}: {e}")
            rec["ms"] = int(round((time.perf_counter() - t0) * 1000))
            attempts.append(rec)
            continue
        rec["ms"] = int(round((time.perf_counter() - t0) * 1000))
        if not frame_has_content(img):
            rec.update(ok=False, error="blank (all-black) frame")
            attempts.append(rec)
            if blank is None:
                blank = (name, img)
            continue
        rec["ok"] = True
        attempts.append(rec)
        return CaptureResult(img, name, attempts)
    if blank is not None:
        return CaptureResult(blank[1], blank[0], attempts, blank=True)
    if attempts and all(a.get("skipped") == "unsafe" for a in attempts):
        raise CaptureError(
            "capture=wgc refused by the onnxruntime guard: "
            + attempts[0]["error"]
            + f"; {WGC_GUARD_HINT}",
            attempts,
            reason="wgc_refused_onnxruntime_guard",
            hint=WGC_GUARD_HINT,
        )
    raise CaptureError("every window capture backend failed", attempts)


def capture_screen_region(origin: dict) -> CaptureResult:
    """Legacy grab of the on-screen client rect. Only meaningful for a front, unoccluded window."""
    import cv2
    from PIL import ImageGrab

    left, top = int(origin["left"]), int(origin["top"])
    w, h = int(origin["width"]), int(origin["height"])
    pil = ImageGrab.grab(bbox=(left, top, left + w, top + h), all_screens=True)
    bgr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    return CaptureResult(bgr, "screen", [{"backend": "screen", "ok": True}])
