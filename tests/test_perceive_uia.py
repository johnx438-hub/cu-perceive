"""S4 perceive --uia: bbox->norm helper + filter/caps."""
from __future__ import annotations

import time
import unittest
from unittest import mock

from cu_perceive.mcp_server import _compact_rows
from cu_perceive.uia import (
    UIA_CONTENT_MAX,
    UIA_MAX_DEPTH,
    UIA_MAX_NODES,
    UIA_VALUE_MAX,
    collect_uia_nodes,
    control_center_occluded,
    read_control_value,
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

    def test_empty_runtime_id_is_not_occlusion(self):
        # GetRuntimeId() is () for hwnd-less nodes on some machines. That used
        # to mark every toolbar, menu, grid cell and lookup row occluded.
        cell = _Node("qty", runtime=())
        same = _Node("qty", runtime=())
        other = _Node("covered", runtime=(7, 1))
        box = (0, 0, 40, 20)
        self.assertFalse(control_center_occluded(cell, box, hit_at=lambda x, y: same))
        self.assertTrue(control_center_occluded(cell, box, hit_at=lambda x, y: other))
        child = _Node("code", parent=cell, runtime=())
        self.assertFalse(control_center_occluded(cell, box, hit_at=lambda x, y: child))


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


class _Rect:
    left, top, right, bottom = 0, 0, 80, 24


class TestReadonlyAndTimeout(unittest.TestCase):
    def test_grid_cell_readonly_comes_from_legacy_state(self):
        class Pat:
            Value = "XS-0001"

            def IsReadOnly(self):
                return True

        class Cell:
            def GetPattern(self, pid):
                return Pat()

        auto = mock.Mock()
        auto.PatternId.ValuePattern = 1
        with mock.patch("cu_perceive.uia._auto", return_value=auto):
            value, readonly = read_control_value(Cell())
        self.assertEqual(value, "XS-0001")
        self.assertIs(readonly, True)

    def test_readonly_state_bit_when_value_pattern_has_no_flag(self):
        class Leg:
            State = 0x40

        class Cell:
            def GetPattern(self, pid):
                if pid == "legacy":
                    return Leg()
                return None

            def GetPropertyValue(self, pid):
                raise RuntimeError("no property")

        auto = mock.Mock()
        auto.PatternId.ValuePattern = 1
        auto.PatternId.LegacyIAccessiblePattern = "legacy"
        auto.PropertyId.ValueIsReadOnlyProperty = 30046
        with mock.patch("cu_perceive.uia._auto", return_value=auto):
            _value, readonly = read_control_value(Cell())
        self.assertIs(readonly, True)

    def test_datagrid_false_readonly_and_null_literal(self):
        class Pat:
            Value = "(null)"

            def IsReadOnly(self):
                return False

        class Cell:
            ControlTypeName = "DataItemControl"

            def GetPattern(self, pid):
                return Pat()

        class Edit:
            ControlTypeName = "EditControl"

            def GetPattern(self, pid):
                return Pat()

        auto = mock.Mock()
        auto.PatternId.ValuePattern = 1
        with mock.patch("cu_perceive.uia._auto", return_value=auto):
            value, readonly = read_control_value(Cell())
            edit_value, edit_ro = read_control_value(Edit())
        self.assertIsNone(value)
        self.assertIsNone(readonly)
        self.assertEqual(edit_value, "(null)")
        self.assertIs(edit_ro, False)

        class Root:
            Name = "qty"
            ControlTypeName = "DataItemControl"
            BoundingRectangle = _Rect()

            def GetPattern(self, pid):
                if pid == 1:
                    return Pat()
                return None

            def GetPropertyValue(self, pid):
                return False

            def GetRuntimeId(self):
                return (3,)

            def GetChildren(self):
                return []

        class Auto:
            class PatternId:
                ValuePattern = 1

            class PropertyId:
                IsOffscreenProperty = 9

            def ControlFromHandle(self, hwnd):
                return Root()

            def ControlFromPoint(self, x, y):
                return Root()

        with mock.patch("cu_perceive.uia._auto", return_value=Auto()), \
                mock.patch("cu_perceive.uia.list_available_patterns", return_value=[]):
            res = collect_uia_nodes(5, width=200, height=100, origin_left=0, origin_top=0, timeout_s=1)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(res["uia_nodes"]), 1)
        node = res["uia_nodes"][0]
        self.assertNotIn("value", node)
        self.assertIn("readonly", node)
        self.assertIsNone(node["readonly"])
        payload = {"items": [], "uia_nodes": [dict(node)]}
        _compact_rows(payload)
        self.assertIsNone(payload["uia_nodes"][0]["readonly"])
        self.assertNotIn("value", payload["uia_nodes"][0])

    def test_walk_timeout_returns_partial_nodes(self):
        class Root:
            Name = "Save"
            ControlTypeName = "ButtonControl"
            BoundingRectangle = _Rect()

            def GetPattern(self, pid):
                return None

            def GetPropertyValue(self, pid):
                return False

            def GetRuntimeId(self):
                return (1, 2)

            def GetChildren(self):
                time.sleep(2)
                return []

        class Auto:
            class PatternId:
                ValuePattern = 1
                LegacyIAccessiblePattern = 2

            class PropertyId:
                ValueIsReadOnlyProperty = 30046

            def ControlFromHandle(self, hwnd):
                return Root()

            def ControlFromPoint(self, x, y):
                return Root()

        with mock.patch("cu_perceive.uia._auto", return_value=Auto()), \
                mock.patch("cu_perceive.uia.list_available_patterns", return_value=["Invoke"]):
            res = collect_uia_nodes(
                5, width=200, height=100, origin_left=0, origin_top=0, timeout_s=0.15,
            )
        self.assertTrue(res["ok"], res)
        self.assertTrue(res.get("uia_timeout"))
        self.assertTrue(any("timed out" in w for w in res.get("warnings") or []))
        self.assertEqual([n["name"] for n in res["uia_nodes"]], ["Save"])


