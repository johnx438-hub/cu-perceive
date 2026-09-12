"""E3 same-stamp multi-step: check_stamp + run_chain stamp_check fields (no real UI)."""
from __future__ import annotations

import unittest
from unittest import mock

from cu_perceive.act import check_stamp, parse_steps, run_chain


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


class TestCheckStamp(unittest.TestCase):
    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("cu_perceive.windows.refresh_geom")
    def test_good_stamp_matching_origin(self, refresh, _is_win):
        refresh.return_value = (
            {"left": 100, "top": 50, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        out = check_stamp(_frame())
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["hwnd"], 42)
        self.assertEqual(out["origin_delta"]["left"], 0)
        self.assertEqual(out["origin_delta"]["top"], 0)

    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("cu_perceive.windows.refresh_geom")
    def test_moved_origin_fails(self, refresh, _is_win):
        refresh.return_value = (
            {"left": 200, "top": 50, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        out = check_stamp(_frame())
        self.assertFalse(out["ok"])
        self.assertIn("moved", out["error"])
        self.assertIn("re-perceive", out["error"])
        self.assertEqual(out["origin_delta"]["left"], 100)

    @mock.patch("win32gui.IsWindow", return_value=True)
    @mock.patch("cu_perceive.windows.refresh_geom")
    def test_slop_allows_small_jitter(self, refresh, _is_win):
        refresh.return_value = (
            {"left": 105, "top": 52, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        out = check_stamp(_frame(), origin_slop_px=8)
        self.assertTrue(out["ok"], out)

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
    @mock.patch("cu_perceive.windows.refresh_geom")
    def test_max_age_exceeded(self, refresh, _is_win):
        refresh.return_value = (
            {"left": 100, "top": 50, "width": 800, "height": 600},
            {"hwnd": 42},
        )
        # stamp far in the past relative to now — use a fixed old stamp
        out = check_stamp(_frame(stamp="20200101-000000"), max_age_sec=60)
        self.assertFalse(out["ok"])
        self.assertIn("max_age_sec", out["error"])
        self.assertIn("age_sec", out)

    @mock.patch("win32gui.IsWindow", return_value=False)
    def test_hwnd_gone(self, _is_win):
        out = check_stamp(_frame())
        self.assertFalse(out["ok"])
        self.assertIn("no longer exists", out["error"])


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
