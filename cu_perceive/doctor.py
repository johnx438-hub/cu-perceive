"""``cu-perceive doctor``: check that this install matches the full maintainer setup.

Reports the Python version, each optional extra (with the exact install command when it
is missing), the torch build (CPU or CUDA), the model weights (present + SHA-256), and runs
an OCR smoke test on a generated image plus a YOLO load test. Exit code 1 on any failure.
"""
from __future__ import annotations

import platform
import sys
import time

from . import config, extras, models

MIN_PYTHON = (3, 11)
SMOKE_TEXT = "HELLO 2026"


def _python_info() -> dict:
    v = sys.version_info
    return {
        "version": platform.python_version(),
        "executable": sys.executable,
        "ok": (v.major, v.minor) >= MIN_PYTHON,
        "required": ">=" + ".".join(map(str, MIN_PYTHON)),
    }


def _extras_info() -> dict:
    out = {}
    for extra in extras.MODULES:
        missing = extras.missing_modules(extra)
        row = {"installed": not missing, "purpose": extras.PURPOSE.get(extra, extra)}
        if missing:
            row["missing"] = missing
            row["install"] = extras.install_command(extra)
        out[extra] = row
    return out


def _torch_info() -> dict:
    if extras.missing_modules("yolo"):
        return {"installed": False}
    try:
        import torch
    except Exception as e:  # noqa: BLE001 - a broken torch install is a finding, not a crash
        return {"installed": False, "error": f"{type(e).__name__}: {e}"}
    cuda_build = getattr(torch.version, "cuda", None)
    try:
        cuda_available = bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        cuda_available = False
    return {
        "installed": True,
        "version": str(torch.__version__),
        "build": "cuda" if cuda_build else "cpu",
        "cuda_version": cuda_build,
        "cuda_available": cuda_available,
    }


def _weights_info() -> dict:
    root = config.weights_dir()
    files = [models.check_file(e, root) for e in models.load_manifest()]
    yolo_weight = config.yolo_weight()
    info = {
        "dir": str(root),
        "ok": all(f["status"] == "ok" for f in files),
        "files": files,
        "yolo_weight": str(yolo_weight),
        "yolo_weight_present": yolo_weight.is_file(),
    }
    if not info["ok"]:
        info["fix"] = f"{extras._python()} -m cu_perceive fetch-models"
    return info


def _smoke_image():
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.load_default(size=48)
    except TypeError:  # Pillow < 10.1 has no sized default font
        font = ImageFont.load_default()
    img = Image.new("RGB", (480, 120), "white")
    ImageDraw.Draw(img).text((24, 30), SMOKE_TEXT, fill="black", font=font)
    return np.ascontiguousarray(np.asarray(img)[:, :, ::-1])  # RGB -> BGR


def _ocr_smoke() -> dict:
    from . import detect_worker

    t0 = time.perf_counter()
    raw = detect_worker.ocr(_smoke_image())
    text = " ".join(str(r.get("text") or "") for r in raw or []).strip()
    ok = "HELLO" in text.upper().replace(" ", "")
    out = {"ok": ok, "text": text, "expected": SMOKE_TEXT, "ms": int((time.perf_counter() - t0) * 1000)}
    if not ok:
        out["error"] = f"OCR did not read {SMOKE_TEXT!r} from the generated image (got {text!r})"
    return out


def _yolo_smoke() -> dict:
    import numpy as np

    from . import detect_worker

    t0 = time.perf_counter()
    boxes = detect_worker.yolo(np.full((320, 320, 3), 255, np.uint8))
    return {"ok": isinstance(boxes, list), "boxes": len(boxes or []),
            "ms": int((time.perf_counter() - t0) * 1000)}


def _run_smoke(name: str, fn, ready: bool, why: str) -> dict:
    if not ready:
        return {"ok": False, "skipped": True, "error": why}
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - report every failure
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def run(smoke: bool = True) -> dict:
    report: dict = {
        "python": _python_info(),
        "platform": platform.platform(),
        "cu_perceive": {"version": models.package_version(), "package_dir": str(config.PACKAGE_DIR)},
        "extras": _extras_info(),
        "install_full": extras.install_command("full"),
        "torch": _torch_info(),
        "weights": _weights_info(),
    }
    failures: list[str] = []
    if not report["python"]["ok"]:
        failures.append(f"python {report['python']['version']} is older than {report['python']['required']}")
    for extra, row in report["extras"].items():
        if not row["installed"]:
            failures.append(f"extra '{extra}' missing: {row['install']}")
    for f in report["weights"]["files"]:
        if f["status"] != "ok":
            failures.append(f"weight {f['path']}: {f['status']} ({report['weights']['fix']})")
    if smoke:
        ocr_ok = report["extras"]["ocr"]["installed"]
        yolo_ok = report["extras"]["yolo"]["installed"] and report["weights"]["yolo_weight_present"]
        report["smoke"] = {
            "ocr": _run_smoke("ocr", _ocr_smoke, ocr_ok, "ocr extra not installed"),
            "yolo": _run_smoke("yolo", _yolo_smoke, yolo_ok, "yolo extra or ScreenParser weight missing"),
        }
        for name, res in report["smoke"].items():
            if not res["ok"]:
                failures.append(f"{name} smoke test: {res.get('error') or 'failed'}")
    report["failures"] = failures
    report["ok"] = not failures
    return report


def format_text(report: dict) -> str:
    def mark(ok) -> str:
        return "ok  " if ok else "FAIL"

    py = report["python"]
    lines = [f"[{mark(py['ok'])}] python {py['version']} ({py['executable']})"]
    for extra, row in report["extras"].items():
        tail = "" if row["installed"] else f"  missing {', '.join(row['missing'])} -> {row['install']}"
        lines.append(f"[{mark(row['installed'])}] extra {extra}{tail}")
    t = report["torch"]
    if t.get("installed"):
        lines.append(f"[ok  ] torch {t['version']} build={t['build']} cuda_available={t['cuda_available']}")
    else:
        lines.append("[FAIL] torch not installed" + (f" ({t['error']})" if t.get("error") else ""))
    w = report["weights"]
    lines.append(f"       weights dir {w['dir']}")
    for f in w["files"]:
        lines.append(f"[{mark(f['status'] == 'ok')}] weight {f['path']}: {f['status']}")
    if w.get("fix"):
        lines.append(f"       fix: {w['fix']}")
    for name, res in (report.get("smoke") or {}).items():
        detail = res.get("error") or (f"read {res.get('text')!r}" if name == "ocr" else "model loaded")
        ms = f" {res['ms']} ms" if "ms" in res else ""
        lines.append(f"[{mark(res['ok'])}] {name} smoke: {detail}{ms}")
    lines.append("doctor: ok" if report["ok"] else f"doctor: {len(report['failures'])} problem(s)")
    return "\n".join(lines)
