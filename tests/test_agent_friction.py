"""Follow-ups from real agents on Windows: LF shell scripts, key names, hold_key without a
pointer step, MCP descriptions, and refusal reason / hint / blocking window."""
from __future__ import annotations

import ast
import contextlib
import io
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from test_round5 import COVER, SIBLING, _Input, _Tree, _window_frame

from cu_perceive import input as inp
from cu_perceive import mcp_server
from cu_perceive.act import parse_steps, run_chain
from cu_perceive.cli import main as cli_main
from cu_perceive.keys import VK, UnknownKeyError, check_key_spec, key_names, key_names_summary, normalize_key
from cu_perceive.windows import capture_geometry_problem, classify_client_geometry

ROOT = Path(__file__).resolve().parents[1]


def _tool_doc(name: str) -> str:
    tree = ast.parse((ROOT / "cu_perceive" / "mcp_server.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"no tool {name}")


class TestShellScriptsStayLF(unittest.TestCase):
    def test_gitattributes_forces_lf_for_sh(self):
        text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", text.splitlines())

    def test_checked_in_script_has_no_crlf(self):
        self.assertNotIn(b"\r", (ROOT / "bin" / "cu-perceive.sh").read_bytes())

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs a git checkout")
    def test_git_resolves_eol_lf(self):
        res = subprocess.run(["git", "check-attr", "eol", "bin/cu-perceive.sh"], cwd=ROOT,
                             capture_output=True, text=True)
        self.assertIn("eol: lf", res.stdout)


class TestKeyNames(unittest.TestCase):
    def test_new_keys_and_codes(self):
        self.assertEqual(VK["pageup"], 0x21)
        self.assertEqual(VK["pagedown"], 0x22)
        self.assertEqual(VK["home"], 0x24)
        self.assertEqual(VK["end"], 0x23)
        self.assertEqual(VK["insert"], 0x2D)
        for d in range(10):
            self.assertEqual(VK[str(d)], 0x30 + d)
        self.assertEqual(VK["f24"], 0x87)

    def test_spellings_models_tried(self):
        for name in ("pagedown", "page_down", "PageDown", "Page-Down", "PgDn", "pgdown"):
            self.assertEqual(normalize_key(name), "pagedown", name)
        self.assertEqual(normalize_key("Return"), "enter")
        self.assertEqual(normalize_key("Escape"), "esc")
        self.assertEqual(normalize_key("del"), "delete")
        self.assertEqual(check_key_spec("Control+Shift+Page_Up"), "ctrl+shift+pageup")

    def test_unknown_key_lists_valid_names(self):
        with self.assertRaises(UnknownKeyError) as cm:
            check_key_spec("ctrl+pgdwn")
        msg = str(cm.exception)
        self.assertIn("unknown key 'pgdwn' in 'ctrl+pgdwn'", msg)
        for k in ("pagedown", "pageup", "home", "end", "0-9", "f1-f24", "enter"):
            self.assertIn(k, msg)
        self.assertIsInstance(cm.exception, KeyError)
        self.assertIsInstance(cm.exception, ValueError)

    def test_tap_key_raises_the_same_error(self):
        with mock.patch.object(inp, "_send") as send:
            with self.assertRaises(UnknownKeyError):
                inp.tap_key("page down please")
            send.assert_not_called()
            inp.tap_key("Page_Down")
        self.assertEqual(send.call_count, 2)

    def test_combo_folds_control_alias_into_modifier(self):
        sent = []
        with mock.patch.object(inp, "_vk_down", side_effect=lambda vk: sent.append(("down", vk))), \
                mock.patch.object(inp, "_vk_up", side_effect=lambda vk: sent.append(("up", vk))), \
                mock.patch.object(inp, "_send", side_effect=lambda i: sent.append(("tap", i.union.ki.wVk))), \
                mock.patch("time.sleep"):
            inp.tap_combo("control+end")
        self.assertEqual(sent[0], ("down", 0x11))
        self.assertEqual(sent[-1], ("up", 0x11))
        self.assertIn(("tap", 0x23), sent)


class TestKeysInAct(_Input):
    def test_dry_run_normalizes_key(self):
        out = run_chain(_window_frame(), parse_steps(key="page_down"), dry_run=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["steps"][0]["key"], "pagedown")

    def test_unknown_key_fails_dry_run_and_go_before_sending(self):
        for dry in (True, False):
            out = run_chain(_window_frame(), parse_steps(norm="500,500", key="enter,pagedwn"), dry_run=dry)
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "unknown_key")
            self.assertIn("pagedown", out["valid_keys"])
            self.assertIn("unknown key 'pagedwn'", out["error"])
            self.assertIn("pagedown", out["error"])
        self.assertEqual(self.calls, [])


class TestHoldKeyNeedsPointer(_Input):
    def test_hold_key_alone(self):
        out = run_chain(_window_frame(), parse_steps(), dry_run=False, hold_key="ctrl")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "hold_key_needs_pointer_step")
        self.assertIn("norm/xy/ids click", out["error"])
        self.assertIn("drag", out["error"])
        self.assertIn("scroll", out["error"])
        self.assertNotIn("no actionable steps", out["error"])
        self.assertIn('key="ctrl+pagedown"', out["hint"])
        self.assertEqual(self.calls, [])

    def test_hold_key_with_only_keys_or_hover(self):
        for kw in ({"key": "pagedown"}, {"text": "x"}, {"norm": "500,500", "hover": True}):
            out = run_chain(_window_frame(), parse_steps(**kw), dry_run=True, hold_key="shift")
            self.assertFalse(out["ok"], kw)
            self.assertEqual(out["reason"], "hold_key_needs_pointer_step", kw)

    def test_hold_key_with_click_drag_or_scroll_plans(self):
        for kw in ({"norm": "500,500"}, {"scroll": "down:3"}, {"drag_norm": "100,100;200,200"},
                   {"norm": "500,500", "scroll": "-2"}, {"xy": "10,10", "key": "enter"}):
            out = run_chain(_window_frame(), parse_steps(**kw), dry_run=True, hold_key="ctrl")
            self.assertTrue(out["ok"], (kw, out))

    def test_no_steps_without_hold_key_keeps_old_error(self):
        out = run_chain(_window_frame(), parse_steps(), dry_run=True)
        self.assertFalse(out["ok"])
        self.assertTrue(out["error"].startswith("no actionable steps"))
        self.assertEqual(out["reason"], "no_steps")


