"""Start an app by name (apps.json) or exe path, optionally with an argv list. No secrets.

apps.json: CU_PERCEIVE_APPS, else <data dir>/apps.json, else the packaged
apps.default.json. ``exe`` values may use %ENV% / ~ (expanded at launch). A row may
carry ``"args": [...]``; call-time ``args`` are appended after the row's.

Browser guard (chrome.exe / msedge.exe): a launch never lands on the browser's main
profile. With no ``--user-data-dir`` the shadow profile is used
(``CU_PERCEIVE_SHADOW_PROFILE_CHROME`` / ``CU_PERCEIVE_SHADOW_PROFILE_EDGE``, default
``~/.cu-perceive-trial-chrome-profile`` / ``~/.cu-perceive-trial-edge-profile``); a
``--user-data-dir`` that is, or is inside, the main ``User Data`` folder is refused
(ProfileRefused). Both sides are compared after filesystem canonicalization
(canonical_path): 8.3 short names, the extended-length and device prefixes, junctions and symlinks,
``..``, trailing dots / spaces and case all collapse first; UNC, device namespaces and paths
that do not resolve are refused.
"""
from __future__ import annotations

import json
import ntpath
import os
import re
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
    return ntpath.expandvars(os.path.expandvars(os.path.expanduser(p)))


def _norm(p: str | Path) -> str:
    """Comparison key for a path: canonical_path() then case-folded (kept for callers and probes)."""
    return ntpath.normcase(canonical_path(p))


# --- filesystem canonicalization -------------------------------------------------------------
# Windows reaches one directory through many spellings: 8.3 short names (USERDA~1), the \\?\ and
# \\.\ prefixes, junctions / symlinks / mount points, forward slashes, mixed case, ``..``
# segments and trailing dots or spaces (Win32 drops those). The guard compares canonical forms:
# both the candidate and every main-profile root go through canonical_path(). The filesystem hooks
# below are module attributes so tests can fake them off Windows.

_DEVICE_PREFIXES = (
    ("\\\\?\\unc\\", "\\\\"),
    ("\\\\.\\unc\\", "\\\\"),
    ("\\??\\unc\\", "\\\\"),
    ("\\\\?\\", ""),
    ("\\\\.\\", ""),
    ("\\??\\", ""),
)
_DRIVE = re.compile(r"[A-Za-z]:")


def _strip_device_prefix(s: str) -> str:
    low = s.lower()
    for pre, repl in _DEVICE_PREFIXES:
        if low.startswith(pre):
            return repl + s[len(pre):]
    return s


def _fs_exists(p: str) -> bool:
    return os.path.lexists(p)


def _fs_long_path(p: str) -> str:
    """GetLongPathNameW: expand 8.3 short components of an existing path (identity off Windows)."""
    if os.name != "nt":
        return p
    import ctypes

    fn = ctypes.windll.kernel32.GetLongPathNameW
    size = 512
    while True:
        buf = ctypes.create_unicode_buffer(size)
        n = fn(p, buf, size)
        if n == 0:
            raise OSError(ctypes.get_last_error() or 2, "GetLongPathNameW failed", p)
        if n < size:
            return buf.value
        size = n + 1


def _fs_realpath(p: str) -> str:
    """Follow junctions / symlinks / mount points of an existing path; raises when it cannot."""
    return os.path.realpath(p, strict=True)


def _fs_abspath(p: str) -> str:
    if os.name == "nt":
        return os.path.abspath(p)
    return p if ntpath.isabs(p) and ntpath.splitdrive(p)[0] else ntpath.join("C:\\", p)


