"""PR16: hover occlusion, shared focus-before-baseline hook (CLI too)."""
import inspect
import unittest
from unittest import mock

from cu_perceive import act as act_mod
from cu_perceive import change, cli
from cu_perceive.act import parse_steps


class TestFocusHook(unittest.TestCase):
    def test_none_without_focus_or_hwnd(self):
        self.assertIsNone(change.focus_baseline_hook(5, False))
        self.assertIsNone(change.focus_baseline_hook(None, True))

    def test_focuses_then_reports(self):
        with mock.patch("cu_perceive.windows.focus_and_verify", return_value={"foreground_ok": True}) as f:
            self.assertEqual(change.focus_baseline_hook(5, True, sleep=lambda s: None)(), "after_focus")
        f.assert_called_once_with(5, retries=1)
        with mock.patch("cu_perceive.windows.focus_and_verify", return_value={"foreground_ok": False}):
            self.assertEqual(change.focus_baseline_hook(5, True, sleep=lambda s: None)(), "focus_not_confirmed")

    def test_cli_uses_hook(self):
        self.assertIn("before_baseline=focus_baseline_hook(target, bool(args.focus))", inspect.getsource(cli))


class TestHoverOcclusion(unittest.TestCase):
    def test_hover_has_a_point(self):
        self.assertEqual(act_mod.step_points({"via": "hover", "screen": [3, 4]}), [("hover", [3, 4])])
        self.assertIn("hover", act_mod.POINTER_VIAS)

    def test_dry_run_reports_covered_hover(self):
        frame = {"hwnd": 42, "kind": "window"}
        steps = [{"via": "hover", "screen": [10, 20]}]
        with mock.patch("cu_perceive.act._has_window", return_value=True), \
                mock.patch("cu_perceive.windows.point_owner", return_value=(False, {"hwnd": 7, "title": "Cover"})):
            found = act_mod.occluded_points(frame, steps, method="input")
        self.assertEqual(found[0]["what"], "hover")
        self.assertEqual(found[0]["hwnd"], 7)


class TestWbMachineIme(unittest.TestCase):
    """en-GB (0809:00020409 -> 0xf0010809, 0809:00000809) + zh-Hans Pinyin, plus a stray 0x04090409
    that GetKeyboardLayoutList reports but the app (WPS) refuses."""

    LAYOUTS = [0xF0010809, 0x08040804, 0x08090809, 0x04090409]

    def run_with(self, accepts):
        from cu_perceive.ime import suspend_ime

        cur = {"hkl": 0x08040804}
        user32 = mock.Mock()
        user32.GetGUIThreadInfo.return_value = 0

        def post(h, msg, w, hkl):
            if hkl in accepts:
                cur["hkl"] = hkl
            return 1

        user32.PostMessageW.side_effect = post
        probe = lambda h: {"ok": True, "hkl": cur["hkl"], "hkl_hex": f"0x{cur['hkl']:08x}", "ime_open": None, "thread_id": 9}
        return suspend_ime(77, user32=user32, imm32=None, layouts=self.LAYOUTS, probe=probe, sleep=lambda s: None)

    def test_candidates_cover_every_english_layout(self):
        from cu_perceive.ime import latin_candidates

        self.assertEqual(latin_candidates(self.LAYOUTS), [0x04090409, 0xF0010809, 0x08090809])

    def test_stray_en_us_refused_then_en_gb_sticks(self):
        st = self.run_with({0xF0010809, 0x08090809})
        self.assertTrue(st["verified"] and st["layout_switched"])
        self.assertEqual(st["english_hkl"], 0xF0010809)
        self.assertEqual(st["tried"], ["0x04090409", "0xf0010809"])

    def test_nothing_sticks_reports_all_tried(self):
        st = self.run_with(set())
        self.assertFalse(st["verified"] or st["layout_switched"])
        self.assertIn("0x04090409, 0xf0010809, 0x08090809", st["error"])

    def test_restore_is_verified(self):
        from cu_perceive.ime import restore_ime

        st = self.run_with({0xF0010809})
        self.assertTrue(st["layout_switched"])
        user32 = st["_user32"]
        user32.PostMessageW.side_effect = None
        self.assertFalse(restore_ime(st)["restored"])  # window stays en-GB: not claimed as restored
        st["_probe"] = lambda h: {"ok": True, "hkl": 0x08040804}
        self.assertTrue(restore_ime(st)["verified"])

    def test_no_latin_closed_ime_counts(self):
        from cu_perceive.ime import suspend_ime

        imm = mock.Mock()
        imm.ImmGetContext.return_value = 5
        imm.ImmSetOpenStatus.return_value = 1
        probes = iter([{"ok": True, "hkl": 0x08040804, "ime_open": True, "thread_id": 9},
                       {"ok": True, "hkl": 0x08040804, "ime_open": False, "thread_id": 9}])
        st = suspend_ime(77, user32=mock.Mock(), imm32=imm, layouts=[0x08040804], probe=lambda h: next(probes),
                         sleep=lambda s: None)
        self.assertTrue(st["verified"] and st["closed_only"])
        self.assertFalse(st["layout_switched"])


if __name__ == "__main__":
    unittest.main()
