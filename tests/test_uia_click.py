"""S2 method=uia: pattern preference + scope + dry-run helpers."""
from __future__ import annotations

import unittest
from unittest import mock

from cu_perceive.uia import (
    PATTERN_PREFERENCE,
    pick_preferred_action,
    public_element_info,
    screen_bbox_to_window,
)
from cu_perceive.act import (
    MethodError,
    normalize_method,
    parse_steps,
    run_chain,
    _uia_scope_error,
    _post_scope_error,
)


class TestPatternPreference(unittest.TestCase):
    def test_order(self):
        names = [n for n, _ in PATTERN_PREFERENCE]
        self.assertEqual(names, ["Invoke", "Toggle", "ExpandCollapse", "Select"])

    def test_pick_invoke_first(self):
        self.assertEqual(
            pick_preferred_action(["Select", "Toggle", "Invoke"]),
            "Invoke",
        )

    def test_pick_toggle_over_select(self):
        self.assertEqual(pick_preferred_action(["Select", "Toggle"]), "Toggle")

    def test_pick_expand(self):
        self.assertEqual(
            pick_preferred_action(["ExpandCollapse", "Select"]),
            "ExpandCollapse",
        )

    def test_pick_none(self):
        self.assertIsNone(pick_preferred_action(["Value", "Scroll"]))

    def test_public_strips_private(self):
        info = {"ok": True, "name": "File", "_control": object(), "patterns": ["Invoke"]}
        pub = public_element_info(info)
        self.assertNotIn("_control", pub)
        self.assertEqual(pub["name"], "File")

    def test_public_bbox_coord_space_screen(self):
        info = {
            "ok": True,
            "name": "File",
            "bbox": {
                "left": 100,
                "top": 200,
                "right": 180,
                "bottom": 240,
                "width": 80,
                "height": 40,
                "coord_space": "screen",
            },
            "bbox_coord_space": "screen",
            "_control": object(),
        }
        pub = public_element_info(info)
        self.assertEqual(pub["bbox"]["coord_space"], "screen")
        self.assertEqual(pub["bbox_coord_space"], "screen")
        self.assertNotIn("bbox_window", pub)

    def test_public_bbox_window_from_frame_origin(self):
        info = {
            "ok": True,
            "bbox": {
                "left": 110,
                "top": 220,
                "right": 190,
                "bottom": 260,
                "width": 80,
                "height": 40,
                "coord_space": "screen",
            },
            "bbox_coord_space": "screen",
        }
        frame = {
            "coord_space": "window",
            "origin": {"left": 10, "top": 20},
        }
        pub = public_element_info(info, frame)
        self.assertEqual(pub["bbox"]["coord_space"], "screen")
        self.assertEqual(pub["bbox_window"]["coord_space"], "window")
        self.assertEqual(pub["bbox_window"]["left"], 100)
        self.assertEqual(pub["bbox_window"]["top"], 200)
        self.assertEqual(pub["bbox_window"]["right"], 180)
        self.assertEqual(pub["bbox_window"]["bottom"], 240)
        self.assertEqual(pub["bbox_window"]["width"], 80)
        self.assertEqual(pub["bbox_window"]["height"], 40)


class TestScreenBboxToWindow(unittest.TestCase):
    def test_math(self):
        bbox = {
            "left": 500,
            "top": 400,
            "right": 580,
            "bottom": 440,
            "width": 80,
            "height": 40,
            "coord_space": "screen",
        }
        win = screen_bbox_to_window(bbox, origin_left=100, origin_top=50)
        self.assertEqual(win["left"], 400)
        self.assertEqual(win["top"], 350)
        self.assertEqual(win["right"], 480)
        self.assertEqual(win["bottom"], 390)
        self.assertEqual(win["width"], 80)
        self.assertEqual(win["height"], 40)
        self.assertEqual(win["coord_space"], "window")


class TestMethodNormalizeUia(unittest.TestCase):
    def test_uia(self):
        self.assertEqual(normalize_method("uia"), "uia")
        self.assertEqual(normalize_method("UIA"), "uia")

    def test_auto_landed(self):
        self.assertEqual(normalize_method("auto"), "auto")

    def test_post_still(self):
        self.assertEqual(normalize_method("post"), "post")


