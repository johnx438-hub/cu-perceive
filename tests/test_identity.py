"""HWND + PID + process creation time: stamp check and every live act step fail closed."""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

_win32gui = types.ModuleType("win32gui")
_win32gui.IsWindow = lambda hwnd: True
_win32gui.IsIconic = lambda hwnd: False
_win32gui.GetWindowPlacement = lambda hwnd: (0, 1, (0, 0), (0, 0), (0, 0, 0, 0))
# Scoped to this module so other test files keep seeing no win32gui on Linux.
_modules_patch = mock.patch.dict(sys.modules, {} if "win32gui" in sys.modules else {"win32gui": _win32gui})
_extras_patch = mock.patch("cu_perceive.extras.missing_modules", return_value=[])


def setUpModule():
    _modules_patch.start()
    _extras_patch.start()


def tearDownModule():
    _extras_patch.stop()
    _modules_patch.stop()


from cu_perceive.act import check_stamp, parse_steps, run_chain, stamp_identity_error  # noqa: E402
from cu_perceive.windows import verify_window_identity

CT = 133000000000000000


def _live(pid=4242, ctime=CT, is_window=True):
    """Patch the Win32 identity primitives to a given live state."""
    return [
        mock.patch("cu_perceive._win32.is_window", return_value=is_window),
        mock.patch("cu_perceive._win32.window_pid", return_value=pid),
        mock.patch("cu_perceive._win32.process_creation_time", return_value=ctime),
    ]


class _Live(unittest.TestCase):
    def live(self, **kw):
        for p in _live(**kw):
            p.start()
            self.addCleanup(p.stop)


class TestVerifyWindowIdentity(_Live):
    def test_same_window(self):
        self.live()
        self.assertIsNone(verify_window_identity(42, 4242, str(CT)))

    def test_hwnd_reused_by_other_process(self):
        self.live(pid=999)
        err = verify_window_identity(42, 4242, str(CT))
        self.assertIn("HWND reused", err)
        self.assertIn("999", err)

    def test_pid_reused_restarted_process(self):
        self.live(ctime=CT + 10_000_000)
        err = verify_window_identity(42, 4242, str(CT))
        self.assertIn("creation time changed", err)

    def test_window_gone(self):
        self.live(is_window=False)
        self.assertIn("no longer exists", verify_window_identity(42, 4242, str(CT)))

    def test_unreadable_creation_time_fails_closed(self):
        self.live(ctime=None)
        self.assertIn("fail closed", verify_window_identity(42, 4242, str(CT)))

    def test_missing_stamp_identity(self):
        self.live()
        self.assertIn("no process identity", verify_window_identity(42, None, None))
        self.assertIn("no process identity", verify_window_identity(42, 4242, ""))

    def test_garbage_identity(self):
        self.live()
        self.assertIn("bad stamp identity", verify_window_identity(42, "x", "y"))

    def test_win32_error_fails_closed(self):
        with mock.patch("cu_perceive._win32.is_window", side_effect=OSError("boom")):
            self.assertIn("identity check failed", verify_window_identity(42, 4242, str(CT)))


def _frame(**extra):
    base = {
        "stamp": "20260912-110000",
        "hwnd": 42,
        "coord_space": "window",
        "origin": {"left": 100, "top": 50, "width": 800, "height": 600},
        "width": 800,
        "height": 600,
        "items": [],
        "identity": {"hwnd": 42, "pid": 4242, "ctime": str(CT)},
    }
    base.update(extra)
    return base


class TestStampIdentityError(_Live):
    def test_screen_stamp_passes(self):
        self.assertIsNone(stamp_identity_error({"coord_space": "screen", "hwnd": None}))

    def test_old_stamp_without_identity_fails(self):
        f = _frame()
        del f["identity"]
        self.assertIn("no window identity", stamp_identity_error(f))

    def test_identity_hwnd_mismatch(self):
        f = _frame(identity={"hwnd": 43, "pid": 4242, "ctime": str(CT)})
        self.assertIn("!= stamp hwnd", stamp_identity_error(f))


