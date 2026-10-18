"""Lazy extras: core works without OCR/UIA/YOLO; missing extra -> exact command, no install."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from cu_perceive import extras
from cu_perceive.extras import MissingExtraError, install_command

ALL_EXTRA_MODULES = [m for mods in extras.MODULES.values() for m in mods]


def _block(*mods):
    """Make imports of mods fail (sys.modules[name] = None)."""
    return mock.patch.dict(sys.modules, {m: None for m in mods})


class _FakeDist:
    def __init__(self, version="0.2.0", direct_url=None):
        self.version = version
        self._direct = direct_url

    def read_text(self, name):
        if name == "direct_url.json" and self._direct is not None:
            return json.dumps(self._direct)
        return None


class TestInstallCommand(unittest.TestCase):
    def _cmd(self, dist, extra="ocr"):
        import importlib.metadata as md

        with mock.patch("importlib.metadata.distribution",
                        side_effect=(md.PackageNotFoundError() if dist is None else None),
                        return_value=dist), \
                mock.patch.object(sys, "executable", "/opt/py/bin/python"):
            return install_command(extra)

    def test_bare_checkout_names_packages(self):
        cmd = self._cmd(None, "uia")
        self.assertEqual(cmd, '/opt/py/bin/python -m pip install "uiautomation>=2.0.29" "comtypes>=1.4"')

    def test_editable_checkout(self):
        dist = _FakeDist(direct_url={"url": "file:///home/u/cu-perceive", "dir_info": {"editable": True}})
        self.assertEqual(self._cmd(dist), '/opt/py/bin/python -m pip install -e "/home/u/cu-perceive[ocr]"')

    def test_git_install_pins_commit(self):
        dist = _FakeDist(direct_url={
            "url": "https://github.com/o/cu-perceive.git",
            "vcs_info": {"vcs": "git", "commit_id": "abc123"},
        })
        self.assertEqual(
            self._cmd(dist, "yolo"),
            '/opt/py/bin/python -m pip install "cu-perceive[yolo] @ git+https://github.com/o/cu-perceive.git@abc123"',
        )

    def test_index_install_pins_version(self):
        self.assertEqual(self._cmd(_FakeDist("0.2.0")), '/opt/py/bin/python -m pip install "cu-perceive[ocr]==0.2.0"')

    def test_interpreter_path_with_spaces_quoted(self):
        import importlib.metadata as md

        with mock.patch("importlib.metadata.distribution", side_effect=md.PackageNotFoundError()), \
                mock.patch.object(sys, "executable", r"C:\Program Files\Python312\python.exe"):
            self.assertTrue(install_command("ocr").startswith(r'"C:\Program Files\Python312\python.exe" -m pip'))

    def test_unknown_extra(self):
        with self.assertRaises(KeyError):
            install_command("gpu")

    def test_packages_match_pyproject(self):
        try:
            import tomllib
        except ImportError:  # pragma: no cover
            self.skipTest("tomllib needs Python 3.11+")
        data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
        opt = data["project"]["optional-dependencies"]
        for extra, pkgs in extras.PACKAGES.items():
            self.assertEqual(list(pkgs), opt[extra], extra)


class TestRequire(unittest.TestCase):
    def test_missing_raises_with_command(self):
        with _block("rapidocr_onnxruntime", "onnxruntime"):
            with self.assertRaises(MissingExtraError) as cm:
                extras.require("ocr")
        e = cm.exception
        self.assertEqual(e.extra, "ocr")
        self.assertIn("rapidocr_onnxruntime", e.missing)
        self.assertIn("pip install", e.command)
        self.assertIn(e.command, str(e))
        d = e.to_dict()
        self.assertFalse(d["ok"])
        self.assertEqual(d["install"], e.command)
        self.assertEqual(d["missing_extra"], "ocr")

    def test_never_runs_pip(self):
        with _block(*ALL_EXTRA_MODULES), \
                mock.patch.object(subprocess, "run") as run, \
                mock.patch.object(subprocess, "Popen") as popen, \
                mock.patch.object(subprocess, "check_call") as cc:
            for extra in extras.MODULES:
                with self.assertRaises(MissingExtraError):
                    extras.require(extra)
        run.assert_not_called()
        popen.assert_not_called()
        cc.assert_not_called()

    def test_present_module_passes(self):
        with mock.patch.object(extras, "MODULES", {"ocr": ("json",)}):
            extras.require("ocr")
            self.assertTrue(extras.available("ocr"))


class TestCoreWithoutExtras(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.dir = Path(self.td.name)
        self.src = self.dir / "in.png"
        cv2.imwrite(str(self.src), np.full((120, 200, 3), 128, np.uint8))

    def test_modules_import_without_extras(self):
        # Fresh interpreter: reloading in-process would swap exception classes under other tests.
        blocked = list(ALL_EXTRA_MODULES) + ["mcp", "uvicorn", "win32gui", "win32process", "win32api",
                                             "psutil", "windows_capture"]
        mods = ["cu_perceive.core", "cu_perceive.act", "cu_perceive.cli", "cu_perceive.windows",
                "cu_perceive.capture", "cu_perceive._win32", "cu_perceive.uia", "cu_perceive.yolo",
                "cu_perceive.mcp_server", "cu_perceive.launch"]
        code = (
            "import sys, importlib\n"
            f"for m in {blocked!r}: sys.modules[m] = None\n"
            f"for m in {mods!r}: importlib.import_module(m)\n"
        )
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_perceive_image_core_only(self):
        with _block(*ALL_EXTRA_MODULES):
            from cu_perceive.core import perceive

            out = perceive(image=str(self.src), out_dir=str(self.dir / "out"))
        self.assertEqual((out["width"], out["height"]), (200, 120))
        self.assertTrue(Path(out["map"]).is_file())

    def test_ocr_missing_fails_before_writing(self):
        from cu_perceive.core import perceive

        with _block("rapidocr_onnxruntime", "onnxruntime"):
            with self.assertRaises(MissingExtraError) as cm:
                perceive(image=str(self.src), out_dir=str(self.dir / "out"), ocr=True)
        self.assertEqual(cm.exception.extra, "ocr")
        self.assertFalse((self.dir / "out").exists())

    def test_yolo_and_uia_missing(self):
        from cu_perceive.core import perceive

        for extra, kw in (("yolo", {"yolo": True}), ("uia", {"uia": True})):
            with _block(*extras.MODULES[extra]):
                with self.assertRaises(MissingExtraError) as cm:
                    perceive(image=str(self.src), out_dir=str(self.dir / "out"), **kw)
            self.assertEqual(cm.exception.extra, extra)

    def test_cli_prints_install_command(self):
        from cu_perceive.cli import main

        buf, err = io.StringIO(), io.StringIO()
        with _block("rapidocr_onnxruntime", "onnxruntime"), redirect_stdout(buf), redirect_stderr(err):
            code = main(["perceive", "--image", str(self.src), "--out", str(self.dir / "o"), "--ocr"])
        self.assertEqual(code, 3)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["missing_extra"], "ocr")
        self.assertIn("pip install", payload["install"])
        self.assertIn(payload["install"], err.getvalue())

    def test_uia_missing_in_act_path_is_fail_closed_uia_error(self):
        from cu_perceive.uia import UiaError, collect_uia_nodes, element_from_screen_point

        with _block("uiautomation", "comtypes"):
            with self.assertRaises(UiaError) as cm:
                element_from_screen_point(1, 1)
            self.assertIn("pip install", str(cm.exception))
            self.assertIn("pip install", cm.exception.install)
            res = collect_uia_nodes(5, width=10, height=10, origin_left=0, origin_top=0)
        self.assertFalse(res["ok"])
        self.assertIn("pip install", res["error"])

    def test_yolo_load_missing(self):
        from cu_perceive import yolo

        with _block("ultralytics", "torch"), mock.patch.object(yolo, "_model", None):
            with self.assertRaises(MissingExtraError):
                yolo._load()

    def test_ocr_parser_missing(self):
        from cu_perceive import core

        with _block("rapidocr_onnxruntime", "onnxruntime"), mock.patch.object(core, "_PARSER", None):
            with self.assertRaises(MissingExtraError):
                core._parser()


if __name__ == "__main__":
    unittest.main()
