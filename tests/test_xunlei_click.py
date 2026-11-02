"""fix/xunlei-click: UIA ancestor walk + target match (R1), overlay window pick and
point_not_on_target (R2), uia_fallback on empty Chromium trees (R3). All mocked."""
from __future__ import annotations

import tempfile
import unittest
from unittest import mock

import numpy as np

from _fakes import stub_live_window
from test_round4_points import COVER, HWND, _Desk, _window_frame

from cu_perceive import uia as uia_mod
from cu_perceive import windows as win_mod
from cu_perceive.act import parse_steps, run_chain

TOP = 3807628


class _Rect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class _Top:
    def __init__(self, hwnd):
        self.NativeWindowHandle = hwnd


class FakeCtrl:
    def __init__(self, name, ctype, rect, patterns=(), parent=None, top=TOP):
        self.Name, self.ControlTypeName = name, ctype
        self.BoundingRectangle = _Rect(*rect)
        self.patterns = list(patterns)
        self.parent = parent
        self.top = top

    def GetParentControl(self):
        return self.parent

    def GetTopLevelControl(self):
        return _Top(self.top)


def _patch_hit(testcase, ctrl):
    for p in (
        mock.patch("cu_perceive.uia.element_from_screen_point", return_value=ctrl),
        mock.patch("cu_perceive.uia.list_available_patterns", side_effect=lambda c: list(c.patterns)),
    ):
        p.start()
        testcase.addCleanup(p.stop)


def _chromium_button():
    """Xunlei: Document > Button '\\ue65f 新建任务' (Invoke) > Text '新建任务'."""
    doc = FakeCtrl("迅雷", "DocumentControl", (138, 290, 1218, 1010))
    btn = FakeCtrl("\ue65f 新建任务", "ButtonControl", (700, 825, 830, 860), ["Invoke"], parent=doc)
    text = FakeCtrl("新建任务", "TextControl", (749, 836, 801, 851), parent=btn)
    return doc, btn, text


