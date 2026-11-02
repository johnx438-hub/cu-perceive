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
        info = uia_mod.resolve_at_screen(766, 842, expect_name="新建任务", expect_control_type="TextControl")
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


if __name__ == "__main__":
    unittest.main()
