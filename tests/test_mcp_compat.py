"""mcp SDK 1.x (FastMCP) vs 2.x (MCPServer) selection, with fake SDK modules."""
from __future__ import annotations

import re
import sys
import tomllib
import types
import unittest
from pathlib import Path
from unittest import mock

from cu_perceive import mcp_server

ROOT = Path(__file__).resolve().parents[1]


def _pkg(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__path__ = []
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


class _Recorder:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.settings = types.SimpleNamespace(host=None, port=None)
        self.app_kwargs = None

    def streamable_http_app(self, **kwargs):
        self.app_kwargs = kwargs
        return "starlette-app"


class FakeMCPServer(_Recorder):
    pass


class FakeFastMCP(_Recorder):
    pass


class FakeImage:
    pass


def _v2_modules():
    return {
        "mcp": _pkg("mcp"),
        "mcp.server": _pkg("mcp.server"),
        "mcp.server.mcpserver": _pkg("mcp.server.mcpserver", MCPServer=FakeMCPServer),
        "mcp.server.mcpserver.utilities": _pkg("mcp.server.mcpserver.utilities"),
        "mcp.server.mcpserver.utilities.types": _pkg("mcp.server.mcpserver.utilities.types", Image=FakeImage),
    }


def _v1_modules():
    return {
        "mcp": _pkg("mcp"),
        "mcp.server": _pkg("mcp.server"),
        "mcp.server.mcpserver": None,
        "mcp.server.fastmcp": _pkg("mcp.server.fastmcp", FastMCP=FakeFastMCP),
        "mcp.server.fastmcp.utilities": _pkg("mcp.server.fastmcp.utilities"),
        "mcp.server.fastmcp.utilities.types": _pkg("mcp.server.fastmcp.utilities.types", Image=FakeImage),
    }


class TestMcpApi(unittest.TestCase):
    def test_v2_uses_mcpserver_and_passes_security_to_app(self):
        with mock.patch.dict(sys.modules, _v2_modules()):
            major, cls, image = mcp_server.mcp_api()
            self.assertEqual((major, cls, image), (2, FakeMCPServer, FakeImage))
            app, http_app = mcp_server.build_server("cu-perceive", "127.0.0.1", 8771, "SEC")
        self.assertIsInstance(app, FakeMCPServer)
        self.assertEqual(app.args, ("cu-perceive",))
        self.assertEqual(app.kwargs, {})
        self.assertEqual(http_app(), "starlette-app")
        self.assertEqual(app.app_kwargs, {"transport_security": "SEC", "host": "127.0.0.1"})

    def test_v1_uses_fastmcp_with_constructor_settings(self):
        with mock.patch.dict(sys.modules, _v1_modules()):
            major, cls, _image = mcp_server.mcp_api()
            self.assertEqual((major, cls), (1, FakeFastMCP))
            app, http_app = mcp_server.build_server("cu-perceive", "127.0.0.1", 8771, "SEC")
        self.assertEqual(app.kwargs, {"host": "127.0.0.1", "port": 8771, "transport_security": "SEC"})
        self.assertEqual((app.settings.host, app.settings.port), ("127.0.0.1", 8771))
        self.assertEqual(http_app(), "starlette-app")
        self.assertEqual(app.app_kwargs, {})

    def test_neither_api_gives_actionable_error(self):
        mods = {"mcp": _pkg("mcp"), "mcp.server": _pkg("mcp.server"),
                "mcp.server.mcpserver": None, "mcp.server.fastmcp": None}
        with mock.patch.dict(sys.modules, mods):
            with self.assertRaises(ImportError) as cm:
                mcp_server.mcp_api()
        self.assertIn('mcp>=1.19,<3', str(cm.exception))

    def test_pyproject_allows_mcp_1_and_2(self):
        deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
        spec = next(d for d in deps if re.match(r"^mcp\b", d))
        self.assertIn(">=1.19", spec)
        self.assertIn("<3", spec)
        self.assertNotIn("<2", spec)


if __name__ == "__main__":
    unittest.main()
