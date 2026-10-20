"""Typed runtime configuration. Built-in defaults match the values that used to be
hard-coded. Precedence, lowest to highest:

1. built-in defaults
2. the selected profile's built-in overrides (``default`` has none)
3. the optional TOML file's base tables
4. that file's ``[profile.<name>.*]`` tables
5. existing ``CU_PERCEIVE_*`` environment variables (names and semantics kept)
6. per-call arguments (function parameters; not stored here)

The file is read on first use and on ``reload()``. A failed load or reload keeps the
last good file. Environment variables are read on each lookup, as they were before
this module existed. There is no file watcher.

Secrets are not accepted in the file. ``config show`` redacts any value marked secret.
Locked keys cannot be set in the file. Tighten-only keys accept a file or profile
value only when it is stricter than the built-in default; legacy env vars are not
tightened, so existing overrides keep working.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

ENV_CONFIG = "CU_PERCEIVE_CONFIG"
ENV_PROFILE = "CU_PERCEIVE_PROFILE"

_REQUIRE_GO = frozenset({"1", "true", "yes", "on"})
_OFF = frozenset({"0", "off", "none", "inf"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SECRET_LEAVES = frozenset({"secret", "token", "password", "api_key", "apikey"})


class ConfigError(ValueError):
    """The TOML file, a profile name, or a value is not acceptable."""


@dataclass(frozen=True)
class Param:
    """One tunable. ``key`` is ``section.name``."""

    key: str
    default: Any
    kind: str  # float, int, str, bool, timeout
    policy: str = "open"  # open, locked, tighten
    tighten: str | None = None  # "le" (smaller/equal) or "ge" (larger/equal)
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    env: str | None = None
    env_mode: str | None = None
    secret: bool = False
    unit: str = ""

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.key.split(".", 1)[1]


def _p(key: str, default: Any, kind: str, **kwargs: Any) -> Param:
    return Param(key, default, kind, **kwargs)


# Defaults are today's values. Ranges reject a file that cannot be what the code
# already accepts; they are not new behavior when the file is absent.
_PARAMS: tuple[Param, ...] = (
    _p("timing.act_gap_s", 0.12, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("timing.type_gap_s", 0.012, "float", minimum=0.0, maximum=1.0, unit="s"),
    _p("timing.click_pre_s", 0.05, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("timing.click_hold_s", 0.0, "float", minimum=0.0, maximum=30.0, unit="s"),
    _p("timing.click_double_gap_s", 0.08, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("timing.hover_linger_s", 0.15, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("timing.scroll_pre_s", 0.04, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("timing.post_down_s", 0.035, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("timing.post_up_s", 0.05, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("timing.tap_combo_s", 0.015, "float", minimum=0.0, maximum=1.0, unit="s"),
    _p("timing.paste_before_s", 0.05, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("timing.paste_chord_s", 0.02, "float", minimum=0.0, maximum=1.0, unit="s"),
    _p("timing.paste_after_s", 0.08, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("timing.launch_wait_s", 1.2, "float", minimum=0.0, maximum=30.0, unit="s"),
    _p("timing.keyboard_settle_tries", 10, "int", minimum=1, maximum=100, unit="count"),
    _p("timing.keyboard_settle_s", 0.05, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("drag.step_px", 4, "int", minimum=1, maximum=100, unit="px"),
    _p("drag.path_step_px", 2, "int", minimum=1, maximum=100, unit="px"),
    _p("drag.gap_s", 0.004, "float", minimum=0.0, maximum=0.1, unit="s"),
    _p("drag.hold_s", 0.06, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("drag.hold_floor_s", 0.08, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("drag.pre_down_s", 0.06, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("drag.pre_up_s", 0.04, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("drag.xy_steps", 12, "int", minimum=2, maximum=200, unit="count"),
    _p("focus.settle_s", 0.15, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("focus.restore_settle_s", 0.30, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("focus.refresh_settle_s", 0.15, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("focus.verify_settle_s", 0.15, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("focus.baseline_extra_s", 0.15, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("focus.default", False, "bool", policy="locked"),
    _p("ime.verify_budget_s", 0.75, "float", minimum=0.0, maximum=30.0, unit="s"),
    _p("ime.restore_tries", 7, "int", minimum=1, maximum=50, unit="count"),
    _p("ime.restore_sleep_s", 0.05, "float", minimum=0.0, maximum=1.0, unit="s"),
    _p("popup.new_window_wait_s", 1.2, "float", minimum=0.0, maximum=30.0, unit="s",
       env="CU_PERCEIVE_NEW_WINDOW_WAIT_MS", env_mode="ms"),
    _p("popup.recheck_s", 0.3, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("popup.chain_step_s", 0.25, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("popup.poll_s", 0.05, "float", minimum=0.001, maximum=1.0, unit="s"),
    _p("change.pixel_threshold", 12, "int", minimum=0, maximum=255, unit="level"),
    _p("change.min_pixels", 8, "int", minimum=0, maximum=1_000_000, unit="px"),
    _p("change.reflow_ratio", 0.3, "float", minimum=0.0, maximum=1.0, unit="ratio"),
    _p("change.reflow_bbox_share", 0.7, "float", minimum=0.0, maximum=1.0, unit="ratio"),
    _p("change.reflow_min_ratio", 0.02, "float", minimum=0.0, maximum=1.0, unit="ratio"),
    _p("change.reflow_bbox_share_mid", 0.4, "float", minimum=0.0, maximum=1.0, unit="ratio"),
    _p("change.reflow_min_ratio_mid", 0.1, "float", minimum=0.0, maximum=1.0, unit="ratio"),
    _p("change.min_ratio", 0.001, "float", minimum=0.0, maximum=1.0, unit="ratio",
       env="CU_PERCEIVE_CHANGE_MIN_RATIO", env_mode="ratio"),
    _p("change.row_band_min", 3, "int", minimum=1, maximum=100, unit="count"),
    _p("change.row_band_min_ratio", 0.01, "float", minimum=0.0, maximum=1.0, unit="ratio"),
    _p("change.row_band_gap_px", 2, "int", minimum=0, maximum=50, unit="px"),
    _p("change.row_band_min_width", 0.15, "float", minimum=0.0, maximum=1.0, unit="ratio"),
    _p("change.row_band_similar_lo", 0.6, "float", minimum=0.0, maximum=10.0, unit="factor"),
    _p("change.row_band_similar_hi", 1.6, "float", minimum=0.0, maximum=10.0, unit="factor"),
    _p("change.minor_ratio", 0.005, "float", minimum=0.0, maximum=1.0, unit="ratio"),
    _p("change.step", 1, "int", minimum=1, maximum=16, unit="px"),
    _p("change.wait_s", 0.35, "float", minimum=0.0, maximum=30.0, unit="s",
       env="CU_PERCEIVE_CHANGE_WAIT_MS", env_mode="ms_float"),
    _p("capture.backend", "auto", "str", choices=("auto", "wgc", "dwm", "printwindow", "screen"),
       env="CU_PERCEIVE_CAPTURE", env_mode="capture"),
    _p("capture.content_threshold", 8, "int", policy="tighten", tighten="ge", minimum=0, maximum=255, unit="level"),
    _p("capture.content_step", 4, "int", minimum=1, maximum=64, unit="px"),
    _p("capture.frame_slop_px", 2, "int", minimum=0, maximum=100, unit="px"),
    _p("capture.wgc_timeout_s", 1.5, "float", minimum=0.05, maximum=60.0, unit="s"),
    _p("capture.dwm_settle_s", 0.012, "float", minimum=0.0, maximum=1.0, unit="s"),
    _p("ocr.yolo_conf", 0.10, "float", minimum=0.0, maximum=1.0, unit="ratio"),
    _p("ocr.yolo_iou", 0.10, "float", minimum=0.0, maximum=1.0, unit="ratio"),
    _p("ocr.yolo_imgsz", 1280, "int", minimum=32, maximum=4096, unit="px"),
    _p("ocr.yolo_verbose", False, "bool"),
    _p("ocr.timeout_s", 30.0, "timeout", minimum=0.0, maximum=600.0, unit="s",
       env="CU_PERCEIVE_DETECT_TIMEOUT", env_mode="detect_timeout"),
    _p("ocr.first_call_factor", 3, "int", minimum=1, maximum=20, unit="factor"),
    _p("ocr.yolo_min_bytes", 1_000_000, "int", minimum=1, maximum=2_000_000_000, unit="bytes"),
    _p("ocr.worker_kill_s", 5.0, "float", minimum=0.1, maximum=60.0, unit="s"),
    _p("ocr.worker_close_s", 2.0, "float", minimum=0.1, maximum=60.0, unit="s"),
    _p("uia.max_nodes", 200, "int", minimum=1, maximum=5000, unit="count"),
    _p("uia.max_depth", 16, "int", minimum=1, maximum=64, unit="count"),
    _p("uia.walk_timeout_s", 20.0, "timeout", minimum=0.0, maximum=600.0, unit="s",
       env="CU_PERCEIVE_UIA_WALK_TIMEOUT", env_mode="detect_timeout"),
    _p("uia.value_max", 120, "int", minimum=1, maximum=10000, unit="chars"),
    _p("uia.content_max", 80, "int", minimum=0, maximum=10000, unit="count"),
    _p("uia.occluded_names_max", 30, "int", minimum=0, maximum=1000, unit="count"),
    _p("server.inline_max_side", 1280, "int", minimum=64, maximum=8192, unit="px"),
    _p("server.jpeg_quality", 80, "int", minimum=1, maximum=95, unit="level"),
    _p("server.port", 8771, "int", minimum=1, maximum=65535, unit="port"),
    _p("server.host", "127.0.0.1", "str", policy="tighten", choices=("127.0.0.1", "localhost", "::1")),
    _p("bsk.timeout_s", 120.0, "float", policy="tighten", tighten="le", minimum=0.1, maximum=3600.0, unit="s",
       env="CU_PERCEIVE_BSK_TIMEOUT", env_mode="bsk_timeout"),
    _p("bsk.command_slack_s", 15.0, "float", minimum=0.0, maximum=120.0, unit="s"),
    _p("bsk.require_go", False, "bool", policy="locked", env="CU_PERCEIVE_BSK_REQUIRE_GO", env_mode="require_go"),
    _p("limits.max_steps", 20, "int", policy="tighten", tighten="le", minimum=1, maximum=20, unit="count"),
    _p("limits.max_scroll_notches", 50, "int", policy="tighten", tighten="le", minimum=1, maximum=50, unit="count"),
    _p("stamp.stale_sec", 120.0, "float", policy="tighten", tighten="le", minimum=0.0, maximum=120.0, unit="s"),
    _p("stamp.origin_slop_px", 8, "int", policy="tighten", tighten="le", minimum=0, maximum=8, unit="px"),
    _p("stamp.size_slop_px", 24, "int", policy="tighten", tighten="le", minimum=0, maximum=24, unit="px"),
    _p("safety.min_client_px", 8, "int", policy="tighten", tighten="ge", minimum=1, maximum=2000, unit="px"),
    _p("safety.small_client_px", 80, "int", policy="tighten", tighten="ge", minimum=1, maximum=4000, unit="px"),
    _p("safety.act_dry_run_default", True, "bool", policy="locked"),
    _p("safety.unknown_args", "refuse", "str", policy="locked", choices=("refuse",)),
    _p("safety.password_readback", "skip", "str", policy="locked", choices=("skip",)),
    _p("safety.elevated_target", "refuse", "str", policy="locked", choices=("refuse",)),
    _p("safety.norm_scale", 1000, "int", policy="locked"),
    _p("safety.detect_inproc", False, "bool", policy="locked",
       env="CU_PERCEIVE_DETECT_INPROC", env_mode="inproc"),
    _p("safety.clipboard_inprocess", False, "bool", policy="locked",
       env="CU_PERCEIVE_CLIPBOARD_INPROCESS", env_mode="exact_1"),
    _p("clipboard.open_tries", 8, "int", minimum=1, maximum=50, unit="count"),
    _p("clipboard.open_base_s", 0.02, "float", minimum=0.0, maximum=2.0, unit="s"),
    _p("clipboard.open_max_s", 0.20, "float", minimum=0.0, maximum=5.0, unit="s"),
    _p("clipboard.worker_timeout_s", 8.0, "float", minimum=0.5, maximum=120.0, unit="s"),
    _p("perception.roi_half", 150, "int", minimum=1, maximum=500, unit="norm"),
    _p("paths.wsl_distro", "Ubuntu", "str", env="CU_PERCEIVE_WSL_DISTRO", env_mode="plain"),
)

PARAMS: dict[str, Param] = {p.key: p for p in _PARAMS}
SECTIONS: tuple[str, ...] = tuple(dict.fromkeys(p.section for p in _PARAMS))

# Opt-in profiles. ``default`` is the built-in table above. These only apply when
# selected, so an unset profile leaves every default unchanged.
BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "default": {},
    "demo-slow-human": {"drag.gap_s": 0.012},
    "fast": {"drag.gap_s": 0.002, "timing.act_gap_s": 0.06},
}

_lock = threading.Lock()
_file_base: dict[str, Any] = {}
_file_profiles: dict[str, dict[str, Any]] = {}
_file_error: str | None = None
_file_path: str | None = None
_loaded = False
_generation = 0
_cache_sig: tuple | None = None
_cache_map: dict[str, Any] | None = None
_cache_src: dict[str, str] | None = None
_profile_error: str | None = None
_process_profile: str | None = None
_call_profile: ContextVar[str | None] = ContextVar("cu_perceive_call_profile", default=None)
_call_show: ContextVar[bool] = ContextVar("cu_perceive_show_config", default=False)


def params() -> dict[str, Param]:
    return PARAMS


def known_profiles() -> list[str]:
    names = list(BUILTIN_PROFILES)
    for name in _file_profiles:
        if name not in names:
            names.append(name)
    return names


def config_file_path() -> Path:
    """Where the TOML file is looked up. Missing default path means "no file"."""
    raw = os.environ.get(ENV_CONFIG, "").strip().strip('"')
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw)))
    return _platform_data_root() / "config.toml"


def _platform_data_root() -> Path:
    """Data dir used to find config.toml. Does not read the file (that would recurse)."""
    import sys

    raw = os.environ.get("CU_PERCEIVE_HOME", "").strip().strip('"')
    if raw:
        return Path(os.path.expandvars(os.path.expanduser(raw)))
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "cu-perceive"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "cu-perceive"


def set_process_profile(name: str | None) -> None:
    """CLI ``--profile``. Empty clears it. Unknown names raise."""
    global _process_profile, _cache_sig
    cleaned = (name or "").strip() or None
    if cleaned is not None and cleaned not in known_profiles():
        raise ConfigError(f"unknown profile {cleaned!r}; known: {', '.join(known_profiles())}")
    _process_profile = cleaned
    _cache_sig = None


def active_profile() -> str:
    chosen = _call_profile.get() or _process_profile or os.environ.get(ENV_PROFILE, "").strip() or "default"
    if chosen not in known_profiles():
        return "default"
    return chosen


def profile_error() -> str | None:
    chosen = _call_profile.get() or _process_profile or os.environ.get(ENV_PROFILE, "").strip() or "default"
    if chosen not in known_profiles():
        return f"unknown profile {chosen!r}; using default. Known: {', '.join(known_profiles())}"
    return _profile_error


@contextmanager
def call_context(*, profile: str | None = None, show_config: bool = False) -> Iterator[None]:
    """Per-call profile and whether the response should include the full config."""
    prof = (profile or "").strip() or None
    if prof is not None and prof not in known_profiles():
        raise ConfigError(f"unknown profile {prof!r}; known: {', '.join(known_profiles())}")
    token_p = _call_profile.set(prof) if prof is not None else None
    token_s = _call_show.set(bool(show_config))
    try:
        yield
    finally:
        _call_show.reset(token_s)
        if token_p is not None:
            _call_profile.reset(token_p)


def pick(explicit: Any, key: str) -> Any:
    """Per-call argument wins. ``None`` means "use the configured value"."""
    if explicit is not None:
        return explicit
    return get(key)


def get(key: str) -> Any:
    """Effective value for ``key`` (defaults, file, profile, env)."""
    spec = PARAMS.get(key)
    if spec is None:
        raise KeyError(key)
    _ensure_loaded()
    values, _sources = _effective(active_profile())
    return values[key]


def file_override(key: str) -> Any | None:
    """Value from the loaded file's base table, ignoring env and profile. None if unset."""
    _ensure_loaded()
    return _file_base.get(key)


