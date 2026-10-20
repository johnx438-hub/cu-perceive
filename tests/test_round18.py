"""PR17: focus-thread IME probe, IME open/conversion, close fallback, honest ime_restored,
strict error text, one verify budget, unlisted popups in after."""
import unittest
from unittest import mock

from cu_perceive import act as act_mod
from cu_perceive import ime
from cu_perceive.act import parse_steps

WPS = [0xF0010809, 0x08040804, 0x08090809, 0x04090409]


def _sim(accepts=(), ime_open=None, closable=False):
    """Probe + user32 that model a window: only HKLs in ``accepts`` stick; IME closable or not."""
    cur = {"hkl": 0x08040804, "open": ime_open}
    user32 = mock.Mock()

    def post(h, msg, w, hkl):
        if hkl in accepts:
            cur["hkl"] = hkl
        return 1

    user32.PostMessageW.side_effect = post
    probe = lambda h: {"ok": True, "hkl": cur["hkl"], "hkl_hex": f"0x{cur['hkl']:08x}", "ime_open": cur["open"],
                       "ime_conversion": 1 if cur["open"] else 0, "thread_id": 75020, "top_thread_id": 10776,
                       "focus_hwnd": 555}

    def set_open(focus, val, **k):
        if closable:
            cur["open"] = bool(val)
            return True
        return False

    return user32, probe, set_open, cur


class TestFocusThread(unittest.TestCase):
    def test_focus_target_uses_focused_child_thread(self):
        user32 = mock.Mock()
        user32.GetWindowThreadProcessId.side_effect = lambda h, p: {100: 10776, 555: 75020}[int(h)]
        with mock.patch("cu_perceive.ime._gui_focus_hwnd", side_effect=lambda u, tid: 555 if tid == 10776 else 0):
            self.assertEqual(ime.focus_target(100, user32), (555, 75020, 10776))

    def test_focus_target_falls_back_to_top(self):
        user32 = mock.Mock()
        user32.GetWindowThreadProcessId.return_value = 10776
        user32.GetAncestor.return_value = 999  # foreground focus belongs to another window
        with mock.patch("cu_perceive.ime._gui_focus_hwnd", side_effect=lambda u, tid: 0 if tid else 777):
            self.assertEqual(ime.focus_target(100, user32), (100, 10776, 10776))

    def test_request_goes_to_focus_child_first(self):
        user32, probe, _, _ = _sim(accepts={0x08090809})
        st = ime.suspend_ime(100, user32=user32, imm32=None, layouts=WPS, probe=probe, sleep=lambda s: None)
        self.assertEqual(st["targets"], [555, 100])
        self.assertEqual(user32.PostMessageW.call_args_list[0][0][0], 555)
        self.assertTrue(st["verified"] and st["layout_switched"])


class TestCloseFallback(unittest.TestCase):
    def test_layout_fails_ime_open_gets_closed(self):
        user32, probe, set_open, cur = _sim(accepts=(), ime_open=True, closable=True)
        with mock.patch("cu_perceive.ime.set_ime_open", side_effect=set_open):
            st = ime.suspend_ime(100, user32=user32, imm32=mock.Mock(), layouts=WPS, probe=probe, sleep=lambda s: None)
            self.assertTrue(st["verified"] and st["closed"] and st["closed_only"])
            self.assertFalse(st["layout_switched"])
            self.assertEqual(cur["hkl"], 0x08040804)
            rest = ime.restore_ime(st)
        self.assertTrue(rest["restored"] and rest["changed"])
        self.assertTrue(cur["open"])

    def test_nothing_changed_is_not_restored(self):
        user32, probe, set_open, _ = _sim(accepts=(), ime_open=None)
        with mock.patch("cu_perceive.ime.set_ime_open", side_effect=set_open):
            st = ime.suspend_ime(100, user32=user32, imm32=mock.Mock(), layouts=WPS, probe=probe, sleep=lambda s: None)
        self.assertFalse(st["verified"] or st["closed"] or st["layout_switched"])
        rest = ime.restore_ime(st)
        self.assertFalse(rest["restored"] or rest["changed"])

    def test_budget_is_total(self):
        user32, probe, _, _ = _sim(accepts=())
        st = ime.suspend_ime(100, user32=user32, imm32=None, layouts=WPS, probe=probe, sleep=lambda s: None)
        self.assertEqual(st["verify_budget_ms"], 750)
        self.assertLessEqual(st["verify_ms"], 800)

    def test_strict_error_names_mode(self):
        user32, probe, _, _ = _sim(accepts=())
        st = ime.suspend_ime(100, user32=user32, imm32=None, layouts=WPS, probe=probe, sleep=lambda s: None,
                             mode="strict")
        self.assertIn('ime="strict"', st["error"])
        self.assertIn("focus hwnd 555", st["error"])


