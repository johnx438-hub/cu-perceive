"""HTTP MCP face. Runs on the Windows machine it controls. Bind loopback (default) or a
specific host, never 0.0.0.0. Extra Host names (e.g. a tailnet name behind a reverse
proxy) are opt-in via --allow-host or CU_PERCEIVE_ALLOWED_HOSTS."""

import inspect
import time
from io import BytesIO
from pathlib import Path

INLINE_MAX_SIDE = 1280

_PASS_THROUGH = ("ok", "error", "minimized", "identity", "capture", "focused", "warnings",
                 "foreground_ok", "focus_attempts", "foreground_hwnd", "stale_suspect",
                 "hwnd_replaced", "suggested_hwnd", "target_integrity", "elevated_target")


def transport_security_lists(extra_hosts: list[str] | None = None) -> tuple[list[str], list[str]]:
    """(allowed_hosts, allowed_origins) for DNS-rebinding protection: loopback + opt-in names."""
    from .config import allowed_hosts as _configured

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    for h in _configured(extra_hosts):
        hosts += [f"{h}:*", h]
        origins += [f"https://{h}:*", f"https://{h}"]
    return hosts, origins


def mcp_api():
    """(major, ServerClass, Image) for the installed mcp SDK.

    mcp 1.x: mcp.server.fastmcp.FastMCP. mcp 2.x renamed it to
    mcp.server.mcpserver.MCPServer and moved host / transport_security from the
    constructor to streamable_http_app().
    """
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.utilities.types import Image

        return 2, MCPServer, Image
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.utilities.types import Image
    except ImportError as e:
        raise ImportError(
            f"cu-perceive mcp needs the mcp SDK 1.19+ or 2.x ({e}); "
            "reinstall cu-perceive or run: python -m pip install \"mcp>=1.19,<3\""
        ) from e
    return 1, FastMCP, Image


def _tool_error_cls():
    try:
        from mcp.server.mcpserver.exceptions import ToolError

        return ToolError
    except ImportError:
        pass
    try:
        from mcp.server.fastmcp.exceptions import ToolError

        return ToolError
    except ImportError:
        return ValueError


UNKNOWN_ARG_HINTS = {
    "steps": (
        "this tool has no steps parameter (steps=[{...}] would be silently dropped); only desktop act takes "
        "steps=[{flat act fields}, ...]. Use flat fields in one call, e.g. "
        'act(stamp="...", norm="500,500", text="hi", key="enter", go=true) - order is click(s) -> text -> key'
    ),
    "x": 'pass the point as norm="x,y" (0-1000 map) or xy="x,y" (stamp px), or norm=[x, y]',
    "y": 'pass the point as norm="x,y" (0-1000 map) or xy="x,y" (stamp px), or norm=[x, y]',
    "point": 'use norm="x,y" (0-1000 map) or xy="x,y" (stamp px)',
    "click": 'the mouse button is button="left"|"right"|"middle"; norm/xy/ids already click',
    "action": "there is no action field: the fields you pass are the actions (norm/xy/ids click, text, key, scroll, drag)",
}


def unknown_args_error(tool: str, unknown: list[str], known: list[str]) -> dict:
    """ok=false payload for arguments a tool does not take (nothing was run)."""
    hints = [UNKNOWN_ARG_HINTS[a] for a in unknown if a in UNKNOWN_ARG_HINTS]
    return {
        "ok": False,
        "reason": "unknown_args",
        "error": (
            f"{tool}: unknown argument(s) {', '.join(sorted(unknown))}; nothing was run "
            "(unknown arguments are refused, not ignored)"
        ),
        "unknown_args": sorted(unknown),
        "valid_args": sorted(known),
        "hint": "; ".join(hints) or f"use only valid_args for {tool}",
    }


def strict_server_cls(server_cls):
    """server_cls whose tools/call refuses arguments the tool's input schema does not declare.
    Keys starting with "_" (e.g. _meta, which some clients put into arguments) are dropped."""

    class StrictArgsServer(server_cls):
        async def call_tool(self, name, arguments, *args, **kwargs):
            unknown: list[str] = []
            known: list[str] = []
            if isinstance(arguments, dict):
                arguments = {k: v for k, v in arguments.items() if not str(k).startswith("_")}
            if isinstance(arguments, dict) and arguments:
                for t in await self.list_tools():
                    if t.name == name:
                        schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
                        known = list((schema.get("properties") or {}).keys())
                        unknown = [k for k in arguments if k not in known]
                        break
            if unknown:
                import json

                raise _tool_error_cls()(json.dumps(unknown_args_error(name, unknown, known), ensure_ascii=False))
            return await super().call_tool(name, arguments, *args, **kwargs)

    StrictArgsServer.__name__ = f"Strict{server_cls.__name__}"
    return StrictArgsServer


def offloaded(fn):
    """Async wrapper running sync tool ``fn`` in a worker thread. mcp 1.x calls sync tools on
    the event loop, so one slow call (OCR) would otherwise stall every other tool call.

    Dict results gain ``config_rev`` (``<hash>@<profile>``). ``profile`` / ``show_config``
    on the tool, when declared, select the profile and attach the effective config.
    """
    import functools

    @functools.wraps(fn)
    async def run(*args, **kwargs):
        import anyio.to_thread

        def _call():
            from .settings import ConfigError, call_context, finish_response

            profile = None
            show = False
            try:
                bound = inspect.signature(fn).bind_partial(*args, **kwargs)
                profile = bound.arguments.get("profile") or None
                show = bool(bound.arguments.get("show_config"))
            except TypeError:
                pass
            try:
                with call_context(profile=profile, show_config=show):
                    result = fn(*args, **kwargs)
            except ConfigError as exc:
                return finish_response({"ok": False, "reason": "bad_args", "error": str(exc)})
            if isinstance(result, dict):
                return finish_response(result)
            return result

        return await anyio.to_thread.run_sync(_call)

    return run


