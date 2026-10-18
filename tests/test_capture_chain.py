"""Background capture chain: geometry, blank detection, fallback order (no real Win32)."""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

import numpy as np

from cu_perceive import capture as cap
from cu_perceive.capture import (
    BACKEND_ORDER,
    CaptureError,
    WindowMinimizedError,
    capture_window,
    crop_to_client,
    frame_has_content,
    heuristic_client_rect,
    parse_capture_choice,
    resolve_frame_rect,
)


def _img(w, h, value=0):
    return np.full((h, w, 3), value, dtype=np.uint8)


def _marked(w, h):
    """Each pixel encodes its own (x, y) so crops can be checked exactly."""
    ys, xs = np.mgrid[0:h, 0:w]
    out = np.zeros((h, w, 3), dtype=np.uint8)
    out[..., 0] = xs % 256
    out[..., 1] = ys % 256
    out[..., 2] = 200
    return out


# Window rect with 8px invisible borders, 31px title bar; client 800x600.
WINDOW = (92, 11, 908, 650)
FRAME = (100, 11, 900, 642)
CLIENT = heuristic_client_rect(WINDOW, 800, 600)
GEOM = {"window": WINDOW, "frame": FRAME, "client": CLIENT}


class TestFrameHasContent(unittest.TestCase):
    def test_black_is_blank(self):
        self.assertFalse(frame_has_content(_img(64, 64, 0)))

    def test_near_black_is_blank(self):
        self.assertFalse(frame_has_content(_img(64, 64, 8)))

    def test_content(self):
        img = _img(64, 64, 0)
        img[10:20, 10:20] = 255
        self.assertTrue(frame_has_content(img))

    def test_none_and_empty(self):
        self.assertFalse(frame_has_content(None))
        self.assertFalse(frame_has_content(np.zeros((0, 0, 3), dtype=np.uint8)))


class TestGeometry(unittest.TestCase):
    def test_heuristic_matches_enikk_rule(self):
        # border_w = (816-800)//2 = 8; border_h = 639-600-8 = 31
        self.assertEqual(CLIENT, (100, 42, 900, 642))

    def test_heuristic_no_client(self):
        self.assertIsNone(heuristic_client_rect(WINDOW, 0, 600))

    def test_resolve_prefers_size_match(self):
        rect, sx, sy = resolve_frame_rect(800, 631, [FRAME, WINDOW])
        self.assertEqual(rect, FRAME)
        self.assertEqual((sx, sy), (1.0, 1.0))
        rect, _, _ = resolve_frame_rect(816, 639, [FRAME, WINDOW])
        self.assertEqual(rect, WINDOW)

    def test_resolve_scales_unmatched(self):
        rect, sx, sy = resolve_frame_rect(1600, 1262, [FRAME, WINDOW])
        self.assertEqual(rect, FRAME)
        self.assertAlmostEqual(sx, 2.0)
        self.assertAlmostEqual(sy, 2.0)

    def test_resolve_no_candidates(self):
        with self.assertRaises(CaptureError):
            resolve_frame_rect(10, 10, [None, None])


class TestCropToClient(unittest.TestCase):
    def test_wgc_frame_crop_keeps_origin_pixel(self):
        frame = _marked(800, 631)  # covers FRAME
        out = crop_to_client(frame, [FRAME, WINDOW], CLIENT, (800, 600))
        self.assertEqual(out.shape[:2], (600, 800))
        # client (100,42) is frame pixel (0,31)
        self.assertEqual(int(out[0, 0, 0]), 0)
        self.assertEqual(int(out[0, 0, 1]), 31)
        self.assertEqual(int(out[599, 799, 0]), 799 % 256)

    def test_printwindow_frame_crop(self):
        frame = _marked(816, 639)  # covers WINDOW incl. invisible borders
        out = crop_to_client(frame, [WINDOW, FRAME], CLIENT, (800, 600))
        self.assertEqual(int(out[0, 0, 0]), 8)
        self.assertEqual(int(out[0, 0, 1]), 31)

    def test_scaled_frame_resized_to_origin(self):
        frame = _img(1600, 1262, 120)  # 2x physical vs stamp space
        out = crop_to_client(frame, [FRAME], CLIENT, (800, 600))
        self.assertEqual(out.shape[:2], (600, 800))

    def test_overhang_padded_not_stretched(self):
        frame = _img(800, 620, 200)  # 11 px short at the bottom
        out = crop_to_client(frame, [(100, 11, 900, 631)], CLIENT, (800, 600))
        self.assertEqual(out.shape[:2], (600, 800))
        self.assertEqual(int(out[0, 0, 0]), 200)
        self.assertEqual(int(out[599, 0, 0]), 0)

    def test_outside_frame_raises(self):
        with self.assertRaises(CaptureError):
            crop_to_client(_img(100, 100, 5), [(5000, 5000, 5100, 5100)], CLIENT, (800, 600))


