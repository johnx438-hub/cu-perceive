"""OCR and YOLO run in a child process, never in the perceive / MCP process.

Once onnxruntime is loaded into a process, the next Windows.Graphics.Capture grab in
that process crashes it with an access violation in windows_capture's
start_free_threaded. Keeping the detectors in a long-lived child (started on first
use with ``python -c CHILD_MAIN``) keeps WGC usable and means a
detector crash costs one perceive call, not the server.

Protocol: length-prefixed pickles over the child's stdin / a private dup of its
stdout; the child's fd 1 is pointed at stderr so library prints can't corrupt it.
Set CU_PERCEIVE_DETECT_INPROC=1 to run the detectors in-process instead (capture
then never uses WGC once onnxruntime is loaded, see capture.wgc_blocker).
"""
from __future__ import annotations

import atexit
import os
import pickle
import struct
import subprocess
import sys
import threading
from pathlib import Path

ENV_INPROC = "CU_PERCEIVE_DETECT_INPROC"
CHILD_MAIN = "from cu_perceive.detect_worker import serve; serve()"
_HEADER = struct.Struct(">Q")
_CREATE_NO_WINDOW = 0x08000000


class DetectWorkerError(RuntimeError):
    """The detection child failed to start, died, or raised something unexpected."""


def _write(fh, obj) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    fh.write(_HEADER.pack(len(data)))
    fh.write(data)
    fh.flush()


def _read_exact(fh, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = fh.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _read(fh):
    head = _read_exact(fh, _HEADER.size)
    if head is None:
        return None
    body = _read_exact(fh, _HEADER.unpack(head)[0])
    if body is None:
        return None
    return pickle.loads(body)


def run_local(op: str, image=None):
    """Run one detector op in this process (the child, or CU_PERCEIVE_DETECT_INPROC=1)."""
    if op == "ocr":
        from .core import _parser

        return _parser().parse(image)
    if op == "yolo":
        from .yolo import detect

        return detect(image)
    if op == "ping":
        return {"pid": os.getpid(), "onnxruntime_loaded": "onnxruntime" in sys.modules}
    raise ValueError(f"unknown detector op {op!r}")


def _encode_error(e: BaseException):
    from .extras import MissingExtraError

    if isinstance(e, MissingExtraError):
        return ("missing_extra", (e.extra, e.missing, e.command))
    if isinstance(e, FileNotFoundError):
        return ("file_not_found", str(e))
    return ("error", f"{type(e).__name__}: {e}")


def _decode_error(kind: str, payload):
    from .extras import MissingExtraError

    if kind == "missing_extra":
        return MissingExtraError(*payload)
    if kind == "file_not_found":
        return FileNotFoundError(payload)
    return DetectWorkerError(f"detection worker: {payload}")


def serve(stdin=None, stdout=None) -> None:
    """Child main loop: one (op, image) request in, one ("ok", result) / (kind, error) out."""
    if stdout is None:
        stdout = os.fdopen(os.dup(1), "wb")
        os.dup2(2, 1)
        sys.stdout = sys.stderr
    stdin = stdin or sys.stdin.buffer
    while True:
        msg = _read(stdin)
        if msg is None:
            return
        op, image = msg
        try:
            reply = ("ok", run_local(op, image))
        except BaseException as e:  # noqa: BLE001 - every failure goes back to the parent
            reply = _encode_error(e)
        _write(stdout, reply)


class _Worker:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def _start(self) -> subprocess.Popen:
        kw = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        env = dict(os.environ)
        pkg_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(p for p in (pkg_root, env.get("PYTHONPATH")) if p)
        try:
            return subprocess.Popen(
                [sys.executable, "-c", CHILD_MAIN],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=env,
                **kw,
            )
        except OSError as e:
            raise DetectWorkerError(
                f"could not start the OCR/YOLO worker process ({e}); set {ENV_INPROC}=1 to run "
                "it in-process (WGC capture is then skipped after OCR)"
            ) from e

    def call(self, op: str, image=None):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                self.proc = self._start()
            proc = self.proc
            try:
                _write(proc.stdin, (op, image))
                reply = _read(proc.stdout)
            except (OSError, ValueError, pickle.PickleError):
                reply = None
            if reply is None:
                code = proc.poll()
                self.proc = None
                raise DetectWorkerError(
                    f"the OCR/YOLO worker process exited during {op} (exit code {code}); "
                    "this perceive call has no detections, the next one starts a new worker"
                )
        kind, payload = reply
        if kind == "ok":
            return payload
        raise _decode_error(kind, payload)

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()


_WORKER = _Worker()
atexit.register(_WORKER.close)


def run(op: str, image=None):
    """Run a detector op in the child process (or in-process with CU_PERCEIVE_DETECT_INPROC=1)."""
    if os.environ.get(ENV_INPROC, "").strip() not in ("", "0"):
        return run_local(op, image)
    return _WORKER.call(op, image)


def ocr(image) -> list[dict]:
    return run("ocr", image)


def yolo(image) -> list[dict]:
    return run("yolo", image)
