"""End-to-end MCP over streamable HTTP against the installed mcp SDK (1.x or 2.x).

Starts `python -m cu_perceive mcp` in a subprocess and speaks raw JSON-RPC, so the
client side does not depend on the SDK version. Skipped when mcp is not installed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
HAVE_MCP = importlib.util.find_spec("mcp") is not None and importlib.util.find_spec("uvicorn") is not None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _parse_body(raw: bytes, ctype: str) -> dict | None:
    text = raw.decode("utf-8")
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    return json.loads(data)
        return None
    return json.loads(text) if text.strip() else None


@unittest.skipUnless(HAVE_MCP, "mcp SDK not installed")
class TestMcpServerE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        home = Path(cls.td.name)
        cls.out = home / "out"
        cls.png = home / "in.png"
        img = np.full((200, 300, 3), 200, np.uint8)
        cv2.putText(img, "\u4e2d", (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
        cv2.imwrite(str(cls.png), img)
        cls.port = _free_port()
        env = dict(os.environ, CU_PERCEIVE_HOME=str(home), CU_PERCEIVE_OUT=str(cls.out), PYTHONPATH=str(ROOT))
        env.pop("CU_PERCEIVE_ALLOWED_HOSTS", None)
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "cu_perceive", "mcp", "--port", str(cls.port)],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                out = cls.proc.stdout.read().decode("utf-8", "replace")
                raise AssertionError(f"mcp server exited early:\n{out}")
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            cls.proc.kill()
            raise AssertionError("mcp server did not start listening")
        cls.session = None
        cls._next_id = 0
        init = cls._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "cu-perceive-test", "version": "0"},
        })
        cls.protocol = init["result"]["protocolVersion"]
        cls._rpc("notifications/initialized", None, notify=True)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.proc.stdout.close()
        cls.td.cleanup()

    @classmethod
    def _post(cls, body: dict, host: str | None = None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if cls.session:
            headers["mcp-session-id"] = cls.session
        if getattr(cls, "protocol", None):
            headers["mcp-protocol-version"] = cls.protocol
        if host:
            headers["Host"] = host
        req = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}/mcp", data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        return urllib.request.urlopen(req, timeout=30)

    @classmethod
    def _rpc(cls, method: str, params, notify: bool = False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            cls._next_id += 1
            body["id"] = cls._next_id
        with cls._post(body) as resp:
            sid = resp.headers.get("mcp-session-id")
            if sid:
                cls.session = sid
            return _parse_body(resp.read(), resp.headers.get("Content-Type", ""))

    def _call(self, name: str, args: dict) -> list[dict]:
        res = self._rpc("tools/call", {"name": name, "arguments": args})
        self.assertNotIn("error", res, res)
        self.assertFalse(res["result"].get("isError"), res)
        return res["result"]["content"]

    def test_tools_listed(self):
        res = self._rpc("tools/list", {})
        names = {t["name"] for t in res["result"]["tools"]}
        self.assertTrue({"windows", "perceive_window", "act", "launch_app", "apps"} <= names, names)
        act = next(t for t in res["result"]["tools"] if t["name"] == "act")
        self.assertIn("replace", act["inputSchema"]["properties"])
        self.assertIn("focus", act["inputSchema"]["properties"])

    def test_perceive_image_then_dry_run_act(self):
        content = self._call("perceive_window", {"image": str(self.png), "map_inline": True})
        kinds = [c["type"] for c in content]
        self.assertEqual(kinds[0], "text")
        self.assertIn("image", kinds)
        payload = json.loads(content[0]["text"])
        self.assertEqual((payload["width"], payload["height"]), (300, 200))
        self.assertTrue(Path(payload["json"]).is_file())

        act = json.loads(self._call("act", {"stamp": payload["stamp"], "norm": "500,500", "text": "hi", "replace": True})[0]["text"])
        self.assertTrue(act["ok"], act)
        self.assertTrue(act["dry_run"])
        vias = [s["via"] for s in act["steps"]]
        self.assertEqual(vias, ["norm", "key", "type"])
        self.assertEqual(act["steps"][1]["key"], "ctrl+a")
        self.assertEqual(act["foreground_click_count"], 1)
        self.assertEqual(act["stamp_source"], "explicit")

        keys = json.loads(self._call("act", {"stamp": payload["stamp"], "text": "hi", "go": True})[0]["text"])
        self.assertFalse(keys["ok"], keys)
        self.assertEqual(keys["keyboard_refused_step"], 0)
        self.assertNotIn("typed", keys["steps"][0])

    def test_scroll_int_and_json_path_stamp(self):
        payload = json.loads(self._call("perceive_window", {"image": str(self.png), "map_inline": False})[0]["text"])
        for scroll, notches in ((5, 5), (-5, -5), ("down:5", -5)):
            act = json.loads(self._call("act", {"stamp": payload["json"], "norm": "500,500", "scroll": scroll})[0]["text"])
            self.assertTrue(act["ok"], act)
            self.assertEqual([s["notches"] for s in act["steps"] if s["via"] == "scroll"], [notches])
            self.assertEqual(act["json"], payload["json"])
        up = json.loads(self._call("act", {"stamp": payload["stamp"], "norm": "500,500", "scroll": 5})[0]["text"])
        self.assertTrue(any("scrolls UP" in w for w in up.get("warnings", [])), up)

    def test_rejects_foreign_host_header(self):
        body = {"jsonrpc": "2.0", "id": 999, "method": "tools/list", "params": {}}
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._post(body, host="evil.example:80")
        self.assertIn(cm.exception.code, (400, 403, 421))


if __name__ == "__main__":
    unittest.main()