class TestToolDescriptions(unittest.TestCase):
    def test_act_first_200_chars_survive_truncation(self):
        head = _tool_doc("act")[:200]
        for part in ("go=true", "dry run", "focus=true", 'scroll="down:5"', "bare 5 = UP", 'key="enter"',
                     "pagedown", 'norm="x,y"', "hwnd="):
            self.assertIn(part, head)

    def test_act_cheat_sheet_and_no_focus_line_follow(self):
        lines = [ln.strip() for ln in _tool_doc("act").splitlines()]
        for part in ('norm="x,y"', "xy=", "ids=", 'scroll="down:3"', "negative = down, positive = UP",
                     "drag_norm=", "replace=true", "key=", "hold_key=", "focus=true", "go=true"):
            self.assertIn(part, lines[1])
        self.assertEqual(lines[2], "perceive_window does not focus; act go needs the window in front or focus=true.")

    def test_perceive_window_says_no_focus_first(self):
        self.assertEqual(_tool_doc("perceive_window").splitlines()[0],
                         "perceive_window does not focus; act go needs the window in front or focus=true.")

    def test_act_lists_every_key_name(self):
        doc = " ".join(_tool_doc("act").split())
        for name in key_names_summary().split(", "):
            self.assertIn(name, doc)

    def test_cli_help_lists_keys_and_scroll_sign(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli_main(["act", "--help"])
        text = " ".join(buf.getvalue().split())
        self.assertIn("pagedown", text)
        self.assertIn("f1-f24", text)
        self.assertIn("negative or down:N = wheel down", text)
        self.assertIn("--hold-key needs a click, drag or scroll", text)

    def test_cli_hold_key_alone_gets_specific_error(self):
        with self.assertRaises(SystemExit) as cm:
            cli_main(["act", "--json", "unused.json", "--hold-key", "ctrl"])
        self.assertIn("hold_key=ctrl needs a norm/xy/ids click", str(cm.exception.code))

    def test_key_names_cover_the_table(self):
        self.assertEqual(sorted(key_names()), sorted(VK))


class TestRefusalDetails(_Tree):
    def test_covered_point_names_window_reason_and_hint(self):
        self.at = COVER
        out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "point_covered")
        self.assertEqual(out["blocking_window"], {"hwnd": COVER, "title": f"w{COVER}"})
        self.assertIn("retry with focus=true", out["hint"])

    def test_keyboard_refusal_has_title(self):
        self.fg = COVER
        out = run_chain(_window_frame(), parse_steps(key="enter"), dry_run=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "not_foreground")
        self.assertIn(f"'w{COVER}'", out["error"])
        self.assertEqual(out["blocking_window"]["title"], f"w{COVER}")
        self.assertIn("focus=true", out["hint"])

    def test_g1_hold_refusal_has_title(self):
        self.fg = COVER
        out = run_chain(_window_frame(), parse_steps(norm="500,500", scroll="down:3"), dry_run=False,
                        hold_key="ctrl")
        self.assertFalse(out["ok"])
        self.assertIn("hold_key_refused_step", out)
        self.assertIn(f"'w{COVER}'", out["error"])
        self.assertEqual(out["blocking_window"], {"hwnd": COVER, "title": f"w{COVER}"})
        self.assertEqual(self.calls, ["hover"])

    def test_still_behind_after_focus(self):
        self.fg = SIBLING
        with mock.patch("cu_perceive.windows.force_foreground"):
            out = run_chain(_window_frame(), parse_steps(key="enter"), dry_run=False, focus=True)
        self.assertEqual(out["reason"], "not_foreground_after_focus")
        self.assertIn(f"w{SIBLING}", out["hint"])

    def test_covered_after_focus_says_move_or_close(self):
        self.at = COVER
        with mock.patch("cu_perceive.windows.force_foreground"):
            out = run_chain(_window_frame(), parse_steps(norm="500,500"), dry_run=False, focus=True)
        self.assertEqual(out["reason"], "point_covered_after_focus")
        self.assertIn("move or close it", out["hint"])

    def test_identity_change_mid_chain(self):
        with mock.patch("cu_perceive.act.stamp_identity_error", side_effect=[None, "HWND reused"]):
            out = run_chain(_window_frame(), parse_steps(norm="500,500;600,600"), dry_run=False)
        self.assertEqual(out["identity_abort_step"], 1)
        self.assertEqual(out["reason"], "identity_changed")
        self.assertIn("re-perceive", out["hint"])

    def test_success_has_no_refusal_fields(self):
        out = run_chain(_window_frame(), parse_steps(norm="500,500", key="enter"), dry_run=False)
        self.assertTrue(out["ok"], out)
        for k in ("reason", "hint", "blocking_window"):
            self.assertNotIn(k, out)