class TestCheckStampIdentity(_Live):
    def setUp(self):
        for p in (
            mock.patch("win32gui.IsWindow", return_value=True),
            mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=False),
            mock.patch(
                "cu_perceive.windows.live_geom",
                return_value=({"left": 100, "top": 50, "width": 800, "height": 600}, {"hwnd": 42}),
            ),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_matching_identity_ok(self):
        self.live()
        out = check_stamp(_frame())
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["identity_ok"])

    def test_reused_hwnd_same_place_fails(self):
        # New window got the same HWND and sits at the same origin: geometry would pass.
        self.live(pid=5555, ctime=CT + 1)
        out = check_stamp(_frame())
        self.assertFalse(out["ok"])
        self.assertFalse(out["identity_ok"])
        self.assertIn("HWND reused", out["error"])

    def test_go_refuses_reused_hwnd(self):
        self.live(pid=5555)
        with mock.patch("cu_perceive.input.click_xy", create=True) as click:
            out = run_chain(_frame(), parse_steps(norm="500,500"), dry_run=False, method="input")
        self.assertFalse(out["ok"])
        self.assertIn("HWND reused", out["error"])
        click.assert_not_called()

    def test_dry_run_fails_on_identity_mismatch_without_strict(self):
        self.live(pid=5555)
        out = run_chain(_frame(), parse_steps(norm="500,500"), dry_run=True, method="input")
        self.assertFalse(out["ok"], out)
        self.assertFalse(out["stamp_ok"])
        self.assertIn("HWND reused", out["error"])
        self.assertEqual(out["stamp_error"], out["error"])
        self.assertFalse(out["stamp_check"]["identity_ok"])

    def test_dry_run_fails_when_window_gone(self):
        self.live()
        with mock.patch("win32gui.IsWindow", return_value=False):
            out = run_chain(_frame(), parse_steps(norm="500,500"), dry_run=True, method="input")
        self.assertFalse(out["ok"])
        self.assertIn("no longer exists", out["error"])
        self.assertFalse(out["stamp_check"]["identity_ok"])

    def test_dry_run_fails_on_missing_identity(self):
        frame = _frame()
        del frame["identity"]
        out = run_chain(frame, parse_steps(norm="500,500"), dry_run=True, method="input")
        self.assertFalse(out["ok"])
        self.assertIn("no window identity", out["error"])

    def test_moved_window_dry_run_still_ok_but_surfaced(self):
        self.live()
        moved = {"left": 900, "top": 900, "width": 640, "height": 480}
        with mock.patch("cu_perceive.windows.is_hwnd_minimized", return_value=False), \
                mock.patch("cu_perceive.windows.live_geom", return_value=(moved, {})):
            out = run_chain(_frame(), parse_steps(norm="500,500"), dry_run=True, method="input")
            self.assertTrue(out["ok"], out)
            self.assertFalse(out["stamp_ok"])
            self.assertIn("window moved", out["stamp_error"])
            strict = run_chain(_frame(), parse_steps(norm="500,500"), dry_run=True, method="input", strict_stamp=True)
        self.assertFalse(strict["ok"])


class TestLiveStepGuard(unittest.TestCase):
    """Identity flips mid-chain: later steps must not run."""

    def setUp(self):
        p = mock.patch("cu_perceive.act.check_stamp", return_value={"ok": True, "stamp": "s", "hwnd": 42})
        p.start()
        self.addCleanup(p.stop)

    def _ident(self, *results):
        p = mock.patch("cu_perceive.act.stamp_identity_error", side_effect=list(results))
        m = p.start()
        self.addCleanup(p.stop)
        return m

    @mock.patch("time.sleep")
    def test_input_chain_stops_before_step(self, _sleep):
        self._ident(None, "hwnd 42 now belongs to pid 9 (HWND reused by another process); re-perceive")
        import cu_perceive.input as inp

        with mock.patch.object(inp, "click_xy") as click, mock.patch.object(inp, "enter_text") as typ:
            out = run_chain(_frame(), parse_steps(norm="100,100", text="hi"), dry_run=False, method="input")
        self.assertFalse(out["ok"])
        self.assertEqual(out["identity_abort_step"], 1)
        self.assertIn("before step 1", out["error"])
        click.assert_called_once()
        typ.assert_not_called()
        self.assertTrue(out["steps"][0].get("clicked"))
        self.assertNotIn("typed", out["steps"][1])

    @mock.patch("time.sleep")
    def test_input_hold_key_released_on_abort(self, _sleep):
        self._ident("reused")
        import cu_perceive.input as inp

        with mock.patch.object(inp, "key_down") as down, mock.patch.object(inp, "key_up") as up, \
                mock.patch.object(inp, "click_xy") as click:
            out = run_chain(_frame(), parse_steps(norm="100,100"), dry_run=False, method="input", hold_key="ctrl")
        self.assertFalse(out["ok"])
        click.assert_not_called()
        down.assert_called_once_with("ctrl")
        up.assert_called_once_with("ctrl")

    def test_post_chain_guarded(self):
        self._ident("reused")
        import cu_perceive.input as inp

        with mock.patch.object(inp, "screen_to_client", return_value=(10, 10)), \
                mock.patch.object(inp, "post_click_xy") as post:
            out = run_chain(_frame(), parse_steps(norm="100,100"), dry_run=False, method="post")
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["identity_abort_step"], 0)
        post.assert_not_called()

    @mock.patch("cu_perceive.uia.resolve_at_screen")
    def test_uia_chain_guarded(self, resolve):
        resolve.return_value = {"ok": True, "_control": object(), "preferred_action": "Invoke",
                                "name": "OK", "control_type": "ButtonControl", "patterns": ["Invoke"]}
        self._ident("reused")
        with mock.patch("cu_perceive.uia.invoke_control") as invoke:
            out = run_chain(_frame(), parse_steps(norm="100,100"), dry_run=False, method="uia")
        self.assertFalse(out["ok"], out)
        invoke.assert_not_called()
        self.assertNotIn("_uia_control", out["steps"][0])


if __name__ == "__main__":
    unittest.main()
