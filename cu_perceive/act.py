"""Plan then run a chain: click / type / key / drag / scroll."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import out_dir as _config_out_dir

DEFAULT_OUT = _config_out_dir()
REPLACE_SELECT_KEY = "ctrl+a"


def load_frame(stamp: str | None = None, json_path: str | None = None) -> tuple[dict, Path | None]:
    if json_path:
        path = Path(json_path)
    elif stamp:
        path = DEFAULT_OUT / f"{stamp}.json"
    else:
        cands = list(DEFAULT_OUT.glob("*.json"))
        if not cands:
            return {}, None
        path = max(cands, key=lambda p: p.stat().st_mtime)
    if not path.is_file():
        raise FileNotFoundError(f"stamp json not found: {path}")
    return json.loads(path.read_text(encoding="utf-8")), path


def window_to_screen(frame: dict, x: int, y: int) -> tuple[int, int]:
    origin = (frame or {}).get("origin")
    if (frame or {}).get("coord_space") == "window" and origin:
        from .windows import is_sentinel_origin
        if is_sentinel_origin(origin) or origin.get("bad"):
            raise RuntimeError(
                "stamp origin is minimized sentinel (-32000); perceive that hwnd again"
            )
        return int(origin["left"] + x), int(origin["top"] + y)
    return int(x), int(y)


def norm_to_window(frame: dict, nx: int, ny: int) -> tuple[int, int]:
    w = int((frame or {}).get("width") or 1)
    h = int((frame or {}).get("height") or 1)
    return int(nx / 1000 * w), int(ny / 1000 * h)


def norm_to_roi_window(nx: int, ny: int, roi: list | tuple) -> tuple[int, int]:
    """Map 0-1000 norm relative to ROI into window/client pixels (ROI origin + local).

    roi = [L, T, W, H] in the same client/image pixel space as stamp width/height.
    """
    if roi is None or len(roi) != 4:
        raise ValueError(f"roi must be [L,T,W,H], got {roi!r}")
    L, T, W, H = (int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3]))
    if W <= 0 or H <= 0:
        raise ValueError(f"roi empty/non-positive: {roi!r}")
    return int(L + nx / 1000 * W), int(T + ny / 1000 * H)


def screen_xy(frame: dict, item: dict) -> tuple[int, int]:
    origin = (frame or {}).get("origin")
    from .windows import is_sentinel_origin
    if (frame or {}).get("coord_space") == "window" and is_sentinel_origin(origin):
        raise RuntimeError(
            "stamp origin is minimized sentinel (-32000); perceive that hwnd again"
        )
    if item.get("screen"):
        sx, sy = int(item["screen"][0]), int(item["screen"][1])
        if sx <= -10000 or sy <= -10000:
            raise RuntimeError(
                "stamp item.screen is off-screen sentinel; perceive that hwnd again"
            )
        return sx, sy
    cx, cy = item["center"]
    return window_to_screen(frame, cx, cy)


def item_by_id(frame: dict, id_: int) -> dict:
    for it in (frame or {}).get("items") or []:
        if int(it["id"]) == int(id_):
            return it
    raise KeyError(f"id {id_} not in frame {(frame or {}).get('stamp')}")


def pairs(s: str) -> list[list[int]]:
    """Parse points. Accepts x,y or x:y; multi with ;."""
    s = s.strip().replace(" ", ";")
    out = []
    for chunk in s.split(";"):
        if not chunk:
            continue
        if "," in chunk:
            parts = chunk.split(",")
        elif ":" in chunk:
            parts = chunk.split(":")
        else:
            raise ValueError(
                f'bad point {chunk!r}: expected "x,y" (MCP/act) or "x:y"; '
                f'multi points use ";" e.g. "177,752;500,200"'
            )
        if len(parts) != 2:
            raise ValueError(
                f'bad point {chunk!r}: expected two ints "x,y" (got {len(parts)} parts)'
            )
        out.append([int(parts[0]), int(parts[1])])
    if not out:
        raise ValueError('expected at least one "x,y" point')
    return out


def _point_pairs(s: str) -> list[list[int]]:
    nums = [int(x) for x in s.replace(";", ",").replace(" ", ",").split(",") if x]
    if len(nums) < 4 or len(nums) % 2:
        raise ValueError("drag needs at least two x,y pairs")
    return [[nums[i], nums[i + 1]] for i in range(0, len(nums), 2)]



class PathActError(Exception):
    """Fail-closed path.json / path-id errors for experimental act --path."""


def load_path_json(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise PathActError(f"path file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except PathActError:
        raise
    except Exception as e:
        raise PathActError(f"bad path file (json): {p}: {e}") from e
    if not isinstance(data, dict):
        raise PathActError("path file root must be an object")
    if int(data.get("version") or 0) != 1:
        raise PathActError(
            f"unsupported path.json version: {data.get('version')!r} (want 1)"
        )
    paths = data.get("paths")
    if not isinstance(paths, list):
        raise PathActError("path file missing paths[]")
    return data


def select_path_row(data: dict, path_id: int = 0) -> dict:
    for row in data.get("paths") or []:
        if int(row.get("id", -1)) == int(path_id):
            return row
    have = [r.get("id") for r in (data.get("paths") or [])]
    raise PathActError(f"path id {path_id} not in path file (have {have})")


def path_to_drag_specs(
    data: dict,
    *,
    path_id: int = 0,
    close: bool = False,
    step_px: int = 2,
) -> list[dict]:
    """Turn one path.json polyline into a single kind:drag (space=norm) spec."""
    row = select_path_row(data, path_id)
    raw = row.get("points") or []
    if not isinstance(raw, list) or len(raw) < 2:
        raise PathActError(f"path id {path_id} needs at least 2 points")
    pts: list[list[int]] = []
    for p in raw:
        if not (isinstance(p, (list, tuple)) and len(p) >= 2):
            raise PathActError(f"bad point in path id {path_id}: {p!r}")
        pts.append([int(p[0]), int(p[1])])
    do_close = bool(row.get("closed")) or bool(close)
    if do_close and pts and pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    spec = {
        "kind": "drag",
        "points": pts,
        "space": "norm",
        "step_px": int(step_px),
    }
    # E2: path.json source.roi shifts norm origin (canvas sub-rect of client).
    src_roi = (data.get("source") or {}).get("roi")
    if src_roi is not None:
        if not (isinstance(src_roi, (list, tuple)) and len(src_roi) == 4):
            raise PathActError(f"source.roi must be [L,T,W,H], got {src_roi!r}")
        try:
            roi = [int(src_roi[0]), int(src_roi[1]), int(src_roi[2]), int(src_roi[3])]
        except (TypeError, ValueError) as e:
            raise PathActError(f"source.roi must be ints [L,T,W,H]: {src_roi!r}") from e
        if roi[2] <= 0 or roi[3] <= 0:
            raise PathActError(f"source.roi empty/non-positive: {roi}")
        spec["roi"] = roi
    return [spec]


def subsample_points(points: list, max_n: int = 24) -> list:
    """Evenly subsample polyline for dry-run preview (keeps ends)."""
    if max_n < 2 or len(points) <= max_n:
        return list(points)
    last = len(points) - 1
    idxs = [int(round(i * last / (max_n - 1))) for i in range(max_n)]
    # de-dupe consecutive index picks
    out = []
    prev = None
    for i in idxs:
        if i != prev:
            out.append(points[i])
            prev = i
    return out


def parse_scroll(raw: str) -> int:
    """Wheel notches. +up / -down. Also accepts up:3, down:3, up, down."""
    s = str(raw).strip().lower().replace(" ", "")
    if s.startswith("scroll:"):
        s = s[7:]
    for word, sign in (("down", -1), ("up", 1)):
        if s == word:
            return sign
        if s.startswith(word):
            rest = s[len(word):].lstrip(":=")
            if not rest:
                return sign
            return sign * abs(int(rest))
    try:
        return int(s)
    except ValueError as e:
        raise ValueError("scroll wants 3, -3, down:3, or up:3 (negative/down = wheel down)") from e


def coerce_ids(ids) -> str | None:
    """Accept str, int, or list. Return a comma-string or None.

    5 -> "5"; [5, 9] -> "5,9"; "14,9,16" is unchanged. Empty/None -> None.
    """
    if ids is None:
        return None
    if isinstance(ids, bool):
        return None
    if isinstance(ids, int):
        return str(ids)
    if isinstance(ids, (list, tuple)):
        parts: list[str] = []
        for x in ids:
            s = coerce_ids(x)
            if s:
                parts.append(s)
        return ",".join(parts) if parts else None
    s = str(ids).strip()
    return s or None


def parse_steps(
    steps: str | None = None,
    ids: str | int | list | None = None,
    xy: str | None = None,
    norm: str | None = None,
    text: str | None = None,
    key: str | None = None,
    drag: str | None = None,
    drag_norm: str | None = None,
    scroll: str | None = None,
    button: str | None = None,
    hover: bool = False,
    wait: str | None = None,
    step_px: int | None = None,
    hold: float = 0,
    extras: list | None = None,
    uia_name: str | list | None = None,
    replace: bool = False,
) -> list[dict]:
    """Flat act fields -> ordered specs. replace=True puts a ctrl+a select-all before
    every type step so the paste replaces the field's text instead of inserting at the caret."""
    out: list[dict] = []
    if steps:
        for raw in steps.replace(" ", "").split(","):
            if not raw:
                continue
            if raw.startswith("id:"):
                out.append({"kind": "id", "id": int(raw[3:])})
            elif raw.startswith("xy:"):
                a, b = raw[3:].split(":")
                out.append({"kind": "xy", "xy": [int(a), int(b)]})
            elif raw.startswith("norm:"):
                a, b = raw[5:].split(":")
                out.append({"kind": "norm", "norm": [int(a), int(b)]})
            elif raw.startswith("key:"):
                out.append({"kind": "key", "key": raw[4:]})
            elif raw.startswith("scroll:"):
                out.append({"kind": "scroll", "notches": parse_scroll(raw[7:])})
            elif raw.startswith("uia:"):
                # E4: uia:File / uia:File@MenuItemControl / uia:#12
                out.append({"kind": "uia_name", "query": raw[4:]})
            else:
                out.append({"kind": "id", "id": int(raw)})
    else:
        ids = coerce_ids(ids)
        if ids:
            for x in ids.replace(" ", "").split(","):
                if x:
                    out.append({"kind": "id", "id": int(x)})
        if xy:
            for p in pairs(xy):
                out.append({"kind": "xy", "xy": p})
        if norm:
            for p in pairs(norm):
                out.append({"kind": "norm", "norm": p})
    extra = {}
    if step_px is not None:
        extra["step_px"] = int(step_px)
    if drag:
        out.append({"kind": "drag", "points": _point_pairs(drag), "space": "xy", **extra})
    if drag_norm:
        out.append({"kind": "drag", "points": _point_pairs(drag_norm), "space": "norm", **extra})
    if scroll not in (None, ""):
        out.append({"kind": "scroll", "notches": parse_scroll(scroll)})
    if extras:
        for e in extras:
            if e.get("kind") == "key":
                for k in str(e.get("key") or "").replace(" ", ",").split(","):
                    if k:
                        out.append({"kind": "key", "key": k})
            elif e.get("kind") == "type":
                out.append({"kind": "type", "text": e.get("text") or "", "input_via": e.get("input_via") or "paste"})
    else:
        if text:
            out.append({"kind": "type", "text": text, "input_via": "keys" if False else "paste"})
        if key:
            for k in str(key).replace(" ", ",").split(","):
                if k:
                    out.append({"kind": "key", "key": k})
    if uia_name not in (None, ""):
        names: list[str] = []
        if isinstance(uia_name, (list, tuple)):
            for x in uia_name:
                if x is None or x == "":
                    continue
                names.extend(str(x).split(","))
        else:
            names.extend(str(uia_name).split(","))
        for n in names:
            n = n.strip()
            if n:
                out.append({"kind": "uia_name", "query": n})
    if replace:
        with_select: list[dict] = []
        for s in out:
            if s.get("kind") == "type":
                with_select.append({"kind": "key", "key": REPLACE_SELECT_KEY, "replace": True})
                s["replace"] = True
            with_select.append(s)
        out = with_select
    if float(hold or 0) > 0:
        for s in out:
            if s.get("kind") in ("id", "xy", "norm", "uia_name"):
                s["hold"] = float(hold)
    has_scroll = any(s.get("kind") == "scroll" for s in out)
    has_pos = any(s.get("kind") in ("id", "xy", "norm", "uia_name") for s in out)
    # scroll + xy/norm/ids => hover (move, do not click) unless an explicit
    # click was asked for (hold>0 long-press). hover=True still wins.
    click_intent = float(hold or 0) > 0
    if hover or (has_scroll and has_pos and not click_intent):
        for s in out:
            if s.get("kind") in ("id", "xy", "norm", "uia_name"):
                s["hover"] = True
    if button:
        for s in out:
            if s.get("kind") in ("id", "xy", "norm", "uia_name"):
                s["button"] = button
    if wait not in (None, ""):
        out.append({"kind": "wait", "sec": float(wait)})
    return out


