"""E2 ROI light-verify perceive: crop + verify_light (no real window)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cu_perceive.core import (
    clamp_roi,
    perceive,
    resolve_perceive_roi,
    roi_norm_to_pixel,
)


def _synth(path: Path, w: int = 800, h: int = 600) -> Path:
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:] = (40, 40, 40)
    img[200:400, 300:500] = (0, 220, 80)
    cv2.imwrite(str(path), img)
    return path


class TestRoiHelpers(unittest.TestCase):
    def test_clamp_roi_inside(self):
        self.assertEqual(clamp_roi([10, 20, 100, 50], 800, 600), [10, 20, 100, 50])

    def test_clamp_roi_clips(self):
        self.assertEqual(clamp_roi([-10, -5, 50, 40], 800, 600), [0, 0, 40, 35])

    def test_clamp_roi_empty_errors(self):
        with self.assertRaises(ValueError):
            clamp_roi([10, 20, 0, 50], 800, 600)
        with self.assertRaises(ValueError):
            clamp_roi([900, 10, 50, 50], 800, 600)

    def test_roi_norm_default_half(self):
        px, rn = roi_norm_to_pixel([500, 500], 1000, 1000)
        self.assertEqual(rn, [500, 500, 150])
        self.assertEqual(px, [350, 350, 300, 300])

    def test_roi_norm_clamps_edge(self):
        px, rn = roi_norm_to_pixel("50,50,200", 1000, 1000)
        self.assertEqual(rn, [50, 50, 200])
        self.assertEqual(px[0], 0)
        self.assertEqual(px[1], 0)
        self.assertGreater(px[2], 0)
        self.assertGreater(px[3], 0)

    def test_mutual_exclusive(self):
        with self.assertRaises(ValueError) as cm:
            resolve_perceive_roi([0, 0, 10, 10], [500, 500], full_w=100, full_h=100)
        self.assertIn("mutually exclusive", str(cm.exception))


class TestRoiPerceive(unittest.TestCase):
    def test_perceive_roi_crop(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            src = _synth(td_path / "full.png", 800, 600)
            out = perceive(image=str(src), out_dir=str(td_path / "out"), roi=[100, 80, 200, 150])
            self.assertTrue(out.get("verify_light"))
            self.assertEqual(out["roi"], [100, 80, 200, 150])
            self.assertEqual(out["full_width"], 800)
            self.assertEqual(out["full_height"], 600)
            self.assertEqual(out["width"], 200)
            self.assertEqual(out["height"], 150)
            self.assertIsNotNone(out.get("roi_norm"))
            self.assertEqual(len(out["roi_norm"]), 3)
            map_p = Path(out["map"])
            png_p = Path(out["png"])
            self.assertTrue(map_p.exists())
            self.assertTrue(png_p.exists())
            crop = cv2.imread(str(png_p))
            self.assertEqual(crop.shape[1], 200)
            self.assertEqual(crop.shape[0], 150)
            full_png = td_path / "full_copy.png"
            full_grid = td_path / "full_grid.png"
            full_img = cv2.imread(str(src))
            from cu_perceive.core import draw_grid
            cv2.imwrite(str(full_png), full_img)
            cv2.imwrite(str(full_grid), draw_grid(full_img))
            self.assertLess(map_p.stat().st_size, full_grid.stat().st_size)
            self.assertLess(png_p.stat().st_size, full_png.stat().st_size)
            data = json.loads(Path(out["json"]).read_text(encoding="utf-8"))
            self.assertTrue(data["verify_light"])
            self.assertEqual(data["width"], 200)

    def test_perceive_roi_norm(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            src = _synth(td_path / "full.png", 1000, 800)
            out = perceive(image=str(src), out_dir=str(td_path / "out"), roi_norm=[500, 400, 100])
            self.assertTrue(out["verify_light"])
            self.assertEqual(out["full_width"], 1000)
            self.assertEqual(out["full_height"], 800)
            self.assertEqual(out["roi_norm"], [500, 400, 100])
            self.assertEqual(out["width"], 200)
            self.assertEqual(out["height"], 160)
            self.assertEqual(out["roi"], [400, 240, 200, 160])

    def test_invalid_roi_errors(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            src = _synth(td_path / "full.png")
            with self.assertRaises(ValueError):
                perceive(image=str(src), out_dir=str(td_path / "out"), roi="bad")
            with self.assertRaises(ValueError):
                perceive(image=str(src), out_dir=str(td_path / "out"), roi=[0, 0, 0, 10])
            with self.assertRaises(ValueError):
                perceive(image=str(src), out_dir=str(td_path / "out"), roi_norm="1")
            with self.assertRaises(ValueError):
                perceive(
                    image=str(src),
                    out_dir=str(td_path / "out"),
                    roi=[1, 1, 10, 10],
                    roi_norm=[500, 500],
                )

    def test_default_no_roi_unchanged_keys(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            src = _synth(td_path / "full.png", 400, 300)
            out = perceive(image=str(src), out_dir=str(td_path / "out"))
            self.assertNotIn("verify_light", out)
            self.assertNotIn("roi", out)
            self.assertEqual(out["width"], 400)
            self.assertEqual(out["height"], 300)


if __name__ == "__main__":
    unittest.main()
