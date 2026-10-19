"""PR11: verified focus, CEF-safe occlusion, rebuilt HWND detection, opt-in check_change."""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

import numpy as np

from test_perceive_uia import _Rect

from cu_perceive import mcp_server
from cu_perceive.change import diff_images, run_with_change_check
from cu_perceive.core import stale_suspect_from_meta
from cu_perceive.uia import collect_uia_nodes, hwnd_occlusion
from cu_perceive.windows import focus_and_verify, hwnd_replacement


# ---------- P0-1 focus ----------
class TestFocusAndVerify(unittest.TestCase):
    def test_ok_first_try(self):
        calls = []
        r = focus_and_verify(10, raise_fn=calls.append, check_fn=lambda h: (True, h), sleep=lambda s: None)
        self.assertEqual(r, {"foreground_ok": True, "foreground_hwnd": 10, "focus_attempts": 1})
        self.assertEqual(calls, [10])

    def test_retry_then_ok(self):
        seq = iter([(False, 99), (True, 10)])
        r = focus_and_verify(10, raise_fn=lambda h: None, check_fn=lambda h: next(seq), sleep=lambda s: None)
        self.assertTrue(r["foreground_ok"])
        self.assertEqual(r["focus_attempts"], 2)

    def test_fails_after_one_retry(self):
        calls = []
        r = focus_and_verify(10, raise_fn=calls.append, check_fn=lambda h: (False, 99), sleep=lambda s: None)
        self.assertIs(r["foreground_ok"], False)
        self.assertEqual(r["foreground_hwnd"], 99)
        self.assertEqual(len(calls), 2)

    def test_owned_window_counts(self):
        # foreground_matches already accepts owned windows; focus_and_verify trusts it
        r = focus_and_verify(10, raise_fn=lambda h: None, check_fn=lambda h: (True, 55), sleep=lambda s: None)
        self.assertTrue(r["foreground_ok"])

    def test_unreadable_foreground_is_none(self):
        def boom(h):
            raise OSError("no win32")
        r = focus_and_verify(10, raise_fn=lambda h: None, check_fn=boom, sleep=lambda s: None)
        self.assertIsNone(r["foreground_ok"])

    def test_raise_error_does_not_skip_check(self):
        def boom(h):
            raise RuntimeError("SetForegroundWindow denied")
        r = focus_and_verify(10, raise_fn=boom, check_fn=lambda h: (True, 10), sleep=lambda s: None)
        self.assertTrue(r["foreground_ok"])


class TestPerceiveFocusedIsMeasured(unittest.TestCase):
    def _run(self, fv):
        from cu_perceive import core

        img = np.full((40, 60, 3), 128, np.uint8)

        class Res:
            image = img
            blank = False

            def meta(self):
                return {"backend": "wgc", "attempts": [{"backend": "wgc", "ok": True}]}

        origin = {"left": 0, "top": 0, "width": 60, "height": 40}
        with mock.patch("cu_perceive.windows.focus_and_verify", return_value=fv), \
                mock.patch("cu_perceive.windows.window_identity", return_value={"hwnd": 5, "pid": 1, "ctime": "x"}), \
                mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=False), \
                mock.patch("cu_perceive.windows.refresh_geom", return_value=(origin, {"hwnd": 5, "title": "T"})), \
                mock.patch("cu_perceive.windows.capture_window_image", return_value=Res()), \
                mock.patch("cu_perceive.windows.capture_looks_implausible", return_value=False), \
                mock.patch("cu_perceive.windows.verify_window_identity", return_value=None), \
                mock.patch("cu_perceive.windows.hwnd_replacement", return_value=None), \
                mock.patch("time.sleep"):
            return core._capture_pinned(5, focus=True, capture="auto")

    def test_focus_failed_reported_and_warned(self):
        res = self._run({"foreground_ok": False, "foreground_hwnd": 77, "focus_attempts": 2})
        self.assertTrue(res["ok"])
        self.assertIs(res["foreground_ok"], False)
        self.assertTrue(any("not the foreground" in w for w in res["warnings"]))

    def test_focus_ok(self):
        res = self._run({"foreground_ok": True, "foreground_hwnd": 5, "focus_attempts": 1})
        self.assertIs(res["foreground_ok"], True)
        self.assertFalse(any("not the foreground" in w for w in res["warnings"]))


