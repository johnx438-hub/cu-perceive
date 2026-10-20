"""Start an app by name (apps.json) or exe path. No secrets.

apps.json: CU_PERCEIVE_APPS, else <data dir>/apps.json, else the packaged
apps.default.json. ``exe`` values may use %ENV% / ~ (expanded at launch).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import config

APPS = config.apps_file()


def list_apps() -> list[dict]:
    path = config.apps_file()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def launch(app: str | None = None, exe: str | None = None) -> dict:
    rec = None
    if app:
        for row in list_apps():
            if (row.get("name") or "").lower() == app.lower():
                rec = row
                break
        if rec is None:
            raise KeyError(f"app {app!r} not in {config.apps_file()}")
        exe = rec.get("exe") or rec.get("app_path")
    if not exe:
        raise ValueError("launch needs --app or --exe")
    path = Path(_expand(exe))
    if not path.exists():
        raise FileNotFoundError(str(path))
    subprocess.Popen([str(path)], cwd=str(path.parent))
    from .settings import get as _cfg

    time.sleep(float(_cfg("timing.launch_wait_s")))
    from .windows import find_window
    win = find_window(exe=path.stem) or find_window(title=path.stem)
    return {"ok": True, "exe": str(path), "app": (rec or {}).get("name"), "window": win}
