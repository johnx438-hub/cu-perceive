"""Round 6: focus_outside names the dialog, no "(null)" read-back, non-text clipboard kept, IME restore."""
from __future__ import annotations

import unittest
from unittest import mock

from cu_perceive.act import parse_steps, run_chain
from cu_perceive.input import prepare_paste


def _window():
    return {
        "stamp": "20261020-010000-001", "hwnd": 42, "coord_space": "window",
        "origin": {"left": 0, "top": 0, "width": 800, "height": 500},
        "width": 800, "height": 500, "items": [],
        "identity": {"hwnd": 42, "pid": 1, "ctime": "2"},
    }


def _common():
    return [
        mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}),
        mock.patch("cu_perceive.act.stamp_identity_error", return_value=None),
        mock.patch("cu_perceive.windows.foreground_matches", return_value=(True, 42)),
        mock.patch("cu_perceive.act._snapshot_windows", return_value=[]),
        mock.patch("cu_perceive.windows.foreground_brief", return_value=None),
        mock.patch("time.sleep"),
    ]


class _Patched:
    def __init__(self, extra):
        self.ps = _common() + list(extra)

    def __enter__(self):
        for p in self.ps:
            p.start()

    def __exit__(self, *a):
        for p in reversed(self.ps):
            p.stop()


class FocusOutsideDialog(unittest.TestCase):
    def test_names_top_level_dialog_and_hint(self):
        titles = {99: "登录失败", 55: ""}
        with _Patched([
            mock.patch("cu_perceive.windows.focus_matches", return_value=(False, 55)),
            mock.patch("cu_perceive._win32.root_window", side_effect=lambda h: 99 if int(h) == 55 else int(h)),
            mock.patch("cu_perceive._win32.window_title", side_effect=lambda h: titles.get(int(h), "")),
            mock.patch("cu_perceive._win32.class_name", return_value="#32770"),
            mock.patch("cu_perceive.input.prepare_paste", return_value={"ok": True}),
        ]):
            out = run_chain(_window(), parse_steps(text="x"), dry_run=False)
        self.assertEqual(out["reason"], "focus_outside")
        bw = out["blocking_window"]
        self.assertEqual(bw["hwnd"], 99)
        self.assertEqual(bw["title"], "登录失败")
        self.assertEqual(bw["class"], "#32770")
        self.assertEqual(bw["focus_hwnd"], 55)
        self.assertIn("close or dismiss the dialog", out["hint"])
        self.assertIn("登录失败", out["hint"])

    def test_focus_in_other_child_keeps_click_hint(self):
        with _Patched([
            mock.patch("cu_perceive.windows.focus_matches", return_value=(False, 0)),
            mock.patch("cu_perceive.input.prepare_paste", return_value={"ok": True}),
        ]):
            out = run_chain(_window(), parse_steps(text="x"), dry_run=False)
        self.assertEqual(out["reason"], "focus_outside")
        self.assertIn("click the target field", out["hint"])


class NullReadback(unittest.TestCase):
    def _run(self, value, text="B2003"):
        with _Patched([
            mock.patch("cu_perceive.windows.focus_matches", return_value=(True, 42)),
            mock.patch("cu_perceive.input.enter_text", return_value={"ok": True, "via": "keys"}),
            mock.patch("cu_perceive.input.focused_readback",
                       return_value={"value": value, "password": False, "in_target": True}),
        ]):
            return run_chain(_window(), parse_steps(text=text, input_via="keys"), dry_run=False)

    def test_null_literal_is_empty_in_error(self):
        out = self._run("(null)")
        self.assertEqual(out["reason"], "typed_mismatch")
        self.assertNotIn("(null)", out["error"])
        self.assertNotIn("null", out["error"].lower())
        self.assertNotIn("(null)", str(out["steps"]))

    def test_null_literal_typed_on_purpose_matches(self):
        out = self._run("(null)", text="(null)")
        self.assertTrue(out["ok"], out)
        self.assertIs(out["typed_verified"], True)


class NonTextClipboard(unittest.TestCase):
    def test_prepare_paste_refuses_without_touching(self):
        calls = []

        def rpc(op, text=None):
            calls.append(op)
            return {"ok": True, "text": None, "foreign_formats": 2} if op == "get" else {"ok": True}

        with mock.patch("cu_perceive.input.clipboard_rpc", side_effect=rpc):
            staged = prepare_paste("abc")
        self.assertFalse(staged["ok"])
        self.assertEqual(staged["reason"], "clipboard_nontext")
        self.assertEqual(calls, ["get"])

    def test_act_falls_back_to_keys(self):
        typed = []
        with _Patched([
            mock.patch("cu_perceive.windows.focus_matches", return_value=(True, 42)),
            mock.patch("cu_perceive.input.prepare_paste",
                       return_value={"ok": False, "reason": "clipboard_nontext", "staged": False}),
            mock.patch("cu_perceive.input.finish_paste", side_effect=AssertionError("must not paste")),
            mock.patch("cu_perceive.input.enter_text",
                       side_effect=lambda t, via=None: typed.append(via) or {"ok": True, "via": "keys"}),
            mock.patch("cu_perceive.input.focused_readback",
                       return_value={"value": "abc", "password": False, "in_target": True}),
        ]):
            out = run_chain(_window(), parse_steps(text="abc"), dry_run=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual(typed, ["keys"])
        self.assertTrue(out["steps"][-1].get("clipboard_preserved"))
        self.assertTrue(any("non-text" in w for w in out.get("warnings") or []))


class ImeRestoreOnError(unittest.TestCase):
    def test_restore_runs_when_inner_raises(self):
        import cu_perceive.act as act
        restored = []
        real = act.run_chain

        def boom(*a, **k):
            act._ime_restore.state = {"hwnd": 42, "hkl": 1}
            raise RuntimeError("inner failure")

        with mock.patch("cu_perceive.ime.restore_ime", side_effect=lambda s: restored.append(s) or {"ok": True}), \
                mock.patch("cu_perceive.act._snapshot_windows", return_value=[]):
            calls = {"n": 0}

            def wrapper(*a, **k):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real(*a, **k)
                return boom(*a, **k)

            with mock.patch("cu_perceive.act.run_chain", side_effect=wrapper):
                with self.assertRaises(RuntimeError):
                    act.run_chain(_window(), parse_steps(text="x"), dry_run=False, ime="off")
        self.assertEqual(len(restored), 1)


class ForeignFormatScan(unittest.TestCase):
    def test_ole_text_companions_are_not_foreign(self):
        from cu_perceive import input as inp

        names = {49161: "DataObject", 50083: "System.String", 49171: "Ole Private Data", 49300: "HTML Format"}

        class CB:
            def __init__(self, fmts):
                self.fmts = fmts

            def EnumClipboardFormats(self, f):
                i = self.fmts.index(f) + 1 if f else 0
                return self.fmts[i] if i < len(self.fmts) else 0

            def GetClipboardFormatName(self, f):
                return names[f]

        def run(fmts):
            with mock.patch("cu_perceive.input._with_open_clipboard", side_effect=lambda fn: fn(CB(fmts))):
                return inp.clipboard_foreign_formats_inprocess()

        self.assertEqual(run([49161, 13, 50083, 1, 49171, 16, 7]), [])
        self.assertEqual(run([49300, 13, 1]), [49300])
        self.assertEqual(run([2, 8]), [2, 8])  # CF_BITMAP, CF_DIB


if __name__ == "__main__":
    unittest.main()
