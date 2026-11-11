"""Main-profile guard against path spellings that reach the same folder.

Fake-filesystem cases run everywhere. Real 8.3 / junction / symlink / \\\\?\\ cases need Windows and
a temp dir (they never touch the real browser profile and never start a browser).
"""
from __future__ import annotations

import ctypes
import ntpath
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cu_perceive import launch as L
from cu_perceive.launch import ProfileRefused, guard_browser_args

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def _k(p: str) -> str:
    return ntpath.normcase(ntpath.normpath(p)).rstrip("\\")


class FakeFS:
    """Directories, 8.3 short names (short path -> long path) and links (link path -> target)."""

    def __init__(self, dirs, short=None, links=None):
        self.dirs = set()
        for d in dirs:
            cur = d
            while True:
                self.dirs.add(_k(cur))
                parent = ntpath.dirname(cur)
                if parent == cur:
                    break
                cur = parent
        self.short = {_k(a): b for a, b in (short or {}).items()}
        self.links = {_k(a): b for a, b in (links or {}).items()}

    def _walk(self, p: str, *, follow: bool, strict: bool) -> str:
        drive, rest = ntpath.splitdrive(ntpath.normpath(p))
        cur = drive + "\\"
        for comp in [c for c in rest.split("\\") if c]:
            cur = ntpath.join(cur, comp)
            cur = self.short.get(_k(cur), cur)
            if follow and _k(cur) in self.links:
                cur = self.links[_k(cur)]
                if strict and _k(cur) not in self.dirs:
                    raise FileNotFoundError(2, "broken link", cur)
        return cur

    def exists(self, p: str) -> bool:
        if _k(p) in self.links or _k(p) in self.short:
            return True
        try:
            return _k(self._walk(p, follow=True, strict=False)) in self.dirs
        except OSError:
            return False

    def long_path(self, p: str) -> str:
        return self._walk(p, follow=False, strict=False)

    def realpath(self, p: str) -> str:
        out = self._walk(p, follow=True, strict=True)
        if _k(out) not in self.dirs:
            raise FileNotFoundError(2, "missing", out)
        return out

    def patch(self):
        return mock.patch.multiple(L, _fs_exists=self.exists, _fs_long_path=self.long_path,
                                   _fs_realpath=self.realpath, _local_appdata_roots=lambda: [LOCAL])


LOCAL = r"C:\Users\u\AppData\Local"
MAIN = LOCAL + r"\Google\Chrome\User Data"
EMAIN = LOCAL + r"\Microsoft\Edge\User Data"
SHADOW = r"C:\Users\u\.cu-perceive-trial-chrome-profile"


class FakeCase(unittest.TestCase):
    def setUp(self):
        self.fs = FakeFS(
            dirs=[MAIN + r"\Default", MAIN + r"\Profile 1", EMAIN + r"\Default", SHADOW, r"C:\tmp",
                  r"D:\elsewhere\Default"],
            short={LOCAL + r"\Google\Chrome\USERDA~1": MAIN, LOCAL + r"\Google\Chrome\User Data\DEFAUL~1": MAIN + r"\Default",
                   r"C:\Users\u\AppData\Local\Google\CHROME~1": LOCAL + r"\Google\Chrome"},
            links={r"C:\tmp\j-main": MAIN, r"C:\tmp\j-default": MAIN + r"\Default", r"C:\tmp\j-local": LOCAL,
                   r"C:\tmp\broken": r"C:\nowhere\gone", r"C:\tmp\j-shadow": SHADOW},
        )
        self._p = self.fs.patch()
        self._p.start()
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": LOCAL, "CU_PERCEIVE_SHADOW_PROFILE_CHROME": SHADOW})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._p.stop()

    def refused(self, udd=None, args=(), exe=CHROME):
        with self.assertRaises(ProfileRefused, msg=f"{udd!r} {args!r}"):
            guard_browser_args(exe, list(args), user_data_dir=udd)

    def allowed(self, udd=None, args=(), exe=CHROME):
        tail, info = guard_browser_args(exe, list(args), user_data_dir=udd)
        return tail, info


