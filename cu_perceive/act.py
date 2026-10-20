"""Plan then run a chain: click / type / key / drag / scroll."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .config import out_dir as _config_out_dir
from .keys import split_key_list

DEFAULT_OUT = _config_out_dir()
# go=true acts share one process-wide lock so parallel MCP calls cannot interleave SendInput.
LIVE_ACT_LOCK = threading.Lock()
_live_lock_state = threading.local()
_ime_restore = threading.local()


def read_frame(path: Path) -> dict:
    """Parse one stamp JSON. ValueError (not a bare JSONDecodeError) when empty or partial."""
    text = Path(path).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"stamp json is empty (still being written?): {path}; retry or re-perceive")
    try:
        frame = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"stamp json is unreadable ({e}): {path}; retry or re-perceive") from e
    if not isinstance(frame, dict):
        raise ValueError(f"stamp json is not an object: {path}; re-perceive")
    return frame


def _is_perceive_stamp_json(p: Path) -> bool:
    """<stamp>.json from perceive. Excludes <stamp>.windows.json desktop maps."""
    from . import stamps

    return p.suffix == ".json" and bool(stamps.STAMP_RE.match(p.name[: -len(".json")]))


def latest_frame(out_dir: Path | None = None, hwnd: int | None = None) -> tuple[dict, Path | None]:
    """Newest readable perceive stamp; skips windows maps and empty / partial files.

    hwnd: newest stamp of that window only. perceive records each window's newest stamp
    (stamps.note_latest), so only that stamp and files with a newer mtime are read; without
    a record, files are read newest-first until one matches.
    """
    from . import stamps

    default_dir = out_dir is None
    out_dir = Path(out_dir or DEFAULT_OUT)
    noted = stamps.read_latest(out_dir, int(hwnd)) if hwnd else None
    if not hwnd and default_dir:
        # Newest stamp anywhere (custom out_dir included), via the registry.
        noted = stamps.latest_any(out_dir)
    noted_frame = None
    floor = None
    if noted is not None:
        try:
            noted_frame = read_frame(noted)
            if not hwnd or _frame_hwnd(noted_frame) == int(hwnd):
                floor = noted.stat().st_mtime
            else:
                noted_frame = None
        except (OSError, ValueError):
            noted_frame = None
    cands = []
    for p in out_dir.glob("*.json"):
        if not _is_perceive_stamp_json(p):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if floor is not None and (mtime < floor or p == noted):
            continue
        cands.append((mtime, p))
    if not cands and noted_frame is None:
        return {}, None
    cands.sort(key=lambda t: t[0], reverse=True)
    for _, p in cands:
        try:
            frame = read_frame(p)
        except (OSError, ValueError):
            continue
        if hwnd and _frame_hwnd(frame) != int(hwnd):
            continue
        return frame, p
    if noted_frame is not None:
        return noted_frame, noted
    if hwnd:
        raise FileNotFoundError(
            f"no perceive stamp for hwnd={int(hwnd)} in {out_dir}; perceive that window first "
            "(perceive_window hwnd=...) or pass its stamp"
        )
    raise ValueError(f"no readable perceive stamp in {out_dir}; re-perceive or pass --stamp")


class StampWindowMismatch(ValueError):
    """act hwnd= names a different window than the stamp's."""


def _frame_hwnd(frame: dict) -> int | None:
    try:
        return int((frame or {}).get("hwnd") or 0) or None
    except (TypeError, ValueError):
        return None


def stamp_json_path(stamp: str) -> Path:
    """Path for --stamp / stamp=: a stamp name, or the json path a perceive returned."""
    s = str(stamp).strip().strip('"').strip("'")
    p = Path(s)
    base = p if (p.is_absolute() or len(p.parts) > 1) else DEFAULT_OUT / p
    bare = not (p.is_absolute() or len(p.parts) > 1)
    if s.lower().endswith(".json"):
        path = base
    else:
        path = base.with_name(base.name + ".json")
        windows_map = base.with_name(base.name + ".windows.json")
        if not path.is_file() and windows_map.is_file():
            path = windows_map
    if bare and not path.is_file():
        from . import stamps

        found = stamps.lookup(s, DEFAULT_OUT)
        if found is not None:
            return found
    return path


def load_frame(
    stamp: str | None = None, json_path: str | None = None, hwnd: int | None = None
) -> tuple[dict, Path | None]:
    """Stamp frame + its path. hwnd (optional): use that window's newest stamp when no
    stamp is given, or refuse a stamp that belongs to another window."""
    if json_path:
        path = Path(json_path)
    elif stamp:
        path = stamp_json_path(stamp)
    else:
        return latest_frame(hwnd=hwnd)
    if not path.is_file():
        raise FileNotFoundError(f"stamp json not found: {path}")
    frame = read_frame(path)
    if hwnd and _frame_hwnd(frame) != int(hwnd):
        raise StampWindowMismatch(
            f"stamp {frame.get('stamp') or path.name} is for hwnd={_frame_hwnd(frame)}, not hwnd={int(hwnd)}; "
            f"perceive hwnd={int(hwnd)} and act on that stamp, or drop hwnd"
        )
    return frame, path


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


class BadIdsError(KeyError, ValueError):
    """An act id that is not an item of the stamp. run_chain turns it into reason=bad_ids."""

    def __init__(self, message: str, *, frame: dict | None = None, id_=None):
        super().__init__(message)
        self.message = message
        self.id = id_
        frame = frame or {}
        self.item_ids = [it.get("id") for it in frame.get("items") or []]
        self.uia_ids = [n.get("id") for n in frame.get("uia_nodes") or []]

    def __str__(self) -> str:
        return self.message

    def hint(self) -> str:
        if self.id is not None and self.id in self.uia_ids:
            return (
                f"id {self.id} is a UIA node id, but ids refer to OCR items when the stamp has them; "
                f"pass uia_name=\"#{self.id}\" (or the node's center_norm as norm)"
            )
        if not self.item_ids:
            return (
                "this stamp has no OCR items (perceive ran with ocr=false); click by norm (center_norm), "
                "by uia_name, or perceive again with ocr=true"
            )
        return "use an id from this stamp's items, or click by norm (center_norm) or uia_name"


def item_by_id(frame: dict, id_: int) -> dict:
    for it in (frame or {}).get("items") or []:
        if int(it["id"]) == int(id_):
            return it
    raise BadIdsError(f"id {id_} not in frame {(frame or {}).get('stamp')}", frame=frame, id_=int(id_))


def _uia_node_for_id(frame: dict, id_: int) -> dict | None:
    """When the stamp has no OCR items, an act id may name a UIA node (compact rows share ids)."""
    frame = frame or {}
    if frame.get("items"):
        return None
    for n in frame.get("uia_nodes") or []:
        try:
            if int(n.get("id")) == int(id_) and n.get("norm"):
                return n
        except (TypeError, ValueError):
            continue
    return None


def pairs(s: str) -> list[list[int]]:
    """Parse points. Accepts x,y or x:y; multi with ;. A 4-number box gives its center."""
    return [p for p, _box in point_boxes(s)]


