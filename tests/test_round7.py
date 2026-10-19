"""Round 7 Windows findings: detection worker timeout, unknown / conflicting act args,
large scroll counts, array points, the INPROC WGC refusal message, and the per-window
newest-stamp pointer."""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np

from test_agent_friction import _mcp_tools
from test_round6 import _OutDir, _screen, _win

from cu_perceive import act, core, detect_worker, extras, mcp_server, stamps
from cu_perceive.act import MAX_SCROLL_NOTCHES, coerce_points, latest_frame, parse_steps, run_chain
from cu_perceive.capture import CaptureError, capture_window

HANG_FIRST = (
    "import os, time\n"
    "flag = os.environ['CU_TEST_HANG_FLAG']\n"
    "if not os.path.exists(flag):\n"
    "    open(flag, 'w').close()\n"
    "    time.sleep(120)\n"
    "from cu_perceive.detect_worker import serve; serve()\n"
)


class TestWorkerTimeout(unittest.TestCase):
    def setUp(self):
        os.environ.pop(detect_worker.ENV_INPROC, None)
        self.w = detect_worker._Worker()
        self.addCleanup(self._close)

    def _close(self):
        if self.w.proc is not None:
            self.w.proc.kill()
            self.w.proc.wait()

    def test_hung_worker_is_killed_restarted_and_the_next_call_works(self):
        flag = Path(tempfile.mkdtemp()) / "hung"
        with mock.patch.dict(os.environ, {"CU_TEST_HANG_FLAG": str(flag)}), \
                mock.patch.object(detect_worker, "CHILD_MAIN", HANG_FIRST):
            t0 = time.monotonic()
            with self.assertRaises(detect_worker.DetectWorkerTimeout) as cm:
                self.w.call("ping", timeout=0.4)
            self.assertLess(time.monotonic() - t0, 10)
            self.assertIn("killed and restarted", str(cm.exception))
            self.assertIn(detect_worker.ENV_TIMEOUT, str(cm.exception))
            self.assertIsNotNone(self.w.proc, "a fresh worker is started right away")
            self.assertIsNone(self.w.proc.poll())
            info = self.w.call("ping", timeout=20)
        self.assertEqual(info["pid"], self.w.proc.pid)

    def test_timeout_kills_the_hung_process(self):
        flag = Path(tempfile.mkdtemp()) / "hung"
        with mock.patch.dict(os.environ, {"CU_TEST_HANG_FLAG": str(flag)}), \
                mock.patch.object(detect_worker, "CHILD_MAIN", HANG_FIRST):
            procs = []
            orig = self.w._start

            def start():
                procs.append(orig())
                return procs[-1]

            with mock.patch.object(self.w, "_start", side_effect=start):
                with self.assertRaises(detect_worker.DetectWorkerTimeout):
                    self.w.call("ping", timeout=0.3)
        self.assertEqual(len(procs), 2)
        self.assertIsNotNone(procs[0].poll(), "the hung worker is still running")
        self.assertIs(self.w.proc, procs[1])

    def test_first_call_of_a_new_worker_gets_more_time(self):
        flag = Path(tempfile.mkdtemp()) / "hung"
        with mock.patch.dict(os.environ, {"CU_TEST_HANG_FLAG": str(flag)}), \
                mock.patch.object(detect_worker, "CHILD_MAIN", HANG_FIRST):
            with self.assertRaises(detect_worker.DetectWorkerTimeout) as cm:
                self.w.call("ping", timeout=0.2)
        self.assertIn(f"{0.2 * detect_worker.FIRST_CALL_FACTOR:g}s", str(cm.exception))

    def test_busy_lock_times_out_instead_of_queueing_forever(self):
        held = threading.Event()
        release = threading.Event()

        def hold():
            with self.w.lock:
                held.set()
                release.wait(10)

        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        try:
            t0 = time.monotonic()
            with self.assertRaises(detect_worker.DetectWorkerTimeout) as cm:
                self.w.call("ocr", np.zeros((4, 4, 3), np.uint8), timeout=0.2)
            self.assertLess(time.monotonic() - t0, 2)
            self.assertIn("busy", str(cm.exception))
        finally:
            release.set()
            t.join()

    def test_timeout_setting(self):
        cases = {"": 30.0, "5": 5.0, "2.5": 2.5, "0": None, "off": None, "junk": 30.0, "-1": None}
        for raw, want in cases.items():
            with mock.patch.dict(os.environ, {detect_worker.ENV_TIMEOUT: raw}):
                self.assertEqual(detect_worker.detect_timeout(), want, raw)


class TestPerceiveWithAHungWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.img = self.tmp / "in.png"
        cv2.imwrite(str(self.img), np.full((100, 200, 3), 200, np.uint8))

    def test_timeout_keeps_the_stamp_and_warns(self):
        boom = detect_worker.DetectWorkerTimeout("ocr took longer than 30s; the OCR/YOLO worker was killed and restarted")
        with mock.patch.object(extras, "require"), mock.patch.object(detect_worker, "ocr", side_effect=boom):
            out = core.perceive(image=str(self.img), out_dir=str(self.tmp / "o"), ocr=True)
        self.assertTrue(Path(out["json"]).is_file())
        self.assertEqual(out["items"], [])
        self.assertIn("killed and restarted", out["warnings"][0])

    def test_plain_perceive_never_waits_on_the_worker_lock(self):
        with detect_worker._WORKER.lock:
            done = {}

            def go():
                done["out"] = core.perceive(image=str(self.img), out_dir=str(self.tmp / "o"))

            t = threading.Thread(target=go)
            t.start()
            t.join(20)
            self.assertFalse(t.is_alive(), "plain perceive blocked on the detection lock")
        self.assertTrue(Path(done["out"]["json"]).is_file())


class TestRawImageTransport(unittest.TestCase):
    def test_image_round_trips_without_pickling_the_pixels(self):
        img = np.random.randint(0, 255, (300, 400, 3), np.uint8)
        buf = io.BytesIO()
        detect_worker._write_request(buf, "ocr", img)
        n_obj, n_raw = detect_worker._HEADER.unpack(buf.getvalue()[: detect_worker._HEADER.size])
        self.assertLess(n_obj, 512)
        self.assertEqual(n_raw, img.nbytes)
        buf.seek(0)
        (op, meta), raw = detect_worker._read_message(buf)
        back = detect_worker._unpack_image(meta, raw)
        self.assertEqual(op, "ocr")
        self.assertTrue(np.array_equal(back, img))
        self.assertTrue(back.flags.writeable)

    def test_non_contiguous_image(self):
        img = np.random.randint(0, 255, (50, 80, 4), np.uint8)[:, ::2, :3]
        buf = io.BytesIO()
        detect_worker._write_request(buf, "yolo", img)
        buf.seek(0)
        (_, meta), raw = detect_worker._read_message(buf)
        self.assertTrue(np.array_equal(detect_worker._unpack_image(meta, raw), img))

    def test_worker_gets_the_same_pixels(self):
        os.environ.pop(detect_worker.ENV_INPROC, None)
        img = np.arange(12 * 10 * 3, dtype=np.uint8).reshape(12, 10, 3)
        seen = {}

        def fake_run_local(op, image=None):
            seen["img"] = image
            return []

        req = io.BytesIO()
        detect_worker._write_request(req, "ocr", img)
        req.seek(0)
        resp = io.BytesIO()
        with mock.patch.object(detect_worker, "run_local", fake_run_local):
            detect_worker.serve(stdin=req, stdout=resp)
        self.assertTrue(np.array_equal(seen["img"], img))
        resp.seek(0)
        self.assertEqual(detect_worker._read(resp), ("ok", []))


class TestUnknownArgs(unittest.TestCase):
    def _server(self):
        calls = []

        class Base:
            async def list_tools(self):
                return [SimpleNamespace(name="act", inputSchema={"properties": {"norm": {}, "go": {}, "dry_run": {}}}),
                        SimpleNamespace(name="apps", input_schema={"properties": {}})]

            async def call_tool(self, name, arguments, *a, **k):
                calls.append((name, arguments))
                return "ran"

        return mcp_server.strict_server_cls(Base)(), calls

    def test_steps_is_refused_and_nothing_runs(self):
        srv, calls = self._server()
        with self.assertRaises(Exception) as cm:
            asyncio.run(srv.call_tool("act", {"steps": [{"action": "click", "x": 1}], "go": True}))
        payload = json.loads(str(cm.exception))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "unknown_args")
        self.assertEqual(payload["unknown_args"], ["steps"])
        self.assertIn("norm", payload["valid_args"])
        self.assertIn("no steps parameter", payload["hint"])
        self.assertEqual(calls, [])

    def test_known_args_pass_through(self):
        srv, calls = self._server()
        self.assertEqual(asyncio.run(srv.call_tool("act", {"norm": "1,1", "go": False})), "ran")
        self.assertEqual(asyncio.run(srv.call_tool("apps", {})), "ran")
        self.assertEqual(len(calls), 2)

    def test_snake_case_schema_attribute(self):
        srv, calls = self._server()
        with self.assertRaises(Exception) as cm:
            asyncio.run(srv.call_tool("apps", {"bogus": 1}))
        self.assertEqual(json.loads(str(cm.exception))["unknown_args"], ["bogus"])

    def test_error_payload(self):
        out = mcp_server.unknown_args_error("act", ["x", "zzz"], ["norm", "xy"])
        self.assertEqual(out["unknown_args"], ["x", "zzz"])
        self.assertIn('norm="x,y"', out["hint"])
        self.assertIn("nothing was run", out["error"])


