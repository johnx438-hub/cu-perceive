"""S2 method=uia: resolve AutomationElement at a screen point, Invoke/Toggle/Expand/Select.

Non-invasive: no SetCursorPos / SendInput / PostMessage. Fail-closed if no pattern.
S3: choose_auto_method / probe_at_screen — uia iff under stamp + preferred pattern, else input.
Soft-miss (no preferred pattern on a real element): one same-point reprobe, then choose uia|input.
"""
from __future__ import annotations

import threading
import time
from typing import Any

# Preference order (S2 contract): Invoke -> Toggle -> ExpandCollapse Expand -> Select
PATTERN_PREFERENCE: tuple[tuple[str, str], ...] = (
    ("Invoke", "Invoke"),
    ("Toggle", "Toggle"),
    ("ExpandCollapse", "Expand"),
    ("Select", "Select"),
)

# PatternId attribute names on uiautomation.PatternId
_PATTERN_ID_ATTR = {
    "Invoke": "InvokePattern",
    "Toggle": "TogglePattern",
    "ExpandCollapse": "ExpandCollapsePattern",
    "Select": "SelectionItemPattern",
}


class UiaError(OSError):
    """Fail-closed UIA resolve / invoke errors (no post/input fallback)."""


def _auto():
    from . import extras

    try:
        extras.require("uia")
        import uiautomation as auto
    except ImportError as e:
        missing = e if isinstance(e, extras.MissingExtraError) else extras.missing_from_import_error("uia", e)
        err = UiaError(str(missing))
        err.install = missing.command
        raise err from e
    return auto


def pattern_id_names() -> list[str]:
    """Canonical pattern names we probe (for tests / dry-run lists)."""
    return [name for name, _ in PATTERN_PREFERENCE]


def pick_preferred_action(available: list[str] | set[str] | tuple[str, ...]) -> str | None:
    """Return first preferred pattern name present in available, else None."""
    avail = set(available)
    for name, _action in PATTERN_PREFERENCE:
        if name in avail:
            return name
    return None


def choose_auto_method(
    *,
    preferred_action: str | None,
    top_hwnd: int | None = None,
    expect_hwnd: int | None = None,
) -> str:
    """S3 auto chooser: return ``uia`` or ``input`` (never ``post``).

    Rules (v0), same top-level hwnd check as S2 resolve_at_screen:
    - uia iff preferred_action present AND not (expect_hwnd set AND top_hwnd set AND mismatch).
    - Otherwise input.
    - post is never chosen in auto v0 (Win11 Notepad proved post weak).
    """
    if not preferred_action:
        return "input"
    if (
        expect_hwnd is not None
        and top_hwnd is not None
        and int(top_hwnd) != int(expect_hwnd)
    ):
        return "input"
    return "uia"


def list_available_patterns(control) -> list[str]:
    """Which of our preferred patterns the control currently exposes."""
    auto = _auto()
    found: list[str] = []
    for name, _action in PATTERN_PREFERENCE:
        attr = _PATTERN_ID_ATTR[name]
        pid = getattr(auto.PatternId, attr)
        try:
            pat = control.GetPattern(pid)
        except Exception:
            pat = None
        if pat is not None:
            found.append(name)
    return found


def describe_control(control) -> dict[str, Any]:
    """Safe name / control_type / bbox / patterns for dry-run JSON."""
    name = ""
    ctype = ""
    bbox = None
    try:
        name = str(control.Name or "")
    except Exception:
        name = ""
    try:
        ctype = str(control.ControlTypeName or "")
    except Exception:
        ctype = ""
    try:
        r = control.BoundingRectangle
        if r is not None:
            bbox = {
                "left": int(r.left),
                "top": int(r.top),
                "right": int(r.right),
                "bottom": int(r.bottom),
                "width": int(r.width()),
                "height": int(r.height()),
                "coord_space": "screen",
            }
    except Exception:
        bbox = None
    patterns = list_available_patterns(control)
    preferred = pick_preferred_action(patterns)
    top_hwnd = None
    try:
        tl = control.GetTopLevelControl()
        if tl is not None:
            top_hwnd = int(tl.NativeWindowHandle or 0) or None
    except Exception:
        top_hwnd = None
    out: dict[str, Any] = {
        "name": name,
        "control_type": ctype,
        "patterns": patterns,
        "preferred_action": preferred,
    }
    if bbox is not None:
        out["bbox"] = bbox
        out["bbox_coord_space"] = "screen"
    if top_hwnd is not None:
        out["top_hwnd"] = top_hwnd
    return out


def element_from_screen_point(sx: int, sy: int):
    """Hit-test: AutomationElement / Control at screen pixel (x, y)."""
    auto = _auto()
    try:
        ctrl = auto.ControlFromPoint(int(sx), int(sy))
    except Exception as e:
        raise UiaError(f"ControlFromPoint({sx},{sy}) failed: {e}") from e
    return ctrl


