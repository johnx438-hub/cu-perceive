"""act ids -> structured bad_ids / UIA-id fallback; compact perceive warns about omitted UIA; windows query=."""
from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from unittest import mock

from cu_perceive import mcp_server
from cu_perceive.act import BadIdsError, item_by_id, parse_steps, plan_steps, run_chain


def _frame(items=None, uia=None):
    f = {
        "stamp": "20261020-013000-001", "hwnd": None, "coord_space": "screen",
        "origin": {"left": 0, "top": 0}, "width": 1000, "height": 1000,
        "items": items if items is not None else [],
    }
    if uia is not None:
        f["uia_nodes"] = uia
    return f


UIA = [
    {"id": 15, "name": "搜索连接器", "control_type": "EditControl", "norm": [815, 67, 835, 87]},
    {"id": 16, "name": "添加", "control_type": "ButtonControl", "norm": [922, 67, 942, 87]},
]
OCR = [{"id": 1, "text": "Q", "center": [209, 78], "norm": [200, 70, 218, 86]}]


class ActIds(unittest.TestCase):
    def test_item_by_id_still_a_keyerror(self):
        with self.assertRaises(KeyError):
            item_by_id(_frame(OCR), 9)
        with self.assertRaises(BadIdsError):
            item_by_id(_frame(OCR), 9)

    def test_uia_id_used_when_no_ocr_items(self):
        steps = plan_steps(_frame([], UIA), parse_steps(ids=[16]))
        self.assertEqual(steps[0]["via"], "norm")
        self.assertEqual(steps[0]["norm"], [932, 77])
        self.assertEqual(steps[0]["id"], 16)
        self.assertEqual(steps[0]["id_source"], "uia")
        self.assertEqual(steps[0]["uia_node_id"], 16)

    def test_ocr_ids_win_when_present(self):
        steps = plan_steps(_frame(OCR, UIA), parse_steps(ids=[1]))
        self.assertEqual(steps[0]["via"], "id")
        self.assertNotIn("id_source", steps[0])

    def test_uia_id_with_ocr_items_is_bad_ids_with_hint(self):
        out = run_chain(_frame(OCR, UIA), parse_steps(ids=[16]), dry_run=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "bad_ids")
        self.assertIn('uia_name="#16"', out["hint"])
        self.assertEqual(out["item_ids"], [1])
        self.assertEqual(out["uia_ids"], [15, 16])

    def test_unknown_id_no_items_no_uia(self):
        out = run_chain(_frame([], None), parse_steps(ids=[16]), dry_run=True)
        self.assertEqual(out["reason"], "bad_ids")
        self.assertIn("ocr=false", out["hint"])

    def test_uia_name_miss_is_structured(self):
        out = run_chain(_frame([], UIA), parse_steps(uia_name="不存在的按钮"), dry_run=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "uia_not_found")
        self.assertTrue(out["hint"])

    def test_dry_run_uia_id_ok(self):
        out = run_chain(_frame([], UIA), parse_steps(ids=[16]), dry_run=True)
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["dry_run"])


def _tools():
    from test_agent_friction import _ToolApp

    app = _ToolApp()
    fake = {
        "mcp": types.ModuleType("mcp"),
        "mcp.server": types.ModuleType("mcp.server"),
        "mcp.server.transport_security": types.SimpleNamespace(TransportSecuritySettings=lambda **k: k),
        "uvicorn": types.SimpleNamespace(Config=lambda *a, **k: None,
                                         Server=lambda c: types.SimpleNamespace(run=lambda: None)),
    }
    with mock.patch.dict(sys.modules, fake), \
            mock.patch.object(mcp_server, "mcp_api", return_value=(2, None, None)), \
            mock.patch.object(mcp_server, "build_server", return_value=(app, lambda: None)):
        mcp_server.serve()
    return app.registered


def _run(fn, **kw):
    r = asyncio.run(fn(**kw))
    if isinstance(r, list):
        r = r[0]
    return json.loads(r) if isinstance(r, str) else r


ROWS = [
    {"hwnd": 1, "title": "豆包", "exe": "Doubao.exe", "pid": 5},
    {"hwnd": 2, "title": "Doubao helper", "exe": "Other.exe", "pid": 6},
]


def _fake_find(title="", exe="", hwnd=None):
    if hwnd:
        return [r for r in ROWS if r["hwnd"] == hwnd]
    t, e = title.lower(), exe.lower()
    return [r for r in ROWS if (not t or t in r["title"].lower()) and (not e or e in r["exe"].lower())]


class WindowsQuery(unittest.TestCase):
    def test_query_matches_title_or_exe_dedup(self):
        with mock.patch("cu_perceive.windows.find_windows", side_effect=_fake_find):
            out = _run(_tools()["windows"], query="doubao")
        self.assertEqual([w["hwnd"] for w in out["windows"]], [2, 1])
        self.assertEqual(out["count"], 2)

    def test_query_with_title_is_refused(self):
        tools = _tools()
        out = _run(tools["windows"], query="x", title="y")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "bad_args")


def _perceive_out(uia):
    out = {
        "stamp": "s1", "items": [], "source": "hwnd:1", "coord_space": "window", "hwnd": 1,
        "origin": {"left": 0, "top": 0}, "width": 10, "height": 10, "png": "p.png", "json": "p.json",
        "map": None, "grid": None, "ok": True,
    }
    if uia:
        out.update({"uia": True, "uia_nodes": [{"id": 1, "name": "OK", "control_type": "ButtonControl",
                                               "norm": [1, 1, 3, 3]}], "uia_count": 1})
    return out


class CompactUia(unittest.TestCase):
    def test_compact_without_uia_warns(self):
        with mock.patch("cu_perceive.core.perceive", return_value=_perceive_out(False)):
            out = _run(_tools()["perceive_window"], hwnd=1, compact=True, map_inline=False)
        self.assertTrue(out["uia_nodes_omitted"])
        self.assertTrue(any("uia=true" in w for w in out["warnings"]))
        self.assertNotIn("uia_nodes", out)

    def test_compact_with_uia_has_no_warning(self):
        with mock.patch("cu_perceive.core.perceive", return_value=_perceive_out(True)):
            out = _run(_tools()["perceive_window"], hwnd=1, compact=True, uia=True, map_inline=False)
        self.assertNotIn("uia_nodes_omitted", out)
        self.assertEqual(out["uia_nodes"][0]["center_norm"], [2, 2])

    def test_non_compact_unchanged(self):
        with mock.patch("cu_perceive.core.perceive", return_value=_perceive_out(False)):
            out = _run(_tools()["perceive_window"], hwnd=1, map_inline=False)
        self.assertNotIn("uia_nodes_omitted", out)
        self.assertNotIn("warnings", out)


if __name__ == "__main__":
    unittest.main()
