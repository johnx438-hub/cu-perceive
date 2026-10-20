"""PR12: new_windows wait/recheck/snapshot, check_change for drags + reflow, elevated target,
OCR bbox + confidence, integrity in status."""
from __future__ import annotations

import inspect
import unittest
from unittest import mock

import numpy as np

from cu_perceive import act as act_mod
from cu_perceive.change import REFLOW_RATIO, run_with_change_check
from cu_perceive.core import _to_items
from cu_perceive.windows import integrity_name, self_integrity, target_integrity_check

A = {"hwnd": 1, "title": "Main", "class": "X"}
D = {"hwnd": 2, "title": "Choose a Template", "class": "Chrome_WidgetWin_1"}


class TestNewWindowsWait(unittest.TestCase):
    def _poll(self, snaps, timeout, recheck):
        it = iter(snaps)
        last = [snaps[-1]]

        def snap():
            try:
                last[0] = next(it)
            except StopIteration:
                pass
            return last[0]

        with mock.patch.object(act_mod, "_snapshot_windows", side_effect=snap), \
                mock.patch("time.sleep") as sl:
            found, latest = act_mod._poll_new_windows([A], timeout, recheck)
        return found, latest, sl

    def test_recheck_catches_late_window(self):
        # wait 0.1 s = 2 polls, nothing; the recheck sees the dialog
        found, _, sl = self._poll([[A], [A], [A], [A, D]], 0.1, 0.3)
        self.assertEqual([w["hwnd"] for w in found], [2])
        self.assertIn(mock.call(0.3), sl.call_args_list)

    def test_no_recheck_when_found(self):
        found, _, sl = self._poll([[A, D]], 1.2, 0.3)
        self.assertEqual(len(found), 1)
        self.assertNotIn(mock.call(0.3), sl.call_args_list)

    def test_zero_wait_no_recheck(self):
        found, _, sl = self._poll([[A], [A, D]], 0.0, 0.0)
        self.assertEqual(found, [])
        sl.assert_not_called()

    def test_default_wait_and_env(self):
        self.assertEqual(act_mod.new_window_wait_s(), 1.2)
        with mock.patch.dict("os.environ", {"CU_PERCEIVE_NEW_WINDOW_WAIT_MS": "2000"}):
            self.assertEqual(act_mod.new_window_wait_s(), 2.0)

    def test_attach_uses_per_call_wait_and_reports_snapshot(self):
        out = {"steps": [{"via": "norm"}]}
        with mock.patch.object(act_mod, "_poll_new_windows", return_value=([], [A, {"hwnd": 1, "title": "Main*"}])) as p, \
                mock.patch("cu_perceive.windows.foreground_brief", return_value=None):
            act_mod._attach_new_windows(out, [A], 2.5)
        p.assert_called_once_with([A], 2.5, act_mod.NEW_WINDOW_RECHECK_S)
        self.assertEqual(out["new_window_wait_ms"], 2500)
        self.assertTrue(out["windows_snapshot_changed"])

    def test_typing_does_not_wait(self):
        out = {"steps": [{"via": "type"}]}
        with mock.patch.object(act_mod, "_poll_new_windows", return_value=([], [A])) as p, \
                mock.patch("cu_perceive.windows.foreground_brief", return_value=None):
            act_mod._attach_new_windows(out, [A], 2.5)
        p.assert_called_once_with([A], 0.0, 0.0)
        self.assertFalse(out["windows_snapshot_changed"])

    def test_snapshot_changed(self):
        f = act_mod.windows_snapshot_changed
        self.assertFalse(f([A], [dict(A)]))
        self.assertTrue(f([A], [A, D]))
        self.assertTrue(f([A, D], [A]))  # a dialog closed
        self.assertTrue(f([A], [{"hwnd": 1, "title": "Main - edited"}]))
        self.assertIsNone(f(None, [A]))


class TestChainStepWait(unittest.TestCase):
    def test_only_last_or_expect_change_step_waits_long(self):
        from cu_perceive.mcp_server import _chain_step_wait as w

        self.assertEqual(w(3000, {}, 0, 3), 0.25)
        self.assertEqual(w(3000, {}, 2, 3), 3.0)
        self.assertEqual(w(3000, {"expect_change": True}, 1, 3), 3.0)
        self.assertIsNone(w(None, {}, 2, 3))  # default wait
        self.assertEqual(w(100, {}, 0, 3), 0.1)

    def test_param_in_schema(self):
        from test_round9 import _collect_tools

        sig = inspect.signature(_collect_tools()["act"])
        self.assertIsNone(sig.parameters["new_window_wait_ms"].default)

    def test_chain_level_field(self):
        from cu_perceive.chain import validate_steps

        _, err = validate_steps([{"norm": "1,1", "new_window_wait_ms": 10}])
        self.assertEqual(err["reason"], "bad_args")


class TestCheckChangeDragAndReflow(unittest.TestCase):
    def _run(self, after_fill):
        a = np.zeros((100, 100, 3), np.uint8)
        b = a.copy()
        after_fill(b)
        frames = iter([a, b])
        return run_with_change_check(5, lambda: {"ok": True, "steps": [{"via": "drag", "dragged": True}]},
                                     grab=lambda h: next(frames), sleep=lambda s: None, wait_s=0)

    def test_drag_with_no_effect_is_unchanged(self):
        out = self._run(lambda b: None)
        self.assertTrue(out["steps"][0]["dragged"])
        self.assertFalse(out["screen_changed"])
        self.assertFalse(out["layout_reflow_suspected"])

    def test_large_change_suspects_reflow(self):
        out = self._run(lambda b: b.__setitem__(slice(0, 60), 255))
        self.assertTrue(out["layout_reflow_suspected"])
        self.assertGreater(out["changed_ratio"], REFLOW_RATIO)
        self.assertTrue(any("re-perceive" in w for w in out["warnings"]))

    def test_small_change_no_reflow(self):
        out = self._run(lambda b: b.__setitem__((slice(0, 10), slice(0, 10)), 255))
        self.assertTrue(out["screen_changed"])
        self.assertFalse(out["layout_reflow_suspected"])
        self.assertNotIn("warnings", out)


