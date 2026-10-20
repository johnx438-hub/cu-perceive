"""Opt-in act(check_change=true): did the target window's pixels change across a go?

Off by default. It only answers "did pixels move"; whether the *right* thing changed
(an in-window page switch, a value) stays the calling agent's judgment.
"""
from __future__ import annotations

import os
import time

import numpy as np

CHANGE_PIXEL_THRESHOLD = 12  # per-pixel max-channel delta that counts as changed (grabs are lossless)
CHANGE_MIN_PIXELS = 8  # real pixels; a WinForms checkbox tick is ~21 px, so the floor stays tiny
REFLOW_RATIO = 0.3  # more than 30% of the window changed: coordinates may all have moved
CHANGE_STEP = 1  # full resolution: sampling every 2nd pixel missed most of a tick


def change_wait_s() -> float:
    try:
        return max(0.0, float(os.environ.get("CU_PERCEIVE_CHANGE_WAIT_MS", "350")) / 1000.0)
    except ValueError:
        return 0.35


def diff_images(before, after, *, threshold: int = CHANGE_PIXEL_THRESHOLD,
                min_pixels: int = CHANGE_MIN_PIXELS, step: int = CHANGE_STEP) -> dict:
    """{screen_changed, changed_ratio, changed_pixels, changed_bbox_norm|None, size_changed?}.

    screen_changed is a count test, not a ratio: a ticked checkbox in a big window is a
    real change at a tiny ratio. A blinking caret (~1x16 px) can also count: read
    changed_pixels / changed_bbox_norm and judge whether it is the change you wanted.
    """
    if before is None or after is None:
        return {"screen_changed": None, "changed_ratio": None, "changed_bbox_norm": None}
    a = np.asarray(before)
    b = np.asarray(after)
    if a.shape != b.shape:
        return {"screen_changed": True, "changed_ratio": 1.0, "changed_bbox_norm": [0, 0, 1000, 1000],
                "size_changed": True}
    h, w = a.shape[:2]
    sa = a[::step, ::step].astype(np.int16)
    sb = b[::step, ::step].astype(np.int16)
    d = np.abs(sa - sb)
    if d.ndim == 3:
        d = d.max(axis=2)
    mask = d > int(threshold)
    total = mask.size or 1
    n_changed = int(mask.sum())
    ratio = float(n_changed) / total
    bbox = None
    if mask.any():
        ys, xs = np.nonzero(mask)
        x0, x1 = int(xs.min()) * step, min(w, (int(xs.max()) + 1) * step)
        y0, y1 = int(ys.min()) * step, min(h, (int(ys.max()) + 1) * step)
        bbox = [round(x0 * 1000 / max(w, 1)), round(y0 * 1000 / max(h, 1)),
                round(x1 * 1000 / max(w, 1)), round(y1 * 1000 / max(h, 1))]
    return {"screen_changed": n_changed * step * step >= int(min_pixels), "changed_ratio": round(ratio, 5),
            "changed_pixels": n_changed * step * step, "changed_bbox_norm": bbox}


def grab_window(hwnd: int):
    """Client-area pixels of hwnd (auto backend, no focus change). None on failure."""
    from .windows import capture_window_image, live_geom

    try:
        origin, _ = live_geom(int(hwnd))
        if not origin:
            return None
        return capture_window_image(int(hwnd), origin, capture="auto", focused=False).image
    except Exception:
        return None


def run_with_change_check(hwnd: int | None, run, *, grab=None, sleep=time.sleep, wait_s: float | None = None) -> dict:
    """grab -> run() -> wait -> grab; merges screen_changed/changed_ratio into run()'s dict."""
    grab = grab or grab_window
    if not hwnd:
        out = run()
        out["screen_changed"] = None
        out["check_change_note"] = "check_change needs a window stamp (hwnd); screen stamps are not diffed"
        return out
    before = grab(hwnd)
    out = run()
    sleep(change_wait_s() if wait_s is None else wait_s)
    after = grab(hwnd)
    res = diff_images(before, after)
    ratio = res.get("changed_ratio")
    res["layout_reflow_suspected"] = bool(ratio is not None and ratio > REFLOW_RATIO)
    if res["layout_reflow_suspected"]:
        out["warnings"] = (out.get("warnings") or []) + [
            f"{ratio:.0%} of the window changed: the layout may have reflowed (panel closed, page "
            "switched, list re-sorted); re-perceive before reusing old coordinates"
        ]
    if before is None or after is None:
        res["check_change_note"] = "window grab failed before or after; screen_changed unknown"
    out.update(res)
    return out
