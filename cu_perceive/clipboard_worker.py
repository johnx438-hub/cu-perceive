"""Clipboard helper process.

pywin32 311 can heap-corrupt the caller (exit 0xC0000374) when
``SetClipboardData(CF_UNICODETEXT, str)`` is given a numeric string. The MCP
server runs get/set in this process so that fault returns ok=false instead of
killing the server. The parent sets ``CU_PERCEIVE_CLIPBOARD_CHILD=1``.
"""
from __future__ import annotations

import json
import os
import sys


def _handle(req) -> dict:
    if not isinstance(req, dict):
        return {"ok": False, "reason": "clipboard_failed", "error": "clipboard request must be an object"}
    from cu_perceive.input import clipboard_rpc

    result = clipboard_rpc(str(req.get("op") or ""), req.get("text"))
    if not isinstance(result, dict):
        result = {"ok": False, "reason": "clipboard_failed", "error": "clipboard helper returned nothing"}
    result.setdefault("reason", "clipboard_failed")
    if result.get("ok"):
        result.pop("reason", None)
    return result


def serve() -> int:
    """Read one JSON object per line and write one JSON object per line.

    The parent keeps this process alive so a paste is not three process startups.
    """
    os.environ["CU_PERCEIVE_CLIPBOARD_CHILD"] = "1"
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _emit({"ok": False, "reason": "clipboard_failed", "error": f"bad clipboard request: {e}"})
            continue
        if isinstance(req, dict) and req.get("op") == "quit":
            _emit({"ok": True})
            return 0
        _emit(_handle(req))
    return 0


def main() -> int:
    os.environ["CU_PERCEIVE_CLIPBOARD_CHILD"] = "1"
    if "--serve" in sys.argv[1:]:
        return serve()
    try:
        raw = sys.stdin.buffer.read()
        req = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except Exception as e:
        _emit({"ok": False, "reason": "clipboard_failed", "error": f"bad clipboard request: {e}"})
        return 1
    result = _handle(req)
    _emit(result)
    return 0 if result.get("ok") else 1


def _emit(payload: dict) -> None:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    raise SystemExit(main())
