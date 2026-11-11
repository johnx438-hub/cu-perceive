"""Load the BrowserSkill (bsk) extension unpacked into a shadow Chrome profile over CDP.

Branded Chrome 137+ ignores --load-extension, so this uses CDP
``Extensions.loadUnpacked``. Chrome must have been started by cu-perceive on the
shadow profile with two extra args, for example::

    python -m cu_perceive launch --app chrome-shadow \
        --arg=--remote-debugging-port=9333 --arg=--enable-unsafe-extension-debugging

then::

    python scripts/shadow_bsk_install.py --port 9333 --path C:\\Users\\me\\.cu-perceive\\bsk-extension\\0.4.2

The unpacked folder is a copy of an installed bsk extension (its manifest carries a
``key``, so the extension id stays the same). Close that Chrome afterwards and start it
again without the debugging args. Only 127.0.0.1 is contacted. Needs websocket-client.
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
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--path", required=True, help="unpacked extension folder (has manifest.json)")
    a = ap.parse_args(argv)
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
    print(json.dumps({"ok": True, "browser": ver.get("Browser"), "extension_id": res.get("result", {}).get("id")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