def plan_steps(frame: dict, specs: list[dict]) -> list[dict]:
    steps = []
    last_screen = None
    for spec in specs:
        kind = spec.get("kind")
        if kind == "id":
            it = item_by_id(frame, spec["id"])
            sx, sy = screen_xy(frame, it)
            last_screen = [sx, sy]
            steps.append({
                "via": "hover" if spec.get("hover") else "id",
                "id": int(it["id"]),
                "text": it.get("text") or "",
                "center": it.get("center"),
                "screen": [sx, sy],
                "button": spec.get("button") or "left",
                "hover": bool(spec.get("hover")),
                "hold": float(spec.get("hold") or 0),
            })
        elif kind == "xy":
            x, y = spec["xy"]
            sx, sy = window_to_screen(frame, x, y)
            last_screen = [sx, sy]
            steps.append({"via": "hover" if spec.get("hover") else "xy", "center": [x, y], "screen": [sx, sy], "button": spec.get("button") or "left", "hold": float(spec.get("hold") or 0)})
        elif kind == "norm":
            nx, ny = spec["norm"]
            x, y = norm_to_window(frame, nx, ny)
            sx, sy = window_to_screen(frame, x, y)
            last_screen = [sx, sy]
            steps.append({"via": "hover" if spec.get("hover") else "norm", "norm": [nx, ny], "center": [x, y], "screen": [sx, sy], "button": spec.get("button") or "left", "hold": float(spec.get("hold") or 0)})
        elif kind == "drag":
            space = spec.get("space") or "xy"
            raw = spec.get("points")
            if not raw:
                raw = [spec["from"], spec["to"]]
            win_pts = []
            screen_pts = []
            roi = spec.get("roi")
            for p in raw:
                if space == "norm":
                    if roi is not None:
                        x, y = norm_to_roi_window(int(p[0]), int(p[1]), roi)
                    else:
                        x, y = norm_to_window(frame, int(p[0]), int(p[1]))
                else:
                    x, y = int(p[0]), int(p[1])
                win_pts.append([x, y])
                sx, sy = window_to_screen(frame, x, y)
                screen_pts.append([sx, sy])
            last_screen = list(screen_pts[-1])
            from .input import _densify
            samples = len(_densify([tuple(p) for p in screen_pts], int(spec.get("step_px") or 4)))
            drag_step = {
                "via": "drag",
                "samples": samples,
                "space": space,
                "points": win_pts,
                "screen_points": screen_pts,
                "from": win_pts[0],
                "to": win_pts[-1],
                "screen_from": screen_pts[0],
                "screen_to": screen_pts[-1],
                "step_px": int(spec.get("step_px") or 4),
            }
            if roi is not None:
                drag_step["roi"] = [int(roi[0]), int(roi[1]), int(roi[2]), int(roi[3])]
            steps.append(drag_step)
        elif kind == "scroll":
            steps.append({"via": "scroll", "notches": int(spec["notches"]), "screen": last_screen})
        elif kind == "type":
            step = {"via": "type", "text": spec["text"], "input_via": spec.get("input_via") or "paste"}
            if spec.get("replace"):
                step["replace"] = True
            steps.append(step)
        elif kind == "key":
            step = {"via": "key", "key": spec["key"]}
            if spec.get("replace"):
                step["replace"] = True
            steps.append(step)
        elif kind == "wait":
            steps.append({"via": "wait", "sec": float(spec["sec"])})
        elif kind == "uia_name":
            # E4: name/id -> norm center; still a coordinate click (via=norm).
            from .uia import resolve_uia_target

            q = spec.get("query") or spec.get("uia_name") or ""
            resolved = resolve_uia_target(frame, str(q))
            if not resolved.get("ok"):
                raise ValueError(resolved.get("error") or f"UIA resolve failed for {q!r}")
            nx, ny = resolved["norm"]
            x, y = norm_to_window(frame, nx, ny)
            sx, sy = window_to_screen(frame, x, y)
            last_screen = [sx, sy]
            node = resolved.get("node") or {}
            step = {
                "via": "norm",
                "norm": [int(nx), int(ny)],
                "center": [x, y],
                "screen": [sx, sy],
                "uia_name": node.get("name") if node.get("name") not in (None, "") else str(q),
                "uia_node_id": node.get("id"),
                "button": spec.get("button") or "left",
                "hover": bool(spec.get("hover")),
                "hold": float(spec.get("hold") or 0),
            }
            if spec.get("hover"):
                step["via"] = "hover"
            steps.append(step)
        else:
            raise ValueError(spec)
    return steps



