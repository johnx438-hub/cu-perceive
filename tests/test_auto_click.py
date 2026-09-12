"""S3 method=auto: chooser rules + scope + dry-run chosen_method."""
from __future__ import annotations

import unittest
from unittest import mock

from cu_perceive.uia import choose_auto_method
from cu_perceive.act import (
    MethodError,
    normalize_method,
    parse_steps,
    run_chain,
    _auto_scope_error,
)


class TestChooseAutoMethod(unittest.TestCase):
    def test_uia_when_invoke_and_hwnd_match(self):
        self.assertEqual(
            choose_auto_method(
                preferred_action="Invoke",
                top_hwnd=42,
                expect_hwnd=42,
            ),
            "uia",
        )

    def test_input_when_no_preferred(self):
        self.assertEqual(
            choose_auto_method(
                preferred_action=None,
                top_hwnd=42,
                expect_hwnd=42,
            ),
            "input",
        )

    def test_input_when_hwnd_mismatch(self):
        self.assertEqual(
            choose_auto_method(
                preferred_action="Invoke",
                top_hwnd=99,
                expect_hwnd=42,
            ),
            "input",
        )

    def test_uia_when_top_hwnd_unknown_but_has_action(self):
        # Same as S2 resolve: only reject when both present and differ.
        self.assertEqual(
            choose_auto_method(
                preferred_action="Toggle",
                top_hwnd=None,
                expect_hwnd=42,
            ),
            "uia",
        )

    def test_uia_without_expect_hwnd(self):
        self.assertEqual(
            choose_auto_method(preferred_action="Select", top_hwnd=1, expect_hwnd=None),
            "uia",
        )

    def test_never_returns_post(self):
        for kwargs in (
            {"preferred_action": "Invoke", "top_hwnd": 1, "expect_hwnd": 1},
            {"preferred_action": None, "top_hwnd": 1, "expect_hwnd": 1},
            {"preferred_action": "Invoke", "top_hwnd": 2, "expect_hwnd": 1},
        ):
            self.assertIn(choose_auto_method(**kwargs), ("uia", "input"))


class TestMethodNormalizeAuto(unittest.TestCase):
    def test_auto(self):
        self.assertEqual(normalize_method("auto"), "auto")
        self.assertEqual(normalize_method("AUTO"), "auto")

    def test_others_still(self):
        self.assertEqual(normalize_method("uia"), "uia")
        self.assertEqual(normalize_method("post"), "post")
        self.assertEqual(normalize_method(None), "input")

    def test_bad(self):
        with self.assertRaises(MethodError):
            normalize_method("sky_click")


class TestAutoScope(unittest.TestCase):
    def test_ok_click(self):
        specs = parse_steps(norm="500,500")
        self.assertIsNone(_auto_scope_error(specs, hold_key=None))

    def test_reject_drag(self):
        specs = parse_steps(drag_norm="100,100;200,200")
        err = _auto_scope_error(specs, hold_key=None)
        self.assertIsNotNone(err)
        self.assertIn("drag", err)
        self.assertIn("method=input", err)

    def test_reject_right(self):
        specs = parse_steps(norm="100,100", button="right")
        err = _auto_scope_error(specs, hold_key=None)
        self.assertIsNotNone(err)
        self.assertIn("button=right", err)


