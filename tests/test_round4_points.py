"""Round 3 Windows finding R1b: under --go, every input-method click, drag (start and
end) and scroll must land on the stamp window or a window it owns, and every keyboard
step re-checks the foreground, even after a click."""
from __future__ import annotations

import unittest
from unittest import mock

from cu_perceive.act import parse_steps, run_chain

HWND = 42
COVER = 6557602


def _window_frame(**extra):
    base = {
        "stamp": "20261019-074141-001",
        "hwnd": HWND,
        "coord_space": "window",
        "origin": {"left": 300, "top": 200, "width": 800, "height": 500},
        "width": 800,
        "height": 500,
        "items": [],
        "identity": {"hwnd": HWND, "pid": 4242, "ctime": "133000000000000000"},
    }
    base.update(extra)
    return base


def _screen_frame():
    return {"stamp": "20261019-074141-002", "hwnd": None, "coord_space": "screen",
            "origin": {"left": 0, "top": 0}, "width": 1000, "height": 1000, "items": []}


class _Desk(unittest.TestCase):
    """A fake desktop: self.covered is a set of screen points under COVER; the rest is
    the stamp window. Input is recorded, never sent."""

    def setUp(self):
        self.calls: list[str] = []
        self.covered: set[tuple[int, int]] = set()
        self.fg = HWND
        self.raised = False
        self.topmost = False
        patches = [
            mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}),
            mock.patch("cu_perceive.act.stamp_identity_error", return_value=None),
            mock.patch("cu_perceive.extras.missing_modules", return_value=[]),
            mock.patch("cu_perceive.windows.point_owner", side_effect=self._owner),
            mock.patch("cu_perceive.windows.foreground_matches", side_effect=lambda h: (self.fg == h, self.fg)),
            mock.patch("cu_perceive.windows.force_foreground", side_effect=self._raise),
            mock.patch("cu_perceive.input.click_xy", side_effect=self._click),
            mock.patch("cu_perceive.input.drag_path",
                       side_effect=lambda pts, step_px=4: self.calls.append("drag") or {"samples": len(pts)}),
            mock.patch("cu_perceive.input.scroll_xy", side_effect=lambda *a: self.calls.append("scroll")),
            mock.patch("cu_perceive.input.hover_xy", side_effect=lambda x, y: self.calls.append(f"hover:{x},{y}")),
            mock.patch("cu_perceive.input.tap_key", side_effect=lambda k: self.calls.append(f"key:{k}")),
            mock.patch("cu_perceive.input.enter_text",
                       side_effect=lambda t, via="paste": self.calls.append(f"type:{t}")),
            mock.patch("cu_perceive.input.clear_field_for_replace",
                       side_effect=lambda **k: self.calls.append("clear") or {
                           "ok": True, "method": "value_pattern", "empty": True, "verified": True}),
            mock.patch("cu_perceive.input.key_down", side_effect=lambda k: self.calls.append(f"down:{k}")),
            mock.patch("cu_perceive.input.key_up", side_effect=lambda k: self.calls.append(f"up:{k}")),
            mock.patch("time.sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _owner(self, hwnd, x, y):
        on_top = (x, y) in self.covered and (self.topmost or not self.raised)
        at = COVER if on_top else hwnd
        return at == hwnd, {"hwnd": at, "title": "EZCU-COVER" if at == COVER else "EZCU-TARGET"}

    def _raise(self, hwnd):
        self.calls.append(f"focus:{hwnd}")
        self.raised = True
        self.fg = hwnd
        return True

    def _click(self, x, y, **kw):
        self.calls.append(f"click:{x},{y}")
        on_top = (x, y) in self.covered and not self.raised
        self.fg = COVER if on_top else HWND

    def go(self, frame=None, focus=False, method="input", hold_key=None, **kw):
        return run_chain(frame or _window_frame(), parse_steps(**kw), dry_run=False, focus=focus,
                         method=method, hold_key=hold_key)


class TestCoveredClick(_Desk):
    def test_round3_repro_refused_and_nothing_sent(self):
        self.covered = {(1020, 550)}
        self.fg = COVER
        out = self.go(norm="900,700", text="hello", replace=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["pointer_refused_step"], 0)
        self.assertIn(f"covered by hwnd {COVER} 'EZCU-COVER'", out["error"])
        self.assertEqual(out["point_window"]["hwnd"], COVER)
        self.assertEqual(out["point_window"]["point"], [1020, 550])
        self.assertEqual(out["foreground_click_count"], 0)
        self.assertEqual(self.calls, [])

    def test_uncovered_point_clicks_then_types(self):
        out = self.go(norm="900,700", text="hello", replace=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["click:1020,550", "clear", "type:hello"])

    def test_focus_raises_then_clicks_target(self):
        self.covered = {(1020, 550)}
        self.fg = COVER
        out = self.go(norm="900,700", text="hello", replace=True, focus=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["focused_window"])
        self.assertEqual(self.calls, [f"focus:{HWND}", "click:1020,550", "clear", "type:hello"])

    def test_focus_still_covered_refuses(self):
        self.covered = {(1020, 550)}
        self.topmost = True
        out = self.go(norm="900,700", focus=True)
        self.assertFalse(out["ok"])
        self.assertIn("even after --focus", out["error"])
        self.assertEqual(self.calls, [f"focus:{HWND}"])

    def test_focus_rechecks_identity(self):
        self.covered = {(1020, 550)}
        with mock.patch("cu_perceive.act.stamp_identity_error", side_effect=[None, "HWND reused by pid 7"]):
            out = self.go(norm="900,700", focus=True)
        self.assertFalse(out["ok"])
        self.assertIn("after --focus failed", out["error"])
        self.assertEqual(self.calls, [f"focus:{HWND}"])

    def test_second_click_covered_stops_chain(self):
        self.covered = {(400, 300)}
        out = self.go(norm="900,700;125,200")
        self.assertFalse(out["ok"])
        self.assertEqual(out["pointer_refused_step"], 1)
        self.assertEqual(self.calls, ["click:1020,550"])
        self.assertEqual(out["foreground_click_count"], 1)

    def test_unreadable_point_fails_closed(self):
        with mock.patch("cu_perceive.windows.point_owner", side_effect=OSError("no user32")):
            out = self.go(norm="500,500")
        self.assertFalse(out["ok"])
        self.assertIn("could not check which window", out["error"])
        self.assertEqual(self.calls, [])


class TestDragAndScroll(_Desk):
    def test_drag_end_covered_refused(self):
        self.covered = {(500, 300)}
        out = self.go(drag="10,10,200,100")
        self.assertFalse(out["ok"])
        self.assertIn("drag end point (500,300)", out["error"])
        self.assertEqual(self.calls, [])

    def test_drag_start_covered_refused(self):
        self.covered = {(310, 210)}
        out = self.go(drag="10,10,200,100")
        self.assertIn("drag start point (310,210)", out["error"])
        self.assertEqual(self.calls, [])

    def test_drag_uncovered_runs(self):
        out = self.go(drag="10,10,200,100")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["drag"])

    def test_ctrl_scroll_on_covered_point_never_presses_ctrl(self):
        self.covered = {(700, 450)}
        out = self.go(norm="500,500", scroll="down:3", hold_key="ctrl")
        self.assertFalse(out["ok"])
        self.assertIn("scroll point (700,450)", out["error"])
        self.assertEqual(self.calls, ["hover:700,450"])

    def test_scroll_at_cursor_checks_cursor(self):
        self.covered = {(5, 5)}
        with mock.patch("cu_perceive._win32.cursor_pos", return_value=(5, 5)):
            out = self.go(scroll="3")
        self.assertFalse(out["ok"])
        self.assertIn("scroll (cursor) point (5,5)", out["error"])
        with mock.patch("cu_perceive._win32.cursor_pos", return_value=(6, 6)):
            out = self.go(scroll="3")
        self.assertTrue(out["ok"], out)

    def test_hold_key_released_after_later_refusal(self):
        self.covered = {(400, 300)}
        out = self.go(norm="900,700;125,200", hold_key="ctrl")
        self.assertFalse(out["ok"])
        self.assertEqual(self.calls, ["down:ctrl", "click:1020,550", "up:ctrl"])