METHODS = frozenset({"input", "post", "uia", "auto"})


class MethodError(ValueError):
    """Fail-closed method / post/uia/auto-scope errors (S1/S2/S3)."""


def normalize_method(method: str | None) -> str:
    """Accept input|post|uia|auto. Default input."""
    if method in (None, ""):
        return "input"
    m = str(method).strip().lower()
    if m not in METHODS:
        raise MethodError(f"method must be input|post|uia|auto (got {method!r})")
    return m


def _noninvasive_scope_error(
    specs: list[dict],
    *,
    hold_key: str | None,
    method: str,
    hover_flag: bool = False,
) -> str | None:
    """post/uia/auto: single-point clicks only (norm/xy/ids). Fail-closed otherwise."""
    kinds = [s.get("kind") for s in specs]
    bad = []
    for k in kinds:
        if k in ("drag", "scroll", "type", "key", "wait"):
            bad.append(k)
        elif k not in ("id", "xy", "norm", "uia_name"):
            bad.append(k or "?")
    if any(s.get("hover") for s in specs) or hover_flag:
        bad.append("hover")
    if any(float(s.get("hold") or 0) > 0 for s in specs):
        bad.append("hold")
    if hold_key:
        bad.append("hold_key")
    if method in ("uia", "auto"):
        for s in specs:
            btn = (s.get("button") or "left").lower()
            if btn != "left":
                bad.append(f"button={btn}")
                break
    seen = set()
    uniq = []
    for b in bad:
        if b not in seen:
            seen.add(b)
            uniq.append(b)
    if uniq:
        label = {"post": "S1", "uia": "S2", "auto": "S3"}.get(method, "?")
        extra = ""
        if method in ("uia", "auto"):
            extra = " left-only;"
        return (
            f"method={method} is {label} single-point clicks only (norm/xy/ids);{extra} "
            f"incompatible with {', '.join(uniq)} — use method=input"
        )
    if not any(k in ("id", "xy", "norm", "uia_name") for k in kinds):
        return f"method={method} needs at least one click target (norm/xy/ids/uia_name)"
    return None


