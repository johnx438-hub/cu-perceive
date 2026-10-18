"""Round 2 Windows findings: keyboard-only --go targets the stamp window (R1), stamps are
published atomically and the latest-stamp lookup skips partial files (R2) and windows
maps (R3); implicit --go needs a window stamp with identity."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cu_perceive import act, cli, stamps
from cu_perceive.act import load_frame, parse_steps, plan_steps, run_chain, unguarded_keyboard_steps

HWND = 42


def _window_frame(**extra):
    base = {
        "stamp": "20261019-064141-001",
        "hwnd": HWND,
        "coord_space": "window",
        "origin": {"left": 100, "top": 50, "width": 800, "height": 600},
        "width": 800,
        "height": 600,
        "items": [],
        "identity": {"hwnd": HWND, "pid": 4242, "ctime": "133000000000000000"},
    }
    base.update(extra)
    return base


def _screen_frame():
    return {"stamp": "20261019-064141-002", "hwnd": None, "coord_space": "screen",
            "origin": {"left": 0, "top": 0}, "width": 1000, "height": 1000, "items": []}


class _Live(unittest.TestCase):
    """Stamp check and identity pass; input functions are recorded, never sent."""

    def setUp(self):
        self.calls: list[str] = []
        self.fg = HWND
        patches = [
            mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}),
            mock.patch("cu_perceive.act.stamp_identity_error", return_value=None),
            mock.patch("cu_perceive.windows.foreground_matches",
                       side_effect=lambda h: (self.fg == h, self.fg)),
            mock.patch("cu_perceive.windows.force_foreground", side_effect=self._focus),
            mock.patch("cu_perceive.input.click_xy", side_effect=lambda *a, **k: self.calls.append("click")),
            mock.patch("cu_perceive.input.drag_path",
                       side_effect=lambda *a, **k: self.calls.append("drag") or {"samples": 2}),
            mock.patch("cu_perceive.input.tap_key", side_effect=lambda k: self.calls.append(f"key:{k}")),
            mock.patch("cu_perceive.input.enter_text",
                       side_effect=lambda t, via="paste": self.calls.append(f"type:{t}")),
            mock.patch("cu_perceive.input.key_down", side_effect=lambda k: self.calls.append(f"down:{k}")),
            mock.patch("cu_perceive.input.key_up", side_effect=lambda k: self.calls.append(f"up:{k}")),
            mock.patch("time.sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.focus_result = HWND

    def _focus(self, hwnd):
        self.calls.append(f"focus:{hwnd}")
        self.fg = self.focus_result
        return True

    def go(self, frame, **kw):
        focus = kw.pop("focus", False)
        return run_chain(frame, parse_steps(**kw), dry_run=False, focus=focus)


def _unguarded(**kw):
    return unguarded_keyboard_steps(plan_steps(_screen_frame(), parse_steps(**kw)))


class TestUnguardedSteps(unittest.TestCase):
    def test_click_or_drag_guards_later_keys(self):
        self.assertEqual(_unguarded(text="a", key="enter"), [0, 1])
        self.assertEqual(_unguarded(norm="1,1", text="a", key="enter"), [])
        self.assertEqual(_unguarded(drag="1,1,5,5", text="a"), [])

    def test_hover_and_scroll_do_not_guard(self):
        self.assertEqual(_unguarded(norm="1,1", hover=True, key="enter"), [1])
        self.assertEqual(_unguarded(scroll="3", key="enter"), [1])


class TestKeyboardOnlyGo(_Live):
    def test_refused_when_other_window_in_front(self):
        self.fg = 999
        out = self.go(_window_frame(), text="hello", key="enter", replace=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["keyboard_refused_step"], 0)
        self.assertIn("not in the foreground", out["error"])
        self.assertIn("999", out["error"])
        self.assertEqual(self.calls, [])

    def test_allowed_when_stamp_window_in_front(self):
        out = self.go(_window_frame(), text="hello", key="enter", replace=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["key:ctrl+a", "type:hello", "key:enter"])
        self.assertNotIn("focused_window", out)

    def test_click_first_needs_no_foreground_check(self):
        self.fg = 999
        out = self.go(_window_frame(), norm="500,500", text="hello", replace=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.calls, ["click", "key:ctrl+a", "type:hello"])

    def test_screen_stamp_refused(self):
        out = self.go(_screen_frame(), key="enter")
        self.assertFalse(out["ok"])
        self.assertIn("needs a window stamp", out["error"])
        self.assertEqual(self.calls, [])

    def test_no_stamp_refused(self):
        out = self.go({}, text="hi")
        self.assertFalse(out["ok"])
        self.assertEqual(self.calls, [])

    def test_hold_key_not_pressed_when_refused(self):
        self.fg = 999
        out = run_chain(_window_frame(), parse_steps(key="c"), dry_run=False, hold_key="ctrl")
        self.assertFalse(out["ok"])
        self.assertEqual(self.calls, [])

    def test_foreground_lost_mid_chain_refuses_next_key(self):
        def tap(k):
            self.calls.append(f"key:{k}")
            self.fg = 999

        with mock.patch("cu_perceive.input.tap_key", side_effect=tap):
            out = self.go(_window_frame(), text="hello", replace=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["keyboard_refused_step"], 1)
        self.assertEqual(self.calls, ["key:ctrl+a"])

    def test_foreground_unreadable_fails_closed(self):
        with mock.patch("cu_perceive.windows.foreground_matches", side_effect=OSError("no user32")):
            out = self.go(_window_frame(), key="enter")
        self.assertFalse(out["ok"])
        self.assertIn("could not read the foreground", out["error"])
        self.assertEqual(self.calls, [])

    def test_focus_brings_window_forward_then_types(self):
        self.fg = 999
        out = self.go(_window_frame(), text="hello", replace=True, focus=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["focused_window"])
        self.assertEqual(self.calls, [f"focus:{HWND}", "key:ctrl+a", "type:hello"])

    def test_focus_rechecks_identity(self):
        self.fg = 999
        with mock.patch("cu_perceive.act.stamp_identity_error",
                        side_effect=["HWND reused by pid 7", None]):
            out = self.go(_window_frame(), key="enter", focus=True)
        self.assertFalse(out["ok"])
        self.assertIn("after --focus failed", out["error"])
        self.assertEqual(self.calls, [f"focus:{HWND}"])

    def test_focus_that_does_not_stick_refuses(self):
        self.fg = 999
        self.focus_result = 999
        out = self.go(_window_frame(), key="enter", focus=True)
        self.assertFalse(out["ok"])
        self.assertIn("still not in the foreground", out["error"])
        self.assertEqual(self.calls, [f"focus:{HWND}"])

    def test_dry_run_warns(self):
        out = run_chain(_window_frame(), parse_steps(text="x"), dry_run=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("no preceding click" in w for w in out["warnings"]))
        out = run_chain(_screen_frame(), parse_steps(text="x"), dry_run=True)
        self.assertTrue(any("--go will refuse" in w for w in out["warnings"]))
        out = run_chain(_screen_frame(), parse_steps(norm="1,1", text="x"), dry_run=True)
        self.assertNotIn("warnings", out)


class TestForegroundMatches(unittest.TestCase):
    def test_root_comparison(self):
        from cu_perceive import windows

        roots = {HWND: HWND, 77: HWND, 555: 555}
        with mock.patch("cu_perceive._win32.root_window", side_effect=lambda h: roots.get(h, 0)):
            with mock.patch("cu_perceive._win32.foreground_window", return_value=77):
                self.assertEqual(windows.foreground_matches(HWND), (True, 77))
            with mock.patch("cu_perceive._win32.foreground_window", return_value=555):
                self.assertEqual(windows.foreground_matches(HWND), (False, 555))
            with mock.patch("cu_perceive._win32.foreground_window", return_value=0):
                self.assertEqual(windows.foreground_matches(HWND), (False, 0))

    def test_off_windows_raises(self):
        from cu_perceive import _win32, windows

        if _win32.IS_WINDOWS:
            self.skipTest("Windows host")
        with self.assertRaises(_win32.Win32Unavailable):
            windows.foreground_matches(HWND)


class _OutDir(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)
        p = mock.patch.object(act, "DEFAULT_OUT", self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.t = time.time() - 100

    def put(self, name: str, content) -> Path:
        p = self.dir / name
        p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        self.t += 1
        os.utime(p, (self.t, self.t))
        return p


class TestAtomicStamps(_OutDir):
    def test_final_json_appears_only_on_commit(self):
        stamp, final = stamps.allocate(self.dir)
        self.assertFalse(final.exists())
        self.assertEqual(list(self.dir.glob("*.json")), [])
        stamps.commit(final, '{"stamp": "%s"}' % stamp)
        self.assertEqual(json.loads(final.read_text(encoding="utf-8"))["stamp"], stamp)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [final.name])

    def test_commit_uses_os_replace(self):
        _, final = stamps.allocate(self.dir)
        with mock.patch("cu_perceive.stamps.os.replace", wraps=os.replace) as rep:
            stamps.commit(final, "{}")
        rep.assert_called_once_with(stamps.tmp_path(final), final)

    def test_failed_commit_leaves_no_json(self):
        _, final = stamps.allocate(self.dir)
        with mock.patch("cu_perceive.stamps.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                stamps.commit(final, "{}")
        stamps.release(final)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_reservation_blocks_same_name_and_committed_name(self):
        from datetime import datetime

        now = datetime(2026, 10, 19, 6, 41, 41, 5000)
        a, fa = stamps.allocate(self.dir, now=now)
        stamps.commit(fa, "{}")
        b, _ = stamps.allocate(self.dir, now=now)
        c, _ = stamps.allocate(self.dir, now=now)
        self.assertEqual(len({a, b, c}), 3)

    def test_perceive_and_windows_map_never_share_a_stamp(self):
        from datetime import datetime

        now = datetime(2026, 10, 19, 6, 41, 41, 5000)
        a, fa = stamps.allocate(self.dir, now=now)
        b, fb = stamps.allocate(self.dir, ".windows.json", now=now)
        self.assertNotEqual(a, b)
        stamps.commit(fa, "{}")
        stamps.commit(fb, "{}")
        c, _ = stamps.allocate(self.dir, ".windows.json", now=now)
        self.assertNotIn(c, (a, b))

    def test_perceive_publishes_once_and_cleans_up_on_failure(self):
        import cv2
        import numpy as np

        from cu_perceive.core import perceive

        png = self.dir / "in.png"
        cv2.imwrite(str(png), np.full((60, 80, 3), 128, np.uint8))
        out = self.dir / "out"
        payload = perceive(image=str(png), out_dir=str(out))
        self.assertEqual(sorted(p.name for p in out.glob("*.json")), [Path(payload["json"]).name])
        self.assertEqual(list(out.glob("*.reserve")) + list(out.glob("*.tmp")), [])
        with mock.patch("cu_perceive.core.stamps.commit", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                perceive(image=str(png), out_dir=str(out))
        self.assertEqual(len(list(out.glob("*.json"))), 1)
        self.assertEqual(len(list(out.glob("*.png"))), 2)
        self.assertEqual(list(out.glob("*.reserve")) + list(out.glob("*.tmp")), [])


class TestLatestLookup(_OutDir):
    def test_skips_empty_and_partial_newest(self):
        good = self.put("20261019-064141-001.json", _window_frame())
        self.put("20261019-064141-002.json", "")
        self.put("20261019-064141-003.json", '{"stamp": "20261019-0')
        frame, path = load_frame()
        self.assertEqual(path, good)
        self.assertEqual(frame["hwnd"], HWND)

    def test_skips_windows_map_and_scratch_files(self):
        good = self.put("20261019-064141-001.json", _window_frame())
        self.put("20261019-064141-002.windows.json", {"coord_space": "screen", "windows": []})
        self.put("20261019-064141-003.reserve", "")
        self.put("20261019-064141-004.json.tmp", "{")
        self.put("apps.json", {})
        self.assertEqual(load_frame()[1], good)

    def test_only_unreadable_stamps_is_clear_error(self):
        self.put("20261019-064141-002.json", "")
        with self.assertRaisesRegex(ValueError, "no readable perceive stamp"):
            load_frame()

    def test_empty_dir_unchanged(self):
        self.put("20261019-064141-002.windows.json", {"windows": []})
        self.assertEqual(load_frame(), ({}, None))

    def test_explicit_stamp_falls_back_to_windows_map(self):
        p = self.put("20261019-064141-002.windows.json", {"stamp": "20261019-064141-002", "windows": []})
        self.assertEqual(load_frame(stamp="20261019-064141-002")[1], p)
        perceive = self.put("20261019-064141-003.json", _window_frame())
        self.put("20261019-064141-003.windows.json", {"windows": []})
        self.assertEqual(load_frame(stamp="20261019-064141-003")[1], perceive)

    def test_explicit_empty_stamp_is_clear_value_error(self):
        self.put("20261019-064141-002.json", "")
        with self.assertRaisesRegex(ValueError, "empty \\(still being written"):
            load_frame(stamp="20261019-064141-002")
        p = self.put("x.json", "{")
        with self.assertRaisesRegex(ValueError, "unreadable"):
            load_frame(json_path=str(p))


def _cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TestImplicitStamp(_OutDir):
    def setUp(self):
        super().setUp()
        p = mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True})
        p.start()
        self.addCleanup(p.stop)

    def test_implicit_go_after_windows_map_uses_perceive_stamp(self):
        self.put("20261019-064141-001.json", _window_frame())
        self.put("20261019-064141-002.windows.json", {"stamp": "20261019-064141-002", "coord_space": "screen",
                                                      "origin": {"left": 0, "top": 0}, "windows": []})
        with mock.patch("cu_perceive.input.click_xy") as click, \
                mock.patch("cu_perceive.act.stamp_identity_error", return_value=None), \
                mock.patch("time.sleep"):
            rc, out, _ = _cli(["act", "--norm", "500,500", "--go"])
        data = json.loads(out)
        self.assertEqual(rc, 0, out)
        self.assertEqual(data["stamp"], "20261019-064141-001")
        self.assertEqual(data["stamp_source"], "latest")
        click.assert_called_once_with(500, 350, double=False, button="left", hold=0.0)

    def test_implicit_go_on_screen_stamp_refused(self):
        self.put("20261019-064141-002.json", _screen_frame())
        with mock.patch("cu_perceive.input.click_xy") as click:
            rc, out, _ = _cli(["act", "--norm", "500,500", "--go"])
        click.assert_not_called()
        self.assertEqual(rc, 1)
        data = json.loads(out)
        self.assertIn("pass --stamp 20261019-064141-002", data["error"])
        self.assertEqual(data["foreground_click_count"], 0)

    def test_implicit_dry_run_on_screen_stamp_warns(self):
        self.put("20261019-064141-002.json", _screen_frame())
        rc, out, _ = _cli(["act", "--norm", "500,500"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(any("--go will refuse" in w for w in json.loads(out)["warnings"]))

    def test_explicit_screen_stamp_go_still_clicks(self):
        self.put("20261019-064141-002.json", _screen_frame())
        with mock.patch("cu_perceive.input.click_xy") as click, mock.patch("time.sleep"):
            rc, out, _ = _cli(["act", "--stamp", "20261019-064141-002", "--norm", "500,500", "--go"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["stamp_source"], "explicit")
        click.assert_called_once()

    def test_cli_focus_flag_reaches_run_chain(self):
        self.put("20261019-064141-001.json", _window_frame())
        with mock.patch("cu_perceive.act.run_chain", return_value={"ok": True}) as rc_:
            _cli(["act", "--stamp", "20261019-064141-001", "--type", "x", "--focus"])
        self.assertTrue(rc_.call_args.kwargs["focus"])
        self.assertEqual(rc_.call_args.kwargs["stamp_source"], "explicit")


if __name__ == "__main__":
    unittest.main()
