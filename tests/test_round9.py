"""PR8: stamp registry for custom out_dir, honest multiline replace, readback_unavailable,
MCP schema vs implementation cross-check."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from cu_perceive import act as act_mod
from cu_perceive import mcp_server, stamps
from cu_perceive.input import clear_field_for_replace


class _Pattern:
    def __init__(self, value):
        self.value = value

    @property
    def Value(self):
        return self.value

    def SetValue(self, v):
        self.value = v


class _Blind(_Pattern):
    """Monaco-like: ValuePattern exists but always reads empty."""

    @property
    def Value(self):
        return ""


def _frame(stamp: str, hwnd: int) -> dict:
    return {"stamp": stamp, "hwnd": hwnd, "width": 100, "height": 100, "origin": {"left": 0, "top": 0}}


class TestStampRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.default = base / "perceive"
        self.custom = base / "custom" / "workbuddy"
        self.default.mkdir(parents=True)
        self.custom.mkdir(parents=True)
        self.patches = [
            mock.patch.object(act_mod, "DEFAULT_OUT", self.default),
            mock.patch.object(stamps, "_default_root", lambda: self.default),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _write(self, dest: Path, hwnd: int) -> tuple[str, Path]:
        stamp, final = stamps.allocate(dest)
        stamps.commit(final, json.dumps(_frame(stamp, hwnd)))
        stamps.note_latest(dest, hwnd, final)
        stamps.register(final, hwnd)
        return stamp, final

    def test_bare_name_found_in_custom_out_dir(self):
        stamp, final = self._write(self.custom, 4242)
        self.assertEqual(act_mod.stamp_json_path(stamp).resolve(), final.resolve())
        frame, path = act_mod.load_frame(stamp=stamp)
        self.assertEqual(frame["stamp"], stamp)
        self.assertEqual(stamps.lookup(stamp + ".json").resolve(), final.resolve())

    def test_hwnd_lookup_finds_custom_out_dir_stamp(self):
        _, final = self._write(self.custom, 4242)
        frame, path = act_mod.load_frame(hwnd=4242)
        self.assertEqual(path.resolve(), final.resolve())

    def test_latest_without_stamp_or_hwnd_sees_custom_dir(self):
        self._write(self.default, 1)
        import os
        import time

        time.sleep(0.01)
        _, final = self._write(self.custom, 2)
        frame, path = act_mod.load_frame()
        self.assertEqual(frame["hwnd"], 2)
        self.assertEqual(path.resolve(), final.resolve())
        # a newer default-dir stamp still wins
        time.sleep(0.01)
        _, newer = self._write(self.default, 3)
        os.utime(newer, None)
        frame, _ = act_mod.load_frame()
        self.assertEqual(frame["hwnd"], 3)

    def test_newer_default_stamp_for_same_hwnd_wins(self):
        self._write(self.custom, 7)
        import time

        time.sleep(0.01)
        _, d = self._write(self.default, 7)
        _, path = act_mod.load_frame(hwnd=7)
        self.assertEqual(path.resolve(), d.resolve())

    def test_deleted_target_is_not_returned(self):
        stamp, final = self._write(self.custom, 9)
        final.unlink()
        self.assertIsNone(stamps.lookup(stamp))
        with self.assertRaises(FileNotFoundError):
            act_mod.load_frame(stamp=stamp)

    def test_default_dir_stamp_unchanged(self):
        stamp, final = self._write(self.default, 5)
        self.assertEqual(act_mod.stamp_json_path(stamp), self.default / f"{stamp}.json")
        # bare-name pointer for default-dir stamps stays a plain file name
        self.assertEqual(stamps._latest_pointer(self.default, 5).read_text(encoding="utf-8"), final.name)

    def test_garbage_names_are_ignored(self):
        self.assertIsNone(stamps.lookup("../../etc/passwd"))
        self.assertIsNone(stamps.lookup("not-a-stamp"))
        stamps.register(self.custom / "random.json", 1)  # not a stamp name: no-op, no raise
        self.assertFalse((self.default / stamps.INDEX_DIR / "random").exists())


class TestMultilineReplace(unittest.TestCase):
    def _keys(self):
        taps = []
        return taps, mock.patch("cu_perceive.input.tap_key", side_effect=lambda k: taps.append(k)), \
            mock.patch("cu_perceive.input.tap_combo", side_effect=lambda k: taps.append(k))

    def test_monaco_like_never_reports_verified_empty(self):
        taps, a, b = self._keys()
        with a, b:
            out = clear_field_for_replace(
                pattern=_Blind("x"), hwnd=77, class_name="Chrome_RenderWidgetHostHWND",
                control_type="EditControl", discover=False, allow_keyboard=True,
            )
        self.assertTrue(out["ok"], out)
        self.assertIsNot(out["empty"], True)
        self.assertFalse(out["verified"])
        self.assertEqual(out["readback"], "unavailable")
        self.assertEqual(out["method"], "ctrl_a_delete")
        self.assertEqual(taps, ["ctrl+a", "delete"])
        self.assertNotIn("shift+home", taps)

    def test_readable_web_textarea_verifies_after_ctrl_a(self):
        pat = _Pattern("line1\nline2\nline3")
        taps = []

        def combo(k):
            taps.append(k)

        def key(k):
            taps.append(k)
            if k == "delete" and "ctrl+a" in taps:
                pat.value = ""

        with mock.patch("cu_perceive.input.tap_key", side_effect=key), \
                mock.patch("cu_perceive.input.tap_combo", side_effect=combo):
            out = clear_field_for_replace(pattern=pat, control_type="EditControl",
                                          discover=False, allow_keyboard=True)
        self.assertTrue(out["ok"])
        self.assertTrue(out["empty"])
        self.assertTrue(out["verified"])
        self.assertTrue(out["multiline"])

    def test_only_current_line_cleared_is_a_failure(self):
        # Simulates End+Shift+Home semantics: deletes the last line only.
        pat = _Pattern("{\n  \"a\": 1\n}")
        taps = []

        def key(k):
            taps.append(k)
            if k == "delete" and "ctrl+a" not in taps:
                pat.value = pat.value.rsplit("\n", 1)[0] + "\n"

        with mock.patch("cu_perceive.input.tap_key", side_effect=key), \
                mock.patch("cu_perceive.input.tap_combo", side_effect=lambda k: taps.append(k)):
            # ctrl+a ignored by this fake control: the value stays multi-line
            out = clear_field_for_replace(pattern=_Sticky(pat.value), control_type="EditControl",
                                          discover=False, allow_keyboard=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "replace_failed")
        self.assertFalse(out["empty"])

    def test_native_multiline_edit_uses_em_setsel_not_end_shift_home(self):
        pat = _Sticky("a\r\nb")
        sent, taps = [], []

        def length(_h):
            return 0 if any(row[1] == 0x0303 for row in sent) else 4

        with mock.patch("cu_perceive.input.tap_key", side_effect=lambda k: taps.append(k)), \
                mock.patch("cu_perceive.input.tap_combo", side_effect=lambda k: taps.append(k)):
            out = clear_field_for_replace(
                pattern=pat, hwnd=5, class_name="WindowsForms10.EDIT.app.0.1", control_type="EditControl",
                send_message=lambda *a: sent.append(a), text_length=length, style=0x0004,
                discover=False, allow_keyboard=True,
            )
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["method"], "em_setsel")
        self.assertTrue(out["verified"])
        self.assertEqual(taps, [])

    def test_single_line_native_edit_unchanged(self):
        taps, a, b = self._keys()
        pat = _Pattern("WEB-02")
        with a, b:
            out = clear_field_for_replace(
                pattern=pat, hwnd=5, class_name="Edit", send_message=lambda *x: None,
                text_length=lambda h: 0, style=0, discover=False, allow_keyboard=True,
            )
        self.assertTrue(out["verified"])
        self.assertEqual(taps, ["end", "shift+home", "delete"])

    def test_masked_multiline_flag_ignored(self):
        taps, a, b = self._keys()
        with a, b:
            clear_field_for_replace(pattern=_Pattern("2020-01-01"), class_name="MaskedTextBox",
                                    style=0x0004, discover=False, allow_keyboard=True)
        self.assertEqual(taps[:2], ["end", "shift+home"])


class _Sticky(_Pattern):
    def SetValue(self, v):
        return None


class TestReadbackUnavailable(unittest.TestCase):
    def _run(self, info):
        out, step = {"ok": True}, {}
        with mock.patch("cu_perceive.input.focused_readback", return_value=info):
            ok = act_mod._note_typed_readback(out, step, "hello\nworld", 0)
        return ok, out, step

    def test_empty_from_non_native_is_warning(self):
        ok, out, step = self._run({"value": "", "password": False, "in_target": None, "native": False})
        self.assertTrue(ok)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["typed_verified"])
        self.assertNotEqual(out.get("reason"), "typed_mismatch")
        self.assertTrue(any("readback_unavailable" in w for w in out["warnings"]))

    def test_different_non_empty_is_still_mismatch(self):
        ok, out, _ = self._run({"value": "something else", "password": False, "in_target": None, "native": False})
        self.assertFalse(ok)
        self.assertEqual(out["reason"], "typed_mismatch")

    def test_null_literal_from_grid_is_still_mismatch(self):
        ok, out, _ = self._run({"value": "(null)", "password": False, "in_target": None, "native": False})
        self.assertFalse(ok)
        self.assertEqual(out["reason"], "typed_mismatch")

    def test_empty_native_edit_is_still_mismatch(self):
        ok, out, _ = self._run({"value": "", "password": False, "in_target": None, "native": True})
        self.assertFalse(ok)
        self.assertEqual(out["reason"], "typed_mismatch")


# Tokens like "reason=..." in docstrings that are result fields / values, not arguments.
_DOC_NON_ARGS = {
    "reason", "ok", "stamp_stale", "scroll_clamped", "id_source", "chosen_method", "typed_verified",
    "minimized", "uia_timeout", "uia_nodes_omitted", "hold_key_needs_pointer_step", "readback",
    "pointer_refused_step", "keyboard_refused_step", "hold_key_refused_step", "dry_run_only",
    "CU_PERCEIVE_DETECT_TIMEOUT", "CU_PERCEIVE_BSK_REQUIRE_GO",
    # result fields named in docs
    "stopped", "expect_change",  # chain result field / step-level field (see chain.STEP_FIELDS)
    "window", "clipboard_preserved", "foreground_click_count", "empty", "verified",
    # arguments the docs name only to say they are refused (unknown_args hints)
    "steps", "x", "action", "click",
    # perceive_window's ocr= named inside act's docs (needs prior ocr=true perceive)
    "ocr",
    # browser_act action= values, written as action=back etc.
    "back", "forward", "scroll", "type",
}


def _collect_tools() -> dict:
    fns: dict = {}

    class FakeApp:
        def tool(self):
            def deco(fn):
                fns[fn.__name__] = getattr(fn, "__wrapped__", fn)
                return fn
            return deco

    fake_mods = {
        "mcp": types.ModuleType("mcp"),
        "mcp.server": types.ModuleType("mcp.server"),
        "mcp.server.transport_security": types.SimpleNamespace(TransportSecuritySettings=lambda **k: None),
        "uvicorn": types.SimpleNamespace(Config=lambda *a, **k: None,
                                         Server=lambda c: types.SimpleNamespace(run=lambda: None)),
    }
    with mock.patch.dict(sys.modules, fake_mods), \
            mock.patch.object(mcp_server, "mcp_api", return_value=(2, object, object)), \
            mock.patch.object(mcp_server, "build_server", return_value=(FakeApp(), lambda: None)):
        mcp_server.serve()
    return fns


class TestSchemaMatchesImplementation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = _collect_tools()

    def test_ten_tools(self):
        self.assertEqual(len(self.tools), 10, sorted(self.tools))

    def test_doc_mentioned_args_are_in_schema(self):
        problems = []
        for name, fn in self.tools.items():
            params = set(inspect.signature(fn).parameters)
            doc = inspect.getdoc(fn) or ""
            for tok in set(re.findall(r"(?<![\w.\"'])([A-Za-z_][A-Za-z0-9_]*)=", doc)):
                if tok not in params and tok not in _DOC_NON_ARGS:
                    problems.append(f"{name}: docstring mentions {tok}= but the schema has no {tok}")
        self.assertEqual(problems, [])

    def test_key_params_exposed(self):
        self.assertIn("ime", inspect.signature(self.tools["act"]).parameters)
        self.assertIn("via", inspect.signature(self.tools["act"]).parameters)
        self.assertIn("query", inspect.signature(self.tools["windows"]).parameters)

    def test_act_params_reach_run_chain_or_parse(self):
        """Every act schema param is either consumed by the MCP wrapper or forwarded."""
        src = inspect.getsource(self.tools["act"])
        body = src.split('"""', 2)[-1]
        missing = [p for p in inspect.signature(self.tools["act"]).parameters if not re.search(rf"\b{p}\b", body)]
        self.assertEqual(missing, [])

    def test_real_sdk_schema_matches_signature(self):
        try:
            mcp_server.mcp_api()
            import uvicorn  # noqa: F401
        except Exception:
            self.skipTest("mcp SDK / uvicorn not installed")
        captured = {}
        real_build = mcp_server.build_server

        def build(*a, **k):
            app, http = real_build(*a, **k)
            captured["app"] = app
            return app, http

        with mock.patch.object(mcp_server, "build_server", side_effect=build), \
                mock.patch("uvicorn.Server.run", lambda self: None):
            mcp_server.serve(port=0)
        listed = asyncio.run(captured["app"].list_tools())
        self.assertEqual(len(listed), 10)
        for t in listed:
            schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None) or {}
            props = set((schema.get("properties") or {}).keys())
            sig = set(inspect.signature(self.tools[t.name]).parameters)
            self.assertEqual(props, sig, t.name)


if __name__ == "__main__":
    unittest.main()