class TestDryRunAlias(_OutDir):
    def setUp(self):
        super().setUp()
        self.put(_screen("20261019-110000-001"))
        self.act = _mcp_tools()["act"]

    def test_dry_run_true_wins_over_go(self):
        with mock.patch.object(act, "run_chain", wraps=act.run_chain) as rc:
            tools = _mcp_tools()
            out = tools["act"](norm="500,500", go=True, dry_run=True)
        self.assertTrue(rc.call_args.kwargs["dry_run"])
        self.assertTrue(out["dry_run"])
        self.assertIn("dry_run=true overrides go=true", out["warnings"][0])

    def test_dry_run_false_alone_does_not_send(self):
        out = self.act(norm="500,500", dry_run=False)
        self.assertTrue(out["dry_run"])
        self.assertIn("pass go=true", out["warnings"][0])

    def test_dry_run_is_in_the_tool_schema(self):
        import inspect

        self.assertIn("dry_run", inspect.signature(self.act).parameters)


class TestScrollClamp(_OutDir):
    def test_large_counts_are_capped_with_a_warning(self):
        for raw, want in (("down:1000", -MAX_SCROLL_NOTCHES), ("up:51", MAX_SCROLL_NOTCHES), (-1000, -MAX_SCROLL_NOTCHES)):
            specs = parse_steps(norm="500,500", scroll=raw)
            s = [x for x in specs if x["kind"] == "scroll"][0]
            self.assertEqual(s["notches"], want, raw)
            out = run_chain(_screen("s"), specs, dry_run=True)
            self.assertTrue(out["scroll_clamped"], raw)
            self.assertTrue(any("wheel notches" in w and "not pixels" in w for w in out["warnings"]), out)
            self.assertEqual([x["notches"] for x in out["steps"] if x["via"] == "scroll"], [want])

    def test_max_itself_is_not_clamped(self):
        out = run_chain(_screen("s"), parse_steps(norm="500,500", scroll="down:50"), dry_run=True)
        self.assertNotIn("scroll_clamped", out)
        self.assertFalse(any("wheel notches" in w for w in out.get("warnings", [])))

    def test_cli_steps_string_is_clamped_too(self):
        self.assertEqual(parse_steps(steps="scroll:-500")[0]["notches"], -MAX_SCROLL_NOTCHES)

    def test_mcp(self):
        self.put(_screen("20261019-110000-002"))
        out = _mcp_tools()["act"](norm="500,500", scroll="down:1000")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["scroll_clamped"])
        self.assertEqual([s["notches"] for s in out["steps"] if s["via"] == "scroll"], [-MAX_SCROLL_NOTCHES])


