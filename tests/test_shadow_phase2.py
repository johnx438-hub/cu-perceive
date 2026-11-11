"""Phase 2 of the shadow browser: loopback-only remote debugging and the edge-shadow row.

Fake-filesystem cases run everywhere; real 8.3 / junction cases for Edge's main root need Windows
and a temp dir (they never touch the real browser profiles and never start a browser).
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cu_perceive import launch as L
from cu_perceive.launch import DebugAddressRefused, LaunchRefused, ProfileRefused, guard_browser_args, resolve

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_profile_guard_paths import CHROME, EDGE, EMAIN, LOCAL, MAIN, SHADOW, FakeFS  # noqa: E402
from test_plan import FakeDeps  # noqa: E402

ESHADOW = r"C:\Users\u\.cu-perceive-trial-edge-profile"


class FakeCase(unittest.TestCase):
    def setUp(self):
        self.fs = FakeFS(
            dirs=[MAIN + r"\Default", EMAIN + r"\Default", EMAIN + r"\Profile 1", SHADOW, ESHADOW, r"C:\tmp"],
            short={LOCAL + r"\Microsoft\Edge\USERDA~1": EMAIN, LOCAL + r"\MICROS~1\Edge\User Data": EMAIN},
            links={r"C:\tmp\j-edge": EMAIN, r"C:\tmp\j-edge-default": EMAIN + r"\Default",
                   r"C:\tmp\j-eshadow": ESHADOW},
        )
        self._p = self.fs.patch()
        self._p.start()
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": LOCAL, "CU_PERCEIVE_SHADOW_PROFILE_CHROME": SHADOW,
                                                 "CU_PERCEIVE_SHADOW_PROFILE_EDGE": ESHADOW})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._p.stop()


class TestDebugLoopbackOnly(FakeCase):
    def test_port_pins_loopback_address(self):
        for exe in (CHROME, EDGE):
            tail, info = guard_browser_args(exe, ["--remote-debugging-port=9333", "--new-window"])
            self.assertIn("--remote-debugging-port=9333", tail)
            self.assertIn("--remote-debugging-address=127.0.0.1", tail)
            self.assertEqual(info["debug_port"], 9333)
            self.assertEqual(info["debug_address"], "127.0.0.1")
            self.assertEqual(sum(a.lower().startswith("--remote-debugging-address") for a in tail), 1)

    def test_switch_spellings_of_port(self):
        for flag in ("-remote-debugging-port", "/remote-debugging-port", "--REMOTE-DEBUGGING-PORT"):
            tail, info = guard_browser_args(CHROME, [f"{flag}=0"])
            self.assertIn("--remote-debugging-port=0", tail)
            self.assertIn("--remote-debugging-address=127.0.0.1", tail)

    def test_loopback_addresses_accepted_and_rewritten(self):
        for addr in ("127.0.0.1", "localhost", "LOCALHOST", '"127.0.0.1"', " localhost "):
            tail, _ = guard_browser_args(CHROME, [f"--remote-debugging-address={addr}", "--remote-debugging-port=9333"])
            self.assertEqual([a for a in tail if "address" in a], ["--remote-debugging-address=127.0.0.1"])

    def test_non_loopback_refused(self):
        for addr in ("0.0.0.0", "::", "[::]", "::1", "192.168.1.5", "100.64.0.1", "host.tailnet.example",
                     "127.0.0.2", "127.1", "localhost.", "", "*"):
            for flag in ("--remote-debugging-address", "-remote-debugging-address", "/Remote-Debugging-Address"):
                with self.assertRaises(DebugAddressRefused, msg=f"{flag}={addr}"):
                    guard_browser_args(CHROME, [f"{flag}={addr}", "--remote-debugging-port=9333"])
        with self.assertRaises(DebugAddressRefused):  # address without a port is still checked
            guard_browser_args(EDGE, ["--remote-debugging-address=0.0.0.0"])

    def test_bare_or_bad_port_refused(self):
        for bad in (["--remote-debugging-address"], ["--remote-debugging-port"], ["--remote-debugging-port=abc"],
                    ["--remote-debugging-port=70000"], ["--remote-debugging-port=-1"],
                    ["--remote-debugging-port=9333", "--remote-debugging-port=9444"],
                    ["--", "--remote-debugging-port=9333"], ["--", "x --remote-debugging-address=0.0.0.0"]):
            with self.assertRaises(DebugAddressRefused, msg=repr(bad)):
                guard_browser_args(CHROME, bad)

    def test_same_port_twice_ok_and_address_before_dashdash(self):
        tail, _ = guard_browser_args(CHROME, ["--remote-debugging-port=9333", "--remote-debugging-port=9333",
                                              "--", "about:blank"])
        self.assertLess(tail.index("--remote-debugging-address=127.0.0.1"), tail.index("--"))

    def test_no_debugging_no_pin(self):
        tail, info = guard_browser_args(CHROME, ["--new-window"])
        self.assertFalse(any("remote-debugging" in a for a in tail))
        self.assertNotIn("debug_port", info)

    def test_pipe_is_local_and_untouched(self):
        tail, info = guard_browser_args(CHROME, ["--remote-debugging-pipe"])
        self.assertIn("--remote-debugging-pipe", tail)
        self.assertNotIn("debug_port", info)

    def test_other_programs(self):
        with self.assertRaises(DebugAddressRefused):
            guard_browser_args(r"C:\x\other.exe", ["--remote-debugging-address=0.0.0.0"])
        with self.assertRaises(DebugAddressRefused):
            guard_browser_args(r"C:\x\other.exe", ["run with /remote-debugging-address=10.0.0.1 please"])
        self.assertEqual(guard_browser_args(r"C:\x\other.exe", ["--remote-debugging-address=127.0.0.1"]),
                         (["--remote-debugging-address=127.0.0.1"], {}))

    def test_reasons_and_hierarchy(self):
        self.assertTrue(issubclass(DebugAddressRefused, LaunchRefused))
        self.assertTrue(issubclass(ProfileRefused, LaunchRefused))
        self.assertTrue(issubclass(LaunchRefused, ValueError))
        self.assertEqual(DebugAddressRefused.reason, "debug_address_refused")
        self.assertEqual(ProfileRefused.reason, "profile_refused")

    def test_main_profile_still_checked_with_debugging(self):
        with self.assertRaises(ProfileRefused):
            guard_browser_args(CHROME, [f"--user-data-dir={MAIN}", "--remote-debugging-port=9333"])


class TestEdgeShadow(FakeCase):
    def test_packaged_row(self):
        rows = json.loads((Path(L.__file__).parent / "apps.default.json").read_text(encoding="utf-8"))
        row = next(r for r in rows if r["name"] == "edge-shadow")
        self.assertTrue(row["exe"].lower().endswith("msedge.exe"))
        self.assertFalse(any("user-data-dir" in a.lower() for a in row.get("args", [])))
        self.assertIn("--new-window", row["args"])

    def test_resolve_defaults_to_edge_shadow(self):
        with mock.patch.dict(os.environ, {"CU_PERCEIVE_APPS": str(Path(L.__file__).parent / "apps.default.json")}):
            out = resolve(app="edge-shadow")
        self.assertEqual(out["browser"], "edge")
        self.assertEqual(out["profile_guard"], "defaulted_to_shadow")
        self.assertEqual(out["user_data_dir"], ESHADOW)
        self.assertEqual(out["args"][0], f"--user-data-dir={ESHADOW}")

    def test_edge_main_refused_every_spelling(self):
        for p in (EMAIN, EMAIN + r"\Default", EMAIN + r"\Profile 1", EMAIN.upper(), EMAIN.replace("\\", "/"),
                  EMAIN + ".", "\\\\?\\" + EMAIN, "\\\\.\\" + EMAIN, LOCAL + r"\Microsoft\Edge\USERDA~1",
                  LOCAL + r"\MICROS~1\Edge\User Data", r"C:\tmp\j-edge", r"C:\tmp\j-edge\Default",
                  r"C:\tmp\j-edge-default", "%LOCALAPPDATA%\\Microsoft\\Edge\\User Data",
                  LOCAL + r"\Microsoft\Edge Beta\User Data", LOCAL + r"\Microsoft\Edge SxS\User Data\Default"):
            with self.assertRaises(ProfileRefused, msg=p):
                guard_browser_args(EDGE, [], user_data_dir=p)
            with self.assertRaises(ProfileRefused, msg=p):
                guard_browser_args(EDGE, [f"/user-data-dir={p}"])

    def test_edge_shadow_env_through_link_to_main_refused(self):
        with mock.patch.dict(os.environ, {"CU_PERCEIVE_SHADOW_PROFILE_EDGE": r"C:\tmp\j-edge"}):
            with self.assertRaises(ProfileRefused):
                guard_browser_args(EDGE, [])

    def test_edge_resolved_link_to_shadow_allowed(self):
        tail, info = guard_browser_args(EDGE, [], user_data_dir=r"C:\tmp\j-eshadow")
        self.assertEqual(tail, [f"--user-data-dir={ESHADOW}"])
        self.assertEqual(info["profile_guard"], "explicit")

    def test_known_folder_root_also_guarded(self):
        other_local = r"D:\RealLocal"
        fs = FakeFS(dirs=[other_local + r"\Microsoft\Edge\User Data\Default", ESHADOW, r"C:\tmp"])
        with mock.patch.multiple(L, _fs_exists=fs.exists, _fs_long_path=fs.long_path, _fs_realpath=fs.realpath,
                                 _local_appdata_roots=lambda: [LOCAL, other_local]):
            with self.assertRaises(ProfileRefused):
                guard_browser_args(EDGE, [], user_data_dir=other_local + r"\Microsoft\Edge\User Data")

    def test_chrome_main_is_not_edge_main_but_both_guarded(self):
        tail, _ = guard_browser_args(EDGE, [], user_data_dir=r"C:\tmp\edge-trial")
        self.assertEqual(tail, [r"--user-data-dir=C:\tmp\edge-trial"])
        with self.assertRaises(ProfileRefused):
            guard_browser_args(r"C:\x\msedge_proxy.exe", [], user_data_dir=EMAIN)


class TestRefusalReasonsSurface(FakeCase):
    def test_plan_dry_run_reports_debug_address_refused(self):
        from cu_perceive.plan import run_plan

        deps = FakeDeps()
        deps.launch_plan = lambda **kw: resolve(exe=EDGE, **{k: v for k, v in kw.items()
                                                             if k in ("args", "user_data_dir")})
        out = run_plan({"steps": [{"op": "launch", "exe": EDGE, "args": ["--remote-debugging-address=0.0.0.0"]}]},
                       deps=deps)
        self.assertEqual([w["reason"] for w in out["would_fail"]], ["debug_address_refused"])
        self.assertEqual(deps.launched, [])
        out = run_plan({"steps": [{"op": "launch", "exe": EDGE, "args": ["--remote-debugging-port=9333"]}]},
                       deps=deps)
        self.assertIn("--remote-debugging-address=127.0.0.1", json.dumps(out))

    def test_cli_launch_resolve_only(self):
        from cu_perceive import cli

        with mock.patch.dict(os.environ, {"CU_PERCEIVE_APPS": str(Path(L.__file__).parent / "apps.default.json")}):
            buf = []
            with mock.patch.object(cli, "_emit", lambda out, args: buf.append(out)):
                rc = cli.main(["launch", "--app", "edge-shadow", "--resolve-only",
                               "--arg=--remote-debugging-address=0.0.0.0"])
        self.assertEqual(rc, 1)
        self.assertEqual(buf[0]["reason"], "debug_address_refused")


@unittest.skipUnless(os.name == "nt", "real 8.3 names and junctions need Windows")
class TestRealEdgePaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cu-edge-guard-")
        self.root = Path(self.tmp.name)
        self.local = self.root / "Local"
        self.main = self.local / "Microsoft" / "Edge" / "User Data"
        (self.main / "Default").mkdir(parents=True)
        self.shadow = self.root / "eshadow"
        self.shadow.mkdir()
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.local),
                                                 "CU_PERCEIVE_SHADOW_PROFILE_EDGE": str(self.shadow)})
        self._env.start()
        self._roots = mock.patch.object(L, "_local_appdata_roots", lambda: [str(self.local)])
        self._roots.start()

    def tearDown(self):
        self._roots.stop()
        self._env.stop()
        for p in self.root.iterdir():
            if p.is_symlink() or p.is_junction():
                try:
                    os.rmdir(p)
                except OSError:
                    p.unlink(missing_ok=True)
        self.tmp.cleanup()

    def test_short_prefix_and_junction(self):
        refused = ["\\\\?\\" + str(self.main), str(self.main) + ".", str(self.main / "Default").upper()]
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(self.main), buf, 1024)
        if n and buf.value.lower() != str(self.main).lower():
            refused.append(buf.value)
        link = self.root / "j-edge"
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.main)], capture_output=True, text=True)
        if r.returncode == 0:
            refused += [str(link), str(link / "Default")]
        for p in refused:
            with self.assertRaises(ProfileRefused, msg=p):
                guard_browser_args(EDGE, [], user_data_dir=p)
        tail, info = guard_browser_args(EDGE, ["--remote-debugging-port=9333"])
        self.assertEqual(info["profile_guard"], "defaulted_to_shadow")
        self.assertIn("--remote-debugging-address=127.0.0.1", tail)


if __name__ == "__main__":
    unittest.main()
