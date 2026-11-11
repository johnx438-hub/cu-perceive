"""FALLBACK: load the BrowserSkill (bsk) extension unpacked into a shadow Chrome over CDP.

Primary path (persistent, no debugging port): once per shadow profile, open
chrome://extensions in a shadow window started by cu-perceive (``launch --app chrome-shadow``),
turn on Developer mode, click "Load unpacked" and pick the unpacked folder (for example
``C:\\Users\\me\\.cu-perceive\\bsk-extension\\0.4.2``). Chrome records it in the profile as an
unpacked extension (Secure Preferences ``location`` 4), so it is there on every later start
without any flag. Stop if Chrome shows a permission, security or confirmation dialog: a person
answers those, not automation.

This script is only for when that is not possible (for example a throwaway profile). Branded
Chrome 137+ ignores --load-extension, so it uses CDP ``Extensions.loadUnpacked``, which lasts
for that browser process only. Start Chrome through cu-perceive with two extra args::

    python -m cu_perceive launch --app chrome-shadow \\
        --arg=--remote-debugging-port=9333 --arg=--enable-unsafe-extension-debugging

cu-perceive pins ``--remote-debugging-address=127.0.0.1`` whenever a debugging port is set and
refuses any other address. Then::

    python scripts/shadow_bsk_install.py --port 9333 --path C:\\Users\\me\\.cu-perceive\\bsk-extension\\0.4.2

The unpacked folder is a copy of an installed bsk extension (its manifest carries a ``key``,
so the extension id stays the same). Close that Chrome afterwards and start it again without
the debugging args. Only 127.0.0.1 is contacted. Needs websocket-client.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path


def cdp_call(ws_url: str, method: str, params: dict, timeout: float = 15.0) -> dict:
    import websocket  # websocket-client

    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg
    finally:
        ws.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, required=True, help="the --remote-debugging-port (1-65535)")
    ap.add_argument("--path", required=True, help="unpacked extension folder (has manifest.json)")
    a = ap.parse_args(argv)
    if not 1 <= a.port <= 65535:
        print(json.dumps({"ok": False, "reason": "bad_port", "port": a.port}))
        return 1
    ext = Path(a.path).resolve()
    if not (ext / "manifest.json").is_file():
        print(json.dumps({"ok": False, "reason": "no_manifest", "path": str(ext)}))
        return 1
    with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/json/version", timeout=5) as r:
        ver = json.loads(r.read().decode("utf-8"))
    ws_url = ver.get("webSocketDebuggerUrl") or ""
    if not ws_url.startswith("ws://127.0.0.1:"):
        print(json.dumps({"ok": False, "reason": "not_local", "browser": ver.get("Browser")}))
        return 1
    res = cdp_call(ws_url, "Extensions.loadUnpacked", {"path": str(ext)})
    if "error" in res:
        print(json.dumps({"ok": False, "reason": "cdp_error", "browser": ver.get("Browser"),
                          "error": res["error"].get("message")}))
        return 1
    print(json.dumps({"ok": True, "browser": ver.get("Browser"), "extension_id": res.get("result", {}).get("id"),
                      "persistent": False,
                      "note": "loaded for this browser process only; use Load unpacked in chrome://extensions to keep it"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
