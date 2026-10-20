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
# Primary reflow signal (WorkBuddy retest on WPS/Xmind): the change *spans* the window even when
# few pixels flip (white page swap 0.167 / bbox 100%, list swap 0.034 / bbox ~90%). A hover highlight
# (0.00055, small bbox) stays out. An Xmind node drag (0.081, bbox near full) does trigger: Xmind
# re-lays the map after a drop, so re-perceiving is the safe call.
REFLOW_BBOX_SHARE = 0.7
REFLOW_MIN_RATIO = 0.02
# Second tier: a page with margins (text column) swaps ~19% of pixels inside a ~57% box (live WinForms test).
REFLOW_BBOX_SHARE_MID = 0.4
REFLOW_MIN_RATIO_MID = 0.1
# screen_changed floor as a share of the window (WorkBuddy retest of c9571c4: hover 0.0005, real
# checkbox with its redraw 0.019, list swap 0.036, page switch 0.213). Below it: screen_changed=false,
# below_min_ratio=true, pixels/ratio/bbox still reported. A bare synthetic tick (~6e-5) falls under it:
# pass change_min_ratio lower when such tiny changes matter.
CHANGE_MIN_RATIO = 0.001
# Row-band reflow (WorkBuddy: Explorer list re-sort 0.033 inside a 17% bbox of a tall window).
# >=3 horizontal bands of change with similar width = rows moved. Gated on ratio so a hover (0.0005),
# one checkbox (0.0032) or sparse homepage animation (~0.003) never qualify.
ROW_BAND_MIN = 3
ROW_BAND_MIN_RATIO = 0.01
ROW_BAND_GAP_PX = 2  # unchanged rows that split two bands
ROW_BAND_MIN_WIDTH = 0.15  # of the window width, median band
ROW_BAND_SIMILAR = (0.6, 1.6)  # band width within this factor of the median
# change_minor band: counted, but under 0.5% of the window. Mostly hover / caret / focus-ring noise
# that cleared the floor; real small controls (a checkbox redraw) land here too, so read the bbox.
CHANGE_MINOR_RATIO = 0.005
CHANGE_STEP = 1  # full resolution: sampling every 2nd pixel missed most of a tick


def change_wait_s() -> float:
    try:
        return max(0.0, float(os.environ.get("CU_PERCEIVE_CHANGE_WAIT_MS", "350")) / 1000.0)
    except ValueError:
        return 0.35


def row_bands(mask) -> list[tuple[int, int, int, int]]:
    """Horizontal bands of change: (y0, y1, x0, x1) per run of changed rows."""
    rows = mask.any(axis=1)
    bands = []
    y = 0
    h = len(rows)
    while y < h:
        if not rows[y]:
            y += 1
            continue
        y0 = y
        gap = 0
        while y < h and gap <= ROW_BAND_GAP_PX:
            gap = 0 if rows[y] else gap + 1
            y += 1
        y1 = y - gap
        cols = mask[y0:y1].any(axis=0)
        xs = cols.nonzero()[0]
        bands.append((y0, y1, int(xs.min()), int(xs.max()) + 1))
    return bands