def _ensure_loaded() -> None:
    """Load the file once. A bad file is not applied; defaults stay and ``load_error`` is set."""
    global _loaded, _file_error, _file_path
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        try:
            _read_file_into_state()
        except ConfigError as exc:
            _file_base.clear()
            _file_profiles.clear()
            _file_error = str(exc)
            _file_path = None
            _invalidate()
        _loaded = True


def reload() -> dict[str, Any]:
    """Re-read the TOML file after the current live act finishes.

    On failure the previous file stays in effect and the result is ``ok: false``.
    """
    global _loaded, _file_error, _file_path
    try:
        from .act import LIVE_ACT_LOCK
    except Exception:
        LIVE_ACT_LOCK = _lock  # type: ignore[assignment]
    with LIVE_ACT_LOCK:
        with _lock:
            previous_base = _file_base.copy()
            previous_profiles = {k: v.copy() for k, v in _file_profiles.items()}
            previous_error = _file_error
            previous_path = _file_path
            try:
                _read_file_into_state()
                _loaded = True
            except ConfigError as exc:
                _file_base.clear()
                _file_base.update(previous_base)
                _file_profiles.clear()
                _file_profiles.update(previous_profiles)
                _file_error = str(exc)
                _file_path = previous_path
                _ = previous_error
                _invalidate()
                return {"ok": False, "error": str(exc), "config_rev": revision(), "profile": active_profile()}
    return {"ok": True, "config_rev": revision(), "profile": active_profile(), "path": _file_path}