def point_boxes(s: str) -> list[tuple[list[int], list[int] | None]]:
    """(point, box) per entry of "x,y;..."; box is set when the entry was a
    "left,top,right,bottom" box (the norm / bbox format of OCR items and UIA nodes)
    and point is its center."""
    import re

    s = re.sub(r"\s*([,:])\s*", r"\1", s.strip()).replace(" ", ";")
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
        if len(parts) == 4:
            l, t, r, b = (int(round(float(v))) for v in parts)
            if r < l or b < t:
                raise ValueError(
                    f"bad box {chunk!r}: 4 numbers are read as left,top,right,bottom (right >= left, "
                    "bottom >= top); for one point pass two numbers"
                )
            out.append(([(l + r) // 2, (t + b) // 2], [l, t, r, b]))
            continue
        if len(parts) != 2:
            raise ValueError(
                f'bad point {chunk!r}: expected two ints "x,y" or a box "left,top,right,bottom" '
                f"(got {len(parts)} parts)"
            )
        out.append(([int(parts[0]), int(parts[1])], None))
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


MAX_SCROLL_NOTCHES = 50
STALE_STAMP_SEC = 120


def scroll_spec(raw) -> dict:
    """kind=scroll spec; counts past MAX_SCROLL_NOTCHES are clamped (clamped_from keeps the ask)."""
    n = parse_scroll(raw)
    spec: dict = {"kind": "scroll", "notches": n}
    if abs(n) > MAX_SCROLL_NOTCHES:
        spec["notches"] = MAX_SCROLL_NOTCHES if n > 0 else -MAX_SCROLL_NOTCHES
        spec["clamped_from"] = n
    return spec


def _num(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(f"bad coordinate {v!r}: expected a number")
    return int(round(float(v)))


def coerce_points(v) -> str | None:
    """Points as act's "x,y;x,y" string. Also takes [x, y], [[x, y], ...], {"x":..,"y":..}
    and the JSON text of those; floats are rounded. Empty -> None."""
    if v is None:
        return None
    if isinstance(v, (bool, int, float)):
        raise ValueError(f'bad point {v!r}: expected "x,y" or [x, y]')
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in ("[", "{"):
            try:
                return coerce_points(json.loads(s))
            except json.JSONDecodeError as e:
                raise ValueError(f'bad point {s!r}: expected "x,y" or [x, y] ({e})') from e
        return s or None
    if isinstance(v, dict):
        if "x" in v and "y" in v:
            return f"{_num(v['x'])},{_num(v['y'])}"
        raise ValueError(f'bad point {v!r}: expected {{"x": .., "y": ..}}')
    if isinstance(v, (list, tuple)):
        if not v:
            return None
        if len(v) in (2, 4) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            return ",".join(str(_num(x)) for x in v)
        parts = [coerce_points(x) for x in v]
        return ";".join(p for p in parts if p) or None
    raise ValueError(f'bad point {v!r}: expected "x,y" or [x, y]')


def parse_scroll(raw: str | int) -> int:
    """Wheel notches (one notch = one wheel click, ~3 lines; not pixels). +up / -down.
    Also accepts up:3, down:3, up, down, and ints."""
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


def normalize_input_via(raw) -> str:
    """'paste' (clipboard + Ctrl+V) or 'keys' (SendInput KEYEVENTF_UNICODE per character)."""
    if raw in (None, ""):
        return "paste"
    s = str(raw).strip().lower()
    if s == "paste":
        return "paste"
    if s == "keys":
        return "keys"
    raise ValueError('via must be "paste" or "keys" (got %r)' % (raw,))


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
    input_via: str | None = None,
) -> list[dict]:
    """Flat act fields -> ordered specs. replace=True marks each type step to clear the
    edit first (ValuePattern / EM_SETSEL / Home+Shift+End), not Ctrl+A."""
    out: list[dict] = []
    xy, norm = coerce_points(xy), coerce_points(norm)
    drag, drag_norm = coerce_points(drag), coerce_points(drag_norm)
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
                out.append(scroll_spec(raw[7:]))
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
        for kind, raw in (("xy", xy), ("norm", norm)):
            for p, box in point_boxes(raw) if raw else []:
                spec = {"kind": kind, kind: p}
                if box:
                    spec["box"] = box
                out.append(spec)
    extra = {}
    if step_px is not None:
        extra["step_px"] = int(step_px)
    if drag:
        out.append({"kind": "drag", "points": _point_pairs(drag), "space": "xy", **extra})
    if drag_norm:
        out.append({"kind": "drag", "points": _point_pairs(drag_norm), "space": "norm", **extra})
    if scroll not in (None, ""):
        spec = scroll_spec(scroll)
        if spec["notches"] > 0 and str(scroll).strip().lstrip("+").isdigit():
            spec["bare_positive"] = True
        out.append(spec)
    if extras:
        for e in extras:
            if e.get("kind") == "key":
                for k in split_key_list(e.get("key") or ""):
                    out.append({"kind": "key", "key": k})
            elif e.get("kind") == "type":
                out.append({"kind": "type", "text": e.get("text") or "", "input_via": e.get("input_via") or "paste"})
    else:
        if text:
            out.append({"kind": "type", "text": text, "input_via": "paste"})
        if key:
            for k in split_key_list(key):
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
    if input_via not in (None, ""):
        mode = normalize_input_via(input_via)
        for s in out:
            if s.get("kind") == "type":
                s["input_via"] = mode
    if replace:
        for s in out:
            if s.get("kind") == "type":
                s["replace"] = True
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
            node = _uia_node_for_id(frame, spec["id"])
            if node is not None:
                spec = {**spec, "kind": "uia_name", "query": f"#{int(spec['id'])}", "id_source": "uia"}
                kind = "uia_name"
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
                step["replace_via"] = "clear_edit"
            steps.append(step)
        elif kind == "key":
            from .keys import check_key_spec

            step = {"via": "key", "key": check_key_spec(spec["key"])}
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
                raise UiaTargetError(resolved.get("error") or f"UIA resolve failed for {q!r}")
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
            if spec.get("id_source"):
                step["id"] = int(spec["query"].lstrip("#"))
                step["id_source"] = spec["id_source"]
            if spec.get("hover"):
                step["via"] = "hover"
            steps.append(step)
        else:
            raise ValueError(spec)
    return steps



class UiaTargetError(ValueError):
    """uia_name did not resolve to exactly one node of the stamp."""


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


HOLD_KEY_POINTER_KINDS = ("id", "xy", "norm", "uia_name", "drag", "scroll")
HOLD_KEY_HINT = (
    'add the pointer step to the same call, e.g. norm="500,500" scroll="down:3" hold_key="ctrl"; '
    'for a key chord use key="ctrl+pagedown" instead'
)


def hold_key_scope_error(specs: list[dict], hold_key: str | None) -> str | None:
    """hold_key is held across clicks, drags and scrolls, so it needs one in the same call."""
    if hold_key in (None, ""):
        return None
    for s in specs or []:
        if s.get("kind") in ("drag", "scroll") or (s.get("kind") in HOLD_KEY_POINTER_KINDS and not s.get("hover")):
            return None
    return (
        f"hold_key={hold_key} needs a norm/xy/ids click, a drag/drag_norm, or a scroll in the same "
        "act call (it is held down during those steps); this call has none"
    )


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


ELEVATED_HINT = (
    "the target runs elevated (as administrator); Windows UIPI drops input from a lower-integrity "
    "process, so clicks and keys would silently do nothing. Restart cu-perceive as administrator, "
    "or run the target without elevation"
)


def _target_integrity(out: dict, frame: dict) -> dict | None:
    """Annotate out with target_integrity / self_integrity / elevated_target (window stamps)."""
    if not _has_window(frame):
        return None
    try:
        from .windows import target_integrity_check

        info = target_integrity_check(int(frame["hwnd"]))
    except Exception:
        return None
    for k in ("elevated_target", "target_integrity", "self_integrity"):
        out[k] = info.get(k)
    return info


def _elevated_target_refusal(out: dict, frame: dict) -> bool:
    """Live go: refuse (before any input) when the target's integrity is above ours."""
    info = _target_integrity(out, frame)
    if not info or not info.get("elevated_target"):
        if info and info.get("elevated_target") is None and info.get("integrity_error"):
            out["warnings"] = (out.get("warnings") or []) + [
                f"could not read the target's integrity level ({info['integrity_error']}); if input "
                "has no effect, the target may be elevated"
            ]
        return False
    out["ok"] = False
    out["error"] = (
        f"target window {frame.get('hwnd')} runs at {info.get('target_integrity')} integrity, "
        f"above cu-perceive ({info.get('self_integrity')}); nothing sent"
    )
    _set_refusal(out, "elevated_target", ELEVATED_HINT)
    return True


def _note_replacement(out: dict, frame: dict, hwnd: int) -> dict | None:
    """Fill hwnd_replaced / suggested_hwnd on out when the stamped hwnd was rebuilt."""
    try:
        from .windows import hwnd_replacement

        title = (frame.get("window") or {}).get("title") if isinstance(frame.get("window"), dict) else None
        rep = hwnd_replacement(hwnd, frame.get("identity") or {}, title)
    except Exception:
        return None
    if rep:
        out["hwnd_replaced"] = bool(rep.get("hwnd_replaced"))
        out["replaced_why"] = rep.get("why")
        out["suggested_hwnd"] = rep.get("suggested_hwnd")
        if rep.get("candidates"):
            out["replacement_candidates"] = rep["candidates"]
    return rep


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
                _note_replacement(out, frame, hwnd_i)
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
            _note_replacement(out, frame, hwnd_i)
            return out
        rep = _note_replacement(out, frame, hwnd_i)
        if rep:
            out["ok"] = False
            out["identity_ok"] = False
            if rep.get("hwnd_replaced"):
                out["error"] = (
                    f"hwnd {hwnd_i} was replaced ({rep['why']}); suggested_hwnd="
                    f"{rep.get('suggested_hwnd')} — re-perceive that window"
                )
            else:
                out["error"] = f"hwnd {hwnd_i} is hidden/cloaked; re-perceive"
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

    Mutates out["steps"] (per-step foreground_used = whether that click would use
    SendInput) and sets planned_foreground_click_count. On a dry run nothing is
    sent, so foreground_used / foreground_click_count are false / 0. On --go they
    start at 0/False and count only clicks actually sent (_count_live_click), so a
    refused --go reports 0.
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
    # Dry-run and not-yet-sent --go both report 0. The plan is planned_foreground_click_count.
    out["foreground_used"] = False
    out["foreground_click_count"] = 0


def _count_live_click(out: dict) -> None:
    out["foreground_click_count"] = int(out.get("foreground_click_count") or 0) + 1
    out["foreground_used"] = True


def _window_title(hwnd) -> str | None:
    try:
        from . import _win32

        return _win32.window_title(int(hwnd))
    except Exception:
        return None


def _set_refusal(out: dict, reason: str, hint: str, window: int | dict | None = None) -> None:
    """Machine-readable side of a refusal: reason code, next step, and the window in the way."""
    out["reason"] = reason
    out["hint"] = hint
    if isinstance(window, dict):
        out["blocking_window"] = {"hwnd": window.get("hwnd"), "title": window.get("title")}
        for extra in ("class", "focus_hwnd"):
            if window.get(extra) is not None:
                out["blocking_window"][extra] = window.get(extra)
    elif window:
        out["blocking_window"] = {"hwnd": int(window), "title": _window_title(window)}


HINT_FOCUS = "retry with focus=true (CLI --focus) to bring the stamp window to the front"
HINT_REPERCEIVE = "re-perceive the window and act on the new stamp"


def _identity_abort(out: dict, frame: dict, steps: list[dict], n: int) -> bool:
    """Re-check stamp identity right before live step n. True = aborted (out updated)."""
    err = stamp_identity_error(frame)
    if err is None:
        return False
    out["ok"] = False
    out["error"] = f"window identity check before step {n} failed: {err}"
    out["identity_abort_step"] = n
    _set_refusal(out, "identity_changed", HINT_REPERCEIVE)
    for s in steps:
        s.pop("_uia_control", None)
        s.pop("_uia_action", None)
    return True


KEYBOARD_VIAS = ("type", "key")
FOCUSING_VIAS = ("id", "xy", "norm", "drag")
FOCUS_SETTLE_S = 0.15


def unguarded_keyboard_steps(steps: list[dict]) -> list[int]:
    """Indices of type/key steps with no click or drag earlier in the chain.

    Keys go to whatever window has keyboard focus; only an earlier click/drag in the
    same chain puts that focus on the stamp window.
    """
    found: list[int] = []
    clicked = False
    for n, step in enumerate(steps):
        via = step.get("via")
        if via in FOCUSING_VIAS:
            clicked = True
        elif via in KEYBOARD_VIAS and not clicked:
            found.append(n)
    return found


def _has_window(frame: dict) -> bool:
    return bool(frame.get("hwnd")) and _needs_window_checks(frame)


KEYBOARD_SETTLE_TRIES = 10
KEYBOARD_SETTLE_S = 0.05


def _blocking_hint(fg, title) -> str:
    """Point the agent at the window in front: it may be an untitled popup missing from windows()."""
    if not fg:
        return ""
    name = f"'{title}'" if title else "an untitled window (it may not appear in windows())"
    return (f". The window in front is {name}, hwnd {int(fg)}: perceive_window(hwnd={int(fg)}) to see it "
            f"and act on that stamp to dismiss it (suggested_hwnd)")


def _foreground_elevated(hwnd) -> bool:
    """True when the foreground window runs above our integrity level (UIPI drops all our input)."""
    if not hwnd:
        return False
    try:
        from .windows import target_integrity_check

        return target_integrity_check(int(hwnd)).get("elevated_target") is True
    except Exception:
        return False


def _bring_forward(frame: dict, out: dict) -> str | None:
    """--focus: raise the stamp window, then re-check its identity. None = done."""
    hwnd = int(frame["hwnd"])
    if frame.get("_focus_verified") == hwnd:
        return None  # already raised and verified earlier in this act call
    from .windows import focus_and_verify

    fv = focus_and_verify(hwnd, retries=1)
    out["foreground_ok"] = fv.get("foreground_ok")
    out["focus_attempts"] = fv.get("focus_attempts")
    if fv.get("foreground_ok") is False:
        out["focused_window"] = False
        out["foreground_hwnd"] = fv.get("foreground_hwnd")
        ftitle = fv.get("foreground_title")
        if ftitle:
            out["foreground_title"] = ftitle
        if fv.get("foreground_hwnd"):
            out["blocking_window"] = {"hwnd": fv.get("foreground_hwnd"), "title": ftitle}
        hint = ((f"'{ftitle}' kept the foreground; " if ftitle else "")
                + "click the stamp window once by hand (or close what is in front), then retry")
        fg_elev = _foreground_elevated(fv.get("foreground_hwnd"))
        if fg_elev:
            out["foreground_elevated"] = True
            hint = ((f"'{ftitle}' " if ftitle else "the foreground window ")
                    + "runs elevated (as administrator): while it is in front Windows drops ALL synthetic "
                    "input from cu-perceive, whatever the target. Ask the user to close or minimize it "
                    "(cu-perceive cannot), or use method=uia on this window meanwhile")
        if not fg_elev and fv.get("foreground_hwnd"):
            hint += _blocking_hint(fv.get("foreground_hwnd"), ftitle)
            out["suggested_hwnd"] = int(fv.get("foreground_hwnd"))
        _set_refusal(out, "focus_failed", hint)
        return (
            f"focus=true but stamp window {hwnd} is not foreground after "
            f"{fv.get('focus_attempts')} attempts (foreground={fv.get('foreground_hwnd')}); nothing sent"
        )
    out["focused_window"] = True
    time.sleep(FOCUS_SETTLE_S)
    err = stamp_identity_error(frame)
    if err:
        _set_refusal(out, "identity_changed", HINT_REPERCEIVE)
        return f"window identity check after --focus failed: {err}"
    frame["_focus_verified"] = hwnd
    return None


def _foreground_ok(target: int, *, settle: bool) -> tuple[bool, int]:
    """foreground_matches(target); with settle, give a just-sent click time to activate."""
    from .windows import foreground_matches

    tries = KEYBOARD_SETTLE_TRIES if settle else 1
    ok, fg = False, 0
    for i in range(tries):
        ok, fg = foreground_matches(int(target))
        if ok:
            break
        if i + 1 < tries:
            time.sleep(KEYBOARD_SETTLE_S)
    return ok, fg


def _keyboard_target_error(
    frame: dict, *, focus: bool, out: dict, clicked_window: int | None = None, settle: bool = False,
    hold: bool = False,
) -> str | None:
    """None when keys sent now would reach the intended window. Fails closed.

    The intended window is the stamp window, or for a screen-space stamp the window
    an earlier click in this chain landed on (clicked_window). With focus (window
    stamps only), brings the stamp window forward, then re-checks identity and foreground.
    """
    frame = frame or {}
    window_stamp = _has_window(frame)
    if window_stamp:
        target, label = int(frame["hwnd"]), f"stamp window {frame['hwnd']}"
    elif clicked_window:
        target, label = int(clicked_window), f"clicked window {clicked_window}"
    else:
        _set_refusal(
            out, "no_window_stamp",
            "add a norm/xy/ids click on the target field first, or perceive the window "
            "(hwnd/title/exe) and pass that stamp",
        )
        return (
            "keyboard step without a preceding click needs a window stamp: keys go to the "
            "foreground window and this stamp has no window to check against; add a click on "
            "the target field, or perceive the window (--hwnd/--title/--exe) and pass --stamp"
        )
    try:
        ok, fg = _foreground_ok(target, settle=settle)
    except Exception as e:
        _set_refusal(out, "foreground_unreadable", "retry; if it keeps failing, re-perceive")
        return f"could not read the foreground window ({e}); keyboard step refused"
    if ok:
        focus_err = _focus_target_error(out, target, label)
        if focus_err:
            return focus_err
        return None
    fg_title = _window_title(fg) if fg else None
    if not focus or not window_stamp:
        if not window_stamp:
            hint = "re-run once that window is in front"
        elif hold:
            hint = "pass --focus (MCP focus=true) to bring the stamp window forward before the modifier goes down"
        elif settle:
            hint = (
                "the click did not bring it to the front (another window kept or took focus); pass "
                "--focus (MCP focus=true) to bring the stamp window forward first"
            )
        else:
            hint = (
                "add a click on the target field, or pass --focus (MCP focus=true) to bring the "
                "stamp window forward first"
            )
        _set_refusal(
            out, "not_foreground",
            HINT_FOCUS if window_stamp else "bring the clicked window to the front, then retry",
            {"hwnd": fg, "title": fg_title} if fg else None,
        )
        return (
            f"{label} is not in the foreground (foreground hwnd {fg} {fg_title!r}), so keys would go "
            f"to another window; {hint}"
        )
    err = _bring_forward(frame, out)
    if err:
        return err + "; keyboard step refused"
    try:
        ok, fg = _foreground_ok(target, settle=False)
    except Exception as e:
        _set_refusal(out, "foreground_unreadable", "retry; if it keeps failing, re-perceive")
        return f"could not read the foreground window after --focus ({e}); keyboard step refused"
    if not ok:
        fg_title = _window_title(fg) if fg else None
        _set_refusal(
            out, "not_foreground_after_focus",
            "Windows kept another window in front; click the stamp window once by hand (or close "
            f"{fg_title or 'the window in front'!s}), then retry" + _blocking_hint(fg, fg_title),
            {"hwnd": fg, "title": fg_title} if fg else None,
        )
        if fg:
            out["suggested_hwnd"] = int(fg)
        return (
            f"{label} is still not in the foreground after --focus (foreground hwnd {fg} "
            f"{fg_title!r}); keyboard step refused"
        )
    focus_err = _focus_target_error(out, target, label)
    if focus_err:
        return focus_err
    return None


def _focus_target_error(out: dict, target: int, label: str) -> str | None:
    """None when keyboard focus is inside the target, or when it cannot be read.

    Foreground alone is not enough: replace can clear a field via UIA while
    SendInput delivers the text to a different focused window, and the act
    still looked successful.
    """
    try:
        from .windows import focus_matches

        ok, focused = focus_matches(int(target))
    except Exception:
        return None
    if ok is not False:
        return None
    dialog = _focus_owner_window(focused, int(target))
    if dialog is not None:
        title = dialog.get("title")
        named = f"{title!r} (hwnd {dialog['hwnd']})" if title else f"hwnd {dialog['hwnd']}"
        _set_refusal(
            out, "focus_outside",
            f"close or dismiss the dialog {named} first (it owns keyboard focus), "
            "then re-perceive and retry",
            dialog,
        )
        return (
            f"{label} is in the foreground but keyboard focus is in dialog {named}, "
            "not inside it; typing refused"
        )
    _set_refusal(
        out, "focus_outside",
        "click the target field so it has keyboard focus, then retry",
        {"hwnd": focused, "title": _window_title(focused)} if focused else None,
    )
    return (
        f"{label} is in the foreground but keyboard focus is hwnd {focused}, "
        "not inside it; typing refused"
    )


def _focus_owner_window(focused: int, target: int) -> dict | None:
    """The top-level window (GA_ROOT) that holds keyboard focus, when it is not target.

    focus_matches reports the focused child (a button or edit inside a modal); the
    caller needs the dialog itself to say what to close.
    """
    if not focused:
        return None
    try:
        from . import _win32

        root = int(_win32.root_window(int(focused)) or int(focused))
    except Exception:
        return None
    if not root or root == int(target):
        return None
    info: dict = {"hwnd": root, "title": _window_title(root) or None, "focus_hwnd": int(focused)}
    try:
        info["class"] = _win32.class_name(root) or None
    except Exception:
        pass
    return info


def _clean_readback(actual, expected) -> str | None:
    """A WinForms/grid provider reports an empty edit as the literal ``(null)``.

    Treat it as empty so typed_readback and typed_mismatch errors never show it,
    unless that literal is exactly what was typed.
    """
    if actual is None:
        return None
    text = str(actual)
    if text.strip().lower() == "(null)" and str(expected or "").strip().lower() != "(null)":
        return ""
    return text


def _note_typed_readback(out: dict, step: dict, expected: str, target_hwnd: int = 0) -> bool:
    """Set typed_verified from the focused control inside the target window.

    true: the value matches. false: it does not (typed_mismatch, never ok).
    null: the value could not be read, focus is not a live window (unit-test
    hwnds), the edit is a password, or the focused element belongs to another
    top-level window.
    """
    if target_hwnd:
        try:
            from .windows import focus_matches

            inside, _focused = focus_matches(int(target_hwnd))
        except Exception:
            inside = None
        if inside is not True:
            step["typed_verified"] = None
            out["typed_verified"] = None
            return True
    try:
        from .input import focused_readback, typed_matches

        info = focused_readback(int(target_hwnd or 0))
    except Exception:
        info = None
    if not isinstance(info, dict):
        step["typed_verified"] = None
        out["typed_verified"] = None
        return True
    if info.get("password"):
        step["typed_verified"] = None
        out["typed_verified"] = None
        note = "password field; read-back skipped"
        step["typed_note"] = note
        out["typed_note"] = note
        return True
    if info.get("in_target") is False:
        step["typed_verified"] = None
        out["typed_verified"] = None
        note = "focused control is not in the target window; read-back skipped"
        step["typed_note"] = note
        out["typed_note"] = note
        return True
    actual = _clean_readback(info.get("value"), expected)
    if (
        info.get("value") is not None and not str(info.get("value")).strip()
        and str(expected or "").strip() and info.get("native") is not True
    ):
        # Monaco-like editors expose no value at all: an empty read-back after typing
        # proves nothing. Not a mismatch; a different non-empty value still is.
        step["typed_verified"] = None
        out["typed_verified"] = None
        step["typed_readback"] = "unavailable"
        note = ("readback_unavailable: the focused control reads back empty (no ValuePattern "
                "text, e.g. a Monaco/CodeMirror editor); typed text not verified - perceive to check")
        step["typed_note"] = note
        out["typed_note"] = note
        out["warnings"] = (out.get("warnings") or []) + [note]
        return True
    try:
        match = typed_matches(expected, actual)
    except Exception:
        actual, match = None, None
    step["typed_verified"] = match
    out["typed_verified"] = match
    if actual is not None:
        step["typed_readback"] = actual[:120]
    if match is False:
        shown = actual if actual is not None else ""
        out["ok"] = False
        out["reason"] = "typed_mismatch"
        out["error"] = f"focused control value is {shown!r} after typing {expected!r}"
        return False
    return True


def _keyboard_refuse(out: dict, n: int, err: str) -> dict:
    out["ok"] = False
    out["error"] = f"keyboard step {n} refused: {err}"
    out["keyboard_refused_step"] = n
    return out


def _hold_refuse(out: dict, n: int, hold: str, err: str) -> dict:
    """The --hold-key modifier goes to the foreground window, like any key."""
    out["ok"] = False
    out["error"] = f"--hold-key {hold} refused before step {n} (a held modifier goes to the foreground window): {err}"
    out["hold_key_refused_step"] = n
    return out


def step_points(step: dict) -> list[tuple[str, list]]:
    """Screen points where a pointer step's mouse events land: (label, [x, y]).

    Clicks: the click point. Drags: start and end. Scroll: the hover point, or [] when
    the wheel goes to the current cursor (resolved live). Hover: the hover point.
    Type/key/wait: none.
    """
    via = step.get("via")
    if via in ("id", "xy", "norm") and step.get("screen"):
        return [("click", list(step["screen"]))]
    if via == "drag" and step.get("screen_points"):
        pts = step["screen_points"]
        return [("drag start", list(pts[0])), ("drag end", list(pts[-1]))]
    if via == "scroll" and step.get("screen"):
        return [("scroll", list(step["screen"]))]
    if via == "hover" and step.get("screen"):
        # PR16: a hover over a covered point hovers the covering window; check it like a click.
        return [("hover", list(step["screen"]))]
    return []


POINTER_VIAS = ("id", "xy", "norm", "drag", "scroll", "hover")


def _point_target_error(frame: dict, step: dict, *, focus: bool, out: dict) -> tuple[str | None, dict | None]:
    """(error, window_info) for a live pointer step on a window stamp; (None, None) = all
    of its points reach the stamp window (or a window it owns). Fails closed.

    Screen-space stamps have no window to compare with and pass.
    """
    frame = frame or {}
    if not _has_window(frame):
        return None, None
    hwnd = int(frame["hwnd"])
    from .windows import point_owner

    def check() -> tuple[str | None, dict | None, bool]:
        points = step_points(step)
        if step.get("via") == "scroll" and not points:
            from . import _win32

            points = [("scroll (cursor)", list(_win32.cursor_pos()))]
        for label, (x, y) in points:
            ok, info = point_owner(hwnd, int(x), int(y))
            if not ok:
                return (
                    f"{label} point ({int(x)},{int(y)}) is covered by hwnd {info.get('hwnd')} "
                    f"{info.get('title')!r}, not stamp window {hwnd}",
                    {**info, "point": [int(x), int(y)], "what": label},
                    True,
                )
        return None, None, False

    try:
        err, info, covered = check()
    except Exception as e:
        _set_refusal(out, "point_unreadable", "retry; if it keeps failing, re-perceive")
        return f"could not check which window is under the target point ({e})", None
    if not covered:
        return None, None
    if not focus:
        _set_refusal(out, "point_covered", HINT_FOCUS + ", or pick a point that is not covered"
                     + _blocking_hint((info or {}).get("hwnd"), (info or {}).get("title")), info)
        if (info or {}).get("hwnd"):
            out["suggested_hwnd"] = int(info["hwnd"])
        return err + "; bring that window to the front (--focus / MCP focus=true) or re-perceive", info
    ferr = _bring_forward(frame, out)
    if ferr:
        return ferr, info
    try:
        err2, info2, covered = check()
    except Exception as e:
        _set_refusal(out, "point_unreadable", "retry; if it keeps failing, re-perceive")
        return f"could not check which window is under the target point after --focus ({e})", None
    if covered:
        _set_refusal(
            out, "point_covered_after_focus",
            f"{(info2 or {}).get('title') or 'the covering window'} stays on top (always-on-top?); "
            "move or close it, then retry" + _blocking_hint((info2 or {}).get("hwnd"), (info2 or {}).get("title")),
            info2,
        )
        if (info2 or {}).get("hwnd"):
            out["suggested_hwnd"] = int(info2["hwnd"])
        return err2 + " even after --focus", info2
    return None, None


def _pointer_refuse(out: dict, n: int, err: str, info: dict | None) -> dict:
    out["ok"] = False
    out["error"] = f"pointer step {n} refused: {err}"
    out["pointer_refused_step"] = n
    if info:
        out["point_window"] = info
    return out


def _clicked_window(step: dict) -> int | None:
    """Top-level window under a click on a screen-space stamp (keyboard target later)."""
    pts = step_points(step)
    if not pts:
        return None
    try:
        from .windows import window_at_point

        return window_at_point(int(pts[0][1][0]), int(pts[0][1][1])) or None
    except Exception:
        return None


def occluded_points(frame: dict, steps: list[dict], *, method: str) -> list[dict] | None:
    """Planned pointer points that are currently covered by another window (dry run).

    None when it can't be checked (off Windows, screen-space stamp, post/uia).
    """
    frame = frame or {}
    if not _has_window(frame) or method in ("post", "uia"):
        return None
    from .windows import point_owner

    found: list[dict] = []
    try:
        for n, step in enumerate(steps):
            if method == "auto" and (step.get("chosen_method") or "input") != "input":
                continue
            for label, (x, y) in step_points(step):
                ok, info = point_owner(int(frame["hwnd"]), int(x), int(y))
                if not ok:
                    found.append({"step": n, "what": label, "point": [int(x), int(y)], **info})
    except Exception:
        return None
    return found


def _implicit_stamp_error(frame: dict, stamp_source: str | None) -> str | None:
    """--go on the latest stamp (no --stamp/--json) needs a window stamp with identity."""
    if stamp_source != "latest":
        return None
    frame = frame or {}
    if _has_window(frame) and frame.get("identity"):
        return None
    stamp = frame.get("stamp") or "<stamp>"
    return (
        f"act without --stamp picked the latest stamp {stamp}, which has no window identity "
        f"(screen-space or older stamp); pass --stamp {stamp} (MCP stamp=...) or --json to act on it"
    )


def diff_new_windows(before: list[dict] | None, after: list[dict] | None) -> list[dict]:
    """Top-level windows present in after but not before: {hwnd, title, class}."""
    if before is None or after is None:
        return []
    seen: set[int] = set()
    for w in before:
        try:
            seen.add(int(w.get("hwnd") or 0))
        except (TypeError, ValueError):
            continue
    out: list[dict] = []
    for w in after:
        try:
            hwnd = int(w.get("hwnd") or 0)
        except (TypeError, ValueError):
            continue
        if not hwnd or hwnd in seen:
            continue
        out.append({"hwnd": hwnd, "title": w.get("title") or "", "class": w.get("class") or ""})
    return out


def _snapshot_windows() -> list[dict] | None:
    try:
        from .windows import top_level_brief

        return top_level_brief()
    except Exception:
        return None


NEW_WINDOW_WAIT_S = 1.2  # slow apps (Electron cold start) open dialogs after ~1 s
NEW_WINDOW_RECHECK_S = 0.3  # one more look after the wait ran out empty
NEW_WINDOW_CHAIN_STEP_S = 0.25  # non-final chain steps (field clicks) keep the short wait
NEW_WINDOW_POLL_S = 0.05
_WINDOW_OPEN_KEYS = frozenset({"enter", "return", "esc", "escape"})
_CTRL_EDIT_KEYS = frozenset("acxzy")


def _key_may_open(spec: str) -> bool:
    """Enter, Esc, an F-key, an Alt combo, or Ctrl(+Shift)+letter/digit (ctrl+p print, ctrl+o open,
    ctrl+shift+s save-as) can open or close a top-level window."""
    parts = [p for p in str(spec).lower().split("+") if p]
    if any(p in _WINDOW_OPEN_KEYS for p in parts):
        return True
    if any(len(p) > 1 and p[0] == "f" and p[1:].isdigit() for p in parts):
        return True
    if "alt" in parts and len(parts) >= 2:
        return True
    if "ctrl" in parts or "control" in parts:
        last = parts[-1]
        if len(parts) < 2 or len(last) != 1 or not last.isalnum():
            return False
        # plain editing chords (select all, copy, cut, undo, redo) never open a window
        return "shift" in parts or last not in _CTRL_EDIT_KEYS
    return False


def chain_may_open_window(steps) -> bool:
    """True when a planned step can create a top-level window.

    Clicks, Enter, Esc, F-keys, Alt combos, and Ctrl(+Shift)+letter wait for ``new_windows``.
    Typing, scrolling, and dragging do not, unless new_window_wait_ms is passed explicitly.
    """
    for step in steps or []:
        if step.get("via") in ("id", "xy", "norm"):
            return True
        if step.get("via") == "key" and _key_may_open(step.get("key") or ""):
            return True
    return False


def new_window_wait_s() -> float:
    """How long to poll for a dialog that appears just after input. 0 disables the wait.

    ``CU_PERCEIVE_NEW_WINDOW_WAIT_MS`` (default 1200; MCP act new_window_wait_ms= per call).
    The wait runs only after a step that can open a window, and it returns as soon as one
    appears. An empty wait is followed by one recheck NEW_WINDOW_RECHECK_S later.
    """
    raw = os.environ.get("CU_PERCEIVE_NEW_WINDOW_WAIT_MS", "").strip().lower()
    if raw in ("0", "off", "none"):
        return 0.0
    if not raw:
        return NEW_WINDOW_WAIT_S
    try:
        ms = float(raw)
    except ValueError:
        return NEW_WINDOW_WAIT_S
    return max(0.0, ms / 1000.0)


def windows_snapshot_changed(before: list[dict] | None, after: list[dict] | None) -> bool | None:
    """Any top-level window appeared, disappeared, or changed title. None = unknown."""
    if before is None or after is None:
        return None
    key = lambda rows: {(int(w.get("hwnd") or 0), w.get("title") or "") for w in rows}  # noqa: E731
    return key(before) != key(after)


def _poll_new_windows(
    before: list[dict] | None, timeout: float | None = None, recheck: float = 0.0,
) -> tuple[list[dict], list[dict] | None]:
    """Snapshot, then poll until a new top-level window shows up or the wait elapses.

    ``timeout`` 0 takes one snapshot and does not sleep. A new window ends the wait early.
    ``recheck`` > 0: when the wait ends empty, look once more after that delay.
    """
    if timeout is None:
        timeout = new_window_wait_s()
    latest = _snapshot_windows()
    found = diff_new_windows(before, latest) if before is not None and latest is not None else []
    waited = 0.0
    while not found and waited < timeout:
        step = min(NEW_WINDOW_POLL_S, timeout - waited)
        time.sleep(step)
        waited += NEW_WINDOW_POLL_S
        latest = _snapshot_windows()
        if before is None or latest is None:
            break
        found = diff_new_windows(before, latest)
    if not found and recheck > 0 and before is not None and latest is not None:
        time.sleep(recheck)
        again = _snapshot_windows()
        if again is not None:
            latest = again
            found = diff_new_windows(before, latest)
    return found, latest


def _unlisted_popup(out: dict, latest: list[dict] | None, stamp_hwnd: int | None) -> None:
    """after.unlisted_foreground / popup_hwnd: a foreground or covering window that windows() does not
    list (Qt / CEF popups, untitled layers), so popups that appear or vanish are visible to the caller."""
    if latest is None:
        return
    listed = {int(w.get("hwnd") or 0) for w in latest}
    after = out.get("after") or {}
    try:
        from . import _win32
        from .windows import window_at_point
    except Exception:
        return
    fg = int(after.get("foreground_hwnd") or 0)
    cands: list[tuple[str, int]] = []
    if fg and fg not in listed:
        after["unlisted_foreground"] = True
        cands.append(("foreground", fg))
    elif fg:
        after["unlisted_foreground"] = False
    if stamp_hwnd:
        try:
            l, t_, r, b = _win32.window_rect(int(stamp_hwnd))
            at = window_at_point((l + r) // 2, (t_ + b) // 2)
            if at and at != int(stamp_hwnd):
                cands.append(("covers_stamp_center", int(at)))
        except Exception:
            pass
    for key in ("suggested_hwnd",):
        h = int(out.get(key) or 0)
        if h:
            cands.append(("blocking", h))
    for why, h in cands:
        if h in listed or h == int(stamp_hwnd or 0):
            continue
        try:
            title = _win32.window_title(h) or ""
            cls = _win32.class_name(h) or ""
        except Exception:
            title, cls = "", ""
        after["popup_hwnd"] = h
        after["popup"] = {"hwnd": h, "title": title, "class": cls, "why": why}
        break
    if after:
        out["after"] = after


def _attach_new_windows(out: dict, before: list[dict] | None, wait_s: float | None = None,
                        cap_s: float | None = None, stamp_hwnd: int | None = None) -> None:
    # An explicit wait always polls, whatever the action; the default only follows actions that can open one.
    may_open = chain_may_open_window(out.get("steps"))
    if wait_s is not None:
        timeout, why = max(0.0, float(wait_s)), "explicit"
    elif may_open and cap_s is not None:
        timeout, why = min(new_window_wait_s(), max(0.0, float(cap_s))), "chain_step"
    elif may_open:
        timeout, why = new_window_wait_s(), "action"
    else:
        timeout, why = 0.0, "action_cannot_open_window"
    recheck = NEW_WINDOW_RECHECK_S if timeout > 0 else 0.0
    found, latest = _poll_new_windows(before, timeout, recheck)
    out["new_windows"] = found
    out["new_window_wait_ms"] = int(round(timeout * 1000))
    out["new_window_wait_reason"] = why
    changed = windows_snapshot_changed(before, latest)
    if changed is not None:
        out["windows_snapshot_changed"] = changed
    try:
        from .windows import foreground_brief

        fore = foreground_brief()
    except Exception:
        fore = None
    if fore:
        out["after"] = fore
    try:
        _unlisted_popup(out, latest, stamp_hwnd)
    except Exception:
        pass


_IME_CHAR_KEYS = {
    "space", "comma", "period", "minus", "equals", "slash", "backslash", "semicolon", "quote",
    "backquote", "bracketleft", "bracketright",
}


def ime_matters(steps: list[dict]) -> bool:
    """True if an IME could swallow this act's input: typed text or a bare character key.

    Hotkeys (enter, esc, tab, arrows, f1-f24, ctrl/alt/win combos) are virtual keys an IME
    passes through, so a key-only act of those does not need ime=off.
    """
    for st in steps or []:
        kind = st.get("via") or st.get("kind")
        if kind == "type":
            return True
        if kind != "key":
            continue
        parts = [x.strip().lower() for x in str(st.get("key") or "").split("+") if x.strip()]
        if not parts or any(m in ("ctrl", "alt", "win") for m in parts[:-1]):
            continue
        last = parts[-1]
        if len(last) == 1 or last in _IME_CHAR_KEYS or (last.startswith("num") and last[3:].isdigit()):
            return True
    return False


def _note_ime(out: dict, frame: dict, steps: list[dict], ime_mode: str | None) -> None:
    """Record the target thread's layout. Warn when an IME would swallow keys."""
    hwnd = (frame or {}).get("hwnd")
    if not hwnd:
        if ime_mode in ("off", "strict"):
            out["warnings"] = (out.get("warnings") or []) + [
                'ime=off needs a window stamp; this stamp has no hwnd, so the layout was not changed'
            ]
        return
    from .ime import probe_ime

    info = probe_ime(int(hwnd))
    if not info.get("ok"):
        return
    out["ime"] = {k: info.get(k) for k in ("hkl_hex", "lang_id", "ime_open", "ime_conversion", "ime_active",
                                           "thread_id", "top_thread_id", "focus_hwnd")}
    keyboard = any(s.get("via") in ("type", "key") for s in steps)
    # ime=off handles the layout itself. Warning ime_active after that is a false alarm.
    if info.get("ime_active") and keyboard and ime_mode not in ("off", "strict"):
        out["warnings"] = (out.get("warnings") or []) + [
            f"ime_active: target thread layout {info.get('hkl_hex')} (lang {info.get('lang_id')}, "
            f"ime_open={info.get('ime_open')}). An IME can swallow keys while each step still reports "
            'tapped=true. Pass ime="off" to switch that window to an installed Latin layout for this act '
            "and restore it after."
        ]
    if ime_mode in ("off", "strict") and out.get("dry_run") and ime_matters(steps):
        out["warnings"] = (out.get("warnings") or []) + [
            "ime=off will switch this window to an English layout and close its IME for the act, then restore both"
        ]


def _suspend_ime_for_act(out: dict, frame: dict, steps: list[dict], ime_mode: str | None) -> dict | None:
    """Switch the stamp window to English. Returns an error dict to return, or None to continue."""
    if ime_mode not in ("off", "strict") or out.get("dry_run"):
        return None
    keys_only = not ime_matters(steps)
    if keys_only and not any((s.get("via") or s.get("kind")) in KEYBOARD_VIAS for s in steps):
        out["ime_skipped"] = "no_keyboard_step"
        out["notes"] = (out.get("notes") or []) + ["ime=off skipped: this act sends no keys"]
        return None
    hwnd = (frame or {}).get("hwnd")
    if not hwnd:
        if any(s.get("via") in KEYBOARD_VIAS for s in steps):
            out["ok"] = False
            out["reason"] = "ime_failed"
            out["error"] = "ime=off needs a window stamp so the target window's layout can be switched"
            return out
        return None
    from .ime import ImeError, suspend_ime

    try:
        state = suspend_ime(int(hwnd), mode=ime_mode)
    except ImeError as e:
        state = {"verified": False, "error": str(e), "switch_requested": False, "closed": False}
    # restore runs only for something that actually changed (a verified switch or an IME close)
    _ime_restore.state = state if (state.get("layout_switched") or state.get("closed")) else None
    out["ime_suspended"] = bool(state.get("layout_switched") or state.get("closed") or state.get("already_latin"))
    out["ime_layout_switched"] = bool(state.get("layout_switched"))
    out["ime_closed"] = bool(state.get("closed"))
    out["ime_verified"] = bool(state.get("verified"))
    if state.get("already_latin"):
        out["notes"] = (out.get("notes") or []) + [f"ime={ime_mode}: the window already uses a non-IME layout; nothing switched"]
    after = state.get("after") or {}
    if state.get("hkl") is not None or after:
        def _h(v):
            return f"0x{int(v) & 0xFFFFFFFF:08x}" if v else None

        out["ime"] = {
            "thread_id": after.get("thread_id") or state.get("thread_id"),
            "top_thread_id": after.get("top_thread_id"),
            "focus_hwnd": state.get("focus_hwnd"),
            "hkl_hex": state.get("after_hkl_hex") or after.get("hkl_hex"),
            "hkl_before_hex": _h(state.get("hkl")),
            "requested_hex": _h(state.get("english_hkl")) if state.get("switch_requested") else None,
            "tried": state.get("tried") or [],
            "ime_open_before": state.get("ime_open"), "ime_open_after": after.get("ime_open"),
            "ime_conversion_before": state.get("ime_conversion"), "ime_conversion_after": after.get("ime_conversion"),
            "verify_ms": state.get("verify_ms"), "verify_budget_ms": state.get("verify_budget_ms"),
        }
    if state.get("verified"):
        if state.get("closed_only"):
            out["warnings"] = (out.get("warnings") or []) + [
                f"ime={ime_mode}: no layout switch stuck; the IME was closed instead (ime_closed=true), "
                "so keys go through as ASCII"]
        return None
    err = state.get("error") or f'ime="{ime_mode}" could not verify the layout switch'
    if ime_mode == "strict":
        out["ok"] = False
        out["reason"] = "ime_failed"
        out["error"] = err
        return out
    what = "keys sent as-is" if keys_only else "text is sent anyway and may be composed by the IME; check typed_verified, or pass ime=\"strict\" to abort instead"
    out["ime_unverified"] = True
    out["warnings"] = (out.get("warnings") or []) + [f"{err}; {what}"]
    return None


def run_chain(frame: dict, specs: list[dict], *, dry_run: bool = True, double: bool = False, gap: float = 0.12, hold_key: str | None = None, method: str | None = None, max_age_sec: float | None = None, strict_stamp: bool = False, stamp_json_path: str | Path | None = None, origin_slop_px: int = 8, focus: bool = False, stamp_source: str | None = None, ime: str | None = None, new_window_wait_s: float | None = None, new_window_wait_cap_s: float | None = None) -> dict:
    """Plan specs against frame; dry_run=False sends input.

    stamp_source: "explicit" (--stamp/--json), "latest" (newest stamp picked implicitly)
    or None.

    Live input (method=input, and auto steps that chose input) on a window stamp checks,
    before each click / drag start and end / scroll, that the window under the point is
    the stamp window or one it owns, and before each type/key step that the stamp window
    is in the foreground. Otherwise the step is refused. focus: bring the stamp window
    forward instead, re-check identity, then re-check the point or foreground.

    ime="off" switches the stamp window to English (and closes its IME) for a live act,
    then restores it. Concurrent dry_run=False calls take LIVE_ACT_LOCK for the whole act.
    """
    from .ime import normalize_ime

    try:
        ime_mode = normalize_ime(ime)
    except ValueError as e:
        return {
            "ok": False,
            "dry_run": bool(dry_run),
            "error": str(e),
            "reason": "bad_args",
            "steps": [],
            "hint": 'omit ime, or pass ime="off" to switch the target window to English for this act',
        }
    if not dry_run and not getattr(_live_lock_state, "held", False):
        with LIVE_ACT_LOCK:
            _live_lock_state.held = True
            _ime_restore.state = None
            before = _snapshot_windows()
            out = None
            try:
                out = run_chain(
                    frame, specs, dry_run=dry_run, double=double, gap=gap, hold_key=hold_key,
                    method=method, max_age_sec=max_age_sec, strict_stamp=strict_stamp,
                    stamp_json_path=stamp_json_path, origin_slop_px=origin_slop_px,
                    focus=focus, stamp_source=stamp_source, ime=ime,
                )
            finally:
                state = getattr(_ime_restore, "state", None)
                _ime_restore.state = None
                _live_lock_state.held = False
                rest = None
                if state is not None:
                    from .ime import restore_ime

                    try:
                        rest = restore_ime(state)
                    except Exception as e:
                        rest = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                if rest is not None and isinstance(out, dict) and rest.get("changed", True):
                    out["ime_restored"] = bool(rest.get("restored"))
                    if not rest.get("ok"):
                        out["warnings"] = (out.get("warnings") or []) + [
                            f"ime restore failed: {rest.get('error') or 'unknown'}"
                        ]
                if isinstance(out, dict):
                    _attach_new_windows(out, before, new_window_wait_s, new_window_wait_cap_s,
                                        stamp_hwnd=(frame or {}).get("hwnd"))
            return out if isinstance(out, dict) else {
                "ok": False, "dry_run": False, "error": "act returned nothing", "steps": [],
            }
    base = {
        "ok": False,
        "dry_run": bool(dry_run),
        "stamp": (frame or {}).get("stamp"),
        "hwnd": (frame or {}).get("hwnd"),
        "coord_space": (frame or {}).get("coord_space"),
        "origin": (frame or {}).get("origin"),
        "steps": [],
    }
    hold_err = hold_key_scope_error(specs, hold_key)
    if hold_err:
        return {**base, "error": hold_err, "reason": "hold_key_needs_pointer_step", "hint": HOLD_KEY_HINT}
    if not specs:
        return {
            **base,
            "error": (
                "no actionable steps: pass flat fields norm/xy/ids/text/key/drag/drag_norm/scroll "
                '(e.g. norm="935,69"), or on MCP act a steps=[{...}] chain whose steps carry those fields.'
            ),
            "reason": "no_steps",
            "hint": 'pass e.g. norm="500,500", scroll="down:3", text="hi", or key="enter"',
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
    from .keys import UnknownKeyError, key_names

    try:
        steps = plan_steps(frame or {}, specs)
    except UnknownKeyError as e:
        return {
            **base,
            "method": method_n,
            "error": str(e),
            "reason": "unknown_key",
            "hint": "use one of valid_keys, e.g. key=\"pagedown\" or key=\"ctrl+end\"",
            "valid_keys": key_names(),
        }
    except BadIdsError as e:
        return {
            **base,
            "method": method_n,
            "error": str(e),
            "reason": "bad_ids",
            "hint": e.hint(),
            "item_ids": e.item_ids[:50],
            "uia_ids": e.uia_ids[:50],
        }
    except UiaTargetError as e:
        return {
            **base,
            "method": method_n,
            "error": str(e),
            "reason": "uia_not_found",
            "hint": "perceive with uia=true and pass a name from uia_nodes, \"Name@ControlType\" or \"#id\"; or click by norm",
        }
    except ValueError as e:
        return {**base, "method": method_n, "error": str(e)}
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
    if stamp_source:
        out["stamp_source"] = stamp_source
    if hold:
        out["hold_key"] = hold
    if uia_missing and method_n == "auto":
        out["warnings"] = [
            "uia extra not installed, so method=auto cannot probe UIA and uses input; install: "
            + extras.install_command("uia")
        ]
    for s in specs:
        if s.get("kind") == "scroll" and s.get("bare_positive"):
            n = int(s["notches"])
            out["warnings"] = (out.get("warnings") or []) + [
                f"scroll={n} is a bare positive number, which scrolls UP (toward the top); "
                f'use scroll="down:{n}" to scroll down, or "up:{n}" to make up explicit'
            ]
        if s.get("box"):
            k = s["kind"]
            out["warnings"] = (out.get("warnings") or []) + [
                f"{k}={','.join(map(str, s['box']))} has 4 numbers, read as a left,top,right,bottom box "
                f"(the format of item / uia_node norm); clicking its center {k}=\"{s[k][0]},{s[k][1]}\""
            ]
        if s.get("kind") == "scroll" and s.get("clamped_from") is not None:
            out["scroll_clamped"] = True
            out["warnings"] = (out.get("warnings") or []) + [
                f"scroll asked for {s['clamped_from']} wheel notches; capped at {s['notches']} "
                f"(max {MAX_SCROLL_NOTCHES}). Scroll counts are wheel notches (~3 lines each), "
                "not pixels; repeat the call to scroll further"
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
    age = stamp_check.get("age_sec")
    if not stamp_bad and max_age_sec is None and age is not None and age > STALE_STAMP_SEC:
        out["stamp_stale"] = True
        out["warnings"] = (out.get("warnings") or []) + [
            f"stamp is {age:.0f}s old (over {STALE_STAMP_SEC}s); if the window changed since, "
            "re-perceive before acting (max_age_sec=N turns this into a refusal)"
        ]
    # A plan for a window that is gone or now belongs to another process is wrong, so
    # identity failures fail the dry run too; moved/aged stamps only with strict_stamp.
    wrong_window = stamp_check.get("identity_ok") is False
    if stamp_bad and (not dry_run or strict_stamp or wrong_window):
        out["ok"] = False
        out["error"] = stamp_check.get("error") or "stamp check failed; re-perceive"
        if stamp_check.get("hwnd_replaced"):
            out["hwnd_replaced"] = True
            out["suggested_hwnd"] = stamp_check.get("suggested_hwnd")
            _set_refusal(out, "hwnd_replaced",
                         f"window was rebuilt; perceive hwnd={stamp_check.get('suggested_hwnd')} and retry")
        else:
            _set_refusal(out, "identity_changed" if wrong_window else "stamp_check_failed", HINT_REPERCEIVE)
        if method_n in ("uia", "auto"):
            for s in steps:
                s.pop("_uia_control", None)
                s.pop("_uia_action", None)
        return out
    implicit_err = _implicit_stamp_error(frame or {}, stamp_source)
    unguarded = unguarded_keyboard_steps(steps) if method_n == "input" else []
    _note_ime(out, frame or {}, steps, ime_mode)
    if dry_run:
        warns = []
        elev = _target_integrity(out, frame or {})
        if elev and elev.get("elevated_target"):
            warns.append(
                f"target runs at {elev.get('target_integrity')} integrity, above cu-perceive "
                f"({elev.get('self_integrity')}): --go will refuse with reason=elevated_target"
            )
        if implicit_err:
            warns.append(implicit_err + "; --go will refuse")
        if unguarded:
            if _has_window(frame or {}):
                warns.append(
                    f"keyboard step(s) {unguarded} have no preceding click: --go sends them only "
                    f"if stamp window {(frame or {}).get('hwnd')} is in the foreground "
                    "(--focus brings it forward), otherwise refuses"
                )
            else:
                warns.append(
                    f"keyboard step(s) {unguarded} have no preceding click and this stamp has no "
                    "window: --go will refuse; add a click on the target field"
                )
        occluded = occluded_points(frame or {}, steps, method=method_n)
        if occluded is not None:
            out["occluded_points"] = occluded
        if occluded:
            where = ", ".join(
                f"step {o['step']} {o['what']} ({o['point'][0]},{o['point'][1]}) under hwnd {o['hwnd']} {o['title']!r}"
                for o in occluded
            )
            first = occluded[0]
            if first.get("hwnd"):
                out["suggested_hwnd"] = int(first["hwnd"])
            warns.append(
                f"planned point(s) currently covered by another window: {where}; --go will refuse "
                "unless --focus brings the stamp window forward" + _blocking_hint(first.get("hwnd"), first.get("title"))
            )
        if warns:
            out["warnings"] = (out.get("warnings") or []) + warns
        if method_n in ("uia", "auto"):
            for s in steps:
                s.pop("_uia_control", None)
                s.pop("_uia_action", None)
        return out
    if implicit_err:
        out["ok"] = False
        out["error"] = implicit_err
        _set_refusal(out, "implicit_screen_stamp", "pass stamp=<stamp> explicitly, or perceive the window first")
        for s in steps:
            s.pop("_uia_control", None)
            s.pop("_uia_action", None)
        return out
    elev_err = _elevated_target_refusal(out, frame or {})
    if elev_err:
        for s in steps:
            s.pop("_uia_control", None)
            s.pop("_uia_action", None)
        return out
    if focus and _has_window(frame or {}):
        # PR11: focus=true is verified up front (one retry): nothing is sent unless the
        # stamp window (or a window it owns) really is the foreground window.
        ferr = _bring_forward(frame or {}, out)
        if ferr:
            out["ok"] = False
            out["error"] = ferr
            for s in steps:
                s.pop("_uia_control", None)
                s.pop("_uia_action", None)
            return out
    ime_err = _suspend_ime_for_act(out, frame or {}, steps, ime_mode)
    if ime_err:
        for s in steps:
            s.pop("_uia_control", None)
            s.pop("_uia_action", None)
        return ime_err
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
                err, info = _point_target_error(frame or {}, step, focus=focus, out=out)
                if err:
                    for s in steps:
                        s.pop("_uia_control", None)
                        s.pop("_uia_action", None)
                    return _pointer_refuse(out, n, err, info)
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
    from .input import (
        clear_field_for_replace, click_xy, discard_paste, drag_path, enter_text, finish_paste, hover_xy,
        key_down, key_up, prepare_paste, scroll_xy, tap_key,
    )
    held = False
    clicked = False
    clicked_window: int | None = None
    try:
        for n, step in enumerate(steps):
            if _identity_abort(out, frame or {}, steps, n):
                return out
            via = step["via"]
            if via in KEYBOARD_VIAS:
                err = _keyboard_target_error(
                    frame or {}, focus=focus, out=out, clicked_window=clicked_window, settle=clicked
                )
                if err:
                    return _keyboard_refuse(out, n, err)
            elif via in POINTER_VIAS:
                err, info = _point_target_error(frame or {}, step, focus=focus, out=out)
                if err:
                    return _pointer_refuse(out, n, err, info)
                if via in FOCUSING_VIAS and not _has_window(frame or {}):
                    clicked_window = _clicked_window(step)
            if hold and not held and via not in ("hover", "wait"):
                hold_target = None if _has_window(frame or {}) else _clicked_window(step)
                err = _keyboard_target_error(frame or {}, focus=focus, out=out, clicked_window=hold_target, hold=True)
                if err:
                    return _hold_refuse(out, n, hold, err)
                key_down(hold)
                held = True
            if via in FOCUSING_VIAS:
                clicked = True
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
                input_via = step.get("input_via") or "paste"
                staged = None
                # Stage the clipboard before replace clears the field. A failed
                # set must not wipe what the user already typed.
                if input_via != "keys":
                    try:
                        staged = prepare_paste(step["text"])
                    except Exception as e:
                        staged = {"ok": False, "reason": "clipboard_failed", "error": str(e)}
                    if staged.get("reason") == "clipboard_nontext":
                        input_via = "keys"
                        staged = None
                        step["clipboard_preserved"] = True
                        out["warnings"] = (out.get("warnings") or []) + [
                            "the clipboard holds non-text data (image, files or rich text); typed "
                            "with keys instead of paste so it is left untouched"
                        ]
                    elif not staged.get("ok"):
                        step["typed"] = False
                        out["ok"] = False
                        out["reason"] = staged.get("reason") or "clipboard_failed"
                        out["error"] = staged.get("error") or "clipboard_set failed"
                        return out
                if step.get("replace"):
                    try:
                        cleared = clear_field_for_replace()
                    except Exception as e:
                        cleared = {"ok": False, "reason": "replace_failed", "error": str(e)}
                    step["replace_clear"] = {
                        k: cleared.get(k)
                        for k in ("ok", "method", "empty", "verified", "readback", "multiline", "reason", "error")
                        if k in cleared
                    }
                    if not cleared.get("ok"):
                        if staged is not None:
                            discard_paste(staged)
                        out["ok"] = False
                        out["reason"] = cleared.get("reason") or "replace_failed"
                        out["error"] = cleared.get("error") or "replace could not clear the field before typing"
                        return out
                    if cleared.get("verified") is False:
                        out["warnings"] = (out.get("warnings") or []) + [
                            "replace could not read the field back to confirm it was empty before typing"
                        ]
                try:
                    if input_via == "keys":
                        typed = enter_text(step["text"], via="keys")
                    else:
                        typed = finish_paste(staged)
                except Exception as e:
                    typed = {"ok": False, "reason": "clipboard_failed", "error": str(e)}
                if isinstance(typed, dict) and typed.get("ok") is False:
                    step["typed"] = False
                    out["ok"] = False
                    out["reason"] = typed.get("reason") or "clipboard_failed"
                    out["error"] = typed.get("error") or "text input failed"
                    return out
                step["typed"] = True
                step["input_via"] = input_via
                if isinstance(typed, dict):
                    if "restored" in typed:
                        step["clipboard_restored"] = bool(typed.get("restored"))
                    for key, label in (
                        ("clipboard_restore_error", "clipboard restore failed"),
                        ("clipboard_save_error", "clipboard was not saved before paste"),
                    ):
                        if typed.get(key):
                            out["warnings"] = (out.get("warnings") or []) + [f"{label}: {typed[key]}"]
                target_hwnd = int((frame or {}).get("hwnd") or 0) or int(clicked_window or 0)
                if not _note_typed_readback(out, step, step["text"], target_hwnd):
                    return out
            elif via == "key":
                tap_key(step["key"])
                step["tapped"] = True
            if n + 1 < len(steps):
                time.sleep(gap)
        return out
    finally:
        if held:
            key_up(hold)