class TestKeyboardAfterClick(_Desk):
    def test_waits_briefly_for_click_to_activate(self):
        seq = iter([COVER, COVER, HWND])
        with mock.patch("cu_perceive.windows.foreground_matches",
                        side_effect=lambda h: (lambda fg: (fg == h, fg))(next(seq, HWND))):
            out = self.go(norm="500,500", key="enter")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["click:700,450", "key:enter"])

    def test_focus_stolen_after_click_refuses(self):
        def click(x, y, **kw):
            self.calls.append("click")
            self.fg = COVER

        with mock.patch("cu_perceive.input.click_xy", side_effect=click):
            out = self.go(norm="500,500", text="hi")
        self.assertFalse(out["ok"])
        self.assertEqual(out["keyboard_refused_step"], 1)
        self.assertIn(str(COVER), out["error"])
        self.assertEqual(self.calls, ["click"])

    def test_every_keyboard_step_rechecked(self):
        def tap(k):
            self.calls.append(f"key:{k}")
            self.fg = COVER

        with mock.patch("cu_perceive.input.tap_key", side_effect=tap):
            out = self.go(norm="500,500", key="a,b")
        self.assertFalse(out["ok"])
        self.assertEqual(out["keyboard_refused_step"], 2)
        self.assertEqual(self.calls, ["click:700,450", "key:a"])