def resolve_at_screen(
    sx: int,
    sy: int,
    *,
    expect_hwnd: int | None = None,
) -> dict[str, Any]:
    """Resolve element at screen point; return describe dict + ok flag.

    If expect_hwnd is set and element's top-level hwnd differs, ok=False.
    If no preferred pattern, ok=False (caller must not fall back).
    """
    ctrl = element_from_screen_point(sx, sy)
    if ctrl is None:
        return {
            "ok": False,
            "error": f"no AutomationElement at screen=({sx},{sy})",
            "screen": [int(sx), int(sy)],
            "method": "uia",
        }
    info = describe_control(ctrl)
    info["ok"] = True
    info["screen"] = [int(sx), int(sy)]
    info["method"] = "uia"
    info["_control"] = ctrl  # live handle; strip before JSON if needed
    if expect_hwnd:
        top = info.get("top_hwnd")
        if top is not None and int(top) != int(expect_hwnd):
            info["ok"] = False
            info["error"] = (
                f"UIA hit top_hwnd={top} != stamp hwnd={expect_hwnd} "
                f"(element may be covered / wrong window)"
            )
            return info
    if not info.get("preferred_action"):
        info["ok"] = False
        info["error"] = (
            "no Invoke/Toggle/ExpandCollapse/Select pattern on element "
            f"{info.get('control_type')!r} name={info.get('name')!r}"
        )
    return info


def probe_at_screen(
    sx: int,
    sy: int,
    *,
    expect_hwnd: int | None = None,
) -> dict[str, Any]:
    """Soft UIA hit-test for method=auto. Never raises; ok reflects uia-worthiness."""
    try:
        return resolve_at_screen(sx, sy, expect_hwnd=expect_hwnd)
    except UiaError as e:
        return {
            "ok": False,
            "error": str(e),
            "screen": [int(sx), int(sy)],
            "method": "uia",
            "preferred_action": None,
            "patterns": [],
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"UIA probe failed: {e}",
            "screen": [int(sx), int(sy)],
            "method": "uia",
            "preferred_action": None,
            "patterns": [],
        }



def is_soft_uia_miss(info: dict[str, Any]) -> bool:
    """True when hit-test found an element but no preferred Invoke/Toggle/Expand/Select.

    Typical: DocumentControl / GroupControl / parent container. Empty patterns with an
    element count the same. Not a soft miss: preferred_action present, hwnd mismatch,
    or ControlFromPoint / probe failure with no element.
    """
    if info.get("preferred_action"):
        return False
    err = str(info.get("error") or "")
    # Do not reprobe on hwnd mismatch (resolve returns early with this error).
    if "stamp hwnd" in err and "top_hwnd=" in err:
        return False
    if info.get("_control") is not None:
        return True
    if info.get("control_type"):
        return True
    # Element-ish describe payload with empty patterns, no preferred.
    if "patterns" in info and ("name" in info or "control_type" in info):
        return True
    return False


