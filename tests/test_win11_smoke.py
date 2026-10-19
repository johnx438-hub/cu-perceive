"""Windows 11 hero-demo smoke fixes: clipboard, IME, via=keys, replace, UIA value, act lock."""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from unittest import mock

from cu_perceive.act import diff_new_windows, normalize_input_via, parse_steps, run_chain
from cu_perceive.ime import (
    layout_ime_active, normalize_ime, pick_installed_latin, probe_ime, restore_ime, suspend_ime,
)
from cu_perceive.input import (
    ClipboardError,
    _clipboard_worker,
    _shutdown_clipboard_worker,
    _worker_failure,
    clear_field_for_replace,
    clipboard_rpc,
    clipboard_set_inprocess,
    paste_text,
    typed_matches,
    unicode_clipboard_bytes,
)


def _screen():
    return {"stamp": "20261019-074141-002", "hwnd": None, "coord_space": "screen",
            "origin": {"left": 0, "top": 0}, "width": 1000, "height": 1000, "items": []}


def _window():
    return {
        "stamp": "20261019-074141-001", "hwnd": 42, "coord_space": "window",
        "origin": {"left": 0, "top": 0, "width": 800, "height": 500},
        "width": 800, "height": 500, "items": [],
        "identity": {"hwnd": 42, "pid": 1, "ctime": "2"},
    }