class TestPasswordValueIsNotRead(unittest.TestCase):
    def test_is_password_skips_value_pattern(self):
        class Edit:
            IsPassword = True
            NativeWindowHandle = 0

            def GetPattern(self, pid):
                raise AssertionError("password ValuePattern must not be requested")

        value, readonly = read_control_value(Edit())
        self.assertIsNone(value)
        self.assertIsNone(readonly)

    def test_es_password_skips_value_pattern(self):
        class Edit:
            NativeWindowHandle = 7

            def GetPattern(self, pid):
                raise AssertionError("password ValuePattern must not be requested")

        with mock.patch("cu_perceive.input._edit_style", return_value=0x0020):
            value, readonly = read_control_value(Edit())
        self.assertIsNone(value)
        self.assertIsNone(readonly)

    def test_collect_does_not_store_a_password_value(self):
        class Edit:
            Name = "secret"
            ControlTypeName = "EditControl"
            IsPassword = True
            NativeWindowHandle = 0
            BoundingRectangle = _Rect()

            def GetPattern(self, pid):
                raise AssertionError("password ValuePattern must not be requested")

            def GetChildren(self):
                return []

            def GetRuntimeId(self):
                return (1,)

        class Auto:
            class PatternId:
                ValuePattern = 1

            class PropertyId:
                IsOffscreenProperty = 30022

            def ControlFromHandle(self, hwnd):
                return Edit()

        with mock.patch("cu_perceive.uia._auto", return_value=Auto()), \
                mock.patch("cu_perceive.uia.list_available_patterns", return_value=["Value"]):
            res = collect_uia_nodes(5, width=200, height=100, origin_left=0, origin_top=0, timeout_s=1)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["uia_nodes"])
        self.assertNotIn("value", res["uia_nodes"][0])


if __name__ == "__main__":
    unittest.main()