def _post_scope_error(specs: list[dict], *, hold_key: str | None, hover_flag: bool = False) -> str | None:
    """S1 post scope (compat wrapper)."""
    return _noninvasive_scope_error(
        specs, hold_key=hold_key, method="post", hover_flag=hover_flag
    )


def _uia_scope_error(specs: list[dict], *, hold_key: str | None, hover_flag: bool = False) -> str | None:
    """S2 uia scope: left single-point only."""
    return _noninvasive_scope_error(
        specs, hold_key=hold_key, method="uia", hover_flag=hover_flag
    )


def _auto_scope_error(specs: list[dict], *, hold_key: str | None, hover_flag: bool = False) -> str | None:
    """S3 auto scope: same as uia — left single-point only; else use method=input."""
    return _noninvasive_scope_error(
        specs, hold_key=hold_key, method="auto", hover_flag=hover_flag
    )


def enrich_post_steps(frame: dict, steps: list[dict], *, double: bool = False) -> list[dict]:
    """Attach client coords + planned PostMessage seq for dry-run / live post."""
    from .input import post_click_messages, screen_to_client

    hwnd = (frame or {}).get("hwnd")
    if not hwnd:
        raise MethodError("method=post requires stamp hwnd (perceive a window first)")
    hwnd = int(hwnd)
    for step in steps:
        via = step.get("via")
        if via not in ("id", "xy", "norm"):
            continue
        sx, sy = int(step["screen"][0]), int(step["screen"][1])
        # Prefer ScreenToClient from screen (open-cu recipe). Fall back to
        # stamp center when coord_space=window (already client pixels).
        try:
            cx, cy = screen_to_client(hwnd, sx, sy)
            client_via = "ScreenToClient"
        except OSError:
            center = step.get("center")
            if center and (frame or {}).get("coord_space") == "window":
                cx, cy = int(center[0]), int(center[1])
                client_via = "stamp_center"
            else:
                raise
        btn = step.get("button") or "left"
        step["client"] = [cx, cy]
        step["client_via"] = client_via
        step["post_messages"] = post_click_messages(cx, cy, button=btn, double=double)
        step["method"] = "post"
    return steps


def enrich_uia_steps(frame: dict, steps: list[dict], *, double: bool = False) -> list[dict]:
    """Resolve UIA element at each planned screen point (dry-run / live). No invoke."""
    from .uia import public_element_info, resolve_at_screen

    if double:
        raise MethodError(
            "method=uia does not support --double in S2 (Invoke once only); omit --double"
        )
    hwnd = (frame or {}).get("hwnd")
    expect = int(hwnd) if hwnd else None
    for step in steps:
        via = step.get("via")
        if via not in ("id", "xy", "norm"):
            continue
        btn = (step.get("button") or "left").lower()
        if btn != "left":
            raise MethodError(
                f"method=uia is left-click only in S2 (got button={btn!r})"
            )
        sx, sy = int(step["screen"][0]), int(step["screen"][1])
        info = resolve_at_screen(sx, sy, expect_hwnd=expect)
        step["method"] = "uia"
        step["uia"] = public_element_info(info, frame)
        # keep live control for --go path
        if info.get("_control") is not None:
            step["_uia_control"] = info["_control"]
            step["_uia_action"] = info.get("preferred_action")
        if not info.get("ok"):
            step["uia_ok"] = False
            step["uia_error"] = info.get("error") or "UIA resolve failed"
        else:
            step["uia_ok"] = True
    return steps