def validate() -> dict[str, Any]:
    """Load (or re-check) the file and report errors without dropping a good config."""
    path = config_file_path()
    explicit = bool(os.environ.get(ENV_CONFIG, "").strip())
    if not path.is_file():
        if explicit:
            return {"ok": False, "error": f"config file not found: {path}", "path": str(path)}
        err = profile_error()
        return {"ok": err is None, "path": str(path), "loaded": False, "error": err, "profile": active_profile()}
    try:
        _parse_toml(path.read_bytes(), source=str(path))
    except ConfigError as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}
    err = profile_error()
    return {
        "ok": err is None,
        "path": str(path),
        "loaded": True,
        "error": err,
        "profile": active_profile(),
        "config_rev": revision(),
    }


def revision(profile: str | None = None) -> str:
    """12 hex chars of the effective tunable values. Profile name is not inside the digest."""
    _ensure_loaded()
    prof = profile or active_profile()
    values, _sources = _effective(prof)
    payload = {k: _canon(v) for k, v in values.items() if not PARAMS[k].secret}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def config_rev(profile: str | None = None) -> str:
    """``<hash>@<profile>`` for responses."""
    prof = profile or active_profile()
    return f"{revision(prof)}@{prof}"


def public_view(*, profile: str | None = None, show_secrets: bool = False) -> dict[str, Any]:
    """Effective config grouped by section. Secret values render as ``<redacted>``."""
    _ensure_loaded()
    prof = profile or active_profile()
    values, sources = _effective(prof)
    grouped: dict[str, dict[str, Any]] = {}
    for spec in _PARAMS:
        shown = values[spec.key]
        if spec.secret and not show_secrets:
            shown = "<redacted>"
        grouped.setdefault(spec.section, {})[spec.name] = _canon(shown)
    return {
        "profile": prof,
        "config_rev": config_rev(prof),
        "path": _file_path or str(config_file_path()),
        "loaded": bool(_file_path) and _file_error is None and _loaded,
        "load_error": _file_error,
        "profile_error": profile_error(),
        "profiles": known_profiles(),
        "values": grouped,
        "sources": sources,
        "locked": [p.key for p in _PARAMS if p.policy == "locked"],
        "tighten_only": [p.key for p in _PARAMS if p.policy == "tighten"],
    }


