"""Perceive: screenshot or image -> 0-1000 grid map. OCR is optional. No click."""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from PIL import ImageGrab

from . import config, extras, stamps

DEFAULT_OUT = config.out_dir()
CU_WEIGHTS = config.weights_dir()


def _is_real_onnx(path: Path, min_bytes: int = 200000) -> bool:
    if not path.exists() or path.stat().st_size < min_bytes:
        return False
    with path.open("rb") as fh:
        return not fh.read(20).startswith(b'version https://')


def _weights_dir():
    """Staged RapidOCR onnx (weights/rapidocr), else None = models bundled in the wheel."""
    staged = CU_WEIGHTS
    det = staged / "rapidocr" / "ch_PP-OCRv4_det_infer.onnx"
    rec = staged / "rapidocr" / "ch_PP-OCRv4_rec_infer.onnx"
    if _is_real_onnx(det, min_bytes=1_000_000) and _is_real_onnx(rec, min_bytes=1_000_000):
        return str(staged)
    return None


def _skip_enikk_yolo(_img):
    # Live YOLO is ScreenParser (cu_perceive.yolo). Do not probe dead Enikk icon_detect/model.onnx.
    return []


_PARSER = None


def _parser():
    global _PARSER
    if _PARSER is None:
        extras.require("ocr")
        try:
            from ._vendor.enikk.ui_parser import UIParser
        except ImportError as e:
            raise extras.missing_from_import_error("ocr", e) from e
        _PARSER = UIParser(weights_dir=_weights_dir(), icon_detector=_skip_enikk_yolo)
    return _PARSER


def _grab(path: Path | None, hwnd: int | None = None, activate: bool = False):
    if path:
        img = cv2.imread(str(path))
        if img is None:
            raise FileNotFoundError(path)
        h, w = img.shape[:2]
        return img, w, h
    if hwnd is not None:
        from .windows import capture_hwnd
        arr = capture_hwnd(int(hwnd), activate=activate)
        if arr is None:
            raise RuntimeError(f"capture failed hwnd={hwnd}")
        h, w = arr.shape[:2]
        return arr, w, h
    pil = ImageGrab.grab()
    arr = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    h, w = arr.shape[:2]
    return arr, w, h