class TestActFocusUpFront(unittest.TestCase):
    """focus=true on an uncovered click used to raise lazily (only when covered); now verified first."""

    def _go(self, fv):
        from test_round5 import _window_frame
        from cu_perceive.act import parse_steps, run_chain

        calls = []
        with mock.patch("cu_perceive.windows.focus_and_verify", return_value=fv) as m, \
                mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.act.stamp_identity_error", return_value=None), \
                mock.patch("cu_perceive.windows.point_owner", return_value=(True, {"hwnd": 42})), \
                mock.patch("cu_perceive.windows.foreground_matches", side_effect=lambda h: (True, h)), \
                mock.patch("cu_perceive.input.click_xy", side_effect=lambda x, y, **k: calls.append("click")), \
                mock.patch("time.sleep"):
            out = run_chain(_window_frame(), parse_steps(norm="500,500;600,600"), dry_run=False, focus=True)
        return out, calls, m

    def test_failed_focus_sends_nothing(self):
        out, calls, _ = self._go({"foreground_ok": False, "foreground_hwnd": 9, "focus_attempts": 2,
                                  "foreground_title": "Thief"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "focus_failed")
        self.assertIs(out["foreground_ok"], False)
        self.assertEqual(out["blocking_window"]["title"], "Thief")
        self.assertEqual(calls, [])

    def test_ok_focus_raises_once(self):
        out, calls, m = self._go({"foreground_ok": True, "foreground_hwnd": 42, "focus_attempts": 1})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["foreground_ok"])
        self.assertEqual(calls, ["click", "click"])
        self.assertEqual(m.call_count, 1)
        self.assertNotIn("_focus_verified", out)


# ---------- P0-3 occlusion ----------
class FakeProbe:
    """hwnds: 1 = target top-level (pid 100); 2 = its CEF render child; 3/4 = MDI children
    of 1; 31 = control inside 3; 9 = other app top-level (pid 200); 8 = own-process popup."""

    parent = {2: 1, 3: 1, 4: 1, 31: 3}
    pids = {1: 100, 9: 200, 8: 100, 7: 300}
    owners = {7: [1]}

    def __init__(self, at):
        self.at = at

    def window_at(self, x, y):
        return self.at

    def root(self, h):
        while h in self.parent:
            h = self.parent[h]
        return h

    def pid(self, h):
        return self.pids.get(self.root(h), 0)

    def _anc(self, h):
        out = []
        while h in self.parent:
            h = self.parent[h]
            out.append(h)
        return out

    def related(self, a, b):
        return a == b or a in self._anc(b) or b in self._anc(a)

    def owner_chain(self, h):
        return self.owners.get(h, [])


BOX = (10, 10, 50, 30)


class TestHwndOcclusion(unittest.TestCase):
    def test_cef_windowless_element_not_occluded(self):
        # DOM element has no own hwnd; nearest native hwnd is the render widget; hit = render widget
        self.assertEqual(hwnd_occlusion(1, 2, BOX, probe=FakeProbe(2)), (False, "self"))

    def test_cef_element_hit_on_top_level_not_occluded(self):
        self.assertFalse(hwnd_occlusion(1, 2, BOX, probe=FakeProbe(1))[0])

    def test_other_app_window_occludes(self):
        self.assertEqual(hwnd_occlusion(1, 2, BOX, probe=FakeProbe(9)), (True, "other_window"))

    def test_own_process_window_does_not_occlude(self):
        self.assertEqual(hwnd_occlusion(1, 2, BOX, probe=FakeProbe(8)), (False, "own_process_window"))

    def test_owned_window_does_not_occlude(self):
        self.assertEqual(hwnd_occlusion(1, 2, BOX, probe=FakeProbe(7)), (False, "owned_window"))

    def test_mdi_sibling_child_still_occludes(self):
        # control lives in MDI child 3 (hwnd 31), the click point hits MDI child 4
        self.assertEqual(hwnd_occlusion(1, 31, BOX, probe=FakeProbe(4)), (True, "sibling_child"))

    def test_descendant_hit_not_occluded(self):
        self.assertFalse(hwnd_occlusion(1, 3, BOX, probe=FakeProbe(31))[0])

    def test_dead_target_is_undecided(self):
        class Dead(FakeProbe):
            def valid(self, h):
                return False
        self.assertEqual(hwnd_occlusion(1, 2, BOX, probe=Dead(9)), (None, "no_target"))

    def test_no_hit_is_undecided(self):
        self.assertIsNone(hwnd_occlusion(1, 2, BOX, probe=FakeProbe(0))[0])


