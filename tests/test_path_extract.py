"""path_extract tests — runnable via pytest if installed, else: python tests/test_path_extract.py"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cu_perceive.act import norm_to_roi_window, path_to_drag_specs, plan_steps
from cu_perceive.path_extract import PathExtractError, extract_paths, parse_roi


def _synth(tmp: Path) -> Path:
    p = tmp / "circle.png"
    img = np.full((400, 400, 3), 255, dtype=np.uint8)
    cv2.circle(img, (200, 200), 120, (0, 0, 0), -1)
    cv2.imwrite(str(p), img)
    return p


def _synth_padded(tmp: Path, *, pad_l=100, pad_t=80, size=400, radius=120) -> tuple[Path, list[int]]:
    """Circle centered in a size×size white canvas, then padded → circle only in subrect."""
    p = tmp / "circle_padded.png"
    canvas_w = size + pad_l + 50
    canvas_h = size + pad_t + 60
    img = np.full((canvas_h, canvas_w, 3), 255, dtype=np.uint8)
    # dark chrome-ish border outside ROI (should be ignored when ROI crops)
    img[0:pad_t, :] = (40, 40, 40)
    img[:, 0:pad_l] = (40, 40, 40)
    cx = pad_l + size // 2
    cy = pad_t + size // 2
    cv2.circle(img, (cx, cy), radius, (0, 0, 0), -1)
    cv2.imwrite(str(p), img)
    roi = [pad_l, pad_t, size, size]
    return p, roi


def test_synth_circle_largest_only(tmp_path=None):
    tmp = Path(tmp_path) if tmp_path is not None else Path(__file__).resolve().parent / "_tmp_synth"
    tmp.mkdir(parents=True, exist_ok=True)
    png = _synth(tmp)
    data = extract_paths(png, invert=True, largest_only=True, min_points=8, max_points=400)
    assert data["version"] == 1
    assert data["experimental"] is True
    assert len(data["paths"]) == 1
    pts = data["paths"][0]["points"]
    assert 8 <= len(pts) <= 400
    assert data["paths"][0]["closed"] is True
    assert data["stats"].get("method") == "approx_poly"
    assert "roi" not in data["source"]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cx, cy = sum(xs) / len(pts), sum(ys) / len(pts)
    assert abs(cx - 500) < 80 and abs(cy - 500) < 80
    radii = [math.hypot(x - cx, y - cy) for x, y in pts]
    mean_r = sum(radii) / len(radii)
    assert 200 < mean_r < 400


def test_arc_resample_spacing_and_overlay(tmp_path=None):
    tmp = Path(tmp_path) if tmp_path is not None else Path(__file__).resolve().parent / "_tmp_synth"
    tmp.mkdir(parents=True, exist_ok=True)
    png = _synth(tmp)
    overlay = tmp / "circle.overlay.png"
    if overlay.exists():
        overlay.unlink()
    data = extract_paths(
        png,
        invert=True,
        simplify=0.0,
        spacing_px=4.0,
        min_points=8,
        max_points=400,
        overlay=overlay,
    )
    assert data["stats"]["method"] == "arc_resample"
    assert data["stats"]["spacing_px"] == 4.0
    n = data["stats"]["n_points"]
    # 2*pi*120/4 ≈ 188.5
    assert 120 <= n <= 280, n
    assert overlay.is_file() and overlay.stat().st_size > 0


def test_simplify_frac_wins_over_absolute(tmp_path=None):
    tmp = Path(tmp_path) if tmp_path is not None else Path(__file__).resolve().parent / "_tmp_synth"
    tmp.mkdir(parents=True, exist_ok=True)
    png = _synth(tmp)
    # Huge absolute simplify would collapse; frac=0.002 keeps a usable polygon.
    data = extract_paths(png, invert=True, simplify=500.0, simplify_frac=0.002)
    assert data["stats"]["method"] == "approx_poly"
    assert 8 <= data["stats"]["n_points"] <= 400


def test_unreadable_fails():
    try:
        extract_paths("/nonexistent/nope_cu_perceive_path.png")
        assert False, "expected PathExtractError"
    except PathExtractError:
        pass


def test_blank_fails(tmp_path=None):
    tmp = Path(tmp_path) if tmp_path is not None else Path(__file__).resolve().parent / "_tmp_synth"
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / "blank.png"
    cv2.imwrite(str(p), np.full((100, 100, 3), 255, dtype=np.uint8))
    try:
        extract_paths(p, invert=True)
        assert False, "expected PathExtractError"
    except PathExtractError:
        pass


def test_overlay_not_written_on_fail(tmp_path=None):
    tmp = Path(tmp_path) if tmp_path is not None else Path(__file__).resolve().parent / "_tmp_synth"
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / "blank2.png"
    overlay = tmp / "should_not_exist.overlay.png"
    if overlay.exists():
        overlay.unlink()
    cv2.imwrite(str(p), np.full((100, 100, 3), 255, dtype=np.uint8))
    try:
        extract_paths(p, invert=True, overlay=overlay)
        assert False, "expected PathExtractError"
    except PathExtractError:
        pass
    assert not overlay.exists()


def test_parse_roi_fail_closed():
    assert parse_roi("10,20,100,80", 200, 200) == [10, 20, 100, 80]
    for bad in ("1,2,3", "0,0,0,10", "-1,0,10,10", "150,0,100,10", None, "a,b,c,d"):
        try:
            parse_roi(bad, 200, 200)
            assert False, f"expected fail for {bad!r}"
        except (PathExtractError, TypeError):
            pass


def test_roi_padded_circle_centroid(tmp_path=None):
    """Circle in padded canvas: with --roi, norm centroid ~500,500 relative to ROI."""
    tmp = Path(tmp_path) if tmp_path is not None else Path(__file__).resolve().parent / "_tmp_synth"
    tmp.mkdir(parents=True, exist_ok=True)
    png, roi = _synth_padded(tmp)
    overlay = tmp / "padded.roi.overlay.png"
    if overlay.exists():
        overlay.unlink()
    data = extract_paths(
        png,
        invert=True,
        roi=roi,
        simplify=0.0,
        spacing_px=4.0,
        overlay=overlay,
    )
    assert data["source"]["roi"] == roi
    assert data["source"]["width"] > roi[2]
    assert data["source"]["height"] > roi[3]
    pts = data["paths"][0]["points"]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cx, cy = sum(xs) / len(pts), sum(ys) / len(pts)
    assert abs(cx - 500) < 80, cx
    assert abs(cy - 500) < 80, cy
    assert overlay.is_file()

    # Without ROI on same padded image, chrome + circle → centroid drifts / may pick chrome
    # (we only assert ROI path is correct here).


def test_norm_to_roi_window_mapping():
    # center of ROI [100,80,400,400] → window (300, 280)
    assert norm_to_roi_window(500, 500, [100, 80, 400, 400]) == (300, 280)
    # top-left
    assert norm_to_roi_window(0, 0, [100, 80, 400, 400]) == (100, 80)
    # bottom-right
    assert norm_to_roi_window(1000, 1000, [100, 80, 400, 400]) == (500, 480)


def test_act_path_roi_shifts_screen_points(tmp_path=None):
    """path.json with source.roi → dry-run window/screen points include ROI offset."""
    tmp = Path(tmp_path) if tmp_path is not None else Path(__file__).resolve().parent / "_tmp_synth"
    tmp.mkdir(parents=True, exist_ok=True)
    png, roi = _synth_padded(tmp)
    data = extract_paths(png, invert=True, roi=roi, simplify=1.5)
    specs = path_to_drag_specs(data, path_id=0, step_px=2)
    assert specs[0].get("roi") == roi

    # Fake stamp: origin (10, 20), size matches padded image
    frame = {
        "stamp": "fake",
        "coord_space": "window",
        "origin": {"left": 10, "top": 20, "width": 800, "height": 600},
        "width": data["source"]["width"],
        "height": data["source"]["height"],
    }
    steps = plan_steps(frame, specs)
    assert steps[0]["via"] == "drag"
    assert steps[0]["roi"] == roi
    # Centroid of window points should be near ROI center
    wpts = steps[0]["points"]
    cx = sum(p[0] for p in wpts) / len(wpts)
    cy = sum(p[1] for p in wpts) / len(wpts)
    expect_cx = roi[0] + roi[2] / 2
    expect_cy = roi[1] + roi[3] / 2
    assert abs(cx - expect_cx) < 40, (cx, expect_cx)
    assert abs(cy - expect_cy) < 40, (cy, expect_cy)
    # Screen = window + origin
    s0 = steps[0]["screen_points"][0]
    w0 = steps[0]["points"][0]
    assert s0 == [w0[0] + 10, w0[1] + 20]

    # Without roi on a copy: norm maps to full frame → different window pts
    data_no = dict(data)
    data_no["source"] = dict(data["source"])
    data_no["source"].pop("roi", None)
    specs_no = path_to_drag_specs(data_no, path_id=0, step_px=2)
    assert "roi" not in specs_no[0]
    steps_no = plan_steps(frame, specs_no)
    # Same norm center 500,500 without ROI → half of full width (not ROI center)
    # Points differ because mapping domain differs.
    assert steps_no[0]["points"] != steps[0]["points"]


def test_roi_out_of_bounds_fail(tmp_path=None):
    tmp = Path(tmp_path) if tmp_path is not None else Path(__file__).resolve().parent / "_tmp_synth"
    tmp.mkdir(parents=True, exist_ok=True)
    png = _synth(tmp)
    try:
        extract_paths(png, invert=True, roi="0,0,500,400")
        assert False, "expected PathExtractError"
    except PathExtractError:
        pass


if __name__ == "__main__":
    # pytest-less runner
    base = Path(__file__).resolve().parent / "_tmp_synth"
    test_synth_circle_largest_only(base)
    test_arc_resample_spacing_and_overlay(base)
    test_simplify_frac_wins_over_absolute(base)
    test_unreadable_fails()
    test_blank_fails(base)
    test_overlay_not_written_on_fail(base)
    test_parse_roi_fail_closed()
    test_roi_padded_circle_centroid(base)
    test_norm_to_roi_window_mapping()
    test_act_path_roi_shifts_screen_points(base)
    test_roi_out_of_bounds_fail(base)
    print("ok")