class TestCaptureChain(unittest.TestCase):
    def _run(self, backends, order=BACKEND_ORDER, minimized=False):
        return capture_window(
            42, (800, 600), order=order,
            is_minimized=lambda h: minimized,
            geometry=lambda h: GEOM,
            backends=backends,
        )

    def test_wgc_wins(self):
        calls = []
        backends = {
            "wgc": lambda h: (calls.append("wgc") or _img(800, 631, 90), None),
            "dwm": lambda h: (calls.append("dwm") or _img(816, 639, 90), None),
        }
        res = self._run(backends)
        self.assertEqual(res.backend, "wgc")
        self.assertEqual(calls, ["wgc"])
        self.assertEqual(res.image.shape[:2], (600, 800))
        self.assertTrue(res.attempts[0]["ok"])

    def test_falls_through_error_and_blank(self):
        def boom(h):
            raise OSError("WGC unsupported")

        backends = {
            "wgc": boom,
            "dwm": lambda h: (_img(816, 639, 0), None),
            "printwindow": lambda h: (_img(816, 639, 77), WINDOW),
        }
        res = self._run(backends)
        self.assertEqual(res.backend, "printwindow")
        self.assertFalse(res.blank)
        self.assertEqual([a["backend"] for a in res.attempts], ["wgc", "dwm", "printwindow"])
        self.assertIn("WGC unsupported", res.attempts[0]["error"])
        self.assertIn("blank", res.attempts[1]["error"])

    def test_all_blank_returns_first_blank_flagged(self):
        backends = {n: (lambda h: (_img(816, 639, 0), WINDOW)) for n in BACKEND_ORDER}
        res = self._run(backends)
        self.assertTrue(res.blank)
        self.assertEqual(res.backend, "wgc")
        self.assertTrue(res.meta()["blank"])

    def test_all_fail_raises_with_attempts(self):
        def boom(h):
            raise OSError("nope")

        with self.assertRaises(CaptureError) as cm:
            self._run({n: boom for n in BACKEND_ORDER})
        self.assertEqual(len(cm.exception.attempts), 3)

    def test_minimized_refused_without_any_backend(self):
        called = []
        backends = {n: (lambda h: called.append(1)) for n in BACKEND_ORDER}
        with self.assertRaises(WindowMinimizedError) as cm:
            self._run(backends, minimized=True)
        self.assertIn("not restored", str(cm.exception))
        self.assertEqual(called, [])

    def test_single_backend_order(self):
        backends = {
            "wgc": lambda h: self.fail("wgc must not run"),
            "dwm": lambda h: (_img(816, 639, 50), None),
        }
        res = self._run(backends, order=("dwm",))
        self.assertEqual(res.backend, "dwm")


class TestParseCaptureChoice(unittest.TestCase):
    def test_choices(self):
        self.assertEqual(parse_capture_choice(None), BACKEND_ORDER)
        self.assertEqual(parse_capture_choice("AUTO"), BACKEND_ORDER)
        self.assertEqual(parse_capture_choice("dwm"), ("dwm",))
        self.assertEqual(parse_capture_choice("screen"), ("screen",))
        with self.assertRaises(ValueError):
            parse_capture_choice("mss")


class _FakeFrame:
    def __init__(self, w, h, value):
        self.frame_buffer = np.full((h, w, 4), value, dtype=np.uint8)