def finish_response(payload: dict, *, show_config: bool | None = None) -> dict:
    """Add ``config_rev`` to a response dict. Existing keys are left as they are.

    ``show_config`` true (or the per-call flag) adds ``config`` with the effective
    values. A list response is not a dict and is not modified by callers.
    """
    if not isinstance(payload, dict):
        return payload
    show = _call_show.get() if show_config is None else bool(show_config)
    payload["config_rev"] = config_rev()
    if show:
        payload["config"] = public_view()
    return payload


def config_action(action: str = "show", *, profile: str | None = None) -> dict:
    """``show``, ``reload``, or ``validate``."""
    name = (action or "show").strip().lower()
    if name not in ("show", "reload", "validate"):
        return {
            "ok": False,
            "reason": "bad_args",
            "error": f"config action must be show, reload, or validate (got {action!r})",
        }
    if profile:
        if profile not in known_profiles():
            return {
                "ok": False,
                "reason": "bad_args",
                "error": f"unknown profile {profile!r}; known: {', '.join(known_profiles())}",
            }
    if name == "reload":
        out = reload()
        if profile:
            out["profile"] = profile
            out["config_rev"] = config_rev(profile)
        out["config"] = public_view(profile=profile)
        return out
    if name == "validate":
        out = validate()
        if profile and profile not in known_profiles():
            out["ok"] = False
        return out
    view = public_view(profile=profile)
    view["ok"] = not view.get("load_error") and not view.get("profile_error")
    return view


