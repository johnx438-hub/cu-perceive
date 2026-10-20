"""PR10: act doc no longer calls steps refused; ime=off on key-only acts; all-occluded UIA warning; stamp age over a chain."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cu_perceive import act as act_mod
from cu_perceive.act import ime_matters, parse_steps
from cu_perceive.chain import run_steps
from cu_perceive.core import uia_occlusion_warnings

ROOT = Path(__file__).resolve().parent.parent


def _sentences(text: str):
    return [s for s in re.split(r"(?<=[.;])\s+|\n", text) if s.strip()]


def _says_steps_refused(text: str) -> list[str]:
    bad = []
    for s in _sentences(text):
        low = s.lower()
        if "steps" in low and ("unknown_args" in low or "refused" in low or "refuses" in low) and "browser" not in low:
            if "steps[" in low or "stopped" in low or "remaining" in low:
                continue  # chain results (steps[i] failed / remaining steps), not the parameter
            bad.append(s.strip())
        if "not a json steps array" in low or "has only flat fields" in low or "no steps=[" in low:
            bad.append(s.strip())
    return bad


class TestActDocDoesNotRefuseSteps(unittest.TestCase):
    def test_act_description(self):
        import test_round9

        doc = test_round9._collect_tools()["act"].__doc__ or ""
        self.assertIn("steps=[", doc)
        self.assertEqual(_says_steps_refused(doc), [])

    def test_readme_and_skill(self):
        for name in ("README.md", "SKILL.md"):
            p = ROOT / name
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("act has only flat fields", text, name)
            self.assertNotIn("没有** `steps=[{...}]`", text, name)

    def test_no_steps_error_text(self):
        out = act_mod.run_chain({"stamp": "x", "width": 10, "height": 10}, [], dry_run=True)
        self.assertNotIn("MCP refuses it", out.get("error", ""))


class TestImeKeyOnly(unittest.TestCase):
    def test_ime_matters(self):
        self.assertTrue(ime_matters(parse_steps(text="hi")))
        self.assertTrue(ime_matters(parse_steps(key="a")))
        self.assertTrue(ime_matters(parse_steps(key="shift+a")))
        for k in ("enter", "f9", "ctrl+s", "esc", "tab,enter", "alt+f4", "down"):
            self.assertFalse(ime_matters(parse_steps(key=k)), k)

    def test_key_only_act_switches_now(self):
        # PR15: an IME can eat Ctrl chords, so ime=off applies to key-only acts too
        out = {"ok": True, "dry_run": False}
        st = {"switch_requested": True, "verified": True, "layout_switched": True}
        with mock.patch("cu_perceive.ime.suspend_ime", return_value=st) as s:
            err = act_mod._suspend_ime_for_act(out, {"hwnd": 123}, parse_steps(key="ctrl+w"), "off")
        self.assertIsNone(err)
        s.assert_called_once_with(123, mode="off")
        self.assertTrue(out["ime_suspended"] and out["ime_layout_switched"] and out["ime_verified"])
        self.assertNotIn("ime_skipped", out)

    def test_key_only_switch_failure_only_warns(self):
        from cu_perceive.ime import ImeError

        out = {"ok": True, "dry_run": False}
        with mock.patch("cu_perceive.ime.suspend_ime", side_effect=ImeError("MessageBox")):
            err = act_mod._suspend_ime_for_act(out, {"hwnd": 123}, parse_steps(key="enter"), "off")
        self.assertIsNone(err)
        self.assertTrue(out["ok"])
        self.assertFalse(out["ime_layout_switched"])
        self.assertTrue(any("MessageBox" in w for w in out["warnings"]))

    def test_text_act_warns_unless_strict(self):
        st = {"switch_requested": True, "verified": False, "error": "nope"}
        out = {"ok": True, "dry_run": False}
        with mock.patch("cu_perceive.ime.suspend_ime", return_value=st):
            self.assertIsNone(act_mod._suspend_ime_for_act(out, {"hwnd": 123}, parse_steps(text="hi"), "off"))
        self.assertTrue(out["ime_unverified"])
        out = {"ok": True, "dry_run": False}
        with mock.patch("cu_perceive.ime.suspend_ime", return_value=st):
            err = act_mod._suspend_ime_for_act(out, {"hwnd": 123}, parse_steps(text="hi"), "strict")
        self.assertEqual(err["reason"], "ime_failed")

    def test_pointer_only_act_skips(self):
        out = {"ok": True, "dry_run": False}
        with mock.patch("cu_perceive.ime.suspend_ime", side_effect=AssertionError("must not switch")):
            act_mod._suspend_ime_for_act(out, {"hwnd": 123}, parse_steps(norm="1,1"), "off")
        self.assertEqual(out["ime_skipped"], "no_keyboard_step")

class TestOcclusionWarning(unittest.TestCase):
    def test_all_occluded(self):
        w, all_occ = uia_occlusion_warnings({"uia_occluded": 7, "uia_count": 0}, [], True)
        self.assertTrue(all_occ)
        self.assertIn("focus=true", w[0])

    def test_partial_and_none(self):
        self.assertEqual(uia_occlusion_warnings({"uia_occluded": 0}, [], True), ([], False))
        self.assertEqual(uia_occlusion_warnings({"uia_occluded": 2}, [1, 2, 3], True), ([], False))
        w, all_occ = uia_occlusion_warnings({"uia_occluded": 9}, [1, 2], True)
        self.assertFalse(all_occ)
        self.assertEqual(len(w), 1)
        self.assertEqual(uia_occlusion_warnings({"uia_occluded": 9}, [], False), ([], False))


class TestChainStampAge(unittest.TestCase):
    def _r(self, age, stale=False):
        r = {"ok": True, "after": {"foreground_hwnd": 1}, "new_windows": [], "stamp_check": {"ok": True, "age_sec": age}}
        if stale:
            r["stamp_stale"] = True
        return r

    def test_stale_mid_chain_warns_once_and_does_not_stop(self):
        res = [self._r(100), self._r(119), self._r(121, True), self._r(125, True)]
        out = run_steps([{"norm": "1,1"}] * 4, lambda st, send, i: res[i], send=True, stamp_hwnd=1)
        self.assertTrue(out["ok"])
        self.assertFalse(out["stopped"])
        self.assertTrue(out["stamp_stale"])
        self.assertEqual(out["stamp_stale_from_step"], 2)
        self.assertEqual(out["stamp_age_sec"], 125)
        self.assertEqual(sum("went stale" in w for w in out["warnings"]), 1)
        self.assertEqual([d.get("stamp_age_sec") for d in out["completed_steps"]], [100, 119, 121, 125])

    def test_fresh_chain_has_no_warning(self):
        out = run_steps([{"norm": "1,1"}] * 2, lambda st, send, i: self._r(3), send=True, stamp_hwnd=1)
        self.assertNotIn("stamp_stale", out)
        self.assertNotIn("warnings", out)

    def test_max_age_refusal_stops(self):
        res = [self._r(5), {"ok": False, "reason": "stamp_too_old", "error": "stamp is 31s old"}]
        out = run_steps([{"norm": "1,1"}] * 3, lambda st, send, i: res[i], send=True, stamp_hwnd=1)
        self.assertEqual(out["stop_reason"], "stamp_too_old")
        self.assertEqual(out["stopped_at"], 1)


if __name__ == "__main__":
    unittest.main()
