"""E3 same-stamp multi-step: check_stamp + run_chain stamp_check fields (no real UI)."""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

# Stamp tests mock win32 APIs; provide a stub so they import on non-Windows.
if "win32gui" not in sys.modules:
    _win32gui = types.ModuleType("win32gui")
    _win32gui.IsWindow = lambda hwnd: True
    _win32gui.IsIconic = lambda hwnd: False
    _win32gui.GetWindowPlacement = lambda hwnd: (0, 1, (0, 0), (0, 0), (0, 0, 0, 0))
    sys.modules["win32gui"] = _win32gui

from cu_perceive.act import check_stamp, parse_steps, run_chain
from cu_perceive.windows import is_hwnd_minimized, live_geom


def _frame(**extra):
    base = {
        "stamp": "20260912-110000",
        "hwnd": 42,
        "coord_space": "window",
        "origin": {"left": 100, "top": 50, "width": 800, "height": 600},
        "width": 800,
        "height": 600,
        "items": [],
    }
    base.update(extra)
    return base


class TestIsHwndMinimized(unittest.TestCase):
    @mock.patch("win32gui.GetWindowPlacement")
    @mock.patch("win32gui.IsIconic", return_value=True)
    def test_iconic_true(self, _iconic, placement):
        self.assertTrue(is_hwnd_minimized(42))
        placement.assert_not_called()

    @mock.patch("win32gui.GetWindowPlacement", return_value=(0, 2, (0, 0), (0, 0), (10, 20, 800, 600)))
    @mock.patch("win32gui.IsIconic", return_value=False)
    def test_showcmd_showminimized(self, _iconic, _place):
        self.assertTrue(is_hwnd_minimized(42))

    @mock.patch("win32gui.GetWindowPlacement", return_value=(0, 1, (0, 0), (0, 0), (10, 20, 800, 600)))
    @mock.patch("win32gui.IsIconic", return_value=False)
    def test_normal_showcmd_not_minimized(self, _iconic, _place):
        self.assertFalse(is_hwnd_minimized(42))

    @mock.patch("win32gui.GetWindowPlacement", side_effect=OSError("no placement"))
    @mock.patch("win32gui.IsIconic", return_value=False)
    def test_placement_error_not_iconic(self, _iconic, _place):
        self.assertFalse(is_hwnd_minimized(42))


class TestLiveGeomNoRestore(unittest.TestCase):
    @mock.patch("cu_perceive.windows.find_window", return_value={"hwnd": 42})
    @mock.patch("cu_perceive.windows.client_origin")
    def test_does_not_force_foreground(self, origin, _find):
        origin.return_value = {"left": 100, "top": 50, "width": 800, "height": 600}
        out_o, win = live_geom(42)
        self.assertEqual(out_o["left"], 100)
        self.assertEqual(win["hwnd"], 42)
        origin.assert_called_once_with(42)
        _find.assert_called_once_with(hwnd=42)


