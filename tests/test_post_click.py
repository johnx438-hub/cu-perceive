"""S1 method=post: lParam packing + dry-run / fail-closed scope."""
from __future__ import annotations

import unittest

from _fakes import stub_live_window

from cu_perceive.input import (
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MOUSEMOVE,
    WM_RBUTTONDOWN,
    pack_lparam,
    post_click_messages,
)
from cu_perceive.act import (
    MethodError,
    normalize_method,
    parse_steps,
    run_chain,
    _post_scope_error,
)


class TestPackLParam(unittest.TestCase):
    def test_basic(self):
        lp = pack_lparam(100, 200)
        self.assertEqual(lp & 0xFFFF, 100)
        self.assertEqual((lp >> 16) & 0xFFFF, 200)

    def test_large_client(self):
        lp = pack_lparam(1920, 1080)
        self.assertEqual(lp & 0xFFFF, 1920)
        self.assertEqual((lp >> 16) & 0xFFFF, 1080)

    def test_sequence_left(self):
        seq = post_click_messages(50, 75, button="left")
        self.assertEqual(len(seq), 3)
        self.assertEqual(seq[0]["msg_id"], WM_MOUSEMOVE)
        self.assertEqual(seq[1]["msg_id"], WM_LBUTTONDOWN)
        self.assertEqual(seq[1]["wparam"], 0x0001)
        self.assertEqual(seq[2]["msg_id"], WM_LBUTTONUP)
        self.assertEqual(seq[0]["lparam"], pack_lparam(50, 75))

    def test_sequence_right_no_double(self):
        seq = post_click_messages(10, 20, button="right", double=True)
        self.assertEqual(len(seq), 3)
        self.assertEqual(seq[1]["msg_id"], WM_RBUTTONDOWN)

    def test_double_left(self):
        seq = post_click_messages(1, 2, button="left", double=True)
        self.assertEqual(len(seq), 5)


class TestMethodNormalize(unittest.TestCase):
    def test_default(self):
        self.assertEqual(normalize_method(None), "input")
        self.assertEqual(normalize_method(""), "input")
        self.assertEqual(normalize_method("INPUT"), "input")

    def test_post(self):
        self.assertEqual(normalize_method("post"), "post")

    def test_uia_landed(self):
        self.assertEqual(normalize_method("uia"), "uia")

    def test_auto_landed(self):
        self.assertEqual(normalize_method("auto"), "auto")

    def test_bad(self):
        with self.assertRaises(MethodError):
            normalize_method("sky_click")


class TestPostScope(unittest.TestCase):
    def test_ok_click(self):
        specs = parse_steps(norm="500,500")
        self.assertIsNone(_post_scope_error(specs, hold_key=None))

    def test_reject_drag(self):
        specs = parse_steps(norm="100,100", drag_norm="100,100;200,200")
        err = _post_scope_error(specs, hold_key=None)
        self.assertIsNotNone(err)
        self.assertIn("drag", err)

    def test_reject_scroll(self):
        specs = parse_steps(norm="100,100", scroll="down:1")
        err = _post_scope_error(specs, hold_key=None)
        self.assertIsNotNone(err)

    def test_reject_hold_key(self):
        specs = parse_steps(norm="100,100")
        err = _post_scope_error(specs, hold_key="ctrl")
        self.assertIsNotNone(err)
        self.assertIn("hold_key", err)


class TestRunChainPostDry(unittest.TestCase):
    def setUp(self):
        stub_live_window(self, 1)
        self.frame = {
            "stamp": "test-post",
            "hwnd": 1,
            "coord_space": "window",
            "origin": {"left": 10, "top": 20, "width": 800, "height": 600},
            "width": 800,
            "height": 600,
            "items": [],
        }

    def test_dry_run_or_clear_error(self):
        specs = parse_steps(norm="500,500")
        out = run_chain(self.frame, specs, dry_run=True, method="post")
        self.assertEqual(out.get("method"), "post")
        if out.get("ok"):
            self.assertTrue(out["dry_run"])
            step = out["steps"][0]
            self.assertEqual(step["method"], "post")
            self.assertIn("client", step)
            self.assertIn("post_messages", step)
            self.assertEqual(step["screen"], [410, 320])
        else:
            self.assertIn("error", out)

    def test_fail_closed_drag(self):
        specs = parse_steps(drag_norm="100,100;200,200")
        out = run_chain(self.frame, specs, dry_run=True, method="post")
        self.assertFalse(out["ok"])
        self.assertIn("method=input", out["error"])

    def test_input_unchanged(self):
        specs = parse_steps(norm="100,200")
        out = run_chain(self.frame, specs, dry_run=True, method="input")
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "input")
        self.assertNotIn("post_messages", out["steps"][0])


if __name__ == "__main__":
    unittest.main()