def canonical_path(p: str | Path) -> str:
    """Absolute, long-name, link-resolved Windows path, case preserved.

    Raises ProfileRefused when the path cannot be resolved safely (UNC / network, volume GUID or
    other device namespaces, drive-relative ``C:x``, alternate data streams, a link that does not
    resolve, no existing ancestor).
    """
    raw = _expand(str(p)).strip().strip('"').strip()
    if not raw:
        raise ProfileRefused(f"empty {UDD}")
    if "\x00" in raw:
        raise ProfileRefused("path contains NUL")
    unslashed = raw.replace("/", "\\")
    s = _strip_device_prefix(unslashed)
    if s.startswith("\\\\"):
        raise ProfileRefused(f"UNC / network path {raw!r}: cannot prove it is not the main profile")
    if s != unslashed and not (_DRIVE.fullmatch(s[:2]) and s[2:3] == "\\"):
        # \\?\Volume{GUID}\..., \\.\GLOBALROOT\..., \\?\C: (no separator): not a plain drive path
        raise ProfileRefused(f"unsupported device path {raw!r}")
    drive, rest = ntpath.splitdrive(s)
    if drive and not _DRIVE.fullmatch(drive):
        raise ProfileRefused(f"unsupported path namespace {raw!r}")
    if drive and not rest.startswith("\\"):
        raise ProfileRefused(f"drive-relative path {raw!r}; pass an absolute path")
    if ":" in rest:
        raise ProfileRefused(f"alternate data stream or bad character in {raw!r}")
    s = ntpath.normpath(_fs_abspath(s))  # Win32 resolves ``..`` lexically, before any link
    d, rest = ntpath.splitdrive(s)
    parts = [c for c in rest.split("\\") if c]
    clean = []
    for c in parts:
        t = c.rstrip(" .")  # Win32 drops trailing dots and spaces from each component
        if not t:
            raise ProfileRefused(f"path component {c!r} in {raw!r}")
        clean.append(t)
    s = d + "\\" + "\\".join(clean)
    head, tail = s, []
    while not _fs_exists(head):
        parent = ntpath.dirname(head)
        if parent == head:
            raise ProfileRefused(f"no existing ancestor for {raw!r}")
        tail.insert(0, ntpath.basename(head))
        head = parent
    try:
        resolved = _fs_realpath(_fs_long_path(head))
    except (OSError, ValueError, RuntimeError) as e:
        raise ProfileRefused(f"cannot resolve {head!r} ({e.__class__.__name__}); refusing") from e
    resolved = _strip_device_prefix(resolved.replace("/", "\\"))
    if resolved.startswith("\\\\"):
        raise ProfileRefused(f"{raw!r} resolves to a network path; cannot prove it is not the main profile")
    rd = ntpath.splitdrive(resolved)[0]
    if not _DRIVE.fullmatch(rd or ""):
        raise ProfileRefused(f"{raw!r} resolves outside a drive letter path ({resolved!r})")
    out = ntpath.join(resolved, *tail) if tail else resolved
    return out.rstrip("\\") if len(out) > 3 else out


def _key(p: str) -> str:
    return ntpath.normcase(p).rstrip("\\")


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


# chrome_proxy.exe / msedge_proxy.exe forward their command line to the browser.
_STEM_ALIASES = {"chrome_proxy": "chrome", "msedge_proxy": "msedge"}


def _stem(exe: str | Path) -> str:
    name = ntpath.basename(str(exe).replace("/", "\\")).rstrip(" .").lower()
    stem = name[:-4] if name.endswith(".exe") else name
    return _STEM_ALIASES.get(stem, stem)


def _browser_row(exe: str | Path):
    row = BROWSERS.get(_stem(exe))
    if row is None:
        try:  # a renamed link / junctioned copy of chrome.exe still is chrome
            real = _fs_realpath(_expand(str(exe)))
            row = BROWSERS.get(_stem(real))
        except (OSError, ValueError, RuntimeError):
            row = None
    return row


def browser_kind(exe: str | Path) -> str | None:
    """'chrome' / 'edge' for chrome.exe / msedge.exe (and their _proxy launchers), else None."""
    row = _browser_row(exe)
    return row[0] if row else None


def shadow_profile(exe: str | Path) -> Path | None:
    row = _browser_row(exe)
    if not row:
        return None
    _, env, default, _ = row
    return Path(_expand(os.environ.get(env) or str(Path.home() / default)))


def _local_appdata_roots() -> list[str]:
    roots = []
    env = os.environ.get("LOCALAPPDATA")
    if env:
        roots.append(env)
    if os.name == "nt":
        try:  # FOLDERID_LocalAppData, in case LOCALAPPDATA was changed for this process
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                            ("d", ctypes.c_ubyte * 8)]

            fid = GUID(0xF1B32785, 0x6FBA, 0x4FCF, (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91))
            out = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(out)) == 0:
                roots.append(out.value)
                ctypes.windll.ole32.CoTaskMemFree(out)
        except Exception:  # pragma: no cover - best effort; LOCALAPPDATA still applies
            pass
    return roots


def main_profile_dirs(exe: str | Path) -> list[str]:
    """Canonical keys of the browser's main User Data roots (same canonicalization as candidates)."""
    row = _browser_row(exe)
    if not row:
        return []
    out: list[str] = []
    for root in _local_appdata_roots():
        for rel in row[3]:
            key = _key(canonical_path(rel.replace("%LOCALAPPDATA%", root)))
            if key not in out:
                out.append(key)
    return out


