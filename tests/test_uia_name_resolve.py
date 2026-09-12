"""E4: UIA name/id -> norm resolve + plan_steps (no live UI)."""
from __future__ import annotations

import unittest

from cu_perceive.uia import (
    find_uia_nodes,
    resolve_uia_target,
    uia_node_to_norm_center,
)
from cu_perceive.act import parse_steps, plan_steps, run_chain


def _fixture_frame(**extra):
    nodes = [
        {
            "id": 1,
            "name": "File",
            "control_type": "MenuItemControl",
            "norm": [20, 10, 80, 40],
            "clickable": True,
            "patterns": ["Invoke", "ExpandCollapse"],
        },
        {
            "id": 2,
            "name": "Edit",
            "control_type": "MenuItemControl",
            "norm": [90, 10, 140, 40],
            "clickable": True,
            "patterns": ["Invoke"],
        },
        {
            "id": 3,
            "name": "Filename:",
            "control_type": "TextControl",
            "norm": [100, 200, 200, 230],
            "clickable": False,
        },
        {
            "id": 4,
            "name": "Open File",
            "control_type": "ButtonControl",
            "norm": [300, 400, 400, 440],
            "clickable": True,
            "patterns": ["Invoke"],
        },
        {
            "id": 5,
            "name": "File",
            "control_type": "ButtonControl",
            "norm": [500, 10, 560, 40],
            "clickable": True,
            "patterns": ["Invoke"],
        },
    ]
    frame = {
        "stamp": "20260912-120000",
        "hwnd": 12345,
        "width": 800,
        "height": 600,
        "coord_space": "window",
        "origin": {"left": 100, "top": 50, "width": 800, "height": 600},
        "items": [],
        "uia_nodes": nodes,
        "uia_count": len(nodes),
        "uia": True,
    }
    frame.update(extra)
    return frame


class TestUiaNodeToNormCenter(unittest.TestCase):
    def test_center(self):
        self.assertEqual(
            uia_node_to_norm_center({"norm": [20, 10, 80, 40]}),
            [50, 25],
        )


class TestFindUiaNodes(unittest.TestCase):
    def setUp(self):
        self.frame = _fixture_frame()

    def test_exact_name(self):
        hits = find_uia_nodes(self.frame, name="file")
        # two exact "File" (menu + button)
        self.assertEqual(len(hits), 2)
        self.assertTrue(all(h["name"] == "File" for h in hits))

    def test_exact_with_ctype(self):
        hits = find_uia_nodes(self.frame, name="File", control_type="MenuItemControl")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], 1)

    def test_ctype_suffix_flexible(self):
        hits = find_uia_nodes(self.frame, name="File", control_type="MenuItem")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["id"], 1)

    def test_substr_when_no_exact(self):
        hits = find_uia_nodes(self.frame, name="Open")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["name"], "Open File")

    def test_name_substr_param(self):
        hits = find_uia_nodes(self.frame, name_substr="file", clickable_only=False)
        # File, Filename:, Open File, File button
        names = {h["name"] for h in hits}
        self.assertIn("File", names)
        self.assertIn("Filename:", names)
        self.assertIn("Open File", names)

    def test_clickable_only(self):
        hits = find_uia_nodes(self.frame, name_substr="Filename", clickable_only=True)
        self.assertEqual(hits, [])
        hits2 = find_uia_nodes(self.frame, name_substr="Filename", clickable_only=False)
        self.assertEqual(len(hits2), 1)

    def test_missing(self):
        self.assertEqual(find_uia_nodes(self.frame, name="Nope"), [])


