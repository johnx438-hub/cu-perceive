"""HTTP MCP face. Runs on the Windows machine it controls. Bind loopback (default) or a
specific host, never 0.0.0.0. Extra Host names (e.g. a tailnet name behind a reverse
proxy) are opt-in via --allow-host or CU_PERCEIVE_ALLOWED_HOSTS."""

from io import BytesIO
from pathlib import Path

INLINE_MAX_SIDE = 1280

_PASS_THROUGH = ("ok", "error", "minimized", "identity", "capture", "focused", "warnings")


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
        "act has no steps parameter (steps=[{...}] would be silently dropped); use flat fields in "
        'one call, e.g. act(stamp="...", norm="500,500", text="hi", key="enter", go=true) - order is '
        "click(s) -> text -> key"
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
    the event loop, so one slow call (OCR) would otherwise stall every other tool call."""
    import functools

    @functools.wraps(fn)
    async def run(*args, **kwargs):
        import anyio.to_thread

        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

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


def _compact_rows(payload: dict) -> None:
    """perceive_window compact=true: short item / uia_node rows with a clickable center_norm."""
    payload["items"] = [
        {"id": it.get("id"), "text": it.get("text"), "kind": it.get("kind"), "center_norm": _center_norm(it.get("norm"))}
        for it in payload.get("items") or []
    ]
    if "uia_nodes" in payload:
        payload["uia_nodes"] = [
            {"id": n.get("id"), "name": n.get("name"), "control_type": n.get("control_type"),
             "center_norm": _center_norm(n.get("norm"))}
            for n in payload.get("uia_nodes") or []
        ]
    payload["compact"] = True


def serve(host: str = "127.0.0.1", port: int = 8771, allowed_hosts: list[str] | None = None) -> None:
    import json
    import threading
    from mcp.server.transport_security import TransportSecuritySettings
    from PIL import Image as PILImage

    from .act import StampWindowMismatch, coerce_ids, load_frame, parse_steps, run_chain
    from .browser.tools import register as register_browser_tools
    from .core import perceive
    from .extras import MissingExtraError
    from .launch import launch, list_apps
    from .windows import PICK_RULE, find_windows, list_windows, windows_map

    _major, _server_cls, Image = mcp_api()

    def _thumb_image(map_path: str):
        im = PILImage.open(map_path)
        w, h = im.size
        if max(w, h) > INLINE_MAX_SIDE:
            scale = INLINE_MAX_SIDE / max(w, h)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), PILImage.Resampling.LANCZOS)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=80, optimize=True)
        return Image(data=buf.getvalue(), format="jpeg")

    def _with_map(payload: dict, map_path, map_inline: bool = True):
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
    act_lock = threading.Lock()

    def _tool(fn):
        app.tool()(offloaded(fn))
        return fn

    @_tool
    def windows(title: str = "", exe: str = "", hwnd: int = 0, map: bool = True, map_inline: bool = True):
        """Desktop window list + ruler map (0-1000, screen space). Read the image to pick hwnd.
        hwnd 0 / empty title+exe = all windows (+ optional map). map_inline=false = JSON only.
        title and/or exe set (no hwnd): returns windows[] of ALL matches (browser multi-window safe),
        plus window=picked by pick_rule (non-minimized, then EnumWindows z-top, then area — not guaranteed foreground).
        hwnd set: one row in windows[] / window. Does not click."""
        hwnd_n = _nz_int(hwnd)
        if hwnd_n or title or exe:
            if hwnd_n:
                rows = find_windows(hwnd=hwnd_n)
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
    ):
        """perceive_window does not focus; act go needs the window in front or focus=true.
        Pin one window (or primary screen). Writes raw + 0-1000 map. Default ocr=false.
        hwnd 0 and empty title/exe = primary screen. Returns JSON plus a compressed map (0-1000 still valid; clicks use stamp width/height).
        The window is captured where it is, even behind other windows; focus stays put. focus=true raises/restores it first.
        Minimized window without focus=true -> ok=false, minimized=true, no stamp (restore it or pass focus=true).
        Window parked off-screen (-32000) or under 8 px -> ok=false, reason=offscreen|tiny_client, hint; move it back on-screen (focus=true does not help).
        Use the returned json/map paths; the server's output dir may differ from the CLI's.
        capture: auto (WGC -> PrintWindow) | wgc | printwindow | screen (needs focus=true) | dwm (experimental, black on Win11).
        map_inline=false = JSON/path only. ocr=true only for text ids. yolo=true runs ScreenParser (class names as item text).
        uia=true (S4) walks UIA under pinned hwnd; additive uia_nodes with 0-1000 norm (same as grid). Default all off. No click.
        ocr/uia/yolo are optional extras; if missing the reply has ok=false and the exact pip command in "install".
        ocr/yolo run in a worker process with a timeout (CU_PERCEIVE_DETECT_TIMEOUT, default 30 s): a hung worker is
        restarted and the stamp comes back without detections plus a warning. Unknown arguments are refused (reason=unknown_args).
        compact=true: items (and uia_nodes) only as {id, text|name, kind|control_type, center_norm}; center_norm goes straight into act norm.
        E2 light verify: roi="L,T,W,H" or roi_norm="CX,CY[,HALF]" (mutually exclusive) crops after full grab; map 0-1000 is ROI-local.
        out_dir optional: Windows path or /mnt/c/... ; response also has map_wsl / png_wsl (lowercase drive)."""
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
            )
        except MissingExtraError as e:
            return e.to_dict()
        if out.get("ok") is False and not out.get("stamp"):
            keep = ("ok", "error", "reason", "hint", "minimized", "degenerate_client", "hwnd", "window", "capture", "focused")
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
            payload["ok"] = out.get("ok", True)
            if out.get("error"):
                payload["error"] = out["error"]
        if compact:
            _compact_rows(payload)
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
        step_px: int = 4,
        scroll: str | int = "",
        button: str = "left",
        hover: bool = False,
        go: bool = False,
        hold: float = 0,
        hold_key: str = "",
        method: str = "input",
        max_age_sec: float | None = None,
        strict_stamp: bool = False,
        uia_name: str = "",
        replace: bool = False,
        focus: bool = False,
        hwnd: int = 0,
        dry_run: bool | None = None,
    ):
        """act: go=true sends (default: dry run; dry_run=true wins); needs window in front or focus=true. scroll="down:5" notches (bare 5 = UP). key="enter"|"pagedown". norm="x,y" or [x,y]. hwnd= checks stamp.
        norm="x,y" or [x,y] (0-1000 map) | xy="x,y" or [x,y] (stamp px) | ids="3"; scroll="down:3"|"up:3" (wheel notches, not pixels; max 50; int or string; bare numbers: negative = down, positive = UP); drag_norm="x1,y1;x2,y2"; text="..." + replace=true; key="enter"|"ctrl+a"|"pagedown"; hold_key=ctrl|shift|alt (needs a click/drag/scroll); button="left"|"right"; focus=true; go=true sends (default: dry run).
        perceive_window does not focus; act go needs the window in front or focus=true.
        stamp: a stamp name or the json path perceive_window returned. hwnd (optional): the window you mean;
        with no stamp it picks that window's newest stamp, with a stamp it refuses one from another window
        (reason=hwnd_mismatch). Pass stamp or hwnd when several windows are open.
        dry_run=true forces a dry run even with go=true; dry_run=false alone does not send (go=true does).
        Unknown arguments (steps=[...], x=, action=, ...) are refused with reason=unknown_args and valid_args; nothing runs.
        Refusals return ok=false with reason (code), hint (next step) and blocking_window {hwnd, title}.

        Plan/run against a stamp. go default false. Prefer norm (map 0-1000) or xy.

        FLAT STRING FIELDS ONLY (not a JSON steps array). Examples:
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
        text=paste+restore. key like ctrl+c (comma-separated ok). Unknown key -> ok=false, reason=unknown_key, valid_keys.
        Keys: a-z, 0-9, f1-f24, num0-num9, alt, backquote, backslash, backspace, bracketleft,
              bracketright, capslock, comma, ctrl, delete, down, end, enter, equals, esc, home, insert,
              left, menu, minus, numlock, pagedown, pageup, pause, period, printscreen, quote, right,
              scrolllock, semicolon, shift, slash, space, tab, up, volumedown, volumemute, volumeup, win
          (case-insensitive; page_down / PgDn / Return / Escape / del also work).
        ids+text in ONE call: ids are real clicks first, then text - NOT a silent focus flag.
        Order is click(s) -> text -> key. replace=true adds ctrl+a right before the text so it
        replaces what the field holds (e.g. norm=address bar, text=URL, replace=true, key=enter).
        go=true on a window stamp: each click / drag start+end / scroll point must be on the stamp
        window (or a window it owns) and each text/key needs it in the foreground, even after a
        click; otherwise ok=false (pointer_refused_step + point_window, or keyboard_refused_step).
        focus=true brings it forward first (identity re-checked). Dry runs list occluded_points.
        Screen stamps: keys need a click first and go only to the window that click landed on.
        No stamp = latest perceive stamp; go=true then needs a window stamp with identity.
        A window that is gone or whose HWND/PID/creation time changed fails even a dry run;
        stamp_ok / stamp_error report the stamp check at top level.
        go=true: foreground_click_count counts clicks actually sent (0 if refused);
        planned_foreground_click_count is the plan.
        scroll+xy/norm/ids => hover then wheel (NO left-click) - will not focus an input.
        hold>0 = long-press. hold_key=ctrl|shift|alt across the chain (not mouse hold); needs a
        norm/xy/ids click, drag or scroll in the same call (else reason=hold_key_needs_pointer_step).
        On go it is pressed only while the stamp window (or one it owns) is in front, else hold_key_refused_step.
        method=input (default SendInput) | post (S1) | uia (S2) | auto (S3).
        post/uia/auto: single left clicks only (norm/xy/ids); reject drag/path/scroll/type/hold.
        uia: Invoke|Toggle|Expand|Select; no silent fallback. auto: probe UIA → chosen_method=uia|input (never post); go runs chosen only.
        E3 same-stamp: multi norm with ";" or repeated clicks on one stamp; max_age_sec / strict_stamp
        pass through to stamp_check (ROI/moved stamps fail on go or when strict_stamp).
        """
        send = bool(go) and not dry_run
        notes = []
        if go and dry_run:
            notes.append("dry_run=true overrides go=true: this was a dry run, nothing was sent")
        elif dry_run is False and not go:
            notes.append("dry_run=false does not send input; pass go=true to send")
        if step_px != 4 and not (_nz_str(drag) or _nz_str(drag_norm)):
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
            )
        except ValueError as e:
            return {"ok": False, "error": str(e), "dry_run": not send, "reason": "bad_args",
                    "hint": 'points are norm="x,y" / xy="x,y" (or [x,y]); scroll="down:3" / "up:3"'}
        try:
            frame, path = load_frame(stamp=_nz_str(stamp), hwnd=_nz_int(hwnd))
        except StampWindowMismatch as e:
            return {"ok": False, "error": str(e), "dry_run": not send, "reason": "hwnd_mismatch",
                    "hint": "perceive_window(hwnd=...) and pass that stamp, or drop hwnd"}
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError) as e:
            return {"ok": False, "error": str(e), "dry_run": not send}
        with act_lock:
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
            )
        out["json"] = str(path) if path else None
        if notes:
            out["warnings"] = notes + (out.get("warnings") or [])
        return out

    @_tool
    def launch_app(app: str = "", exe: str = ""):
        """Start a named app from apps.json, or an exe path. Always starts; no dry-run."""
        return launch(app=_nz_str(app), exe=_nz_str(exe))

    @_tool
    def apps():
        """List quick-launch names in apps.json."""
        return {"apps": list_apps()}

    register_browser_tools(_tool)

    import uvicorn
    config = uvicorn.Config(http_app(), host=host, port=port, log_level="info", http="h11")
    uvicorn.Server(config).run()


if __name__ == "__main__":
    serve()
