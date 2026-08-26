"""Start an app by name (apps.json) or exe path. No secrets."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

APPS = Path(r"C:\Users\jawn\src\cu-perceive\apps.json")


def list_apps() -> list[dict]:
    if not APPS.exists():
        return []
    return json.loads(APPS.read_text(encoding="utf-8"))


def launch(app: str | None = None, exe: str | None = None) -> dict:
    rec = None
    if app:
        for row in list_apps():
            if (row.get("name") or "").lower() == app.lower():
                rec = row
                break
        if rec is None:
            raise KeyError(f"app {app!r} not in {APPS}")
        exe = rec.get("exe") or rec.get("app_path")
    if not exe:
        raise ValueError("launch needs --app or --exe")
    path = Path(exe)
    if not path.exists():
        raise FileNotFoundError(exe)
    subprocess.Popen([str(path)], cwd=str(path.parent))
    time.sleep(1.2)
    from .windows import find_window
    win = find_window(exe=path.stem) or find_window(title=path.stem)
    return {"ok": True, "exe": str(path), "app": (rec or {}).get("name"), "window": win}
