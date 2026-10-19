"""Fixes from the Windows 11 test run of PR #2 (UTF-8 output, stamps, --replace,
uia missing on act, truthful click counts, dry-run identity, minimized capture field)."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from cu_perceive import cli, stamps
from cu_perceive.act import _parse_stamp_age_sec, parse_steps, run_chain
from cu_perceive.capture import CaptureResult
from cu_perceive.core import perceive

CJK_TITLE = "\u7231\u53d1\u7535 - \u5c0f\u7ea2\u4e66 \u2014 Chrome"


def _window_frame(**extra):
    base = {
        "stamp": "20261019-064141-001",
        "hwnd": 42,
        "coord_space": "window",
        "origin": {"left": 100, "top": 50, "width": 800, "height": 600},
        "width": 800,
        "height": 600,
        "items": [],
        "identity": {"hwnd": 42, "pid": 4242, "ctime": "133000000000000000"},
    }
    base.update(extra)
    return base


def _screen_frame():
    return {"stamp": "s", "hwnd": None, "coord_space": "screen", "origin": {"left": 0, "top": 0},
            "width": 1000, "height": 1000, "items": []}


class _TmpDir(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)
        self.png = self.dir / "in.png"
        cv2.imwrite(str(self.png), np.full((120, 200, 3), 128, np.uint8))

    def write_frame(self, frame: dict) -> Path:
        p = self.dir / "frame.json"
        p.write_text(json.dumps(frame), encoding="utf-8")
        return p


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TestUtf8Output(unittest.TestCase):
    """windows on a cp1252 console crashed with UnicodeEncodeError on CJK titles."""

    def _cp1252_stdout(self):
        buf = io.BytesIO()
        return buf, io.TextIOWrapper(buf, encoding="cp1252", write_through=True)

    def test_cp1252_stream_really_cannot_encode_cjk(self):
        _buf, stream = self._cp1252_stdout()
        with self.assertRaises(UnicodeEncodeError):
            stream.write(CJK_TITLE)

    def test_windows_no_map_prints_cjk_as_utf8(self):
        buf, stream = self._cp1252_stdout()
        rows = [{"hwnd": 7, "title": CJK_TITLE, "exe": "chrome.exe", "pid": 1, "ctime": "1",
                 "rect": [0, 0, 10, 10], "minimized": False}]
        with mock.patch.object(sys, "stdout", stream), \
                mock.patch("cu_perceive.windows.list_windows", return_value=rows):
            rc = cli.main(["windows", "--no-map"])
            stream.flush()
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue().decode("utf-8"))
        self.assertEqual(data[0]["title"], CJK_TITLE)

    def test_utf8_stdio_leaves_utf8_streams_alone(self):
        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="utf-8")
        with mock.patch.object(sys, "stdout", stream), mock.patch.object(sys, "stderr", stream):
            with mock.patch.object(stream, "reconfigure") as rc:
                cli.utf8_stdio()
        rc.assert_not_called()


class TestUniqueStamps(_TmpDir):
    def test_format_has_milliseconds(self):
        s = stamps.base_stamp(datetime(2026, 10, 19, 6, 41, 41, 123456))
        self.assertEqual(s, "20261019-064141-123")

    def test_same_instant_gets_suffix(self):
        now = datetime(2026, 10, 19, 6, 41, 41, 5000)
        a, pa = stamps.allocate(self.dir, now=now)
        b, pb = stamps.allocate(self.dir, now=now)
        c, _ = stamps.allocate(self.dir, now=now)
        self.assertEqual((a, b, c), ("20261019-064141-005", "20261019-064141-005-2", "20261019-064141-005-3"))
        self.assertFalse(pa.exists() or pb.exists())
        self.assertTrue(stamps.reserve_path(pa).is_file() and stamps.reserve_path(pb).is_file())

    def test_two_perceives_same_millisecond_do_not_overwrite(self):
        out = self.dir / "out"
        with mock.patch("cu_perceive.stamps.base_stamp", return_value="20261019-064141-000"):
            a = perceive(image=str(self.png), out_dir=str(out))
            b = perceive(image=str(self.png), out_dir=str(out))
        self.assertNotEqual(a["stamp"], b["stamp"])
        for key in ("png", "map", "json"):
            self.assertNotEqual(a[key], b[key])
            self.assertTrue(Path(a[key]).is_file() and Path(b[key]).is_file())
        self.assertEqual(json.loads(Path(a["json"]).read_text(encoding="utf-8"))["stamp"], a["stamp"])

    def test_failed_perceive_leaves_no_empty_reservation(self):
        out = self.dir / "out"
        with mock.patch("cu_perceive.core.draw_grid", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                perceive(image=str(self.png), out_dir=str(out))
        self.assertEqual(list(out.glob("*.json")), [])

    def test_windows_map_stamp_unique(self):
        from PIL import Image

        from cu_perceive import windows

        with mock.patch.object(windows, "virtual_screen", return_value=(0, 0, 64, 48)), \
                mock.patch("PIL.ImageGrab.grab", return_value=Image.new("RGB", (64, 48))), \
                mock.patch("cu_perceive.stamps.base_stamp", return_value="20261019-064141-000"):
            a = windows.windows_map(out_dir=str(self.dir), rows=[])
            b = windows.windows_map(out_dir=str(self.dir), rows=[])
        self.assertNotEqual(a["stamp"], b["stamp"])
        self.assertNotEqual(a["map"], b["map"])
        self.assertTrue(Path(a["json"]).stat().st_size > 0 and Path(b["json"]).stat().st_size > 0)

    def test_age_parses_new_and_old_stamps(self):
        for s in ("20200101-000000", "20200101-000000-250", "20200101-000000-250-3"):
            self.assertGreater(_parse_stamp_age_sec(s), 1e6, s)
        self.assertIsNone(stamps.parse_time("not-a-stamp"))
        self.assertEqual(stamps.parse_time("20200101-000000-250").microsecond, 250000)


class TestReplace(_TmpDir):
    def test_parse_steps_inserts_select_all_before_type(self):
        specs = parse_steps(norm="491,63", text="https://example.org", key="enter", replace=True)
        self.assertEqual(
            [(s["kind"], s.get("key") or s.get("text")) for s in specs],
            [("norm", None), ("type", "https://example.org"), ("key", "enter")],
        )
        self.assertTrue(specs[1]["replace"])
        self.assertNotIn("replace", specs[2])

    def test_default_unchanged(self):
        specs = parse_steps(norm="491,63", text="x")
        self.assertEqual([s["kind"] for s in specs], ["norm", "type"])

    def test_cli_replace_dry_run_plan(self):
        p = self.write_frame(_screen_frame())
        rc, out, _ = _run_cli(["act", "--json", str(p), "--norm", "491,63", "--type", "URL", "--replace", "--key", "enter"])
        self.assertEqual(rc, 0, out)
        steps = json.loads(out)["steps"]
        self.assertEqual([s["via"] for s in steps], ["norm", "type", "key"])
        self.assertTrue(steps[1]["replace"])
        self.assertEqual(steps[1]["replace_via"], "clear_edit")
        self.assertNotIn("replace", steps[2])

    def test_live_order_select_all_then_paste(self):
        calls = []
        with mock.patch("cu_perceive.input.click_xy", side_effect=lambda *a, **k: calls.append("click")), \
                mock.patch("cu_perceive.input.tap_key", side_effect=lambda k: calls.append(f"key:{k}")), \
                mock.patch("cu_perceive.input.enter_text", side_effect=lambda t, via="paste": calls.append(f"type:{t}")), \
                mock.patch("cu_perceive.input.clear_field_for_replace",
                           side_effect=lambda **k: calls.append("clear") or {
                               "ok": True, "method": "value_pattern", "empty": True, "verified": True}), \
                mock.patch("cu_perceive.windows.window_at_point", return_value=77), \
                mock.patch("cu_perceive.windows.foreground_matches", return_value=(True, 77)), \
                mock.patch("time.sleep"):
            out = run_chain(_screen_frame(), parse_steps(norm="491,63", text="URL", key="enter", replace=True), dry_run=False)
        self.assertTrue(out["ok"], out)
        self.assertEqual(calls, ["click", "clear", "type:URL", "key:enter"])
        self.assertEqual(out["steps"][1]["replace_clear"]["method"], "value_pattern")


class TestActUiaMissing(_TmpDir):
    def setUp(self):
        super().setUp()
        p = mock.patch("cu_perceive.extras.missing_modules", return_value=["uiautomation"])
        p.start()
        self.addCleanup(p.stop)

    def test_cli_exit_3_with_install(self):
        p = self.write_frame(_window_frame())
        rc, out, err = _run_cli(["act", "--json", str(p), "--norm", "500,500", "--method", "uia"])
        self.assertEqual(rc, 3)
        data = json.loads(out)
        self.assertFalse(data["ok"])
        self.assertEqual(data["missing_extra"], "uia")
        self.assertIn("pip install", data["install"])
        self.assertEqual(data["method"], "uia")
        self.assertIn("uia", err)

    def test_run_chain_never_resolves_uia(self):
        with mock.patch("cu_perceive.uia.resolve_at_screen") as resolve:
            out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=False, method="uia")
        resolve.assert_not_called()
        self.assertEqual(out["missing_extra"], "uia")

    def test_auto_falls_back_with_warning(self):
        with mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}):
            out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=True, method="auto")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["chosen_method"], "input")
        self.assertTrue(any("uia extra not installed" in w for w in out["warnings"]))


class TestTruthfulClickCount(unittest.TestCase):
    def test_refused_go_reports_zero(self):
        frame = _window_frame()
        del frame["identity"]
        with mock.patch("cu_perceive.input.click_xy") as click:
            out = run_chain(frame, parse_steps(norm="500,500"), dry_run=False)
        click.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertEqual(out["foreground_click_count"], 0)
        self.assertFalse(out["foreground_used"])
        self.assertEqual(out["planned_foreground_click_count"], 1)

    def test_dry_run_reports_plan(self):
        out = run_chain(_screen_frame(), parse_steps(norm="1,1;2,2"), dry_run=True)
        self.assertEqual(out["foreground_click_count"], 0)
        self.assertFalse(out["foreground_used"])
        self.assertEqual(out["planned_foreground_click_count"], 2)

    def test_go_counts_clicks_sent(self):
        with mock.patch("cu_perceive.input.click_xy"), mock.patch("time.sleep"):
            out = run_chain(_screen_frame(), parse_steps(norm="1,1;2,2"), dry_run=False)
        self.assertEqual(out["foreground_click_count"], 2)
        self.assertTrue(out["foreground_used"])

    def test_mid_chain_abort_counts_only_sent(self):
        with mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.act.stamp_identity_error", side_effect=[None, "reused"]), \
                mock.patch("cu_perceive.input.click_xy") as click, mock.patch("time.sleep"), \
                mock.patch("cu_perceive.windows.point_owner", return_value=(True, {"hwnd": 42, "title": "T"})):
            out = run_chain(_window_frame(), parse_steps(norm="1,1;2,2"), dry_run=False)
        self.assertEqual(click.call_count, 1)
        self.assertEqual(out["identity_abort_step"], 1)
        self.assertEqual(out["foreground_click_count"], 1)
        self.assertEqual(out["planned_foreground_click_count"], 2)

    def test_uia_go_counts_no_foreground(self):
        with mock.patch("cu_perceive.extras.missing_modules", return_value=[]), \
                mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True}), \
                mock.patch("cu_perceive.act.stamp_identity_error", return_value=None), \
                mock.patch("cu_perceive.uia.resolve_at_screen", return_value={
                    "ok": True, "_control": object(), "preferred_action": "Invoke",
                    "name": "OK", "control_type": "ButtonControl", "patterns": ["Invoke"]}), \
                mock.patch("cu_perceive.uia.invoke_control", return_value={"action": "Invoke"}):
            out = run_chain(_window_frame(), parse_steps(norm="1,1"), dry_run=False, method="uia")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["foreground_click_count"], 0)


class TestMinimizedAndDwm(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.minimized = False
        win = {"hwnd": 77, "title": "t", "exe": "e.exe", "pid": 1, "ctime": "2", "rect": [0, 0, 10, 10]}
        origin = {"left": 0, "top": 0, "width": 64, "height": 48}
        img = np.full((48, 64, 3), 90, np.uint8)
        for target, kw in {
            "cu_perceive.windows.find_window": {"return_value": win},
            "cu_perceive.windows.is_hwnd_minimized": {"side_effect": lambda h: self.minimized},
            "cu_perceive.windows.window_identity": {"return_value": {"hwnd": 77, "pid": 1, "ctime": "2"}},
            "cu_perceive.windows.verify_window_identity": {"return_value": None},
            "cu_perceive.windows.live_geom": {"return_value": (origin, win)},
            "cu_perceive.windows.capture_window_image": {"return_value": CaptureResult(img, "dwm", [])},
        }.items():
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def test_minimized_result_has_capture_field(self):
        self.minimized = True
        out = perceive(hwnd=77, out_dir=self.td.name)
        self.assertTrue(out["minimized"])
        self.assertEqual(out["capture"], {"backend": None, "attempts": [], "skipped": "minimized"})

    def test_explicit_dwm_warns_experimental(self):
        out = perceive(hwnd=77, out_dir=self.td.name, capture="dwm")
        self.assertTrue(any("experimental" in w for w in out["warnings"]))

    def test_auto_has_no_dwm_warning(self):
        out = perceive(hwnd=77, out_dir=self.td.name)
        self.assertFalse(any("experimental" in w for w in out.get("warnings") or []))


if __name__ == "__main__":
    unittest.main()