class TestScreenStamp(_Desk):
    def test_no_point_check_and_keys_follow_clicked_window(self):
        with mock.patch("cu_perceive.windows.point_owner") as owner, \
                mock.patch("cu_perceive.windows.window_at_point", return_value=77):
            self.fg = 77
            with mock.patch("cu_perceive.input.click_xy", side_effect=lambda *a, **k: self.calls.append("click")):
                out = self.go(_screen_frame(), norm="400,250", key="enter")
            self.assertTrue(out["ok"], out)
            owner.assert_not_called()
            self.assertEqual(self.calls, ["click", "key:enter"])

    def test_keys_refused_when_clicked_window_not_in_front(self):
        with mock.patch("cu_perceive.windows.window_at_point", return_value=77), \
                mock.patch("cu_perceive.input.click_xy", side_effect=lambda *a, **k: self.calls.append("click")):
            self.fg = 88
            out = self.go(_screen_frame(), norm="400,250", key="enter")
        self.assertFalse(out["ok"])
        self.assertIn("clicked window 77", out["error"])
        self.assertEqual(self.calls, ["click"])


def _choose(method):
    def enrich(frame, steps, double=False):
        for s in steps:
            s["chosen_method"] = method
    return enrich


class TestOtherMethods(_Desk):
    def test_auto_input_fallback_checks_point(self):
        self.covered = {(700, 450)}
        with mock.patch("cu_perceive.act.enrich_auto_steps", side_effect=_choose("input")):
            out = self.go(norm="500,500", method="auto")
        self.assertFalse(out["ok"])
        self.assertEqual(out["pointer_refused_step"], 0)
        self.assertEqual(self.calls, [])

    def test_auto_input_fallback_uncovered_clicks(self):
        with mock.patch("cu_perceive.act.enrich_auto_steps", side_effect=_choose("input")):
            out = self.go(norm="500,500", method="auto")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["click:700,450"])

    def test_auto_uia_unchanged(self):
        self.covered = {(700, 450)}
        with mock.patch("cu_perceive.act.enrich_auto_steps", side_effect=_choose("uia")), \
                mock.patch("cu_perceive.uia.resolve_at_screen",
                           return_value={"ok": True, "_control": object(), "preferred_action": "invoke"}), \
                mock.patch("cu_perceive.uia.invoke_control", return_value={"action": "invoke"}), \
                mock.patch("cu_perceive.windows.point_owner") as owner:
            out = self.go(norm="500,500", method="auto")
        self.assertTrue(out["ok"], out)
        owner.assert_not_called()

    def test_post_unchanged(self):
        self.covered = {(700, 450)}
        with mock.patch("cu_perceive.act.enrich_post_steps"), \
                mock.patch("cu_perceive.input.post_click_xy", return_value={}) as post, \
                mock.patch("cu_perceive.windows.point_owner") as owner:
            out = self.go(norm="500,500", method="post")
        self.assertTrue(out["ok"], out)
        post.assert_called_once()
        owner.assert_not_called()


