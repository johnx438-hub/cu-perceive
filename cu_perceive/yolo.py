"""Optional ScreenParser (YOLO11-L) detector. Default off. Needs the 'yolo' extra (AGPL-3.0)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import config, extras

WEIGHT = config.yolo_weight()
_model = None


def _load():
    global _model
    if _model is None:
        extras.require("yolo")
        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise extras.missing_from_import_error("yolo", e) from e
        weight = Path(WEIGHT)
        if not weight.exists() or weight.stat().st_size < 1_000_000:
            raise FileNotFoundError(
                f"ScreenParser weight missing: {weight}. Download best.pt (see weights/README.md) "
                f"or set {config.ENV_YOLO_WEIGHT} to its path."
            )
        _model = YOLO(str(weight))
    return _model


def detect(bgr: np.ndarray, conf: float = 0.10, iou: float = 0.10) -> list[dict]:
    """Boxes in Enikk-style 0-1000 bbox + class name as text."""
    h, w = bgr.shape[:2]
    model = _load()
    results = model.predict(source=bgr, imgsz=1280, conf=conf, iou=iou, verbose=False)
    out: list[dict] = []
    for r in results:
        names = r.names or model.names
        if r.boxes is None:
            continue
        for box, cls_id, c in zip(r.boxes.xyxy, r.boxes.cls, r.boxes.conf):
            x1, y1, x2, y2 = (int(v) for v in box.tolist())
            out.append({
                "text": str(names[int(cls_id)]),
                "conf": float(c),
                "bbox": [
                    int(x1 / max(w, 1) * 1000),
                    int(y1 / max(h, 1) * 1000),
                    int(x2 / max(w, 1) * 1000),
                    int(y2 / max(h, 1) * 1000),
                ],
            })
    return out
