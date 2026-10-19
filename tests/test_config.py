"""Config: no hard-coded hosts / personal paths; env overrides; MCP allowed hosts opt-in."""
from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from cu_perceive import config

ROOT = Path(__file__).resolve().parents[1]


class TestNoHardcodedMachineValues(unittest.TestCase):
    PATTERNS = [
        re.compile(r"ts\.net", re.I),
        re.compile(r"tail[0-9a-f]{4,}", re.I),
        re.compile(r"[A-Z]:\\\\?Users\\\\?[A-Za-z]", re.I),
        re.compile(r"/mnt/c/Users/", re.I),
        re.compile(r"miniconda", re.I),
    ]

    def _files(self):
        for p in list((ROOT / "cu_perceive").rglob("*.py")) + list((ROOT / "cu_perceive").rglob("*.json")):
            yield p
        yield ROOT / "bin" / "cu-perceive.sh"
        yield ROOT / "pyproject.toml"

    def test_code_and_config_clean(self):
        hits = []
        for p in self._files():
            text = p.read_text(encoding="utf-8")
            for pat in self.PATTERNS:
                for m in pat.finditer(text):
                    hits.append(f"{p.relative_to(ROOT)}: {m.group(0)}")
        self.assertEqual(hits, [])


class TestEnvOverrides(unittest.TestCase):
    def test_out_weights_apps(self):
        env = {
            config.ENV_OUT: "/tmp/cu-out",
            config.ENV_WEIGHTS: "/tmp/cu-w",
            config.ENV_APPS: "/tmp/apps.json",
            config.ENV_YOLO_WEIGHT: "/tmp/best.pt",
        }
        with mock.patch.dict(os.environ, env):
            self.assertEqual(config.out_dir(), Path("/tmp/cu-out"))
            self.assertEqual(config.weights_dir(), Path("/tmp/cu-w"))
            self.assertEqual(config.apps_file(), Path("/tmp/apps.json"))
            self.assertEqual(config.yolo_weight(), Path("/tmp/best.pt"))

    def test_home_drives_defaults(self):
        with mock.patch.dict(os.environ, {config.ENV_HOME: "/tmp/cuhome"}):
            os.environ.pop(config.ENV_OUT, None)
            self.assertEqual(config.out_dir(), Path("/tmp/cuhome/shots/perceive"))

    def test_windows_default_is_localappdata(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": r"C:\LAD"}, clear=False), \
                mock.patch.object(config.sys, "platform", "win32"):
            os.environ.pop(config.ENV_HOME, None)
            self.assertEqual(config.data_root(), Path(r"C:\LAD") / "cu-perceive")

    def test_default_apps_file_is_packaged_and_portable(self):
        with mock.patch.dict(os.environ, {config.ENV_HOME: "/nonexistent-cu-home"}):
            os.environ.pop(config.ENV_APPS, None)
            self.assertEqual(config.apps_file(), config.DEFAULT_APPS_FILE)
        rows = json.loads(config.DEFAULT_APPS_FILE.read_text(encoding="utf-8"))
        self.assertTrue(rows)
        for r in rows:
            self.assertIn("%", r["exe"])

    def test_capture_default(self):
        with mock.patch.dict(os.environ, {config.ENV_CAPTURE: "PrintWindow"}):
            self.assertEqual(config.default_capture(), "printwindow")


class TestAllowedHosts(unittest.TestCase):
    def test_none_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(config.ENV_ALLOWED_HOSTS, None)
            self.assertEqual(config.allowed_hosts(), [])
            from cu_perceive.mcp_server import transport_security_lists

            hosts, origins = transport_security_lists()
        self.assertEqual(hosts, ["127.0.0.1:*", "localhost:*", "[::1]:*"])
        self.assertTrue(all("127.0.0.1" in o or "localhost" in o or "[::1]" in o for o in origins))

    def test_env_and_flag_merge(self):
        with mock.patch.dict(os.environ, {config.ENV_ALLOWED_HOSTS: "box.example.net, other "}):
            self.assertEqual(config.allowed_hosts(["box.example.net", "cli.host"]),
                             ["box.example.net", "other", "cli.host"])
            from cu_perceive.mcp_server import transport_security_lists

            hosts, origins = transport_security_lists(["cli.host"])
        self.assertIn("box.example.net:*", hosts)
        self.assertIn("cli.host", hosts)
        self.assertIn("https://other", origins)

    def test_cli_flag_reaches_serve(self):
        from cu_perceive import cli

        with mock.patch("cu_perceive.mcp_server.serve") as serve:
            cli.main(["mcp", "--allow-host", "a.example", "--allow-host", "b.example"])
        serve.assert_called_once_with(host="127.0.0.1", port=8771, allowed_hosts=["a.example", "b.example"])


class TestOutDirCoercion(unittest.TestCase):
    def test_posix_passthrough_off_windows(self):
        from cu_perceive import paths

        with mock.patch.object(paths.sys, "platform", "linux"):
            self.assertEqual(paths.coerce_out_dir("/home/u/shots"), "/home/u/shots")

    def test_windows_rewrites_wsl_paths(self):
        from cu_perceive import paths

        with mock.patch.object(paths.sys, "platform", "win32"):
            self.assertEqual(paths.coerce_out_dir("/mnt/c/x/y"), "C:\\x\\y")
            self.assertTrue(paths.coerce_out_dir("/home/u/s").startswith("\\\\wsl.localhost\\"))


class TestLaunchExpandsEnv(unittest.TestCase):
    def test_expand(self):
        from cu_perceive.launch import _expand

        with mock.patch.dict(os.environ, {"CU_TEST_DIR": "/opt/apps"}):
            self.assertEqual(_expand("$CU_TEST_DIR/app.exe"), "/opt/apps/app.exe")


if __name__ == "__main__":
    unittest.main()
