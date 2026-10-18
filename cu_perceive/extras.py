"""Optional feature packs (OCR / UIA / YOLO). Loaded on first use, never auto-installed.

A missing extra raises MissingExtraError carrying the exact pip command for *this*
interpreter and *this* install of cu-perceive. Keep PACKAGES in sync with
[project.optional-dependencies] in pyproject.toml.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import sys
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

DIST_NAME = "cu-perceive"

# extra -> import names probed before first use
MODULES: dict[str, tuple[str, ...]] = {
    "ocr": ("rapidocr_onnxruntime", "onnxruntime"),
    "uia": ("uiautomation", "comtypes"),
    "yolo": ("ultralytics", "torch"),
}

# extra -> pip requirement strings (mirror of pyproject optional-dependencies)
PACKAGES: dict[str, tuple[str, ...]] = {
    "ocr": ("rapidocr-onnxruntime>=1.4.4", "onnxruntime>=1.18"),
    "uia": ("uiautomation>=2.0.29", "comtypes>=1.4"),
    "yolo": ("ultralytics>=8.3",),
}

PURPOSE = {
    "ocr": "OCR text ids (perceive --ocr / ocr=true)",
    "uia": "UI Automation nodes and clicks (perceive --uia, act --method uia|auto, --uia-name)",
    "yolo": "ScreenParser icon boxes (perceive --yolo / yolo=true; AGPL-3.0, large download)",
}


class MissingExtraError(ImportError):
    """An optional extra is not installed. Never auto-installs; carries the install command."""

    def __init__(self, extra: str, missing: list[str], command: str):
        self.extra = extra
        self.missing = list(missing)
        self.command = command
        super().__init__(
            f"{PURPOSE.get(extra, extra)} needs the optional '{extra}' extra "
            f"(missing: {', '.join(self.missing)}). Install it with:\n    {command}"
        )

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "error": str(self),
            "missing_extra": self.extra,
            "missing_modules": self.missing,
            "install": self.command,
        }


def _quote(s: str) -> str:
    return f'"{s}"' if any(c in s for c in ' \t&()[]{}^=;!\'+,`~') else s


def _python() -> str:
    return _quote(sys.executable or "python")


def _direct_url(dist) -> dict | None:
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _file_url_to_path(url: str) -> str:
    parsed = urlparse(url)
    path = unquote(parsed.path)
    if parsed.netloc:
        path = f"//{parsed.netloc}{path}"
    return url2pathname(path) if sys.platform == "win32" else path


def install_command(extra: str) -> str:
    """Exact pip command that adds ``extra`` to the running install."""
    if extra not in PACKAGES:
        raise KeyError(f"unknown extra {extra!r}; known: {', '.join(sorted(PACKAGES))}")
    py = _python()
    plain = f"{py} -m pip install " + " ".join(f'"{p}"' for p in PACKAGES[extra])
    try:
        dist = importlib.metadata.distribution(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        # Running from a bare checkout (PYTHONPATH): name the packages directly.
        return plain
    info = _direct_url(dist)
    if info is None:
        return f'{py} -m pip install "{DIST_NAME}[{extra}]=={dist.version}"'
    url = str(info.get("url") or "")
    if (info.get("dir_info") or {}).get("editable") and url.startswith("file:"):
        return f'{py} -m pip install -e "{_file_url_to_path(url)}[{extra}]"'
    vcs = info.get("vcs_info") or {}
    if vcs.get("vcs") == "git" and url:
        rev = vcs.get("commit_id") or vcs.get("requested_revision")
        at = f"@{rev}" if rev else ""
        return f'{py} -m pip install "{DIST_NAME}[{extra}] @ git+{url}{at}"'
    return plain


def missing_modules(extra: str) -> list[str]:
    out = []
    for mod in MODULES[extra]:
        if mod in sys.modules and sys.modules[mod] is not None:
            continue
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            out.append(mod)
    return out


def available(extra: str) -> bool:
    return not missing_modules(extra)


def require(extra: str) -> None:
    """Raise MissingExtraError (with the install command) if ``extra`` is not importable."""
    missing = missing_modules(extra)
    if missing:
        raise MissingExtraError(extra, missing, install_command(extra))


def missing_from_import_error(extra: str, exc: ImportError) -> MissingExtraError:
    """Wrap an ImportError raised while loading an extra's modules."""
    name = getattr(exc, "name", None) or str(exc)
    return MissingExtraError(extra, [name], install_command(extra))
