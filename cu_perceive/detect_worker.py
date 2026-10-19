"""OCR and YOLO run in a child process, never in the perceive / MCP process.

Once onnxruntime is loaded into a process, the next Windows.Graphics.Capture grab in
that process crashes it with an access violation in windows_capture's
start_free_threaded. Keeping the detectors in a long-lived child (started on first
use with ``python -c CHILD_MAIN``) keeps WGC usable and means a
detector crash costs one perceive call, not the server.

Protocol: each message is a header (pickle length, raw length), a small pickle and an
optional raw byte block, over the child's stdin / a private dup of its stdout; the
child's fd 1 is pointed at stderr so library prints can't corrupt it. Images travel as
(shape, dtype) in the pickle plus their pixel bytes in the raw block, so a frame is
not pickled and unpickled on every call.

Every call has a timeout (CU_PERCEIVE_DETECT_TIMEOUT seconds, default 30; the first
call of a new worker, which loads the models, gets 3x). On timeout the worker is
killed, a fresh one is started, and the call raises DetectWorkerTimeout, which perceive
turns into a warning on an otherwise normal stamp. Only OCR/YOLO calls take the worker
lock; perceive without ocr/yolo never touches it.

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
ENV_TIMEOUT = "CU_PERCEIVE_DETECT_TIMEOUT"
DEFAULT_TIMEOUT_S = 30.0
FIRST_CALL_FACTOR = 3
CHILD_MAIN = "from cu_perceive.detect_worker import serve; serve()"
_HEADER = struct.Struct(">QQ")
_CREATE_NO_WINDOW = 0x08000000


class DetectWorkerError(RuntimeError):
    """The detection child failed to start, died, or raised something unexpected."""


class DetectWorkerTimeout(DetectWorkerError):
    """A detection call ran past the timeout (the worker was killed and restarted),
    or the worker stayed busy with another call for that long."""


def detect_timeout() -> float | None:
    """Seconds per detection call; None = wait forever (CU_PERCEIVE_DETECT_TIMEOUT=0)."""
    raw = os.environ.get(ENV_TIMEOUT, "").strip().lower()
    if not raw:
        return DEFAULT_TIMEOUT_S
    if raw in ("0", "off", "none", "inf"):
        return None
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return val if val > 0 else None


def _pack_image(image):
    """(meta, raw bytes) for a numpy image; (image, None) for anything else."""
    try:
        import numpy as np
    except ImportError:
        return image, None
    if not isinstance(image, np.ndarray):
        return image, None
    arr = np.ascontiguousarray(image)
    return {"ndarray": True, "shape": arr.shape, "dtype": arr.dtype.str}, memoryview(arr).cast("B")


def _unpack_image(meta, raw):
    if raw is None or not (isinstance(meta, dict) and meta.get("ndarray")):
        return meta
    import numpy as np

    return np.frombuffer(raw, dtype=np.dtype(meta["dtype"])).reshape(meta["shape"])


def _write(fh, obj, raw=None) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    fh.write(_HEADER.pack(len(data), len(raw) if raw is not None else 0))
    fh.write(data)
    if raw is not None and len(raw):
        fh.write(raw)
    fh.flush()


def _read_exact(fh, n: int) -> bytearray | None:
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        if hasattr(fh, "readinto"):
            k = fh.readinto(view[got:])
        else:
            chunk = fh.read(n - got)
            k = len(chunk) if chunk else 0
            view[got:got + k] = chunk or b""
        if not k:
            return None
        got += k
    return buf


def _read_message(fh):
    """(obj, raw) or None at EOF."""
    head = _read_exact(fh, _HEADER.size)
    if head is None:
        return None
    n_obj, n_raw = _HEADER.unpack(head)
    body = _read_exact(fh, n_obj)
    if body is None:
        return None
    raw = None
    if n_raw:
        raw = _read_exact(fh, n_raw)
        if raw is None:
            return None
    return pickle.loads(body), raw


def _read(fh):
    msg = _read_message(fh)
    return None if msg is None else msg[0]


def _write_request(fh, op: str, image=None) -> None:
    meta, raw = _pack_image(image)
    _write(fh, (op, meta), raw)


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
        msg = _read_message(stdin)
        if msg is None:
            return
        (op, meta), raw = msg
        try:
            reply = ("ok", run_local(op, _unpack_image(meta, raw)))
        except BaseException as e:  # noqa: BLE001 - every failure goes back to the parent
            reply = _encode_error(e)
        _write(stdout, reply)


class _Worker:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.served = 0
        self.lock = threading.Lock()

    def _start(self) -> subprocess.Popen:
        kw = {"creationflags": _CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        env = dict(os.environ)
        pkg_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = os.pathsep.join(p for p in (pkg_root, env.get("PYTHONPATH")) if p)
        try:
            proc = subprocess.Popen(
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
        self.served = 0
        return proc

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    def call(self, op: str, image=None, timeout: float | None = -1.0):
        """timeout: seconds, None = no limit, -1 = CU_PERCEIVE_DETECT_TIMEOUT."""
        if timeout is not None and timeout < 0:
            timeout = detect_timeout()
        if not self.lock.acquire(timeout=timeout if timeout else -1):
            raise DetectWorkerTimeout(
                f"the OCR/YOLO worker stayed busy with another call for {timeout:g}s; "
                f"this {op} was skipped (no detections), retry or raise {ENV_TIMEOUT}"
            )
        try:
            return self._call_locked(op, image, timeout)
        finally:
            self.lock.release()

    def _call_locked(self, op: str, image, timeout: float | None):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = self._start()
        proc = self.proc
        limit = timeout * FIRST_CALL_FACTOR if (timeout and self.served == 0) else timeout
        box: dict = {}

        def talk():
            try:
                _write_request(proc.stdin, op, image)
                box["reply"] = _read(proc.stdout)
            except (OSError, ValueError, EOFError, pickle.PickleError):
                box["reply"] = None

        t = threading.Thread(target=talk, name=f"cu-perceive-detect-{op}", daemon=True)
        t.start()
        t.join(limit)
        if t.is_alive():
            self._kill(proc)
            t.join(5)
            self.proc = None
            try:
                self.proc = self._start()
            except DetectWorkerError:
                pass
            raise DetectWorkerTimeout(
                f"{op} took longer than {limit:g}s; the OCR/YOLO worker was killed and restarted "
                f"(this perceive call has no {op} detections; timeout: {ENV_TIMEOUT})"
            )
        reply = box.get("reply")
        if reply is None:
            code = proc.poll()
            self.proc = None
            raise DetectWorkerError(
                f"the OCR/YOLO worker process exited during {op} (exit code {code}); "
                "this perceive call has no detections, the next one starts a new worker"
            )
        self.served += 1
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


def run(op: str, image=None, timeout: float | None = -1.0):
    """Run a detector op in the child process (or in-process with CU_PERCEIVE_DETECT_INPROC=1)."""
    if os.environ.get(ENV_INPROC, "").strip() not in ("", "0"):
        return run_local(op, image)
    return _WORKER.call(op, image, timeout=timeout)


def ocr(image) -> list[dict]:
    return run("ocr", image)


def yolo(image) -> list[dict]:
    return run("yolo", image)