class TestGhostHint(unittest.TestCase):
    def test_offscreen_hint_is_move_back_not_focus(self):
        reason, err, hint, warn = classify_client_geometry({"left": -32000, "top": -32000, "width": 144, "height": 1})
        self.assertEqual(reason, "offscreen")
        self.assertIn("move the window back on-screen", hint)
        self.assertIn("focus=true does not help", hint)
        self.assertNotIn("--focus restores", err)

    def test_tiny_client(self):
        reason, err, hint, _ = classify_client_geometry({"left": 10, "top": 10, "width": 144, "height": 1})
        self.assertEqual(reason, "tiny_client")
        self.assertIn("resize", hint)

    def test_old_two_tuple_api(self):
        err, warn = capture_geometry_problem({"left": 10, "top": 10, "width": 60, "height": 300})
        self.assertIsNone(err)
        self.assertIn("60x300", warn)


class _ToolApp:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _mcp_tools() -> dict:
    app = _ToolApp()
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
    return app.tools


class TestMcpPerceiveFailurePassthrough(unittest.TestCase):
    def test_ghost_reason_hint_and_geometry_reach_the_client(self):
        ghost = {
            "ok": False, "stamp": None, "hwnd": 77, "focused": True,
            "error": "hwnd=77: window is parked off-screen ...",
            "reason": "offscreen", "hint": "move the window back on-screen ...",
            "degenerate_client": {"left": -32000, "top": -32000, "width": 144, "height": 1, "image": [144, 1]},
        }
        with mock.patch("cu_perceive.core.perceive", return_value=ghost):
            out = _mcp_tools()["perceive_window"](hwnd=77, focus=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "offscreen")
        self.assertIn("move the window back", out["hint"])
        self.assertEqual(out["degenerate_client"]["image"], [144, 1])


if __name__ == "__main__":
    unittest.main()