class TestDryRun(_Desk):
    def test_warns_on_covered_points(self):
        self.covered = {(1020, 550), (500, 300)}
        out = run_chain(_window_frame(), parse_steps(norm="900,700", drag="10,10,200,100"), dry_run=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual([(o["step"], o["what"]) for o in out["occluded_points"]], [(0, "click"), (1, "drag end")])
        self.assertEqual(out["occluded_points"][0]["hwnd"], COVER)
        self.assertTrue(any("currently covered" in w and "EZCU-COVER" in w for w in out["warnings"]))
        self.assertEqual(self.calls, [])

    def test_clear_points_report_empty_list(self):
        out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=True)
        self.assertEqual(out["occluded_points"], [])
        self.assertNotIn("warnings", out)

    def test_unknown_off_windows(self):
        with mock.patch("cu_perceive.windows.point_owner", side_effect=OSError("no user32")):
            out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=True)
        self.assertTrue(out["ok"], out)
        self.assertNotIn("occluded_points", out)

    def test_post_dry_run_not_checked(self):
        self.covered = {(700, 450)}
        with mock.patch("cu_perceive.act.enrich_post_steps"):
            out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=True, method="post")
        self.assertNotIn("occluded_points", out)


class TestOwnership(unittest.TestCase):
    """windows.owned_by / point_owner against a fake window tree."""

    def setUp(self):
        self.owner = {HWND: 0, 100: HWND, 101: 100, COVER: 0, 300: 0}
        self.cls = {300: "#32768", COVER: "TkTopLevel"}
        self.thread = {HWND: 9, 300: 9, COVER: 5}
        patches = [
            mock.patch("cu_perceive._win32.root_window", side_effect=lambda h: h),
            mock.patch("cu_perceive._win32.owner_window", side_effect=lambda h: self.owner.get(h, 0)),
            mock.patch("cu_perceive._win32.class_name", side_effect=lambda h: self.cls.get(h, "")),
            mock.patch("cu_perceive._win32.window_thread", side_effect=lambda h: self.thread.get(h, 0)),
            mock.patch("cu_perceive._win32.window_title", side_effect=lambda h: f"w{h}"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_owned_by(self):
        from cu_perceive.windows import owned_by

        self.assertTrue(owned_by(HWND, HWND))
        self.assertTrue(owned_by(100, HWND))
        self.assertTrue(owned_by(101, HWND))
        self.assertTrue(owned_by(300, HWND))
        self.assertFalse(owned_by(COVER, HWND))
        self.assertFalse(owned_by(0, HWND))
        self.thread[300] = 5
        self.assertFalse(owned_by(300, HWND))

    def test_point_owner_names_window(self):
        from cu_perceive.windows import point_owner

        with mock.patch("cu_perceive._win32.window_from_point", return_value=COVER):
            self.assertEqual(point_owner(HWND, 1, 2), (False, {"hwnd": COVER, "title": f"w{COVER}"}))
        with mock.patch("cu_perceive._win32.window_from_point", return_value=101):
            self.assertTrue(point_owner(HWND, 1, 2)[0])



class TestOffWindows(unittest.TestCase):
    def test_point_owner_raises(self):
        from cu_perceive import _win32, windows

        if _win32.IS_WINDOWS:
            self.skipTest("Windows host")
        with self.assertRaises(_win32.Win32Unavailable):
            windows.point_owner(HWND, 1, 2)


if __name__ == "__main__":
    unittest.main()