class TestIntegrity(unittest.TestCase):
    def test_names(self):
        self.assertEqual(integrity_name(0x2000), "medium")
        self.assertEqual(integrity_name(0x3000), "high")
        self.assertEqual(integrity_name(0x4000), "system")
        self.assertEqual(integrity_name(0x2100), "medium_plus")
        self.assertIsNone(integrity_name(None))

    def _check(self, rids):
        import os

        probe = lambda pid: rids.get(pid, (None, "denied"))  # noqa: E731
        with mock.patch.dict(rids, {os.getpid(): rids["self"]}):
            return target_integrity_check(10, probe=probe, pid_of=lambda h: 77)

    def test_elevated_target(self):
        r = self._check({"self": (0x2000, None), 77: (0x3000, None)})
        self.assertIs(r["elevated_target"], True)
        self.assertEqual((r["target_integrity"], r["self_integrity"]), ("high", "medium"))

    def test_same_level(self):
        self.assertIs(self._check({"self": (0x2000, None), 77: (0x2000, None)})["elevated_target"], False)

    def test_we_are_elevated_too(self):
        self.assertIs(self._check({"self": (0x3000, None), 77: (0x3000, None)})["elevated_target"], False)

    def test_unknown_when_token_denied(self):
        r = self._check({"self": (0x2000, None)})
        self.assertIsNone(r["elevated_target"])
        self.assertIn("integrity_error", r)

    def test_self_integrity(self):
        r = self_integrity(probe=lambda pid: (0x2000, None))
        self.assertEqual(r["level"], "medium")
        self.assertIs(r["elevated"], False)

    def _go(self, info, dry=False):
        from test_round5 import _window_frame
        from cu_perceive.act import parse_steps, run_chain

        calls = []
        with mock.patch("cu_perceive.windows.target_integrity_check", return_value=info), \
                mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.act.stamp_identity_error", return_value=None), \
                mock.patch("cu_perceive.windows.point_owner", return_value=(True, {"hwnd": 42})), \
                mock.patch("cu_perceive.windows.foreground_matches", side_effect=lambda h: (True, h)), \
                mock.patch("cu_perceive.windows.focus_and_verify",
                           return_value={"foreground_ok": True, "focus_attempts": 1}) as fv, \
                mock.patch("cu_perceive.input.click_xy", side_effect=lambda x, y, **k: calls.append("click")), \
                mock.patch("time.sleep"):
            out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=dry, focus=True)
        return out, calls, fv

    def test_act_refuses_elevated_before_focus_or_input(self):
        out, calls, fv = self._go({"elevated_target": True, "target_integrity": "high", "self_integrity": "medium"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "elevated_target")
        self.assertIn("administrator", out["hint"])
        self.assertEqual(calls, [])
        fv.assert_not_called()

    def test_dry_run_warns(self):
        out, calls, _ = self._go({"elevated_target": True, "target_integrity": "high", "self_integrity": "medium"},
                                 dry=True)
        self.assertTrue(out["ok"])
        self.assertTrue(any("elevated_target" in w for w in out.get("warnings") or []))

    def test_same_level_goes(self):
        out, calls, _ = self._go({"elevated_target": False, "target_integrity": "medium", "self_integrity": "medium"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(calls, ["click"])
        self.assertEqual(out["target_integrity"], "medium")

    def test_unknown_warns_and_goes(self):
        out, calls, _ = self._go({"elevated_target": None, "target_integrity": None, "self_integrity": "medium",
                                  "integrity_error": "OpenProcessToken failed (winerror 5)"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("integrity level" in w for w in out["warnings"]))

    def test_browser_status_reports_integrity(self):
        from cu_perceive.browser import tools

        with mock.patch.object(tools, "_guard", return_value={"ok": True}), \
                mock.patch("cu_perceive.windows.self_integrity",
                           return_value={"level": "medium", "elevated": False}):
            out = tools.browser_status()
        self.assertEqual(out["cu_perceive"]["integrity_level"], "medium")
        self.assertIs(out["cu_perceive"]["elevated"], False)


class TestOcrBboxConfidence(unittest.TestCase):
    def test_items_carry_bbox_and_confidence(self):
        items = _to_items([{"bbox": [100, 200, 300, 400], "text": "OK", "confidence": 0.91234},
                           {"bbox": [0, 0, 10, 10], "label": "icon"}], 1000, 500)
        self.assertEqual(items[0]["box"], [100, 100, 300, 200])
        self.assertEqual(items[0]["bbox_norm"], [100, 200, 300, 400])
        self.assertEqual(items[0]["confidence"], 0.912)
        self.assertIsNone(items[1]["confidence"])

    def test_compact_keeps_bbox_and_confidence(self):
        payload = {"items": [{"id": 1, "text": "OK", "kind": "ocr", "norm": [100, 200, 300, 400], "confidence": 0.8}]}
        mcp = __import__("cu_perceive.mcp_server", fromlist=["_compact_rows"])
        mcp._compact_rows(payload)
        self.assertEqual(payload["items"][0]["bbox_norm"], [100, 200, 300, 400])
        self.assertEqual(payload["items"][0]["confidence"], 0.8)
        self.assertEqual(payload["items"][0]["center_norm"], [200, 300])


if __name__ == "__main__":
    unittest.main()