# Chromium on Windows accepts --switch, -switch and /switch, and lowercases switch names.
_UDD_SWITCH = re.compile(r"^(?:--|-|/)user-data-dir(?:=(.*))?$", re.IGNORECASE | re.DOTALL)
_UDD_ANYWHERE = re.compile(r"(?:--|-|/)user-data-dir(?:\s*=\s*(\"[^\"]*\"|'[^']*'|\S+))?", re.IGNORECASE)
_SHELLS = {"cmd", "powershell", "pwsh", "explorer", "wscript", "cscript", "rundll32", "conhost", "wt",
           "start", "mshta", "bash", "wsl"}
_BROWSER_TOKEN = re.compile(r"(?:^|[\\/\s\"'])(?:chrome|msedge|chrome_proxy|msedge_proxy)(?:\.exe)?(?=$|[\s\"'])",
                            re.IGNORECASE)


def _refuse_if_main(kind: str, path: str, mains: list[str]) -> str:
    canon = canonical_path(path)
    target = _key(canon)
    for main in mains:
        if target == main or target.startswith(main + "\\"):
            raise ProfileRefused(
                f"refusing to launch {kind} on its main profile ({path}); use a shadow --user-data-dir")
    return canon


def guard_browser_args(exe: str | Path, args: list[str], user_data_dir: str | None = None) -> tuple[list[str], dict]:
    """Return (argv tail, guard info).

    chrome / msedge: the user-data-dir is canonicalized (8.3 names, \\\\?\\ / \\\\.\\ prefixes,
    junctions and symlinks, ``..``, trailing dots / spaces, case) and refused when it is, or is
    inside, a main User Data root canonicalized the same way, or when it cannot be resolved
    safely. ``--``, ``-`` and ``/`` switch spellings all count. With none given the shadow dir is
    used. The argv carries the resolved path.

    Other exes: an embedded user-data-dir switch is checked the same way, and a shell
    (cmd / powershell / explorer ...) whose args name chrome or msedge is refused, since the guard
    cannot add a shadow profile to that inner launch.
    """
    kind = browser_kind(exe)
    if kind is None:
        if user_data_dir:
            raise ValueError("user_data_dir only applies to chrome / msedge")
        _guard_other(exe, args)
        return list(args), {}
    mains = main_profile_dirs(exe)
    given: list[str] = []
    rest: list[str] = []
    after_dashdash = False
    for a in args:
        if a == "--":
            after_dashdash = True
        m = None if after_dashdash else _UDD_SWITCH.match(a.strip())
        if m:
            if m.group(1) is None:
                raise ProfileRefused(f"write {UDD}=<dir> as one arg; a bare {UDD} is refused")
            given.append(m.group(1).strip().strip('"').strip("'"))
        else:
            if after_dashdash and _UDD_ANYWHERE.search(a):
                raise ProfileRefused("user-data-dir after '--' is refused")
            rest.append(a)
    if user_data_dir:
        given.append(str(user_data_dir).strip().strip('"'))
    if any(not d for d in given):
        raise ProfileRefused(f"empty {UDD}")
    if given:
        canon = [_refuse_if_main(kind, d, mains) for d in given]
        if len({_key(c) for c in canon}) > 1:
            raise ProfileRefused("conflicting user-data-dir values")
        udd, how = canon[0], "explicit"
    else:
        udd, how = _refuse_if_main(kind, str(shadow_profile(exe)), mains), "defaulted_to_shadow"
    return [f"{UDD}={udd}"] + rest, {"browser": kind, "user_data_dir": str(udd), "profile_guard": how}


def _guard_other(exe: str | Path, args: list[str]) -> None:
    values: list[str] = []
    for a in args:
        whole = _UDD_SWITCH.match(a.strip())
        if whole:
            if whole.group(1) is None:
                raise ProfileRefused("a bare user-data-dir switch inside another program's args is refused")
            values.append(whole.group(1))
            continue
        for m in _UDD_ANYWHERE.finditer(a):
            if not m.group(1):
                raise ProfileRefused("a user-data-dir switch without a value inside another program's args is refused")
            values.append(m.group(1))
            values.append(a[m.start(1):])  # unquoted value with spaces: check the rest of the arg too
    for val in values:
        val = val.strip().strip('"').strip("'").strip()
        if not val:
            raise ProfileRefused("empty user-data-dir inside another program's args")
        for kind_exe in ("chrome.exe", "msedge.exe"):  # fail closed: unresolvable counts as refused
            _refuse_if_main(browser_kind(kind_exe) or "browser", val, main_profile_dirs(kind_exe))
    joined = " ".join(args)
    if _stem(exe) in _SHELLS and _BROWSER_TOKEN.search(" " + joined + " "):
        raise ProfileRefused(
            f"{_stem(exe)} would start chrome / msedge without the profile guard; launch the browser exe directly")


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