def probe_at_screen_with_reprobe(
    sx: int,
    sy: int,
    *,
    expect_hwnd: int | None = None,
    sleep_ms: int = 50,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Probe once; on soft miss, sleep briefly and re-probe the same screen point once.

    Returns (info, meta). meta keys:
    - reprobed: bool — True when a second probe ran
    - first_control_type / first_name / first_error — first-hit snapshot when reprobed
    """
    import time

    first = probe_at_screen(sx, sy, expect_hwnd=expect_hwnd)
    meta: dict[str, Any] = {"reprobed": False}
    if not is_soft_uia_miss(first):
        return first, meta
    meta["reprobed"] = True
    meta["first_control_type"] = first.get("control_type")
    meta["first_name"] = first.get("name")
    meta["first_error"] = first.get("error")
    if sleep_ms and int(sleep_ms) > 0:
        time.sleep(int(sleep_ms) / 1000.0)
    second = probe_at_screen(sx, sy, expect_hwnd=expect_hwnd)
    return second, meta


def invoke_control(control, *, action: str | None = None) -> dict[str, Any]:
    """Execute preferred (or named) pattern on control. No mouse injection."""
    auto = _auto()
    patterns = list_available_patterns(control)
    chosen = action or pick_preferred_action(patterns)
    if not chosen:
        raise UiaError(
            "no Invoke/Toggle/ExpandCollapse/Select pattern "
            f"(available={patterns})"
        )
    if chosen == "Invoke":
        pat = control.GetPattern(auto.PatternId.InvokePattern)
        if pat is None:
            raise UiaError("InvokePattern missing at invoke time")
        pat.Invoke()
        return {"ok": True, "action": "Invoke"}
    if chosen == "Toggle":
        pat = control.GetPattern(auto.PatternId.TogglePattern)
        if pat is None:
            raise UiaError("TogglePattern missing at invoke time")
        pat.Toggle()
        return {"ok": True, "action": "Toggle"}
    if chosen == "ExpandCollapse":
        pat = control.GetPattern(auto.PatternId.ExpandCollapsePattern)
        if pat is None:
            raise UiaError("ExpandCollapsePattern missing at invoke time")
        pat.Expand()
        return {"ok": True, "action": "Expand"}
    if chosen == "Select":
        pat = control.GetPattern(auto.PatternId.SelectionItemPattern)
        if pat is None:
            raise UiaError("SelectionItemPattern missing at invoke time")
        pat.Select()
        return {"ok": True, "action": "Select"}
    raise UiaError(f"unknown UIA action {chosen!r}")


def screen_bbox_to_window(
    bbox: dict[str, Any],
    *,
    origin_left: float,
    origin_top: float,
) -> dict[str, Any]:
    """Convert screen-pixel BoundingRectangle dict to client/window pixels.

    Width/height unchanged; left/top/right/bottom subtract origin.
    """
    ol = float(origin_left)
    ot = float(origin_top)
    left = int(bbox["left"]) - ol
    top = int(bbox["top"]) - ot
    right = int(bbox["right"]) - ol
    bottom = int(bbox["bottom"]) - ot
    out: dict[str, Any] = {
        "left": int(left),
        "top": int(top),
        "right": int(right),
        "bottom": int(bottom),
        "width": int(bbox.get("width", right - left)),
        "height": int(bbox.get("height", bottom - top)),
        "coord_space": "window",
    }
    return out


def public_element_info(
    info: dict[str, Any],
    frame: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drop non-JSON live handles from resolve result.

    When ``frame`` is window-coord with origin, also stamp ``bbox_window``
    (client) beside screen ``bbox`` (``coord_space=screen``).
    """
    out = {k: v for k, v in info.items() if not str(k).startswith("_")}
    bbox = out.get("bbox")
    if isinstance(bbox, dict) and "coord_space" not in bbox:
        bbox = {**bbox, "coord_space": "screen"}
        out["bbox"] = bbox
    if out.get("bbox") is not None and "bbox_coord_space" not in out:
        out["bbox_coord_space"] = "screen"
    frame = frame or {}
    origin = frame.get("origin") if isinstance(frame, dict) else None
    if (
        isinstance(bbox, dict)
        and isinstance(frame, dict)
        and frame.get("coord_space") == "window"
        and isinstance(origin, dict)
        and "left" in origin
        and "top" in origin
    ):
        out["bbox_window"] = screen_bbox_to_window(
            bbox,
            origin_left=origin["left"],
            origin_top=origin["top"],
        )
    return out


# --- S4: perceive --uia tree walk (compact nodes + norm bboxes) ---

UIA_MAX_NODES = 200
UIA_MAX_DEPTH = 16
# One real tree walk hung for over 3 minutes. Stop and return what we have.
UIA_WALK_TIMEOUT_S = 20.0


def uia_walk_timeout_s() -> float | None:
    """Seconds for one UIA tree walk. None waits with no limit (env 0 / off)."""
    from .settings import get as _cfg

    value = _cfg("uia.walk_timeout_s")
    return None if value is None else float(value)
UIA_VALUE_MAX = 120
# Name/value nodes that are not already preferred (Text, DataItem, ...).
# Kept so a text-only reader can see labels, message-box bodies, and grid cells,
# without letting them crowd out buttons.
UIA_CONTENT_MAX = 80

# Read-only grid cells, lookup rows, labels, message text. Edit is already preferred.
CONTENT_CONTROL_TYPES: frozenset[str] = frozenset(
    {
        "Text",
        "TextControl",
        "DataItem",
        "DataItemControl",
        "Document",
        "DocumentControl",
        "Edit",
        "EditControl",
        "ListItem",
        "ListItemControl",
    }
)

# Prefer these control types (with or without "Control" suffix).
PREFERRED_CONTROL_TYPES: frozenset[str] = frozenset(
    {
        "Button",
        "ButtonControl",
        "MenuItem",
        "MenuItemControl",
        "Edit",
        "EditControl",
        "TreeItem",
        "TreeItemControl",
        "TabItem",
        "TabItemControl",
        "ListItem",
        "ListItemControl",
        "CheckBox",
        "CheckBoxControl",
        "Hyperlink",
        "HyperlinkControl",
        "RadioButton",
        "RadioButtonControl",
        "ComboBox",
        "ComboBoxControl",
        "SplitButton",
        "SplitButtonControl",
        "MenuBar",
        "MenuBarControl",
        "Tab",
        "TabControl",
        "List",
        "ListControl",
        "Tree",
        "TreeControl",
    }
)


def screen_bbox_to_norm(
    left: float,
    top: float,
    right: float,
    bottom: float,
    *,
    origin_left: float,
    origin_top: float,
    width: int,
    height: int,
) -> list[int]:
    """Map screen-space bbox to 0-1000 norm relative to window client / perceive map.

    Same convention as grid map / act --norm: nx = (x - origin.left) / width * 1000.
    """
    w = max(1, int(width))
    h = max(1, int(height))
    nx1 = int(round((float(left) - float(origin_left)) / w * 1000))
    ny1 = int(round((float(top) - float(origin_top)) / h * 1000))
    nx2 = int(round((float(right) - float(origin_left)) / w * 1000))
    ny2 = int(round((float(bottom) - float(origin_top)) / h * 1000))
    # Clamp lightly so tiny overhangs still usable; keep order.
    nx1 = max(0, min(1000, nx1))
    ny1 = max(0, min(1000, ny1))
    nx2 = max(0, min(1000, nx2))
    ny2 = max(0, min(1000, ny2))
    if nx2 < nx1:
        nx1, nx2 = nx2, nx1
    if ny2 < ny1:
        ny1, ny2 = ny2, ny1
    return [nx1, ny1, nx2, ny2]


def _ctype_preferred(ctype: str) -> bool:
    return bool(ctype) and ctype in PREFERRED_CONTROL_TYPES


def _ctype_base(ctype: str) -> str:
    c = (ctype or "").strip()
    if c.endswith("Control"):
        return c[: -len("Control")]
    return c


def _ctype_content(ctype: str) -> bool:
    if not ctype:
        return False
    if ctype in CONTENT_CONTROL_TYPES:
        return True
    return _ctype_base(ctype) in {"Text", "DataItem", "Document", "Edit", "ListItem"}


def truncate_uia_value(value, limit: int | None = None) -> str | None:
    """ValuePattern text, capped so one cell cannot dominate the payload."""
    if limit is None:
        from .settings import get as _cfg

        limit = int(_cfg("uia.value_max"))
    if value is None:
        return None
    s = str(value).replace("\r\n", "\n").replace("\r", "\n")
    cap = max(1, int(limit))
    if len(s) <= cap:
        return s
    if cap <= 3:
        return s[:cap]
    return s[: cap - 3] + "..."


def content_only_uia_node(control_type: str, patterns) -> bool:
    """True for a name/value node that is not already kept as an action or preferred type."""
    if pick_preferred_action(patterns) is not None or _ctype_preferred(control_type):
        return False
    return _ctype_content(control_type)


def should_keep_uia_node(
    *,
    name: str,
    control_type: str,
    patterns: list[str] | tuple[str, ...] | set[str],
    norm: list[int] | None,
    offscreen: bool = False,
    skip_empty_name: bool = False,
    value: str | None = None,
    content_kept: int = 0,
    content_cap: int | None = UIA_CONTENT_MAX,
) -> bool:
    """Filter for compact perceive --uia list (caps applied here for content-only nodes)."""
    if offscreen:
        return False
    if not norm or len(norm) != 4:
        return False
    nx1, ny1, nx2, ny2 = (int(norm[0]), int(norm[1]), int(norm[2]), int(norm[3]))
    # Skip zero-area / fully outside after clamp.
    if nx2 <= nx1 or ny2 <= ny1:
        return False
    # Entirely outside map (after clamp all on edge with zero span already handled).
    if nx2 <= 0 or ny2 <= 0 or nx1 >= 1000 or ny1 >= 1000:
        return False
    name_s = (name or "").strip()
    value_s = (value or "").strip() if value is not None else ""
    if skip_empty_name and not name_s and not value_s:
        return False
    has_pat = pick_preferred_action(patterns) is not None
    preferred = _ctype_preferred(control_type)
    if has_pat or preferred:
        # Still drop nameless panes even if somehow typed preferred? preferred types ok empty.
        if not name_s and not has_pat and not preferred:
            return False
        return True
    # Text / DataItem / readonly-looking content: keep when there is a name or a value.
    if _ctype_content(control_type) and (name_s or value_s):
        if content_cap is not None and int(content_kept) >= int(content_cap):
            return False
        return True
    return False


def _runtime_id(control) -> tuple | None:
    """Runtime id, including an empty tuple. None when it cannot be read.

    An empty tuple is a real result on some machines (hwnd-less nodes). It must
    not be treated as "unknown", or every such node looks occluded.
    """
    try:
        rid = control.GetRuntimeId()
    except Exception:
        return None
    if rid is None:
        return None
    try:
        return tuple(rid)
    except TypeError:
        return None


def _controls_are_same(a, b) -> bool | None:
    """``uiautomation.ControlsAreSame``, or None when it cannot be used.

    Test fakes have no ``Element``. Real controls do, and ControlsAreSame is
    what distinguishes them when GetRuntimeId() is ``()``.
    """
    if a is None or b is None:
        return None
    if not hasattr(a, "Element") or not hasattr(b, "Element"):
        return None
    try:
        import uiautomation as auto

        fn = getattr(auto, "ControlsAreSame", None)
        if fn is None:
            return None
        return bool(fn(a, b))
    except Exception:
        return None


def _same_uia(a, b) -> bool:
    if a is b:
        return True
    same = _controls_are_same(a, b)
    if same is not None:
        return same
    ida, idb = _runtime_id(a), _runtime_id(b)
    if ida is None or idb is None:
        return False
    # () == () matches ControlsAreSame. Dropping that equality hid every
    # hwnd-less toolbar, menu, status bar, grid cell and lookup row.
    return ida == idb


def _uia_is_descendant(ancestor, node, *, limit: int = 32) -> bool:
    """True when ``node`` is under ``ancestor`` (a hit on a child is not occlusion)."""
    cur = node
    for _ in range(limit):
        try:
            parent = cur.GetParentControl()
        except Exception:
            return False
        if parent is None:
            return False
        if _same_uia(parent, ancestor):
            return True
        cur = parent
    return False


def control_center_occluded(control, bbox, *, hit_at) -> bool:
    """True when ElementFromPoint at the bbox center is some other control.

    Used to drop UIA nodes that belong to an MDI child covered by another child.
    ``hit_at(x, y)`` returns the topmost control or None. A failed hit-test is
    not occlusion (the node stays). A hit on this control or its descendant stays.
    """
    if not bbox or len(bbox) < 4 or hit_at is None:
        return False
    left, top, right, bottom = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
    if right <= left or bottom <= top:
        return False
    cx = (left + right) // 2
    cy = (top + bottom) // 2
    try:
        hit = hit_at(cx, cy)
    except Exception:
        return False
    if hit is None:
        return False
    if _same_uia(hit, control) or _uia_is_descendant(control, hit):
        return False
    return True


UIA_OCCLUDED_NAMES_MAX = 30


class Win32Probe:
    """HWND facts for the occlusion test; injectable so tests run without Win32."""

    def valid(self, hwnd: int) -> bool:
        from . import _win32

        return bool(_win32.is_window(int(hwnd)))

    def window_at(self, x: int, y: int) -> int:
        from . import _win32

        return int(_win32.window_from_point(int(x), int(y)) or 0)

    def root(self, hwnd: int) -> int:
        from . import _win32

        return int(_win32.root_window(int(hwnd)) or 0)

    def pid(self, hwnd: int) -> int:
        from . import _win32

        return int(_win32.window_pid(int(hwnd)) or 0)

    def related(self, a: int, b: int) -> bool:
        """a == b, or one is a descendant of the other."""
        from . import _win32

        return int(a) == int(b) or _win32.is_child(int(a), int(b)) or _win32.is_child(int(b), int(a))

    def owner_chain(self, hwnd: int) -> list[int]:
        from . import _win32

        out, cur = [], int(hwnd)
        for _ in range(8):
            cur = int(_win32.owner_window(cur) or 0)
            if not cur:
                break
            out.append(cur)
        return out


def hwnd_occlusion(target_hwnd: int, ctrl_hwnd: int | None, bbox, *, probe) -> tuple[bool | None, str]:
    """Window-level occlusion verdict at the bbox center: (True|False|None, why).

    Only *another* top-level window (other process, not owned by the target) occludes.
    Inside the target's own root, the hit HWND occludes only when it is unrelated to the
    control's own nearest HWND (sibling MDI child); the control's own HWND, its ancestors
    and descendants never do. That last rule fixes CEF/Chromium, whose windowless DOM
    elements all hit-test to Chrome_RenderWidgetHostHWND. None = undecided (no HWND info),
    the caller falls back to the UIA ControlFromPoint test.
    """
    if not bbox or len(bbox) < 4:
        return False, "no_bbox"
    left, top, right, bottom = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
    if right <= left or bottom <= top:
        return False, "empty_bbox"
    cx, cy = (left + right) // 2, (top + bottom) // 2
    try:
        valid = getattr(probe, "valid", None)
        if valid is not None and not valid(int(target_hwnd)):
            return None, "no_target"
        hit = probe.window_at(cx, cy)
        if not hit:
            return None, "no_hit"
        t_root = probe.root(target_hwnd) or int(target_hwnd)
        h_root = probe.root(hit) or int(hit)
        if h_root != t_root:
            if probe.pid(h_root) == probe.pid(t_root):
                return False, "own_process_window"
            if t_root in probe.owner_chain(h_root):
                return False, "owned_window"
            return True, "other_window"
        if hit == t_root or not ctrl_hwnd:
            return False, "self"
        if probe.related(int(ctrl_hwnd), int(hit)):
            return False, "self"
        return True, "sibling_child"
    except Exception:
        return None, "probe_error"


def _native_hwnd(control) -> int:
    try:
        return int(getattr(control, "NativeWindowHandle", 0) or 0)
    except Exception:
        return 0


# UIA_ValueIsReadOnlyPropertyId. DataGridView cells often expose this even when
# the ValuePattern wrapper's IsReadOnly attribute is missing.
_VALUE_IS_READONLY = 30046
_STATE_SYSTEM_READONLY = 0x40


def _coerce_bool(flag) -> bool | None:
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, int):
        return bool(flag)
    if isinstance(flag, str):
        s = flag.strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
    return None