class TestUiaScope(unittest.TestCase):
    def test_ok_click(self):
        specs = parse_steps(norm="500,500")
        self.assertIsNone(_uia_scope_error(specs, hold_key=None))

    def test_reject_drag(self):
        specs = parse_steps(drag_norm="100,100;200,200")
        err = _uia_scope_error(specs, hold_key=None)
        self.assertIsNotNone(err)
        self.assertIn("drag", err)

    def test_reject_scroll_type(self):
        specs = parse_steps(norm="100,100", scroll="down:1", text="hi")
        err = _uia_scope_error(specs, hold_key=None)
        self.assertIsNotNone(err)

    def test_reject_right(self):
        specs = parse_steps(norm="100,100", button="right")
        err = _uia_scope_error(specs, hold_key=None)
        self.assertIsNotNone(err)
        self.assertIn("button=right", err)

    def test_post_still_allows_right(self):
        specs = parse_steps(norm="100,100", button="right")
        self.assertIsNone(_post_scope_error(specs, hold_key=None))


class TestRunChainUiaDry(unittest.TestCase):
    def setUp(self):
        p = mock.patch("cu_perceive.extras.missing_modules", return_value=[])
        p.start()
        self.addCleanup(p.stop)
        self.frame = {
            "stamp": "test-uia",
            "hwnd": 1,
            "coord_space": "window",
            "origin": {"left": 10, "top": 20, "width": 800, "height": 600},
            "width": 800,
            "height": 600,
            "items": [],
        }

    def test_fail_closed_drag(self):
        specs = parse_steps(drag_norm="100,100;200,200")
        out = run_chain(self.frame, specs, dry_run=True, method="uia")
        self.assertFalse(out["ok"])
        self.assertEqual(out["method"], "uia")
        self.assertIn("method=input", out["error"])

    def test_input_unchanged(self):
        specs = parse_steps(norm="100,200")
        out = run_chain(self.frame, specs, dry_run=True, method="input")
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "input")
        self.assertNotIn("uia", out["steps"][0])

    @mock.patch("cu_perceive.uia.resolve_at_screen")
    def test_dry_run_reports_element(self, resolve):
        resolve.return_value = {
            "ok": True,
            "method": "uia",
            "screen": [410, 320],
            "name": "File",
            "control_type": "MenuItemControl",
            "patterns": ["Invoke", "ExpandCollapse"],
            "preferred_action": "Invoke",
            "top_hwnd": 1,
            "bbox": {
                "left": 110,
                "top": 220,
                "right": 190,
                "bottom": 260,
                "width": 80,
                "height": 40,
                "coord_space": "screen",
            },
            "bbox_coord_space": "screen",
            "_control": object(),
        }
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=True, method="uia")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["method"], "uia")
        step = out["steps"][0]
        self.assertEqual(step["method"], "uia")
        self.assertEqual(step["uia"]["name"], "File")
        self.assertEqual(step["uia"]["control_type"], "MenuItemControl")
        self.assertIn("Invoke", step["uia"]["patterns"])
        self.assertEqual(step["uia"]["preferred_action"], "Invoke")
        self.assertEqual(step["uia"]["bbox"]["coord_space"], "screen")
        self.assertEqual(step["uia"]["bbox_window"]["left"], 100)
        self.assertEqual(step["uia"]["bbox_window"]["top"], 200)
        self.assertEqual(step["uia"]["bbox_window"]["coord_space"], "window")
        self.assertNotIn("_uia_control", step)
        self.assertNotIn("_control", step["uia"])
        resolve.assert_called()

    @mock.patch("cu_perceive.uia.resolve_at_screen")
    def test_no_pattern_fail_closed(self, resolve):
        resolve.return_value = {
            "ok": False,
            "error": "no Invoke/Toggle/ExpandCollapse/Select pattern on element",
            "method": "uia",
            "screen": [410, 320],
            "name": "Document",
            "control_type": "DocumentControl",
            "patterns": [],
            "preferred_action": None,
            "_control": object(),
        }
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=True, method="uia")
        self.assertFalse(out["ok"])
        self.assertIn("no Invoke", out["error"])


if __name__ == "__main__":
    unittest.main()
