"""Browser tools for cu-perceive.

Page tasks shell out to the external ``bsk`` CLI. Desktop apps stay on
``windows`` / ``perceive_window`` / ``act``. No BrowserSkill source is vendored.

``BrowserBackend`` is the seam a later in-process port can implement. The only
implementation today is ``SubprocessBsk``.
"""

from .tools import (
    browser_act,
    browser_observe,
    browser_session,
    browser_status,
    browser_tabs,
    register,
    reset_browser_state,
)

__all__ = [
    "browser_act",
    "browser_observe",
    "browser_session",
    "browser_status",
    "browser_tabs",
    "register",
    "reset_browser_state",
]
