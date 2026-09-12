"""Experimental contour → 0–1000 polyline extract (OpenCV only). No OCR/YOLO."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np


class PathExtractError(Exception):
    """Fail-closed extract error (unreadable / no contour / too few points)."""


def parse_roi(roi: Any, img_w: int, img_h: int) -> list[int]:
    """Validate ROI as [L, T, W, H] in image pixel coords (top-left = 0,0).

    Fail-closed if missing parts, non-int, empty, or out of bounds.
    """
    if roi is None:
        raise PathExtractError("roi is None")
    if isinstance(roi, str):
        parts = [p.strip() for p in roi.replace(" ", ",").split(",") if p.strip() != ""]
        if len(parts) != 4:
            raise PathExtractError(f"roi must be L,T,W,H (4 ints), got {roi!r}")
        try:
            L, T, W, H = (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
        except ValueError as e:
            raise PathExtractError(f"roi must be integers L,T,W,H: {roi!r}") from e
    elif isinstance(roi, (list, tuple)) and len(roi) == 4:
        try:
            L, T, W, H = (int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3]))
        except (TypeError, ValueError) as e:
            raise PathExtractError(f"roi must be integers L,T,W,H: {roi!r}") from e
    else:
        raise PathExtractError(f"roi must be L,T,W,H (4 ints), got {roi!r}")

    if W <= 0 or H <= 0:
        raise PathExtractError(f"roi empty/non-positive size: [{L},{T},{W},{H}]")
    if L < 0 or T < 0:
        raise PathExtractError(f"roi origin out of bounds: [{L},{T},{W},{H}]")
    if L + W > img_w or T + H > img_h:
        raise PathExtractError(
            f"roi out of bounds: [{L},{T},{W},{H}] vs image {img_w}x{img_h}"
        )
    return [L, T, W, H]


def _to_norm(x: float, y: float, w: int, h: int) -> list[int]:
    nx = int(round(float(x) / max(w, 1) * 1000))
    ny = int(round(float(y) / max(h, 1) * 1000))
    return [max(0, min(1000, nx)), max(0, min(1000, ny))]


def _subsample(pts: np.ndarray, max_points: int) -> tuple[np.ndarray, bool]:
    n = int(len(pts))
    if n <= max_points:
        return pts, False
    # Even index pick including first and last.
    idx = np.linspace(0, n - 1, num=max_points, dtype=int)
    return pts[idx], True


def _resolve_eps(contour: np.ndarray, *, simplify: float, simplify_frac: float) -> float:
    """Douglas–Peucker epsilon. simplify_frac wins when > 0 (relative to perimeter)."""
    if simplify_frac is not None and float(simplify_frac) > 0:
        peri = float(cv2.arcLength(contour, True))
        return float(simplify_frac) * peri
    return float(simplify)


def _approx_poly(
    contour: np.ndarray,
    *,
    eps: float,
    max_points: int,
) -> tuple[np.ndarray, bool, bool]:
    """approxPolyDP path. Returns (Nx2 float pts, closed, truncated)."""
    if eps > 0:
        approx = cv2.approxPolyDP(contour, eps, True)
    else:
        approx = contour
    pts = approx.reshape(-1, 2).astype(np.float64)
    truncated = False
    if len(pts) > max_points:
        pts, truncated = _subsample(pts, max_points)
    closed = True
    if len(pts) >= 2 and np.linalg.norm(pts[0] - pts[-1]) < 1.5:
        pts = pts[:-1]
    return pts, closed, truncated


def _resample_by_arc_length(
    pts: np.ndarray,
    spacing_px: float,
    *,
    closed: bool,
    max_points: int,
) -> tuple[np.ndarray, bool]:
    """Evenly sample along polyline arc length at *spacing_px* (approx)."""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 2:
        return pts, False

    spacing = float(spacing_px)
    if spacing <= 0:
        raise PathExtractError(f"spacing_px must be > 0, got {spacing_px!r}")

    if closed:
        loop = np.vstack([pts, pts[0:1]])
    else:
        loop = pts

    diffs = np.diff(loop, axis=0)
    seg_lens = np.linalg.norm(diffs, axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg_lens)])
    total = float(cum[-1])
    if total <= 1e-9:
        return pts[:1], False

    n_target = max(2, int(round(total / spacing)))
    truncated = False
    if n_target > max_points:
        n_target = max_points
        truncated = True

    if closed:
        distances = np.linspace(0.0, total, num=n_target, endpoint=False)
    else:
        distances = np.linspace(0.0, total, num=n_target, endpoint=True)

    out = np.empty((n_target, 2), dtype=np.float64)
    for j, d in enumerate(distances):
        i = int(np.searchsorted(cum, d, side="right") - 1)
        i = max(0, min(i, len(seg_lens) - 1))
        sl = seg_lens[i]
        t = 0.0 if sl <= 1e-9 else (d - cum[i]) / sl
        out[j] = loop[i] * (1.0 - t) + loop[i + 1] * t
    return out, truncated


def _contour_points(
    contour: np.ndarray,
    *,
    simplify: float,
    simplify_frac: float,
    spacing_px: float | None,
    max_points: int,
) -> tuple[np.ndarray, bool, bool, str]:
    """Return (pixel Nx2, closed, truncated, method)."""
    eps = _resolve_eps(contour, simplify=simplify, simplify_frac=simplify_frac)
    closed = True

    if spacing_px is not None:
        # Optional light DP (or none), then arc-length resample.
        if eps > 0:
            approx = cv2.approxPolyDP(contour, eps, True)
        else:
            approx = contour
        pts = approx.reshape(-1, 2).astype(np.float64)
        if len(pts) >= 2 and np.linalg.norm(pts[0] - pts[-1]) < 1.5:
            pts = pts[:-1]
        pts, truncated = _resample_by_arc_length(
            pts, float(spacing_px), closed=closed, max_points=max_points
        )
        return pts, closed, truncated, "arc_resample"

    pts, closed, truncated = _approx_poly(contour, eps=eps, max_points=max_points)
    return pts, closed, truncated, "approx_poly"


def _write_overlay(
    img: np.ndarray,
    paths_px: list[tuple[np.ndarray, bool]],
    overlay: str | Path,
    *,
    roi: list[int] | None = None,
) -> None:
    """Draw polylines + vertex dots on a copy of *img*; only call on success.

    *paths_px* are in full-image pixel coords. If *roi* is set, also draw the ROI rect.
    """
    vis = img.copy()
    if roi is not None:
        L, T, W, H = (int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3]))
        cv2.rectangle(vis, (L, T), (L + W - 1, T + H - 1), (255, 180, 0), 2)
    for pts, closed in paths_px:
        if len(pts) < 2:
            continue
        poly = np.round(pts).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(vis, [poly], isClosed=bool(closed), color=(0, 220, 0), thickness=2)
        for x, y in pts:
            cv2.circle(vis, (int(round(x)), int(round(y))), 2, (0, 0, 255), -1)
    out = Path(overlay)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(out), vis):
        raise PathExtractError(f"failed to write overlay: {out}")


def extract_paths(
    image: str | Path,
    *,
    mode: str = "contour",
    simplify: float = 1.5,
    simplify_frac: float = 0.0,
    spacing_px: float | None = None,
    min_points: int = 8,
    max_points: int = 400,
    invert: bool = False,
    largest_only: bool = True,
    overlay: str | Path | None = None,
    roi: Any = None,
) -> dict[str, Any]:
    """Extract polylines from *image*. Raises PathExtractError on fail-closed cases.

    Precedence for Douglas–Peucker epsilon:
      - if simplify_frac > 0: eps = simplify_frac * contour_perimeter
      - else: eps = simplify (absolute px; 0 disables DP)

    When spacing_px is set: optional DP then arc-length resample (method=arc_resample).
    When unset: approxPolyDP path (method=approx_poly), backward-compatible.

    Optional *roi* = [L,T,W,H] (image pixels): crop before contour; norm is 0–1000
    relative to ROI width/height. Overlay (if any) is drawn on the full image with
    ROI rectangle + polyline in full-image coords.
    """
    if mode != "contour":
        raise PathExtractError(f"mode {mode!r} not implemented in v0 (only contour)")

    path = Path(image)
    img = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if img is None:
        raise PathExtractError(f"unreadable image: {path}")

    full_h, full_w = img.shape[:2]
    roi_list: list[int] | None = None
    if roi is not None:
        roi_list = parse_roi(roi, full_w, full_h)
        L, T, W, H = roi_list
        work = img[T : T + H, L : L + W]
    else:
        L = T = 0
        work = img
        W, H = full_w, full_h

    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    if invert:
        gray = cv2.bitwise_not(gray)

    # Bright = foreground after optional invert (white-on-black silhouettes).
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        raise PathExtractError("no contour found")

    # Prefer longest perimeter (silhouette outline), not just area.
    contours = sorted(contours, key=lambda c: cv2.arcLength(c, True), reverse=True)
    if largest_only:
        contours = contours[:1]

    paths: list[dict[str, Any]] = []
    paths_px: list[tuple[np.ndarray, bool]] = []
    any_truncated = False
    method_used = "approx_poly"
    for i, c in enumerate(contours):
        pts_local, closed, truncated, method = _contour_points(
            c,
            simplify=simplify,
            simplify_frac=simplify_frac,
            spacing_px=spacing_px,
            max_points=max_points,
        )
        method_used = method
        if truncated:
            any_truncated = True
        if len(pts_local) < min_points:
            continue
        # Norm relative to ROI (or full image) width/height.
        points = [_to_norm(x, y, W, H) for x, y in pts_local]
        # Re-check after norm dedupe of consecutive identical ints.
        deduped: list[list[int]] = []
        for p in points:
            if not deduped or p != deduped[-1]:
                deduped.append(p)
        if len(deduped) < min_points:
            continue
        paths.append({"id": i, "closed": bool(closed), "points": deduped})
        # Overlay uses full-image pixel coords.
        pts_full = pts_local.copy()
        pts_full[:, 0] += L
        pts_full[:, 1] += T
        paths_px.append((pts_full, bool(closed)))

    if not paths:
        raise PathExtractError(
            f"no contour with at least min_points={min_points}"
        )

    if overlay is not None:
        _write_overlay(img, paths_px, overlay, roi=roi_list)

    n_points = sum(len(p["points"]) for p in paths)
    stats: dict[str, Any] = {
        "n_paths": len(paths),
        "n_points": n_points,
        "method": method_used,
    }
    if spacing_px is not None:
        stats["spacing_px"] = float(spacing_px)
    if any_truncated:
        stats["truncated"] = True

    source: dict[str, Any] = {
        "image": str(path),
        "width": int(full_w),
        "height": int(full_h),
        "mode": mode,
    }
    if roi_list is not None:
        source["roi"] = roi_list

    return {
        "version": 1,
        "experimental": True,
        "source": source,
        "coord_space": "norm",
        "paths": paths,
        "stats": stats,
    }


def path_extract_cli_result(
    *,
    image: str,
    out: str | None,
    mode: str = "contour",
    simplify: float = 1.5,
    simplify_frac: float = 0.0,
    spacing_px: float | None = None,
    min_points: int = 8,
    max_points: int = 400,
    invert: bool = False,
    largest_only: bool = True,
    overlay: str | None = None,
    roi: Any = None,
) -> tuple[dict[str, Any], int]:
    """Return (payload, exit_code). Writes *out* / *overlay* only on success."""
    try:
        data = extract_paths(
            image,
            mode=mode,
            simplify=simplify,
            simplify_frac=simplify_frac,
            spacing_px=spacing_px,
            min_points=min_points,
            max_points=max_points,
            invert=invert,
            largest_only=largest_only,
            overlay=overlay,
            roi=roi,
        )
    except PathExtractError as e:
        return {"ok": False, "error": str(e)}, 1
    except Exception as e:  # noqa: BLE001 — surface unexpected CV errors fail-closed
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}, 1

    out_path = None
    if out:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "ok": True,
        "out": str(out_path) if out_path else None,
        "overlay": str(Path(overlay)) if overlay else None,
        "stats": data["stats"],
        "source": data["source"],
        "coord_space": data["coord_space"],
        "n_paths": data["stats"]["n_paths"],
    }
    return summary, 0
