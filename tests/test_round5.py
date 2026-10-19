"""Round 4 Windows findings: --hold-key only presses into the stamp window (G1), the
keyboard check accepts the same owned windows as the point check (N1), and ghost /
degenerate client areas fail perceive (N2)."""
from __future__ import annotations

import unittest
from unittest import mock

import numpy as np
from test_perceive_background import ORIGIN, WIN, _Mocks

from cu_perceive.act import parse_steps, run_chain
from cu_perceive.capture import CaptureResult
from cu_perceive.core import perceive
from cu_perceive.windows import capture_geometry_problem

HWND = 42
COVER = 6557602
DIALOG = 2887724
SIBLING = 3000001


def _window_frame():
    return {
        "stamp": "20261019-081500-001",
        "hwnd": HWND,
        "coord_space": "window",
        "origin": {"left": 300, "top": 200, "width": 800, "height": 500},
        "width": 800,
        "height": 500,
        "items": [],
        "identity": {"hwnd": HWND, "pid": 4242, "ctime": "133000000000000000"},
    }


def _screen_frame():
    return {"stamp": "20261019-081500-002", "hwnd": None, "coord_space": "screen",
            "origin": {"left": 0, "top": 0}, "width": 1000, "height": 1000, "items": []}


class _Input(unittest.TestCase):
    """Stamp checks pass; input is recorded, never sent."""

    def setUp(self):
        self.calls: list[str] = []
        patches = [
            mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}),
            mock.patch("cu_perceive.act.stamp_identity_error", return_value=None),
            mock.patch("cu_perceive.input.click_xy", side_effect=lambda x, y, **k: self.calls.append(f"click:{x},{y}")),
            mock.patch("cu_perceive.input.hover_xy", side_effect=lambda x, y: self.calls.append("hover")),
            mock.patch("cu_perceive.input.scroll_xy", side_effect=lambda *a: self.calls.append("scroll")),
            mock.patch("cu_perceive.input.tap_key", side_effect=lambda k: self.calls.append(f"key:{k}")),
            mock.patch("cu_perceive.input.enter_text", side_effect=lambda t, via="paste": self.calls.append(f"type:{t}")),
            mock.patch("cu_perceive.input.key_down", side_effect=lambda k: self.calls.append(f"down:{k}")),
            mock.patch("cu_perceive.input.key_up", side_effect=lambda k: self.calls.append(f"up:{k}")),
            mock.patch("time.sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class TestHoldKeyTarget(_Input):
    def setUp(self):
        super().setUp()
        self.fg = COVER
        for p in (
            mock.patch("cu_perceive.windows.point_owner", return_value=(True, {"hwnd": HWND, "title": "T"})),
            mock.patch("cu_perceive.windows.foreground_matches", side_effect=lambda h: (self.fg == h, self.fg)),
            mock.patch("cu_perceive.windows.force_foreground", side_effect=self._raise),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _raise(self, hwnd):
        self.calls.append(f"focus:{hwnd}")
        self.fg = hwnd

    def go(self, frame=None, **kw):
        focus = kw.pop("focus", False)
        hold = kw.pop("hold_key", "ctrl")
        return run_chain(frame or _window_frame(), parse_steps(**kw), dry_run=False, hold_key=hold, focus=focus)

    def test_round4_repro_ctrl_scroll_refused_before_pressing(self):
        out = self.go(norm="500,500", scroll="down:3")
        self.assertFalse(out["ok"])
        self.assertEqual(out["hold_key_refused_step"], 1)
        self.assertIn("--hold-key ctrl refused", out["error"])
        self.assertIn(str(COVER), out["error"])
        self.assertEqual(self.calls, ["hover"])

    def test_alt_and_shift_click_refused(self):
        for hold in ("alt", "shift"):
            self.calls.clear()
            out = self.go(norm="500,500", hold_key=hold)
            self.assertFalse(out["ok"], hold)
            self.assertEqual(out["hold_key_refused_step"], 0)
            self.assertEqual(self.calls, [], hold)

    def test_stamp_window_in_front_presses(self):
        self.fg = HWND
        out = self.go(norm="500,500;600,600")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["down:ctrl", "click:700,450", "click:780,500", "up:ctrl"])

    def test_focus_raises_then_presses(self):
        out = self.go(norm="500,500", scroll="down:3", focus=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["focused_window"])
        self.assertEqual(self.calls, ["hover", f"focus:{HWND}", "down:ctrl", "scroll", "up:ctrl"])

    def test_screen_stamp_needs_window_under_point_in_front(self):
        with mock.patch("cu_perceive.windows.window_at_point", return_value=77):
            out = self.go(_screen_frame(), norm="400,250")
            self.assertFalse(out["ok"])
            self.assertIn("clicked window 77", out["error"])
            self.assertEqual(self.calls, [])
            self.fg = 77
            out = self.go(_screen_frame(), norm="400,250")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["down:ctrl", "click:400,250", "up:ctrl"])

    def test_no_hold_key_unaffected(self):
        out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["click:700,450"])


