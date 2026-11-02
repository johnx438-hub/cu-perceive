"""Model weights: manifest, SHA-256 verification and download (``cu-perceive fetch-models``).

Files land in ``config.weights_dir()`` (CU_PERCEIVE_WEIGHTS, else ``<checkout>/weights`` for a
source checkout, else ``<CU_PERCEIVE_HOME>/weights``). A file whose size and SHA-256 already
match the manifest is skipped. Downloads stream into ``<name>.part`` and are renamed into place
only after size and hash check out, so a half-finished or corrupt download never looks valid.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config

MANIFEST_FILE = Path(__file__).resolve().parent / "models_manifest.json"
ENV_NO_AUTO_FETCH = "CU_PERCEIVE_NO_AUTO_FETCH"
GROUPS = ("ocr", "yolo")
TIMEOUT_S = 60.0
CHUNK = 1 << 20
YOLO_PATH = "screenparser/best.pt"

# Patched in tests; never called with a network in the test suite.
_urlopen = urllib.request.urlopen
_LOCK = threading.Lock()


class FetchError(RuntimeError):
    """A model download failed or the downloaded bytes did not match the manifest."""


@dataclass(frozen=True)
class ModelFile:
    path: str
    group: str
    size: int
    sha256: str
    url: str
    source: str = ""

    def dest(self, root: Path | None = None) -> Path:
        return Path(root if root is not None else config.weights_dir()) / Path(self.path)


def load_manifest(path: Path | None = None) -> list[ModelFile]:
    data = json.loads(Path(path or MANIFEST_FILE).read_text(encoding="utf-8"))
    out: list[ModelFile] = []
    for raw in data["files"]:
        entry = ModelFile(
            path=str(raw["path"]),
            group=str(raw["group"]),
            size=int(raw["size"]),
            sha256=str(raw["sha256"]).lower(),
            url=str(raw["url"]),
            source=str(raw.get("source", "")),
        )
        if entry.group not in GROUPS:
            raise ValueError(f"manifest entry {entry.path}: unknown group {entry.group!r}")
        if len(entry.sha256) != 64 or any(c not in "0123456789abcdef" for c in entry.sha256):
            raise ValueError(f"manifest entry {entry.path}: sha256 is not 64 hex digits")
        if entry.size <= 0 or not entry.url.startswith("https://"):
            raise ValueError(f"manifest entry {entry.path}: needs a positive size and an https url")
        out.append(entry)
    return out


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def check_file(entry: ModelFile, root: Path | None = None) -> dict:
    """{path, dest, status, size, ...}; status is ok | missing | size_mismatch | hash_mismatch."""
    dest = entry.dest(root)
    out = {"path": entry.path, "group": entry.group, "dest": str(dest), "expected_size": entry.size}
    if not dest.is_file():
        out["status"] = "missing"
        return out
    size = dest.stat().st_size
    out["size"] = size
    if size != entry.size:
        out["status"] = "size_mismatch"
        return out
    digest = sha256_file(dest)
    out["sha256"] = digest
    out["status"] = "ok" if digest == entry.sha256 else "hash_mismatch"
    return out


def package_version() -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version("cu-perceive")
    except Exception:
        return "unknown"


def _download(entry: ModelFile, dest: Path) -> None:
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part.unlink(missing_ok=True)
    req = urllib.request.Request(entry.url, headers={"User-Agent": f"cu-perceive/{package_version()}"})
    h = hashlib.sha256()
    got = 0
    try:
        with _urlopen(req, timeout=TIMEOUT_S) as resp, part.open("wb") as fh:
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                if got > entry.size:
                    raise FetchError(f"{entry.url} sent more than the expected {entry.size} bytes")
                h.update(chunk)
                fh.write(chunk)
        if got != entry.size:
            raise FetchError(f"{entry.url} sent {got} bytes, expected {entry.size}")
        digest = h.hexdigest()
        if digest != entry.sha256:
            raise FetchError(f"{entry.url}: sha256 {digest} does not match the manifest ({entry.sha256})")
        os.replace(part, dest)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        part.unlink(missing_ok=True)
        raise FetchError(
            f"download of {entry.url} failed ({type(e).__name__}: {e}). Check the network or proxy "
            f"(HTTPS_PROXY), or download it by hand to {dest}"
        ) from e
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def fetch_file(entry: ModelFile, root: Path | None = None, *, force: bool = False, log=None) -> dict:
    """Make one file valid. action: skipped | downloaded | refetched | failed."""
    with _LOCK:
        before = check_file(entry, root)
        if before["status"] == "ok" and not force:
            return {**before, "action": "skipped"}
        dest = Path(before["dest"])
        if log:
            why = "forced" if before["status"] == "ok" else before["status"]
            log(f"cu-perceive: fetching {entry.path} ({entry.size / 1e6:.1f} MB, {why}) from {entry.url}")
        try:
            _download(entry, dest)
        except FetchError as e:
            return {**before, "action": "failed", "error": str(e)}
        after = check_file(entry, root)
        after["action"] = "downloaded" if before["status"] == "missing" else "refetched"
        if after["status"] != "ok":  # pragma: no cover - _download already verified the bytes
            after["action"] = "failed"
            after["error"] = f"{dest} does not verify after download ({after['status']})"
        return after


def fetch_models(groups=None, *, force: bool = False, check_only: bool = False,
                 root: Path | None = None, log=None) -> dict:
    """Fetch (or with check_only, only verify) the manifest files of ``groups`` (default all)."""
    wanted = tuple(groups) if groups else GROUPS
    bad = [g for g in wanted if g not in GROUPS]
    if bad:
        raise ValueError(f"unknown model group(s) {bad}; known: {', '.join(GROUPS)}")
    root = Path(root) if root is not None else config.weights_dir()
    files = []
    for entry in load_manifest():
        if entry.group not in wanted:
            continue
        if check_only:
            files.append(check_file(entry, root))
        else:
            files.append(fetch_file(entry, root, force=force, log=log))
    ok = all(f["status"] == "ok" and f.get("action") != "failed" for f in files)
    return {"ok": ok, "weights_dir": str(root), "check_only": bool(check_only), "files": files}


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def auto_fetch_disabled() -> bool:
    return os.environ.get(ENV_NO_AUTO_FETCH, "").strip().lower() in ("1", "true", "yes", "on")


def ensure_yolo_weight(log=_stderr) -> str | None:
    """Download best.pt on first --yolo use when it is missing at the default path.

    Returns a note for the result's warnings, or None when nothing was done. Never touches a
    path set with CU_PERCEIVE_YOLO_WEIGHT, and never replaces an existing file.
    """
    from .settings import get as _cfg

    if auto_fetch_disabled():
        return None
    entry = next(e for e in load_manifest() if e.path == YOLO_PATH)
    weight = Path(config.yolo_weight())
    if weight != entry.dest():
        return None
    if weight.is_file() and weight.stat().st_size >= int(_cfg("ocr.yolo_min_bytes")):
        return None
    if log:
        log(f"cu-perceive: ScreenParser weight missing at {weight}; downloading it once "
            f"(set {ENV_NO_AUTO_FETCH}=1 to turn this off)")
    res = fetch_file(entry, force=False, log=log)
    if res.get("action") == "failed":
        raise FetchError(res.get("error") or "ScreenParser download failed")
    return f"downloaded the ScreenParser weight to {weight} (first --yolo use)"
