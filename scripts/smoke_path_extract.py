"""Self-check: synth black circle on white -> path-extract -> assert circular polyline.

Covers E1 (arc_resample/overlay) + E2 (--roi on padded canvas + act mapping).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cu_perceive.act import norm_to_roi_window, path_to_drag_specs, plan_steps  # noqa: E402
from cu_perceive.path_extract import extract_paths, path_extract_cli_result  # noqa: E402


def make_synth_circle(dest: Path, size: int = 400, radius: int = 120) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    img = np.full((size, size, 3), 255, dtype=np.uint8)
    cx = cy = size // 2
    cv2.circle(img, (cx, cy), radius, (0, 0, 0), thickness=-1)
    cv2.imwrite(str(dest), img)
    return dest


def make_padded_circle(
    dest: Path,
    *,
    pad_l: int = 100,
    pad_t: int = 80,
    size: int = 400,
    radius: int = 120,
) -> list[int]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    canvas_w = size + pad_l + 50
    canvas_h = size + pad_t + 60
    img = np.full((canvas_h, canvas_w, 3), 255, dtype=np.uint8)
    img[0:pad_t, :] = (40, 40, 40)
    img[:, 0:pad_l] = (40, 40, 40)
    cv2.circle(img, (pad_l + size // 2, pad_t + size // 2), radius, (0, 0, 0), -1)
    cv2.imwrite(str(dest), img)
    return [pad_l, pad_t, size, size]


def assert_circular(path_data: dict, *, size: int = 400) -> None:
    assert path_data.get("version") == 1
    assert path_data.get("experimental") is True
    assert path_data.get("coord_space") == "norm"
    paths = path_data["paths"]
    assert len(paths) == 1, f"expected 1 path, got {len(paths)}"
    p0 = paths[0]
    assert p0.get("closed") is True
    pts = p0["points"]
    n = len(pts)
    assert 8 <= n <= 400, f"n_points={n} out of [8,400]"
    for x, y in pts:
        assert 0 <= x <= 1000 and 0 <= y <= 1000, (x, y)

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    cx, cy = sum(xs) / n, sum(ys) / n
    # Image/ROI center in norm space ~ 500,500
    assert abs(cx - 500) < 80, f"centroid x={cx}"
    assert abs(cy - 500) < 80, f"centroid y={cy}"

    radii = [math.hypot(x - cx, y - cy) for x, y in pts]
    mean_r = sum(radii) / n
    # radius 120/400*1000 = 300 norm units
    assert 200 < mean_r < 400, f"mean radius norm={mean_r}"
    # consistency: stdev not huge
    var = sum((r - mean_r) ** 2 for r in radii) / n
    std = math.sqrt(var)
    assert std < mean_r * 0.25, f"radius std={std} mean={mean_r}"


def main() -> int:
    examples = ROOT / "docs" / "examples"
    png = examples / "synth_circle.png"
    out_json = examples / "synth_circle.path.json"
    overlay = examples / "synth_circle.overlay.png"
    make_synth_circle(png)

    # --- legacy approx_poly path (no spacing-px) ---
    summary, code = path_extract_cli_result(
        image=str(png),
        out=str(out_json),
        mode="contour",
        simplify=1.5,
        min_points=8,
        max_points=400,
        invert=True,  # white bg / black fill
        largest_only=True,
    )
    print(json.dumps({"legacy": summary}, ensure_ascii=False))
    if code != 0 or not summary.get("ok"):
        print("FAIL: path-extract (legacy) returned error", file=sys.stderr)
        return 1

    data = json.loads(out_json.read_text(encoding="utf-8"))
    try:
        assert_circular(data)
        assert data["stats"].get("method") == "approx_poly"
    except AssertionError as e:
        print(f"FAIL legacy: {e}", file=sys.stderr)
        return 1

    n_approx = data["stats"]["n_points"]

    # Also exercise module API once
    data2 = extract_paths(png, invert=True)
    assert_circular(data2)
    assert data2["stats"].get("method") == "approx_poly"

    # --- E1 arc_resample + overlay ---
    # perimeter ~ 2*pi*120 ≈ 754; spacing=4 → ~189 pts (band 120–280)
    out_arc = examples / "synth_circle.arc.path.json"
    if overlay.exists():
        overlay.unlink()
    summary_arc, code_arc = path_extract_cli_result(
        image=str(png),
        out=str(out_arc),
        mode="contour",
        simplify=0.0,  # skip DP so resample follows raw contour
        spacing_px=4.0,
        min_points=8,
        max_points=400,
        invert=True,
        largest_only=True,
        overlay=str(overlay),
    )
    print(json.dumps({"arc_resample": summary_arc}, ensure_ascii=False))
    if code_arc != 0 or not summary_arc.get("ok"):
        print("FAIL: path-extract (arc_resample) returned error", file=sys.stderr)
        return 1

    data_arc = json.loads(out_arc.read_text(encoding="utf-8"))
    try:
        assert_circular(data_arc)
        assert data_arc["stats"].get("method") == "arc_resample"
        assert data_arc["stats"].get("spacing_px") == 4.0
        n_arc = data_arc["stats"]["n_points"]
        assert 120 <= n_arc <= 280, f"arc n_points={n_arc} not in [120,280] for spacing=4"
        assert overlay.is_file() and overlay.stat().st_size > 0, "overlay missing"
    except AssertionError as e:
        print(f"FAIL arc: {e}", file=sys.stderr)
        return 1

    # relative simplify-frac smoke (still approx if no spacing)
    data_frac = extract_paths(png, invert=True, simplify=99.0, simplify_frac=0.01)
    assert data_frac["stats"]["method"] == "approx_poly"
    # frac should win over huge absolute simplify → still enough points for a circle
    assert 8 <= data_frac["stats"]["n_points"] <= 400

    # --- E2 ROI on padded canvas ---
    png_pad = examples / "synth_circle_padded.png"
    out_roi = examples / "synth_circle.roi.path.json"
    overlay_roi = examples / "synth_circle.roi.overlay.png"
    roi = make_padded_circle(png_pad)
    if overlay_roi.exists():
        overlay_roi.unlink()
    summary_roi, code_roi = path_extract_cli_result(
        image=str(png_pad),
        out=str(out_roi),
        mode="contour",
        simplify=0.0,
        spacing_px=4.0,
        invert=True,
        largest_only=True,
        overlay=str(overlay_roi),
        roi=roi,
    )
    print(json.dumps({"roi": summary_roi}, ensure_ascii=False))
    if code_roi != 0 or not summary_roi.get("ok"):
        print("FAIL: path-extract (--roi) returned error", file=sys.stderr)
        return 1
    data_roi = json.loads(out_roi.read_text(encoding="utf-8"))
    try:
        assert data_roi["source"]["roi"] == roi
        assert_circular(data_roi)
        assert overlay_roi.is_file()
        assert norm_to_roi_window(500, 500, roi) == (
            roi[0] + roi[2] // 2,
            roi[1] + roi[3] // 2,
        )
        specs = path_to_drag_specs(data_roi, step_px=2)
        assert specs[0]["roi"] == roi
        frame = {
            "coord_space": "window",
            "origin": {"left": 0, "top": 0, "width": 800, "height": 600},
            "width": data_roi["source"]["width"],
            "height": data_roi["source"]["height"],
        }
        steps = plan_steps(frame, specs)
        wpts = steps[0]["points"]
        cx = sum(p[0] for p in wpts) / len(wpts)
        cy = sum(p[1] for p in wpts) / len(wpts)
        assert abs(cx - (roi[0] + roi[2] / 2)) < 40
        assert abs(cy - (roi[1] + roi[3] / 2)) < 40

        # with vs without roi screen delta (same path points, fake stamp origin)
        frame2 = {
            "coord_space": "window",
            "origin": {"left": 50, "top": 60, "width": 800, "height": 600},
            "width": data_roi["source"]["width"],
            "height": data_roi["source"]["height"],
        }
        steps_roi = plan_steps(frame2, specs)
        data_no = dict(data_roi)
        data_no["source"] = dict(data_roi["source"])
        del data_no["source"]["roi"]
        specs_no = path_to_drag_specs(data_no, step_px=2)
        steps_no = plan_steps(frame2, specs_no)
        sp_roi = steps_roi[0]["screen_points"][0]
        sp_no = steps_no[0]["screen_points"][0]
        delta = [sp_roi[0] - sp_no[0], sp_roi[1] - sp_no[1]]
    except AssertionError as e:
        print(f"FAIL roi: {e}", file=sys.stderr)
        return 1

    pts = data["paths"][0]["points"]
    print(
        json.dumps(
            {
                "smoke": "pass",
                "n_points_approx": n_approx,
                "n_points_arc": n_arc,
                "n_points_roi": data_roi["stats"]["n_points"],
                "method_approx": "approx_poly",
                "method_arc": "arc_resample",
                "spacing_px": 4.0,
                "source_roi": data_roi["source"]["roi"],
                "screen_points_first_with_roi": sp_roi,
                "screen_points_first_without_roi": sp_no,
                "screen_points_delta_roi_minus_no": delta,
                "first3_approx": pts[:3],
                "last3_approx": pts[-3:],
                "png": str(png),
                "path_json": str(out_json),
                "arc_path_json": str(out_arc),
                "roi_path_json": str(out_roi),
                "overlay": str(overlay),
                "overlay_roi": str(overlay_roi),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