class TestFakeBypasses(FakeCase):
    def test_short_names(self):
        for p in (LOCAL + r"\Google\Chrome\USERDA~1", LOCAL + r"\Google\Chrome\userda~1",
                  LOCAL + r"\Google\Chrome\USERDA~1\Default", MAIN + r"\DEFAUL~1",
                  LOCAL + r"\Google\CHROME~1\User Data"):
            self.refused(p)
            self.refused(args=[f"--user-data-dir={p}"])

    def test_device_prefixes(self):
        for p in ("\\\\?\\" + MAIN, "\\\\?\\" + MAIN.upper(), "\\\\.\\" + MAIN, "\\??\\" + MAIN,
                  "\\\\?\\" + MAIN + "\\Default", "//?/" + MAIN.replace("\\", "/")):
            self.refused(p)

    def test_unc_and_device_namespaces_refused(self):
        for p in (r"\\?\UNC\localhost\C$\Users\u\AppData\Local\Google\Chrome\User Data",
                  r"\\localhost\C$\Users\u\AppData\Local\Google\Chrome\User Data",
                  r"\\127.0.0.1\c$\x", r"\\?\Volume{12345678-1234-1234-1234-123456789abc}\Users\u",
                  r"\\.\GLOBALROOT\Device\HarddiskVolume3\Users", r"\\?\C:", r"C:relative\User Data",
                  MAIN + r":stream", MAIN + r"::$INDEX_ALLOCATION"):
            self.refused(p)

    def test_links(self):
        for p in (r"C:\tmp\j-main", r"C:\tmp\j-main\Default", r"C:\tmp\j-default", r"C:\tmp\J-MAIN",
                  r"C:\tmp\j-local\Google\Chrome\User Data", r"C:\tmp\j-main\not-yet\deeper"):
            self.refused(p)

    def test_broken_link_refused(self):
        self.refused(r"C:\tmp\broken")
        self.refused(r"C:\tmp\broken\child")

    def test_lexical_variants(self):
        for p in (MAIN.upper(), MAIN.replace("\\", "/"), MAIN + "\\", MAIN + "/", MAIN + ".", MAIN + " ",
                  MAIN + ". .", MAIN + r"\Default.", LOCAL + r"\Google\Chrome.\User Data",
                  LOCAL + r"\Google\Chrome\x\..\User Data", SHADOW + r"\..\AppData\Local\Google\Chrome\User Data",
                  "%LOCALAPPDATA%\\Google\\Chrome\\User Data", f'"{MAIN}"'):
            self.refused(p)

    def test_component_of_only_dots_refused(self):
        self.refused(r"C:\tmp\...\x")

    def test_switch_spellings(self):
        for flag in ("--user-data-dir", "-user-data-dir", "/user-data-dir", "/USER-DATA-DIR", "--User-Data-Dir"):
            self.refused(args=[f"{flag}={MAIN}"])
            self.refused(args=[flag])  # bare
        tail, _ = self.allowed(args=["/user-data-dir=C:\\tmp\\mine", "--new-window"])
        self.assertEqual(tail, ["--user-data-dir=C:\\tmp\\mine", "--new-window"])
        self.refused(args=["--user-data-dir=C:\\tmp\\a", "-user-data-dir=C:\\tmp\\b"])

    def test_after_dashdash_refused(self):
        self.refused(args=["--", f"--user-data-dir={MAIN}"])

    def test_main_root_is_itself_a_link(self):
        fs = FakeFS(dirs=[r"D:\real\User Data\Default", r"C:\tmp", SHADOW, LOCAL + r"\Google\Chrome"],
                    links={MAIN: r"D:\real\User Data"})
        with fs.patch():
            self.refused(r"D:\real\User Data")
            self.refused(r"D:\real\User Data\Default")
            self.refused(MAIN)

    def test_shadow_env_through_link_to_main(self):
        with mock.patch.dict(os.environ, {"CU_PERCEIVE_SHADOW_PROFILE_CHROME": r"C:\tmp\j-main"}):
            self.refused()

    def test_allowed_and_resolved(self):
        tail, info = self.allowed()
        self.assertEqual(info["profile_guard"], "defaulted_to_shadow")
        tail, info = self.allowed(r"C:\tmp\j-shadow")
        self.assertEqual(tail, [f"--user-data-dir={SHADOW}"])  # argv carries the resolved path
        tail, _ = self.allowed(MAIN + "-shadow")  # sibling, not inside main
        self.assertEqual(tail, [f"--user-data-dir={MAIN}-shadow"])
        tail, _ = self.allowed(r"C:\tmp\new\profile")  # nonexistent: nearest parent resolved
        self.assertEqual(tail, [r"--user-data-dir=C:\tmp\new\profile"])
        self.allowed(r"D:\elsewhere")

    def test_edge(self):
        self.refused(EMAIN, exe=EDGE)
        self.refused("\\\\?\\" + EMAIN + "\\Default", exe=EDGE)

    def test_exe_aliases_are_browsers(self):
        for exe in (r"C:\x\chrome_proxy.exe", r"C:\x\CHROME.EXE", "C:\\x\\chrome.exe.", "C:/x/chrome.exe ",
                    r"C:\x\msedge_proxy.exe"):
            self.assertIsNotNone(L.browser_kind(exe), exe)
            self.refused(MAIN if "chrome" in exe.lower() else EMAIN, exe=exe)

    def test_shell_wrapping_browser_refused(self):
        self.refused(args=["/c", "start", "chrome"], exe=r"C:\Windows\System32\cmd.exe")
        self.refused(args=["/c", "start chrome.exe --new-window"], exe=r"C:\Windows\System32\cmd.exe")
        self.refused(args=["-Command", "Start-Process msedge"], exe="powershell.exe")
        self.refused(args=["/c", f"C:\\x\\chrome.exe --user-data-dir=\"{MAIN}\""], exe=r"C:\Windows\System32\cmd.exe")
        self.refused(args=[f"--user-data-dir={MAIN}"], exe=r"C:\x\other.exe")
        self.assertEqual(guard_browser_args(r"C:\Windows\System32\cmd.exe", ["/c", "echo", "hi"]),
                         (["/c", "echo", "hi"], {}))
        self.assertEqual(guard_browser_args(r"C:\x\app.exe", ["--chrome-like-flag"]), (["--chrome-like-flag"], {}))