class TestClipboard(unittest.TestCase):
    def test_numeric_strings_are_utf16_bytes(self):
        for text in ("4", "45", "123456", "12.5", "B2003"):
            payload = unicode_clipboard_bytes(text)
            self.assertIsInstance(payload, bytes)
            self.assertTrue(payload.endswith(b"\x00\x00"))
            self.assertEqual(payload.decode("utf-16-le").rstrip("\x00"), text)

    def test_set_passes_bytes_and_retries_open(self):
        cb = mock.Mock()
        cb.OpenClipboard.side_effect = [OSError("Access is denied"), OSError("Access is denied"), None]
        con = mock.Mock()
        con.CF_UNICODETEXT = 13
        with mock.patch.dict(sys.modules, {"win32clipboard": cb, "win32con": con}), \
                mock.patch("cu_perceive.input.time.sleep"):
            clipboard_set_inprocess("4")
        self.assertEqual(cb.OpenClipboard.call_count, 3)
        fmt, data = cb.SetClipboardData.call_args[0]
        self.assertEqual(fmt, 13)
        self.assertIsInstance(data, bytes)
        self.assertEqual(data, unicode_clipboard_bytes("4"))
        cb.SetClipboardData.assert_called_once()

    def test_setclipboardtext_fallback_when_bytes_rejected(self):
        cb = mock.Mock()
        cb.SetClipboardData.side_effect = TypeError("want str")
        con = mock.Mock()
        con.CF_UNICODETEXT = 13
        with mock.patch.dict(sys.modules, {"win32clipboard": cb, "win32con": con}), \
                mock.patch("cu_perceive.input.time.sleep"):
            clipboard_set_inprocess("45")
        cb.SetClipboardText.assert_called_once_with("45", 13)

    def test_open_failure_is_clipboard_failed(self):
        cb = mock.Mock()
        cb.OpenClipboard.side_effect = OSError("Access is denied")
        con = mock.Mock()
        con.CF_UNICODETEXT = 13
        with mock.patch.dict(sys.modules, {"win32clipboard": cb, "win32con": con}), \
                mock.patch("cu_perceive.input.time.sleep"):
            with self.assertRaises(ClipboardError) as cm:
                clipboard_set_inprocess("4")
        self.assertEqual(cm.exception.reason, "clipboard_failed")
        cb.SetClipboardData.assert_not_called()

    def test_failed_set_does_not_paste_or_raise(self):
        def rpc(op, text=None):
            if op == "get":
                return {"ok": True, "text": "user clip"}
            return {"ok": False, "reason": "clipboard_failed", "error": "boom"}

        with mock.patch("cu_perceive.input.clipboard_rpc", side_effect=rpc), \
                mock.patch("cu_perceive.input._ctrl_v") as ctrl:
            out = paste_text("4")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "clipboard_failed")
        ctrl.assert_not_called()

    def test_paste_restores_previous_text(self):
        seen = []

        def rpc(op, text=None):
            seen.append((op, text))
            if op == "get":
                return {"ok": True, "text": "keep me"}
            return {"ok": True}

        with mock.patch("cu_perceive.input.clipboard_rpc", side_effect=rpc), \
                mock.patch("cu_perceive.input._ctrl_v"), \
                mock.patch("cu_perceive.input.time.sleep"):
            out = paste_text("4")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["restored"])
        self.assertEqual(seen, [("get", None), ("set", "4"), ("set", "keep me")])

    def test_empty_clipboard_is_emptied_again(self):
        seen = []

        def rpc(op, text=None):
            seen.append((op, text))
            if op == "get":
                return {"ok": True, "text": None}
            return {"ok": True}

        with mock.patch("cu_perceive.input.clipboard_rpc", side_effect=rpc), \
                mock.patch("cu_perceive.input._ctrl_v"):
            out = paste_text("secret")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["restored"])
        self.assertEqual(seen, [("get", None), ("set", "secret"), ("empty", None)])

    def test_helper_crash_returns_ok_false(self):
        failed = _worker_failure(0xC0000374, b"", b"heap")
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["reason"], "clipboard_failed")
        self.assertIn("exited", failed["error"])
        self.assertIn("heap", failed["error"])
        proc = mock.Mock()
        proc.poll.side_effect = [None, 0xC0000374]
        proc.stdin = mock.Mock()
        proc.stdout = mock.Mock()
        proc.stdout.readline.return_value = b""
        _clipboard_worker["proc"] = proc
        self.addCleanup(_shutdown_clipboard_worker)
        with mock.patch("cu_perceive.input._ctrl_v") as ctrl:
            out = paste_text("4", restore=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "clipboard_failed")
        self.assertIn("exited", out["error"])
        ctrl.assert_not_called()

    def test_helper_json_error_is_clipboard_failed(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        proc.stdout = mock.Mock()
        proc.stdout.readline.return_value = (
            json.dumps({"ok": False, "reason": "clipboard_failed", "error": "OpenClipboard"}).encode() + b"\n"
        )
        _clipboard_worker["proc"] = proc
        self.addCleanup(_shutdown_clipboard_worker)
        with mock.patch("cu_perceive.input._start_clipboard_worker", return_value=proc):
            out = paste_text("B2003", restore=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "clipboard_failed")
        self.assertIn("OpenClipboard", out["error"])

    def test_persistent_worker_is_reused(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.stdin = mock.Mock()
        proc.stdout = mock.Mock()
        proc.stdout.readline.side_effect = [b'{"ok": true, "text": null}\n', b'{"ok": true}\n']
        starts = []
        _clipboard_worker["proc"] = None
        self.addCleanup(_shutdown_clipboard_worker)
        with mock.patch("cu_perceive.input._start_clipboard_worker", side_effect=lambda: starts.append(1) or proc):
            self.assertTrue(clipboard_rpc("get")["ok"])
            self.assertTrue(clipboard_rpc("set", "4")["ok"])
        self.assertEqual(starts, [1])
        self.assertEqual(proc.stdin.write.call_count, 2)


class TestIme(unittest.TestCase):
    def test_pinyin_layout_is_active_english_is_not(self):
        self.assertTrue(layout_ime_active(0x08040804, None))
        self.assertTrue(layout_ime_active(0x04090409, True))
        self.assertFalse(layout_ime_active(0x04090409, False))
        self.assertFalse(layout_ime_active(0x08090809, None))
        self.assertEqual(normalize_ime("off"), "off")
        self.assertIsNone(normalize_ime(""))
        with self.assertRaises(ValueError):
            normalize_ime("zh")

    def test_probe_reads_target_thread(self):
        user32 = mock.Mock()
        user32.GetWindowThreadProcessId.return_value = 9
        user32.GetKeyboardLayout.return_value = 0x08040804
        imm = mock.Mock()
        imm.ImmGetContext.return_value = 1
        imm.ImmGetOpenStatus.return_value = 1
        info = probe_ime(42, user32=user32, imm32=imm)
        self.assertTrue(info["ok"])
        self.assertTrue(info["ime_active"])
        self.assertEqual(info["hkl_hex"], "0x08040804")
        self.assertEqual(info["thread_id"], 9)
        user32.GetKeyboardLayout.assert_called_with(9)
        imm.ImmReleaseContext.assert_called_with(42, 1)

    def test_off_uses_installed_english_and_does_not_load_one(self):
        self.assertEqual(pick_installed_latin([0x08040804, 0x08090809]), 0x08090809)
        self.assertEqual(pick_installed_latin([0x08040804, 0x04090409, 0x08090809]), 0x04090409)
        self.assertIsNone(pick_installed_latin([0x08040804]))
        user32 = mock.Mock()
        user32.GetWindowThreadProcessId.return_value = 9
        user32.GetKeyboardLayout.return_value = 0x08040804
        imm = mock.Mock()
        imm.ImmGetContext.return_value = 5
        imm.ImmGetOpenStatus.return_value = 1
        state = suspend_ime(77, user32=user32, imm32=imm, layouts=[0x08040804, 0x08090809])
        user32.LoadKeyboardLayoutW.assert_not_called()
        hwnd, msg, _w, hkl = user32.SendMessageW.call_args[0]
        self.assertEqual((hwnd, msg, hkl), (77, 0x0050, 0x08090809))
        self.assertTrue(state["layout_switched"])
        imm.ImmSetOpenStatus.assert_called_with(5, False)
        user32.SendMessageW.reset_mock()
        imm.ImmSetOpenStatus.reset_mock()
        imm.ImmGetContext.return_value = 5
        rest = restore_ime(state)
        self.assertTrue(rest["ok"], rest)
        self.assertEqual(user32.SendMessageW.call_args[0][3], 0x08040804)
        imm.ImmSetOpenStatus.assert_called_with(5, True)

    def test_off_closes_ime_when_no_latin_layout_is_installed(self):
        user32 = mock.Mock()
        user32.GetWindowThreadProcessId.return_value = 9
        user32.GetKeyboardLayout.return_value = 0x08040804
        imm = mock.Mock()
        imm.ImmGetContext.return_value = 5
        imm.ImmGetOpenStatus.return_value = 1
        state = suspend_ime(77, user32=user32, imm32=imm, layouts=[0x08040804])
        user32.SendMessageW.assert_not_called()
        user32.LoadKeyboardLayoutW.assert_not_called()
        self.assertTrue(state["closed_only"])
        self.assertFalse(state["layout_switched"])
        imm.ImmSetOpenStatus.assert_called_with(5, False)


class TestActImeAndVia(unittest.TestCase):
    def test_dry_run_warns_when_ime_active(self):
        info = {
            "ok": True, "hkl_hex": "0x08040804", "lang_id": "0x0804",
            "ime_open": True, "ime_active": True, "thread_id": 9,
        }
        with mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.ime.probe_ime", return_value=info):
            out = run_chain(_window(), parse_steps(key="a"), dry_run=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["ime"]["hkl_hex"], "0x08040804")
        self.assertTrue(any("ime_active" in w for w in out["warnings"]))
        with mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.ime.probe_ime", return_value=info):
            planned = run_chain(_window(), parse_steps(key="a"), dry_run=True, ime="off")
        self.assertTrue(any("ime=off will switch" in w for w in planned["warnings"]))
        self.assertFalse(any(w.startswith("ime_active:") for w in planned["warnings"]))

    def test_ime_off_restores_even_after_the_act(self):
        calls = []

        def suspend(hwnd, **k):
            calls.append(("suspend", int(hwnd)))
            return {"hwnd": int(hwnd), "hkl": 0x08040804, "ime_open": True, "closed": True}

        def restore(state):
            calls.append(("restore", state["hwnd"]))
            return {"ok": True, "restored": True}

        with mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.act.stamp_identity_error", return_value=None), \
                mock.patch("cu_perceive.windows.foreground_matches", return_value=(True, 42)), \
                mock.patch("cu_perceive.ime.probe_ime", return_value={"ok": False}), \
                mock.patch("cu_perceive.ime.suspend_ime", side_effect=suspend), \
                mock.patch("cu_perceive.ime.restore_ime", side_effect=restore), \
                mock.patch("cu_perceive.input.tap_key", return_value={"ok": True}), \
                mock.patch("cu_perceive.act._snapshot_windows", return_value=[]), \
                mock.patch("cu_perceive.windows.foreground_brief", return_value=None), \
                mock.patch("time.sleep"):
            out = run_chain(_window(), parse_steps(key="a"), dry_run=False, ime="off")
        self.assertTrue(out["ok"], out)
        self.assertEqual(calls, [("suspend", 42), ("restore", 42)])
        self.assertTrue(out["ime_suspended"])
        self.assertTrue(out["ime_restored"])

    def test_via_keys_is_passed_through(self):
        self.assertEqual(normalize_input_via("keys"), "keys")
        specs = parse_steps(text="B2003", input_via="keys")
        self.assertEqual(specs[0]["input_via"], "keys")
        seen = []
        with mock.patch("cu_perceive.windows.window_at_point", return_value=77), \
                mock.patch("cu_perceive.windows.foreground_matches", return_value=(True, 77)), \
                mock.patch("cu_perceive.input.click_xy"), \
                mock.patch("cu_perceive.input.enter_text",
                           side_effect=lambda t, via="paste": seen.append((t, via)) or {"ok": True}), \
                mock.patch("time.sleep"):
            out = run_chain(_screen(), parse_steps(norm="1,1", text="B2003", input_via="keys"), dry_run=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual(seen, [("B2003", "keys")])
        self.assertEqual(out["steps"][1]["input_via"], "keys")
        with self.assertRaises(ValueError):
            parse_steps(text="a", input_via="clipboard")


class _Pattern:
    def __init__(self, value):
        self.value = value

    def SetValue(self, v):
        self.value = v

    @property
    def Value(self):
        return self.value


class _Sticky(_Pattern):
    def SetValue(self, v):
        return None


class TestReplaceClear(unittest.TestCase):
    def test_value_pattern_clears_without_ctrl_a(self):
        pat = _Pattern("WEB-02")
        with mock.patch("cu_perceive.input.tap_key") as tap:
            out = clear_field_for_replace(pattern=pat, discover=False, allow_keyboard=True)
        self.assertEqual(out["method"], "value_pattern")
        self.assertTrue(out["empty"])
        self.assertEqual(pat.value, "")
        tap.assert_not_called()

    def test_em_setsel_when_keys_do_not_stick(self):
        pat = _Sticky("WEB-02")
        sent = []

        def send(hwnd, msg, wparam, lparam):
            sent.append((hwnd, msg, wparam, lparam))

        def length(_h):
            return 0 if any(row[1] == 0x0303 for row in sent) else 4

        with mock.patch("cu_perceive.input.tap_key"), mock.patch("cu_perceive.input.tap_combo"):
            out = clear_field_for_replace(
                pattern=pat, hwnd=5, class_name="Edit", send_message=send,
                text_length=length, discover=False, allow_keyboard=True,
            )
        self.assertEqual(out["method"], "em_setsel")
        self.assertEqual(sent[0][1:], (0x00B1, 0, -1))
        self.assertEqual(sent[1][1], 0x0303)
        self.assertEqual(pat.value, "WEB-02")

    def test_masked_edit_clears_with_keys_not_setvalue(self):
        pat = _Pattern("2020-01-01")
        taps = []

        def tap(k):
            taps.append(k)
            if k == "delete":
                pat.value = ""

        with mock.patch("cu_perceive.input.tap_key", side_effect=tap), \
                mock.patch("cu_perceive.input.tap_combo", side_effect=lambda k: taps.append(k)):
            out = clear_field_for_replace(
                pattern=pat, class_name="WindowsForms10.EDIT.app", control_type="EditControl",
                discover=False, allow_keyboard=True,
            )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["method"], "end_shift_home")
        self.assertEqual(taps, ["end", "shift+home", "delete"])
        self.assertEqual(pat.value, "")

        sticky = _Pattern("2020-01-01")
        calls = []
        real_set = sticky.SetValue

        def lying(v):
            calls.append(v)
            real_set(v)

        sticky.SetValue = lying
        with mock.patch("cu_perceive.input.tap_key"), mock.patch("cu_perceive.input.tap_combo"):
            failed = clear_field_for_replace(
                pattern=sticky, class_name="MaskedTextBox", discover=False, allow_keyboard=True,
            )
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["reason"], "replace_failed")
        self.assertEqual(calls, [])

    def test_end_shift_home_then_fail_if_still_filled(self):
        pat = _Sticky("WEB-02WEB-02")
        taps = []
        with mock.patch("cu_perceive.input.tap_key", side_effect=lambda k: taps.append(k)), \
                mock.patch("cu_perceive.input.tap_combo", side_effect=lambda k: taps.append(k)):
            out = clear_field_for_replace(
                pattern=pat, hwnd=5, class_name="Edit",
                send_message=lambda *a: None, text_length=lambda h: 8,
                discover=False, allow_keyboard=True,
            )
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "replace_failed")
        self.assertEqual(taps, ["end", "shift+home", "delete"])
        self.assertNotIn("ctrl+a", taps)

    def test_typed_matches_ignores_mask_literals_and_rejects_append(self):
        self.assertTrue(typed_matches("20261019", "2026-10-19"))
        self.assertFalse(typed_matches("WEB-02", "WEB-02WEB-02"))
        self.assertIsNone(typed_matches("B2003", None))
        self.assertFalse(typed_matches("B2003", "old"))