def similar_row_bands(bands, width: int) -> int:
    """How many bands have a width close to the median band width (and that median is wide)."""
    if len(bands) < ROW_BAND_MIN:
        return 0
    widths = sorted(b[3] - b[2] for b in bands)
    med = widths[len(widths) // 2]
    if med < ROW_BAND_MIN_WIDTH * max(width, 1):
        return 0
    lo, hi = ROW_BAND_SIMILAR
    return sum(1 for w in widths if lo * med <= w <= hi * med)


def change_min_ratio() -> float:
    try:
        return max(0.0, float(os.environ.get("CU_PERCEIVE_CHANGE_MIN_RATIO", CHANGE_MIN_RATIO)))
    except ValueError:
        return CHANGE_MIN_RATIO


def bbox_share(bbox) -> float | None:
    """Area of a 0-1000 changed_bbox_norm as a share of the window (0-1)."""
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    return round(max(0, x1 - x0) * max(0, y1 - y0) / 1_000_000, 4)


def reflow_reason(ratio, bbox, bands: int | None = None) -> str | None:
    """Why the layout may have reflowed: "ratio" (>30% of pixels), "bbox_span" (change spans
    >70% of the window with >=2% of pixels, or >40% with >=10%), "row_bands" (>=3 similar-width
    horizontal bands of change with >=1% of pixels: a list re-sorted), or None."""
    if ratio is None:
        return None
    if ratio > REFLOW_RATIO:
        return "ratio"
    share = bbox_share(bbox)
    if share is not None and ((share > REFLOW_BBOX_SHARE and ratio >= REFLOW_MIN_RATIO)
                              or (share > REFLOW_BBOX_SHARE_MID and ratio >= REFLOW_MIN_RATIO_MID)):
        return "bbox_span"
    if bands is not None and bands >= ROW_BAND_MIN and ratio >= ROW_BAND_MIN_RATIO:
        return "row_bands"
    return None


def diff_images(before, after, *, threshold: int = CHANGE_PIXEL_THRESHOLD,
                min_pixels: int = CHANGE_MIN_PIXELS, step: int = CHANGE_STEP,
                min_ratio: float | None = None) -> dict:
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
    floor = change_min_ratio() if min_ratio is None else max(0.0, float(min_ratio))
    px = n_changed * step * step
    counted = px >= int(min_pixels) and ratio >= floor
    res = {"screen_changed": counted, "changed_ratio": round(ratio, 6 if ratio < 0.001 else 5),
           "changed_pixels": px, "changed_bbox_norm": bbox, "change_min_ratio": floor}
    if mask.any():
        res["changed_row_bands"] = similar_row_bands(row_bands(mask), mask.shape[1])
    if counted and ratio < CHANGE_MINOR_RATIO:
        res["change_minor"] = True
    elif not counted and px >= int(min_pixels):
        res["below_min_ratio"] = True
    return res


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


def run_with_change_check(hwnd: int | None, run, *, grab=None, sleep=time.sleep, wait_s: float | None = None,
                          min_ratio: float | None = None, before_baseline=None) -> dict:
    """grab -> run() -> wait -> grab; merges screen_changed/changed_ratio into run()'s dict."""
    grab = grab or grab_window
    if not hwnd:
        out = run()
        out["screen_changed"] = None
        out["check_change_note"] = "check_change needs a window stamp (hwnd); screen stamps are not diffed"
        return out
    baseline_note = None
    if before_baseline is not None:
        # focus=true: raise the window first so activation (title bar, focus ring) is not diffed
        try:
            baseline_note = before_baseline()
        except Exception as e:  # the act itself reports focus problems
            baseline_note = f"pre-focus failed: {type(e).__name__}"
    before = grab(hwnd)
    out = run()
    sleep(change_wait_s() if wait_s is None else wait_s)
    after = grab(hwnd)
    res = diff_images(before, after, min_ratio=min_ratio)
    ratio = res.get("changed_ratio")
    share = bbox_share(res.get("changed_bbox_norm"))
    if share is not None:
        res["changed_bbox_share"] = share
    why = reflow_reason(ratio, res.get("changed_bbox_norm"), res.get("changed_row_bands"))
    if baseline_note:
        res["change_baseline"] = baseline_note
    res["layout_reflow_suspected"] = why is not None
    if why:
        res["reflow_reason"] = why
        span = f", spanning {share:.0%} of it" if share is not None else ""
        if why == "row_bands":
            span += f" in {res.get('changed_row_bands')} row bands"
        out["warnings"] = (out.get("warnings") or []) + [
            f"{ratio:.1%} of the window changed{span}: the layout may have reflowed (panel closed, page "
            "switched, list re-sorted); re-perceive before reusing old coordinates"
        ]
    if before is None or after is None:
        res["check_change_note"] = "window grab failed before or after; screen_changed unknown"
    out.update(res)
    return out