def enrich_auto_steps(frame: dict, steps: list[dict], *, double: bool = False) -> list[dict]:
    """S3: probe UIA at each planned point; set chosen_method=uia|input (never post).

    Soft miss (element, no preferred pattern): one same-point reprobe, then choose.
    Never silent: dry-run exposes uia_reprobe / uia_first / uia_probe_attempts.
    """
    from .uia import (
        choose_auto_method,
        probe_at_screen_with_reprobe,
        public_element_info,
    )

    if double:
        raise MethodError(
            "method=auto does not support --double in S3; omit --double or use method=input"
        )
    hwnd = (frame or {}).get("hwnd")
    expect = int(hwnd) if hwnd else None
    for step in steps:
        via = step.get("via")
        if via not in ("id", "xy", "norm"):
            continue
        btn = (step.get("button") or "left").lower()
        if btn != "left":
            raise MethodError(
                f"method=auto is left-click only in S3 (got button={btn!r})"
            )
        sx, sy = int(step["screen"][0]), int(step["screen"][1])
        info, meta = probe_at_screen_with_reprobe(sx, sy, expect_hwnd=expect)
        chosen = choose_auto_method(
            preferred_action=info.get("preferred_action"),
            top_hwnd=info.get("top_hwnd"),
            expect_hwnd=expect,
        )
        step["method"] = "auto"
        step["chosen_method"] = chosen
        step["uia"] = public_element_info(info, frame)
        if meta.get("reprobed"):
            step["uia_reprobe"] = True
            step["uia_probe_attempts"] = 2
            first = {
                k: meta.get(fk)
                for k, fk in (
                    ("control_type", "first_control_type"),
                    ("name", "first_name"),
                    ("error", "first_error"),
                )
                if meta.get(fk) is not None
            }
            step["uia_first"] = first
        else:
            step["uia_probe_attempts"] = 1
        if chosen == "uia":
            if info.get("_control") is not None:
                step["_uia_control"] = info["_control"]
                step["_uia_action"] = info.get("preferred_action")
            step["uia_ok"] = True
        else:
            step["uia_ok"] = False
            if info.get("error"):
                step["uia_skip_reason"] = info.get("error")
    return steps


HOLD_KEYS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt"}


def normalize_hold_key(hold_key: str | None) -> str | None:
    if hold_key in (None, ""):
        return None
    key = HOLD_KEYS.get(str(hold_key).strip().lower())
    if key is None:
        raise ValueError("hold_key must be ctrl, control, shift, or alt")
    return key




def _parse_stamp_age_sec(stamp: str | None, json_path: str | Path | None = None) -> float | None:
    """Age in seconds from stamp YYYYMMDD-HHMMSS[-mmm][-N], else json file mtime. None if unknown."""
    from datetime import datetime
    import time as _time

    from .stamps import parse_time

    if stamp:
        dt = parse_time(str(stamp))
        if dt is not None:
            return max(0.0, (datetime.now() - dt).total_seconds())
    if json_path:
        p = Path(json_path)
        try:
            if p.is_file():
                return max(0.0, _time.time() - p.stat().st_mtime)
        except OSError:
            pass
    return None


def _needs_window_checks(frame: dict) -> bool:
    coord = frame.get("coord_space")
    return coord == "window" or (frame.get("hwnd") is not None and coord != "screen")


def stamp_identity_error(frame: dict | None) -> str | None:
    """HWND + PID + process creation time on the stamp vs live. None = same window.

    Screen-space stamps carry no window and pass. Window stamps without an
    identity block (older stamps) fail closed.
    """
    frame = frame or {}
    if not _needs_window_checks(frame):
        return None
    hwnd = frame.get("hwnd")
    ident = frame.get("identity") or {}
    if not ident:
        return "stamp has no window identity (perceived by an older cu-perceive); re-perceive"
    try:
        if int(ident.get("hwnd")) != int(hwnd):
            return f"stamp identity hwnd {ident.get('hwnd')!r} != stamp hwnd {hwnd!r}; re-perceive"
    except (TypeError, ValueError):
        return f"bad stamp identity hwnd {ident.get('hwnd')!r}; re-perceive"
    from .windows import verify_window_identity

    return verify_window_identity(hwnd, ident.get("pid"), ident.get("ctime"))


