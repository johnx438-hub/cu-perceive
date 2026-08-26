"""Plan then run a chain: click / type / key / drag / scroll."""
from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_OUT = Path(r"C:\Users\jawn\agent-bus\archive\shots\perceive")


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
    s = s.strip().replace(" ", ";")
    out = []
    for chunk in s.split(";"):
        if not chunk:
            continue
        a, b = chunk.split(",")
        out.append([int(a), int(b)])
    return out


def _point_pairs(s: str) -> list[list[int]]:
    nums = [int(x) for x in s.replace(";", ",").replace(" ", ",").split(",") if x]
    if len(nums) < 4 or len(nums) % 2:
        raise ValueError("drag needs at least two x,y pairs")
    return [[nums[i], nums[i + 1]] for i in range(0, len(nums), 2)]



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
) -> list[dict]:
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
    if float(hold or 0) > 0:
        for s in out:
            if s.get("kind") in ("id", "xy", "norm"):
                s["hold"] = float(hold)
    has_scroll = any(s.get("kind") == "scroll" for s in out)
    has_pos = any(s.get("kind") in ("id", "xy", "norm") for s in out)
    # scroll + xy/norm/ids => hover (move, do not click) unless an explicit
    # click was asked for (hold>0 long-press). hover=True still wins.
    click_intent = float(hold or 0) > 0
    if hover or (has_scroll and has_pos and not click_intent):
        for s in out:
            if s.get("kind") in ("id", "xy", "norm"):
                s["hover"] = True
    if button:
        for s in out:
            if s.get("kind") in ("id", "xy", "norm"):
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
            for p in raw:
                if space == "norm":
                    x, y = norm_to_window(frame, int(p[0]), int(p[1]))
                else:
                    x, y = int(p[0]), int(p[1])
                win_pts.append([x, y])
                sx, sy = window_to_screen(frame, x, y)
                screen_pts.append([sx, sy])
            last_screen = list(screen_pts[-1])
            from .input import _densify
            samples = len(_densify([tuple(p) for p in screen_pts], int(spec.get("step_px") or 4)))
            steps.append({
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
            })
        elif kind == "scroll":
            steps.append({"via": "scroll", "notches": int(spec["notches"]), "screen": last_screen})
        elif kind == "type":
            steps.append({"via": "type", "text": spec["text"], "input_via": spec.get("input_via") or "paste"})
        elif kind == "key":
            steps.append({"via": "key", "key": spec["key"]})
        elif kind == "wait":
            steps.append({"via": "wait", "sec": float(spec["sec"])})
        else:
            raise ValueError(spec)
    return steps


HOLD_KEYS = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt"}


def normalize_hold_key(hold_key: str | None) -> str | None:
    if hold_key in (None, ""):
        return None
    key = HOLD_KEYS.get(str(hold_key).strip().lower())
    if key is None:
        raise ValueError("hold_key must be ctrl, control, shift, or alt")
    return key


def run_chain(frame: dict, specs: list[dict], *, dry_run: bool = True, double: bool = False, gap: float = 0.12, hold_key: str | None = None) -> dict:
    steps = plan_steps(frame or {}, specs)
    hold = normalize_hold_key(hold_key)
    out = {
        "ok": True,
        "dry_run": bool(dry_run),
        "stamp": (frame or {}).get("stamp"),
        "hwnd": (frame or {}).get("hwnd"),
        "coord_space": (frame or {}).get("coord_space"),
        "origin": (frame or {}).get("origin"),
        "steps": steps,
    }
    if hold:
        out["hold_key"] = hold
    if dry_run:
        return out
    from .input import click_xy, drag_path, enter_text, hover_xy, key_down, key_up, scroll_xy, tap_key
    try:
        if hold:
            key_down(hold)
        for n, step in enumerate(steps):
            via = step["via"]
            if via == "hover":
                hover_xy(step["screen"][0], step["screen"][1])
                step["hovered"] = True
            elif via in ("id", "xy", "norm"):
                click_xy(step["screen"][0], step["screen"][1], double=double, button=step.get("button") or "left", hold=float(step.get("hold") or 0))
                step["clicked"] = True
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