class TestArrayPoints(_OutDir):
    def test_coerce(self):
        cases = [
            ([640, 500], "640,500"),
            ((640, 500), "640,500"),
            ([640.4, 499.6], "640,500"),
            ([[1, 2], [3, 4]], "1,2;3,4"),
            ({"x": 5, "y": 6}, "5,6"),
            ([{"x": 5, "y": 6}, [7, 8]], "5,6;7,8"),
            ("[640, 500]", "640,500"),
            ("[[1,2],[3,4]]", "1,2;3,4"),
            (["1,2", "3,4"], "1,2;3,4"),
            ("640,500", "640,500"),
            ([], None),
            ("", None),
            (None, None),
        ]
        for raw, want in cases:
            self.assertEqual(coerce_points(raw), want, raw)

    def test_bad_points(self):
        for raw in ([1, 2, 3], {"x": 1}, "[1,", [True, False], 5):
            with self.assertRaises(ValueError, msg=raw):
                pairs = coerce_points(raw)
                act.pairs(pairs)

    def test_parse_steps(self):
        self.assertEqual(parse_steps(norm=[640, 500]), [{"kind": "norm", "norm": [640, 500]}])
        self.assertEqual([s["xy"] for s in parse_steps(xy=[[1, 2], [3, 4]])], [[1, 2], [3, 4]])
        self.assertEqual(parse_steps(drag_norm=[[100, 100], [200, 200]])[0]["points"], [[100, 100], [200, 200]])

    def test_mcp(self):
        self.put(_screen("20261019-110000-003"))
        tools = _mcp_tools()
        out = tools["act"](norm=[640, 500])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["steps"][0]["norm"], [640, 500])
        out = tools["act"](xy="[10, 20]")
        self.assertEqual((out["steps"][0]["via"], out["steps"][0]["center"]), ("xy", [10, 20]))
        out = tools["act"](norm=[1, 2, 3])
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "bad_args")

    def test_act_description_states_the_array_form_early(self):
        head = _act_doc()[:200]
        self.assertIn("[x,y]", head)
        self.assertIn("dry_run=true wins", head)
        self.assertIn("notches", head)


def _act_doc() -> str:
    return _mcp_tools()["act"].__doc__


class TestInprocWgcRefusal(unittest.TestCase):
    GEOM = {"client": (0, 0, 40, 30), "window": (0, 0, 40, 30), "frame": (0, 0, 40, 30)}

    def test_message_names_the_guard(self):
        with mock.patch.dict(sys.modules, {"onnxruntime": mock.MagicMock()}):
            with self.assertRaises(CaptureError) as cm:
                capture_window(5, (40, 30), order=("wgc",), is_minimized=lambda h: False,
                               geometry=lambda h: self.GEOM, backends={"wgc": lambda h: None})
        e = cm.exception
        self.assertIn("capture=wgc refused by the onnxruntime guard", str(e))
        self.assertEqual(e.reason, "wgc_refused_onnxruntime_guard")
        self.assertIn("capture=printwindow", e.hint)
        self.assertIn("CU_PERCEIVE_DETECT_INPROC", e.hint)

    def test_other_failures_keep_the_generic_message(self):
        def boom(h):
            raise OSError("nope")

        with self.assertRaises(CaptureError) as cm:
            capture_window(5, (40, 30), order=("printwindow",), is_minimized=lambda h: False,
                           geometry=lambda h: self.GEOM, backends={"printwindow": boom})
        self.assertIn("every window capture backend failed", str(cm.exception))
        self.assertIsNone(cm.exception.reason)

    def test_perceive_passes_reason_and_hint(self):
        from cu_perceive import windows

        err = CaptureError("capture=wgc refused by the onnxruntime guard: x", [{"backend": "wgc", "skipped": "unsafe"}],
                           reason="wgc_refused_onnxruntime_guard", hint="use capture=printwindow")
        origin = {"left": 0, "top": 0, "width": 40, "height": 30}
        with mock.patch.object(windows, "is_hwnd_minimized", return_value=False), \
                mock.patch.object(windows, "window_identity", return_value={"hwnd": 5}), \
                mock.patch.object(windows, "live_geom", return_value=(origin, {"hwnd": 5})), \
                mock.patch.object(windows, "classify_client_geometry", return_value=(None, None, None, None)), \
                mock.patch.object(windows, "capture_window_image", side_effect=err):
            out = core._capture_pinned(5, focus=False, capture="wgc")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "wgc_refused_onnxruntime_guard")
        self.assertIn("onnxruntime guard", out["error"])
        self.assertEqual(out["hint"], "use capture=printwindow")