def check_stamp(
    frame: dict | None,
    *,
    max_age_sec: float | None = None,
    origin_slop_px: int = 8,
    json_path: str | Path | None = None,
    size_slop_px: int = 24,
) -> dict:
    """E3 stamp usability: hwnd/origin/age/ROI/minimized. Fail closed for planning/--go.

    Returns {ok, stamp, hwnd, age_sec?, origin_delta?, error?}.
    Live iconic check (IsIconic / showCmd), then live_geom vs stamp origin.
    Never clicks; never calls refresh_geom (that path restores iconic hwnds).
    """
    frame = frame or {}
    stamp = frame.get("stamp")
    hwnd = frame.get("hwnd")
    out: dict = {"ok": True, "stamp": stamp, "hwnd": hwnd}

    # ROI / light-verify stamps are verify-only — not for multi-step act planning.
    if frame.get("verify_light"):
        out["ok"] = False
        out["error"] = (
            "ROI / verify_light stamp is verify-only; use a full-window perceive "
            "stamp for multi-step planning (re-perceive without --roi/--roi-norm)"
        )
        return out

    age = _parse_stamp_age_sec(stamp if isinstance(stamp, str) else None, json_path)
    if age is not None:
        out["age_sec"] = round(age, 3)
    if max_age_sec is not None:
        try:
            limit = float(max_age_sec)
        except (TypeError, ValueError):
            out["ok"] = False
            out["error"] = f"invalid max_age_sec: {max_age_sec!r}"
            return out
        if age is None:
            out["ok"] = False
            out["error"] = (
                "max_age_sec set but stamp age unknown "
                "(need YYYYMMDD-HHMMSS stamp or json mtime); re-perceive"
            )
            return out
        if age > limit:
            out["ok"] = False
            out["error"] = (
                f"stamp age {age:.1f}s exceeds max_age_sec={limit:g}; re-perceive"
            )
            return out

    coord = frame.get("coord_space")
    # Window frames (or hwnd-bearing non-screen): require live hwnd/origin match.
    if coord == "window" or (hwnd is not None and coord != "screen"):
        if not hwnd:
            out["ok"] = False
            out["error"] = "window-space stamp missing hwnd; re-perceive a window"
            return out
        try:
            hwnd_i = int(hwnd)
        except (TypeError, ValueError):
            out["ok"] = False
            out["error"] = f"bad hwnd on stamp: {hwnd!r}; re-perceive"
            return out
        out["hwnd"] = hwnd_i

        try:
            import win32gui

            if not win32gui.IsWindow(hwnd_i):
                out["ok"] = False
                out["identity_ok"] = False
                out["error"] = f"hwnd {hwnd_i} no longer exists; re-perceive"
                return out
        except Exception as e:
            out["ok"] = False
            out["error"] = f"hwnd check failed: {e}; re-perceive"
            return out

        id_err = stamp_identity_error(frame)
        out["identity_ok"] = id_err is None
        if id_err:
            out["ok"] = False
            out["error"] = id_err
            return out

        origin = frame.get("origin")
        from .windows import is_hwnd_minimized, is_sentinel_origin

        # Fail closed on live iconic state *before* any origin compare.
        # refresh_geom restores iconic hwnds (capture path) and then returns
        # the restore rect, which matches a pre-minimize stamp (Δ=0).
        # live_geom never force_foregrounds; is_hwnd_minimized ignores
        # GetWindowPlacement.rcNormalPosition.
        try:
            if is_hwnd_minimized(hwnd_i):
                out["ok"] = False
                out["error"] = "window minimized; stamp invalid — re-perceive"
                return out
        except Exception as e:
            out["ok"] = False
            out["error"] = f"minimized check failed: {e}; re-perceive"
            return out

        if is_sentinel_origin(origin) or (isinstance(origin, dict) and origin.get("bad")):
            out["ok"] = False
            out["error"] = (
                "stamp origin is minimized sentinel / bad; restore window and re-perceive"
            )
            return out

        try:
            from .windows import live_geom

            live_origin, _win = live_geom(hwnd_i)
        except Exception as e:
            out["ok"] = False
            out["error"] = f"live_geom failed: {e}; re-perceive"
            return out

        if live_origin is None:
            out["ok"] = False
            out["error"] = "live client origin unavailable; re-perceive"
            return out
        if is_sentinel_origin(live_origin):
            out["ok"] = False
            out["error"] = (
                "window is minimized (live sentinel origin); restore and re-perceive"
            )
            return out

        try:
            d_left = abs(int(live_origin["left"]) - int(origin["left"]))
            d_top = abs(int(live_origin["top"]) - int(origin["top"]))
            d_w = abs(int(live_origin.get("width", 0)) - int((origin or {}).get("width", 0)))
            d_h = abs(int(live_origin.get("height", 0)) - int((origin or {}).get("height", 0)))
        except (TypeError, ValueError, KeyError) as e:
            out["ok"] = False
            out["error"] = f"stamp/live origin compare failed: {e}; re-perceive"
            return out

        out["origin_delta"] = {"left": d_left, "top": d_top, "width": d_w, "height": d_h}
        slop = int(origin_slop_px)
        if d_left > slop or d_top > slop:
            out["ok"] = False
            out["error"] = (
                f"window moved (origin \u0394left={d_left} \u0394top={d_top} > slop={slop}px); "
                "stamp invalid \u2014 re-perceive (do not blind-click)"
            )
            return out
        size_slop = int(size_slop_px)
        if d_w > size_slop or d_h > size_slop:
            out["ok"] = False
            out["error"] = (
                f"window resized (\u0394w={d_w} \u0394h={d_h} > slop={size_slop}px); "
                "stamp invalid \u2014 re-perceive"
            )
            return out

    return out


def apply_foreground_accounting(out: dict) -> None:
    """Mark SendInput / true-cursor usage on click steps and the act JSON.

    Mutates out["steps"] (per-step foreground_used = planned) and sets top-level
    planned_foreground_click_count. foreground_used / foreground_click_count are the
    planned values on a dry run; on --go they start at 0/False and count only clicks
    actually sent (_count_live_click), so a refused --go reports 0.
    Only via in id/xy/norm count as clicks. Effective method is chosen_method when
    out["method"]=="auto" (missing chosen -> input); otherwise out["method"].
    input -> True; post/uia -> False.
    """
    method = out.get("method")
    count = 0
    for step in out.get("steps") or []:
        if step.get("via") not in ("id", "xy", "norm"):
            continue
        if method == "auto":
            effective = step.get("chosen_method") or "input"
        else:
            effective = method
        used = effective == "input"
        step["foreground_used"] = used
        if used:
            count += 1
    out["planned_foreground_click_count"] = count
    if out.get("dry_run", True):
        out["foreground_used"] = count > 0
        out["foreground_click_count"] = count
    else:
        out["foreground_used"] = False
        out["foreground_click_count"] = 0


def _count_live_click(out: dict) -> None:
    out["foreground_click_count"] = int(out.get("foreground_click_count") or 0) + 1
    out["foreground_used"] = True


def _identity_abort(out: dict, frame: dict, steps: list[dict], n: int) -> bool:
    """Re-check stamp identity right before live step n. True = aborted (out updated)."""
    err = stamp_identity_error(frame)
    if err is None:
        return False
    out["ok"] = False
    out["error"] = f"window identity check before step {n} failed: {err}"
    out["identity_abort_step"] = n
    for s in steps:
        s.pop("_uia_control", None)
        s.pop("_uia_action", None)
    return True