class TestActFields(unittest.TestCase):
    def _act(self, st, mode="off"):
        out = {"ok": True, "dry_run": False}
        with mock.patch("cu_perceive.ime.suspend_ime", return_value=st):
            err = act_mod._suspend_ime_for_act(out, {"hwnd": 100}, parse_steps(key="ctrl+w"), mode)
        return out, err

    def test_unverified_has_no_restore_state(self):
        st = {"switch_requested": True, "verified": False, "layout_switched": False, "closed": False,
              "error": 'ime="off" could not switch', "hkl": 0x08040804, "after": {"thread_id": 75020}}
        out, err = self._act(st)
        self.assertIsNone(err)
        self.assertIsNone(act_mod._ime_restore.state)
        self.assertFalse(out["ime_suspended"])
        self.assertEqual(out["ime"]["thread_id"], 75020)

    def test_closed_fields(self):
        st = {"switch_requested": True, "verified": True, "closed": True, "closed_only": True, "layout_switched": False,
              "hkl": 0x08040804, "ime_open": True, "ime_conversion": 1, "focus_hwnd": 555,
              "after": {"thread_id": 75020, "ime_open": False, "ime_conversion": 0}, "verify_budget_ms": 750}
        out, _ = self._act(st)
        self.assertTrue(out["ime_suspended"] and out["ime_closed"])
        self.assertEqual((out["ime"]["ime_open_before"], out["ime"]["ime_open_after"]), (True, False))
        self.assertEqual((out["ime"]["ime_conversion_before"], out["ime"]["ime_conversion_after"]), (1, 0))
        self.assertEqual(out["ime"]["focus_hwnd"], 555)
        act_mod._ime_restore.state = None

    def test_strict_aborts_with_mode_text(self):
        st = {"verified": False, "error": 'ime="strict" could not switch hwnd 100', "switch_requested": True}
        out, err = self._act(st, "strict")
        self.assertEqual(err["reason"], "ime_failed")
        self.assertIn('ime="strict"', err["error"])


class TestUnlistedPopup(unittest.TestCase):
    def test_flags_unlisted_foreground_and_popup(self):
        out = {"after": {"foreground_hwnd": 793044, "title": "", "class": "Qt5QWindowPopupSaveBits"}}
        latest = [{"hwnd": 100}, {"hwnd": 200}]
        with mock.patch("cu_perceive._win32.window_rect", return_value=(0, 0, 10, 10)), \
                mock.patch("cu_perceive.windows.window_at_point", return_value=793044), \
                mock.patch("cu_perceive._win32.window_title", return_value=""), \
                mock.patch("cu_perceive._win32.class_name", return_value="Qt5QWindowPopupSaveBits"):
            act_mod._unlisted_popup(out, latest, 100)
        self.assertTrue(out["after"]["unlisted_foreground"])
        self.assertEqual(out["after"]["popup_hwnd"], 793044)
        self.assertEqual(out["after"]["popup"]["why"], "foreground")

    def test_covering_popup_while_main_is_foreground(self):
        out = {"after": {"foreground_hwnd": 100}}
        with mock.patch("cu_perceive._win32.window_rect", return_value=(0, 0, 10, 10)), \
                mock.patch("cu_perceive.windows.window_at_point", return_value=793044), \
                mock.patch("cu_perceive._win32.window_title", return_value=""), \
                mock.patch("cu_perceive._win32.class_name", return_value="Qt"):
            act_mod._unlisted_popup(out, [{"hwnd": 100}], 100)
        self.assertFalse(out["after"]["unlisted_foreground"])
        self.assertEqual(out["after"]["popup"]["why"], "covers_stamp_center")

    def test_all_listed_no_popup(self):
        out = {"after": {"foreground_hwnd": 100}}
        with mock.patch("cu_perceive._win32.window_rect", return_value=(0, 0, 10, 10)), \
                mock.patch("cu_perceive.windows.window_at_point", return_value=100):
            act_mod._unlisted_popup(out, [{"hwnd": 100}], 100)
        self.assertNotIn("popup_hwnd", out["after"])


if __name__ == "__main__":
    unittest.main()