def _to_items(raw: list[dict], w: int, h: int) -> list[dict]:
    items = []
    for i, row in enumerate(raw, start=1):
        nx1, ny1, nx2, ny2 = row["bbox"]
        x1, y1 = int(nx1 / 1000 * w), int(ny1 / 1000 * h)
        x2, y2 = int(nx2 / 1000 * w), int(ny2 / 1000 * h)
        text = (row.get("text") or "").strip()
        kind = "ocr" if text else "icon"
        items.append({
            "id": i,
            "text": text,
            "kind": kind,
            "box": [x1, y1, x2, y2],
            "center": [(x1 + x2) // 2, (y1 + y2) // 2],
            "norm": [nx1, ny1, nx2, ny2],
        })
    return items


def _annotate(img: np.ndarray, items: list[dict]) -> np.ndarray:
    out = img.copy()
    for it in items:
        x1, y1, x2, y2 = it["box"]
        color = (0, 200, 80) if it["kind"] == "ocr" else (0, 140, 255) if it["kind"] == "yolo" else (80, 160, 255)
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 2)
        label = str(it["id"])
        cv2.putText(out, label, (x1, max(14, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    return out


def draw_grid(img: np.ndarray) -> np.ndarray:
    """0-1000 ticks on the raw frame so a VLM can read coords without OCR."""
    out = img.copy()
    h, w = out.shape[:2]
    color = (0, 210, 255)
    for n in range(0, 1001, 100):
        x = min(w - 1, int(n / 1000 * w))
        y = min(h - 1, int(n / 1000 * h))
        cv2.line(out, (x, 0), (x, h - 1), color, 1)
        cv2.line(out, (0, y), (w - 1, y), color, 1)
        cv2.putText(out, str(n), (min(x + 3, w - 36), 16), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
        if n:
            cv2.putText(out, str(n), (3, min(y + 14, h - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
    tag = f"{w}x{h}  0-1000"
    cv2.putText(out, tag, (w - 150, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    return out


def _grid(img: np.ndarray) -> np.ndarray:
    return draw_grid(img)


def _parse_roi_parts(roi) -> list[int]:
    """Parse roi as L,T,W,H (list/tuple or comma string) -> 4 ints."""
    if isinstance(roi, str):
        parts = [p.strip() for p in roi.replace(" ", ",").split(",") if p.strip() != ""]
        if len(parts) != 4:
            raise ValueError(f"roi must be L,T,W,H (4 ints), got {roi!r}")
        try:
            return [int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])]
        except ValueError as e:
            raise ValueError(f"roi must be integers L,T,W,H: {roi!r}") from e
    if isinstance(roi, (list, tuple)) and len(roi) == 4:
        try:
            return [int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3])]
        except (TypeError, ValueError) as e:
            raise ValueError(f"roi must be integers L,T,W,H: {roi!r}") from e
    raise ValueError(f"roi must be L,T,W,H (4 ints), got {roi!r}")


def clamp_roi(roi, img_w: int, img_h: int) -> list[int]:
    """Parse L,T,W,H and clamp to image bounds. Fail if empty after clamp."""
    L0, T0, W0, H0 = _parse_roi_parts(roi)
    if W0 <= 0 or H0 <= 0:
        raise ValueError(f"roi empty/non-positive size: [{L0},{T0},{W0},{H0}]")
    L = max(0, L0)
    T = max(0, T0)
    R = min(int(img_w), L0 + W0)
    B = min(int(img_h), T0 + H0)
    W = R - L
    H = B - T
    if W <= 0 or H <= 0:
        raise ValueError(
            f"roi empty after clamp: [{L0},{T0},{W0},{H0}] vs image {img_w}x{img_h}"
        )
    return [L, T, W, H]


def roi_norm_to_pixel(roi_norm, full_w: int, full_h: int, default_half: int = 150):
    """Convert [cx,cy] or [cx,cy,half] in full-window 0-1000 -> clamped pixel ROI + normalized triple.

    half is in 0-1000 units on both axes (square in norm space). Default half=150.
    """
    if isinstance(roi_norm, str):
        parts = [p.strip() for p in roi_norm.replace(" ", ",").split(",") if p.strip() != ""]
        try:
            nums = [int(p) for p in parts]
        except ValueError as e:
            raise ValueError(f"roi_norm must be CX,CY[,HALF] ints, got {roi_norm!r}") from e
    elif isinstance(roi_norm, (list, tuple)):
        try:
            nums = [int(x) for x in roi_norm]
        except (TypeError, ValueError) as e:
            raise ValueError(f"roi_norm must be CX,CY[,HALF] ints, got {roi_norm!r}") from e
    else:
        raise ValueError(f"roi_norm must be CX,CY[,HALF], got {roi_norm!r}")
    if len(nums) not in (2, 3):
        raise ValueError(f"roi_norm must be CX,CY[,HALF] (2 or 3 ints), got {roi_norm!r}")
    cx, cy = nums[0], nums[1]
    half = int(nums[2]) if len(nums) == 3 else int(default_half)
    if half <= 0:
        raise ValueError(f"roi_norm half must be > 0, got {half}")
    cx_px = cx / 1000.0 * full_w
    cy_px = cy / 1000.0 * full_h
    half_w = half / 1000.0 * full_w
    half_h = half / 1000.0 * full_h
    L0 = int(round(cx_px - half_w))
    T0 = int(round(cy_px - half_h))
    W0 = int(round(2 * half_w))
    H0 = int(round(2 * half_h))
    if W0 < 1:
        W0 = 1
    if H0 < 1:
        H0 = 1
    roi_px = clamp_roi([L0, T0, W0, H0], full_w, full_h)
    return roi_px, [cx, cy, half]


def resolve_perceive_roi(roi=None, roi_norm=None, *, full_w: int, full_h: int):
    """Resolve optional roi / roi_norm into (pixel_roi, roi_norm_triple). Mutually exclusive."""
    if roi is not None and roi_norm is not None:
        raise ValueError("roi and roi_norm are mutually exclusive")
    if roi is None and roi_norm is None:
        return None, None
    if roi_norm is not None:
        return roi_norm_to_pixel(roi_norm, full_w, full_h)
    roi_px = clamp_roi(roi, full_w, full_h)
    L, T, W, H = roi_px
    cx = int(round((L + W / 2.0) / max(full_w, 1) * 1000))
    cy = int(round((T + H / 2.0) / max(full_h, 1) * 1000))
    half_x = (W / 2.0) / max(full_w, 1) * 1000
    half_y = (H / 2.0) / max(full_h, 1) * 1000
    half = int(round(max(half_x, half_y)))
    if half < 1:
        half = 1
    return roi_px, [cx, cy, half]


def _capture_pinned(hwnd: int, *, focus: bool, capture: str) -> dict:
    """Identity + origin + client grab for one hwnd. Returns {ok, ...}; never raises for
    minimized / capture failure / identity change (perceive reports those)."""
    import time

    from .capture import DWM_EXPERIMENTAL_WARNING, EXPERIMENTAL_BACKENDS, CaptureError, WindowMinimizedError
    from .windows import (
        RESTORE_SETTLE_S,
        capture_geometry_problem,
        capture_looks_implausible,
        capture_window_image,
        force_foreground,
        is_hwnd_minimized,
        is_sentinel_origin,
        live_geom,
        refresh_geom,
        verify_window_identity,
        window_identity,
    )

    if not focus and is_hwnd_minimized(hwnd):
        return {
            "ok": False,
            "minimized": True,
            "error": (
                f"window hwnd={hwnd} is minimized; not captured and not restored. "
                "Restore it yourself, or re-run with focus=true (CLI --focus) to restore and raise it."
            ),
            "capture": {"backend": None, "attempts": [], "skipped": "minimized"},
        }
    identity = window_identity(hwnd)
    if focus:
        force_foreground(hwnd)
        time.sleep(RESTORE_SETTLE_S)
        origin, win = refresh_geom(hwnd)
    else:
        origin, win = live_geom(hwnd)
    if origin is None or int(origin.get("width") or 0) <= 0 or int(origin.get("height") or 0) <= 0:
        return {"ok": False, "error": f"window hwnd={hwnd} has no client area; re-check the hwnd"}
    try:
        result = capture_window_image(hwnd, origin, capture=capture, focused=focus)
    except WindowMinimizedError as e:
        return {
            "ok": False,
            "minimized": True,
            "error": str(e),
            "capture": {"backend": None, "attempts": e.attempts, "skipped": "minimized"},
        }
    except CaptureError as e:
        hint = "" if focus else " Retry with focus=true (CLI --focus) to raise the window and grab the screen."
        return {
            "ok": False,
            "error": f"capture failed for hwnd={hwnd}: {e}.{hint}",
            "capture": {"backend": None, "attempts": e.attempts},
        }
    warnings: list[str] = []
    if capture in EXPERIMENTAL_BACKENDS:
        warnings.append(DWM_EXPERIMENTAL_WARNING)
    if result.blank:
        warnings.append("capture is all black on every backend; the window may really be black, or hidden")
    if focus and capture_looks_implausible(result.image, origin):
        time.sleep(RESTORE_SETTLE_S)
        origin, win2 = refresh_geom(hwnd)
        win = win2 or win
        if origin and not is_sentinel_origin(origin):
            try:
                result = capture_window_image(hwnd, origin, capture=capture, focused=True)
            except CaptureError:
                pass
    try:
        img_h, img_w = result.image.shape[:2]
        image_size = (int(img_w), int(img_h))
    except Exception:
        image_size = None
    geom_err, geom_warn = capture_geometry_problem(origin, image_size)
    if geom_err:
        hint = " (even after --focus restored and raised it)" if focus else "; --focus restores and raises it"
        return {
            "ok": False,
            "error": f"hwnd={hwnd}: {geom_err}{hint}",
            "capture": result.meta(),
            "degenerate_client": {**{k: (origin or {}).get(k) for k in ("left", "top", "width", "height")},
                                  "image": list(image_size) if image_size else None},
        }
    if geom_warn:
        warnings.append(geom_warn)
    after = verify_window_identity(hwnd, identity.get("pid"), identity.get("ctime"))
    if identity.get("pid") and identity.get("ctime") and after:
        return {"ok": False, "error": f"window changed during capture: {after}"}
    if after:
        warnings.append(f"window identity incomplete ({after}); act on this stamp will fail closed")
    return {
        "ok": True,
        "image": result.image,
        "origin": origin,
        "window": win,
        "identity": identity,
        "capture": result.meta(),
        "warnings": warnings,
    }


def _no_stamp_payload(res: dict, *, hwnd: int, win: dict | None, focus: bool) -> dict:
    """perceive result when nothing was captured: same top-level keys, ok=False, no stamp file."""
    win_slim = None
    if win:
        win_slim = {k: win[k] for k in ("hwnd", "title", "exe", "pid", "ctime", "exe_path", "rect") if k in win}
    out = {
        "ok": False,
        "error": res.get("error") or "capture failed",
        "stamp": None,
        "width": None,
        "height": None,
        "source": f"hwnd:{hwnd}",
        "hwnd": int(hwnd),
        "window": win_slim,
        "origin": None,
        "coord_space": "window",
        "items": [],
        "ocr": False,
        "yolo": False,
        "uia": False,
        "png": None,
        "map": None,
        "grid": None,
        "anno": None,
        "json": None,
        "focused": bool(focus),
    }
    if res.get("minimized"):
        out["minimized"] = True
    if res.get("capture"):
        out["capture"] = res["capture"]
    if res.get("degenerate_client"):
        out["degenerate_client"] = res["degenerate_client"]
    return out


def perceive(
    image: str | None = None,
    out_dir: str | None = None,
    hwnd: int | None = None,
    title: str | None = None,
    exe: str | None = None,
    focus: bool = False,
    ocr: bool = False,
    yolo: bool = False,
    uia: bool = False,
    roi=None,
    roi_norm=None,
    capture: str | None = None,
) -> dict:
    """Grab -> grid map (+ optional OCR / YOLO / UIA ids). Never clicks.

    A pinned window (hwnd/title/exe) is captured in place, even behind other
    windows; it is only raised / restored when ``focus`` is set. A minimized
    window without ``focus`` returns ok=False, minimized=True and writes no stamp.
    ``capture``: auto (WGC -> PrintWindow) | wgc | printwindow | screen | dwm (experimental).
    """
    from .paths import attach_wsl, coerce_out_dir
    if ocr:
        extras.require("ocr")
    if yolo:
        extras.require("yolo")
    if uia:
        extras.require("uia")
    capture = (capture or config.default_capture()).strip().lower()
    from .capture import parse_capture_choice
    parse_capture_choice(capture)
    out_dir = coerce_out_dir(out_dir)
    dest = Path(out_dir) if out_dir else DEFAULT_OUT
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(image) if image else None
    win = None
    origin = None
    identity = None
    capture_meta = None
    warnings: list[str] = []
    if src is None and hwnd is None and (title or exe):
        from .windows import find_window
        win = find_window(title=title or "", exe=exe or "")
        if win is None:
            raise RuntimeError(f"no window title={title!r} exe={exe!r}")
        hwnd = int(win["hwnd"])
    elif hwnd is not None:
        from .windows import find_window
        win = find_window(hwnd=int(hwnd))
    pin = src is None and hwnd is not None
    if pin:
        res = _capture_pinned(int(hwnd), focus=bool(focus), capture=capture)
        if not res.get("ok"):
            return _no_stamp_payload(res, hwnd=int(hwnd), win=win, focus=bool(focus))
        bgr, origin, identity = res["image"], res["origin"], res["identity"]
        win = res.get("window") or win
        capture_meta = res["capture"]
        warnings.extend(res.get("warnings") or [])
        h, w = bgr.shape[:2]
    else:
        bgr, w, h = _grab(src, hwnd=None, activate=False)
    full_w, full_h = int(w), int(h)
    # E2 light verify: optional ROI crop. Map grid 0-1000 is relative to the ROI
    # crop (local verify space). For next act clicks prefer a fresh full perceive
    # unless converting ROI-local norms back to full-window coordinates.
    roi_px, roi_norm_out = resolve_perceive_roi(roi, roi_norm, full_w=full_w, full_h=full_h)
    verify_light = roi_px is not None
    if verify_light:
        L, T, Wcrop, Hcrop = roi_px
        bgr = bgr[T : T + Hcrop, L : L + Wcrop]
        if bgr.size == 0:
            raise ValueError(f"roi crop empty: {roi_px} vs {full_w}x{full_h}")
        w, h = int(bgr.shape[1]), int(bgr.shape[0])

    items: list[dict] = []
    ocr_ms = None
    if ocr:
        import time as _time

        _t_ocr = _time.perf_counter()
        raw = _parser().parse(bgr)
        ocr_ms = int(round((_time.perf_counter() - _t_ocr) * 1000))
        items.extend(_to_items(raw, w, h))
    if yolo:
        from .yolo import detect as yolo_detect
        yitems = _to_items(yolo_detect(bgr), w, h)
        for it in yitems:
            it["kind"] = "yolo"
        items.extend(yitems)
    for i, it in enumerate(items, start=1):
        it["id"] = i
    space = "window" if hwnd is not None and src is None else "screen"
    from .windows import is_sentinel_origin
    if space == "window" and origin and not is_sentinel_origin(origin):
        for it in items:
            it["screen"] = [origin["left"] + it["center"][0], origin["top"] + it["center"][1]]
    else:
        for it in items:
            it["screen"] = list(it["center"])
        if space == "window" and is_sentinel_origin(origin):
            origin = origin or {}
            origin = dict(origin)
            origin["bad"] = True
    stamp, js = stamps.allocate(dest)
    png = dest / f"{stamp}.png"
    gridp = dest / f"{stamp}.grid.png"
    try:
        cv2.imwrite(str(png), bgr)
        cv2.imwrite(str(gridp), draw_grid(bgr))
        anno = None
        if ocr or yolo:
            anno = dest / f"{stamp}.anno.png"
            cv2.imwrite(str(anno), _annotate(bgr, items))
        if src:
            source = str(src)
        elif hwnd is not None:
            source = f"hwnd:{hwnd}"
        else:
            source = "screen"
        win_slim = None
        if win:
            win_slim = {k: win[k] for k in ("hwnd", "title", "exe", "pid", "ctime", "exe_path", "rect") if k in win}
        # S4: optional compact UIA nodes (additive; fail-closed when requested).
        uia_nodes: list = []
        uia_meta: dict = {}
        uia_ok = True
        uia_error = None
        if uia:
            from .uia import collect_uia_nodes
            if verify_light:
                uia_ok = False
                uia_error = (
                    "perceive --uia is full-window only for E2; omit --roi/--roi-norm when using --uia"
                )
            elif hwnd is None:
                uia_ok = False
                uia_error = "perceive --uia requires a pinned window hwnd (use --hwnd/--title/--exe)"
            else:
                import time as _time

                ol = float((origin or {}).get("left") or 0)
                ot = float((origin or {}).get("top") or 0)
                _t_uia = _time.perf_counter()
                uia_res = collect_uia_nodes(
                    int(hwnd),
                    width=int(w),
                    height=int(h),
                    origin_left=ol,
                    origin_top=ot,
                )
                uia_ms = int(round((_time.perf_counter() - _t_uia) * 1000))
                uia_ok = bool(uia_res.get("ok"))
                uia_error = uia_res.get("error")
                uia_nodes = list(uia_res.get("uia_nodes") or [])
                uia_meta = {
                    k: uia_res[k]
                    for k in ("uia_count", "uia_visited", "uia_truncated", "caps")
                    if k in uia_res
                }
                uia_meta["uia_ms"] = uia_ms

        payload = {
            "stamp": stamp,
            "width": w,
            "height": h,
            "source": source,
            "hwnd": int(hwnd) if hwnd is not None else None,
            "window": win_slim,
            "origin": origin,
            "coord_space": space,
            "items": items,
            "ocr": bool(ocr),
            "yolo": bool(yolo),
            "uia": bool(uia),
            "png": str(png),
            "map": str(gridp),
            "grid": str(gridp),
            "anno": str(anno) if anno else None,
            "json": str(js),
        }
        if pin:
            payload["identity"] = identity
            payload["capture"] = capture_meta
            payload["focused"] = bool(focus)
        if warnings:
            payload["warnings"] = warnings
        if verify_light:
            payload["roi"] = list(roi_px)
            payload["full_width"] = full_w
            payload["full_height"] = full_h
            payload["verify_light"] = True
            payload["roi_norm"] = list(roi_norm_out) if roi_norm_out is not None else None
        if ocr_ms is not None:
            payload["ocr_ms"] = ocr_ms
        if uia:
            payload["uia_nodes"] = uia_nodes
            payload["uia_count"] = len(uia_nodes)
            payload.update(uia_meta)
            payload["ok"] = bool(uia_ok)
            if uia_error:
                payload["error"] = uia_error
        attach_wsl(payload, 'png', 'map', 'grid', 'anno', 'json')
        stamps.commit(js, json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    except BaseException:
        stamps.release(js)
        for orphan in (png, gridp, dest / f"{stamp}.anno.png"):
            try:
                orphan.unlink()
            except OSError:
                pass
        raise
