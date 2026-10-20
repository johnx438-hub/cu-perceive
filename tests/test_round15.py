"""PR14: change_min_ratio warning only when ignored, chain new-window summary, blocking-window hint."""
import unittest

import numpy as _np
from unittest import mock

from cu_perceive import act as act_mod
from cu_perceive import chain
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(__file__))
from test_round9 import _collect_tools as _capture_tools  # noqa: E402


class TestMinRatioWarning(unittest.TestCase):
    def _patched(self):
        return (mock.patch("cu_perceive.act.load_frame", return_value=({"hwnd": 5, "stamp": "s"}, None)),
                mock.patch("cu_perceive.act.run_chain", return_value={"ok": True, "steps": []}),
                mock.patch("cu_perceive.change.grab_window", side_effect=lambda h: _np.zeros((10, 10, 3), _np.uint8)),
                mock.patch("cu_perceive.change.change_wait_s", return_value=0.0))

    def _flat(self, **kw):
        a, b, c, d = self._patched()
        with a, b as rc, c, d:
            act = _capture_tools()["act"]
            return act(stamp="s", norm="1,1", **kw), rc

    def test_no_warning_with_check_change(self):
        out, _ = self._flat(go=True, check_change=True, change_min_ratio=0.0003)
        self.assertFalse(any("change_min_ratio" in w for w in out.get("warnings") or []), out.get("warnings"))
        self.assertEqual(out.get("change_min_ratio"), 0.0003)

    def test_warning_without_check_change(self):
        out, _ = self._flat(go=True, change_min_ratio=0.0003)
        self.assertTrue(any("change_min_ratio" in w and "ignored" in w for w in out.get("warnings") or []))
        self.assertNotIn("change_min_ratio", out)  # really ignored: no diff ran

    def test_steps_path(self):
        a, b, c, d = self._patched()
        with a, b, c, d:
            act = _capture_tools()["act"]
            on = act(stamp="s", steps=[{"norm": "1,1"}], go=True, check_change=True, change_min_ratio=0.002)
            off = act(stamp="s", steps=[{"norm": "1,1"}], go=True, change_min_ratio=0.002)
        self.assertFalse(any("change_min_ratio" in w for w in on.get("warnings") or []))
        self.assertTrue(any("change_min_ratio" in w for w in off.get("warnings") or []))


class TestChainSummary(unittest.TestCase):
    def test_summary(self):
        res = [
            {"ok": True, "new_windows": [], "new_window_wait_ms": 250, "new_window_wait_reason": "action",
             "windows_snapshot_changed": False},
            {"ok": True, "new_windows": [], "new_window_wait_ms": 0, "new_window_wait_reason": "action_cannot_open_window"},
            {"ok": True, "new_windows": [{"hwnd": 9, "title": "Dlg"}], "new_window_wait_ms": 1200,
             "new_window_wait_reason": "action", "windows_snapshot_changed": True},
        ]
        it = iter(res)
        out = chain.run_steps([{"norm": "1,1"}, {"text": "a"}, {"key": "enter", "expect_change": True}],
                              lambda s, send, i: next(it), send=False)
        steps = out["completed_steps"]
        self.assertEqual([s["new_window_wait_ms"] for s in steps], [250, 0, 1200])
        self.assertEqual(steps[1]["new_window_wait_reason"], "action_cannot_open_window")
        self.assertEqual(out["new_window_wait_ms"], 1200)
        self.assertEqual(out["new_window_wait_reason"], "action")
        self.assertEqual(out["new_windows_steps"], [2])
        self.assertEqual(out["new_windows"], [{"hwnd": 9, "title": "Dlg"}])
        self.assertTrue(out["windows_snapshot_changed"])

    def test_no_windows(self):
        it = iter([{"ok": True, "new_windows": [], "new_window_wait_ms": 0, "new_window_wait_reason": "x"}])
        out = chain.run_steps([{"text": "a"}], lambda s, send, i: next(it), send=False)
        self.assertEqual(out["new_windows_steps"], [])
        self.assertNotIn("new_windows", out)


class TestChainStepReason(unittest.TestCase):
    def _run(self, steps, wait_s, cap_s):
        out = {"steps": steps}
        with mock.patch.object(act_mod, "_poll_new_windows", return_value=([], [])) as p, \
                mock.patch("cu_perceive.windows.foreground_brief", return_value=None):
            act_mod._attach_new_windows(out, [], wait_s, cap_s)
        return out, p

    def test_typing_step_in_chain_does_not_poll(self):
        out, p = self._run([{"via": "type"}], None, 0.25)
        self.assertEqual(out["new_window_wait_ms"], 0)
        self.assertEqual(out["new_window_wait_reason"], "action_cannot_open_window")

    def test_click_step_in_chain_capped(self):
        out, _ = self._run([{"via": "norm"}], None, 0.25)
        self.assertEqual(out["new_window_wait_ms"], 250)
        self.assertEqual(out["new_window_wait_reason"], "chain_step")

    def test_explicit_wins(self):
        out, _ = self._run([{"via": "type"}], 0.25, None)
        self.assertEqual(out["new_window_wait_reason"], "explicit")


class TestBlockingHint(unittest.TestCase):
    def test_hint_text(self):
        h = act_mod._blocking_hint(77, "")
        self.assertIn("untitled", h)
        self.assertIn("perceive_window(hwnd=77)", h)
        self.assertIn("'Popup'", act_mod._blocking_hint(77, "Popup"))
        self.assertEqual(act_mod._blocking_hint(0, "x"), "")

    def test_focus_failed_suggests(self):
        out = {}
        fv = {"foreground_ok": False, "focus_attempts": 2, "foreground_hwnd": 77, "foreground_title": ""}
        with mock.patch("cu_perceive.windows.focus_and_verify", return_value=fv), \
                mock.patch.object(act_mod, "_foreground_elevated", return_value=False):
            act_mod._bring_forward({"hwnd": 5}, out)
        self.assertEqual(out["suggested_hwnd"], 77)
        self.assertIn("perceive_window(hwnd=77)", out["hint"])
        self.assertEqual(out["blocking_window"]["hwnd"], 77)


if __name__ == "__main__":
    unittest.main()