def reset_for_tests() -> None:
    """Drop the loaded file and caches. Tests only."""
    global _loaded, _file_error, _file_path, _generation, _process_profile, _profile_error
    with _lock:
        _file_base.clear()
        _file_profiles.clear()
        _file_error = None
        _file_path = None
        _loaded = False
        _generation += 1
        _process_profile = None
        _profile_error = None
        _invalidate()


def _invalidate() -> None:
    global _cache_sig, _cache_map, _cache_src, _generation
    _cache_sig = None
    _cache_map = None
    _cache_src = None
    _generation += 1


def _read_file_into_state() -> None:
    """Replace file state. Raises ConfigError; caller may roll back."""
    global _file_error, _file_path, _loaded
    path = config_file_path()
    explicit = bool(os.environ.get(ENV_CONFIG, "").strip())
    _invalidate()
    if not path.is_file():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        _file_base.clear()
        _file_profiles.clear()
        _file_error = None
        _file_path = None
        return
    base, profiles = _parse_toml(path.read_bytes(), source=str(path))
    _file_base.clear()
    _file_base.update(base)
    _file_profiles.clear()
    _file_profiles.update(profiles)
    _file_error = None
    _file_path = str(path)


def _parse_toml(data: bytes, *, source: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    import tomllib

    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{source}: config file is not utf-8 ({exc})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ConfigError(f"{source}: config root must be a table")
    base: dict[str, Any] = {}
    profiles: dict[str, dict[str, Any]] = {}
    for section, body in doc.items():
        leaf = str(section).rsplit(".", 1)[-1].lower()
        if leaf in _SECRET_LEAVES or str(section).lower() in _SECRET_LEAVES:
            raise ConfigError(f"{source}: secrets are not allowed in the config file ({section})")
        if section == "profile":
            if not isinstance(body, dict):
                raise ConfigError(f"{source}: [profile] must be a table of profile names")
            for pname, pbody in body.items():
                profiles[str(pname)] = _parse_profile(pbody, source=source, profile=str(pname))
            continue
        if section not in SECTIONS:
            raise ConfigError(f"{source}: unknown section [{section}]")
        if not isinstance(body, dict):
            raise ConfigError(f"{source}: [{section}] must be a table")
        for name, raw in body.items():
            key = f"{section}.{name}"
            _reject_secret_leaf(str(name), source)
            base[key] = _coerce_file_value(key, raw, source=source)
    for pname, overrides in profiles.items():
        for key, raw in overrides.items():
            _coerce_file_value(key, raw, source=f"{source} profile {pname}")
    for key, raw in {**BUILTIN_PROFILES.get("demo-slow-human", {}), **BUILTIN_PROFILES.get("fast", {})}.items():
        _coerce_file_value(key, raw, source="built-in profile")
    return base, profiles


def _parse_profile(body: Any, *, source: str, profile: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ConfigError(f"{source}: profile {profile} must be a table")
    out: dict[str, Any] = {}
    for section, table in body.items():
        if str(section).lower() in _SECRET_LEAVES:
            raise ConfigError(f"{source}: secrets are not allowed in the config file ({profile}.{section})")
        if section not in SECTIONS:
            raise ConfigError(f"{source}: unknown section [profile.{profile}.{section}]")
        if not isinstance(table, dict):
            raise ConfigError(f"{source}: [profile.{profile}.{section}] must be a table")
        for name, raw in table.items():
            _reject_secret_leaf(str(name), source)
            out[f"{section}.{name}"] = raw
    return out


def _reject_secret_leaf(name: str, source: str) -> None:
    if name.lower().replace("-", "_") in _SECRET_LEAVES:
        raise ConfigError(f"{source}: secrets are not allowed in the config file ({name})")


def _coerce_file_value(key: str, raw: Any, *, source: str) -> Any:
    spec = PARAMS.get(key)
    if spec is None:
        raise ConfigError(f"{source}: unknown key {key}")
    if spec.policy == "locked":
        raise ConfigError(f"{source}: {key} is locked and cannot be set in the config file")
    value = _coerce_kind(spec, raw, source=source)
    _check_range(spec, value, source=source)
    if spec.policy == "tighten":
        _check_tighten(spec, value, source=source)
    return value


def _coerce_kind(spec: Param, raw: Any, *, source: str) -> Any:
    if isinstance(raw, bool) and spec.kind != "bool":
        raise ConfigError(f"{source}: {spec.key} must be {spec.kind}, not a boolean")
    try:
        if spec.kind == "bool":
            if not isinstance(raw, bool):
                raise TypeError
            return raw
        if spec.kind == "int":
            if isinstance(raw, bool) or isinstance(raw, float) or not isinstance(raw, int):
                if isinstance(raw, float) and raw == int(raw):
                    return int(raw)
                raise TypeError
            return int(raw)
        if spec.kind in ("float", "timeout"):
            if not isinstance(raw, (int, float)) or isinstance(raw, bool):
                raise TypeError
            number = float(raw)
            if spec.kind == "timeout" and number == 0.0:
                return None
            return number
        if spec.kind == "str":
            if not isinstance(raw, str):
                raise TypeError
            text = raw.strip()
            if spec.env_mode == "capture":
                text = text.lower()
            return text
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{source}: {spec.key} must be {spec.kind} (got {raw!r})") from exc
    raise ConfigError(f"{source}: {spec.key} has unsupported kind {spec.kind}")


def _check_range(spec: Param, value: Any, *, source: str) -> None:
    if spec.choices is not None and spec.kind == "str":
        if value not in spec.choices:
            raise ConfigError(
                f"{source}: {spec.key} must be one of {'|'.join(spec.choices)} (got {value!r})"
            )
        if spec.policy == "tighten" and value not in _LOOPBACK_HOSTS and spec.key == "server.host":
            raise ConfigError(f"{source}: {spec.key} must stay on loopback")
    if value is None:
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if spec.minimum is not None and value < spec.minimum:
            raise ConfigError(f"{source}: {spec.key} {value} is below {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise ConfigError(f"{source}: {spec.key} {value} is above {spec.maximum}")


def _check_tighten(spec: Param, value: Any, *, source: str) -> None:
    if spec.tighten is None or value is None:
        if spec.key == "server.host" and value not in _LOOPBACK_HOSTS:
            raise ConfigError(f"{source}: {spec.key} must stay on loopback")
        return
    default = spec.default
    if spec.tighten == "le" and value > default:
        raise ConfigError(
            f"{source}: {spec.key} is tighten-only; {value} is looser than the built-in {default}"
        )
    if spec.tighten == "ge" and value < default:
        raise ConfigError(
            f"{source}: {spec.key} is tighten-only; {value} is looser than the built-in {default}"
        )


def _effective(profile: str) -> tuple[dict[str, Any], dict[str, str]]:
    global _cache_sig, _cache_map, _cache_src, _profile_error
    sig = (profile, _generation, _env_sig(), _call_profile.get(), _process_profile)
    if _cache_sig == sig and _cache_map is not None and _cache_src is not None:
        return _cache_map, _cache_src
    if profile not in known_profiles():
        _profile_error = f"unknown profile {profile!r}"
        profile = "default"
    else:
        _profile_error = None
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    builtin = BUILTIN_PROFILES.get(profile, {})
    file_prof = _file_profiles.get(profile, {})
    for spec in _PARAMS:
        value = spec.default
        source = "default"
        if spec.key in builtin and spec.policy != "locked":
            value = builtin[spec.key]
            source = "profile"
        if spec.key in _file_base and spec.policy != "locked":
            value = _file_base[spec.key]
            source = "file"
        if spec.key in file_prof and spec.policy != "locked":
            value = file_prof[spec.key]
            source = "profile"
        env_value, env_hit = _from_env(spec, value)
        if env_hit:
            value = env_value
            source = "env"
        values[spec.key] = value
        sources[spec.key] = source
    _cache_sig = sig
    _cache_map = values
    _cache_src = sources
    return values, sources


def _env_sig() -> tuple:
    keys = [ENV_CONFIG, ENV_PROFILE, "CU_PERCEIVE_HOME"]
    keys += [p.env for p in _PARAMS if p.env]
    return tuple(os.environ.get(k) for k in keys)


def _from_env(spec: Param, current: Any) -> tuple[Any, bool]:
    """Return (value, used_env). Invalid env matches the historical fallback."""
    if not spec.env or not spec.env_mode:
        return current, False
    mode = spec.env_mode
    if mode == "exact_1":
        # In-process is exactly "1". The clipboard child flag forces the same path
        # even when CU_PERCEIVE_CLIPBOARD_INPROCESS itself is unset.
        child = os.environ.get("CU_PERCEIVE_CLIPBOARD_CHILD") == "1"
        raw = os.environ.get(spec.env)
        if raw is None and not child:
            return current, False
        text = (raw or "").strip()
        return child or text == "1", True
    raw = os.environ.get(spec.env)
    if raw is None:
        return current, False
    text = raw.strip()
    if mode == "inproc":
        return text not in ("", "0"), True
    if mode == "require_go":
        return text.lower() in _REQUIRE_GO, True
    if text == "" and mode != "plain":
        return current, False
    if mode == "plain":
        if text == "":
            return current, False
        return text, True
    if mode == "capture":
        if text == "":
            return current, False
        return text.lower(), True
    if mode == "ms":
        low = text.lower()
        if low in ("0", "off", "none"):
            return 0.0, True
        try:
            return max(0.0, float(text) / 1000.0), True
        except ValueError:
            return spec.default, True
    if mode == "ms_float":
        try:
            return max(0.0, float(text) / 1000.0), True
        except ValueError:
            return spec.default, True
    if mode == "ratio":
        try:
            return max(0.0, float(text)), True
        except ValueError:
            return spec.default, True
    if mode == "detect_timeout":
        low = text.lower()
        if low in _OFF:
            return None, True
        try:
            val = float(text)
        except ValueError:
            return spec.default, True
        if val <= 0:
            return None, True
        return val, True
    if mode == "bsk_timeout":
        try:
            val = float(text)
        except ValueError:
            return spec.default, True
        if val <= 0:
            return spec.default, True
        return val, True
    return current, False


def _canon(value: Any) -> Any:
    if isinstance(value, float):
        return float(format(value, ".12g"))
    if isinstance(value, (tuple, list)):
        return [_canon(v) for v in value]
    return value


def _validate_builtin_profiles() -> None:
    for name, overrides in BUILTIN_PROFILES.items():
        for key, raw in overrides.items():
            _coerce_file_value(key, raw, source=f"built-in profile {name}")


_validate_builtin_profiles()