class TestNewWindowsAndLock(unittest.TestCase):
    def test_diff_reports_hwnd_title_class(self):
        before = [{"hwnd": 1, "title": "app", "class": "EzDemo"}]
        after = before + [{"hwnd": 9, "title": "保存成功", "class": "#32770"}]
        self.assertEqual(
            diff_new_windows(before, after),
            [{"hwnd": 9, "title": "保存成功", "class": "#32770"}],
        )

    def test_live_act_lists_new_windows(self):
        snaps = iter([
            [{"hwnd": 1, "title": "app", "class": "EzDemo"}],
            [{"hwnd": 1, "title": "app", "class": "EzDemo"},
             {"hwnd": 9, "title": "保存成功", "class": "#32770"}],
        ])
        with mock.patch("cu_perceive.act._snapshot_windows", side_effect=lambda: next(snaps)), \
                mock.patch("cu_perceive.windows.foreground_brief",
                           return_value={"foreground_hwnd": 9, "title": "保存成功", "class": "#32770"}), \
                mock.patch("cu_perceive.input.click_xy"), \
                mock.patch("time.sleep"):
            out = run_chain(_screen(), parse_steps(norm="1,1"), dry_run=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["new_windows"], [{"hwnd": 9, "title": "保存成功", "class": "#32770"}])
        self.assertEqual(out["after"]["foreground_hwnd"], 9)
        self.assertEqual(out["foreground_click_count"], 1)

    def test_three_live_acts_do_not_interleave(self):
        order = []
        start = threading.Barrier(3)

        def click(*a, **k):
            order.append("down")
            time.sleep(0.04)
            order.append("up")

        def worker():
            start.wait(timeout=5)
            run_chain(_screen(), parse_steps(norm="1,1"), dry_run=False)

        with mock.patch("cu_perceive.input.click_xy", side_effect=click), \
                mock.patch("cu_perceive.act._snapshot_windows", return_value=[]), \
                mock.patch("cu_perceive.windows.foreground_brief", return_value=None):
            threads = [threading.Thread(target=worker) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
                self.assertFalse(t.is_alive())
        self.assertEqual(order, ["down", "up", "down", "up", "down", "up"])

    def test_poll_waits_for_a_dialog_that_appears_late(self):
        snaps = iter([
            [{"hwnd": 1, "title": "app", "class": "EzDemo"}],
            [{"hwnd": 1, "title": "app", "class": "EzDemo"}],
            [{"hwnd": 1, "title": "app", "class": "EzDemo"},
             {"hwnd": 9, "title": "保存成功", "class": "#32770"}],
        ])
        with mock.patch("cu_perceive.act._snapshot_windows", side_effect=lambda: next(snaps)), \
                mock.patch("cu_perceive.windows.foreground_brief", return_value=None), \
                mock.patch("cu_perceive.input.click_xy"), \
                mock.patch("time.sleep") as slept:
            out = run_chain(_screen(), parse_steps(norm="1,1"), dry_run=False)
        self.assertEqual(out["new_windows"], [{"hwnd": 9, "title": "保存成功", "class": "#32770"}])
        self.assertGreaterEqual(slept.call_count, 1)

    def test_focus_outside_refuses_before_clear_or_type(self):
        calls = []
        with mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.act.stamp_identity_error", return_value=None), \
                mock.patch("cu_perceive.windows.foreground_matches", return_value=(True, 42)), \
                mock.patch("cu_perceive.windows.focus_matches", return_value=(False, 99)), \
                mock.patch("cu_perceive.input.clear_field_for_replace",
                           side_effect=lambda **k: calls.append("clear")), \
                mock.patch("cu_perceive.input.prepare_paste",
                           side_effect=lambda t: calls.append("stage")), \
                mock.patch("cu_perceive.input.enter_text",
                           side_effect=lambda *a, **k: calls.append("type")), \
                mock.patch("time.sleep"):
            out = run_chain(_window(), parse_steps(text="hello", replace=True), dry_run=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "focus_outside")
        self.assertEqual(out["keyboard_refused_step"], 0)
        self.assertEqual(calls, [])

    def test_readback_mismatch_is_not_ok(self):
        with mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.act.stamp_identity_error", return_value=None), \
                mock.patch("cu_perceive.windows.foreground_matches", return_value=(True, 42)), \
                mock.patch("cu_perceive.windows.focus_matches", return_value=(True, 42)), \
                mock.patch("cu_perceive.input.enter_text", return_value={"ok": True, "via": "keys"}), \
                mock.patch("cu_perceive.input.read_focused_value", return_value="old"), \
                mock.patch("cu_perceive.act._snapshot_windows", return_value=[]), \
                mock.patch("cu_perceive.windows.foreground_brief", return_value=None), \
                mock.patch("time.sleep"):
            out = run_chain(_window(), parse_steps(text="B2003", input_via="keys"), dry_run=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "typed_mismatch")
        self.assertIs(out["typed_verified"], False)

    def test_ime_off_does_not_warn_when_the_switch_sticks(self):
        active = {
            "ok": True, "hkl": 0x08040804, "hkl_hex": "0x08040804", "lang_id": "0x0804",
            "ime_open": True, "ime_active": True, "thread_id": 9,
        }
        english = {
            "ok": True, "hkl": 0x08090809, "hkl_hex": "0x08090809", "lang_id": "0x0809",
            "ime_open": False, "ime_active": False, "thread_id": 9,
        }
        probes = iter([active, english])

        def suspend(hwnd, **k):
            return {
                "hwnd": int(hwnd), "hkl": 0x08040804, "ime_open": True, "closed": True,
                "layout_switched": True, "english_hkl": 0x08090809, "closed_only": False,
            }

        with mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.act.stamp_identity_error", return_value=None), \
                mock.patch("cu_perceive.windows.foreground_matches", return_value=(True, 42)), \
                mock.patch("cu_perceive.windows.focus_matches", return_value=(None, 0)), \
                mock.patch("cu_perceive.ime.probe_ime", side_effect=lambda *a, **k: next(probes)), \
                mock.patch("cu_perceive.ime.suspend_ime", side_effect=suspend), \
                mock.patch("cu_perceive.ime.restore_ime", return_value={"ok": True, "restored": True}), \
                mock.patch("cu_perceive.input.tap_key", return_value={"ok": True}), \
                mock.patch("cu_perceive.act._snapshot_windows", return_value=[]), \
                mock.patch("cu_perceive.windows.foreground_brief", return_value=None), \
                mock.patch("time.sleep"):
            out = run_chain(_window(), parse_steps(key="a"), dry_run=False, ime="off")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["ime_verified"])
        self.assertFalse(any(str(w).startswith("ime_active:") for w in out.get("warnings") or []))

    def test_ime_off_fails_when_the_layout_stays(self):
        active = {
            "ok": True, "hkl": 0x08040804, "hkl_hex": "0x08040804", "lang_id": "0x0804",
            "ime_open": True, "ime_active": True, "thread_id": 9,
        }

        def suspend(hwnd, **k):
            return {
                "hwnd": int(hwnd), "hkl": 0x08040804, "ime_open": True, "closed": True,
                "layout_switched": True, "english_hkl": 0x04090409, "closed_only": False,
            }

        with mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.act.stamp_identity_error", return_value=None), \
                mock.patch("cu_perceive.ime.probe_ime", return_value=active), \
                mock.patch("cu_perceive.ime.suspend_ime", side_effect=suspend), \
                mock.patch("cu_perceive.ime.restore_ime", return_value={"ok": True, "restored": True}), \
                mock.patch("cu_perceive.input.tap_key") as tap, \
                mock.patch("cu_perceive.act._snapshot_windows", return_value=[]), \
                mock.patch("cu_perceive.windows.foreground_brief", return_value=None), \
                mock.patch("time.sleep"):
            out = run_chain(_window(), parse_steps(key="a"), dry_run=False, ime="off")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "ime_failed")
        tap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