class TestResolveAncestorWalk(unittest.TestCase):
    def test_child_text_resolves_to_button(self):
        _doc, _btn, text = _chromium_button()
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(766, 842, expect_hwnd=TOP)
        self.assertTrue(info["ok"], info.get("error"))
        self.assertEqual(info["control_type"], "ButtonControl")
        self.assertEqual(info["preferred_action"], "Invoke")
        self.assertEqual(info["pattern"], "Invoke")
        self.assertEqual(info["ancestor_hops"], 1)
        self.assertEqual(info["hit"], {"name": "新建任务", "control_type": "TextControl"})

    def test_direct_invokable_hit_unchanged(self):
        _doc, btn, _text = _chromium_button()
        _patch_hit(self, btn)
        info = uia_mod.resolve_at_screen(766, 842, expect_hwnd=TOP)
        self.assertTrue(info["ok"])
        self.assertEqual(info["ancestor_hops"], 0)
        self.assertNotIn("hit", info)
        self.assertEqual(info["name"], "\ue65f 新建任务")

    def test_document_only_hit_fails_closed(self):
        doc, _btn, _text = _chromium_button()
        _patch_hit(self, doc)
        info = uia_mod.resolve_at_screen(813, 325, expect_hwnd=TOP)
        self.assertFalse(info["ok"])
        self.assertIn("no Invoke/Toggle/ExpandCollapse/Select pattern", info["error"])

    def test_never_resolves_to_document_with_pattern(self):
        doc = FakeCtrl("迅雷", "DocumentControl", (0, 0, 2000, 2000), ["Invoke"])
        text = FakeCtrl("label", "TextControl", (10, 10, 50, 50), parent=doc)
        _patch_hit(self, text)
        self.assertFalse(uia_mod.resolve_at_screen(20, 20)["ok"])

    def test_ancestor_not_containing_point_fails_closed(self):
        btn = FakeCtrl("新建任务", "ButtonControl", (0, 0, 100, 100), ["Invoke"])
        text = FakeCtrl("新建任务", "TextControl", (749, 836, 801, 851), parent=btn)
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(766, 842)
        self.assertFalse(info["ok"])
        self.assertEqual(info["control_type"], "TextControl")

    def test_ancestor_in_other_top_level_window_fails_closed(self):
        btn = FakeCtrl("新建任务", "ButtonControl", (700, 825, 830, 860), ["Invoke"], top=999)
        text = FakeCtrl("新建任务", "TextControl", (749, 836, 801, 851), parent=btn)
        _patch_hit(self, text)
        self.assertFalse(uia_mod.resolve_at_screen(766, 842)["ok"])

    def test_depth_limit(self):
        top = FakeCtrl("deep", "ButtonControl", (0, 0, 2000, 2000), ["Invoke"])
        cur = top
        for i in range(5):  # 5 pattern-less groups between the hit and the button
            cur = FakeCtrl(f"g{i}", "GroupControl", (0, 0, 2000, 2000), parent=cur)
        hit = FakeCtrl("x", "TextControl", (10, 10, 50, 50), parent=cur)
        _patch_hit(self, hit)
        info = uia_mod.resolve_at_screen(20, 20)
        self.assertFalse(info["ok"])
        self.assertIn("within 4 levels", info["error"])
        ok = uia_mod.resolve_at_screen(20, 20, max_hops=6)
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["ancestor_hops"], 6)

    def test_expected_name_mismatch(self):
        login = FakeCtrl("登录", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        _patch_hit(self, login)
        info = uia_mod.resolve_at_screen(
            766, 842, expect_name="\ue65f 新建任务", expect_control_type="ButtonControl"
        )
        self.assertFalse(info["ok"])
        self.assertEqual(info["reason"], "uia_target_mismatch")
        self.assertIn("登录", info["error"])
        self.assertIn("新建任务", info["error"])

    def test_expected_type_mismatch(self):
        _doc, _btn, text = _chromium_button()
        _patch_hit(self, text)
        # D1: the hit TextControl now matches by name+type
        info = uia_mod.resolve_at_screen(766, 842, expect_name="新建任务", expect_control_type="CheckBoxControl")
        self.assertEqual(info.get("reason"), "uia_target_mismatch")

    def test_glyph_prefixed_name_matches(self):
        _doc, _btn, text = _chromium_button()
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(766, 842, expect_name="新建任务", expect_control_type="Button")
        self.assertTrue(info["ok"], info.get("error"))
        self.assertEqual(uia_mod.normalize_uia_name("\ue65f 新建任务"), uia_mod.normalize_uia_name("新建任务"))

    def test_auto_probe_mismatch_never_chooses_uia(self):
        login = FakeCtrl("登录", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        _patch_hit(self, login)
        info = uia_mod.probe_at_screen(766, 842, expect_name="新建任务", expect_control_type="ButtonControl")
        self.assertIsNone(info["preferred_action"])
        self.assertFalse(uia_mod.is_soft_uia_miss(info))


def _xunlei_frame():
    # origin (138,290) 1080x720; node norm center (582,767) -> screen (766,842)
    return {
        "stamp": "20261102-165845-921",
        "hwnd": TOP,
        "coord_space": "window",
        "origin": {"left": 138, "top": 290, "width": 1080, "height": 720},
        "width": 1080,
        "height": 720,
        "items": [],
        "uia": True,
        "uia_nodes": [
            {"id": 150, "name": "\ue65f 新建任务", "control_type": "ButtonControl",
             "norm": [557, 747, 607, 787], "clickable": True, "patterns": ["Invoke"]},
            {"id": 153, "name": "新建任务", "control_type": "TextControl",
             "norm": [565, 760, 600, 775], "clickable": False},
        ],
    }


class TestRunChainUiaXunlei(unittest.TestCase):
    def setUp(self):
        stub_live_window(self, TOP)
        p = mock.patch("cu_perceive.extras.missing_modules", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def test_uia_name_resolves_through_label(self):
        _doc, _btn, text = _chromium_button()
        _patch_hit(self, text)
        out = run_chain(_xunlei_frame(), parse_steps(uia_name="新建任务"), dry_run=True, method="uia")
        self.assertTrue(out["ok"], out.get("error"))
        u = out["steps"][0]["uia"]
        self.assertEqual(u["control_type"], "ButtonControl")
        self.assertEqual(u["ancestor_hops"], 1)
        self.assertEqual(u["hit"]["control_type"], "TextControl")

    def test_uia_name_on_other_control_refused(self):
        login = FakeCtrl("登录", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        _patch_hit(self, login)
        out = run_chain(_xunlei_frame(), parse_steps(uia_name="新建任务"), dry_run=True, method="uia")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "uia_target_mismatch")
        self.assertEqual(out["resolved_uia"]["name"], "登录")
        self.assertEqual(out["expected_uia"]["name"], "\ue65f 新建任务")

    def test_live_mismatch_invokes_nothing(self):
        login = FakeCtrl("登录", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        _patch_hit(self, login)
        with mock.patch("cu_perceive.uia.invoke_control") as inv, mock.patch("time.sleep"):
            out = run_chain(_xunlei_frame(), parse_steps(uia_name="新建任务"), dry_run=False,
                            method="uia", new_window_wait_s=0)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "uia_target_mismatch")
        inv.assert_not_called()


def _row(hwnd, title, rect, exstyle=0x100, minimized=False, pid=40900):
    return {"hwnd": hwnd, "title": title, "pid": pid, "ctime": "1", "exe": "Thunder.exe",
            "exe_path": "C:/x/Thunder.exe", "class": "Chrome_WidgetWin_0", "rect": list(rect),
            "minimized": minimized, "exstyle": exstyle}


MONITORS = [(0, 0, 2560, 1440)]
OVERLAY = _row(6689582, "suspension-renderer", (2060, -160, 2868, 532), exstyle=0x00080008)
MAIN = _row(3807628, "迅雷", (138, 290, 1218, 1010))


class TestOverlayPick(unittest.TestCase):
    def _find(self, rows, **kw):
        with mock.patch("cu_perceive.windows.list_windows", return_value=[dict(r) for r in rows]), \
                mock.patch("cu_perceive.windows.monitor_rects", return_value=MONITORS):
            ranked = win_mod.find_windows(**kw)
            return ranked, win_mod.pick_info(ranked)

    def test_overlay_reasons(self):
        self.assertEqual(win_mod.overlay_reasons(OVERLAY, MONITORS), ["layered", "offscreen"])
        self.assertEqual(win_mod.overlay_reasons(MAIN, MONITORS), [])
        ball = _row(1, "ball", (100, 100, 152, 156), exstyle=0x80)
        self.assertEqual(win_mod.overlay_reasons(ball, MONITORS), ["toolwindow", "tiny"])

    def test_main_picked_over_overlay(self):
        ranked, meta = self._find([OVERLAY, MAIN], exe="thunder")
        self.assertEqual(ranked[0]["hwnd"], MAIN["hwnd"])
        self.assertEqual(meta["pick_reason"], "overlay_deprioritized")
        self.assertEqual(meta["deprioritized"][0]["hwnd"], OVERLAY["hwnd"])
        self.assertNotIn("pick_warning", meta)

    def test_only_overlay_picked_with_warning(self):
        ranked, meta = self._find([OVERLAY], exe="thunder")
        self.assertEqual(ranked[0]["hwnd"], OVERLAY["hwnd"])
        self.assertEqual(meta["pick_warning"], "overlay_candidate")
        self.assertIsNone(meta["suggested_hwnd"])

    def test_exact_title_overlay_warns_with_suggestion(self):
        ranked, meta = self._find([OVERLAY, MAIN], title="suspension-renderer")
        self.assertEqual(ranked[0]["hwnd"], OVERLAY["hwnd"])
        self.assertEqual(meta["pick_warning"], "overlay_candidate")

    def test_hwnd_untouched(self):
        with mock.patch("cu_perceive.windows.list_windows", return_value=[dict(OVERLAY), dict(MAIN)]), \
                mock.patch("cu_perceive.windows.monitor_rects") as mon:
            rows = win_mod.find_windows(hwnd=OVERLAY["hwnd"])
        self.assertEqual([r["hwnd"] for r in rows], [OVERLAY["hwnd"]])
        self.assertNotIn("overlay", rows[0])
        mon.assert_not_called()

    def test_cli_windows_reports_pick(self):
        import io
        import json
        from contextlib import redirect_stdout

        from cu_perceive.cli import main

        buf = io.StringIO()
        with mock.patch("cu_perceive.windows.list_windows", return_value=[dict(OVERLAY), dict(MAIN)]), \
                mock.patch("cu_perceive.windows.monitor_rects", return_value=MONITORS), redirect_stdout(buf):
            rc = main(["windows", "--exe", "thunder"])
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(out["window"]["hwnd"], MAIN["hwnd"])
        self.assertEqual(out["pick_reason"], "overlay_deprioritized")
        self.assertEqual(out["windows"][1]["overlay"], ["layered", "offscreen"])


POPUP = 777001
PIDS = {HWND: 4242, COVER: 999, POPUP: 4242}


class _OffTargetDesk(_Desk):
    """_Desk plus a readable target window: rect (300,200)-(1100,700), pids per PIDS."""

    def setUp(self):
        super().setUp()
        self.exstyle = 0x100
        self.at_override = None
        for target, fn in {
            "window_pid": lambda h: PIDS.get(int(h), 0),
            "root_window": lambda h: int(h),
            "window_rect": lambda h: (300, 200, 1100, 700),
            "window_exstyle": lambda h: self.exstyle,
            "window_title": lambda h: {COVER: "Grok Bot", POPUP: "popup"}.get(int(h), "T"),
        }.items():
            p = mock.patch(f"cu_perceive._win32.{target}", side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def _owner(self, hwnd, x, y):
        if self.at_override is not None:
            at, ok = self.at_override
            return ok, {"hwnd": at, "title": "popup"}
        return super()._owner(hwnd, x, y)


class TestPointNotOnTarget(_OffTargetDesk):
    def test_point_outside_window_refused(self):
        self.covered = {(1200, 800)}
        out = self.go(xy="900,600")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "point_not_on_target")
        self.assertEqual(out["point_window"]["why"], "outside_window")
        self.assertEqual(out["point_window"]["pid"], 999)
        self.assertEqual(out["blocking_window"]["pid"], 999)
        self.assertEqual(out["foreground_click_count"], 0)
        self.assertEqual(self.calls, [])

    def test_other_process_window_at_point_refused(self):
        self.at_override = (COVER, True)  # owner check passes but the window is another process
        out = self.go(xy="100,100")
        self.assertEqual(out["reason"], "point_not_on_target")
        self.assertEqual(out["point_window"]["why"], "other_process")
        self.assertEqual(self.calls, [])

    def test_transparent_overlay_refused_even_with_focus(self):
        self.exstyle = 0x00080008
        self.covered = {(400, 300)}
        self.topmost = True  # raising the overlay does not stop clicks passing through it
        out = self.go(xy="100,100", focus=True)
        self.assertEqual(out["reason"], "point_not_on_target")
        self.assertEqual(out["point_window"]["why"], "transparent_overlay")
        self.assertFalse(any(c.startswith("click") for c in self.calls))

    def test_same_process_owned_popup_outside_rect_allowed(self):
        self.at_override = (POPUP, True)
        out = self.go(xy="900,600")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(self.calls, ["click:1200,800"])

    def test_same_process_window_point_allowed(self):
        out = self.go(xy="100,100")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(self.calls, ["click:400,300"])

    def test_covered_by_other_app_inside_rect_stays_point_covered(self):
        self.covered = {(400, 300)}
        out = self.go(xy="100,100")
        self.assertEqual(out["reason"], "point_covered")

    def test_dry_run_warns(self):
        self.covered = {(1200, 800)}
        out = run_chain(_window_frame(), parse_steps(xy="900,600"), dry_run=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["points_not_on_target"][0]["why"], "outside_window")
        self.assertEqual(out["occluded_points"], [])
        self.assertTrue(any("point_not_on_target" in w for w in out["warnings"]))


class TestUiaFallbackHint(unittest.TestCase):
    def test_empty_chromium_tree(self):
        from cu_perceive.capture import CaptureResult
        from cu_perceive.core import perceive

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        win = {"hwnd": 1842978, "title": "新建任务", "exe": "Thunder.exe", "pid": 1, "ctime": "2",
               "rect": [338, 500, 1018, 800]}
        origin = {"left": 338, "top": 500, "width": 680, "height": 300}
        ident = {"hwnd": 1842978, "pid": 1, "ctime": "2", "class": "Chrome_WidgetWin_0"}
        img = np.full((300, 680, 3), 90, np.uint8)
        with mock.patch("cu_perceive.windows.find_window", return_value=win), \
                mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=False), \
                mock.patch("cu_perceive.windows.window_identity", return_value=ident), \
                mock.patch("cu_perceive.windows.verify_window_identity", return_value=None), \
                mock.patch("cu_perceive.windows.live_geom", return_value=(origin, win)), \
                mock.patch("cu_perceive.windows.capture_window_image",
                           return_value=CaptureResult(img, "wgc", [])), \
                mock.patch("cu_perceive.extras.require"), \
                mock.patch("cu_perceive.uia.collect_uia_nodes",
                           return_value={"ok": True, "uia_nodes": [], "uia_count": 0}):
            out = perceive(hwnd=1842978, out_dir=td.name, uia=True)
        self.assertEqual(out["uia_count"], 0)
        self.assertEqual(out["uia_fallback"], "no_accessibility_tree")
        self.assertIn("ocr", out["uia_fallback_hint"])


def _yunpan_nav():
    doc = FakeCtrl("迅雷", "DocumentControl", (138, 290, 1218, 1010))
    wrap = FakeCtrl("", "TextControl", (138, 395, 330, 445), ["Invoke"], parent=doc)
    text = FakeCtrl("云盘", "TextControl", (150, 405, 320, 435), parent=wrap)
    return doc, wrap, text


class TestNamePolicyD1(unittest.TestCase):
    def setUp(self):
        stub_live_window(self, TOP)
        p = mock.patch("cu_perceive.extras.missing_modules", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def test_hit_name_matches_unnamed_invokable_wrapper(self):
        _doc, _wrap, text = _yunpan_nav()
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(
            224, 419, expect_hwnd=TOP, expect_name="云盘", expect_control_type="TextControl"
        )
        self.assertTrue(info["ok"], info.get("error"))
        self.assertEqual(info["ancestor_hops"], 1)
        self.assertEqual(info["name"], "")
        self.assertEqual(info["name_matched"], "hit")
        self.assertEqual(info["pattern"], "Invoke")

    def test_resolved_name_still_matches(self):
        _doc, _btn, text = _chromium_button()
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(
            766, 842, expect_hwnd=TOP, expect_name="\ue65f 新建任务", expect_control_type="ButtonControl"
        )
        self.assertTrue(info["ok"], info.get("error"))
        self.assertEqual(info["name_matched"], "resolved")

    def test_hit_name_ignored_when_not_descendant(self):
        _doc, _wrap, text = _yunpan_nav()
        _patch_hit(self, text)
        with mock.patch("cu_perceive.uia._uia_is_descendant", return_value=False):
            info = uia_mod.resolve_at_screen(
                224, 419, expect_hwnd=TOP, expect_name="云盘", expect_control_type="TextControl"
            )
        self.assertFalse(info["ok"])
        self.assertEqual(info["reason"], "uia_target_mismatch")

    def test_type_checked_on_name_matched_node(self):
        _doc, _wrap, text = _yunpan_nav()
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(
            224, 419, expect_hwnd=TOP, expect_name="云盘", expect_control_type="ButtonControl"
        )
        self.assertFalse(info["ok"])
        self.assertEqual(info.get("reason"), "uia_target_mismatch")

    def test_stale_login_plan_still_refused(self):
        login = FakeCtrl("登录", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        _patch_hit(self, login)
        info = uia_mod.resolve_at_screen(
            766, 842, expect_name="\ue65f 新建任务", expect_control_type="ButtonControl"
        )
        self.assertFalse(info["ok"])
        self.assertEqual(info["reason"], "uia_target_mismatch")
        self.assertEqual(info["ancestor_hops"], 0)
        self.assertIn("登录", info["error"])

    def test_stale_login_label_in_login_button_refused(self):
        btn = FakeCtrl("登录", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        text = FakeCtrl("登录", "TextControl", (700, 825, 830, 860), parent=btn)
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(
            766, 842, expect_name="\ue65f 新建任务", expect_control_type="ButtonControl"
        )
        self.assertFalse(info["ok"])
        self.assertEqual(info["reason"], "uia_target_mismatch")

    def test_run_chain_stale_plan_refused_uia_and_live(self):
        login = FakeCtrl("登录", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        _patch_hit(self, login)
        dry = run_chain(_xunlei_frame(), parse_steps(uia_name="新建任务"), dry_run=True, method="uia")
        self.assertEqual(dry["reason"], "uia_target_mismatch")
        with mock.patch("cu_perceive.uia.invoke_control") as inv, mock.patch("time.sleep"):
            live = run_chain(
                _xunlei_frame(), parse_steps(uia_name="新建任务"), dry_run=False,
                method="uia", new_window_wait_s=0,
            )
        self.assertEqual(live["reason"], "uia_target_mismatch")
        inv.assert_not_called()

    def test_unnamed_node_plan_has_no_expect_name(self):
        from cu_perceive.act import plan_steps, uia_step_expectation

        frame = _xunlei_frame()
        frame["uia_nodes"] = list(frame["uia_nodes"]) + [
            {"id": 113, "name": "", "control_type": "GroupControl", "norm": [10, 160, 150, 200],
             "clickable": True, "patterns": ["Invoke"]},
        ]
        steps = plan_steps(frame, parse_steps(steps="uia:#113"))
        self.assertIsNone(steps[0]["uia_name"])
        self.assertEqual(steps[0]["uia_query"], "#113")
        self.assertEqual(
            uia_step_expectation(frame, steps[0]),
            {"expect_name": None, "expect_control_type": "GroupControl"},
        )

    def test_unnamed_node_resolves_by_type(self):
        doc = FakeCtrl("迅雷", "DocumentControl", (138, 290, 1218, 1010))
        group = FakeCtrl("", "GroupControl", (138, 395, 330, 445), ["Invoke"], parent=doc)
        text = FakeCtrl("上传", "TextControl", (150, 405, 320, 435), parent=group)
        _patch_hit(self, text)
        frame = _xunlei_frame()
        frame["uia_nodes"] = list(frame["uia_nodes"]) + [
            {"id": 113, "name": "", "control_type": "GroupControl", "norm": [10, 160, 150, 200],
             "clickable": True, "patterns": ["Invoke"]},
        ]
        out = run_chain(frame, parse_steps(steps="uia:#113"), dry_run=True, method="uia")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["steps"][0]["uia"]["ancestor_hops"], 1)

    def test_unnamed_node_type_mismatch_never_echoes_query(self):
        doc = FakeCtrl("迅雷", "DocumentControl", (138, 290, 1218, 1010))
        btn = FakeCtrl("", "ButtonControl", (138, 395, 330, 445), ["Invoke"], parent=doc)
        text = FakeCtrl("上传", "TextControl", (150, 405, 320, 435), parent=btn)
        _patch_hit(self, text)
        frame = _xunlei_frame()
        frame["uia_nodes"] = list(frame["uia_nodes"]) + [
            {"id": 113, "name": "", "control_type": "GroupControl", "norm": [10, 160, 150, 200],
             "clickable": True, "patterns": ["Invoke"]},
        ]
        out = run_chain(frame, parse_steps(steps="uia:#113"), dry_run=True, method="uia")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "uia_target_mismatch")
        self.assertNotIn("#113", out["error"])
        self.assertIn("unnamed", out["error"])
        self.assertIsNone(out["expected_uia"]["name"])

    def test_glyph_only_expect_name_is_unnamed(self):
        _doc, _btn, text = _chromium_button()
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(
            766, 842, expect_name="\ue65f ", expect_control_type="ButtonControl"
        )
        self.assertTrue(info["ok"], info.get("error"))


class TestAutoMismatchD2(unittest.TestCase):
    def setUp(self):
        stub_live_window(self, TOP)
        p = mock.patch("cu_perceive.extras.missing_modules", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def test_auto_dry_run_mismatch_refused(self):
        login = FakeCtrl("登录", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        _patch_hit(self, login)
        out = run_chain(_xunlei_frame(), parse_steps(uia_name="新建任务"), dry_run=True, method="auto")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "uia_target_mismatch")
        self.assertIsNone(out["steps"][0]["chosen_method"])
        self.assertEqual(out.get("planned_foreground_click_count", 0), 0)
        self.assertEqual(out["resolved_uia"]["name"], "登录")

    def test_auto_live_mismatch_sends_nothing(self):
        login = FakeCtrl("登录", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        _patch_hit(self, login)
        with mock.patch("cu_perceive.input.click_xy") as click, \
                mock.patch("cu_perceive.uia.invoke_control") as inv, \
                mock.patch("time.sleep"):
            out = run_chain(
                _xunlei_frame(), parse_steps(uia_name="新建任务"), dry_run=False,
                method="auto", new_window_wait_s=0,
            )
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "uia_target_mismatch")
        click.assert_not_called()
        inv.assert_not_called()
        self.assertEqual(out.get("foreground_click_count", 0), 0)

    def test_auto_no_pattern_still_falls_back_to_input(self):
        doc = FakeCtrl("迅雷", "DocumentControl", (138, 290, 1218, 1010))
        text = FakeCtrl("label", "TextControl", (700, 825, 830, 860), parent=doc)
        _patch_hit(self, text)
        frame = _xunlei_frame()
        frame["uia_nodes"] = list(frame["uia_nodes"]) + [
            {"id": 160, "name": "label", "control_type": "TextControl", "norm": [557, 747, 607, 787]},
        ]
        out = run_chain(frame, parse_steps(steps="uia:#160"), dry_run=True, method="auto")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["steps"][0]["chosen_method"], "input")
        self.assertIn("uia_skip_reason", out["steps"][0])
        self.assertNotIn("uia_reason", out["steps"][0])

    def test_auto_match_chooses_uia(self):
        _doc, _btn, text = _chromium_button()
        _patch_hit(self, text)
        out = run_chain(_xunlei_frame(), parse_steps(uia_name="新建任务"), dry_run=True, method="auto")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["steps"][0]["chosen_method"], "uia")


SIB = 777002


class TestOwnedPopupD3(unittest.TestCase):
    def setUp(self):
        self.at = HWND
        self.class_names = {}
        self.threads = {HWND: 10, POPUP: 10, SIB: 11}
        self.pids = {HWND: 4242, POPUP: 4242, SIB: 4242}
        patches = {
            "window_from_point": lambda x, y: self.at,
            "root_window": lambda h: h,
            "owner_window": lambda h: {POPUP: HWND}.get(h, 0),
            "class_name": lambda h: self.class_names.get(h, "Chrome_WidgetWin_0"),
            "window_thread": lambda h: self.threads.get(h, 99),
            "window_pid": lambda h: self.pids.get(h, 0),
            "window_rect": lambda h: (300, 200, 1100, 700),
            "window_title": lambda h: "t",
            "window_exstyle": lambda h: 0x100,
        }
        for name, fn in patches.items():
            p = mock.patch(f"cu_perceive._win32.{name}", side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def test_owned_popup_outside_rect_allowed(self):
        self.at = POPUP
        ok, info = win_mod.point_owner(HWND, 1200, 800)
        self.assertTrue(ok)
        self.assertIsNone(win_mod.point_off_target(HWND, 1200, 800, info["hwnd"], ok))

    def test_unrelated_same_pid_toplevel_outside_rect_refused(self):
        self.at = SIB
        ok, info = win_mod.point_owner(HWND, 1200, 800)
        self.assertFalse(ok)
        off = win_mod.point_off_target(HWND, 1200, 800, info["hwnd"], ok)
        self.assertEqual(off["why"], "outside_window")
        self.assertEqual(off["pid"], 4242)

    def test_same_pid_menu_on_target_thread_allowed(self):
        menu = 777003
        self.at = menu
        self.class_names[menu] = "#32768"
        self.threads[menu] = 10
        self.pids[menu] = 4242
        ok, info = win_mod.point_owner(HWND, 1200, 800)
        self.assertTrue(ok)
        self.assertIsNone(win_mod.point_off_target(HWND, 1200, 800, info["hwnd"], ok))

    def test_unrelated_same_pid_inside_rect_is_covered(self):
        self.at = SIB
        ok, info = win_mod.point_owner(HWND, 400, 300)
        self.assertFalse(ok)
        self.assertIsNone(win_mod.point_off_target(HWND, 400, 300, info["hwnd"], ok))


class TestCliPerceivePassthroughD4(unittest.TestCase):
    def test_cli_perceive_forwards_pick_and_fallback(self):
        import io
        import json
        from contextlib import redirect_stdout

        from cu_perceive.cli import PERCEIVE_PASSTHROUGH, main

        payload = {
            "stamp": "s", "items": [], "source": "window", "coord_space": "window", "hwnd": 1,
            "json": "s.json", "uia": True, "uia_nodes": [], "uia_count": 0, "ok": True,
            "pick_reason": "overlay_deprioritized",
            "deprioritized": [{"hwnd": 2, "title": "x", "overlay": ["layered"]}],
            "pick_warning": "overlay_candidate", "pick_overlay": ["layered"], "pick_hint": "h",
            "suggested_hwnd": None, "uia_fallback": "no_accessibility_tree", "uia_fallback_hint": "fh",
        }
        buf = io.StringIO()
        with mock.patch("cu_perceive.core.perceive", return_value=dict(payload)), redirect_stdout(buf):
            rc = main(["perceive", "--exe", "thunder", "--uia"])
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        for k in ("stamp", "source", "coord_space", "hwnd", "json", "uia", "uia_nodes",
                  "uia_count", "ok", *PERCEIVE_PASSTHROUGH):
            self.assertIn(k, out)
            self.assertEqual(out[k], payload[k], k)
        self.assertIsNone(out["suggested_hwnd"])

    def test_cli_perceive_omits_absent_keys(self):
        import io
        import json
        from contextlib import redirect_stdout

        from cu_perceive.cli import PERCEIVE_PASSTHROUGH, main

        payload = {
            "stamp": "s", "items": [], "source": "window", "coord_space": "window", "hwnd": 1,
            "json": "s.json", "uia": True, "uia_nodes": [], "uia_count": 0, "ok": True,
        }
        buf = io.StringIO()
        with mock.patch("cu_perceive.core.perceive", return_value=dict(payload)), redirect_stdout(buf):
            rc = main(["perceive", "--exe", "thunder", "--uia"])
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        for k in PERCEIVE_PASSTHROUGH:
            self.assertNotIn(k, out)


class TestOverlayRankP31(unittest.TestCase):
    def _find(self, rows, **kw):
        with mock.patch("cu_perceive.windows.list_windows", return_value=[dict(r) for r in rows]), \
                mock.patch("cu_perceive.windows.monitor_rects", return_value=MONITORS):
            ranked = win_mod.find_windows(**kw)
            return ranked, win_mod.pick_info(ranked)

    def test_overlay_with_exact_title_ranks_below_normal(self):
        overlay = _row(1, "迅雷", (2060, -160, 2868, 532), exstyle=0x00080008)
        main = _row(2, "迅雷 主窗口", (138, 290, 1218, 1010))
        ranked, meta = self._find([overlay, main], title="迅雷")
        self.assertEqual(ranked[0]["hwnd"], 2)
        self.assertEqual(meta["pick_reason"], "overlay_deprioritized")


class TestWalkSkipP35(unittest.TestCase):
    def test_walk_skips_mid_ancestor_not_containing_point(self):
        btn = FakeCtrl("新建任务", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        group = FakeCtrl("g", "GroupControl", (0, 0, 10, 10), parent=btn)
        text = FakeCtrl("x", "TextControl", (749, 836, 801, 851), parent=group)
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(766, 842)
        self.assertTrue(info["ok"], info.get("error"))
        self.assertEqual(info["ancestor_hops"], 2)
        self.assertEqual(info["control_type"], "ButtonControl")

    def test_walk_skips_invokable_mid_ancestor_not_containing_point(self):
        btn = FakeCtrl("新建任务", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        group = FakeCtrl("g", "GroupControl", (0, 0, 10, 10), ["Invoke"], parent=btn)
        text = FakeCtrl("x", "TextControl", (749, 836, 801, 851), parent=group)
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(766, 842)
        self.assertTrue(info["ok"], info.get("error"))
        self.assertEqual(info["ancestor_hops"], 2)
        self.assertEqual(info["control_type"], "ButtonControl")

    def test_walk_skip_respects_depth_limit(self):
        btn = FakeCtrl("ok", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        cur = btn
        for i in range(4):
            cur = FakeCtrl(f"g{i}", "GroupControl", (0, 0, 10, 10), parent=cur)
        hit = FakeCtrl("x", "TextControl", (749, 836, 801, 851), parent=cur)
        _patch_hit(self, hit)
        info = uia_mod.resolve_at_screen(766, 842)
        self.assertFalse(info["ok"])
        self.assertIn("within 4 levels", info["error"])
        ok = uia_mod.resolve_at_screen(766, 842, max_hops=5)
        self.assertTrue(ok["ok"], ok.get("error"))
        self.assertEqual(ok["ancestor_hops"], 5)


class TestUnreadableTopHwndP36(unittest.TestCase):
    def test_unreadable_top_hwnd_blocks_ancestor_walk(self):
        doc = FakeCtrl("迅雷", "DocumentControl", (138, 290, 1218, 1010), top=None)
        btn = FakeCtrl("\ue65f 新建任务", "ButtonControl", (700, 825, 830, 860), ["Invoke"], parent=doc, top=None)
        text = FakeCtrl("新建任务", "TextControl", (749, 836, 801, 851), parent=btn, top=None)
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(766, 842)
        self.assertFalse(info["ok"])
        self.assertIn("top-level window could not be read", info["error"])

    def test_unreadable_top_hwnd_direct_hit_unchanged(self):
        btn = FakeCtrl("新建任务", "ButtonControl", (700, 825, 830, 860), ["Invoke"], top=None)
        _patch_hit(self, btn)
        info = uia_mod.resolve_at_screen(766, 842)
        self.assertTrue(info["ok"], info.get("error"))
        self.assertEqual(info["ancestor_hops"], 0)

    def test_ancestor_with_unreadable_top_skipped(self):
        btn = FakeCtrl("新建任务", "ButtonControl", (700, 825, 830, 860), ["Invoke"], top=TOP)
        group = FakeCtrl("g", "GroupControl", (700, 825, 830, 860), ["Invoke"], parent=btn, top=None)
        text = FakeCtrl("新建任务", "TextControl", (749, 836, 801, 851), parent=group, top=TOP)
        _patch_hit(self, text)
        info = uia_mod.resolve_at_screen(766, 842)
        self.assertTrue(info["ok"], info.get("error"))
        self.assertEqual(info["ancestor_hops"], 2)
        self.assertEqual(info["control_type"], "ButtonControl")


class TestSplitNotOnTargetP37(unittest.TestCase):
    def test_split_not_on_target_by_position(self):
        from cu_perceive.act import _split_not_on_target

        off, rest = _split_not_on_target([{"a": 1}, {"a": 1, "not_on_target": True}])
        self.assertEqual((off, rest), ([{"a": 1}], [{"a": 1}]))
        self.assertEqual(_split_not_on_target(None), ([], None))


def _root_doc():
    return FakeCtrl("迅雷", "DocumentControl", (138, 290, 1218, 1010))


def _patch_hits(testcase, *hits):
    """element_from_screen_point returns hits in order (an exception instance is raised)."""
    for p in (
        mock.patch("cu_perceive.uia.element_from_screen_point", side_effect=list(hits)),
        mock.patch("cu_perceive.uia.list_available_patterns", side_effect=lambda c: list(c.patterns)),
    ):
        p.start()
        testcase.addCleanup(p.stop)


class TestTargetUnverifiedD5(unittest.TestCase):
    def setUp(self):
        stub_live_window(self, TOP)
        p = mock.patch("cu_perceive.extras.missing_modules", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def _run(self, method, dry_run, specs=None, frame=None):
        with mock.patch("cu_perceive.input.click_xy") as click, \
                mock.patch("cu_perceive.uia.invoke_control") as inv, mock.patch("time.sleep"):
            out = run_chain(frame or _xunlei_frame(), specs or parse_steps(uia_name="新建任务"),
                            dry_run=dry_run, method=method, new_window_wait_s=0)
        return out, click, inv

    def _assert_refused(self, out, click, inv, why):
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "uia_target_mismatch")
        self.assertEqual(out["uia_unverified"], why)
        self.assertEqual(out.get("planned_foreground_click_count", 0), 0)
        self.assertEqual(out.get("foreground_click_count", 0), 0)
        click.assert_not_called()
        inv.assert_not_called()

    def test_resolve_root_hit_named_is_mismatch(self):
        _patch_hit(self, _root_doc())
        info = uia_mod.resolve_at_screen(
            766, 842, expect_hwnd=TOP, expect_name="\ue65f 新建任务", expect_control_type="ButtonControl"
        )
        self.assertFalse(info["ok"])
        self.assertEqual(info["reason"], "uia_target_mismatch")
        self.assertEqual(info["uia_unverified"], "root_hit")
        self.assertIsNone(info["preferred_action"])
        self.assertEqual(info["expected"], {"name": "\ue65f 新建任务", "control_type": "ButtonControl"})
        self.assertIn("迅雷", info["error"])
        self.assertIn("新建任务", info["error"])

    def test_stale_root_hit_uia_dry(self):
        _patch_hit(self, _root_doc())
        out, click, inv = self._run("uia", True)
        self._assert_refused(out, click, inv, "root_hit")
        self.assertEqual(out["resolved_uia"], {"name": "迅雷", "control_type": "DocumentControl"})
        self.assertEqual(out["expected_uia"]["name"], "\ue65f 新建任务")

    def test_stale_root_hit_uia_go(self):
        _patch_hit(self, _root_doc())
        out, click, inv = self._run("uia", False)
        self._assert_refused(out, click, inv, "root_hit")
        self.assertEqual(out["resolved_uia"], {"name": "迅雷", "control_type": "DocumentControl"})
        self.assertEqual(out["expected_uia"]["name"], "\ue65f 新建任务")

    def test_stale_root_hit_auto_dry(self):
        _patch_hit(self, _root_doc())
        out, click, inv = self._run("auto", True)
        self._assert_refused(out, click, inv, "root_hit")
        self.assertEqual(out["resolved_uia"], {"name": "迅雷", "control_type": "DocumentControl"})
        self.assertEqual(out["expected_uia"]["name"], "\ue65f 新建任务")
        self.assertIsNone(out["steps"][0]["chosen_method"])
        self.assertFalse(out["steps"][0]["foreground_used"])
        self.assertTrue(out["steps"][0]["uia_reprobe"])
        self.assertEqual(out["steps"][0]["uia_probe_attempts"], 2)

    def test_stale_root_hit_auto_go(self):
        _patch_hit(self, _root_doc())
        out, click, inv = self._run("auto", False)
        self._assert_refused(out, click, inv, "root_hit")
        self.assertEqual(out["resolved_uia"], {"name": "迅雷", "control_type": "DocumentControl"})
        self.assertEqual(out["expected_uia"]["name"], "\ue65f 新建任务")
        self.assertIsNone(out["steps"][0]["chosen_method"])
        self.assertFalse(out["steps"][0]["foreground_used"])
        self.assertTrue(out["steps"][0]["uia_reprobe"])
        self.assertEqual(out["steps"][0]["uia_probe_attempts"], 2)

    def test_no_pattern_other_name_refused(self):
        hit = FakeCtrl("登录", "TextControl", (700, 825, 830, 860), parent=_root_doc())
        _patch_hit(self, hit)
        for method, dry in (("uia", True), ("auto", True), ("auto", False)):
            with self.subTest(method=method, dry=dry):
                out, click, inv = self._run(method, dry)
                self._assert_refused(out, click, inv, "no_pattern_name_mismatch")
                self.assertEqual(out["resolved_uia"]["name"], "登录")

    def test_no_pattern_own_name_match_still_allowed(self):
        frame = _xunlei_frame()
        hit = FakeCtrl("新建任务", "TextControl", (749, 836, 801, 851), parent=_root_doc())
        _patch_hit(self, hit)
        specs = parse_steps(steps="uia:#153")
        out, _click, _inv = self._run("auto", True, specs=specs, frame=frame)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["steps"][0]["chosen_method"], "input")
        self.assertEqual(out["steps"][0]["uia"]["name_matched"], "hit")
        self.assertEqual(out["steps"][0]["uia"]["reason"], "uia_no_pattern")
        self.assertNotIn("uia_reason", out["steps"][0])
        self.assertEqual(out["planned_foreground_click_count"], 1)
        out, _click, _inv = self._run("uia", True, specs=specs, frame=frame)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "uia_no_pattern")
        self.assertIn("hint", out)
        self.assertEqual(out.get("planned_foreground_click_count", 0), 0)

    def test_uia_error_with_expectation_refused(self):
        err = uia_mod.UiaError("ControlFromPoint(766,842) failed: boom")
        _patch_hits(self, *[err] * 3)
        for method, dry in (("uia", True), ("auto", True), ("auto", False)):
            with self.subTest(method=method, dry=dry):
                out, click, inv = self._run(method, dry)
                self._assert_refused(out, click, inv, "probe_failed")
                self.assertIn("boom", out["error"])
                if method == "auto":
                    self.assertEqual(out["steps"][0]["uia_probe_attempts"], 1)

    def test_plain_norm_auto_unchanged(self):
        _patch_hit(self, _root_doc())
        out, _click, _inv = self._run("auto", True, specs=parse_steps(norm="582,767"))
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["steps"][0]["chosen_method"], "input")
        self.assertNotIn("uia_reason", out["steps"][0])
        self.assertEqual(out["planned_foreground_click_count"], 1)
        self.assertEqual(out["steps"][0]["uia"]["reason"], "uia_no_pattern")
        self.assertNotIn("uia_unverified", out["steps"][0]["uia"])
        out, _click, _inv = self._run("uia", True, specs=parse_steps(norm="582,767"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "uia_no_pattern")

    def test_reprobe_root_then_login_refused(self):
        login = FakeCtrl("登录", "ButtonControl", (700, 825, 830, 860), ["Invoke"])
        _patch_hits(self, _root_doc(), login)
        out, _click, _inv = self._run("auto", True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "uia_target_mismatch")
        self.assertEqual(out["resolved_uia"]["name"], "登录")
        self.assertEqual(out["steps"][0]["uia_probe_attempts"], 2)
        self.assertEqual(out["planned_foreground_click_count"], 0)

    def test_reprobe_root_then_match_chooses_uia(self):
        _doc, _btn, text = _chromium_button()
        _patch_hits(self, _root_doc(), text)
        out, _click, _inv = self._run("auto", True)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["steps"][0]["chosen_method"], "uia")
        self.assertEqual(out["steps"][0]["uia_probe_attempts"], 2)

    def test_live_reresolve_root_refused(self):
        real = uia_mod.resolve_at_screen
        calls = []

        def fake(*a, **k):
            calls.append(k)
            if len(calls) == 1:
                return {"ok": True, "method": "uia", "screen": [766, 842], "name": "\ue65f 新建任务",
                        "control_type": "ButtonControl", "patterns": ["Invoke"], "preferred_action": "Invoke",
                        "pattern": "Invoke", "top_hwnd": TOP, "ancestor_hops": 0, "name_matched": "resolved"}
            return real(*a, **k)

        _patch_hit(self, _root_doc())
        with mock.patch("cu_perceive.uia.resolve_at_screen", side_effect=fake):
            for method in ("auto", "uia"):
                with self.subTest(method=method):
                    calls.clear()
                    out, click, inv = self._run(method, False)
                    self.assertEqual(len(calls), 2)
                    self._assert_refused(out, click, inv, "root_hit")
                    self.assertFalse(out["steps"][0]["invoked"])
                    self.assertNotIn("UIA invoke failed", out["error"])

    def test_live_reresolve_uia_error_refused(self):
        calls = []

        def fake(*a, **k):
            calls.append(k)
            if len(calls) == 1:
                return {"ok": True, "method": "uia", "screen": [766, 842], "name": "\ue65f 新建任务",
                        "control_type": "ButtonControl", "patterns": ["Invoke"], "preferred_action": "Invoke",
                        "pattern": "Invoke", "top_hwnd": TOP, "ancestor_hops": 0, "name_matched": "resolved"}
            raise uia_mod.UiaError("ControlFromPoint(766,842) failed: boom")

        _patch_hit(self, _root_doc())
        with mock.patch("cu_perceive.uia.resolve_at_screen", side_effect=fake):
            for method in ("auto", "uia"):
                with self.subTest(method=method):
                    calls.clear()
                    out, click, inv = self._run(method, False)
                    self._assert_refused(out, click, inv, "probe_failed")

    def test_hwnd_mismatch_named_refused(self):
        hit = FakeCtrl("\ue65f 新建任务", "ButtonControl", (700, 825, 830, 860), ["Invoke"], top=999)
        _patch_hit(self, hit)
        info = uia_mod.resolve_at_screen(
            766, 842, expect_hwnd=TOP, expect_name="新建任务", expect_control_type="ButtonControl"
        )
        self.assertEqual(info["uia_unverified"], "other_window")
        out, click, inv = self._run("auto", True)
        self._assert_refused(out, click, inv, "other_window")
        self.assertEqual(out["steps"][0]["uia_probe_attempts"], 1)

    def test_no_element_named_refused(self):
        _patch_hit(self, None)
        out, click, inv = self._run("auto", True)
        self._assert_refused(out, click, inv, "no_element")

    def test_act_guard_without_uia_reason(self):
        BASE = {
            "ok": False,
            "error": "no Invoke/Toggle/ExpandCollapse/Select pattern on element",
            "method": "uia",
            "screen": [766, 842],
            "name": "",
            "control_type": "GroupControl",
            "patterns": [],
            "preferred_action": None,
            "top_hwnd": TOP,
        }
        with mock.patch("cu_perceive.uia.probe_at_screen", side_effect=lambda *a, **k: dict(BASE)):
            out, click, inv = self._run("auto", True, specs=parse_steps(uia_name="新建任务"))
            self._assert_refused(out, click, inv, "unverified")
            out, _click, _inv = self._run("auto", True, specs=parse_steps(norm="582,767"))
            self.assertTrue(out["ok"], out.get("error"))
            self.assertEqual(out["steps"][0]["chosen_method"], "input")

    def test_resolve_expected_reraises_missing_extra(self):
        err = uia_mod.UiaError("missing")
        err.install = "pip ..."
        _patch_hits(self, err)
        with self.assertRaises(uia_mod.UiaError):
            uia_mod.resolve_expected(1, 1, expect_name="x")
        _patch_hits(self, uia_mod.UiaError("boom"))
        with self.assertRaises(uia_mod.UiaError):
            uia_mod.resolve_expected(1, 1, expect_name=None)

    def test_glyph_only_name_root_hit_keeps_no_pattern(self):
        _patch_hit(self, _root_doc())
        info = uia_mod.resolve_at_screen(766, 842, expect_name="\ue65f ", expect_control_type="ButtonControl")
        self.assertEqual(info["reason"], "uia_no_pattern")
        self.assertNotIn("uia_unverified", info)


if __name__ == "__main__":
    unittest.main()