class _FakeControl:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def _fake_windows_capture(*, deliver=True, fail_border=False):
    seen: list[dict] = []

    class WindowsCapture:
        def __init__(self, **kw):
            seen.append(kw)
            if fail_border and kw.get("draw_border") is False:
                raise RuntimeError("border toggle unsupported")
            self.handlers = {}

        def event(self, fn):
            self.handlers[fn.__name__] = fn
            return fn

        def start_free_threaded(self):
            control = _FakeControl()
            if deliver:
                self.handlers["on_frame_arrived"](_FakeFrame(800, 631, 99), _FakeControl())
            return control

    mod = types.ModuleType("windows_capture")
    mod.WindowsCapture = WindowsCapture
    return mod, seen


class TestWgcBackend(unittest.TestCase):
    def test_single_frame_by_hwnd_borderless_no_cursor(self):
        mod, seen = _fake_windows_capture()
        with mock.patch.dict(sys.modules, {"windows_capture": mod}):
            img, hint = cap.wgc_capture(4242, timeout=0.5)
        self.assertIsNone(hint)
        self.assertEqual(img.shape, (631, 800, 3))
        self.assertEqual(seen[0]["window_hwnd"], 4242)
        self.assertIs(seen[0]["draw_border"], False)
        self.assertIs(seen[0]["cursor_capture"], False)

    def test_border_unsupported_retries_default(self):
        mod, seen = _fake_windows_capture(fail_border=True)
        with mock.patch.dict(sys.modules, {"windows_capture": mod}):
            img, _ = cap.wgc_capture(1, timeout=0.5)
        self.assertEqual([s["draw_border"] for s in seen], [False, None])
        self.assertEqual(img.shape[2], 3)

    def test_timeout(self):
        mod, _ = _fake_windows_capture(deliver=False)
        with mock.patch.dict(sys.modules, {"windows_capture": mod}):
            with self.assertRaises(TimeoutError):
                cap.wgc_capture(1, timeout=0.05)

    def test_package_missing(self):
        with mock.patch.dict(sys.modules, {"windows_capture": None}):
            with self.assertRaises(cap.BackendUnavailable):
                cap.wgc_capture(1)


class TestCaptureWindowImage(unittest.TestCase):
    ORIGIN = {"left": 100, "top": 42, "width": 800, "height": 600}

    def test_screen_needs_focus(self):
        from cu_perceive.windows import capture_window_image

        with self.assertRaises(CaptureError):
            capture_window_image(1, self.ORIGIN, capture="screen", focused=False)

    @mock.patch("cu_perceive.capture.capture_screen_region")
    @mock.patch("cu_perceive.capture.capture_window", side_effect=CaptureError("all failed", [{"backend": "wgc"}]))
    def test_unfocused_failure_never_grabs_screen(self, _cw, screen):
        from cu_perceive.windows import capture_window_image

        with self.assertRaises(CaptureError):
            capture_window_image(1, self.ORIGIN, capture="auto", focused=False)
        screen.assert_not_called()

    @mock.patch("cu_perceive.capture.capture_screen_region")
    @mock.patch("cu_perceive.capture.capture_window", side_effect=CaptureError("all failed", [{"backend": "wgc"}]))
    def test_focused_failure_falls_back_to_screen(self, _cw, screen):
        from cu_perceive.capture import CaptureResult
        from cu_perceive.windows import capture_window_image

        screen.return_value = CaptureResult(_img(800, 600, 9), "screen", [{"backend": "screen", "ok": True}])
        res = capture_window_image(1, self.ORIGIN, capture="auto", focused=True)
        self.assertEqual(res.backend, "screen")
        self.assertEqual([a["backend"] for a in res.attempts], ["wgc", "screen"])

    @mock.patch("cu_perceive.capture.capture_screen_region")
    @mock.patch("cu_perceive.capture.capture_window", side_effect=WindowMinimizedError("min"))
    def test_minimized_never_falls_back(self, _cw, screen):
        from cu_perceive.windows import capture_window_image

        with self.assertRaises(WindowMinimizedError):
            capture_window_image(1, self.ORIGIN, capture="auto", focused=True)
        screen.assert_not_called()


class TestWin32ImportGuard(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "non-Windows guard")
    def test_calls_raise_off_windows(self):
        from cu_perceive import _win32

        with self.assertRaises(_win32.Win32Unavailable):
            _win32.window_pid(1)
        with self.assertRaises(_win32.Win32Unavailable):
            _win32.printwindow_capture(1)


if __name__ == "__main__":
    unittest.main()