class TestRunChainAutoDry(unittest.TestCase):
    def setUp(self):
        self.frame = {
            "stamp": "test-auto",
            "hwnd": 1,
            "coord_space": "window",
            "origin": {"left": 10, "top": 20, "width": 800, "height": 600},
            "width": 800,
            "height": 600,
            "items": [],
        }

    def test_fail_closed_drag(self):
        specs = parse_steps(drag_norm="100,100;200,200")
        out = run_chain(self.frame, specs, dry_run=True, method="auto")
        self.assertFalse(out["ok"])
        self.assertEqual(out["method"], "auto")
        self.assertIn("method=input", out["error"])

    @mock.patch("cu_perceive.uia.probe_at_screen")
    def test_dry_run_chooses_uia(self, probe):
        probe.return_value = {
            "ok": True,
            "method": "uia",
            "screen": [410, 320],
            "name": "File",
            "control_type": "MenuItemControl",
            "patterns": ["Invoke", "ExpandCollapse"],
            "preferred_action": "Invoke",
            "top_hwnd": 1,
            "_control": object(),
        }
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=True, method="auto")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["method"], "auto")
        self.assertEqual(out["chosen_method"], "uia")
        step = out["steps"][0]
        self.assertEqual(step["method"], "auto")
        self.assertEqual(step["chosen_method"], "uia")
        self.assertEqual(step["uia"]["name"], "File")
        self.assertEqual(step["uia"]["preferred_action"], "Invoke")
        self.assertNotIn("_uia_control", step)
        self.assertFalse(step["foreground_used"])
        self.assertFalse(out["foreground_used"])
        self.assertEqual(out["foreground_click_count"], 0)
        probe.assert_called()

    @mock.patch("cu_perceive.uia.probe_at_screen")
    def test_dry_run_chooses_input_no_pattern(self, probe):
        probe.return_value = {
            "ok": False,
            "error": "no Invoke/Toggle/ExpandCollapse/Select pattern on element",
            "method": "uia",
            "screen": [410, 320],
            "name": "Document",
            "control_type": "DocumentControl",
            "patterns": [],
            "preferred_action": None,
            "top_hwnd": 1,
            "_control": object(),
        }
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=True, method="auto")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["method"], "auto")
        self.assertEqual(out["chosen_method"], "input")
        step = out["steps"][0]
        self.assertEqual(step["chosen_method"], "input")
        self.assertEqual(step["uia"]["control_type"], "DocumentControl")
        self.assertIn("uia_skip_reason", step)
        self.assertTrue(step["foreground_used"])
        self.assertTrue(out["foreground_used"])
        self.assertGreaterEqual(out["foreground_click_count"], 1)

    @mock.patch("cu_perceive.uia.probe_at_screen")
    def test_hwnd_mismatch_chooses_input(self, probe):
        probe.return_value = {
            "ok": False,
            "error": "UIA hit top_hwnd=99 != stamp hwnd=1",
            "method": "uia",
            "screen": [410, 320],
            "name": "Other",
            "control_type": "ButtonControl",
            "patterns": ["Invoke"],
            "preferred_action": "Invoke",
            "top_hwnd": 99,
            "_control": object(),
        }
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=True, method="auto")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["chosen_method"], "input")

    @mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True, "stamp": "test-auto", "hwnd": 1})
    @mock.patch("cu_perceive.act.invoke_control", create=True)
    @mock.patch("cu_perceive.uia.invoke_control")
    @mock.patch("cu_perceive.uia.probe_at_screen")
    def test_go_uia_fail_no_input_fallback(self, probe, invoke, _unused, _stamp):
        probe.return_value = {
            "ok": True,
            "method": "uia",
            "screen": [410, 320],
            "name": "File",
            "control_type": "MenuItemControl",
            "patterns": ["Invoke"],
            "preferred_action": "Invoke",
            "top_hwnd": 1,
            "_control": object(),
        }
        from cu_perceive.uia import UiaError

        invoke.side_effect = UiaError("boom")
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=False, method="auto")
        self.assertFalse(out["ok"])
        self.assertIn("no input fallback", out["error"])
        self.assertEqual(out["chosen_method"], "uia")

    def test_input_dry_run_sets_foreground_used(self):
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=True, method="input")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["method"], "input")
        step = out["steps"][0]
        self.assertTrue(step["foreground_used"])
        self.assertTrue(out["foreground_used"])
        self.assertGreaterEqual(out["foreground_click_count"], 1)


if __name__ == "__main__":
    unittest.main()