def _read_flag(obj, names: tuple[str, ...]) -> bool | None:
    if obj is None:
        return None
    for name in names:
        try:
            flag = getattr(obj, name)
        except Exception:
            continue
        if callable(flag):
            try:
                flag = flag()
            except Exception:
                continue
        got = _coerce_bool(flag)
        if got is not None:
            return got
    return None


def _control_type_name(control) -> str:
    try:
        return str(getattr(control, "ControlTypeName", "") or "")
    except Exception:
        return ""


def provider_readonly_unreliable(control_type: str) -> bool:
    """DataGridView cells report IsReadOnly false even when the cell cannot be edited."""
    c = (control_type or "").lower()
    return "dataitem" in c or "datagrid" in c


def normalize_cell_value(value: str | None, control_type: str = "") -> str | None:
    """A grid cell whose value is the literal ``(null)`` is an empty cell."""
    if value is None:
        return None
    if provider_readonly_unreliable(control_type) and str(value).strip().lower() == "(null)":
        return None
    return value


def _control_is_password(control) -> bool:
    """UIA IsPassword or an edit style with ES_PASSWORD. Never reads Value."""
    try:
        from .input import _edit_style, _uia_is_password, edit_is_password

        return edit_is_password(
            uia_password=_uia_is_password(control),
            style=_edit_style(_native_hwnd(control)),
        )
    except Exception:
        return False


