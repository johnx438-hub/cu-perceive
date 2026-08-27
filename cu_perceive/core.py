"""Perceive: screenshot or image -> 0-1000 grid map. OCR is optional. No click."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import ImageGrab

ENIKK_ROOT = Path(r"C:\Users\jawn\src\enikk")
DEFAULT_OUT = Path(r"C:\Users\jawn\agent-bus\archive\shots\perceive")
WEIGHTS = ENIKK_ROOT / "weights"
CU_WEIGHTS = Path(r"C:\Users\jawn\src\cu-perceive\weights")

if str(ENIKK_ROOT) not in sys.path:
    sys.path.insert(0, str(ENIKK_ROOT))


def _is_real_onnx(path: Path, min_bytes: int = 200000) -> bool:
    if not path.exists() or path.stat().st_size < min_bytes:
        return False
    return not path.read_bytes()[:20].startswith(b'version https://')


def _weights_dir():
    """cu-perceive/weights. RapidOCR official onnx lives here; skip dead Enikk YOLO."""
    staged = CU_WEIGHTS
    staged.mkdir(parents=True, exist_ok=True)
    det = staged / "rapidocr" / "ch_PP-OCRv4_det_infer.onnx"
    rec = staged / "rapidocr" / "ch_PP-OCRv4_rec_infer.onnx"
    if _is_real_onnx(det, min_bytes=1_000_000) and _is_real_onnx(rec, min_bytes=1_000_000):
        return str(staged)
    return None


def _skip_enikk_yolo(_img):
    # Live YOLO is ScreenParser (cu_perceive.yolo). Do not probe dead Enikk icon_detect/model.onnx.
    return []


def _parser():
    from enikk.ui_parser import UIParser
    return UIParser(weights_dir=_weights_dir(), icon_detector=_skip_enikk_yolo)


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


def perceive(
    image: str | None = None,
    out_dir: str | None = None,
    hwnd: int | None = None,
    title: str | None = None,
    exe: str | None = None,
    focus: bool = False,
    ocr: bool = False,
    yolo: bool = False,
) -> dict:
    from .paths import attach_wsl, coerce_out_dir
    out_dir = coerce_out_dir(out_dir)
    dest = Path(out_dir) if out_dir else DEFAULT_OUT
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    src = Path(image) if image else None
    win = None
    origin = None
    if src is None and hwnd is None and (title or exe):
        from .windows import find_window
        win = find_window(title=title or "", exe=exe or "")
        if win is None:
            raise RuntimeError(f"no window title={title!r} exe={exe!r}")
        hwnd = int(win["hwnd"])
    elif hwnd is not None:
        from .windows import find_window
        win = find_window(hwnd=int(hwnd))
    # Pin-one-window is the product: bring to front so capture_hwnd
    # always double-shots (discard first frame). Quiet screen/image grabs stay single.
    pin = src is None and hwnd is not None
    bgr, w, h = _grab(src, hwnd=None if src else hwnd, activate=bool(focus or pin))
    if pin:
        from .windows import capture_looks_implausible, is_sentinel_origin, refresh_geom
        origin, win2 = refresh_geom(int(hwnd))
        if win2:
            win = win2
        if is_sentinel_origin(origin):
            origin, win2 = refresh_geom(int(hwnd))
            if win2:
                win = win2
        # Double-shot already happened inside capture_hwnd. Recapture here
        # only if the second frame is still implausible.
        if capture_looks_implausible(bgr, origin):
            bgr, w, h = _grab(None, hwnd=int(hwnd), activate=False)
            origin, win2 = refresh_geom(int(hwnd))
            if win2:
                win = win2
    items: list[dict] = []
    if ocr:
        raw = _parser().parse(bgr)
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
    png = dest / f"{stamp}.png"
    gridp = dest / f"{stamp}.grid.png"
    js = dest / f"{stamp}.json"
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
        win_slim = {k: win[k] for k in ("hwnd", "title", "exe", "pid", "exe_path", "rect") if k in win}
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
        "png": str(png),
        "map": str(gridp),
        "grid": str(gridp),
        "anno": str(anno) if anno else None,
        "json": str(js),
    }
    attach_wsl(payload, 'png', 'map', 'grid', 'anno', 'json')
    js.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