def _ctrl(name, rect, hwnd=0, children=()):
    class C:
        Name = name
        ControlTypeName = "ButtonControl"
        BoundingRectangle = rect
        NativeWindowHandle = hwnd

        def GetPattern(self, pid):
            return None

        def GetPropertyValue(self, pid):
            return False

        def GetRuntimeId(self):
            return (id(self),)

        def GetChildren(self):
            return list(children)

    return C()


class _R:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b


class TestWalkOccludedNames(unittest.TestCase):
    def _walk(self, at, **kw):
        a = _ctrl("Login", _R(0, 0, 40, 20))
        b = _ctrl("Search", _R(100, 0, 140, 20))
        render = _ctrl("", _R(0, 0, 160, 100), hwnd=2, children=[a, b])
        root = _ctrl("Edge", _R(0, 0, 160, 100), hwnd=1, children=[render])

        class Auto:
            class PatternId:
                ValuePattern = 1
                LegacyIAccessiblePattern = 2

            class PropertyId:
                ValueIsReadOnlyProperty = 30046

            def ControlFromHandle(self, h):
                return root

            def ControlFromPoint(self, x, y):
                return None

        class P(FakeProbe):
            def window_at(self, x, y):
                return at(x, y)

        with mock.patch("cu_perceive.uia._auto", return_value=Auto()), \
                mock.patch("cu_perceive.uia.list_available_patterns", return_value=["Invoke"]):
            return collect_uia_nodes(1, width=200, height=100, origin_left=0, origin_top=0,
                                     probe=P(0), **kw)

    def test_cef_nodes_survive(self):
        res = self._walk(lambda x, y: 2)
        self.assertEqual(res["uia_occluded"], 0)
        self.assertIn("Login", [n["name"] for n in res["uia_nodes"]])

    def test_other_window_drops_and_names(self):
        res = self._walk(lambda x, y: 9 if x > 90 else 2)
        names = [n["name"] for n in res["uia_nodes"]]
        self.assertNotIn("Search", names)
        self.assertIn("Login", names)
        self.assertEqual(res["uia_occluded_names"], ["Search"])
        self.assertEqual(res["uia_occluded_why"], {"other_window": 1})

    def test_include_occluded_keeps_them_marked(self):
        res = self._walk(lambda x, y: 9 if x > 90 else 2, include_occluded=True)
        by = {n["name"]: n for n in res["uia_nodes"]}
        self.assertTrue(by["Search"]["occluded"])
        self.assertNotIn("occluded", by["Login"])
        self.assertEqual(res["uia_occluded"], 1)

    def test_names_capped(self):
        from cu_perceive import uia

        self.assertEqual(uia.UIA_OCCLUDED_NAMES_MAX, 30)

    def test_all_occluded_warning_still_fires(self):
        from cu_perceive.core import uia_occlusion_warnings

        res = self._walk(lambda x, y: 9)
        warns, all_occ = uia_occlusion_warnings(res, res["uia_nodes"], True)
        self.assertTrue(all_occ)
        self.assertTrue(warns)


# ---------- P0-4 rebuilt hwnd ----------
ROWS = [
    {"hwnd": 200, "pid": 50, "class": "Qt5QWindow", "title": "App", "rect": [0, 0, 800, 600]},
    {"hwnd": 201, "pid": 50, "class": "Other", "title": "tip", "rect": [0, 0, 50, 20]},
    {"hwnd": 300, "pid": 60, "class": "Qt5QWindow", "title": "App", "rect": [0, 0, 900, 900]},
]
IDENT = {"hwnd": 100, "pid": 50, "ctime": "t", "class": "Qt5QWindow"}