def read_control_value(control) -> tuple[str | None, bool | None]:
    """(ValuePattern.Value truncated, IsReadOnly or None). Never raises.

    Password edits are not read: UIA ``IsPassword`` or ``ES_PASSWORD`` returns
    ``(None, None)`` and does not touch ``ValuePattern.Value``. Read-only grid
    cells often have no ``IsReadOnly`` attribute on the pattern object. This
    also checks the ValueIsReadOnly property and the legacy accessible state
    bit. A DataItem/DataGrid ``False`` is not evidence, so those cells report
    ``None``. A cell value of ``(null)`` is ``None``.
    """
    if _control_is_password(control):
        return None, None
    auto = None
    pat = None
    try:
        auto = _auto()
        pat = control.GetPattern(auto.PatternId.ValuePattern)
    except Exception:
        pat = None
    value = None
    if pat is not None:
        try:
            raw = pat.Value
            if raw is not None:
                value = truncate_uia_value(raw)
        except Exception:
            value = None
    readonly = _read_flag(pat, ("IsReadOnly", "CurrentIsReadOnly"))
    if readonly is None and auto is not None:
        try:
            pid = getattr(getattr(auto, "PropertyId", None), "ValueIsReadOnlyProperty", _VALUE_IS_READONLY)
            readonly = _coerce_bool(control.GetPropertyValue(pid))
        except Exception:
            readonly = None
    if readonly is None and auto is not None:
        try:
            leg_id = getattr(auto.PatternId, "LegacyIAccessiblePattern", None)
            leg = control.GetPattern(leg_id) if leg_id is not None else None
            raw_state = getattr(leg, "State", None) if leg is not None else None
            if callable(raw_state):
                raw_state = raw_state()
            if isinstance(raw_state, int) and not isinstance(raw_state, bool):
                readonly = bool(raw_state & _STATE_SYSTEM_READONLY)
        except Exception:
            pass
    ctype = _control_type_name(control)
    value = normalize_cell_value(value, ctype)
    if provider_readonly_unreliable(ctype) and readonly is not True:
        readonly = None
    return value, readonly