class _Tree(_Input):
    """Real point_owner / foreground_matches over a fake window tree:
    HWND (stamp) owns DIALOG; SIBLING is another top-level window on the same thread."""

    def setUp(self):
        super().setUp()
        self.fg = HWND
        self.at = HWND
        owners = {DIALOG: HWND}
        threads = {HWND: 9, DIALOG: 9, SIBLING: 9, COVER: 5}
        for p in (
            mock.patch("cu_perceive._win32.window_from_point", side_effect=lambda x, y: self.at),
            mock.patch("cu_perceive._win32.foreground_window", side_effect=lambda: self.fg),
            mock.patch("cu_perceive._win32.root_window", side_effect=lambda h: h),
            mock.patch("cu_perceive._win32.owner_window", side_effect=lambda h: owners.get(h, 0)),
            mock.patch("cu_perceive._win32.class_name", return_value="TkTopLevel"),
            mock.patch("cu_perceive._win32.window_thread", side_effect=lambda h: threads.get(h, 0)),
            mock.patch("cu_perceive._win32.window_title", side_effect=lambda h: f"w{h}"),
        ):
            p.start()
            self.addCleanup(p.stop)


class TestKeysInOwnedDialog(_Tree):
    def _click_opens(self, window):
        def click(x, y, **kw):
            self.calls.append("click")
            self.at = window
            self.fg = window
        return mock.patch("cu_perceive.input.click_xy", side_effect=click)

    def test_round4_n1_key_after_click_into_owned_dialog(self):
        with self._click_opens(DIALOG):
            out = run_chain(_window_frame(), parse_steps(norm="500,500;520,520", text="hi", key="enter"),
                            dry_run=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["click", "click", "type:hi", "key:enter"])

    def test_keys_to_unowned_sibling_still_refused(self):
        with self._click_opens(SIBLING):
            out = run_chain(_window_frame(), parse_steps(norm="500,500", key="enter"), dry_run=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["keyboard_refused_step"], 1)
        self.assertIn(str(SIBLING), out["error"])
        self.assertEqual(self.calls, ["click"])

    def test_keys_only_with_owned_dialog_in_front(self):
        self.fg = DIALOG
        out = run_chain(_window_frame(), parse_steps(key="enter"), dry_run=False)
        self.assertTrue(out["ok"], out)
        self.fg = COVER
        out = run_chain(_window_frame(), parse_steps(key="enter"), dry_run=False)
        self.assertFalse(out["ok"])

    def test_hold_key_with_owned_dialog_in_front(self):
        self.fg = DIALOG
        self.at = DIALOG
        out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=False, hold_key="ctrl")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["down:ctrl", "click:700,450", "up:ctrl"])


class TestGeometryProblem(unittest.TestCase):
    def test_cases(self):
        err, warn = capture_geometry_problem({"left": -32000, "top": -32000, "width": 144, "height": 1})
        self.assertIn("parked off-screen at (-32000,-32000) with a 144x1 client area", err)
        self.assertIsNone(warn)
        err, _ = capture_geometry_problem({"left": 10, "top": 10, "width": 144, "height": 1})
        self.assertIn("only 144x1 px", err)
        err, warn = capture_geometry_problem({"left": 10, "top": 10, "width": 60, "height": 300})
        self.assertIsNone(err)
        self.assertIn("60x300", warn)
        self.assertEqual(capture_geometry_problem(dict(ORIGIN)), (None, None))
        self.assertIn("only 640x2", capture_geometry_problem(dict(ORIGIN), (640, 2))[0])
        self.assertIsNotNone(capture_geometry_problem(None)[0])


GHOST = {"left": -32000, "top": -32000, "width": 144, "height": 1}


class TestGhostWindowPerceive(_Mocks):
    def _ghost(self, origin=GHOST, img=(1, 144)):
        self.m["live"].return_value = (dict(origin), dict(WIN))
        self.m["refresh"].return_value = (dict(origin), dict(WIN))
        self.m["capture"].return_value = CaptureResult(np.full(img + (3,), 90, np.uint8), "wgc", [])

    def test_focus_ghost_fails_without_stamp(self):
        self._ghost()
        self.m["implausible"].return_value = True
        out = perceive(hwnd=77, out_dir=str(self.out), focus=True)
        self.assertFalse(out["ok"])
        self.assertIsNone(out["stamp"])
        self.assertIn("parked off-screen", out["error"])
        self.assertIn("even after --focus", out["error"])
        self.assertEqual(out["degenerate_client"]["image"], [144, 1])
        self.assertEqual(list(self.out.glob("*.json")), [])

    def test_no_focus_ghost_suggests_focus(self):
        self._ghost()
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.assertFalse(out["ok"])
        self.assertIn("--focus restores and raises it", out["error"])
        self.assertEqual(list(self.out.glob("*")), [])

    def test_on_screen_but_one_pixel_high(self):
        self._ghost({"left": 300, "top": 200, "width": 144, "height": 1})
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.assertFalse(out["ok"])
        self.assertIn("only 144x1 px", out["error"])

    def test_small_window_warns(self):
        self._ghost({"left": 300, "top": 200, "width": 400, "height": 60}, img=(60, 400))
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.assertTrue(out.get("ok", True), out)
        self.assertIsNotNone(out["stamp"])
        self.assertTrue(any("400x60" in w for w in out["warnings"]))

    def test_normal_window_unchanged(self):
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.assertTrue(out.get("ok", True), out)
        self.assertNotIn("warnings", out)


if __name__ == "__main__":
    unittest.main()
