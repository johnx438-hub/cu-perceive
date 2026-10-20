"""PR13: reflow by bbox span, ctrl-key window polling, min-ratio / change_minor, foreground_elevated."""
import unittest
from unittest import mock

import numpy as np

from cu_perceive import act as act_mod
from cu_perceive import change


def _img(h=400, w=1000):
    return np.full((h, w, 3), 255, dtype=np.uint8)


class TestReflowReason(unittest.TestCase):
    def test_retest_data(self):
        r = change.reflow_reason
        self.assertEqual(r(0.167, [0, 0, 1000, 1000]), "bbox_span")  # WPS white page swap
        self.assertEqual(r(0.034, [0, 30, 1000, 930]), "bbox_span")  # WPS list swap, bbox ~90%
        self.assertIsNone(r(0.00055, [400, 400, 480, 430]))  # hover highlight
        self.assertEqual(r(0.081, [10, 20, 990, 980]), "bbox_span")  # Xmind node drop re-layout
        self.assertEqual(r(0.35, [0, 0, 500, 700]), "ratio")  # old rule still triggers
        self.assertIsNone(r(0.01, [0, 0, 1000, 1000]))  # caret + clock far apart: spans but too few px
        self.assertIsNone(r(0.0056, [51, 62, 801, 449]))  # slider drag
        self.assertEqual(r(0.19111, [14, 112, 679, 969]), "bbox_span")  # live page swap with margins
        self.assertIsNone(r(0.05, [0, 0, 700, 700]))  # mid box, too few px
        self.assertIsNone(r(0.00404, [298, 25, 398, 67]))  # live hover
        self.assertIsNone(r(0.00203, [11, 17, 199, 75]))  # live checkbox + focus ring
        self.assertIsNone(r(None, None))

    def test_bbox_share(self):
        self.assertEqual(change.bbox_share([0, 0, 1000, 1000]), 1.0)
        self.assertEqual(change.bbox_share([0, 0, 500, 500]), 0.25)
        self.assertIsNone(change.bbox_share(None))

    def test_run_reports_reason(self):
        a = _img()
        b = a.copy()
        b[::8, :, :] = 0  # sparse full-width change: ~12.5% of pixels, bbox whole window
        imgs = iter([a, b])
        out = change.run_with_change_check(5, lambda: {"ok": True}, grab=lambda h: next(imgs), sleep=lambda s: None)
        self.assertTrue(out["layout_reflow_suspected"])
        self.assertEqual(out["reflow_reason"], "bbox_span")
        self.assertGreater(out["changed_bbox_share"], 0.9)
        self.assertTrue(any("spanning" in w for w in out["warnings"]))

    def test_small_change_no_reflow(self):
        a = _img()
        b = a.copy()
        b[100:110, 100:110] = 0
        imgs = iter([a, b])
        out = change.run_with_change_check(5, lambda: {"ok": True}, grab=lambda h: next(imgs), sleep=lambda s: None)
        self.assertFalse(out["layout_reflow_suspected"])
        self.assertNotIn("reflow_reason", out)


class TestMinRatio(unittest.TestCase):
    def test_minor_flag(self):
        a = _img()
        b = a.copy()
        b[0:5, 0:5] = 0  # 25 px of 400k = 0.0000625
        res = change.diff_images(a, b)
        self.assertTrue(res["screen_changed"])
        self.assertTrue(res["change_minor"])

    def test_below_floor(self):
        a = _img()
        b = a.copy()
        b[0:5, 0:5] = 0
        res = change.diff_images(a, b, min_ratio=0.0003)
        self.assertFalse(res["screen_changed"])
        self.assertTrue(res["below_min_ratio"])
        self.assertEqual(res["change_min_ratio"], 0.0003)

    def test_not_minor_when_large(self):
        a = _img()
        b = a.copy()
        b[0:40, 0:40] = 0  # 0.004
        res = change.diff_images(a, b)
        self.assertTrue(res["screen_changed"])
        self.assertNotIn("change_minor", res)

    def test_env_floor(self):
        with mock.patch.dict("os.environ", {"CU_PERCEIVE_CHANGE_MIN_RATIO": "0.01"}):
            self.assertEqual(change.change_min_ratio(), 0.01)
        with mock.patch.dict("os.environ", {"CU_PERCEIVE_CHANGE_MIN_RATIO": "x"}):
            self.assertEqual(change.change_min_ratio(), change.CHANGE_MIN_RATIO)

    def test_run_passes_min_ratio(self):
        a = _img()
        b = a.copy()
        b[0:5, 0:5] = 0
        imgs = iter([a, b])
        out = change.run_with_change_check(5, lambda: {"ok": True}, grab=lambda h: next(imgs),
                                           sleep=lambda s: None, min_ratio=0.001)
        self.assertFalse(out["screen_changed"])


class TestCtrlKeys(unittest.TestCase):
    def test_ctrl_letters(self):
        k = act_mod._key_may_open
        for s in ("ctrl+p", "ctrl+o", "ctrl+shift+s", "control+n", "ctrl+1"):
            self.assertTrue(k(s), s)
        for s in ("ctrl+pagedown", "ctrl+end", "shift+a", "a", "ctrl", "tab", "ctrl+c", "ctrl+a"):
            self.assertFalse(k(s), s)
        self.assertTrue(k("alt+f4"))
        self.assertTrue(k("enter"))

    def test_chain(self):
        self.assertTrue(act_mod.chain_may_open_window([{"via": "key", "key": "ctrl+p"}]))
        self.assertFalse(act_mod.chain_may_open_window([{"via": "key", "key": "ctrl+home"}]))


class TestForegroundElevated(unittest.TestCase):
    def test_helper(self):
        with mock.patch("cu_perceive.windows.target_integrity_check", return_value={"elevated_target": True}):
            self.assertTrue(act_mod._foreground_elevated(7))
        with mock.patch("cu_perceive.windows.target_integrity_check", return_value={"elevated_target": None}):
            self.assertFalse(act_mod._foreground_elevated(7))
        self.assertFalse(act_mod._foreground_elevated(0))
        with mock.patch("cu_perceive.windows.target_integrity_check", side_effect=OSError("x")):
            self.assertFalse(act_mod._foreground_elevated(7))

    def _bring(self, elevated):
        out = {}
        fv = {"foreground_ok": False, "focus_attempts": 2, "foreground_hwnd": 99, "foreground_title": "Admin App"}
        with mock.patch("cu_perceive.windows.focus_and_verify", return_value=fv), \
                mock.patch.object(act_mod, "_foreground_elevated", return_value=elevated):
            err = act_mod._bring_forward({"hwnd": 5}, out)
        return err, out

    def test_focus_failed_elevated(self):
        err, out = self._bring(True)
        self.assertTrue(err)
        self.assertEqual(out["reason"], "focus_failed")
        self.assertTrue(out["foreground_elevated"])
        self.assertIn("elevated", out["hint"])

    def test_focus_failed_normal(self):
        err, out = self._bring(False)
        self.assertEqual(out["reason"], "focus_failed")
        self.assertNotIn("foreground_elevated", out)


class TestDocs(unittest.TestCase):
    def test_docstrings(self):
        import inspect

        from cu_perceive import mcp_server
        src = inspect.getsource(mcp_server)
        for s in ("reflow_reason", "change_minor", "change_min_ratio", "foreground_elevated",
                  "ranking signal", "new_window_wait_reason", "Ctrl(+Shift)+letter"):
            self.assertIn(s, src)


if __name__ == "__main__":
    unittest.main()