class TestNewestStampPointer(_OutDir):
    def _perceive_like(self, frame) -> Path:
        p = self.put(frame)
        stamps.note_latest(self.dir, frame["hwnd"], p)
        return p

    def test_reads_only_the_pointed_stamp_and_newer_files(self):
        target = self._perceive_like(_win("20261019-120000-000", 111))
        for i in range(1, 60):
            self._perceive_like(_win(f"20261019-120000-{i:03d}", 222))
        with mock.patch.object(act, "read_frame", wraps=act.read_frame) as rf:
            frame, path = latest_frame(self.dir, hwnd=111)
        self.assertEqual(path, target)
        self.assertLessEqual(rf.call_count, 60)
        with mock.patch.object(act, "read_frame", wraps=act.read_frame) as rf:
            latest_frame(self.dir, hwnd=222)
        self.assertEqual(rf.call_count, 1)

    def test_old_window_reads_one_file_not_all(self):
        for i in range(60):
            self._perceive_like(_win(f"20261019-120001-{i:03d}", 222))
        target = self._perceive_like(_win("20261019-120002-000", 111))
        for i in range(5):
            self._perceive_like(_win(f"20261019-120003-{i:03d}", 222))
        with mock.patch.object(act, "read_frame", wraps=act.read_frame) as rf:
            _, path = latest_frame(self.dir, hwnd=111)
        self.assertEqual(path, target)
        self.assertEqual(rf.call_count, 6)

    def test_a_newer_unrecorded_stamp_still_wins(self):
        self._perceive_like(_win("20261019-120004-000", 111))
        newer = self.put(_win("20261019-120004-001", 111))
        self.assertEqual(latest_frame(self.dir, hwnd=111)[1], newer)

    def test_stale_or_missing_pointer_falls_back_to_the_scan(self):
        p = self._perceive_like(_win("20261019-120005-000", 111))
        older = self.put(_win("20261019-120005-001", 111))
        p.unlink()
        self.assertEqual(latest_frame(self.dir, hwnd=111)[1], older)
        (self.dir / stamps.LATEST_DIR / "hwnd-111").write_text("../../etc/passwd", encoding="utf-8")
        self.assertEqual(latest_frame(self.dir, hwnd=111)[1], older)

    def test_pointer_to_another_windows_stamp_is_ignored(self):
        a = self.put(_win("20261019-120006-000", 111))
        b = self.put(_win("20261019-120006-001", 222))
        stamps.note_latest(self.dir, 111, b)
        self.assertEqual(latest_frame(self.dir, hwnd=111)[1], a)

    def test_image_perceive_writes_no_pointer(self):
        img = self.dir / "in.png"
        cv2.imwrite(str(img), np.full((20, 30, 3), 90, np.uint8))
        out = core.perceive(image=str(img), out_dir=str(self.dir))
        self.assertFalse(out.get("hwnd"))
        self.assertFalse((self.dir / stamps.LATEST_DIR).exists())
        stamps.note_latest(self.dir, 77, Path(out["json"]))
        self.assertEqual(stamps.read_latest(self.dir, 77), Path(out["json"]))


class TestToolsRunOffTheEventLoop(unittest.TestCase):
    def test_slow_tool_does_not_block_another(self):
        try:
            import anyio  # noqa: F401
        except ImportError:
            self.skipTest("anyio not installed")

        def slow():
            time.sleep(0.5)
            return "slow"

        def fast():
            return "fast"

        wslow, wfast = mcp_server.offloaded(slow), mcp_server.offloaded(fast)
        self.assertTrue(asyncio.iscoroutinefunction(wslow))
        self.assertIs(wslow.__wrapped__, slow)

        async def main():
            t0 = time.monotonic()
            task = asyncio.ensure_future(wslow())
            await asyncio.sleep(0.05)
            r = await wfast()
            fast_at = time.monotonic() - t0
            return r, fast_at, await task

        r, fast_at, s = asyncio.run(main())
        self.assertEqual((r, s), ("fast", "slow"))
        self.assertLess(fast_at, 0.4)

    def test_every_tool_is_registered_async(self):
        from test_agent_friction import _ToolApp

        app = _ToolApp()
        import types

        fake = {
            "mcp": types.ModuleType("mcp"),
            "mcp.server": types.ModuleType("mcp.server"),
            "mcp.server.transport_security": types.SimpleNamespace(TransportSecuritySettings=lambda **k: k),
            "uvicorn": types.SimpleNamespace(Config=lambda *a, **k: None,
                                             Server=lambda c: types.SimpleNamespace(run=lambda: None)),
        }
        with mock.patch.dict(sys.modules, fake), \
                mock.patch.object(mcp_server, "mcp_api", return_value=(2, None, None)), \
                mock.patch.object(mcp_server, "build_server", return_value=(app, lambda: None)):
            mcp_server.serve()
        self.assertEqual(set(app.registered), {
            "windows", "perceive_window", "act", "launch_app", "apps",
            "browser_status", "browser_session", "browser_observe", "browser_tabs", "browser_act",
        })
        for name, fn in app.registered.items():
            self.assertTrue(asyncio.iscoroutinefunction(fn), name)


if __name__ == "__main__":
    unittest.main()
