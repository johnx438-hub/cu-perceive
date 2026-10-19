"""Runtime paths and host settings. Nothing machine-specific is hard-coded; every
value has a portable default and an environment-variable override."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ENV_HOME = "CU_PERCEIVE_HOME"
ENV_OUT = "CU_PERCEIVE_OUT"
ENV_WEIGHTS = "CU_PERCEIVE_WEIGHTS"
ENV_YOLO_WEIGHT = "CU_PERCEIVE_YOLO_WEIGHT"
ENV_APPS = "CU_PERCEIVE_APPS"
ENV_ALLOWED_HOSTS = "CU_PERCEIVE_ALLOWED_HOSTS"
ENV_WSL_DISTRO = "CU_PERCEIVE_WSL_DISTRO"
ENV_CAPTURE = "CU_PERCEIVE_CAPTURE"

PACKAGE_DIR = Path(__file__).resolve().parent
CHECKOUT_DIR = PACKAGE_DIR.parent
DEFAULT_APPS_FILE = PACKAGE_DIR / "apps.default.json"


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip().strip('"')
    return Path(os.path.expandvars(os.path.expanduser(raw))) if raw else None


def data_root() -> Path:
    """Per-user data dir: %LOCALAPPDATA%\\cu-perceive on Windows, XDG data dir elsewhere."""
    env = _env_path(ENV_HOME)
    if env:
        return env
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "cu-perceive"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "cu-perceive"


def out_dir() -> Path:
    """Where perceive writes png/map/json stamps (act reads stamps from here)."""
    return _env_path(ENV_OUT) or data_root() / "shots" / "perceive"


def weights_dir() -> Path:
    """Model weights. A source checkout's ./weights wins over the per-user dir."""
    env = _env_path(ENV_WEIGHTS)
    if env:
        return env
    checkout = CHECKOUT_DIR / "weights"
    if (CHECKOUT_DIR / "pyproject.toml").is_file() and checkout.is_dir():
        return checkout
    return data_root() / "weights"


def yolo_weight() -> Path:
    return _env_path(ENV_YOLO_WEIGHT) or weights_dir() / "screenparser" / "best.pt"


def apps_file() -> Path:
    """Quick-launch list: env override, then per-user apps.json, then the packaged default."""
    env = _env_path(ENV_APPS)
    if env:
        return env
    user = data_root() / "apps.json"
    if user.is_file():
        return user
    return DEFAULT_APPS_FILE


def allowed_hosts(extra: list[str] | tuple[str, ...] | None = None) -> list[str]:
    """Extra Host names the HTTP MCP accepts besides loopback (e.g. a tailnet name).

    Comma-separated CU_PERCEIVE_ALLOWED_HOSTS plus any passed explicitly. Empty by default.
    """
    hosts: list[str] = []
    for raw in [os.environ.get(ENV_ALLOWED_HOSTS, "")] + list(extra or []):
        for part in str(raw or "").split(","):
            h = part.strip()
            if h and h not in hosts:
                hosts.append(h)
    return hosts


def wsl_distro() -> str:
    return os.environ.get(ENV_WSL_DISTRO, "").strip() or "Ubuntu"


def default_capture() -> str:
    return os.environ.get(ENV_CAPTURE, "").strip().lower() or "auto"