def _control_offscreen(control) -> bool:
    try:
        auto = _auto()
        return bool(control.GetPropertyValue(auto.PropertyId.IsOffscreenProperty))
    except Exception:
        try:
            return bool(getattr(control, "IsOffscreen", False))
        except Exception:
            return False


def _control_bbox_screen(control) -> tuple[int, int, int, int] | None:
    try:
        r = control.BoundingRectangle
        if r is None:
            return None
        left, top, right, bottom = int(r.left), int(r.top), int(r.right), int(r.bottom)
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom
    except Exception:
        return None


def collect_uia_nodes(
    hwnd: int,
    *,
    width: int,
    height: int,
    origin_left: float,
    origin_top: float,
    max_nodes: int | None = None,
    max_depth: int | None = None,
    skip_empty_name: bool = False,
    timeout_s: float | None = None,
    include_occluded: bool = False,
    probe=None,
) -> dict[str, Any]:
    """Walk UIA tree under stamp hwnd; return compact nodes with norm bboxes.

    Occluded nodes (covered by another top-level window, or a sibling MDI child) are
    dropped and named in ``uia_occluded_names``; ``include_occluded`` keeps them with
    ``occluded: true``.

    Fail-closed: returns ``ok=False`` + ``error`` on missing package / bad hwnd.
    Does not raise for soft per-node errors.
    """
    from .settings import get as _cfg

    if max_nodes is None:
        max_nodes = int(_cfg("uia.max_nodes"))
    if max_depth is None:
        max_depth = int(_cfg("uia.max_depth"))
    if not hwnd:
        return {
            "ok": False,
            "error": "perceive --uia requires a pinned window hwnd",
            "uia_nodes": [],
            "caps": {"max_nodes": int(max_nodes), "max_depth": int(max_depth)},
        }
    try:
        auto = _auto()
    except UiaError as e:
        return {
            "ok": False,
            "error": str(e),
            "uia_nodes": [],
            "caps": {"max_nodes": int(max_nodes), "max_depth": int(max_depth)},
        }
    try:
        root = auto.ControlFromHandle(int(hwnd))
    except Exception as e:
        return {
            "ok": False,
            "error": f"ControlFromHandle({hwnd}) failed: {e}",
            "uia_nodes": [],
            "caps": {"max_nodes": int(max_nodes), "max_depth": int(max_depth)},
        }
    if root is None:
        return {
            "ok": False,
            "error": f"no AutomationElement for hwnd={hwnd}",
            "uia_nodes": [],
            "caps": {"max_nodes": int(max_nodes), "max_depth": int(max_depth)},
        }

    nodes: list[dict[str, Any]] = []
    visited = 0
    truncated = False
    occluded_n = 0
    occluded_names: list[str] = []
    occluded_why: dict[str, int] = {}
    probe = probe if probe is not None else Win32Probe()
    content_kept = 0
    timed_out = False
    if timeout_s is None:
        timeout_s = uia_walk_timeout_s()
    deadline = None if timeout_s is None else time.monotonic() + float(timeout_s)
    state_lock = threading.Lock()

    def hit_at(x: int, y: int):
        try:
            return auto.ControlFromPoint(int(x), int(y))
        except Exception:
            return None

    def walk(ctrl, depth: int, near_hwnd: int = 0) -> None:
        nonlocal truncated, visited, occluded_n, content_kept, timed_out
        if truncated or timed_out:
            return
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            return
        if depth > int(max_depth):
            return
        visited += 1
        try:
            name = ""
            ctype = ""
            try:
                name = str(ctrl.Name or "")
            except Exception:
                name = ""
            try:
                ctype = str(ctrl.ControlTypeName or "")
            except Exception:
                ctype = ""
            off = _control_offscreen(ctrl)
            bbox = None if off else _control_bbox_screen(ctrl)
            norm = None
            if bbox is not None:
                left, top, right, bottom = bbox
                norm = screen_bbox_to_norm(
                    left,
                    top,
                    right,
                    bottom,
                    origin_left=origin_left,
                    origin_top=origin_top,
                    width=width,
                    height=height,
                )
            patterns: list[str] = []
            try:
                patterns = list_available_patterns(ctrl)
            except Exception:
                patterns = []
            value, readonly = read_control_value(ctrl)
            content_only = content_only_uia_node(ctype, patterns)
            if should_keep_uia_node(
                name=name,
                control_type=ctype,
                patterns=patterns,
                norm=norm,
                offscreen=off,
                skip_empty_name=skip_empty_name,
                value=value,
                content_kept=content_kept if content_only else 0,
                content_cap=int(_cfg("uia.content_max")),
            ):
                # MDI children stacked under the active one still walk as on-screen.
                # Drop a node whose center is covered by a different element.
                is_occ = False
                if bbox is not None:
                    verdict, why = hwnd_occlusion(int(hwnd), _native_hwnd(ctrl) or near_hwnd or None, bbox, probe=probe)
                    if verdict is None:
                        verdict = control_center_occluded(ctrl, bbox, hit_at=hit_at)
                        why = "uia_hit"
                    is_occ = bool(verdict)
                if is_occ:
                    occluded_n += 1
                    occluded_why[why] = occluded_why.get(why, 0) + 1
                    if len(occluded_names) < int(_cfg("uia.occluded_names_max")):
                        occluded_names.append(name or f"[{ctype}]")
                if not is_occ or include_occluded:
                    preferred = pick_preferred_action(patterns)
                    node: dict[str, Any] = {
                        "id": len(nodes) + 1,
                        "name": name,
                        "control_type": ctype,
                        "norm": list(norm) if norm else None,
                        "clickable": preferred is not None,
                    }
                    if patterns:
                        node["patterns"] = patterns
                    if preferred:
                        node["preferred_action"] = preferred
                    if value is not None:
                        node["value"] = value
                    if is_occ:
                        node["occluded"] = True
                    if readonly is True or readonly is False:
                        if readonly or value is not None or content_only or "edit" in ctype.lower():
                            node["readonly"] = readonly
                    elif provider_readonly_unreliable(ctype):
                        # Explicit null: the provider's "not readonly" is not evidence.
                        node["readonly"] = None
                    with state_lock:
                        nodes.append(node)
                        kept_n = len(nodes)
                    if content_only:
                        content_kept += 1
                    if kept_n >= int(max_nodes):
                        truncated = True
                        return
        except Exception:
            pass
        try:
            children = ctrl.GetChildren()
        except Exception:
            children = []
        for ch in children or []:
            if truncated:
                break
            walk(ch, depth + 1, _native_hwnd(ctrl) or near_hwnd)

    walk_error: list[BaseException] = []

    def _run_walk() -> None:
        try:
            walk(root, 0)
        except Exception as e:
            walk_error.append(e)

    if timeout_s is None:
        _run_walk()
    else:
        worker = threading.Thread(target=_run_walk, name="cu-perceive-uia-walk", daemon=True)
        worker.start()
        worker.join(float(timeout_s))
        if worker.is_alive():
            timed_out = True
    if walk_error and not timed_out:
        return {
            "ok": False,
            "error": f"UIA tree walk failed: {walk_error[0]}",
            "uia_nodes": [],
            "caps": {"max_nodes": int(max_nodes), "max_depth": int(max_depth)},
        }

    with state_lock:
        snapshot = list(nodes)
    warnings: list[str] = []
    if timed_out:
        warnings.append(
            f"UIA tree walk timed out after {float(timeout_s or 0):.0f}s; "
            f"returning {len(snapshot)} partial nodes"
        )
    result = {
        "ok": True,
        "uia_nodes": snapshot,
        "uia_count": len(snapshot),
        "uia_visited": visited,
        "uia_truncated": truncated or timed_out,
        "uia_occluded": occluded_n,
        "uia_occluded_names": occluded_names,
        "uia_occluded_why": occluded_why,
        "uia_include_occluded": bool(include_occluded),
        "caps": {
            "max_nodes": int(max_nodes),
            "max_depth": int(max_depth),
            "skip_empty_name": bool(skip_empty_name),
            "value_max": int(_cfg("uia.value_max")),
            "content_max": int(_cfg("uia.content_max")),
        },
    }
    if timed_out:
        result["uia_timeout"] = True
        result["warnings"] = warnings
    return result


