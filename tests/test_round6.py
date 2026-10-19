"""Round 6 Windows findings: MCP scroll as int, stamp= as a json path, hwnd= instead of a
silently dropped param, key-string normalization, and the hold-key refusal hint."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_agent_friction import _mcp_tools
from test_round5 import COVER, SIBLING, TestKeysInOwnedDialog, _Tree, _window_frame

from cu_perceive import act
from cu_perceive.act import StampWindowMismatch, load_frame, parse_steps, run_chain, stamp_json_path
from cu_perceive.cli import main as cli_main
from cu_perceive.keys import UnknownKeyError, check_key_spec, split_key_list


def _screen(stamp):
    return {"stamp": stamp, "hwnd": None, "coord_space": "screen", "origin": {"left": 0, "top": 0},
            "width": 1000, "height": 1000, "items": []}


def _win(stamp, hwnd):
    return {"stamp": stamp, "hwnd": hwnd, "coord_space": "window",
            "origin": {"left": 0, "top": 0, "width": 800, "height": 600}, "width": 800, "height": 600,
            "items": [], "identity": {"hwnd": hwnd, "pid": 1, "ctime": "1"}}


class _OutDir(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        p = mock.patch.object(act, "DEFAULT_OUT", self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.n = 0

    def put(self, frame) -> Path:
        path = self.dir / f"{frame['stamp']}.json"
        path.write_text(json.dumps(frame), encoding="utf-8")
        self.n += 1
        t = time.time() - 100 + self.n
        os.utime(path, (t, t))
        return path


class TestStampAsPath(_OutDir):
    def test_returned_json_path_works(self):
        p = self.put(_screen("20261019-100000-001"))
        self.assertEqual(stamp_json_path(str(p)), p)
        self.assertEqual(load_frame(stamp=str(p))[1], p)
        self.assertNotIn(".json.json", str(stamp_json_path(str(p))))

    def test_name_with_json_and_path_without_json(self):
        p = self.put(_screen("20261019-100000-002"))
        self.assertEqual(load_frame(stamp=p.name)[1], p)
        self.assertEqual(load_frame(stamp=str(p)[: -len(".json")])[1], p)
        self.assertEqual(load_frame(stamp="20261019-100000-002")[1], p)
        self.assertEqual(load_frame(stamp=f'"{p}"')[1], p)

    def test_windows_map_fallback_still_works(self):
        m = self.dir / "20261019-100000-003.windows.json"
        m.write_text(json.dumps(_screen("20261019-100000-003")), encoding="utf-8")
        self.assertEqual(load_frame(stamp="20261019-100000-003")[1], m)

    def test_missing_path_is_reported_as_given(self):
        with self.assertRaises(FileNotFoundError) as cm:
            load_frame(stamp=str(self.dir / "nope.json"))
        self.assertNotIn(".json.json", str(cm.exception))

    def test_mcp_and_cli_accept_the_path(self):
        p = self.put(_screen("20261019-100000-004"))
        out = _mcp_tools()["act"](stamp=str(p), norm="500,500")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["json"], str(p))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli_main(["act", "--stamp", str(p), "--norm", "500,500"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buf.getvalue())["ok"])


class TestHwndParam(_OutDir):
    def setUp(self):
        super().setUp()
        self.a = self.put(_win("20261019-100000-010", 111))
        self.b = self.put(_win("20261019-100000-011", 222))

    def test_no_stamp_picks_that_windows_newest_stamp(self):
        self.assertEqual(load_frame()[1], self.b)
        self.assertEqual(load_frame(hwnd=111)[1], self.a)
        newer_a = self.put(_win("20261019-100000-012", 111))
        self.assertEqual(load_frame(hwnd=111)[1], newer_a)

    def test_stamp_of_another_window_refused(self):
        with self.assertRaises(StampWindowMismatch) as cm:
            load_frame(stamp="20261019-100000-011", hwnd=111)
        self.assertIn("is for hwnd=222, not hwnd=111", str(cm.exception))
        self.assertEqual(load_frame(stamp="20261019-100000-011", hwnd=222)[1], self.b)

    def test_no_stamp_for_that_window(self):
        with self.assertRaises(FileNotFoundError) as cm:
            load_frame(hwnd=333)
        self.assertIn("no perceive stamp for hwnd=333", str(cm.exception))

    def test_mcp_hwnd(self):
        tools = _mcp_tools()
        out = tools["act"](stamp="20261019-100000-011", hwnd=111, norm="500,500", go=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "hwnd_mismatch")
        with mock.patch.object(act, "check_stamp", return_value={"ok": True}):
            out = tools["act"](hwnd=111, norm="500,500")
        self.assertEqual(out["stamp"], "20261019-100000-010")
        self.assertEqual(out["json"], str(self.a))

    def test_cli_hwnd(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = cli_main(["act", "--stamp", "20261019-100000-011", "--hwnd", "111", "--norm", "1,1"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buf.getvalue())["reason"], "hwnd_mismatch")


class TestScrollIntAndSign(_OutDir):
    def setUp(self):
        super().setUp()
        self.put(_screen("20261019-100000-020"))
        self.act = _mcp_tools()["act"]

    def _notches(self, out):
        return [s["notches"] for s in out["steps"] if s["via"] == "scroll"]

    def test_int_accepted(self):
        out = self.act(norm="500,500", scroll=5)
        self.assertEqual(self._notches(out), [5])
        out = self.act(norm="500,500", scroll=-5)
        self.assertEqual(self._notches(out), [-5])

    def test_bare_positive_warns_it_scrolls_up(self):
        for v in (5, "5", "+5"):
            out = self.act(norm="500,500", scroll=v)
            self.assertTrue(any("scrolls UP" in w and 'scroll="down:5"' in w for w in out.get("warnings", [])), v)

    def test_explicit_direction_has_no_warning(self):
        for v, n in (("down:5", -5), ("up:5", 5), (-5, -5)):
            out = self.act(norm="500,500", scroll=v)
            self.assertEqual(self._notches(out), [n])
            self.assertFalse(any("scrolls UP" in w for w in out.get("warnings", [])), v)

    def test_zero_or_empty_means_no_scroll(self):
        out = self.act(norm="500,500", scroll=0)
        self.assertEqual(self._notches(out), [])


class TestKeyStrings(unittest.TestCase):
    def test_wrong_aliases_dropped(self):
        for bad in ("plus", "tilde"):
            with self.assertRaises(UnknownKeyError):
                check_key_spec(bad)

    def test_spaces_normalized_before_split(self):
        self.assertEqual(split_key_list("ctrl + end"), ["ctrl+end"])
        self.assertEqual(split_key_list("page down"), ["pagedown"])
        self.assertEqual(split_key_list("ctrl + shift + page up, tab"), ["ctrl+shift+pageup", "tab"])
        self.assertEqual(split_key_list("tab enter"), ["tab", "enter"])
        self.assertEqual(split_key_list("up down"), ["up", "down"])
        self.assertEqual(split_key_list("ctrl+a,enter"), ["ctrl+a", "enter"])

    def test_plan_has_one_chord(self):
        out = run_chain(_screen("s"), parse_steps(key="ctrl + end"), dry_run=True)
        self.assertEqual([s["key"] for s in out["steps"]], ["ctrl+end"])
        out = run_chain(_screen("s"), parse_steps(key="Page Down"), dry_run=True)
        self.assertEqual([s["key"] for s in out["steps"]], ["pagedown"])


class TestHoldKeyHint(_Tree):
    def test_g1_hint_does_not_say_click_first(self):
        self.fg = COVER
        out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=False, hold_key="ctrl")
        self.assertIn("hold_key_refused_step", out)
        self.assertNotIn("add a click", out["error"])
        self.assertIn("before the modifier goes down", out["error"])
        self.assertIn("focus=true", out["hint"])


class TestKeysAfterClickHint(_Tree):
    _click_opens = TestKeysInOwnedDialog._click_opens

    def test_after_click_hint_says_the_click_did_not_help(self):
        with self._click_opens(SIBLING):
            out = run_chain(_window_frame(), parse_steps(norm="500,500", key="enter"), dry_run=False)
        self.assertEqual(out["keyboard_refused_step"], 1)
        self.assertIn("the click did not bring it to the front", out["error"])
        self.assertNotIn("add a click", out["error"])

    def test_keys_only_hint_unchanged(self):
        self.fg = COVER
        out = run_chain(_window_frame(), parse_steps(key="enter"), dry_run=False)
        self.assertIn("add a click on the target field", out["error"])


if __name__ == "__main__":
    unittest.main()
