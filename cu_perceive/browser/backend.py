"""How a planned bsk argv is carried out.

``BrowserBackend`` is the replaceable seam. ``SubprocessBsk`` shells out to the
``bsk`` binary (``CU_PERCEIVE_BSK``, otherwise ``PATH``). A later in-process
port implements the same two methods and leaves the tool layer alone: ``run``
still receives the argv ``catalog`` built (without ``--json``) and returns a
``BskRun`` whose ``stdout`` is the JSON payload and whose ``exit_code`` follows
bsk (0 ok, 1 user, 2 transport, 3 browser, 4 timeout, 5 version).
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Protocol


DEFAULT_TIMEOUT_SEC = 120.0
ENV_BIN = "CU_PERCEIVE_BSK"
ENV_TIMEOUT = "CU_PERCEIVE_BSK_TIMEOUT"


@dataclass
class BskRun:
    """One finished attempt. ``missing`` means the binary itself was not found."""

    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    missing: bool = False
    spawn_error: str | None = None


class BrowserBackend(Protocol):
    """Execute a catalog argv. ``executable`` is None when the dependency is absent."""

    name: str

    def executable(self) -> str | None: ...

    def run(self, args: list[str], *, timeout: float) -> BskRun: ...


def command_argv(executable: str, args: list[str]) -> list[str]:
    """Exact bsk argv, including ``--json``.

    ``--json`` goes before a ``--`` positional separator. Clap treats every
    argument after ``--`` as a positional, so the flag cannot follow a url,
    key, or target.
    """
    argv = [executable, *args]
    if "--json" not in argv:
        if "--" in argv:
            argv.insert(argv.index("--"), "--json")
        else:
            argv.append("--json")
    return argv


def bsk_on_path() -> str | None:
    """Resolve ``bsk`` from ``PATH``.

    On Windows only ``bsk.exe`` counts. ``shutil.which`` would also accept
    ``bsk.cmd`` / ``bsk.bat`` via ``PATHEXT``, and those wrappers are not the
    CLI.
    """
    if sys.platform == "win32":
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            candidate = os.path.join(directory, "bsk.exe")
            if os.path.isfile(candidate):
                return candidate
        return None
    return shutil.which("bsk")


def _spawn_argv(argv: list[str]) -> list[str]:
    """How to start ``argv``. A ``.py`` fake is handed to this interpreter.

    Windows cannot execute a shebang script (WinError 193). Production
    ``bsk.exe`` does not use this path.
    """
    if argv and argv[0].lower().endswith(".py"):
        return [sys.executable, *argv]
    return argv


def timeout_sec(command_timeout_ms: int = 0) -> float:
    """Subprocess ceiling. A per-command ``--timeout`` extends it, never shortens it."""
    base = DEFAULT_TIMEOUT_SEC
    raw = os.environ.get(ENV_TIMEOUT, "").strip()
    if raw:
        try:
            parsed = float(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            base = parsed
    if command_timeout_ms and command_timeout_ms > 0:
        return max(base, command_timeout_ms / 1000.0 + 15.0)
    return base


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return bytes(value).decode("utf-8", errors="replace")


class SubprocessBsk:
    """Spawn ``bsk <args> --json``. Non-zero exits are results, not exceptions."""

    name = "subprocess"

    def executable(self) -> str | None:
        override = os.environ.get(ENV_BIN)
        if override is not None and override.strip() != "":
            path = override.strip()
            if os.path.isfile(path):
                return path
            return None
        return bsk_on_path()

    def run(self, args: list[str], *, timeout: float) -> BskRun:
        exe = self.executable()
        argv = command_argv(exe or "bsk", args)
        if exe is None:
            return BskRun(argv=argv, exit_code=None, stdout="", stderr="", missing=True)
        flags: dict = {}
        if sys.platform == "win32":
            flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                _spawn_argv(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                **flags,
            )
        except subprocess.TimeoutExpired as exc:
            return BskRun(
                argv=argv,
                exit_code=None,
                stdout=_as_text(exc.stdout),
                stderr=_as_text(exc.stderr),
                timed_out=True,
            )
        except OSError as exc:
            missing = isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT
            return BskRun(
                argv=argv,
                exit_code=None,
                stdout="",
                stderr="",
                missing=missing,
                spawn_error=str(exc),
            )
        return BskRun(
            argv=argv,
            exit_code=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
        )


_backend: BrowserBackend | None = None


def get_backend() -> BrowserBackend:
    """The process backend. Tests and a future port replace it via ``set_backend``."""
    if _backend is not None:
        return _backend
    return SubprocessBsk()


def set_backend(backend: BrowserBackend | None) -> None:
    global _backend
    _backend = backend
