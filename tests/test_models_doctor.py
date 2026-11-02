"""fetch-models manifest / hash / skip-refetch logic and doctor JSON shape. No network."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cu_perceive import config, doctor, extras, models


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _entry(data: bytes, path="rapidocr/x.onnx", group="ocr") -> models.ModelFile:
    return models.ModelFile(path=path, group=group, size=len(data),
                            sha256=hashlib.sha256(data).hexdigest(), url="https://example.invalid/x")


class _Net:
    """Fake urlopen: serves ``body`` and counts calls."""

    def __init__(self, body: bytes):
        self.body = body
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(req.full_url)
        return _Resp(self.body)


class TestManifest(unittest.TestCase):
    def test_packaged_manifest_matches_inventory(self):
        files = {e.path: e for e in models.load_manifest()}
        self.assertEqual(set(files), {
            "screenparser/best.pt",
            "rapidocr/ch_PP-OCRv4_det_infer.onnx",
            "rapidocr/ch_PP-OCRv4_rec_infer.onnx",
            "rapidocr/ch_ppocr_mobile_v2.0_cls_infer.onnx",
        })
        best = files["screenparser/best.pt"]
        self.assertEqual(best.size, 153259543)
        self.assertEqual(best.sha256, "dbcb4f583ccfdb8100a68e606525c247890a2de4c1a54b14741e0ee29ce0ab88")
        self.assertEqual(best.group, "yolo")
        self.assertIn("docling-project/ScreenParser", best.url)
        self.assertEqual(files["rapidocr/ch_ppocr_mobile_v2.0_cls_infer.onnx"].size, 585532)
        for e in files.values():
            self.assertTrue(e.url.startswith("https://huggingface.co/"), e.url)

    def test_manifest_is_package_data(self):
        try:
            import tomllib
        except ImportError:  # pragma: no cover
            self.skipTest("tomllib needs Python 3.11+")
        data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
        self.assertIn("models_manifest.json", data["tool"]["setuptools"]["package-data"]["cu_perceive"])

    def test_bad_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.json"
            for bad in ({"sha256": "abc"}, {"group": "gpu"}, {"url": "http://x"}, {"size": 0}):
                row = {"path": "a", "group": "ocr", "size": 1, "sha256": "0" * 64, "url": "https://x"}
                row.update(bad)
                p.write_text(json.dumps({"files": [row]}))
                with self.assertRaises(ValueError, msg=bad):
                    models.load_manifest(p)


class TestCheckAndFetch(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.data = b"model-bytes" * 100
        self.entry = _entry(self.data)
        self.dest = self.root / "rapidocr" / "x.onnx"

    def _fetch(self, body, **kw):
        net = _Net(body)
        with mock.patch.object(models, "_urlopen", net):
            return models.fetch_file(self.entry, self.root, **kw), net

    def test_check_statuses(self):
        self.assertEqual(models.check_file(self.entry, self.root)["status"], "missing")
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(self.data[:-1])
        self.assertEqual(models.check_file(self.entry, self.root)["status"], "size_mismatch")
        self.dest.write_bytes(b"X" + self.data[1:])
        self.assertEqual(models.check_file(self.entry, self.root)["status"], "hash_mismatch")
        self.dest.write_bytes(self.data)
        self.assertEqual(models.check_file(self.entry, self.root)["status"], "ok")

    def test_download_when_missing(self):
        res, net = self._fetch(self.data)
        self.assertEqual((res["action"], res["status"]), ("downloaded", "ok"))
        self.assertEqual(self.dest.read_bytes(), self.data)
        self.assertEqual(len(net.calls), 1)
        self.assertFalse(self.dest.with_name("x.onnx.part").exists())

    def test_skip_valid_file(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(self.data)
        res, net = self._fetch(b"never served")
        self.assertEqual(res["action"], "skipped")
        self.assertEqual(net.calls, [])

    def test_force_refetches_valid_file(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(self.data)
        res, net = self._fetch(self.data, force=True)
        self.assertEqual(res["action"], "refetched")
        self.assertEqual(len(net.calls), 1)

    def test_refetch_corrupt_file(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
        res, _ = self._fetch(self.data)
        self.assertEqual((res["action"], res["status"]), ("refetched", "ok"))
        self.assertEqual(self.dest.read_bytes(), self.data)

    def test_bad_hash_download_keeps_old_file_and_no_part(self):
        self.dest.parent.mkdir(parents=True)
        self.dest.write_bytes(b"old")
        res, _ = self._fetch(b"Y" * len(self.data))
        self.assertEqual(res["action"], "failed")
        self.assertIn("sha256", res["error"])
        self.assertEqual(self.dest.read_bytes(), b"old")
        self.assertFalse(self.dest.with_name("x.onnx.part").exists())

    def test_wrong_size_download_fails(self):
        for body in (self.data[:-5], self.data + b"extra"):
            res, _ = self._fetch(body)
            self.assertEqual(res["action"], "failed")
            self.assertFalse(self.dest.exists())
            self.assertFalse(self.dest.with_name("x.onnx.part").exists())

    def test_network_error_is_clear(self):
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.URLError("no route")

        with mock.patch.object(models, "_urlopen", boom):
            res = models.fetch_file(self.entry, self.root)
        self.assertEqual(res["action"], "failed")
        self.assertIn("https://example.invalid/x", res["error"])
        self.assertIn(str(self.dest), res["error"])

    def test_fetch_models_groups_and_check_only(self):
        entries = [self.entry, _entry(b"yolo!", path="screenparser/best.pt", group="yolo")]
        net = _Net(self.data)
        with mock.patch.object(models, "load_manifest", return_value=entries), \
                mock.patch.object(models, "_urlopen", net):
            checked = models.fetch_models(check_only=True, root=self.root)
            self.assertFalse(checked["ok"])
            self.assertEqual(net.calls, [])
            out = models.fetch_models(["ocr"], root=self.root)
        self.assertTrue(out["ok"])
        self.assertEqual([f["path"] for f in out["files"]], ["rapidocr/x.onnx"])
        with self.assertRaises(ValueError):
            models.fetch_models(["gpu"], root=self.root)

    def test_default_root_respects_env(self):
        with mock.patch.dict(os.environ, {config.ENV_WEIGHTS: str(self.root)}):
            self.assertEqual(self.entry.dest(), self.dest)

    def test_cli_fetch_models_check(self):
        from cu_perceive.cli import main

        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {config.ENV_WEIGHTS: str(self.root)}), \
                mock.patch.object(models, "_urlopen", _Net(b"")) as net, \
                redirect_stdout(buf), redirect_stderr(err):
            code = main(["fetch-models", "--check"])
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["weights_dir"], str(self.root))
        self.assertEqual({f["status"] for f in payload["files"]}, {"missing"})
        self.assertEqual(net.calls, [])


class TestAutoFetch(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def _env(self, **extra):
        env = {config.ENV_WEIGHTS: str(self.root), models.ENV_NO_AUTO_FETCH: ""}
        env.update(extra)
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        if config.ENV_YOLO_WEIGHT not in extra:
            os.environ.pop(config.ENV_YOLO_WEIGHT, None)

    def test_missing_weight_is_fetched_with_log_line(self):
        self._env()
        logs = []
        with mock.patch.object(models, "fetch_file", return_value={"action": "downloaded"}) as ff:
            note = models.ensure_yolo_weight(log=logs.append)
        ff.assert_called_once()
        self.assertIn("downloaded", note)
        self.assertTrue(any(models.ENV_NO_AUTO_FETCH in m for m in logs), logs)

    def test_opt_out(self):
        self._env(**{models.ENV_NO_AUTO_FETCH: "1"})
        with mock.patch.object(models, "fetch_file") as ff:
            self.assertIsNone(models.ensure_yolo_weight(log=None))
        ff.assert_not_called()

    def test_custom_weight_path_untouched(self):
        self._env(**{config.ENV_YOLO_WEIGHT: str(self.root / "mine.pt")})
        with mock.patch.object(models, "fetch_file") as ff:
            self.assertIsNone(models.ensure_yolo_weight(log=None))
        ff.assert_not_called()

    def test_present_weight_not_refetched(self):
        self._env()
        w = self.root / "screenparser" / "best.pt"
        w.parent.mkdir(parents=True)
        w.write_bytes(b"\0" * 2_000_000)
        with mock.patch.object(models, "fetch_file") as ff:
            self.assertIsNone(models.ensure_yolo_weight(log=None))
        ff.assert_not_called()

    def test_failure_raises_fetch_error(self):
        self._env()
        with mock.patch.object(models, "fetch_file", return_value={"action": "failed", "error": "offline"}):
            with self.assertRaises(models.FetchError):
                models.ensure_yolo_weight(log=None)


class TestDoctor(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.data = b"w" * 64
        self.entries = [_entry(self.data)]
        patches = [
            mock.patch.dict(os.environ, {config.ENV_WEIGHTS: str(self.root)}),
            mock.patch.object(models, "load_manifest", return_value=self.entries),
            mock.patch.object(extras, "missing_modules", return_value=[]),
            mock.patch.object(doctor, "_torch_info", return_value={
                "installed": True, "version": "2.13.0+cpu", "build": "cpu",
                "cuda_version": None, "cuda_available": False}),
            mock.patch.object(doctor, "_ocr_smoke", return_value={"ok": True, "text": "HELLO 2026", "ms": 5}),
            mock.patch.object(doctor, "_yolo_smoke", return_value={"ok": True, "boxes": 0, "ms": 7}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop(config.ENV_YOLO_WEIGHT, None)

    def _write_weights(self):
        dest = self.entries[0].dest(self.root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.data)
        yw = self.root / "screenparser" / "best.pt"
        yw.parent.mkdir(parents=True, exist_ok=True)
        yw.write_bytes(b"pt")

    def test_json_shape_ok(self):
        self._write_weights()
        from cu_perceive.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["doctor", "--json"])
        self.assertEqual(code, 0, buf.getvalue())
        rep = json.loads(buf.getvalue())
        for key in ("ok", "python", "platform", "cu_perceive", "extras", "install_full", "torch",
                    "weights", "smoke", "failures"):
            self.assertIn(key, rep)
        self.assertTrue(rep["ok"])
        self.assertEqual(set(rep["extras"]), {"ocr", "uia", "yolo"})
        self.assertEqual(rep["torch"]["build"], "cpu")
        self.assertEqual(rep["weights"]["dir"], str(self.root))
        self.assertEqual(rep["weights"]["files"][0]["status"], "ok")
        self.assertEqual(set(rep["smoke"]), {"ocr", "yolo"})
        self.assertIn("pip install", rep["install_full"])

    def test_failures_exit_nonzero(self):
        from cu_perceive.cli import main

        def missing(extra):
            return ["ultralytics"] if extra == "yolo" else []

        buf = io.StringIO()
        with mock.patch.object(extras, "missing_modules", side_effect=missing), redirect_stdout(buf):
            code = main(["doctor", "--json"])
        self.assertEqual(code, 1)
        rep = json.loads(buf.getvalue())
        self.assertFalse(rep["ok"])
        self.assertFalse(rep["extras"]["yolo"]["installed"])
        self.assertIn("pip install", rep["extras"]["yolo"]["install"])
        self.assertEqual(rep["weights"]["files"][0]["status"], "missing")
        self.assertIn("fetch-models", rep["weights"]["fix"])
        self.assertTrue(rep["smoke"]["yolo"]["skipped"])
        self.assertTrue(any("yolo" in f for f in rep["failures"]))

    def test_smoke_exception_reported(self):
        self._write_weights()
        with mock.patch.object(doctor, "_ocr_smoke", side_effect=RuntimeError("boom")):
            rep = doctor.run()
        self.assertFalse(rep["ok"])
        self.assertIn("boom", rep["smoke"]["ocr"]["error"])

    def test_text_output_and_no_smoke(self):
        self._write_weights()
        from cu_perceive.cli import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["doctor", "--no-smoke"])
        self.assertEqual(code, 0)
        self.assertIn("doctor: ok", buf.getvalue())
        doctor._ocr_smoke.assert_not_called()

    def test_smoke_image_renders_text(self):
        img = doctor._smoke_image()
        self.assertEqual(img.ndim, 3)
        self.assertLess(int(img.min()), 128)  # some dark text pixels on white


class TestFullExtra(unittest.TestCase):
    def test_full_covers_every_extra(self):
        full = set(extras.PACKAGES["full"])
        for extra in ("ocr", "uia", "yolo"):
            self.assertLessEqual(set(extras.PACKAGES[extra]), full, extra)
        self.assertTrue(any(p.startswith("torch>=") for p in full))

    def test_requirements_full_pins(self):
        req = (Path(__file__).resolve().parents[1] / "requirements-full.txt").read_text()
        for pin in ("rapidocr-onnxruntime==1.4.4", "onnxruntime==1.24.4", "ultralytics==8.4.129",
                    "torch==2.13.0+cpu", "torchvision==0.28.0", "uiautomation==2.0.29",
                    "comtypes==1.4.16", "mcp==1.29.1", "windows-capture==2.0.1",
                    "--extra-index-url https://download.pytorch.org/whl/cpu"):
            self.assertIn(pin, req)


if __name__ == "__main__":
    unittest.main()
