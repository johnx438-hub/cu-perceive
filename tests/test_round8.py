"""Round 8 Windows findings: _meta in tool arguments, 4-number boxes as points, step_px
without a drag, the click= hint, stale stamps, and perceive_window compact=true."""
from __future__ import annotations

import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from test_agent_friction import _mcp_tools
from test_round6 import _OutDir, _screen

from cu_perceive import act, mcp_server
from cu_perceive.act import STALE_STAMP_SEC, coerce_points, parse_steps, point_boxes, run_chain


def _fresh(stamp="s"):
    return _screen(stamp)


class TestUnderscoreArgs(unittest.TestCase):
    def _server(self):
        calls = []

        class Base:
            async def list_tools(self):
                return [SimpleNamespace(name="act", inputSchema={"properties": {"norm": {}}})]

            async def call_tool(self, name, arguments, *a, **k):
                calls.append(arguments)
                return "ran"

        return mcp_server.strict_server_cls(Base)(), calls

    def test_meta_and_underscore_keys_are_dropped(self):
        srv, calls = self._server()
        args = {"norm": "1,1", "_meta": {"progressToken": 1}, "_trace": "x"}
        self.assertEqual(asyncio.run(srv.call_tool("act", args)), "ran")
        self.assertEqual(calls, [{"norm": "1,1"}])

    def test_only_underscore_keys(self):
        srv, calls = self._server()
        self.assertEqual(asyncio.run(srv.call_tool("act", {"_meta": {}})), "ran")
        self.assertEqual(calls, [{}])

    def test_other_unknown_keys_still_refused(self):
        srv, calls = self._server()
        with self.assertRaises(Exception) as cm:
            asyncio.run(srv.call_tool("act", {"_meta": {}, "click": "left"}))
        payload = json.loads(str(cm.exception))
        self.assertEqual(payload["unknown_args"], ["click"])
        self.assertIn('button="left"', payload["hint"])
        self.assertEqual(calls, [])


class TestBoxPoints(_OutDir):
    def test_box_gives_its_center(self):
        self.assertEqual(point_boxes("100,200,300,400"), [([200, 300], [100, 200, 300, 400])])
        self.assertEqual(point_boxes("1,2;100,200,300,400"), [([1, 2], None), ([200, 300], [100, 200, 300, 400])])
        self.assertEqual(coerce_points([100, 200, 300, 400]), "100,200,300,400")
        self.assertEqual(act.pairs("100, 200, 300, 400"), [[200, 300]])

    def test_spaces_after_commas(self):
        self.assertEqual(act.pairs("640, 500"), [[640, 500]])
        self.assertEqual(act.pairs("1, 2; 3 ,4"), [[1, 2], [3, 4]])
        self.assertEqual(act.pairs("1,2 3,4"), [[1, 2], [3, 4]])

    def test_inverted_box_and_three_numbers(self):
        with self.assertRaises(ValueError) as cm:
            point_boxes("300,200,100,400")
        self.assertIn("left,top,right,bottom", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            point_boxes("1,2,3")
        self.assertIn("left,top,right,bottom", str(cm.exception))

    def test_plan_clicks_center_and_warns(self):
        out = run_chain(_fresh(), parse_steps(norm=[100, 200, 300, 400]), dry_run=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["steps"][0]["center"], [200, 300])
        self.assertTrue(any("left,top,right,bottom box" in w and 'norm="200,300"' in w for w in out["warnings"]))

    def test_mcp_with_an_item_norm(self):
        self.put(_screen(time.strftime("%Y%m%d-%H%M%S") + "-001"))
        out = _mcp_tools()["act"](norm=[100, 200, 300, 400])
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["steps"][0]["center"], [200, 300])


class TestStepPx(_OutDir):
    def setUp(self):
        super().setUp()
        self.put(_screen(time.strftime("%Y%m%d-%H%M%S") + "-002"))
        self.act = _mcp_tools()["act"]

    def test_warns_without_drag(self):
        out = self.act(norm="500,500", scroll="down:1", step_px=800)
        self.assertTrue(any("step_px=800 ignored" in w and "wheel notches" in w for w in out["warnings"]), out)

    def test_quiet_with_drag_or_default(self):
        for kw in ({"drag_norm": "100,100;200,200", "step_px": 8}, {"norm": "500,500"}):
            out = self.act(**kw)
            self.assertFalse(any("step_px" in w for w in out.get("warnings", [])), kw)


class TestStaleStamp(unittest.TestCase):
    def _run(self, age, **kw):
        with mock.patch.object(act, "check_stamp", return_value={"ok": True, "age_sec": age}):
            return run_chain(_fresh(), parse_steps(norm="500,500"), dry_run=True, **kw)

    def test_old_stamp_warns(self):
        out = self._run(STALE_STAMP_SEC + 180)
        self.assertTrue(out["stamp_stale"])
        self.assertTrue(any("re-perceive" in w and "max_age_sec" in w for w in out["warnings"]))

    def test_fresh_stamp_or_max_age_set_is_quiet(self):
        self.assertNotIn("stamp_stale", self._run(5))
        self.assertNotIn("stamp_stale", self._run(STALE_STAMP_SEC + 180, max_age_sec=10_000))
        self.assertNotIn("stamp_stale", self._run(None))


class TestCompact(unittest.TestCase):
    def test_compact_rows(self):
        out = {
            "stamp": "s", "items": [{"id": 1, "text": "OK", "kind": "ocr", "box": [1, 2, 3, 4], "center": [2, 3],
                                     "norm": [100, 200, 300, 400], "screen": [5, 6]}],
            "uia": True, "uia_count": 1, "uia_nodes": [{"id": 1, "name": "File", "control_type": "MenuItem",
                                           "norm": [0, 0, 100, 50], "clickable": True, "patterns": ["Invoke"],
                                           "value": "XS-0001"}],
            "source": "image", "coord_space": "screen", "hwnd": None, "png": "p", "json": "j", "map": None,
        }
        with mock.patch("cu_perceive.core.perceive", return_value=out):
            res = _mcp_tools()["perceive_window"](image="x.png", compact=True, uia=True, map_inline=False)
        payload = json.loads(res)
        self.assertTrue(payload["compact"])
        self.assertEqual(payload["items"], [{"id": 1, "text": "OK", "kind": "ocr", "center_norm": [200, 300]}])
        self.assertEqual(payload["uia_nodes"],
                         [{"id": 1, "name": "File", "control_type": "MenuItem", "center_norm": [50, 25],
                           "value": "XS-0001"}])

    def test_default_is_full(self):
        out = {"stamp": "s", "items": [{"id": 1, "text": "OK", "kind": "ocr", "norm": [1, 2, 3, 4]}],
               "source": "image", "coord_space": "screen", "hwnd": None, "png": "p", "json": "j", "map": None}
        with mock.patch("cu_perceive.core.perceive", return_value=out):
            payload = json.loads(_mcp_tools()["perceive_window"](image="x.png", map_inline=False))
        self.assertNotIn("compact", payload)
        self.assertEqual(payload["items"][0]["norm"], [1, 2, 3, 4])


class TestDescription(unittest.TestCase):
    def test_button_mentioned(self):
        doc = _mcp_tools()["act"].__doc__
        self.assertIn('button="left"|"right"', doc.splitlines()[1])
        self.assertIn("there is no click= param", doc)
        self.assertIn("compact=true", _mcp_tools()["perceive_window"].__doc__)


if __name__ == "__main__":
    unittest.main()
