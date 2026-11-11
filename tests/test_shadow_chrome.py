"""Shadow-profile browser launches (args / user_data_dir guard) and bsk cooperate context."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cu_perceive import launch as launch_mod
from cu_perceive.launch import ProfileRefused, guard_browser_args, normalize_args, resolve
from cu_perceive.plan import run_plan, validate_plan
from cu_perceive.browser.ledger import LEDGER
from cu_perceive.browser.tools import browser_act, browser_cooperate, browser_session

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_browser_bsk import BrowserCase  # noqa: E402
from test_plan import FakeDeps  # noqa: E402


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.local = root / "Local"
        self.shadow = root / "shadow-chrome"
        self.eshadow = root / "shadow-edge"
        self._env = mock.patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.local),
            "CU_PERCEIVE_SHADOW_PROFILE_CHROME": str(self.shadow),
            "CU_PERCEIVE_SHADOW_PROFILE_EDGE": str(self.eshadow),
        })
        self._env.start()
        self.chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        self.edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()


class TestNormalizeArgs(unittest.TestCase):
    def test_list_string_and_empty(self):
        self.assertEqual(normalize_args(None), [])
        self.assertEqual(normalize_args(["--a", "", "b"]), ["--a", "b"])
        self.assertEqual(normalize_args('--new-window "https://x.test/a b"'), ["--new-window", "https://x.test/a b"])

    def test_rejects_bad_types_and_sizes(self):
        for bad in ([1], [True], 5, ["a\x00b"], ["x" * 5000], ["a"] * 65):
            with self.assertRaises(ValueError, msg=repr(bad)[:40]):
                normalize_args(bad)


class TestBrowserGuard(GuardCase):
    def test_defaults_to_shadow_profile(self):
        tail, info = guard_browser_args(self.chrome, ["--new-window"])
        self.assertEqual(tail, [f"--user-data-dir={self.shadow}", "--new-window"])
        self.assertEqual(info["profile_guard"], "defaulted_to_shadow")
        self.assertEqual(info["browser"], "chrome")

    def test_edge_defaults_to_its_own_shadow(self):
        tail, info = guard_browser_args(self.edge, [])
        self.assertEqual(tail, [f"--user-data-dir={self.eshadow}"])
        self.assertEqual(info["browser"], "edge")

    def test_explicit_dir_kept_once(self):
        d = Path(self.tmp.name) / "mine"
        tail, info = guard_browser_args(self.chrome, [f"--user-data-dir={d}", "--x"], user_data_dir=str(d))
        self.assertEqual(tail, [f"--user-data-dir={d}", "--x"])
        self.assertEqual(info["profile_guard"], "explicit")

    def test_main_profile_refused(self):
        main = self.local / "Google" / "Chrome" / "User Data"
        for args, udd in (([f"--user-data-dir={main}"], None), ([], str(main)),
                          ([f"--user-data-dir={main / 'Profile 1'}"], None)):
            with self.assertRaises(ProfileRefused):
                guard_browser_args(self.chrome, args, user_data_dir=udd)
        with self.assertRaises(ProfileRefused):
            guard_browser_args(self.edge, [], user_data_dir=str(self.local / "Microsoft" / "Edge" / "User Data"))

    def test_sibling_of_main_is_not_main(self):
        sib = self.local / "Google" / "Chrome" / "User Data-shadow"
        tail, _ = guard_browser_args(self.chrome, [], user_data_dir=str(sib))
        self.assertEqual(tail, [f"--user-data-dir={sib}"])

    def test_bare_conflicting_and_empty_refused(self):
        with self.assertRaises(ProfileRefused):
            guard_browser_args(self.chrome, ["--user-data-dir", "C:\\x"])
        with self.assertRaises(ProfileRefused):
            guard_browser_args(self.chrome, ["--user-data-dir=C:\\a"], user_data_dir="C:\\b")
        with self.assertRaises(ProfileRefused):
            guard_browser_args(self.chrome, ["--user-data-dir="])

    def test_non_browser_untouched(self):
        self.assertEqual(guard_browser_args(r"C:\x\app.exe", ["--a"]), (["--a"], {}))
        with self.assertRaises(ValueError):
            guard_browser_args(r"C:\x\app.exe", [], user_data_dir="C:\\d")


class TestResolveAndLaunch(GuardCase):
    def apps(self, rows):
        return mock.patch.object(launch_mod, "list_apps", return_value=rows)

    def test_packaged_chrome_shadow_row(self):
        names = {r["name"] for r in json.loads(
            (Path(launch_mod.__file__).with_name("apps.default.json")).read_text(encoding="utf-8"))}
        self.assertIn("chrome-shadow", names)

    def test_row_args_then_call_args(self):
        rows = [{"name": "cs", "exe": self.chrome, "args": ["--no-first-run"]}]
        with self.apps(rows):
            out = resolve(app="cs", args=["about:blank"])
        self.assertEqual(out["args"], [f"--user-data-dir={self.shadow}", "--no-first-run", "about:blank"])
        self.assertEqual(out["app"], "cs")
        self.assertEqual(out["profile_guard"], "defaulted_to_shadow")

    def test_row_user_data_dir_main_refused(self):
        rows = [{"name": "bad", "exe": self.chrome,
                 "user_data_dir": str(self.local / "Google" / "Chrome" / "User Data")}]
        with self.apps(rows), self.assertRaises(ProfileRefused):
            resolve(app="bad")

    def test_launch_picks_new_window_by_pid(self):
        exe = Path(self.tmp.name) / "chrome.exe"
        exe.write_bytes(b"")
        old = {"hwnd": 1, "pid": 9, "title": "Old", "exe": "chrome.exe"}
        new_other = {"hwnd": 2, "pid": 8, "title": "Other", "exe": "chrome.exe"}
        mine = {"hwnd": 3, "pid": 4242, "title": "about:blank", "exe": "chrome.exe"}
        seq = [[old], [old, new_other, mine]]
        proc = mock.Mock(pid=4242)
        with mock.patch("cu_perceive.windows.find_windows", side_effect=lambda **k: seq.pop(0) if len(seq) > 1 else seq[0]), \
                mock.patch.object(launch_mod.subprocess, "Popen", return_value=proc) as popen, \
                mock.patch.object(launch_mod.time, "sleep"):
            out = launch_mod.launch(exe=str(exe), args=["--new-window"])
        argv = popen.call_args[0][0]
        self.assertEqual(argv[1:], [f"--user-data-dir={self.shadow}", "--new-window"])
        self.assertTrue(self.shadow.is_dir())
        self.assertEqual(out["window"]["hwnd"], 3)
        self.assertTrue(out["new_window"])

    def test_browser_without_new_window_warns_not_old(self):
        exe = Path(self.tmp.name) / "chrome.exe"
        exe.write_bytes(b"")
        old = {"hwnd": 1, "pid": 9, "title": "Old", "exe": "chrome.exe"}
        clock = iter(range(0, 1000))
        with mock.patch("cu_perceive.windows.find_windows", return_value=[old]), \
                mock.patch.object(launch_mod.subprocess, "Popen", return_value=mock.Mock(pid=1)), \
                mock.patch.object(launch_mod.time, "sleep"), \
                mock.patch.object(launch_mod.time, "monotonic", side_effect=lambda: float(next(clock))):
            out = launch_mod.launch(exe=str(exe))
        self.assertIsNone(out["window"])
        self.assertFalse(out["new_window"])
        self.assertTrue(out["warnings"])


class TestPlanLaunch(GuardCase):
    def test_validate_args_types(self):
        plan, err = validate_plan({"steps": [{"op": "launch", "app": "chrome-shadow", "args": ["about:blank"]}]})
        self.assertIsNone(err, err)
        plan, err = validate_plan({"steps": [{"op": "launch", "app": "chrome-shadow", "args": 5}]})
        self.assertIsNotNone(err)
        plan, err = validate_plan({"steps": [{"op": "launch", "app": "chrome-shadow", "user_data_dir": ""}]})
        self.assertIsNotNone(err)

    def test_dry_run_reports_would_args_and_refuses_main(self):
        deps = FakeDeps()
        deps.launch_plan = lambda **kw: resolve(exe=self.chrome, **{k: v for k, v in kw.items()
                                                                     if k in ("args", "user_data_dir")})
        out = run_plan({"steps": [{"op": "launch", "exe": self.chrome, "args": ["about:blank"]}]}, deps=deps)
        text = json.dumps(out)
        self.assertIn("would_args", text)
        self.assertIn("defaulted_to_shadow", text)
        self.assertEqual(deps.launched, [])
        main = str(self.local / "Google" / "Chrome" / "User Data")
        out = run_plan({"steps": [{"op": "launch", "exe": self.chrome, "user_data_dir": main}]}, deps=deps)
        self.assertEqual([w["reason"] for w in out["would_fail"]], ["profile_refused"])
        self.assertEqual(deps.launched, [])

    def test_go_passes_args_to_deps(self):
        deps = FakeDeps()
        run_plan({"steps": [{"op": "launch", "app": "chrome-shadow", "args": ["about:blank"],
                             "user_data_dir": "C:\\s"}]}, go=True, deps=deps)
        self.assertEqual(deps.launched, ["chrome-shadow"])
        self.assertEqual(deps.launch_kw, {"args": ["about:blank"], "user_data_dir": "C:\\s"})


class TestCooperateContext(BrowserCase):
    def setUp(self):
        super().setUp()
        os.environ["BSK_FAKE_MODE"] = "coop"

    def test_sync_stores_context_and_act_passes_it(self):
        out = browser_cooperate(action="sync", session="abcd", policy="target")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["context_stored"])
        self.assertEqual(LEDGER.context("abcd"), "ctx-1")
        self.assertIn("@e33", LEDGER.latest("abcd").refs)
        self.assertEqual(self.calls()[-1], ["cooperate", "sync", "--session", "abcd", "--json",
                                            "--policy", "target", "--settle"])
        obs = LEDGER.latest("abcd").id
        act = browser_act(verb="fill", session="abcd", ref="@e33", observation_id=obs, value="hi")
        self.assertTrue(act["ok"], act)
        last = self.calls()[-1]
        self.assertEqual(last[last.index("--context") + 1], "ctx-1")

    def test_status_is_read_only_and_bad_args(self):
        self.assertTrue(browser_cooperate(action="status", session="abcd")["ok"])
        self.assertIsNone(LEDGER.context("abcd"))
        self.assertEqual(browser_cooperate(action="grab", session="abcd")["reason"], "bad_args")
        self.assertEqual(browser_cooperate(action="sync")["reason"], "bad_args")
        self.assertEqual(browser_cooperate(action="sync", session="abcd", policy="x")["reason"], "bad_args")
        self.assertEqual(browser_cooperate(action="status", session="abcd", policy="auto")["reason"], "bad_args")

    def test_sync_required_auto_syncs_once_without_resending(self):
        self.observe()
        obs = LEDGER.latest("abcd").id
        out = browser_act(verb="click", session="abcd", ref="@e3", observation_id=obs)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "cooperation_sync_required")
        self.assertTrue(out["synced"])
        self.assertIn("@e33", out["refs"])
        self.assertNotEqual(out["observation_id"], obs)
        verbs = [c[:2] for c in self.calls()]
        self.assertEqual(sum(1 for c in self.calls() if "click" in c), 1)  # never re-sent
        self.assertIn(["cooperate", "sync"], verbs)
        self.assertEqual(LEDGER.context("abcd"), "ctx-1")
        again = browser_act(verb="click", session="abcd", ref="@e33", observation_id=out["observation_id"])
        self.assertTrue(again["ok"], again)

    def test_invalid_context_cleared_and_no_auto_sync(self):
        self.observe()
        LEDGER.set_context("abcd", "stale")
        out = browser_act(verb="click", session="abcd", ref="@e3", observation_id=LEDGER.latest("abcd").id,
                          auto_sync=False)
        self.assertEqual(out["reason"], "control_context_invalid")
        self.assertIsNone(LEDGER.context("abcd"))
        self.assertNotIn("synced", out)
        self.assertFalse(any(c[:1] == ["cooperate"] for c in self.calls()))

    def test_explicit_context_and_dry_run(self):
        out = browser_act(verb="navigate", session="abcd", url="https://example.com", context="ctx-1",
                          dry_run=True)
        self.assertTrue(out["dry_run"])
        cmd = out["command"]
        self.assertEqual(cmd[cmd.index("--context") + 1], "ctx-1")
        self.assertEqual(self.calls(), [])

    def test_session_stop_clears_context(self):
        LEDGER.set_context("abcd", "ctx-1")
        os.environ["BSK_FAKE_MODE"] = "ok"
        out = browser_session(action="stop", session="abcd")
        self.assertTrue(out["ok"], out)
        self.assertIsNone(LEDGER.context("abcd"))


if __name__ == "__main__":
    unittest.main()