class TestResolveUiaTarget(unittest.TestCase):
    def setUp(self):
        self.frame = _fixture_frame()

    def test_exact_disambiguated(self):
        r = resolve_uia_target(self.frame, "File@MenuItemControl")
        self.assertTrue(r["ok"])
        self.assertEqual(r["norm"], [50, 25])
        self.assertEqual(r["node"]["id"], 1)

    def test_ambiguous_exact(self):
        r = resolve_uia_target(self.frame, "File")
        self.assertFalse(r["ok"])
        self.assertIn("ambiguous", r["error"])
        self.assertGreaterEqual(len(r.get("candidates") or []), 2)

    def test_substr_unique(self):
        r = resolve_uia_target(self.frame, "Open File")
        self.assertTrue(r["ok"])
        self.assertEqual(r["node"]["id"], 4)
        self.assertEqual(r["norm"], [350, 420])

    def test_missing(self):
        r = resolve_uia_target(self.frame, "NoSuchControl")
        self.assertFalse(r["ok"])
        self.assertIn("no UIA node", r["error"])

    def test_id_hash(self):
        r = resolve_uia_target(self.frame, "#2")
        self.assertTrue(r["ok"])
        self.assertEqual(r["node"]["name"], "Edit")
        self.assertEqual(r["norm"], [115, 25])

    def test_id_uia_prefix(self):
        r = resolve_uia_target(self.frame, "uia:2")
        self.assertTrue(r["ok"])
        self.assertEqual(r["node"]["id"], 2)

    def test_missing_uia_nodes_key(self):
        frame = {"stamp": "x", "hwnd": 1, "width": 10, "height": 10, "origin": {"left": 0, "top": 0}}
        r = resolve_uia_target(frame, "File")
        self.assertFalse(r["ok"])
        self.assertIn("perceive --uia first", r["error"])

    def test_empty_uia_nodes(self):
        frame = _fixture_frame(uia_nodes=[], uia_count=0)
        r = resolve_uia_target(frame, "File")
        self.assertFalse(r["ok"])
        self.assertIn("perceive --uia first", r["error"])


class TestParseAndPlanUiaName(unittest.TestCase):
    def test_parse_steps_token(self):
        specs = parse_steps(steps="uia:File,uia:Edit@MenuItemControl,uia:#12")
        self.assertEqual(
            [s["kind"] for s in specs],
            ["uia_name", "uia_name", "uia_name"],
        )
        self.assertEqual(specs[0]["query"], "File")
        self.assertEqual(specs[1]["query"], "Edit@MenuItemControl")
        self.assertEqual(specs[2]["query"], "#12")

    def test_parse_uia_name_arg(self):
        specs = parse_steps(uia_name="File,Edit")
        self.assertEqual(len(specs), 2)
        self.assertEqual(specs[0]["query"], "File")
        self.assertEqual(specs[1]["query"], "Edit")

    def test_plan_steps_norm_center(self):
        frame = _fixture_frame()
        specs = parse_steps(steps="uia:File@MenuItemControl")
        steps = plan_steps(frame, specs)
        self.assertEqual(len(steps), 1)
        s = steps[0]
        self.assertEqual(s["via"], "norm")
        self.assertEqual(s["norm"], [50, 25])
        self.assertEqual(s["uia_name"], "File")
        self.assertEqual(s["uia_node_id"], 1)
        # center in window px: nx/1000*w
        self.assertEqual(s["center"], [40, 15])
        self.assertEqual(s["screen"], [140, 65])

    def test_plan_id_token(self):
        frame = _fixture_frame()
        steps = plan_steps(frame, parse_steps(steps="uia:#4"))
        self.assertEqual(steps[0]["uia_node_id"], 4)
        self.assertEqual(steps[0]["norm"], [350, 420])


class TestRunChainMissingUia(unittest.TestCase):
    def test_missing_uia_nodes_dry(self):
        frame = {
            "stamp": "20260912-120000",
            "hwnd": 1,
            "width": 100,
            "height": 100,
            "coord_space": "window",
            "origin": {"left": 0, "top": 0, "width": 100, "height": 100},
            "items": [],
        }
        out = run_chain(frame, parse_steps(uia_name="File"), dry_run=True, method="auto")
        self.assertFalse(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertIn("perceive --uia first", out.get("error", ""))
        self.assertEqual(out.get("steps"), [])

    def test_empty_uia_nodes_dry(self):
        frame = _fixture_frame(uia_nodes=[], uia_count=0)
        out = run_chain(frame, parse_steps(steps="uia:File"), dry_run=True, method="input")
        self.assertFalse(out["ok"])
        self.assertIn("perceive --uia first", out.get("error", ""))


if __name__ == "__main__":
    unittest.main()
