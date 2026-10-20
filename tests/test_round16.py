"""PR15: row-band reflow, post-focus baseline, covered-point hints, expect_change_unmet."""
import inspect
import unittest

import numpy as np

from cu_perceive import act as act_mod
from cu_perceive import chain, change


def _img(h=990, w=430):
    return np.full((h, w, 3), 255, dtype=np.uint8)


def _run(a, b, **kw):
    imgs = iter([a, b])
    return change.run_with_change_check(5, lambda: {"ok": True}, grab=lambda h: next(imgs),
                                        sleep=lambda s: None, **kw)


class TestRowBands(unittest.TestCase):
    def test_explorer_resort_flags_row_bands(self):
        a, b = _img(), _img()
        for i in range(9):  # 9 list rows in the top ~28%, text shifts per row
            y = 40 + i * 30
            b[y:y + 12, 30 + (i % 3) * 4:300] = 0
        out = _run(a, b)
        self.assertTrue(out["layout_reflow_suspected"])
        self.assertEqual(out["reflow_reason"], "row_bands")
        self.assertGreaterEqual(out["changed_row_bands"], 3)

    def test_hover_checkbox_noise_not_flagged(self):
        a = _img()
        hover = a.copy(); hover[400:410, 100:120] = 0
        box = a.copy(); box[100:130, 20:60] = 0; box[100:101, 20:250] = 0
        noise = a.copy()
        for x in range(0, 430, 60):
            noise[x:x + 3, x:x + 3] = 0
        for img in (hover, box, noise):
            out = _run(a, img)
            self.assertFalse(out.get("layout_reflow_suspected"), out)

    def test_narrow_bands_ignored(self):
        bands = [(i * 10, i * 10 + 5, 0, 20) for i in range(5)]
        self.assertEqual(change.similar_row_bands(bands, 430), 0)

    def test_reason_order(self):
        self.assertEqual(change.reflow_reason(0.033, [0, 0, 100, 174], bands=9), "row_bands")
        self.assertIsNone(change.reflow_reason(0.0032, [0, 0, 500, 520], bands=1))
        self.assertIsNone(change.reflow_reason(0.003, [0, 0, 1000, 1000], bands=5))


class TestFocusBaseline(unittest.TestCase):
    def test_baseline_taken_after_pre_hook(self):
        order = []
        a, b = _img(), _img()

        def grab(h):
            order.append("grab")
            return a if len(order) < 3 else b

        out = change.run_with_change_check(5, lambda: order.append("run") or {"ok": True}, grab=grab,
                                           sleep=lambda s: None,
                                           before_baseline=lambda: order.append("focus") or "after_focus")
        self.assertEqual(order[:2], ["focus", "grab"])
        self.assertEqual(out["change_baseline"], "after_focus")


class TestChainExpectChange(unittest.TestCase):
    def test_unmet_warns(self):
        out = chain.run_steps([{"key": "enter", "expect_change": True}], lambda s, send, i: {"ok": True}, send=True)
        self.assertTrue(out["expect_change_unmet"])
        self.assertTrue(any("check_change" in w for w in out["warnings"]))

    def test_dry_run_silent(self):
        out = chain.run_steps([{"key": "enter", "expect_change": True}], lambda s, send, i: {"ok": True}, send=False)
        self.assertNotIn("expect_change_unmet", out)


class TestCoveredHints(unittest.TestCase):
    def test_source_sets_suggested_hwnd(self):
        src = inspect.getsource(act_mod)
        self.assertGreaterEqual(src.count('out["suggested_hwnd"] = int(info'), 2)
        self.assertIn('out["suggested_hwnd"] = int(first["hwnd"])', src)

    def test_blocking_hint_mentions_perceive_window(self):
        self.assertIn("perceive_window", act_mod._blocking_hint(4242, "Cover"))
