"""Opt-in: CU_PERCEIVE_TEST_SIMULATE_WIN32=1 installs a fake win32gui in which no window
exists (IsWindow is always False), the way a made-up hwnd behaves on real Windows.

Off Windows, ``import win32gui`` fails and the stamp checks bail out early, which can
hide tests that rely on a fake hwnd. Run the suite once with this set to catch them.
"""
from __future__ import annotations

import os
import sys
import types

if os.environ.get("CU_PERCEIVE_TEST_SIMULATE_WIN32") == "1" and "win32gui" not in sys.modules:
    _fake = types.ModuleType("win32gui")
    _fake.IsWindow = lambda hwnd: False
    _fake.IsIconic = lambda hwnd: False
    _fake.GetWindowPlacement = lambda hwnd: (0, 1, (0, 0), (0, 0), (0, 0, 0, 0))
    sys.modules["win32gui"] = _fake