def build_server(name: str, host: str, port: int, security):
    """Server object plus a zero-arg factory for its streamable-HTTP Starlette app."""
    major, server_cls, _image = mcp_api()
    server_cls = strict_server_cls(server_cls)
    if major >= 2:
        app = server_cls(name)
        return app, lambda: app.streamable_http_app(transport_security=security, host=host)
    app = server_cls(name, host=host, port=port, transport_security=security)
    app.settings.host = host
    app.settings.port = port
    return app, app.streamable_http_app


def _nz_int(v: int):
    return v if v else None


def _nz_scroll(v):
    """MCP scroll: int or string; 0 / "" = no scroll."""
    if isinstance(v, bool) or v in (None, "", 0):
        return None
    return str(v).strip() or None


def _nz_str(v: str):
    return v if v else None


def _center_norm(box):
    if isinstance(box, (list, tuple)) and len(box) == 4:
        return [int((box[0] + box[2]) // 2), int((box[1] + box[3]) // 2)]
    return None


def _wait_s(ms):
    """new_window_wait_ms -> seconds (None = default)."""
    if ms is None or ms == "":
        return None
    try:
        return max(0.0, float(ms)) / 1000.0
    except (TypeError, ValueError):
        return None


def _chain_step_s() -> float:
    from .settings import get as _cfg

    return float(_cfg("popup.chain_step_s"))


def _chain_step_wait(ms, step: dict, i: int, n: int):
    """Chains wait long only on the last / expect_change step; field clicks keep 250 ms.

    Returns the explicit wait (seconds) when the caller passed new_window_wait_ms, else None.
    """
    full = _wait_s(ms)
    if full is None or i == n - 1 or step.get("expect_change"):
        return full
    return min(_chain_step_s(), full)


def _chain_step_cap(ms, step: dict, i: int, n: int):
    """Without an explicit wait, non-final steps cap the default wait at 250 ms (reason chain_step)."""
    if _wait_s(ms) is not None or i == n - 1 or step.get("expect_change"):
        return None
    return _chain_step_s()


def _compact_rows(payload: dict) -> None:
    """perceive_window compact=true: short item / uia_node rows with a clickable center_norm."""
    rows_i = []
    for it in payload.get("items") or []:
        row = {"id": it.get("id"), "text": it.get("text"), "kind": it.get("kind"),
               "center_norm": _center_norm(it.get("norm")), "bbox_norm": it.get("norm")}
        if it.get("confidence") is not None:
            row["confidence"] = it["confidence"]
        rows_i.append(row)
    payload["items"] = rows_i
    if "uia_nodes" in payload:
        rows = []
        for n in payload.get("uia_nodes") or []:
            row = {
                "id": n.get("id"),
                "name": n.get("name"),
                "control_type": n.get("control_type"),
                "center_norm": _center_norm(n.get("norm")),
            }
            if n.get("value") is not None:
                row["value"] = n.get("value")
            if "readonly" in n:
                row["readonly"] = n["readonly"]
            if n.get("occluded"):
                row["occluded"] = True
            rows.append(row)
        payload["uia_nodes"] = rows
    payload["compact"] = True


def serve(host: str = "127.0.0.1", port: int = 8771, allowed_hosts: list[str] | None = None) -> None:
    import json
    from mcp.server.transport_security import TransportSecuritySettings
    from PIL import Image as PILImage

    from .act import (StampWindowMismatch, abort_state, annotate_verification, clear_abort,
                      coerce_ids, load_frame, parse_steps, request_abort, run_chain)
    from .browser.tools import register as register_browser_tools
    from .core import perceive
    from .extras import MissingExtraError
    from .launch import launch, list_apps
    from .windows import PICK_RULE, find_windows, list_windows, windows_map

    _major, _server_cls, Image = mcp_api()

    def _thumb_image(map_path: str):
        im = PILImage.open(map_path)
        w, h = im.size
        from .settings import get as _cfg

        side = int(_cfg("server.inline_max_side"))
        if max(w, h) > side:
            scale = side / max(w, h)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), PILImage.Resampling.LANCZOS)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=int(_cfg("server.jpeg_quality")), optimize=True)
        return Image(data=buf.getvalue(), format="jpeg")

    def _with_map(payload: dict, map_path, map_inline: bool = True):
        from .settings import finish_response

        if isinstance(payload, dict):
            finish_response(payload)
        # FastMCP turns [str, Image] into separate content blocks:
        #   TextContent(JSON) + ImageContent(type=image, data=base64, mimeType=...)
        # Do NOT concat blocks into one string and JSON.parse — that blows up at the join.
        # Clients must: parse text blocks as JSON; treat type=image blocks as the map.
        text = json.dumps(payload, ensure_ascii=False)
        if not map_inline:
            return text
        if map_path and Path(map_path).exists():
            return [text, _thumb_image(map_path)]
        return text

    hosts, origins = transport_security_lists(allowed_hosts)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )
    app, http_app = build_server("cu-perceive", host, port, security)

    def _tool(fn):
        app.tool()(offloaded(fn))
        return fn

    @_tool
    def windows(title: str = "", exe: str = "", hwnd: int = 0, map: bool = True, map_inline: bool = True, query: str = "",
                profile: str = "", show_config: bool = False):
        """Desktop window list + ruler map (0-1000, screen space). Read the image to pick hwnd.
        hwnd 0 / empty title+exe = all windows (+ optional map). map_inline=false = JSON only.
        title and/or exe set (no hwnd): returns windows[] of ALL matches (browser multi-window safe),
        plus window=picked by pick_rule (non-minimized, then EnumWindows z-top, then area — not guaranteed foreground).
        hwnd set: one row in windows[] / window. Does not click.
        query: case-insensitive substring of title OR exe (title matches first); cannot be combined with title/exe.
        profile selects a configuration profile for this call. show_config attaches the effective config."""
        _ = (profile, show_config)  # applied by the MCP wrapper before this body, and stamped on the result
        hwnd_n = _nz_int(hwnd)
        query_n = (_nz_str(query) or "").strip()
        if query_n and (title or exe):
            return {"ok": False, "reason": "bad_args", "error": "query cannot be combined with title or exe",
                    "hint": "pass query alone, or title/exe"}
        if hwnd_n or title or exe or query_n:
            if hwnd_n:
                rows = find_windows(hwnd=hwnd_n)
            elif query_n:
                rows, seen = [], set()
                for w in find_windows(title=query_n) + find_windows(exe=query_n):
                    if w and int(w.get("hwnd") or 0) not in seen:
                        seen.add(int(w.get("hwnd") or 0))
                        rows.append(w)
            else:
                rows = find_windows(title=title or "", exe=exe or "")
            slim = [
                {k: w.get(k) for k in ("hwnd", "title", "exe", "pid", "ctime", "minimized", "rect", "class")}
                for w in rows
                if w
            ]
            picked = slim[0] if slim else None
            return {
                "windows": slim,
                "window": picked,
                "count": len(slim),
                "pick_rule": PICK_RULE,
            }

        if not map:
            rows = list_windows()
            slim = [{k: w.get(k) for k in ("hwnd", "title", "exe", "pid", "ctime", "minimized", "rect")} for w in rows]
            return {"windows": slim}
        out = windows_map()
        payload = {
            "stamp": out["stamp"],
            "count": len(out["windows"]),
            "origin": out["origin"],
            "map": out["map"],
            "json": out["json"],
            "windows": out["windows"],
            "inline": "jpeg-1280" if map_inline else False,
        }
        return _with_map(payload, out["map"], map_inline)

    @_tool
    def perceive_window(
        hwnd: int = 0,
        title: str = "",
        exe: str = "",
        image: str = "",
        ocr: bool = False,
        yolo: bool = False,
        uia: bool = False,
        map_inline: bool = True,
        out_dir: str = "",
        roi: str = "",
        roi_norm: str = "",
        focus: bool = False,
        capture: str = "",
        compact: bool = False,
        uia_include_occluded: bool = False,
        profile: str = "",
        show_config: bool = False,
    ):
        """perceive_window does not focus; act go needs the window in front or focus=true.
        Pin one window (or primary screen). Writes raw + 0-1000 map. Default ocr=false.
        hwnd 0 and empty title/exe = primary screen. Returns JSON plus a compressed map (0-1000 still valid; clicks use stamp width/height).
        The window is captured where it is, even behind other windows; focus stays put. focus=true raises/restores it first.
        Minimized window without focus=true -> ok=false, minimized=true, no stamp (restore it or pass focus=true).
        Window parked off-screen (-32000) or under 8 px -> ok=false, reason=offscreen|tiny_client, hint; move it back on-screen (focus=true does not help).
        Use the returned json/map paths; the server's output dir may differ from the CLI's.
        capture: auto (WGC -> PrintWindow) | wgc | printwindow | screen (needs focus=true) | dwm (experimental, black on Win11).
        map_inline=false = JSON/path only. ocr=true only for text ids. OCR confidence (0-1) is a ranking signal
        from the recognizer, not a correctness verdict: it depends on the crop, and a confident item can still be
        misread. Compare items with it; verify text that matters. yolo=true runs ScreenParser (class names as item text).
        uia=true (S4) walks UIA under pinned hwnd; additive uia_nodes with 0-1000 norm (same as grid). Default all off. No click.
        ocr/uia/yolo are optional extras; if missing the reply has ok=false and the exact pip command in "install".
        ocr/yolo run in a worker process with a timeout (CU_PERCEIVE_DETECT_TIMEOUT, default 30 s): a hung worker is
        restarted and the stamp comes back without detections plus a warning. Unknown arguments are refused (reason=unknown_args).
        compact=true without uia=true adds uia_nodes_omitted=true and a warning (UIA stays opt-in).
        compact=true: items as {id, text, kind, center_norm, bbox_norm, confidence} (OCR confidence 0-1, omitted when unknown; full items also carry box px and norm); uia_nodes keep name, control_type, center_norm, value, and readonly (null when a DataItem/DataGrid provider cannot say; a cell value of "(null)" is omitted). Text/DataItem/Edit content stays. Occluded nodes are omitted (uia_occluded); an empty runtime id is not occlusion. A walk that times out returns partial nodes and uia_timeout. center_norm goes straight into act norm.
        focus=true is measured: focused/foreground_ok report whether the window really is foreground afterwards (a warning when not).
        Occlusion only counts another top-level window (other process) or a sibling MDI child over a node, never the window itself or its own child HWNDs (CEF/Chromium safe); dropped nodes are named in uia_occluded_names (max 30). uia_include_occluded=true keeps them with occluded=true.
        stale_suspect=true: WGC failed and the grab fell back to PrintWindow (may be an old frame). hwnd_replaced/suggested_hwnd: the hwnd looks rebuilt.
        E2 light verify: roi="L,T,W,H" or roi_norm="CX,CY[,HALF]" (mutually exclusive) crops after full grab; map 0-1000 is ROI-local.
        out_dir optional: Windows path or /mnt/c/... ; response also has map_wsl / png_wsl (lowercase drive).
        A stamp written to a custom out_dir is registered under the default dir, so act finds it by bare stamp name, by hwnd, and as the latest stamp."""
        _ = (profile, show_config)  # applied by the MCP wrapper before this body, and stamped on the result
        hwnd_n = _nz_int(hwnd)
        title_n = _nz_str(title)
        exe_n = _nz_str(exe)
        roi_n = _nz_str(roi)
        roi_norm_n = _nz_str(roi_norm)
        if roi_n and roi_norm_n:
            return {"ok": False, "error": "roi and roi_norm are mutually exclusive"}
        try:
            out = perceive(
                image=_nz_str(image),
                hwnd=hwnd_n,
                title=title_n,
                exe=exe_n,
                focus=bool(focus),
                ocr=ocr,
                yolo=yolo,
                uia=uia,
                out_dir=_nz_str(out_dir),
                roi=roi_n or None,
                roi_norm=roi_norm_n or None,
                capture=_nz_str(capture),
                uia_include_occluded=bool(uia_include_occluded),
            )
        except MissingExtraError as e:
            return e.to_dict()
        if out.get("ok") is False and not out.get("stamp"):
            keep = ("ok", "error", "reason", "hint", "minimized", "degenerate_client", "hwnd", "window", "capture", "focused",
                    "foreground_ok", "hwnd_replaced", "suggested_hwnd")
            return {k: out.get(k) for k in keep if k in out}
        payload = {
            "stamp": out["stamp"],
            "count": len(out["items"]),
            "ocr": out.get("ocr"),
            "yolo": out.get("yolo"),
            "uia": out.get("uia"),
            "source": out["source"],
            "coord_space": out["coord_space"],
            "hwnd": out["hwnd"],
            "origin": out.get("origin"),
            "width": out.get("width"),
            "height": out.get("height"),
            "items": out["items"],
            "map": out.get("map") or out.get("grid"),
            "grid": out.get("grid"),
            "png": out["png"],
            "anno": out.get("anno"),
            "json": out["json"],
            "map_wsl": out.get("map_wsl"),
            "png_wsl": out.get("png_wsl"),
            "json_wsl": out.get("json_wsl"),
            "inline": "jpeg-1280" if map_inline else False,
        }
        for k in _PASS_THROUGH:
            if k in out:
                payload[k] = out[k]
        if out.get("verify_light"):
            payload["verify_light"] = True
            payload["roi"] = out.get("roi")
            payload["roi_norm"] = out.get("roi_norm")
            payload["full_width"] = out.get("full_width")
            payload["full_height"] = out.get("full_height")
        if out.get("ocr_ms") is not None:
            payload["ocr_ms"] = out["ocr_ms"]
        if out.get("uia"):
            payload["uia_nodes"] = out.get("uia_nodes") or []
            payload["uia_count"] = out.get("uia_count", len(payload["uia_nodes"]))
            if "uia_ms" in out:
                payload["uia_ms"] = out["uia_ms"]
            if "caps" in out:
                payload["caps"] = out["caps"]
            if "uia_truncated" in out:
                payload["uia_truncated"] = out["uia_truncated"]
            if "uia_occluded" in out:
                payload["uia_occluded"] = out["uia_occluded"]
            if out.get("uia_occluded_names"):
                payload["uia_occluded_names"] = out["uia_occluded_names"]
                payload["uia_occluded_why"] = out.get("uia_occluded_why")
            if out.get("uia_all_occluded"):
                payload["uia_all_occluded"] = True
                payload.setdefault("hint", "every UIA node is covered by another window: perceive_window(..., focus=true); if it is still covered, a topmost window is in the way and must be moved")
            if out.get("uia_timeout"):
                payload["uia_timeout"] = True
            payload["ok"] = out.get("ok", True)
            if out.get("error"):
                payload["error"] = out["error"]
        if compact:
            _compact_rows(payload)
            if not uia:
                payload["uia_nodes_omitted"] = True
                payload["warnings"] = (payload.get("warnings") or []) + [
                    "compact=true without uia=true: uia_nodes are not included (UIA is opt-in); pass uia=true "
                    "for Chrome/Electron/WinForms controls"
                    + ("" if payload.get("items") else ", and ocr=true for text ids")
                ]
        return _with_map(payload, payload["map"], map_inline)

    @_tool
    def act(
        stamp: str = "",
        ids: str | int | list = "",
        xy: str | list = "",
        norm: str | list = "",
        text: str = "",
        key: str = "",
        drag: str | list = "",
        drag_norm: str | list = "",
        step_px: int | None = None,
        scroll: str | int = "",
        button: str = "left",
        hover: bool = False,
        go: bool = False,
        hold: float | None = None,
        hold_key: str = "",
        method: str = "input",
        max_age_sec: float | None = None,
        strict_stamp: bool = False,
        uia_name: str = "",
        replace: bool = False,
        via: str = "",
        ime: str = "",
        focus: bool = False,
        hwnd: int = 0,
        dry_run: bool | None = None,
        steps: list | None = None,
        check_change: bool = False,
        new_window_wait_ms: int | None = None,
        change_min_ratio: float | None = None,
        profile: str = "",
        show_config: bool = False,
    ):
        """act: go=true sends (default: dry run; dry_run=true wins); needs window in front or focus=true. scroll="down:5" notches (bare 5 = UP). key="enter"|"pagedown". norm="x,y" or [x,y]. hwnd= checks stamp.
        norm="x,y" or [x,y] (0-1000 map) | xy="x,y" or [x,y] (stamp px) | ids="3"; scroll="down:3"|"up:3" (wheel notches, not pixels; max 50; int or string; bare numbers: negative = down, positive = UP); drag_norm="x1,y1;x2,y2"; text="..." + replace=true; key="enter"|"ctrl+a"|"pagedown"; hold_key=ctrl|shift|alt (needs a click/drag/scroll); button="left"|"right"; focus=true; go=true sends (default: dry run); via=paste|keys; ime=off.
        perceive_window does not focus; act go needs the window in front or focus=true.
        stamp: a stamp name or the json path perceive_window returned. hwnd (optional): the window you mean;
        with no stamp it picks that window's newest stamp, with a stamp it refuses one from another window
        (reason=hwnd_mismatch). Pass stamp or hwnd when several windows are open.
        dry_run=true forces a dry run even with go=true; dry_run=false alone does not send (go=true does).
        Unknown arguments (x=, action=, click=, ...) are refused with reason=unknown_args and valid_args; nothing runs.
        Refusals return ok=false with reason (code), hint (next step) and blocking_window {hwnd, title}.
        ids are OCR item ids. When the stamp has no OCR items (ocr=false) an id is matched against uia_nodes (step id_source="uia"). An id that matches neither is reason=bad_ids with hint, item_ids and uia_ids; a uia_name that does not resolve is reason=uia_not_found.

        Plan/run against a stamp. go default false. Prefer norm (map 0-1000) or xy.

        Fields are flat (strings/numbers); for a multi-step chain pass steps=[{...}, ...], each step an object of the same flat fields (see steps below). Examples:
          click:  act(stamp="...", norm="935,69", go=true)
          chain:  act(stamp="...", ids="3", key="ctrl+a", text="hi", go=true)
          keys only: act(stamp="...", text="hi", replace=true, go=true, focus=true)
          scroll: act(norm="500,500", scroll="down:3")  # hover then wheel, no click

        norm/xy: "x,y" or "x:y"; multi with ";" e.g. "177,752;500,200"; arrays too: [640,500] or [[177,752],[500,200]].
        4 numbers (an item / uia_node norm box "left,top,right,bottom") click the box center, with a warning.
        Mouse button: button="left"|"right"|"middle" (there is no click= param). step_px only affects drags.
        Stamps older than 120 s add stamp_stale=true and a warning (max_age_sec=N refuses instead).
        scroll counts are wheel notches (one notch ~3 lines), not pixels; more than 50 is capped at 50 with scroll_clamped=true and a warning.
        ids: comma string / int / list; needs prior ocr/yolo perceive.
        text=paste+restore by default (via="paste"|"keys"; keys = SendInput unicode, for a grid cell that ignores Ctrl+V). key like ctrl+c (comma-separated ok). Unknown key -> ok=false, reason=unknown_key, valid_keys.
        Clipboard errors return ok=false reason=clipboard_failed (the server stays up). Previous clipboard text is restored; an empty clipboard is emptied again. Paste is staged before replace clears the field.
        After typing, typed_verified is true/false/null from a read-back of the target window's focused element. A mismatch is reason=typed_mismatch (never ok=true). A non-native control (Monaco/CodeMirror) that reads back empty is not a mismatch: typed_verified=null with warning readback_unavailable; a different non-empty value still is typed_mismatch. A provider value of "(null)" reads back as empty. Password edits (UIA IsPassword or ES_PASSWORD) skip that read-back: typed_verified is null and typed_note says so. A mask template with no letters or digits (____-__-__) counts as empty.
        ime="off" switches the focused input thread to an already-installed Latin layout (never loads a new one), else closes its IME, then verifies and restores what it changed. A handled ime=off does not also warn ime_active.
        Before any clear or type, the top-level window that owns keyboard focus (GA_ROOT of hwndFocus) must be the stamp hwnd. An owned modal dialog is reason=focus_outside and nothing is cleared. blocking_window then names that top-level dialog {hwnd, title, class, focus_hwnd} and the hint says to close or dismiss it first.
        When the clipboard holds non-text data (image, files, rich text) that cannot be restored, paste is skipped and the text is typed with keys; the step reports clipboard_preserved=true and a warning.
        Concurrent go=true acts run one at a time. A dry run reports foreground_click_count=0; the plan is planned_foreground_click_count.
        go=true adds new_windows [{hwnd, title, class}] after a click, Enter, F-key, Alt combo, or Esc (polled up to new_window_wait_ms, default 1200 or CU_PERCEIVE_NEW_WINDOW_WAIT_MS, then one recheck 300 ms later; it returns as soon as a window appears; in a steps chain only the last / expect_change step waits that long, earlier steps 250 ms). windows_snapshot_changed=true means some top-level window appeared, closed or changed title. Typing does not wait. after is {foreground_hwnd, title, class}.
        Keys: a-z, 0-9, f1-f24, num0-num9, alt, backquote, backslash, backspace, bracketleft,
              bracketright, capslock, comma, ctrl, delete, down, end, enter, equals, esc, home, insert,
              left, menu, minus, numlock, pagedown, pageup, pause, period, printscreen, quote, right,
              scrolllock, semicolon, shift, slash, space, tab, up, volumedown, volumemute, volumeup, win
          (case-insensitive; page_down / PgDn / Return / Escape / del also work).
        ids+text in ONE call: ids are real clicks first, then text - NOT a silent focus flag.
        Order is click(s) -> text -> key. replace=true clears the focused edit before the text
        (single-line edit or masked edit: End+Shift+Home+Delete — SetValue("") does not clear a
        WinForms MaskedTextBox — else ValuePattern or EM_SETSEL; multiline / code editor (ES_MULTILINE,
        line breaks, browser textarea, Monaco, Document): EM_SETSEL+WM_CLEAR or Ctrl+A, Delete) and checks the
        field is empty before typing. replace_clear.empty is true only when that is really read back;
        a control that exposes no value gives empty=null, verified=false, readback="unavailable" and a warning.
        go=true on a window stamp: each click / drag start+end / scroll point must be on the stamp
        window (or a window it owns) and each text/key needs it in the foreground, even after a
        click; otherwise ok=false (pointer_refused_step + point_window, or keyboard_refused_step).
        focus=true brings it forward first (identity re-checked). Dry runs list occluded_points.
        Screen stamps: keys need a click first and go only to the window that click landed on.
        No stamp = latest perceive stamp; go=true then needs a window stamp with identity.
        A window that is gone or whose HWND/PID/creation time changed fails even a dry run;
        stamp_ok / stamp_error report the stamp check at top level.
        go=true: foreground_click_count counts clicks actually sent (0 if refused or on a dry run);
        planned_foreground_click_count is the plan. go=true calls are serialized in-process.
        scroll+xy/norm/ids => hover then wheel (NO left-click) - will not focus an input.
        hold>0 = long-press. hold_key=ctrl|shift|alt across the chain (not mouse hold); needs a
        norm/xy/ids click, drag or scroll in the same call (else reason=hold_key_needs_pointer_step).
        On go it is pressed only while the stamp window (or one it owns) is in front, else hold_key_refused_step.
        method=input (default SendInput) | post (S1) | uia (S2) | auto (S3).
        post/uia/auto: single left clicks only (norm/xy/ids); reject drag/path/scroll/type/hold.
        uia: Invoke|Toggle|Expand|Select; no silent fallback. auto: probe UIA → chosen_method=uia|input (never post); go runs chosen only.
        E3 same-stamp: multi norm with ";" or repeated clicks on one stamp; max_age_sec / strict_stamp
        pass through to stamp_check (ROI/moved stamps fail on go or when strict_stamp).
        steps=[{...}, ...] (max 20): a guarded chain, each step an object of flat act fields
        (ids xy norm text key drag drag_norm step_px scroll button hover hold hold_key method uia_name replace via
        expect_change), all against the same stamp. stamp hwnd go dry_run focus ime max_age_sec strict_stamp
        go on the call, not in a step; do not mix steps with top-level action fields. Default is a dry run that
        plans every step (planned_steps). Live, the chain stops after a step that opened a window
        (new_windows), moved the foreground off the stamp window (foreground_changed), changed its title
        (title_changed), or failed (focus_outside, typed_mismatch, replace_failed, any error). A stop returns
        ok=false, stopped=true, stopped_at, stop_reason, completed_steps, remaining_steps and a hint to
        re-perceive and continue. expect_change=true marks the one step meant to change the screen
        (submit/OK/Next/Enter); it must be the last step (else bad_args). Classify targets after a perceive:
        stable ones (fields, checkboxes, same-panel buttons) batch into one chain, with at most one
        change-causing step at the end.
        check_change=true (opt-in, default off) grabs the stamp window before and after a go and adds
        screen_changed, changed_ratio and changed_bbox_norm (pixels only; whether it is the right change is
        yours to judge). Dry runs skip it. focus=true is verified: if the stamp window is still not
        foreground after a retry, nothing is sent (reason=focus_failed, foreground_ok=false). A rebuilt window
        (hwnd gone, class changed, or hidden while a same-process window replaced it) is reason=hwnd_replaced
        with hwnd_replaced=true and suggested_hwnd. check_change also covers drags; layout_reflow_suspected=true
        means much of the window moved: re-perceive before reusing old coordinates. reflow_reason="bbox_span"
        (changed_bbox_share over 0.7 with changed_ratio >= 0.02, or over 0.4 with >= 0.1: white page swap, list
        re-sort, Xmind re-layout after a node drop) or "ratio" (changed_ratio over 0.3). screen_changed counts a change of >= 8 px and
        >= change_min_ratio of the window (default 0.001, env CU_PERCEIVE_CHANGE_MIN_RATIO). Under the floor:
        screen_changed=false, below_min_ratio=true, changed_pixels/changed_ratio/changed_bbox_norm still given
        (hover ~0.0005 lands there; pass a lower change_min_ratio for tiny real changes). Counted but under 0.005
        adds change_minor=true: mostly hover / caret / focus-ring noise, though a checkbox redraw can be there too;
        read changed_bbox_norm and decide. change_min_ratio without check_change=true is ignored (warned).
        steps: each completed step carries new_window_wait_ms / new_window_wait_reason; the top level has the last
        step's values plus new_windows_steps (indexes of steps that opened windows). With an explicit
        new_window_wait_ms every non-final step (text too) reports min(250, it) / explicit.
        below_min_ratio appears only when 0 < changed_pixels and the ratio is under the floor; no change at all
        is screen_changed=false without it. reflow_reason="row_bands": >= 3 horizontal bands of change of
        similar width with changed_ratio >= 0.01 (a list re-sorted inside a small panel); changed_row_bands
        is the band count. With focus=true the baseline is grabbed after the window is raised
        (change_baseline=after_focus), so activation alone is not a change.
        expect_change only checks window-level changes (new_windows, foreground, title); with nothing seen the
        reply warns expect_change_unmet. Pixel changes need check_change=true.
        Hover steps are checked like clicks (a dry-run occluded_points entry says "hover"; go refuses with point_covered).
        Covered points (point_covered / point_covered_after_focus, dry-run occluded_points) give suggested_hwnd
        = the covering window: perceive_window(hwnd=suggested_hwnd) even when it is untitled and missing from
        windows(). Popups and dialogs may belong to a different process (pid) than the app window.
        ime="off" also applies to key-only acts (Ctrl chords can be eaten by an IME). It acts on the thread of the
        focused child window (GetGUIThreadInfo; else the stamp window's thread). Each installed English layout
        (en-US, en-GB-US, en-GB), then other Latin ones, is tried and verified there by language id, all within one
        0.75 s budget (ime.verify_budget_ms; ime.verify_ms = time used). If no layout sticks but the IME is open,
        it is closed instead (ime_closed=true counts as suspended). ime reports thread_id, top_thread_id,
        focus_hwnd, hkl_hex, hkl_before_hex, requested_hex, tried, ime_open_before/after and
        ime_conversion_before/after (bit 0x1 = native input). Unverified: warning + ime_unverified=true and the act
        still runs; ime="strict" aborts (reason=ime_failed). ime_layout_switched only for a verified change.
        ime_restored only appears when a layout switch or IME close actually happened: true once undone, false if
        undoing failed. ime_suspended alone never sets it.
        after.unlisted_foreground=true when the foreground window is not in windows(); after.popup_hwnd /
        after.popup {hwnd, title, class, why} name an unlisted window in front, over the stamp window's centre,
        or blocking, so Qt/CEF popups that new_windows cannot see are visible.
        new_windows: clicks, Enter/Esc, F-keys, Alt combos and Ctrl(+Shift)+letter poll by default; an explicit
        new_window_wait_ms always polls, whatever the action (new_window_wait_reason=explicit|action|
        chain_step|action_cannot_open_window; chain_step = non-final chain step, default wait capped at 250 ms). If focus fails because the foreground window is elevated,
        foreground_elevated=true: Windows then drops all synthetic input until the user closes it.
        Capability boundary: a target running elevated (as administrator) above cu-perceive's integrity level
        cannot receive input (Windows UIPI); go refuses with reason=elevated_target before sending anything
        (dry runs warn; elevated_target / target_integrity / self_integrity are reported).
        """
        _ = (profile, show_config)  # applied by the MCP wrapper before this body, and stamped on the result
        from .settings import get as _cfg

        if step_px is None:
            step_px = int(_cfg("drag.step_px"))
        if hold is None:
            hold = float(_cfg("timing.click_hold_s"))
        if check_change:
            # locals() in a closure also holds free variables: keep only real parameters
            params = inspect.signature(act).parameters
            kw = {k: v for k, v in locals().items() if k in params}
            kw["check_change"] = False
            return annotate_verification(_act_with_change(kw))
        ignored_min_ratio = (["change_min_ratio only applies with check_change=true; ignored"]
                             if change_min_ratio is not None else [])
        if steps is not None and steps != []:
            out = _act_chain(steps, locals())
            if ignored_min_ratio and isinstance(out, dict):
                out["warnings"] = ignored_min_ratio + (out.get("warnings") or [])
            return annotate_verification(out)
        send = bool(go) and not dry_run
        notes = []
        notes.extend(ignored_min_ratio)
        if go and dry_run:
            notes.append("dry_run=true overrides go=true: this was a dry run, nothing was sent")
        elif dry_run is False and not go:
            notes.append("dry_run=false does not send input; pass go=true to send")
        if step_px != int(_cfg("drag.step_px")) and not (_nz_str(drag) or _nz_str(drag_norm)):
            notes.append(
                f"step_px={step_px} ignored: it only sets drag smoothness (px per mouse move) for drag/drag_norm; "
                'scroll distance is scroll="down:N" in wheel notches'
            )
        try:
            specs = parse_steps(
                ids=coerce_ids(ids), xy=_nz_str(xy), norm=_nz_str(norm), text=_nz_str(text),
                key=_nz_str(key), drag=_nz_str(drag), drag_norm=_nz_str(drag_norm),
                scroll=_nz_scroll(scroll), button=button, hover=hover, step_px=step_px, hold=hold,
                uia_name=_nz_str(uia_name) or None,
                replace=bool(replace),
                input_via=_nz_str(via),
            )
        except ValueError as e:
            return annotate_verification({
                "ok": False, "error": str(e), "dry_run": not send, "reason": "bad_args",
                "hint": 'points are norm="x,y" / xy="x,y" (or [x,y]); scroll="down:3" / "up:3"'})
        try:
            frame, path = load_frame(stamp=_nz_str(stamp), hwnd=_nz_int(hwnd))
        except StampWindowMismatch as e:
            return annotate_verification({
                "ok": False, "error": str(e), "dry_run": not send, "reason": "hwnd_mismatch",
                "hint": "perceive_window(hwnd=...) and pass that stamp, or drop hwnd"})
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError) as e:
            return annotate_verification({"ok": False, "error": str(e), "dry_run": not send})
        out = run_chain(
            frame,
            specs,
            dry_run=not send,
            hold_key=_nz_str(hold_key),
            method=_nz_str(method) or "input",
            max_age_sec=max_age_sec,
            strict_stamp=bool(strict_stamp),
            stamp_json_path=str(path) if path else None,
            focus=bool(focus),
            stamp_source=("explicit" if _nz_str(stamp) else "latest") if path else None,
            ime=_nz_str(ime),
            new_window_wait_s=_wait_s(new_window_wait_ms),
        )
        out["json"] = str(path) if path else None
        if notes:
            out["warnings"] = notes + (out.get("warnings") or [])
        return annotate_verification(out)

    @_tool
    def abort(clear: bool = False, reason: str = "operator", status: bool = False):
        """Operator stop for live acts. Every call returns the switch state (abort_active, abort_remaining_s).

        Default: raise the stop flag. It is process-wide (every desktop act of every client of this
        server stops; browser_act is not covered) and is
        checked right before each dispatched step, so an in-flight act sends at most the step already
        running and then returns verification "aborted" with abort_step (chain calls also report
        abort_inner_step) and evidence.steps_sent. Live acts keep refusing until clear=true or the
        flag auto-clears after CU_PERCEIVE_ABORT_TTL_S seconds (default 120, 0 = never); a repeat
        request restarts the timer. status=true only reads the state. Dry runs still plan and report
        abort_active. An agent that hits verification "aborted" must stop and report to the operator,
        not clear the flag itself.
        """
        if clear and status:
            return {"ok": False, "reason": "bad_args", "error": "pass clear=true or status=true, not both",
                    **abort_state()}
        if clear:
            return {"ok": True, "cleared": True, **clear_abort()}
        if status:
            return {"ok": True, **abort_state()}
        return {"ok": True, **request_abort(reason or "operator")}

    def _flat_specs(f: dict):
        from .settings import get as _cfg

        return parse_steps(
            ids=coerce_ids(f.get("ids", "")), xy=_nz_str(f.get("xy", "")), norm=_nz_str(f.get("norm", "")),
            text=_nz_str(f.get("text", "")), key=_nz_str(f.get("key", "")), drag=_nz_str(f.get("drag", "")),
            drag_norm=_nz_str(f.get("drag_norm", "")), scroll=_nz_scroll(f.get("scroll", "")),
            button=f.get("button") or "left", hover=bool(f.get("hover", False)),
            step_px=int(_cfg("drag.step_px") if f.get("step_px") in (None, "") else (f.get("step_px") or _cfg("drag.step_px"))),
            hold=float(_cfg("timing.click_hold_s") if f.get("hold") in (None, "") else (f.get("hold") or 0)),
            uia_name=_nz_str(f.get("uia_name", "")) or None, replace=bool(f.get("replace", False)),
            input_via=_nz_str(f.get("via", "")),
        )

    def _act_with_change(kw: dict):
        from .change import run_with_change_check

        if not (bool(kw.get("go")) and not kw.get("dry_run")):
            mr = kw.pop("change_min_ratio", None)  # skipped with check_change, not "ignored"
            out = act(**kw)
            if isinstance(out, dict):
                out["check_change"] = "skipped (dry run)"
                if mr is not None:
                    out["notes"] = (out.get("notes") or []) + [
                        "check_change is skipped in a dry run, so change_min_ratio is skipped too"]
            return out
        try:
            frame, _ = load_frame(stamp=_nz_str(kw.get("stamp", "")), hwnd=_nz_int(kw.get("hwnd", 0)))
            target = int(frame.get("hwnd") or 0) or None
        except Exception:
            target = None  # act() itself reports the stamp problem
        mr = kw.pop("change_min_ratio", None)  # consumed here; act() would warn it was ignored
        from .change import focus_baseline_hook

        pre = focus_baseline_hook(target, bool(kw.get("focus")))
        return run_with_change_check(target, lambda: act(**kw), min_ratio=None if mr in (None, "") else float(mr),
                                     before_baseline=pre)

    def _act_chain(steps, call: dict):
        from .chain import ACTION_FIELDS, run_steps, validate_steps

        send = bool(call.get("go")) and not call.get("dry_run")
        mixed = [k for k in ACTION_FIELDS if call.get(k) not in (None, "", [], 0)]
        if mixed:
            return {"ok": False, "reason": "bad_args", "dry_run": not send,
                    "error": f"steps cannot be combined with top-level action field(s) {', '.join(mixed)}",
                    "hint": "put every action inside steps=[...], or drop steps for a flat single-step act"}
        norm_steps, err = validate_steps(steps)
        if err:
            err["dry_run"] = not send  # a live call with bad steps is refused, not a dry run
            return err
        try:
            frame, path = load_frame(stamp=_nz_str(call.get("stamp", "")), hwnd=_nz_int(call.get("hwnd", 0)))
        except StampWindowMismatch as e:
            return {"ok": False, "error": str(e), "dry_run": not send, "reason": "hwnd_mismatch",
                    "hint": "perceive_window(hwnd=...) and pass that stamp, or drop hwnd"}
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError) as e:
            return {"ok": False, "error": str(e), "dry_run": not send}
        stamp_source = ("explicit" if _nz_str(call.get("stamp", "")) else "latest") if path else None

        def run_one(step: dict, send_flag: bool, i: int) -> dict:
            try:
                specs = _flat_specs(step)
            except ValueError as e:
                return {"ok": False, "error": str(e), "dry_run": not send_flag, "reason": "bad_args",
                        "hint": 'points are norm="x,y" / xy="x,y" (or [x,y]); scroll="down:3" / "up:3"'}
            return run_chain(
                frame, specs, dry_run=not send_flag,
                hold_key=_nz_str(step.get("hold_key", "")) or _nz_str(call.get("hold_key", "")),
                method=_nz_str(step.get("method", "")) or _nz_str(call.get("method", "")) or "input",
                max_age_sec=call.get("max_age_sec"), strict_stamp=bool(call.get("strict_stamp")),
                stamp_json_path=str(path) if path else None,
                focus=bool(call.get("focus")) and i == 0,
                stamp_source=stamp_source, ime=_nz_str(call.get("ime", "")),
                new_window_wait_s=_chain_step_wait(call.get("new_window_wait_ms"), step, i, len(norm_steps)),
                new_window_wait_cap_s=_chain_step_cap(call.get("new_window_wait_ms"), step, i, len(norm_steps)),
            )

        stamp_hwnd = int((frame or {}).get("hwnd") or 0)

        def title_now():
            if not stamp_hwnd:
                return None
            from ._win32 import window_title

            return window_title(stamp_hwnd)

        if send:
            # One abort epoch for the whole chain: a stop on at chain start stays on for every step.
            from .act import abort_epoch

            with abort_epoch():
                out = run_steps(norm_steps, run_one, send=send, stamp_hwnd=stamp_hwnd, title_now=title_now)
        else:
            out = run_steps(norm_steps, run_one, send=send, stamp_hwnd=stamp_hwnd, title_now=title_now)
        out["stamp"] = (frame or {}).get("stamp")
        out["hwnd"] = stamp_hwnd or None
        out["json"] = str(path) if path else None
        if call.get("go") and call.get("dry_run"):
            out["warnings"] = ["dry_run=true overrides go=true: this was a dry run, nothing was sent"] + (out.get("warnings") or [])
        return out

    @_tool
    def launch_app(app: str = "", exe: str = ""):
        """Start a named app from apps.json, or an exe path. Always starts; no dry-run."""
        return launch(app=_nz_str(app), exe=_nz_str(exe))

    @_tool
    def apps():
        """List quick-launch names in apps.json."""
        return {"apps": list_apps()}

    @_tool
    def config(action: str = "show", profile: str = "", show_config: bool = True):
        """Show, reload, or validate the effective configuration.

        action=show returns the effective values and config_rev. action=reload re-reads the TOML
        file after the current live act finishes; a failed reload keeps the previous file.
        action=validate checks the file without dropping a good config. profile selects which
        built-in or file profile to display. show_config includes the same values (default true).
        Locked keys cannot be set in the file. Tighten-only keys can only move stricter.
        """
        _ = show_config  # the wrapper stamps config_rev; show already returns the values
        from .settings import config_action

        return config_action(action, profile=profile or None)

    register_browser_tools(_tool)

    import uvicorn
    config = uvicorn.Config(http_app(), host=host, port=port, log_level="info", http="h11")
    uvicorn.Server(config).run()


if __name__ == "__main__":
    serve()
