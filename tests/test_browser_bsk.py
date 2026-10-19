"""Browser tools against a fake bsk. No real browser, no Windows, no vendored source."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cu_perceive import mcp_server
from cu_perceive.browser.backend import BskRun, bsk_on_path, command_argv, set_backend
from cu_perceive.browser.catalog import (
    ACT_ALIASES,
    ACT_VERBS,
    OBSERVE_KINDS,
    SESSION_ACTIONS,
    TAB_ACTIONS,
    allowlist,
)
from cu_perceive.browser.tools import (
    browser_act,
    browser_observe,
    browser_session,
    browser_status,
    browser_tabs,
    bsk_require_go,
    reset_browser_state,
)

ROOT = Path(__file__).resolve().parents[1]

FAKE_BODY = r"""
import json, os, sys

mode = os.environ.get("BSK_FAKE_MODE", "ok")
log = os.environ.get("BSK_FAKE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]


def emit(obj, code):
    json.dump(obj, sys.stdout)
    sys.stdout.write("\n")
    raise SystemExit(code)


if mode == "outcome_unknown":
    emit({
        "code": "not_found",
        "message": "visual target changed after the first click",
        "hint": "do not repeat the input",
        "exit_code": 1,
        "data": {"reason": "input_outcome_unknown", "effect_state": "unknown"},
    }, 1)
elif mode == "ref_not_found":
    emit({
        "code": "not_found",
        "message": "ref @e99 unknown",
        "hint": "rerun observe",
        "exit_code": 1,
        "data": {"reason": "ref_not_found"},
    }, 1)
elif mode == "visual_target_changed":
    emit({
        "code": "not_found",
        "message": "visual target changed",
        "hint": "the element moved",
        "exit_code": 1,
        "data": {"reason": "visual_target_changed"},
    }, 1)
elif mode == "timeout_exit":
    emit({"code": "timeout", "message": "timed out", "hint": "wait", "exit_code": 4}, 4)
elif mode == "transport":
    emit({"code": None, "message": "daemon unreachable", "hint": "try bsk status", "exit_code": 2}, 2)
elif mode == "browser":
    emit({"code": "cdp_failed", "message": "cdp failed", "hint": "retry", "exit_code": 3, "data": {}}, 3)
elif mode == "version":
    emit({"code": "version_too_old", "message": "extension is old", "hint": "update", "exit_code": 5}, 5)
elif mode == "clap":
    sys.stderr.write(
        "error: unexpected argument '-t' found\n\n"
        "Usage: bsk fill --session <SESSION> --value <VALUE> [TARGET]\n\n"
        "For more information, try '--help'.\n"
    )
    raise SystemExit(1)
elif mode == "clap_invalid":
    sys.stderr.write(
        "error: invalid value 'nope' for '--timeout <TIMEOUT>'\n\n"
        "For more information, try '--help'.\n"
    )
    raise SystemExit(1)
elif mode == "plain":
    sys.stdout.write("usage: nope\n")
    raise SystemExit(2)
elif mode == "user_plain":
    sys.stdout.write("bad flag\n")
    raise SystemExit(1)
elif mode == "nav_timeout":
    emit({"tab_id": 1, "url": "https://example.com", "reached": "timeout"}, 4)
elif args[:2] == ["session", "start"]:
    emit({"session_id": "abcd", "browser_instance_id": "browser-1"}, 0)
elif args[:2] == ["session", "list"]:
    sys.stdout.write('[\n  {"session_id": "abcd"}\n]\n')
    raise SystemExit(0)
elif "status" in args:
    emit({"daemon": "0.3.3", "protocol": "1.3"}, 0)
elif "observe" in args or "snapshot" in args:
    text = os.environ.get("BSK_FAKE_OBSERVE", '[button] @e3 "Go"')
    body = {"text": text, "ref_count": text.count("@e"), "tab_id": 1}
    page = os.environ.get("BSK_FAKE_PAGE_URL")
    if page:
        body["url"] = page
    emit(body, 0)
elif "click" in args or "press" in args or "fill" in args:
    body = {"tab_id": 1, "x": 1, "y": 2}
    url = os.environ.get("BSK_FAKE_URL")
    if url:
        body["url"] = url
    previous = os.environ.get("BSK_FAKE_PREVIOUS_URL")
    if previous:
        body["previous_url"] = previous
    if os.environ.get("BSK_FAKE_NAVIGATED") == "1":
        body["navigated"] = True
    emit(body, 0)
else:
    emit({"echo": args}, 0)
"""


class BrowserCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.log = home / "bsk.log"
        # A .py file launched by sys.executable. A shebang script is WinError 193 on Windows.
        self.bsk = home / "bsk.py"
        self.bsk.write_text(FAKE_BODY, encoding="utf-8")
        self._env = mock.patch.dict(
            os.environ,
            {
                "CU_PERCEIVE_BSK": str(self.bsk),
                "BSK_FAKE_LOG": str(self.log),
                "BSK_FAKE_MODE": "ok",
                "BSK_FAKE_OBSERVE": '[button] @e3 "Go"',
            },
        )
        self._env.start()
        reset_browser_state()

    def tearDown(self):
        reset_browser_state()
        self._env.stop()
        self.tmp.cleanup()

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def observe(self, session: str = "abcd", **kwargs):
        out = browser_observe(session=session, **kwargs)
        self.assertTrue(out["ok"], out)
        self.assertFalse(out["dry_run"])
        return out


class TestAllowlist(unittest.TestCase):
    def test_read_and_act_split(self):
        self.assertEqual(
            ACT_VERBS,
            (
                "click",
                "hover",
                "wheel",
                "scroll_to",
                "focus",
                "blur",
                "fill",
                "select",
                "press",
                "navigate",
                "navigate_back",
                "navigate_forward",
                "reload",
            ),
        )
        self.assertEqual(
            ACT_ALIASES,
            {"type": "fill", "scroll": "wheel", "key": "press", "back": "navigate_back", "forward": "navigate_forward"},
        )
        self.assertEqual(OBSERVE_KINDS, ("observe", "snapshot"))
        self.assertEqual(SESSION_ACTIONS, {"list": "read", "start": "act", "stop": "act"})
        self.assertEqual(TAB_ACTIONS["list"], "read")
        for name in ("create", "close", "select", "borrow", "return"):
            self.assertEqual(TAB_ACTIONS[name], "act")
        listed = allowlist()
        self.assertIn("browser_status", listed["read"])
        self.assertIn("browser_act verb=click", listed["act"])
        self.assertNotIn("browser_act verb=evaluate", listed["act"])


class TestDryRunAndGo(BrowserCase):
    def test_status_and_session_list_run_without_go(self):
        status = browser_status()
        self.assertTrue(status["ok"], status)
        self.assertFalse(status["dry_run"])
        self.assertEqual(status["result"]["daemon"], "0.3.3")
        self.assertEqual(status["command"][1:], ["status", "--json"])
        self.assertEqual(status["backend"], "subprocess")

        listed = browser_session(action="list")
        self.assertTrue(listed["ok"], listed)
        self.assertFalse(listed["dry_run"])
        self.assertEqual(listed["result"], {"items": [{"session_id": "abcd"}]})
        self.assertEqual(self.calls()[-1], ["session", "list", "--json"])

        warned = browser_session(action="list", go=True)
        self.assertTrue(warned["ok"], warned)
        self.assertTrue(any("read-only" in note for note in warned["warnings"]))

    def test_click_runs_by_default_and_dry_run_previews(self):
        obs = self.observe()
        oid = obs["observation_id"]
        preview = browser_act(verb="click", session="abcd", ref="@e3", observation_id=oid, dry_run=True)
        self.assertTrue(preview["ok"], preview)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["ref"], "@e3")
        self.assertEqual(preview["command"][0], str(self.bsk))
        self.assertEqual(preview["command"][1:], ["click", "--session", "abcd", "--json", "--", "@e3"])
        self.assertIn("preview only", preview["warnings"][0])
        self.assertEqual(len(self.calls()), 1)

        # go is accepted and does not gate. dry_run=true still previews.
        forced = browser_act(
            verb="click", session="abcd", ref="@e3", observation_id=oid, go=True, dry_run=True
        )
        self.assertTrue(forced["dry_run"])
        self.assertIn("preview only", forced["warnings"][0])
        self.assertNotIn("overrides go=true", " ".join(forced.get("warnings") or []))
        self.assertEqual(len(self.calls()), 1)

        live = browser_act(verb="click", session="abcd", ref="@e3", observation_id=oid)
        self.assertTrue(live["ok"], live)
        self.assertFalse(live["dry_run"])
        self.assertEqual(live["result"], {"tab_id": 1, "x": 1, "y": 2})
        self.assertEqual(self.calls()[-1], preview["command"][1:])

        again = browser_act(
            verb="click", session="abcd", ref="e3", observation_id=oid, go=False, dry_run=False
        )
        self.assertTrue(again["ok"], again)
        self.assertFalse(again["dry_run"])
        self.assertEqual(again["ref"], "@e3")
        self.assertFalse(any("pass go=true" in note for note in again.get("warnings") or []))
        self.assertEqual(self.calls()[-1], preview["command"][1:])

    def test_require_go_restores_the_dry_run_gate(self):
        os.environ["CU_PERCEIVE_BSK_REQUIRE_GO"] = "1"
        obs = self.observe()
        oid = obs["observation_id"]
        dry = browser_act(verb="click", session="abcd", ref="@e3", observation_id=oid)
        self.assertTrue(dry["ok"], dry)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["command"][1:], ["click", "--session", "abcd", "--json", "--", "@e3"])
        self.assertTrue(any("pass go=true" in note for note in dry.get("warnings") or []))
        self.assertEqual(len(self.calls()), 1)

        idle = browser_act(verb="click", session="abcd", ref="@e3", observation_id=oid, dry_run=False)
        self.assertTrue(idle["dry_run"])
        self.assertEqual(idle["ref"], "@e3")
        self.assertIn("pass go=true", idle["warnings"][0])
        self.assertEqual(len(self.calls()), 1)

        forced = browser_act(
            verb="click", session="abcd", ref="@e3", observation_id=oid, go=True, dry_run=True
        )
        self.assertTrue(forced["dry_run"])
        self.assertIn("dry_run=true overrides go=true", forced["warnings"][0])
        self.assertEqual(len(self.calls()), 1)

        live = browser_act(verb="click", session="abcd", ref="@e3", observation_id=oid, go=True)
        self.assertTrue(live["ok"], live)
        self.assertFalse(live["dry_run"])
        self.assertEqual(self.calls()[-1], dry["command"][1:])

    def test_require_go_env_values(self):
        for value in ("", "0", "false", "no", "off", "  ", "2"):
            os.environ["CU_PERCEIVE_BSK_REQUIRE_GO"] = value
            self.assertFalse(bsk_require_go(), repr(value))
        os.environ.pop("CU_PERCEIVE_BSK_REQUIRE_GO", None)
        self.assertFalse(bsk_require_go())
        for value in ("1", "true", "YES", " on "):
            os.environ["CU_PERCEIVE_BSK_REQUIRE_GO"] = value
            self.assertTrue(bsk_require_go(), repr(value))

    def test_aliases_and_other_verbs_plan_the_bsk_argv(self):
        obs = self.observe()
        oid = obs["observation_id"]
        filled = browser_act(
            verb="type", session="abcd", ref="@e3", observation_id=oid, value="hello world", dry_run=True
        )
        self.assertEqual(filled["verb"], "fill")
        self.assertEqual(
            filled["command"][1:],
            ["fill", "--session", "abcd", "--value=hello world", "--json", "--", "@e3"],
        )
        self.assertTrue(any("runs as fill" in note for note in filled["warnings"]))

        pressed = browser_act(
            verb="key", session="abcd", ref="@e3", observation_id=oid, key="Enter", dry_run=True
        )
        self.assertEqual(
            pressed["command"][1:],
            ["press", "--session", "abcd", "--ref", "@e3", "--json", "--", "Enter"],
        )

        wheeled = browser_act(verb="scroll", session="abcd", delta_y=600, dry_run=True)
        self.assertEqual(wheeled["verb"], "wheel")
        self.assertEqual(wheeled["command"][1:], ["wheel", "--session", "abcd", "--delta-x", "0", "--delta-y", "600", "--json"])

        started = browser_session(action="start", browser="chrome", dry_run=True)
        self.assertTrue(started["dry_run"])
        self.assertEqual(started["command"][1:], ["session", "start", "--browser", "chrome", "--json"])
        self.assertTrue(any("Agent Window" in note for note in started["warnings"]))

        borrowed = browser_tabs(action="borrow", session="abcd", tab_id=7, dry_run=True)
        self.assertTrue(borrowed["dry_run"])
        self.assertEqual(borrowed["command"][1:], ["tab", "borrow", "7", "--session", "abcd", "--json"])
        self.assertTrue(any("confirm" in note for note in borrowed["warnings"]))
        # Planning the borrow did not drop the observe, and did not spawn.
        self.assertEqual(len(self.calls()), 1)
        still = browser_act(verb="click", session="abcd", ref="@e3", observation_id=oid)
        self.assertTrue(still["ok"], still)
        opened = browser_session(action="start", go=True)
        self.assertTrue(opened["ok"], opened)
        self.assertFalse(opened["dry_run"])
        self.assertEqual(opened["session"], "abcd")
        self.assertEqual(opened["result"]["session_id"], "abcd")

    def test_selector_click_does_not_need_an_observe(self):
        out = browser_act(
            verb="click", session="abcd", selector="#go", button="right", click_count=2, dry_run=True
        )
        self.assertTrue(out["dry_run"], out)
        self.assertEqual(
            out["command"][1:],
            ["click", "--session", "abcd", "--button", "right", "--click-count", "2", "--json", "--", "#go"],
        )
        self.assertTrue(any("selector" in note for note in out["warnings"]))
        self.assertEqual(self.calls(), [])

        live = browser_act(verb="click", session="abcd", selector="#go", button="right", click_count=2)
        self.assertTrue(live["ok"], live)
        self.assertFalse(live["dry_run"])
        self.assertEqual(self.calls(), [out["command"][1:]])

    def test_hyphen_values_are_not_flags(self):
        obs = self.observe()
        oid = obs["observation_id"]
        filled = browser_act(
            verb="fill", session="abcd", ref="@e3", observation_id=oid, value="-test", dry_run=True
        )
        self.assertEqual(
            filled["command"][1:],
            ["fill", "--session", "abcd", "--value=-test", "--json", "--", "@e3"],
        )
        selected = browser_act(
            verb="select", session="abcd", ref="@e3", observation_id=oid, value="-opt", dry_run=True
        )
        self.assertIn("--value=-opt", selected["command"])
        opened = browser_tabs(action="create", session="abcd", url="-https://example.com/a?q=1", dry_run=True)
        self.assertIn("--url=-https://example.com/a?q=1", opened["command"])
        navigated = browser_act(verb="navigate", session="abcd", url="-https://example.com", dry_run=True)
        self.assertEqual(
            navigated["command"][1:],
            ["navigate", "--session", "abcd", "--json", "--", "-https://example.com"],
        )
        pressed = browser_act(verb="key", session="abcd", key="-", dry_run=True)
        self.assertEqual(pressed["command"][1:], ["press", "--session", "abcd", "--json", "--", "-"])

    def test_huge_timeout_tab_id_and_max_tokens_are_bad_args(self):
        obs = self.observe()
        oid = obs["observation_id"]
        spawned = len(self.calls())
        for value in (10**12, 1e15, 600_001):
            out = browser_act(
                verb="click", session="abcd", ref="@e3", observation_id=oid, timeout_ms=value
            )
            self.assertEqual(out["reason"], "bad_args", out)
            self.assertIn("timeout_ms", out["error"])
            self.assertIn("600000", out["error"])
        allowed = browser_act(
            verb="click", session="abcd", ref="@e3", observation_id=oid, timeout_ms=600_000, dry_run=True
        )
        self.assertTrue(allowed["ok"], allowed)
        self.assertIn("600000ms", allowed["command"])

        huge_tab = browser_act(verb="click", session="abcd", ref="@e3", observation_id=oid, tab_id=2**31)
        self.assertEqual(huge_tab["reason"], "bad_args", huge_tab)
        self.assertIn("tab_id", huge_tab["error"])
        tab_ok = browser_act(
            verb="click", session="abcd", ref="@e3", observation_id=oid, tab_id=2**31 - 1, dry_run=True
        )
        self.assertTrue(tab_ok["ok"], tab_ok)
        self.assertIn("2147483647", tab_ok["command"])

        huge_tokens = browser_observe(session="abcd", max_tokens=10**15)
        self.assertEqual(huge_tokens["reason"], "bad_args", huge_tokens)
        self.assertIn("max_tokens", huge_tokens["error"])
        self.assertEqual(len(self.calls()), spawned)
        tokens_ok = browser_observe(session="abcd", max_tokens=1_000_000)
        self.assertTrue(tokens_ok["ok"], tokens_ok)
        self.assertIn("--max-tokens", tokens_ok["command"])
        self.assertEqual(len(self.calls()), spawned + 1)

    def test_path_lookup_when_env_is_unset(self):
        os.environ.pop("CU_PERCEIVE_BSK")
        home = self.bsk.parent
        if sys.platform == "win32":
            # PATH may only yield bsk.exe. A .cmd next to the fake must not be launched.
            (home / "bsk.cmd").write_text("@echo off\r\n", encoding="utf-8")
            os.environ["PATH"] = str(home)
            out = browser_status()
            self.assertEqual(out["reason"], "missing_dependency")
            self.assertEqual(self.calls(), [])
            return
        shim = home / "bsk"
        shim.write_text(f"#!{sys.executable}\n{FAKE_BODY}", encoding="utf-8")
        shim.chmod(0o755)
        os.environ["PATH"] = str(home)
        out = browser_status()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["command"][0], str(shim))

    def test_windows_path_accepts_only_bsk_exe(self):
        home = self.bsk.parent
        cmd = home / "bsk.cmd"
        bat = home / "bsk.bat"
        exe = home / "bsk.exe"
        cmd.write_text("@echo off\n", encoding="utf-8")
        bat.write_text("@echo off\n", encoding="utf-8")
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(
            os.environ, {"PATH": str(home)}, clear=False
        ):
            self.assertIsNone(bsk_on_path())
            exe.write_text("", encoding="utf-8")
            found = bsk_on_path()
            self.assertEqual(found, str(exe))
            self.assertTrue(found.lower().endswith(".exe"))


class TestUnknownArgs(BrowserCase):
    def test_schema_refuses_unknown_arguments(self):
        props = {name: {} for name in inspect.signature(browser_act).parameters}

        class Base:
            async def list_tools(self):
                return [SimpleNamespace(name="browser_act", inputSchema={"properties": props})]

            async def call_tool(self, name, arguments, *args, **kwargs):
                raise AssertionError(f"tool ran with {arguments}")

        server = mcp_server.strict_server_cls(Base)()
        with self.assertRaises(Exception) as caught:
            asyncio.run(server.call_tool("browser_act", {"verb": "click", "steps": [{"x": 1}], "go": True}))
        payload = json.loads(str(caught.exception))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "unknown_args")
        self.assertEqual(payload["unknown_args"], ["steps"])
        self.assertIn("verb", payload["valid_args"])
        self.assertIn("go", payload["valid_args"])
        self.assertIn("dry_run", payload["valid_args"])
        self.assertIn("hint", payload)
        self.assertEqual(self.calls(), [])

    def test_unknown_verb_and_unused_fields_do_not_spawn(self):
        unknown = browser_act(verb="evaluate", session="abcd", go=True)
        self.assertEqual(unknown["reason"], "unknown_verb")
        self.assertIn("click", unknown["valid_verbs"])
        self.assertNotIn("evaluate", unknown["valid_verbs"])
        self.assertTrue(unknown["dry_run"])

        obs = self.observe()
        extra = browser_act(
            verb="click", session="abcd", ref="@e3", observation_id=obs["observation_id"], url="https://example.com"
        )
        self.assertEqual(extra["reason"], "bad_args")
        self.assertIn("url", extra["unused_args"])
        self.assertEqual(len(self.calls()), 1)

        empty = browser_session(action="")
        self.assertEqual(empty["reason"], "bad_args")
        bad = browser_tabs(action="explode", session="abcd")
        self.assertEqual(bad["reason"], "unknown_action")
        self.assertIn("borrow", bad["valid_actions"])


class TestMissingBinary(BrowserCase):
    def test_env_override_does_not_fall_back_to_path(self):
        os.environ["CU_PERCEIVE_BSK"] = str(self.bsk.parent / "missing-bsk")
        os.environ["PATH"] = str(self.bsk.parent) + os.pathsep + os.environ.get("PATH", "")
        out = browser_status()
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "missing_dependency")
        self.assertEqual(out["missing_dependency"], "bsk")
        self.assertIn("install", out)
        self.assertIn("CU_PERCEIVE_BSK", out["hint"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(self.calls(), [])

        planned = browser_act(verb="navigate", session="abcd", url="https://example.com", go=True)
        self.assertEqual(planned["reason"], "missing_dependency")
        self.assertEqual(self.calls(), [])


class TestErrorMapping(BrowserCase):
    def test_exit_codes_and_reasons(self):
        cases = [
            ("timeout_exit", "timeout", 4),
            ("transport", "transport", 2),
            ("browser", "browser", 3),
            ("version", "version", 5),
            ("ref_not_found", "ref_not_found", 1),
            ("plain", "transport", 2),
            ("user_plain", "user_error", 1),
        ]
        for mode, reason, code in cases:
            os.environ["BSK_FAKE_MODE"] = mode
            out = browser_status()
            self.assertFalse(out["ok"], mode)
            self.assertEqual(out["reason"], reason, out)
            self.assertEqual(out["exit_code"], code, mode)
            self.assertIn("hint", out)

    def test_clap_usage_is_bad_args_without_the_dump(self):
        os.environ["BSK_FAKE_MODE"] = "clap"
        out = browser_status()
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["reason"], "bad_args")
        self.assertEqual(out["error"], "unexpected argument '-t' found")
        self.assertNotIn("Usage:", out["error"])
        self.assertNotIn("stderr", out)
        self.assertNotIn("Usage:", json.dumps(out))

        os.environ["BSK_FAKE_MODE"] = "clap_invalid"
        invalid = browser_status()
        self.assertEqual(invalid["reason"], "bad_args")
        self.assertEqual(invalid["error"], "invalid value 'nope' for '--timeout <TIMEOUT>'")
        self.assertNotIn("For more information", json.dumps(invalid))

    def test_input_outcome_unknown_says_not_to_retry_blindly(self):
        obs = self.observe()
        os.environ["BSK_FAKE_MODE"] = "outcome_unknown"
        out = browser_act(verb="click", session="abcd", ref="@e3", observation_id=obs["observation_id"], go=True)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["reason"], "input_outcome_unknown")
        self.assertIn("do not retry blindly", out["hint"])
        self.assertEqual(out["effect_state"], "unknown")
        self.assertEqual(out["bsk_code"], "not_found")
        self.assertEqual(out["exit_code"], 1)
        self.assertIn("command", out)

    def test_navigation_timeout_stays_a_result(self):
        obs = self.observe()
        os.environ["BSK_FAKE_MODE"] = "nav_timeout"
        out = browser_act(verb="navigate", session="abcd", url="https://example.com", go=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["exit_code"], 4)
        self.assertEqual(out["result"]["reached"], "timeout")
        self.assertTrue(any("reached=timeout" in note for note in out["warnings"]))
        stale = browser_act(verb="click", session="abcd", ref="@e3", observation_id=obs["observation_id"], go=True)
        self.assertEqual(stale["reason"], "stale_observation")


class TestStaleRef(BrowserCase):
    def test_ref_from_an_older_observe_is_refused(self):
        first = self.observe()
        os.environ["BSK_FAKE_OBSERVE"] = '[button] @e9 "Next"'
        second = self.observe()
        self.assertEqual(second["refs"], ["@e9"])
        self.assertNotEqual(first["observation_id"], second["observation_id"])
        spawned = len(self.calls())

        stale = browser_act(
            verb="click", session="abcd", ref="@e3", observation_id=first["observation_id"], go=True
        )
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["reason"], "stale_observation")
        self.assertEqual(stale["latest_observation_id"], second["observation_id"])
        self.assertIn("browser_observe", stale["hint"])

        missing = browser_act(verb="click", session="abcd", ref="@e9", go=True)
        self.assertEqual(missing["reason"], "stale_observation")

        reused = browser_act(
            verb="click", session="abcd", ref="@e3", observation_id=second["observation_id"], go=True
        )
        self.assertEqual(reused["reason"], "stale_observation")
        self.assertEqual(len(self.calls()), spawned)

        fresh = browser_act(
            verb="click", session="abcd", ref="@e9", observation_id=second["observation_id"], go=True
        )
        self.assertTrue(fresh["ok"], fresh)
        self.assertEqual(len(self.calls()), spawned + 1)

    def test_navigation_and_borrow_drop_the_observe(self):
        obs = self.observe()
        nav = browser_act(verb="navigate", session="abcd", url="https://example.com", go=True)
        self.assertTrue(nav["ok"], nav)
        self.assertEqual(
            self.calls()[-1],
            ["navigate", "--session", "abcd", "--json", "--", "https://example.com"],
        )
        after = browser_act(verb="click", session="abcd", ref="@e3", observation_id=obs["observation_id"], go=True)
        self.assertEqual(after["reason"], "stale_observation")
        self.assertIsNone(after["latest_observation_id"])

        again = self.observe()
        borrowed = browser_tabs(action="borrow", session="abcd", tab_id=3, go=True)
        self.assertTrue(borrowed["ok"], borrowed)
        dropped = browser_act(
            verb="click", session="abcd", ref="@e3", observation_id=again["observation_id"], go=True
        )
        self.assertEqual(dropped["reason"], "stale_observation")

    def test_click_that_changes_the_url_drops_refs(self):
        os.environ["BSK_FAKE_PAGE_URL"] = "https://a.example/"
        obs = self.observe()
        os.environ["BSK_FAKE_URL"] = "https://a.example/"
        same = browser_act(verb="click", session="abcd", ref="@e3", observation_id=obs["observation_id"])
        self.assertTrue(same["ok"], same)
        still = browser_act(verb="press", session="abcd", ref="@e3", observation_id=obs["observation_id"], key="Enter")
        self.assertTrue(still["ok"], still)

        os.environ["BSK_FAKE_URL"] = "https://b.example/"
        moved = browser_act(verb="fill", session="abcd", ref="@e3", observation_id=obs["observation_id"], value="go")
        self.assertTrue(moved["ok"], moved)
        self.assertTrue(any("URL changed" in note for note in moved["warnings"]))
        stale = browser_act(verb="click", session="abcd", ref="@e3", observation_id=obs["observation_id"])
        self.assertEqual(stale["reason"], "stale_observation")
        self.assertIsNone(stale["latest_observation_id"])
        self.assertEqual(len(self.calls()), 4)

    def test_previous_url_in_the_result_drops_refs(self):
        obs = self.observe()
        os.environ["BSK_FAKE_PREVIOUS_URL"] = "https://a.example/"
        os.environ["BSK_FAKE_URL"] = "https://b.example/"
        moved = browser_act(verb="click", session="abcd", ref="@e3", observation_id=obs["observation_id"])
        self.assertTrue(moved["ok"], moved)
        stale = browser_act(verb="click", session="abcd", ref="@e3", observation_id=obs["observation_id"])
        self.assertEqual(stale["reason"], "stale_observation")
        self.assertEqual(self.calls()[-1][0], "click")
        self.assertEqual(len(self.calls()), 2)

    def test_missing_target_clears_refs_and_says_observe_again(self):
        obs = self.observe()
        oid = obs["observation_id"]
        for mode, reason in (
            ("ref_not_found", "ref_not_found"),
            ("visual_target_changed", "visual_target_changed"),
        ):
            reset_browser_state()
            os.environ["BSK_FAKE_MODE"] = "ok"
            obs = self.observe()
            oid = obs["observation_id"]
            os.environ["BSK_FAKE_MODE"] = mode
            before = len(self.calls())
            out = browser_act(verb="click", session="abcd", ref="@e3", observation_id=oid)
            self.assertEqual(out["reason"], reason, out)
            self.assertIn("observe again", out["hint"])
            os.environ["BSK_FAKE_MODE"] = "ok"
            stale = browser_act(verb="click", session="abcd", ref="@e3", observation_id=oid)
            self.assertEqual(stale["reason"], "stale_observation")
            self.assertIsNone(stale["latest_observation_id"])
            self.assertEqual(len(self.calls()), before + 1)

    def test_cursor_page_replaces_refs_because_bsk_does(self):
        first = self.observe()
        os.environ["BSK_FAKE_OBSERVE"] = '[link] @e9 "Next"'
        second = browser_observe(session="abcd", cursor="page-2")
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["refs"], ["@e9"])
        self.assertIn("--cursor=page-2", second["command"])
        self.assertTrue(any("replaces the ref map" in note for note in second["warnings"]))
        stale = browser_act(verb="click", session="abcd", ref="@e3", observation_id=first["observation_id"])
        self.assertEqual(stale["reason"], "stale_observation")
        also = browser_act(verb="click", session="abcd", ref="@e3", observation_id=second["observation_id"])
        self.assertEqual(also["reason"], "stale_observation")
        self.assertEqual(len(self.calls()), 2)
        live = browser_act(verb="hover", session="abcd", ref="@e9", observation_id=second["observation_id"])
        self.assertTrue(live["ok"], live)

    def test_snapshot_refs_count_as_an_observe(self):
        os.environ["BSK_FAKE_OBSERVE"] = "link @e4"
        out = browser_observe(session="abcd", kind="snapshot", max_tokens=40)
        self.assertEqual(out["verb"], "snapshot")
        self.assertEqual(out["refs"], ["@e4"])
        self.assertEqual(self.calls()[-1][0], "snapshot")
        self.assertIn("--max-tokens", self.calls()[-1])
        live = browser_act(verb="hover", session="abcd", ref="@e4", observation_id=out["observation_id"], go=True)
        self.assertTrue(live["ok"], live)


class TestBackendSeam(BrowserCase):
    def test_tools_call_whatever_backend_is_installed(self):
        seen = {}

        class FakeBackend:
            name = "inprocess"

            def executable(self):
                return "bsk-inprocess"

            def run(self, args, *, timeout):
                seen["args"] = list(args)
                seen["timeout"] = timeout
                return BskRun(
                    argv=command_argv("bsk-inprocess", args),
                    exit_code=0,
                    stdout=json.dumps({"daemon": "in-process"}),
                    stderr="",
                )

        set_backend(FakeBackend())
        out = browser_status()
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["backend"], "inprocess")
        self.assertEqual(out["result"]["daemon"], "in-process")
        self.assertEqual(seen["args"], ["status"])
        self.assertEqual(self.calls(), [])


class TestRegistrationAndNotices(unittest.TestCase):
    def test_mcp_server_registers_browser_tools_beside_act(self):
        from test_agent_friction import _mcp_tools

        tools = _mcp_tools()
        for name in (
            "windows",
            "perceive_window",
            "act",
            "browser_status",
            "browser_session",
            "browser_observe",
            "browser_tabs",
            "browser_act",
        ):
            self.assertIn(name, tools)
        self.assertIn("dry_run", inspect.signature(tools["browser_act"]).parameters)
        self.assertIn("go", inspect.signature(tools["browser_act"]).parameters)

    def test_notices_credit_browserskill_and_runtime_text_is_unbranded(self):
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        self.assertIn("BrowserSkill", notices)
        self.assertIn("Copyright (c) 2026 Tencent", notices)
        self.assertIn("Not vendored", notices)
        self.assertIn("MIT", notices)
        docs = " ".join(fn.__doc__ or "" for fn in (
            browser_status, browser_session, browser_observe, browser_tabs, browser_act
        ))
        self.assertNotIn("Tencent", docs)
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("browser_act", skill)
        self.assertIn("perceive_window", skill)
        self.assertIn("browser_act", readme)
        self.assertIn("CU_PERCEIVE_BSK", readme)
        self.assertIn("CU_PERCEIVE_BSK_REQUIRE_GO", readme)
        self.assertIn("CU_PERCEIVE_BSK_REQUIRE_GO", skill)


if __name__ == "__main__":
    unittest.main()