@unittest.skipUnless(os.name == "nt", "real 8.3 names, junctions and symlinks need Windows")
class TestRealWindowsPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cu-guard-")
        root = Path(self.tmp.name)
        self.root = root
        self.local = root / "Local"
        self.main = self.local / "Google" / "Chrome" / "User Data"
        (self.main / "Default").mkdir(parents=True)
        self.shadow = root / "shadow"
        self.shadow.mkdir()
        self._env = mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.local),
                                                 "CU_PERCEIVE_SHADOW_PROFILE_CHROME": str(self.shadow)})
        self._env.start()
        self._roots = mock.patch.object(L, "_local_appdata_roots", lambda: [str(self.local)])
        self._roots.start()

    def tearDown(self):
        self._roots.stop()
        self._env.stop()
        for p in self.root.iterdir():  # links are direct children; remove the link, never its target
            if p.is_symlink() or p.is_junction():
                try:
                    os.rmdir(p)
                except OSError:
                    p.unlink(missing_ok=True)
        self.tmp.cleanup()

    def refused(self, udd):
        with self.assertRaises(ProfileRefused, msg=udd):
            guard_browser_args(CHROME, [], user_data_dir=udd)

    def _junction(self, link: Path, target: Path) -> None:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest(f"mklink /J failed: {r.stdout}{r.stderr}")

    def test_short_name(self):
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(self.main), buf, 1024)
        if not n or buf.value.lower() == str(self.main).lower():
            self.skipTest("volume has no 8.3 names")
        self.refused(buf.value)
        self.refused(buf.value.lower())
        self.refused(buf.value + "\\Default")

    def test_extended_prefix(self):
        self.refused("\\\\?\\" + str(self.main))
        self.refused("\\\\?\\" + str(self.main / "Default"))
        self.refused("\\\\.\\" + str(self.main))

    def test_junctions(self):
        self._junction(self.root / "j-main", self.main)
        self._junction(self.root / "j-default", self.main / "Default")
        self._junction(self.root / "j-local", self.local)
        for p in ("j-main", r"j-main\Default", "j-default", r"j-local\Google\Chrome\User Data", r"j-main\new\x"):
            self.refused(str(self.root / p))
        other = self.root / "other"
        other.mkdir()
        self._junction(self.root / "j-other", other)
        tail, info = guard_browser_args(CHROME, [], user_data_dir=str(self.root / "j-other"))
        self.assertEqual(L._key(info["user_data_dir"]), L._key(os.path.realpath(other)))

    def test_symlink(self):
        link = self.root / "s-main"
        try:
            os.symlink(self.main, link, target_is_directory=True)
        except OSError as e:
            self.skipTest(f"no symlink privilege: {e}")
        self.refused(str(link))
        self.refused(str(link / "Default"))

    def test_trailing_dot_and_case(self):
        self.refused(str(self.main) + ".")
        self.refused(str(self.main).upper() + " ")
        self.refused(str(self.main).replace("\\", "/"))

    def test_real_shadow_allowed(self):
        tail, info = guard_browser_args(CHROME, [])
        self.assertEqual(info["profile_guard"], "defaulted_to_shadow")
        self.assertEqual(L._key(info["user_data_dir"]), L._key(os.path.realpath(self.shadow)))


if __name__ == "__main__":
    unittest.main()