# --- E4: resolve UIA name/id -> map norm (still click via norm + method) ---


def uia_node_to_norm_center(node: dict[str, Any]) -> list[int]:
    """Center of node.norm bbox as [nx, ny] ints 0-1000."""
    norm = (node or {}).get("norm")
    if not norm or len(norm) != 4:
        raise ValueError(f"uia node missing norm bbox: {norm!r}")
    nx = int(round((int(norm[0]) + int(norm[2])) / 2))
    ny = int(round((int(norm[1]) + int(norm[3])) / 2))
    return [max(0, min(1000, nx)), max(0, min(1000, ny))]


def _ctype_matches(node_ctype: str, want: str | None) -> bool:
    if not want:
        return True
    a = (node_ctype or "").strip().lower()
    b = (want or "").strip().lower()
    if not b:
        return True
    if a == b:
        return True
    # MenuItem <-> MenuItemControl
    if a.endswith("control"):
        a_base = a[: -len("control")]
    else:
        a_base = a
    if b.endswith("control"):
        b_base = b[: -len("control")]
    else:
        b_base = b
    return a_base == b_base


def find_uia_nodes(
    frame: dict,
    *,
    name: str | None = None,
    name_substr: str | None = None,
    control_type: str | None = None,
    clickable_only: bool = True,
) -> list:
    """Match against frame["uia_nodes"]. Case-insensitive; prefer exact name then substr."""
    nodes = list((frame or {}).get("uia_nodes") or [])
    out: list = []
    for n in nodes:
        if clickable_only and not n.get("clickable"):
            continue
        if not _ctype_matches(str(n.get("control_type") or ""), control_type):
            continue
        out.append(n)

    if name is None and name_substr is None:
        return out

    query = name if name is not None else name_substr
    if query is None:
        return []
    q = str(query).strip().lower()
    if not q:
        return []

    exact: list = []
    substr: list = []
    for n in out:
        nm = str(n.get("name") or "").strip().lower()
        if nm == q:
            exact.append(n)
        elif q in nm:
            substr.append(n)

    # Prefer exact when name= provided; name_substr keeps exact+substr (exact first).
    if name is not None:
        if exact:
            return exact
        return substr
    return exact + substr


