"""Shared test doubles that behave the same on Linux and on real Windows.

Tests that build frames with a made-up hwnd must not reach the live Win32 checks: on
Windows ``IsWindow(1)`` is False, so the identity check rejects the stamp, while on
Linux the same check fails for a different reason. Stub them explicitly instead.
"""
from __future__ import annotations

from unittest import mock


def stub_live_window(testcase, hwnd: int = 1) -> None:
    """Make ``hwnd`` look like a live, unchanged, uncovered, foreground window."""
    patches = [
        mock.patch(
            "cu_perceive.act.check_stamp",
            return_value={"ok": True, "identity_ok": True, "stamp": None, "hwnd": hwnd},
        ),
        mock.patch("cu_perceive.act.stamp_identity_error", return_value=None),
        mock.patch("cu_perceive.windows.point_owner", return_value=(True, {"hwnd": hwnd, "title": ""})),
        mock.patch("cu_perceive.windows.foreground_matches", return_value=(True, hwnd)),
    ]
    for p in patches:
        p.start()
        testcase.addCleanup(p.stop)
