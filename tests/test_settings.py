"""Phase-1 configuration: defaults match the old constants, and overrides follow
the documented precedence without changing unset behavior."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cu_perceive import settings
from cu_perceive.settings import ConfigError


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._patch = mock.patch.dict(os.environ, {}, clear=False)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        for key in list(os.environ):
            if key.startswith("CU_PERCEIVE_"):
                del os.environ[key]
        os.environ["CU_PERCEIVE_HOME"] = str(self.root)
        settings.reset_for_tests()
        self.addCleanup(settings.reset_for_tests)

    def _use(self, text: str, name: str = "config.toml") -> Path:
        path = self.root / name
        _write(path, text)
        os.environ["CU_PERCEIVE_CONFIG"] = str(path)
        settings.reset_for_tests()
        return path

    def test_defaults_match_previous_constants(self):
        from cu_perceive import _win32, act, capture, chain, change, detect_worker, ime, input as inp
        from cu_perceive import uia, windows
        from cu_perceive.browser import backend

        pairs = {
            "limits.max_scroll_notches": act.MAX_SCROLL_NOTCHES,
            "stamp.stale_sec": act.STALE_STAMP_SEC,
            "focus.settle_s": act.FOCUS_SETTLE_S,
            "timing.keyboard_settle_tries": act.KEYBOARD_SETTLE_TRIES,
            "timing.keyboard_settle_s": act.KEYBOARD_SETTLE_S,
            "popup.new_window_wait_s": act.NEW_WINDOW_WAIT_S,
            "popup.recheck_s": act.NEW_WINDOW_RECHECK_S,
            "popup.chain_step_s": act.NEW_WINDOW_CHAIN_STEP_S,
            "popup.poll_s": act.NEW_WINDOW_POLL_S,
            "change.pixel_threshold": change.CHANGE_PIXEL_THRESHOLD,
            "change.min_pixels": change.CHANGE_MIN_PIXELS,
            "change.reflow_ratio": change.REFLOW_RATIO,
            "change.reflow_bbox_share": change.REFLOW_BBOX_SHARE,
            "change.reflow_min_ratio": change.REFLOW_MIN_RATIO,
            "change.reflow_bbox_share_mid": change.REFLOW_BBOX_SHARE_MID,
            "change.reflow_min_ratio_mid": change.REFLOW_MIN_RATIO_MID,
            "change.min_ratio": change.CHANGE_MIN_RATIO,
            "change.row_band_min": change.ROW_BAND_MIN,
            "change.row_band_min_ratio": change.ROW_BAND_MIN_RATIO,
            "change.row_band_gap_px": change.ROW_BAND_GAP_PX,
            "change.row_band_min_width": change.ROW_BAND_MIN_WIDTH,
            "change.minor_ratio": change.CHANGE_MINOR_RATIO,
            "change.step": change.CHANGE_STEP,
            "change.wait_s": 0.35,
            "clipboard.open_tries": inp.CLIPBOARD_OPEN_TRIES,
            "clipboard.open_base_s": inp.CLIPBOARD_OPEN_BASE_S,
            "clipboard.open_max_s": inp.CLIPBOARD_OPEN_MAX_S,
            "ime.verify_budget_s": ime.IME_VERIFY_BUDGET_S,
            "focus.restore_settle_s": windows.RESTORE_SETTLE_S,
            "focus.verify_settle_s": windows.FOCUS_VERIFY_SETTLE_S,
            "safety.min_client_px": windows.MIN_CLIENT_PX,
            "safety.small_client_px": windows.SMALL_CLIENT_PX,
            "capture.content_threshold": capture.CONTENT_THRESHOLD,
            "capture.content_step": capture.CONTENT_STEP,
            "ocr.timeout_s": detect_worker.DEFAULT_TIMEOUT_S,
            "ocr.first_call_factor": detect_worker.FIRST_CALL_FACTOR,
            "uia.max_nodes": uia.UIA_MAX_NODES,
            "uia.max_depth": uia.UIA_MAX_DEPTH,
            "uia.walk_timeout_s": uia.UIA_WALK_TIMEOUT_S,
            "uia.value_max": uia.UIA_VALUE_MAX,
            "uia.content_max": uia.UIA_CONTENT_MAX,
            "uia.occluded_names_max": uia.UIA_OCCLUDED_NAMES_MAX,
            "limits.max_steps": chain.MAX_STEPS,
            "bsk.timeout_s": backend.DEFAULT_TIMEOUT_SEC,
            "capture.dwm_settle_s": _win32.DWM_SETTLE_S,
            "timing.act_gap_s": 0.12,
            "drag.step_px": 4,
            "drag.path_step_px": 2,
            "drag.gap_s": 0.004,
            "drag.hold_s": 0.06,
            "drag.hold_floor_s": 0.08,
            "server.host": "127.0.0.1",
            "server.port": 8771,
            "perception.roi_half": 150,
            "paths.wsl_distro": "Ubuntu",
            "safety.act_dry_run_default": True,
            "focus.default": False,
        }
        self.assertEqual(settings.PARAMS["change.row_band_similar_lo"].default, change.ROW_BAND_SIMILAR[0])
        self.assertEqual(settings.PARAMS["change.row_band_similar_hi"].default, change.ROW_BAND_SIMILAR[1])
        for key, old in pairs.items():
            self.assertEqual(settings.PARAMS[key].default, old, key)
            self.assertEqual(settings.get(key), old, key)
        self.assertEqual(act.new_window_wait_s(), act.NEW_WINDOW_WAIT_S)
        self.assertEqual(change.change_min_ratio(), change.CHANGE_MIN_RATIO)
        self.assertEqual(change.change_wait_s(), 0.35)
        self.assertEqual(detect_worker.detect_timeout(), detect_worker.DEFAULT_TIMEOUT_S)
        self.assertNotIn("capture.choices", settings.PARAMS)
        self.assertEqual(capture.CAPTURE_CHOICES, ("auto", "wgc", "dwm", "printwindow", "screen"))
        self.assertEqual(settings.PARAMS["capture.content_threshold"].policy, "tighten")
        self.assertEqual(settings.PARAMS["capture.content_threshold"].tighten, "ge")

    def test_env_keeps_old_semantics(self):
        from cu_perceive import act, change, detect_worker

        cases = {
            "CU_PERCEIVE_NEW_WINDOW_WAIT_MS": {
                "2000": 2.0,
                "0": 0.0,
                "off": 0.0,
                "none": 0.0,
                "junk": act.NEW_WINDOW_WAIT_S,
            },
        }
        for raw, expected in cases["CU_PERCEIVE_NEW_WINDOW_WAIT_MS"].items():
            os.environ["CU_PERCEIVE_NEW_WINDOW_WAIT_MS"] = raw
            self.assertEqual(act.new_window_wait_s(), expected, raw)
        os.environ.pop("CU_PERCEIVE_NEW_WINDOW_WAIT_MS")

        os.environ["CU_PERCEIVE_CHANGE_MIN_RATIO"] = "0.01"
        self.assertEqual(change.change_min_ratio(), 0.01)
        os.environ["CU_PERCEIVE_CHANGE_MIN_RATIO"] = "nope"
        self.assertEqual(change.change_min_ratio(), change.CHANGE_MIN_RATIO)
        os.environ["CU_PERCEIVE_CHANGE_MIN_RATIO"] = "-1"
        self.assertEqual(change.change_min_ratio(), 0.0)
        os.environ.pop("CU_PERCEIVE_CHANGE_MIN_RATIO")

        os.environ["CU_PERCEIVE_CHANGE_WAIT_MS"] = "off"
        self.assertEqual(change.change_wait_s(), 0.35)
        os.environ["CU_PERCEIVE_CHANGE_WAIT_MS"] = "100"
        self.assertEqual(change.change_wait_s(), 0.1)
        os.environ.pop("CU_PERCEIVE_CHANGE_WAIT_MS")

        for raw, expected in {"": 30.0, "5": 5.0, "2.5": 2.5, "0": None, "off": None, "junk": 30.0, "-1": None}.items():
            os.environ["CU_PERCEIVE_DETECT_TIMEOUT"] = raw
            self.assertEqual(detect_worker.detect_timeout(), expected, raw)
        os.environ.pop("CU_PERCEIVE_DETECT_TIMEOUT")

        os.environ["CU_PERCEIVE_BSK_TIMEOUT"] = "0"
        self.assertEqual(settings.get("bsk.timeout_s"), 120.0)
        os.environ["CU_PERCEIVE_BSK_TIMEOUT"] = "200"
        self.assertEqual(settings.get("bsk.timeout_s"), 200.0)
        os.environ.pop("CU_PERCEIVE_BSK_TIMEOUT")

        self.assertIs(settings.get("safety.detect_inproc"), False)
        os.environ["CU_PERCEIVE_DETECT_INPROC"] = "false"
        self.assertIs(settings.get("safety.detect_inproc"), True)
        os.environ["CU_PERCEIVE_DETECT_INPROC"] = "0"
        self.assertIs(settings.get("safety.detect_inproc"), False)
        os.environ.pop("CU_PERCEIVE_DETECT_INPROC")

        self.assertIs(settings.get("safety.clipboard_inprocess"), False)
        os.environ["CU_PERCEIVE_CLIPBOARD_INPROCESS"] = "true"
        self.assertIs(settings.get("safety.clipboard_inprocess"), False)
        os.environ["CU_PERCEIVE_CLIPBOARD_INPROCESS"] = "1"
        self.assertIs(settings.get("safety.clipboard_inprocess"), True)
        os.environ.pop("CU_PERCEIVE_CLIPBOARD_INPROCESS")
        os.environ["CU_PERCEIVE_CLIPBOARD_CHILD"] = "1"
        self.assertIs(settings.get("safety.clipboard_inprocess"), True)

    def test_file_then_env_then_call(self):
        self._use("[timing]\nact_gap_s = 0.2\n[popup]\nnew_window_wait_s = 2.0\n[capture]\nbackend = \"printwindow\"\n")
        self.assertEqual(settings.get("timing.act_gap_s"), 0.2)
        self.assertEqual(settings.get("popup.new_window_wait_s"), 2.0)
        self.assertEqual(settings.get("capture.backend"), "printwindow")
        self.assertEqual(settings.public_view()["sources"]["timing.act_gap_s"], "file")
        os.environ["CU_PERCEIVE_NEW_WINDOW_WAIT_MS"] = "500"
        os.environ["CU_PERCEIVE_CAPTURE"] = "WGC"
        self.assertEqual(settings.get("popup.new_window_wait_s"), 0.5)
        self.assertEqual(settings.get("capture.backend"), "wgc")
        os.environ["CU_PERCEIVE_NEW_WINDOW_WAIT_MS"] = "junk"
        self.assertEqual(settings.get("popup.new_window_wait_s"), 1.2)
        self.assertEqual(settings.pick(None, "timing.act_gap_s"), 0.2)
        self.assertEqual(settings.pick(0, "timing.click_hold_s"), 0)
        self.assertEqual(settings.pick(0.4, "timing.act_gap_s"), 0.4)

    def test_bad_file_is_not_applied_and_reload_keeps_the_previous_one(self):
        path = self._use("[timing]\nact_gap_s = 0.2\n")
        self.assertEqual(settings.get("timing.act_gap_s"), 0.2)
        _write(path, "[timing]\nact_gap_s = 9\n")
        failed = settings.reload()
        self.assertFalse(failed["ok"])
        self.assertEqual(settings.get("timing.act_gap_s"), 0.2)
        self.assertIn("above", settings.public_view()["load_error"])

        os.environ["CU_PERCEIVE_CONFIG"] = str(self.root / "missing.toml")
        settings.reset_for_tests()
        self.assertEqual(settings.get("timing.act_gap_s"), 0.12)
        self.assertIn("not found", settings.public_view()["load_error"] or "")

        self._use("[focus]\ndefault = true\n")
        self.assertIs(settings.get("focus.default"), False)
        self.assertIn("locked", settings.public_view()["load_error"])

    def test_tighten_unknown_and_secrets(self):
        self._use("[capture]\ncontent_threshold = 7\n")
        self.assertEqual(settings.get("capture.content_threshold"), 8)
        self.assertIn("tighten-only", settings.public_view()["load_error"])

        self._use("[capture]\ncontent_threshold = 9\n")
        self.assertEqual(settings.get("capture.content_threshold"), 9)

        self._use("[limits]\nmax_steps = 21\n")
        self.assertEqual(settings.get("limits.max_steps"), 20)
        self._use("[limits]\nmax_steps = 10\n")
        self.assertEqual(settings.get("limits.max_steps"), 10)

        self._use("[nope]\nx = 1\n")
        self.assertIn("unknown section", settings.public_view()["load_error"])
        self._use("[timing]\nnope = 1\n")
        self.assertIn("unknown key", settings.public_view()["load_error"])
        self._use("[timing]\npassword = \"hunter2\"\n")
        self.assertIn("secrets", settings.public_view()["load_error"])
        self._use("[capture]\nbackend = \"nope\"\n")
        self.assertIn("must be one of", settings.public_view()["load_error"])
        self._use("[server]\nhost = \"0.0.0.0\"\n")
        self.assertIsNotNone(settings.public_view()["load_error"])
        self._use("[server]\nhost = \"localhost\"\n")
        self.assertEqual(settings.get("server.host"), "localhost")

        self._use("[bsk]\ntimeout_s = 200\n")
        self.assertEqual(settings.get("bsk.timeout_s"), 120.0)
        self._use("[bsk]\ntimeout_s = 60\n")
        self.assertEqual(settings.get("bsk.timeout_s"), 60.0)

    def test_profiles_and_hash(self):
        self.assertEqual(settings.get("drag.gap_s"), 0.004)
        self.assertEqual(settings.get("timing.act_gap_s"), 0.12)
        with settings.call_context(profile="demo-slow-human"):
            self.assertEqual(settings.get("drag.gap_s"), 0.012)
            self.assertEqual(settings.get("timing.act_gap_s"), 0.12)
        with settings.call_context(profile="fast"):
            self.assertEqual(settings.get("drag.gap_s"), 0.002)
            self.assertEqual(settings.get("timing.act_gap_s"), 0.06)
        with self.assertRaises(ConfigError):
            with settings.call_context(profile="nope"):
                pass
        os.environ["CU_PERCEIVE_PROFILE"] = "nope"
        self.assertEqual(settings.active_profile(), "default")
        self.assertIn("unknown profile", settings.profile_error() or "")
        self.assertEqual(settings.get("drag.gap_s"), 0.004)
        os.environ.pop("CU_PERCEIVE_PROFILE")

        first = settings.revision()
        self.assertEqual(settings.revision(), first)
        self.assertEqual(settings.config_rev(), f"{first}@default")
        self._use("[profile.empty.timing]\n")
        self.assertEqual(settings.revision("empty"), settings.revision("default"))
        self.assertNotEqual(settings.config_rev("empty"), settings.config_rev("default"))
        self.assertNotEqual(settings.revision("fast"), settings.revision("default"))

        before = settings.revision()
        self._use("[timing]\nact_gap_s = 0.2\n")
        self.assertNotEqual(settings.revision(), before)

        spec = settings.PARAMS["timing.act_gap_s"]
        object.__setattr__(spec, "secret", True)
        try:
            shown = settings.public_view()
            self.assertEqual(shown["values"]["timing"]["act_gap_s"], "<redacted>")
            opened = settings.public_view(show_secrets=True)
            self.assertEqual(opened["values"]["timing"]["act_gap_s"], 0.2)
            redacted = settings.revision()
        finally:
            object.__setattr__(spec, "secret", False)
        self.assertNotEqual(redacted, settings.revision())

    def test_finish_response_and_cli(self):
        from cu_perceive import cli

        payload = {"ok": True, "stamp": "s"}
        settings.finish_response(payload)
        self.assertEqual(payload["stamp"], "s")
        self.assertTrue(payload["config_rev"].endswith("@default"))
        self.assertNotIn("config", payload)
        settings.finish_response(payload, show_config=True)
        self.assertEqual(payload["config"]["values"]["timing"]["act_gap_s"], 0.12)
        self.assertEqual(settings.finish_response(["keep"]), ["keep"])

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = cli.main(["config", "show"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["ok"])
        self.assertEqual(data["values"]["drag"]["gap_s"], 0.004)
        self.assertIn("@default", data["config_rev"])

        err = io.StringIO()
        with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", err):
            rc = cli.main(["config", "--profile", "nope"])
        self.assertEqual(rc, 2)

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = cli.main(["config", "validate"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(buf.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