class TestHwndReplacement(unittest.TestCase):
    def rep(self, st, ident=IDENT, title="App"):
        return hwnd_replacement(100, ident, title, probe=lambda h: st, rows=ROWS)

    def test_same_window(self):
        self.assertIsNone(self.rep({"exists": True, "class": "Qt5QWindow", "visible": True, "cloaked": False}))

    def test_destroyed_suggests_same_pid_same_class(self):
        r = self.rep({"exists": False})
        self.assertTrue(r["hwnd_replaced"])
        self.assertEqual(r["why"], "destroyed")
        self.assertEqual(r["suggested_hwnd"], 200)  # not 300: other pid

    def test_class_changed(self):
        r = self.rep({"exists": True, "class": "#32770", "visible": True})
        self.assertEqual(r["why"], "class_changed")
        self.assertTrue(r["hwnd_replaced"])

    def test_hidden_with_replacement(self):
        r = self.rep({"exists": True, "class": "Qt5QWindow", "visible": False})
        self.assertTrue(r["hwnd_replaced"])
        self.assertEqual(r["suggested_hwnd"], 200)

    def test_cloaked_counts_as_hidden(self):
        r = self.rep({"exists": True, "class": "Qt5QWindow", "visible": True, "cloaked": True})
        self.assertEqual(r["why"], "hidden")

    def test_hidden_without_replacement_is_not_replaced(self):
        r = hwnd_replacement(100, IDENT, "App", probe=lambda h: {"exists": True, "class": "Qt5QWindow",
                                                                  "visible": False}, rows=ROWS[2:])
        self.assertFalse(r["hwnd_replaced"])
        self.assertIsNone(r["suggested_hwnd"])

    def test_old_stamp_without_class_skips(self):
        self.assertIsNone(self.rep({"exists": True, "visible": False}, ident={"hwnd": 100, "pid": 50}))

    def test_check_stamp_refuses_with_suggestion(self):
        from cu_perceive import act

        frame = {"coord_space": "window", "hwnd": 100, "identity": IDENT, "window": {"title": "App"},
                 "origin": {"left": 0, "top": 0, "width": 800, "height": 600}}
        fake_gui = mock.MagicMock()
        fake_gui.IsWindow.return_value = True
        with mock.patch.dict("sys.modules", {"win32gui": fake_gui}), \
                mock.patch("cu_perceive.act.stamp_identity_error", return_value=None), \
                mock.patch("cu_perceive.windows.hwnd_replacement",
                           return_value={"hwnd_replaced": True, "why": "hidden", "suggested_hwnd": 200,
                                         "candidates": [{"hwnd": 200}]}):
            out = act.check_stamp(frame)
        self.assertFalse(out["ok"])
        self.assertTrue(out["hwnd_replaced"])
        self.assertEqual(out["suggested_hwnd"], 200)
        self.assertIs(out["identity_ok"], False)
        self.assertIn("suggested_hwnd=200", out["error"])


class TestStaleSuspect(unittest.TestCase):
    def test_fallback_after_wgc_failure(self):
        self.assertTrue(stale_suspect_from_meta({"backend": "printwindow", "attempts": [
            {"backend": "wgc", "ok": False, "error": "x"}, {"backend": "printwindow", "ok": True}]}))

    def test_wgc_ok_not_stale(self):
        self.assertFalse(stale_suspect_from_meta({"backend": "wgc", "attempts": [{"backend": "wgc", "ok": True}]}))

    def test_explicit_printwindow_not_stale(self):
        self.assertFalse(stale_suspect_from_meta({"backend": "printwindow", "attempts": [
            {"backend": "printwindow", "ok": True}]}))

    def test_wgc_skipped_unsafe_not_stale(self):
        self.assertFalse(stale_suspect_from_meta({"backend": "printwindow", "attempts": [
            {"backend": "wgc", "ok": False, "skipped": "unsafe"}, {"backend": "printwindow", "ok": True}]}))


