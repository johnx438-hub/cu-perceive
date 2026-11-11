"""Start an app by name (apps.json) or exe path, optionally with an argv list. No secrets.

apps.json: CU_PERCEIVE_APPS, else <data dir>/apps.json, else the packaged
apps.default.json. ``exe`` values may use %ENV% / ~ (expanded at launch). A row may
carry ``"args": [...]``; call-time ``args`` are appended after the row's.

Browser guard (chrome.exe / msedge.exe): a launch never lands on the browser's main
profile. With no ``--user-data-dir`` the shadow profile is used
(``CU_PERCEIVE_SHADOW_PROFILE_CHROME`` / ``CU_PERCEIVE_SHADOW_PROFILE_EDGE``, default
``~/.cu-perceive-trial-chrome-profile`` / ``~/.cu-perceive-trial-edge-profile``); a
``--user-data-dir`` inside the main ``User Data`` folder is refused (ProfileRefused).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import config

APPS = config.apps_file()

MAX_ARGS = 64
MAX_ARG_LEN = 4096
# exe stem -> (kind, shadow env var, default shadow dir name, main profile dirs)
BROWSERS = {
    "chrome": ("chrome", "CU_PERCEIVE_SHADOW_PROFILE_CHROME", ".cu-perceive-trial-chrome-profile",
               (r"%LOCALAPPDATA%\Google\Chrome\User Data", r"%LOCALAPPDATA%\Google\Chrome Beta\User Data",
                r"%LOCALAPPDATA%\Google\Chrome Dev\User Data", r"%LOCALAPPDATA%\Google\Chrome SxS\User Data")),
    "msedge": ("edge", "CU_PERCEIVE_SHADOW_PROFILE_EDGE", ".cu-perceive-trial-edge-profile",
               (r"%LOCALAPPDATA%\Microsoft\Edge\User Data", r"%LOCALAPPDATA%\Microsoft\Edge Beta\User Data",
                r"%LOCALAPPDATA%\Microsoft\Edge Dev\User Data", r"%LOCALAPPDATA%\Microsoft\Edge SxS\User Data")),
}
UDD = "--user-data-dir"


class ProfileRefused(ValueError):
    """A browser launch that would use the main profile (or an ambiguous one)."""


def list_apps() -> list[dict]:
    path = config.apps_file()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(_expand(str(p))))).rstrip("\\/")


def normalize_args(args) -> list[str]:
    """args as a list of strings. A string is split on whitespace with Windows-style quotes kept together."""
    if args is None or args == "" or args == []:
        return []
    if isinstance(args, str):
        import shlex

        parts = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a
                 for a in shlex.split(args, posix=False)]
    elif isinstance(args, (list, tuple)):
        parts = list(args)
    else:
        raise ValueError("args must be a list of strings (or one string)")
    if len(parts) > MAX_ARGS:
        raise ValueError(f"at most {MAX_ARGS} args")
    out = []
    for a in parts:
        if not isinstance(a, str) or isinstance(a, bool):
            raise ValueError("every arg must be a string")
        if "\x00" in a or len(a) > MAX_ARG_LEN:
            raise ValueError("arg contains NUL or is too long")
        if a:
            out.append(a)
    return out


def browser_kind(exe: str | Path) -> str | None:
    """'chrome' / 'edge' for chrome.exe / msedge.exe, else None."""
    row = BROWSERS.get(Path(str(exe)).stem.lower())
    return row[0] if row else None


def shadow_profile(exe: str | Path) -> Path | None:
    row = BROWSERS.get(Path(str(exe)).stem.lower())
    if not row:
        return None
    _, env, default, _ = row
    return Path(_expand(os.environ.get(env) or str(Path.home() / default)))


def main_profile_dirs(exe: str | Path) -> list[str]:
    row = BROWSERS.get(Path(str(exe)).stem.lower())
    return [_norm(p) for p in row[3]] if row else []


def guard_browser_args(exe: str | Path, args: list[str], user_data_dir: str | None = None) -> tuple[list[str], dict]:
    """Return (argv tail, guard info). Non-browsers pass through unchanged.

    Raises ProfileRefused for the main profile, a bare ``--user-data-dir`` without ``=``,
    or two different user-data-dirs.
    """
    kind = browser_kind(exe)
    if kind is None:
        if user_data_dir:
            raise ValueError("user_data_dir only applies to chrome / msedge")
        return list(args), {}
    given: list[str] = []
    rest: list[str] = []
    for a in args:
        low = a.lower()
        if low == UDD:
            raise ProfileRefused(f"write {UDD}=<dir> as one arg; a bare {UDD} is refused")
        if low.startswith(UDD + "="):
            given.append(a.split("=", 1)[1].strip().strip('"'))
        else:
            rest.append(a)
    if user_data_dir:
        given.append(str(user_data_dir).strip().strip('"'))
    dirs = {_norm(d) for d in given if d}
    if len(dirs) > 1:
        raise ProfileRefused("conflicting user-data-dir values")
    if any(not d for d in given):
        raise ProfileRefused(f"empty {UDD}")
    if given:
        udd, how = Path(_expand(given[0])), "explicit"
    else:
        udd, how = shadow_profile(exe), "defaulted_to_shadow"
    target = _norm(udd)
    for main in main_profile_dirs(exe):
        if target == main or target.startswith(main + os.sep):
            raise ProfileRefused(
                f"refusing to launch {kind} on its main profile ({udd}); use a shadow --user-data-dir")
    return [f"{UDD}={udd}"] + rest, {"browser": kind, "user_data_dir": str(udd), "profile_guard": how}


def _app_row(app: str) -> dict:
    for row in list_apps():
        if (row.get("name") or "").lower() == app.lower():
            return row
    raise KeyError(f"app {app!r} not in {config.apps_file()}")


def resolve(app: str | None = None, exe: str | None = None, args=None,
            user_data_dir: str | None = None) -> dict:
    """Plan a launch without starting anything: exe path, final argv, guard info."""
    rec = None
    if app:
        rec = _app_row(app)
        exe = rec.get("exe") or rec.get("app_path")
    if not exe:
        raise ValueError("launch needs --app or --exe")
    path = Path(_expand(exe))
    tail = normalize_args(rec.get("args") if rec else None) + normalize_args(args)
    tail, guard = guard_browser_args(path, tail, user_data_dir or (rec or {}).get("user_data_dir"))
    return {"exe": str(path), "app": (rec or {}).get("name"), "args": tail, **guard}


def _new_window(stem: str, before: set[int], pid: int, wait_s: float) -> dict | None:
    from .windows import find_windows

    deadline = time.monotonic() + max(wait_s, 0.0)
    while True:
        fresh = [w for w in find_windows(exe=stem) if int(w.get("hwnd") or 0) not in before]
        if fresh:
            own = [w for w in fresh if int(w.get("pid") or 0) == pid]
            return (own or fresh)[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def launch(app: str | None = None, exe: str | None = None, args=None,
           user_data_dir: str | None = None) -> dict:
    plan = resolve(app=app, exe=exe, args=args, user_data_dir=user_data_dir)
    path = Path(plan["exe"])
    if not path.exists():
        raise FileNotFoundError(str(path))
    if plan.get("user_data_dir"):
        Path(plan["user_data_dir"]).mkdir(parents=True, exist_ok=True)
    from .settings import get as _cfg
    from .windows import find_windows

    before = {int(w.get("hwnd") or 0) for w in find_windows(exe=path.stem)}
    proc = subprocess.Popen([str(path)] + plan["args"], cwd=str(path.parent))
    wait_s = float(_cfg("timing.launch_wait_s"))
    time.sleep(wait_s)
    browser = bool(plan.get("browser"))
    # Browsers start slower and reuse processes; poll a little longer for the new top-level window.
    win = _new_window(path.stem, before, proc.pid, 8.0 if browser else 1.0)
    out = {"ok": True, **plan, "pid": proc.pid, "window": win, "new_window": win is not None}
    if win is None:
        if browser:
            out["warnings"] = ["no new browser window appeared yet; do not drive an older window of the same exe"]
        else:
            from .windows import find_window
            out["window"] = find_window(exe=path.stem) or find_window(title=path.stem)
    return out