class TestCheckStamp(unittest.TestCase):
    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=False)
    @mock.patch("cu_perceive.windows.refresh_geom")
    @mock.patch("cu_perceive.windows.live_geom")
    def test_good_stamp_matching_origin(self, live, refresh, _mini, _is_win):
        live.return_value = (
            {"left": 100, "top": 50, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        out = check_stamp(_frame())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["hwnd"], 42)
        self.assertEqual(out["origin_delta"]["left"], 0)
        self.assertEqual(out["origin_delta"]["top"], 0)
        live.assert_called_once_with(42)
        refresh.assert_not_called()

    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=False)
    @mock.patch("cu_perceive.windows.refresh_geom")
    @mock.patch("cu_perceive.windows.live_geom")
    def test_moved_origin_fails(self, live, refresh, _mini, _is_win):
        live.return_value = (
            {"left": 200, "top": 50, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        out = check_stamp(_frame())
        self.assertFalse(out["ok"])
        self.assertIn("moved", out["error"])
        self.assertIn("re-perceive", out["error"])
        self.assertEqual(out["origin_delta"]["left"], 100)
        refresh.assert_not_called()

    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=False)
    @mock.patch("cu_perceive.windows.refresh_geom")
    @mock.patch("cu_perceive.windows.live_geom")
    def test_slop_allows_small_jitter(self, live, refresh, _mini, _is_win):
        live.return_value = (
            {"left": 105, "top": 52, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        out = check_stamp(_frame(), origin_slop_px=8)
        self.assertTrue(out["ok"], out)
        refresh.assert_not_called()

    def test_missing_hwnd_window_space(self):
        out = check_stamp(_frame(hwnd=None))
        self.assertFalse(out["ok"])
        self.assertIn("hwnd", out["error"].lower())

    def test_verify_light_roi_fails(self):
        out = check_stamp(
            _frame(verify_light=True, roi=[10, 10, 100, 100], width=100, height=100)
        )
        self.assertFalse(out["ok"])
        self.assertIn("verify_light", out["error"])
        self.assertIn("full-window", out["error"])

    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=False)
    @mock.patch("cu_perceive.windows.refresh_geom")
    @mock.patch("cu_perceive.windows.live_geom")
    def test_max_age_exceeded(self, live, refresh, _mini, _is_win):
        live.return_value = (
            {"left": 100, "top": 50, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        # stamp far in the past relative to now — use a fixed old stamp
        out = check_stamp(_frame(stamp="20200101-000000"), max_age_sec=60)
        self.assertFalse(out["ok"])
        self.assertIn("max_age_sec", out["error"])
        self.assertIn("age_sec", out)
        refresh.assert_not_called()
        live.assert_not_called()

    @mock.patch("win32gui.IsWindow", return_value=False)
    def test_hwnd_gone(self, _is_win):
        out = check_stamp(_frame())
        self.assertFalse(out["ok"])
        self.assertIn("no longer exists", out["error"])

    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=True)
    @mock.patch("cu_perceive.windows.refresh_geom")
    @mock.patch("cu_perceive.windows.live_geom")
    def test_minimized_hwnd_fails_even_if_restore_rect_matches(self, live, refresh, _mini, _is_win):
        # Bug: refresh_geom restores iconic hwnds, so live origin matches stamp.
        live.return_value = (
            {"left": 100, "top": 50, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        refresh.return_value = live.return_value
        out = check_stamp(_frame())
        self.assertFalse(out["ok"])
        self.assertIn("minimized", out["error"])
        self.assertIn("re-perceive", out["error"])
        refresh.assert_not_called()
        live.assert_not_called()

    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("win32gui.IsIconic", return_value=True)
    @mock.patch("win32gui.GetWindowPlacement")
    @mock.patch("cu_perceive.windows.refresh_geom")
    @mock.patch("cu_perceive.windows.live_geom")
    def test_minimized_via_live_isiconic(self, live, refresh, placement, _iconic, _is_win):
        live.return_value = (
            {"left": 100, "top": 50, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        out = check_stamp(_frame())
        self.assertFalse(out["ok"])
        self.assertIn("minimized", out["error"])
        refresh.assert_not_called()
        live.assert_not_called()
        placement.assert_not_called()


class TestRunChainStampCheck(unittest.TestCase):
    def setUp(self):
        self.frame = _frame()

    @mock.patch("cu_perceive.act.check_stamp")
    def test_dry_multi_step_attaches_fields(self, ck):
        ck.return_value = {"ok": True, "stamp": self.frame["stamp"], "hwnd": 42}
        specs = parse_steps(steps="norm:80:40,norm:500:500")
        out = run_chain(self.frame, specs, dry_run=True, method="input")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["dry_run"])
        self.assertGreaterEqual(out["step_count"], 2)
        self.assertTrue(out["stamp_reuse"])
        self.assertTrue(out["stamp_check"]["ok"])
        self.assertEqual(len(out["steps"]), 2)

    @mock.patch("cu_perceive.act.check_stamp")
    def test_dry_bad_stamp_still_ok_without_strict(self, ck):
        ck.return_value = {
            "ok": False,
            "stamp": self.frame["stamp"],
            "hwnd": 42,
            "error": "window moved; re-perceive",
        }
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=True, method="input")
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["stamp_check"]["ok"])
        self.assertTrue(out["stamp_reuse"])

    @mock.patch("cu_perceive.act.check_stamp")
    def test_dry_strict_stamp_fails(self, ck):
        ck.return_value = {
            "ok": False,
            "stamp": self.frame["stamp"],
            "hwnd": 42,
            "error": "window moved; re-perceive",
        }
        specs = parse_steps(norm="500,500")
        out = run_chain(
            self.frame, specs, dry_run=True, method="input", strict_stamp=True
        )
        self.assertFalse(out["ok"])
        self.assertIn("re-perceive", out["error"])
        self.assertFalse(out["stamp_check"]["ok"])

    @mock.patch("cu_perceive.act.check_stamp")
    def test_go_refuses_bad_stamp(self, ck):
        ck.return_value = {
            "ok": False,
            "stamp": self.frame["stamp"],
            "hwnd": 42,
            "error": "ROI / verify_light stamp is verify-only",
        }
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=False, method="input")
        self.assertFalse(out["ok"])
        self.assertIn("verify_light", out["error"])
        # must not have clicked
        step = out["steps"][0]
        self.assertNotIn("clicked", step)

    @mock.patch("cu_perceive.act.check_stamp")
    def test_go_refuses_verify_light_via_real_check(self, _ck):
        # Use real check_stamp path for ROI (no win32 needed — fails before hwnd)
        _ck.side_effect = check_stamp
        frame = _frame(verify_light=True, roi=[0, 0, 50, 50], width=50, height=50)
        specs = parse_steps(steps="norm:100:100,norm:200:200")
        out = run_chain(frame, specs, dry_run=False, method="input")
        self.assertFalse(out["ok"])
        self.assertFalse(out["stamp_check"]["ok"])
        self.assertIn("verify_light", out["stamp_check"]["error"])

    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=True)
    @mock.patch("cu_perceive.windows.refresh_geom")
    @mock.patch("cu_perceive.windows.live_geom")
    def test_go_refuses_minimized_via_real_check(self, live, refresh, _mini, _is_win):
        live.return_value = (
            {"left": 100, "top": 50, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=False, method="input")
        self.assertFalse(out["ok"])
        self.assertFalse(out["stamp_check"]["ok"])
        self.assertIn("minimized", out["stamp_check"]["error"])
        self.assertIn("re-perceive", out["error"])
        self.assertNotIn("clicked", out["steps"][0])
        refresh.assert_not_called()
        live.assert_not_called()

    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=True)
    @mock.patch("cu_perceive.windows.refresh_geom")
    @mock.patch("cu_perceive.windows.live_geom")
    def test_strict_stamp_dry_run_fails_minimized(self, live, refresh, _mini, _is_win):
        live.return_value = (
            {"left": 100, "top": 50, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        specs = parse_steps(norm="500,500")
        out = run_chain(
            self.frame, specs, dry_run=True, method="input", strict_stamp=True
        )
        self.assertFalse(out["ok"])
        self.assertFalse(out["stamp_check"]["ok"])
        self.assertIn("minimized", out["error"])
        refresh.assert_not_called()
        live.assert_not_called()


class TestMultiNormParse(unittest.TestCase):
    def test_steps_multi_norm(self):
        specs = parse_steps(steps="norm:80:40,norm:500:500")
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0]["norm"], [80, 40])
        self.assertEqual(specs[1]["norm"], [500, 500])

    def test_norm_semicolon_multi(self):
        specs = parse_steps(norm="80,40;500,500")
        self.assertEqual(len(specs), 2)


if __name__ == "__main__":
    unittest.main()