# ---------- check_change ----------
class TestCheckChange(unittest.TestCase):
    def test_identical_is_unchanged(self):
        a = np.zeros((100, 100, 3), np.uint8)
        r = diff_images(a, a.copy())
        self.assertFalse(r["screen_changed"])
        self.assertEqual(r["changed_ratio"], 0.0)
        self.assertIsNone(r["changed_bbox_norm"])

    def test_region_change(self):
        a = np.zeros((100, 200, 3), np.uint8)
        b = a.copy()
        b[50:100, 100:200] = 255
        r = diff_images(a, b)
        self.assertTrue(r["screen_changed"])
        self.assertAlmostEqual(r["changed_ratio"], 0.25, places=2)
        self.assertEqual(r["changed_bbox_norm"], [500, 500, 1000, 1000])

    def test_checkbox_sized_change_counts(self):
        a = np.full((600, 800, 3), 240, np.uint8)
        b = a.copy()
        b[200:212, 100:112] = 0  # 12x12 tick in a 800x600 window: ratio ~0.0003
        r = diff_images(a, b)
        self.assertTrue(r["screen_changed"])
        self.assertLess(r["changed_ratio"], 0.001)

    def test_subtle_background_tint_counts(self):
        a = np.full((100, 100, 3), 255, np.uint8)
        b = a.copy()
        b[..., 0] = 238  # white -> #ffffee style tint (delta 17)
        self.assertTrue(diff_images(a, b)["screen_changed"])

    def test_real_tick_size_counts_but_specks_do_not(self):
        a = np.full((261, 504, 3), 240, np.uint8)
        b = a.copy()
        b[110:117, 24:27] = 0  # 21 px, the measured WinForms tick
        r = diff_images(a, b)
        self.assertTrue(r["screen_changed"])
        self.assertEqual(r["changed_pixels"], 21)
        c = a.copy()
        c[5, 5:10] = 0  # 5 stray pixels
        self.assertFalse(diff_images(a, c)["screen_changed"])

    def test_noise_below_threshold(self):
        a = np.full((50, 50, 3), 100, np.uint8)
        self.assertFalse(diff_images(a, a + 10)["screen_changed"])

    def test_size_change(self):
        r = diff_images(np.zeros((10, 10, 3), np.uint8), np.zeros((12, 10, 3), np.uint8))
        self.assertTrue(r["screen_changed"] and r["size_changed"])

    def test_run_wrapper_orders_grabs_around_run(self):
        log = []
        frames = iter([np.zeros((20, 20, 3), np.uint8), np.full((20, 20, 3), 255, np.uint8)])

        def grab(h):
            log.append("grab")
            return next(frames)

        def run():
            log.append("run")
            return {"ok": True}

        out = run_with_change_check(5, run, grab=grab, sleep=lambda s: None, wait_s=0)
        self.assertEqual(log, ["grab", "run", "grab"])
        self.assertTrue(out["screen_changed"])
        self.assertEqual(out["changed_ratio"], 1.0)

    def test_failed_grab_is_unknown(self):
        out = run_with_change_check(5, lambda: {"ok": True}, grab=lambda h: None, sleep=lambda s: None, wait_s=0)
        self.assertIsNone(out["screen_changed"])
        self.assertIn("check_change_note", out)

    def test_act_check_change_call_runs(self):
        """Regression: the MCP wrapper re-calls act(**params); free variables must not leak in."""
        from test_round9 import _collect_tools

        act = _collect_tools()["act"]
        out = act(stamp="no-such-stamp-pr11", norm="500,500", check_change=True)
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("check_change"), "skipped (dry run)")
        with mock.patch("cu_perceive.change.run_with_change_check",
                        side_effect=lambda h, run, **k: {**run(), "screen_changed": False}) as m:
            out = act(stamp="no-such-stamp-pr11", norm="500,500", go=True, check_change=True)
        self.assertTrue(m.called)
        self.assertIs(out["screen_changed"], False)

    def test_act_param_default_off(self):
        from test_round9 import _collect_tools

        tools = _collect_tools()
        sig = inspect.signature(tools["act"])
        self.assertIs(sig.parameters["check_change"].default, False)
        self.assertIs(inspect.signature(tools["perceive_window"]).parameters["uia_include_occluded"].default, False)


if __name__ == "__main__":
    unittest.main()