def run_chain(frame: dict, specs: list[dict], *, dry_run: bool = True, double: bool = False, gap: float = 0.12, hold_key: str | None = None, method: str | None = None, max_age_sec: float | None = None, strict_stamp: bool = False, stamp_json_path: str | Path | None = None, origin_slop_px: int = 8) -> dict:
    if not specs:
        return {
            "ok": False,
            "dry_run": bool(dry_run),
            "stamp": (frame or {}).get("stamp"),
            "hwnd": (frame or {}).get("hwnd"),
            "coord_space": (frame or {}).get("coord_space"),
            "origin": (frame or {}).get("origin"),
            "steps": [],
            "error": (
                "no actionable steps: pass flat fields norm/xy/ids/text/key/drag/drag_norm/scroll "
                '(e.g. norm="935,69"). Unknown keys like steps=[{...}] are ignored by MCP - not executed.'
            ),
        }
    method_n = normalize_method(method)
    if method_n in ("post", "uia", "auto"):
        scope_fn = {
            "post": _post_scope_error,
            "uia": _uia_scope_error,
            "auto": _auto_scope_error,
        }[method_n]
        err = scope_fn(specs, hold_key=hold_key)
        if err:
            return {
                "ok": False,
                "dry_run": bool(dry_run),
                "method": method_n,
                "stamp": (frame or {}).get("stamp"),
                "hwnd": (frame or {}).get("hwnd"),
                "coord_space": (frame or {}).get("coord_space"),
                "origin": (frame or {}).get("origin"),
                "steps": [],
                "error": err,
            }
    from . import extras

    uia_missing = method_n in ("uia", "auto") and bool(extras.missing_modules("uia"))
    if method_n == "uia" and uia_missing:
        try:
            extras.require("uia")
        except extras.MissingExtraError as e:
            return {
                **e.to_dict(),
                "dry_run": bool(dry_run),
                "method": method_n,
                "stamp": (frame or {}).get("stamp"),
                "hwnd": (frame or {}).get("hwnd"),
                "coord_space": (frame or {}).get("coord_space"),
                "origin": (frame or {}).get("origin"),
                "steps": [],
            }
    try:
        steps = plan_steps(frame or {}, specs)
    except ValueError as e:
        return {
            "ok": False,
            "dry_run": bool(dry_run),
            "method": method_n,
            "stamp": (frame or {}).get("stamp"),
            "hwnd": (frame or {}).get("hwnd"),
            "coord_space": (frame or {}).get("coord_space"),
            "origin": (frame or {}).get("origin"),
            "steps": [],
            "error": str(e),
        }
    hold = normalize_hold_key(hold_key)
    out = {
        "ok": True,
        "dry_run": bool(dry_run),
        "method": method_n,
        "stamp": (frame or {}).get("stamp"),
        "hwnd": (frame or {}).get("hwnd"),
        "coord_space": (frame or {}).get("coord_space"),
        "origin": (frame or {}).get("origin"),
        "steps": steps,
    }
    if hold:
        out["hold_key"] = hold
    if uia_missing and method_n == "auto":
        out["warnings"] = [
            "uia extra not installed, so method=auto cannot probe UIA and uses input; install: "
            + extras.install_command("uia")
        ]
    if method_n == "post":
        try:
            enrich_post_steps(frame or {}, steps, double=double)
        except (MethodError, OSError, ValueError) as e:
            out["ok"] = False
            out["error"] = str(e)
            out["steps"] = steps
            apply_foreground_accounting(out)
            return out
    if method_n == "uia":
        try:
            enrich_uia_steps(frame or {}, steps, double=double)
        except (MethodError, OSError, ValueError) as e:
            out["ok"] = False
            out["error"] = str(e)
            out["steps"] = steps
            apply_foreground_accounting(out)
            return out
        # Fail-closed if any step lacked a usable pattern / hwnd mismatch
        bad = [s for s in steps if s.get("via") in ("id", "xy", "norm") and not s.get("uia_ok")]
        if bad:
            out["ok"] = False
            out["error"] = bad[0].get("uia_error") or "UIA resolve failed"
            # strip live handles before return
            for s in steps:
                s.pop("_uia_control", None)
                s.pop("_uia_action", None)
            out["steps"] = steps
            apply_foreground_accounting(out)
            return out
    if method_n == "auto":
        try:
            enrich_auto_steps(frame or {}, steps, double=double)
        except (MethodError, OSError, ValueError) as e:
            out["ok"] = False
            out["error"] = str(e)
            out["steps"] = steps
            apply_foreground_accounting(out)
            return out
        click_steps = [s for s in steps if s.get("via") in ("id", "xy", "norm")]
        choices = [s.get("chosen_method") for s in click_steps]
        if choices and all(c == choices[0] for c in choices):
            out["chosen_method"] = choices[0]
        elif choices:
            out["chosen_method"] = choices[0]
            out["chosen_methods"] = choices
    apply_foreground_accounting(out)
    stamp_check = check_stamp(
        frame or {},
        max_age_sec=max_age_sec,
        origin_slop_px=origin_slop_px,
        json_path=stamp_json_path,
    )
    out["stamp_check"] = stamp_check
    out["stamp_reuse"] = True
    out["step_count"] = len(steps)
    stamp_bad = not stamp_check.get("ok")
    out["stamp_ok"] = not stamp_bad
    if stamp_bad:
        out["stamp_error"] = stamp_check.get("error") or "stamp check failed; re-perceive"
    # A plan for a window that is gone or now belongs to another process is wrong, so
    # identity failures fail the dry run too; moved/aged stamps only with strict_stamp.
    wrong_window = stamp_check.get("identity_ok") is False
    if stamp_bad and (not dry_run or strict_stamp or wrong_window):
        out["ok"] = False
        out["error"] = stamp_check.get("error") or "stamp check failed; re-perceive"
        if method_n in ("uia", "auto"):
            for s in steps:
                s.pop("_uia_control", None)
                s.pop("_uia_action", None)
        return out
    if dry_run:
        if method_n in ("uia", "auto"):
            for s in steps:
                s.pop("_uia_control", None)
                s.pop("_uia_action", None)
        return out
    if method_n == "auto":
        # Live auto: execute chosen path only. No mid-go fallback uia->input.
        from .uia import UiaError, invoke_control
        from .input import click_xy

        for n, step in enumerate(steps):
            if _identity_abort(out, frame or {}, steps, n):
                return out
            via = step["via"]
            if via not in ("id", "xy", "norm"):
                out["ok"] = False
                out["error"] = f"method=auto unexpected step via={via!r}"
                for s in steps:
                    s.pop("_uia_control", None)
                    s.pop("_uia_action", None)
                return out
            chosen = step.get("chosen_method") or "input"
            if chosen == "uia":
                ctrl = step.pop("_uia_control", None)
                action = step.pop("_uia_action", None)
                try:
                    if ctrl is None:
                        from .uia import resolve_at_screen

                        info = resolve_at_screen(
                            step["screen"][0],
                            step["screen"][1],
                            expect_hwnd=int((frame or {}).get("hwnd") or 0) or None,
                        )
                        if not info.get("ok"):
                            raise UiaError(info.get("error") or "UIA re-resolve failed")
                        ctrl = info.get("_control")
                        action = info.get("preferred_action")
                    result = invoke_control(ctrl, action=action)
                except (UiaError, OSError, ValueError) as e:
                    out["ok"] = False
                    out["error"] = f"UIA invoke failed (no input fallback): {e}"
                    step["invoked"] = False
                    for s in steps:
                        s.pop("_uia_control", None)
                        s.pop("_uia_action", None)
                    return out
                step["invoked"] = True
                step["uia_action"] = result.get("action")
            else:
                step.pop("_uia_control", None)
                step.pop("_uia_action", None)
                click_xy(
                    step["screen"][0],
                    step["screen"][1],
                    double=double,
                    button=step.get("button") or "left",
                    hold=float(step.get("hold") or 0),
                )
                step["clicked"] = True
                _count_live_click(out)
            if n + 1 < len(steps):
                time.sleep(gap)
        for s in steps:
            s.pop("_uia_control", None)
            s.pop("_uia_action", None)
        return out
    if method_n == "uia":
        # Live uia: pattern Invoke/Toggle/Expand/Select only. No post/input fallback.
        from .uia import UiaError, invoke_control

        for n, step in enumerate(steps):
            if _identity_abort(out, frame or {}, steps, n):
                return out
            via = step["via"]
            if via not in ("id", "xy", "norm"):
                out["ok"] = False
                out["error"] = f"method=uia unexpected step via={via!r}"
                for s in steps:
                    s.pop("_uia_control", None)
                    s.pop("_uia_action", None)
                return out
            ctrl = step.pop("_uia_control", None)
            action = step.pop("_uia_action", None)
            try:
                if ctrl is None:
                    # re-resolve if handle lost
                    from .uia import resolve_at_screen

                    info = resolve_at_screen(
                        step["screen"][0],
                        step["screen"][1],
                        expect_hwnd=int((frame or {}).get("hwnd") or 0) or None,
                    )
                    if not info.get("ok"):
                        raise UiaError(info.get("error") or "UIA re-resolve failed")
                    ctrl = info.get("_control")
                    action = info.get("preferred_action")
                result = invoke_control(ctrl, action=action)
            except (UiaError, OSError, ValueError) as e:
                out["ok"] = False
                out["error"] = f"UIA invoke failed (no post/input fallback): {e}"
                step["invoked"] = False
                for s in steps:
                    s.pop("_uia_control", None)
                    s.pop("_uia_action", None)
                return out
            step["invoked"] = True
            step["uia_action"] = result.get("action")
            if n + 1 < len(steps):
                time.sleep(gap)
        for s in steps:
            s.pop("_uia_control", None)
            s.pop("_uia_action", None)
        return out
    if method_n == "post":
        # Live post: PostMessage only. Never SetCursorPos / SendInput fallback.
        from .input import post_click_xy
        hwnd = int((frame or {}).get("hwnd") or 0)
        for n, step in enumerate(steps):
            if _identity_abort(out, frame or {}, steps, n):
                return out
            via = step["via"]
            if via not in ("id", "xy", "norm"):
                out["ok"] = False
                out["error"] = f"method=post unexpected step via={via!r}"
                return out
            try:
                info = post_click_xy(
                    hwnd,
                    step["screen"][0],
                    step["screen"][1],
                    button=step.get("button") or "left",
                    double=double,
                    screen=True,
                )
            except (OSError, ValueError) as e:
                out["ok"] = False
                out["error"] = f"PostMessage failed (no SendInput fallback): {e}"
                step["posted"] = False
                return out
            step["posted"] = True
            step["client"] = info.get("client")
            step["lparam"] = info.get("lparam")
            if n + 1 < len(steps):
                time.sleep(gap)
        return out
    from .input import click_xy, drag_path, enter_text, hover_xy, key_down, key_up, scroll_xy, tap_key
    try:
        if hold:
            key_down(hold)
        for n, step in enumerate(steps):
            if _identity_abort(out, frame or {}, steps, n):
                return out
            via = step["via"]
            if via == "hover":
                hover_xy(step["screen"][0], step["screen"][1])
                step["hovered"] = True
            elif via in ("id", "xy", "norm"):
                click_xy(step["screen"][0], step["screen"][1], double=double, button=step.get("button") or "left", hold=float(step.get("hold") or 0))
                step["clicked"] = True
                _count_live_click(out)
            elif via == "wait":
                time.sleep(float(step["sec"]))
                step["waited"] = True
            elif via == "drag":
                info = drag_path(step["screen_points"], step_px=int(step.get("step_px") or 4))
                step["dragged"] = True
                step["samples"] = info.get("samples")
            elif via == "scroll":
                xy = step.get("screen")
                if xy:
                    scroll_xy(step["notches"], xy[0], xy[1])
                else:
                    scroll_xy(step["notches"])
                step["scrolled"] = True
            elif via == "type":
                enter_text(step["text"], via=step.get("input_via") or "paste")
                step["typed"] = True
                step["input_via"] = step.get("input_via") or "paste"
            elif via == "key":
                tap_key(step["key"])
                step["tapped"] = True
            if n + 1 < len(steps):
                time.sleep(gap)
        return out
    finally:
        if hold:
            key_up(hold)