def _candidates_summary(nodes: list, *, limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for n in nodes[:limit]:
        rows.append(
            {
                "id": n.get("id"),
                "name": n.get("name"),
                "control_type": n.get("control_type"),
                "norm": n.get("norm"),
                "clickable": n.get("clickable"),
            }
        )
    return rows


def resolve_uia_target(frame: dict, query: str) -> dict[str, Any]:
    """Resolve a single UIA target to map norm center.

    query forms: "File", "File@MenuItemControl", "uia:12", or "#12".
    Returns {ok, norm, node, error?, candidates?}. Fail-closed on ambiguous/missing.
    """
    frame = frame or {}
    if "uia_nodes" not in frame:
        return {
            "ok": False,
            "norm": None,
            "node": None,
            "error": "perceive --uia first (no uia_nodes on stamp; fall back to grid/OCR)",
        }
    nodes = list(frame.get("uia_nodes") or [])
    if not nodes:
        return {
            "ok": False,
            "norm": None,
            "node": None,
            "error": "perceive --uia first (uia_nodes empty; weak a11y — use grid/OCR)",
        }

    raw = (query or "").strip()
    if not raw:
        return {
            "ok": False,
            "norm": None,
            "node": None,
            "error": "empty UIA name/query",
        }

    q = raw
    if q.lower().startswith("uia:"):
        q = q[4:].strip()

    node_id = None
    if q.startswith("#"):
        try:
            node_id = int(q[1:])
        except ValueError:
            return {
                "ok": False,
                "norm": None,
                "node": None,
                "error": f"bad uia node id: {raw!r}",
            }
    elif q.isdigit():
        node_id = int(q)

    if node_id is not None:
        hit = None
        for n in nodes:
            try:
                if int(n.get("id")) == int(node_id):
                    hit = n
                    break
            except (TypeError, ValueError):
                continue
        if hit is None:
            return {
                "ok": False,
                "norm": None,
                "node": None,
                "error": f"uia node id {node_id} not found (perceive --uia first / check id)",
            }
        try:
            center = uia_node_to_norm_center(hit)
        except ValueError as e:
            return {"ok": False, "norm": None, "node": hit, "error": str(e)}
        return {"ok": True, "norm": center, "node": hit}

    ctype = None
    name_q = q
    if "@" in q:
        name_q, ctype = q.rsplit("@", 1)
        name_q = name_q.strip()
        ctype = ctype.strip() or None

    matches = find_uia_nodes(
        frame,
        name=name_q,
        control_type=ctype,
        clickable_only=True,
    )
    if not matches:
        matches = find_uia_nodes(
            frame,
            name=name_q,
            control_type=ctype,
            clickable_only=False,
        )

    if not matches:
        return {
            "ok": False,
            "norm": None,
            "node": None,
            "error": f"no UIA node matching {raw!r} (perceive --uia first or use grid/OCR)",
        }
    if len(matches) > 1:
        return {
            "ok": False,
            "norm": None,
            "node": None,
            "error": (
                f"ambiguous UIA match for {raw!r}: {len(matches)} candidates "
                f"(fail-closed; disambiguate with @ControlType or uia:#id)"
            ),
            "candidates": _candidates_summary(matches),
        }

    hit = matches[0]
    try:
        center = uia_node_to_norm_center(hit)
    except ValueError as e:
        return {"ok": False, "norm": None, "node": hit, "error": str(e)}
    return {"ok": True, "norm": center, "node": hit}
