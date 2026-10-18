"""perceive(hwnd) captures in place: no focus, minimized reported, identity stamped."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from cu_perceive.capture import CaptureError, CaptureResult
from cu_perceive.core import perceive

ORIGIN = {"left": 300, "top": 200, "width": 640, "height": 480}
IDENT = {"hwnd": 77, "pid": 1234, "ctime": "133700000000000000"}
WIN = {"hwnd": 77, "title": "Notepad", "exe": "notepad.exe", "pid": 1234, "ctime": IDENT["ctime"],
       "exe_path": "C:\\Windows\\notepad.exe", "rect": [292, 169, 948, 688], "minimized": False}


def _frame_img():
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[100:200, 100:300] = 180
    return img


class _Mocks(unittest.TestCase):
    minimized = False

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.out = Path(self.td.name)
        patches = {
            "find": mock.patch("cu_perceive.windows.find_window", return_value=dict(WIN)),
            "minimized": mock.patch("cu_perceive.windows.is_hwnd_minimized", side_effect=lambda h: self.minimized),
            "identity": mock.patch("cu_perceive.windows.window_identity", return_value=dict(IDENT)),
            "verify": mock.patch("cu_perceive.windows.verify_window_identity", return_value=None),
            "live": mock.patch("cu_perceive.windows.live_geom", return_value=(dict(ORIGIN), dict(WIN))),
            "refresh": mock.patch("cu_perceive.windows.refresh_geom", return_value=(dict(ORIGIN), dict(WIN))),
            "focus": mock.patch("cu_perceive.windows.force_foreground", return_value=True),
            "capture": mock.patch(
                "cu_perceive.windows.capture_window_image",
                return_value=CaptureResult(_frame_img(), "wgc", [{"backend": "wgc", "ok": True}]),
            ),
            "implausible": mock.patch("cu_perceive.windows.capture_looks_implausible", return_value=False),
            "sleep": mock.patch("time.sleep"),
        }
        self.m = {}
        for k, p in patches.items():
            self.m[k] = p.start()
            self.addCleanup(p.stop)


class TestPerceiveNoFocus(_Mocks):
    def test_does_not_focus_or_restore(self):
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.m["focus"].assert_not_called()
        self.m["refresh"].assert_not_called()
        self.m["live"].assert_called_once_with(77)
        _args, kw = self.m["capture"].call_args
        self.assertFalse(kw["focused"])
        self.assertEqual(kw["capture"], "auto")
        self.assertFalse(out["focused"])
        self.assertEqual(out["capture"]["backend"], "wgc")

    def test_map_contract_unchanged(self):
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.assertEqual(out["coord_space"], "window")
        self.assertEqual((out["width"], out["height"]), (640, 480))
        self.assertEqual(out["origin"], ORIGIN)
        self.assertEqual(out["source"], "hwnd:77")
        for key in ("png", "map", "grid", "json"):
            self.assertTrue(Path(out[key]).is_file(), key)

    def test_identity_stamped_to_disk(self):
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.assertEqual(out["identity"], IDENT)
        disk = json.loads(Path(out["json"]).read_text(encoding="utf-8"))
        self.assertEqual(disk["identity"], IDENT)
        self.assertEqual(disk["window"]["ctime"], IDENT["ctime"])

    def test_capture_choice_passed(self):
        perceive(hwnd=77, out_dir=str(self.out), capture="printwindow")
        self.assertEqual(self.m["capture"].call_args.kwargs["capture"], "printwindow")

    def test_bad_capture_choice(self):
        with self.assertRaises(ValueError):
            perceive(hwnd=77, out_dir=str(self.out), capture="mss")

    def test_capture_failure_reports_and_hints_focus(self):
        self.m["capture"].side_effect = CaptureError("every window capture backend failed", [{"backend": "wgc"}])
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.assertFalse(out["ok"])
        self.assertIsNone(out["stamp"])
        self.assertIn("focus=true", out["error"])
        self.assertEqual(out["capture"]["attempts"], [{"backend": "wgc"}])
        self.assertEqual(list(self.out.iterdir()), [])

    def test_window_changed_during_capture(self):
        self.m["verify"].return_value = "hwnd 77 now belongs to pid 999"
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.assertFalse(out["ok"])
        self.assertIn("changed during capture", out["error"])
        self.assertEqual(list(self.out.iterdir()), [])

    def test_blank_capture_warns(self):
        self.m["capture"].return_value = CaptureResult(
            np.zeros((480, 640, 3), np.uint8), "wgc", [], blank=True
        )
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.assertTrue(out["capture"]["blank"])
        self.assertTrue(any("black" in w for w in out["warnings"]))

    def test_screen_perceive_has_no_identity(self):
        with mock.patch("cu_perceive.core.ImageGrab.grab") as grab:
            from PIL import Image
            grab.return_value = Image.new("RGB", (320, 200), (10, 20, 30))
            out = perceive(out_dir=str(self.out))
        self.assertEqual(out["coord_space"], "screen")
        self.assertNotIn("identity", out)
        self.m["capture"].assert_not_called()


class TestPerceiveMinimized(_Mocks):
    minimized = True

    def test_minimized_reported_not_restored(self):
        out = perceive(hwnd=77, out_dir=str(self.out))
        self.assertFalse(out["ok"])
        self.assertTrue(out["minimized"])
        self.assertIn("minimized", out["error"])
        self.assertIn("focus=true", out["error"])
        self.assertIsNone(out["stamp"])
        self.assertIsNone(out["json"])
        self.m["focus"].assert_not_called()
        self.m["refresh"].assert_not_called()
        self.m["capture"].assert_not_called()
        self.assertEqual(list(self.out.iterdir()), [])

    def test_focus_true_restores_explicitly(self):
        out = perceive(hwnd=77, out_dir=str(self.out), focus=True)
        self.m["focus"].assert_called_once_with(77)
        self.m["refresh"].assert_called()
        self.assertTrue(self.m["capture"].call_args.kwargs["focused"])
        self.assertTrue(out["focused"])
        self.assertTrue(Path(out["json"]).is_file())


class TestCliMinimized(_Mocks):
    minimized = True

    def test_cli_prints_and_exits_1(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from cu_perceive.cli import main

        buf, err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(err):
            code = main(["perceive", "--hwnd", "77", "--out", str(self.out)])
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["minimized"])
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
