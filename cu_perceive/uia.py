"""S2 method=uia: resolve AutomationElement at a screen point, Invoke/Toggle/Expand/Select.

Non-invasive: no SetCursorPos / SendInput / PostMessage. Fail-closed if no pattern.
S3: choose_auto_method / probe_at_screen — uia iff under stamp + preferred pattern, else input.
Soft-miss (no preferred pattern on a real element): one same-point reprobe, then choose uia|input.
"""
from __future__ import annotations

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
    try:
        import uiautomation as auto
    except ImportError as e:
        raise UiaError(f"uiautomation not installed: {e}") from e
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


def should_keep_uia_node(
    *,
    name: str,
    control_type: str,
    patterns: list[str] | tuple[str, ...] | set[str],
    norm: list[int] | None,
    offscreen: bool = False,
    skip_empty_name: bool = False,
) -> bool:
    """Filter for compact perceive --uia list (caps applied by caller)."""
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
    if skip_empty_name and not name_s:
        return False
    has_pat = pick_preferred_action(patterns) is not None
    if has_pat or _ctype_preferred(control_type):
        # Still drop nameless panes even if somehow typed preferred? preferred types ok empty.
        if not name_s and not has_pat and not _ctype_preferred(control_type):
            return False
        return True
    # Non-preferred type without action pattern: skip (keeps payload bounded).
    return False


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
    max_nodes: int = UIA_MAX_NODES,
    max_depth: int = UIA_MAX_DEPTH,
    skip_empty_name: bool = False,
) -> dict[str, Any]:
    """Walk UIA tree under stamp hwnd; return compact nodes with norm bboxes.

    Fail-closed: returns ``ok=False`` + ``error`` on missing package / bad hwnd.
    Does not raise for soft per-node errors.
    """
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

    def walk(ctrl, depth: int) -> None:
        nonlocal truncated, visited
        if truncated:
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
            if should_keep_uia_node(
                name=name,
                control_type=ctype,
                patterns=patterns,
                norm=norm,
                offscreen=off,
                skip_empty_name=skip_empty_name,
            ):
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
                nodes.append(node)
                if len(nodes) >= int(max_nodes):
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
            walk(ch, depth + 1)

    try:
        walk(root, 0)
    except Exception as e:
        return {
            "ok": False,
            "error": f"UIA tree walk failed: {e}",
            "uia_nodes": [],
            "caps": {"max_nodes": int(max_nodes), "max_depth": int(max_depth)},
        }

    return {
        "ok": True,
        "uia_nodes": nodes,
        "uia_count": len(nodes),
        "uia_visited": visited,
        "uia_truncated": truncated,
        "caps": {
            "max_nodes": int(max_nodes),
            "max_depth": int(max_depth),
            "skip_empty_name": bool(skip_empty_name),
        },
    }


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
