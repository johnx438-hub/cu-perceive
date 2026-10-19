"""P0: onnxruntime in the capture process makes the next WGC grab crash it. OCR/YOLO run
in a child process, and capture never starts WGC once onnxruntime is loaded."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from cu_perceive import core, detect_worker, extras
from cu_perceive.capture import CaptureError, capture_window, wgc_blocker

ROOT = Path(__file__).resolve().parents[1]
GEOM = {"client": (0, 0, 40, 30), "window": (0, 0, 40, 30), "frame": (0, 0, 40, 30)}
FRAME = np.full((30, 40, 3), 120, dtype=np.uint8)


class TestWorkerProcess(unittest.TestCase):
    def setUp(self):
        os.environ.pop(detect_worker.ENV_INPROC, None)

    def test_runs_in_another_process(self):
        info = detect_worker.run("ping")
        self.assertNotEqual(info["pid"], os.getpid())

    def test_ocr_never_loads_onnxruntime_here(self):
        before = "onnxruntime" in sys.modules
        try:
            out = detect_worker.ocr(np.zeros((20, 20, 3), np.uint8))
            self.assertIsInstance(out, list)
        except extras.MissingExtraError as e:
            self.assertFalse(extras.available("ocr"))
            self.assertEqual(e.extra, "ocr")
            self.assertIn("pip install", e.command)
        self.assertEqual("onnxruntime" in sys.modules, before)

    def test_dead_worker_is_restarted(self):
        first = detect_worker.run("ping")["pid"]
        detect_worker._WORKER.proc.kill()
        detect_worker._WORKER.proc.wait()
        second = detect_worker.run("ping")["pid"]
        self.assertNotEqual(first, second)

    def test_crash_mid_call_is_reported_and_reset(self):
        detect_worker.run("ping")
        with mock.patch.object(detect_worker, "_read", return_value=None):
            with self.assertRaises(detect_worker.DetectWorkerError) as cm:
                detect_worker.run("ocr", FRAME)
        self.assertIn("exited during ocr", str(cm.exception))
        self.assertIsNone(detect_worker._WORKER.proc)
        self.assertIn("pid", detect_worker.run("ping"))

    def test_inproc_env_runs_here(self):
        with mock.patch.dict(os.environ, {detect_worker.ENV_INPROC: "1"}):
            self.assertEqual(detect_worker.run("ping")["pid"], os.getpid())


class TestServeLoop(unittest.TestCase):
    def test_replies_and_errors(self):
        req = io.BytesIO()
        detect_worker._write(req, ("ping", None))
        detect_worker._write(req, ("nope", None))
        req.seek(0)
        resp = io.BytesIO()
        detect_worker.serve(stdin=req, stdout=resp)
        resp.seek(0)
        kind, payload = detect_worker._read(resp)
        self.assertEqual(kind, "ok")
        self.assertEqual(payload["pid"], os.getpid())
        kind, payload = detect_worker._read(resp)
        self.assertEqual(kind, "error")
        self.assertIn("unknown detector op", payload)

    def test_missing_extra_round_trips(self):
        err = extras.MissingExtraError("ocr", ["onnxruntime"], "pip install x")
        back = detect_worker._decode_error(*detect_worker._encode_error(err))
        self.assertIsInstance(back, extras.MissingExtraError)
        self.assertEqual((back.extra, back.missing, back.command), ("ocr", ["onnxruntime"], "pip install x"))


class TestWgcGuard(unittest.TestCase):
    def _capture(self, order):
        called = []

        def wgc(h):
            called.append("wgc")
            return FRAME, None

        def pw(h):
            called.append("printwindow")
            return FRAME, None

        res = capture_window(5, (40, 30), order=order, is_minimized=lambda h: False,
                             geometry=lambda h: GEOM, backends={"wgc": wgc, "printwindow": pw})
        return res, called

    def test_auto_uses_wgc_normally(self):
        with mock.patch.dict(sys.modules):
            sys.modules.pop("onnxruntime", None)
            self.assertIsNone(wgc_blocker())
            res, called = self._capture(("wgc", "printwindow"))
        self.assertEqual(res.backend, "wgc")

    def test_auto_skips_wgc_once_onnxruntime_is_loaded(self):
        with mock.patch.dict(sys.modules, {"onnxruntime": mock.MagicMock()}):
            res, called = self._capture(("wgc", "printwindow"))
        self.assertEqual(called, ["printwindow"])
        self.assertEqual(res.backend, "printwindow")
        self.assertEqual(res.attempts[0]["skipped"], "unsafe")
        self.assertIn("onnxruntime is loaded", res.attempts[0]["error"])

    def test_explicit_wgc_refuses_instead_of_crashing(self):
        with mock.patch.dict(sys.modules, {"onnxruntime": mock.MagicMock()}):
            with self.assertRaises(CaptureError) as cm:
                self._capture(("wgc",))
        self.assertEqual(cm.exception.attempts[0]["skipped"], "unsafe")


class TestPerceiveUsesWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.img = self.tmp / "in.png"
        cv2.imwrite(str(self.img), np.full((100, 200, 3), 200, np.uint8))

    def test_ocr_goes_through_worker(self):
        raw = [{"bbox": [100, 100, 300, 300], "text": "OK"}]
        with mock.patch.object(extras, "require"), \
                mock.patch.object(detect_worker, "ocr", return_value=raw) as ocr, \
                mock.patch.object(core, "_parser", side_effect=AssertionError("in-process OCR")):
            out = core.perceive(image=str(self.img), out_dir=str(self.tmp / "o"), ocr=True)
        ocr.assert_called_once()
        self.assertEqual(out["items"][0]["text"], "OK")

    def test_worker_crash_keeps_the_stamp_and_warns(self):
        boom = detect_worker.DetectWorkerError("the OCR/YOLO worker process exited during ocr")
        with mock.patch.object(extras, "require"), mock.patch.object(detect_worker, "ocr", side_effect=boom):
            out = core.perceive(image=str(self.img), out_dir=str(self.tmp / "o"), ocr=True)
        self.assertTrue(Path(out["json"]).is_file())
        self.assertEqual(out["items"], [])
        self.assertIn("OCR failed", out["warnings"][0])


WIN_SCRIPT = r'''
import json, sys, tempfile, time, tkinter as tk
root = tk.Tk()
root.geometry("420x300+200+200")
tk.Label(root, text="cu-perceive WGC regression", font=("Segoe UI", 20)).pack(expand=True)
for _ in range(10):
    root.update(); time.sleep(0.05)
hwnd = int(root.wm_frame(), 16)
from cu_perceive.core import perceive
out = tempfile.mkdtemp()
res = {"first": perceive(hwnd=hwnd, out_dir=out)["capture"]}
if sys.argv[1] == "inproc":
    import onnxruntime  # noqa: F401
else:
    perceive(hwnd=hwnd, out_dir=out, ocr=True)
for i in range(3):
    root.update()
    res[f"after{i}"] = perceive(hwnd=hwnd, out_dir=out)["capture"]
res["onnx_in_parent"] = "onnxruntime" in sys.modules
root.destroy()
print(json.dumps(res))
'''


@unittest.skipUnless(sys.platform == "win32", "Windows-only: WGC + onnxruntime crash regression")
class TestWindowsWgcAfterOnnxruntime(unittest.TestCase):
    def _run(self, mode):
        if importlib.util.find_spec("onnxruntime") is None:
            self.skipTest("onnxruntime not installed (pip install cu-perceive[ocr])")
        if importlib.util.find_spec("tkinter") is None:
            self.skipTest("tkinter not available")
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        env.pop(detect_worker.ENV_INPROC, None)
        proc = subprocess.run([sys.executable, "-c", WIN_SCRIPT, mode], capture_output=True, text=True,
                              timeout=180, env=env, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0, f"process died (0xC0000005 = access violation): {proc.returncode}\n{proc.stderr[-2000:]}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_ocr_then_capture_keeps_wgc(self):
        res = self._run("worker")
        self.assertFalse(res["onnx_in_parent"])
        for i in range(3):
            self.assertEqual(res[f"after{i}"]["backend"], res["first"]["backend"])

    def test_onnxruntime_in_process_falls_back_to_printwindow(self):
        res = self._run("inproc")
        for i in range(3):
            self.assertEqual(res[f"after{i}"]["backend"], "printwindow")
            self.assertEqual(res[f"after{i}"]["attempts"][0]["skipped"], "unsafe")


if __name__ == "__main__":
    unittest.main()
