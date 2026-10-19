"""S4 perceive --uia: bbox->norm helper + filter/caps."""
from __future__ import annotations

import unittest

from cu_perceive.uia import (
    UIA_CONTENT_MAX,
    UIA_MAX_DEPTH,
    UIA_MAX_NODES,
    UIA_VALUE_MAX,
    control_center_occluded,
    screen_bbox_to_norm,
    should_keep_uia_node,
    truncate_uia_value,
)


class TestScreenBboxToNorm(unittest.TestCase):
    def test_origin_zero(self):
        # window client at (0,0), 1000x500 -> identity-ish
        n = screen_bbox_to_norm(0, 0, 100, 50, origin_left=0, origin_top=0, width=1000, height=500)
        self.assertEqual(n, [0, 0, 100, 100])

    def test_with_origin_offset(self):
        # client origin (100,200), size 800x600; element at screen (180,260)-(260,300)
        n = screen_bbox_to_norm(
            180, 260, 260, 300,
            origin_left=100, origin_top=200, width=800, height=600,
        )
        # (80/800)*1000=100, (60/600)*1000=100, (160/800)*1000=200, (100/600)*1000=166.6->167
        self.assertEqual(n[0], 100)
        self.assertEqual(n[1], 100)
        self.assertEqual(n[2], 200)
        self.assertEqual(n[3], 167)

    def test_clamps_to_0_1000(self):
        n = screen_bbox_to_norm(
            -50, -50, 2000, 2000,
            origin_left=0, origin_top=0, width=1000, height=1000,
        )
        self.assertEqual(n, [0, 0, 1000, 1000])

    def test_matches_act_norm_center(self):
        # center of norm box should match act: nx/1000*w + origin
        n = screen_bbox_to_norm(
            178, 252, 218, 284,
            origin_left=78, origin_top=220, width=885, height=1054,
        )
        cx = (n[0] + n[2]) / 2
        cy = (n[1] + n[3]) / 2
        # reverse via same formula as act.norm_to_window + window_to_screen
        wx = cx / 1000 * 885
        wy = cy / 1000 * 1054
        sx = 78 + wx
        sy = 220 + wy
        self.assertAlmostEqual(sx, (178 + 218) / 2, delta=2)
        self.assertAlmostEqual(sy, (252 + 284) / 2, delta=2)


class TestShouldKeepUiaNode(unittest.TestCase):
    def test_keep_menuitem_with_name(self):
        self.assertTrue(
            should_keep_uia_node(
                name="File",
                control_type="MenuItemControl",
                patterns=["Invoke", "ExpandCollapse"],
                norm=[10, 20, 50, 40],
            )
        )

    def test_skip_offscreen(self):
        self.assertFalse(
            should_keep_uia_node(
                name="File",
                control_type="MenuItemControl",
                patterns=["Invoke"],
                norm=[10, 20, 50, 40],
                offscreen=True,
            )
        )

    def test_skip_zero_area(self):
        self.assertFalse(
            should_keep_uia_node(
                name="X",
                control_type="ButtonControl",
                patterns=["Invoke"],
                norm=[10, 20, 10, 40],
            )
        )

    def test_skip_empty_name_when_flag(self):
        self.assertFalse(
            should_keep_uia_node(
                name="",
                control_type="ButtonControl",
                patterns=["Invoke"],
                norm=[10, 20, 50, 40],
                skip_empty_name=True,
            )
        )

    def test_skip_pane_no_pattern(self):
        self.assertFalse(
            should_keep_uia_node(
                name="",
                control_type="PaneControl",
                patterns=[],
                norm=[0, 0, 900, 900],
            )
        )

    def test_keep_edit_even_without_invoke(self):
        self.assertTrue(
            should_keep_uia_node(
                name="Document",
                control_type="EditControl",
                patterns=[],
                norm=[10, 100, 900, 900],
            )
        )

    def test_keep_text_label_and_dataitem_value(self):
        self.assertTrue(
            should_keep_uia_node(
                name="保存成功，单号 XS-0001",
                control_type="TextControl",
                patterns=[],
                norm=[10, 20, 400, 80],
            )
        )
        self.assertTrue(
            should_keep_uia_node(
                name="",
                control_type="DataItemControl",
                patterns=[],
                norm=[10, 40, 80, 70],
                value="B2003",
            )
        )
        self.assertFalse(
            should_keep_uia_node(
                name="",
                control_type="DataItemControl",
                patterns=[],
                norm=[10, 40, 80, 70],
                value="",
            )
        )

    def test_content_cap_drops_extra_text_nodes(self):
        self.assertFalse(
            should_keep_uia_node(
                name="row",
                control_type="Text",
                patterns=[],
                norm=[1, 1, 10, 10],
                content_kept=UIA_CONTENT_MAX,
            )
        )
        self.assertTrue(
            should_keep_uia_node(
                name="OK",
                control_type="ButtonControl",
                patterns=["Invoke"],
                norm=[1, 1, 10, 10],
                content_kept=UIA_CONTENT_MAX,
            )
        )

    def test_value_is_truncated(self):
        raw = "x" * (UIA_VALUE_MAX + 20)
        out = truncate_uia_value(raw)
        self.assertEqual(len(out), UIA_VALUE_MAX)
        self.assertTrue(out.endswith("..."))


class _Node:
    def __init__(self, name, parent=None, runtime=None):
        self.name = name
        self.parent = parent
        self.runtime = runtime if runtime is not None else (id(self),)

    def GetParentControl(self):
        return self.parent

    def GetRuntimeId(self):
        return self.runtime


class TestOccludedMdi(unittest.TestCase):
    def test_front_child_hides_the_back_child(self):
        back = _Node("order")
        front = _Node("report")
        cell = _Node("code", parent=back)
        box = (0, 0, 40, 20)
        self.assertTrue(control_center_occluded(back, box, hit_at=lambda x, y: front))
        self.assertFalse(control_center_occluded(front, box, hit_at=lambda x, y: front))
        self.assertFalse(control_center_occluded(back, box, hit_at=lambda x, y: cell))
        self.assertFalse(control_center_occluded(back, box, hit_at=lambda x, y: None))


class TestCapsDefaults(unittest.TestCase):
    def test_caps(self):
        self.assertEqual(UIA_MAX_NODES, 200)
        self.assertEqual(UIA_MAX_DEPTH, 16)

    def test_filter_cap_simulation(self):
        # Simulate applying max_nodes on a long candidate stream.
        kept = []
        max_nodes = 5
        for i in range(20):
            ok = should_keep_uia_node(
                name=f"B{i}",
                control_type="ButtonControl",
                patterns=["Invoke"],
                norm=[i, i, i + 10, i + 10],
            )
            if ok:
                kept.append(i)
            if len(kept) >= max_nodes:
                break
        self.assertEqual(len(kept), 5)


if __name__ == "__main__":
    unittest.main()
