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


def _nz_int(v: int):
    return v if v else None


def _nz_str(v: str):
    return v if v else None


def serve(host: str = "127.0.0.1", port: int = 8771, allowed_hosts: list[str] | None = None) -> None:
    import json
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.utilities.types import Image
    from mcp.server.transport_security import TransportSecuritySettings
    from PIL import Image as PILImage

    from .act import coerce_ids, load_frame, parse_steps, run_chain
    from .core import perceive
    from .extras import MissingExtraError
    from .launch import launch, list_apps
    from .windows import PICK_RULE, find_windows, list_windows, windows_map

    def _thumb_image(map_path: str) -> Image:
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
    app = FastMCP(
        "cu-perceive",
        host=host,
        port=port,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=origins,
        ),
    )
    app.settings.host = host
    app.settings.port = port

    @app.tool()
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

    @app.tool()
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
    ):
        """Pin one window (or primary screen). Writes raw + 0-1000 map. Default ocr=false.
        hwnd 0 and empty title/exe = primary screen. Returns JSON plus a compressed map (0-1000 still valid; clicks use stamp width/height).
        The window is captured where it is, even behind other windows; focus stays put. focus=true raises/restores it first.
        Minimized window without focus=true -> ok=false, minimized=true, no stamp (restore it or pass focus=true).
        capture: auto (WGC -> DWM thumbnail -> PrintWindow) | wgc | dwm | printwindow | screen (screen needs focus=true).
        map_inline=false = JSON/path only. ocr=true only for text ids. yolo=true runs ScreenParser (class names as item text).
        uia=true (S4) walks UIA under pinned hwnd; additive uia_nodes with 0-1000 norm (same as grid). Default all off. No click.
        ocr/uia/yolo are optional extras; if missing the reply has ok=false and the exact pip command in "install".
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
            return {k: out.get(k) for k in ("ok", "error", "minimized", "hwnd", "window", "capture", "focused") if k in out}
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
        return _with_map(payload, payload["map"], map_inline)

    @app.tool()
    def act(
        stamp: str = "",
        ids: str | int | list = "",
        xy: str = "",
        norm: str = "",
        text: str = "",
        key: str = "",
        drag: str = "",
        drag_norm: str = "",
        step_px: int = 4,
        scroll: str = "",
        button: str = "left",
        hover: bool = False,
        go: bool = False,
        hold: float = 0,
        hold_key: str = "",
        method: str = "input",
        max_age_sec: float | None = None,
        strict_stamp: bool = False,
        uia_name: str = "",
    ):
        """
        Plan/run against a stamp. go default false. Prefer norm (map 0-1000) or xy.

        FLAT STRING FIELDS ONLY (not a JSON steps array). Examples:
          click:  act(norm="935,69", go=true)
          chain:  act(stamp="...", ids="3", key="ctrl+a", text="hi", go=true)
          scroll: act(norm="500,500", scroll="down:3")  # hover then wheel, no click

        norm/xy: "x,y" or "x:y"; multi with ";" e.g. "177,752;500,200".
        ids: comma string / int / list; needs prior ocr/yolo perceive.
        text=paste+restore. key like ctrl+c (comma-separated ok).
        ids+text in ONE call: ids are real clicks first, then text - NOT a silent focus flag.
        If field already has text, pass key=ctrl+a (or split acts).
        scroll+xy/norm/ids => hover then wheel (NO left-click) - will not focus an input.
        hold>0 = long-press. hold_key=ctrl|shift|alt across the chain (not mouse hold).
        method=input (default SendInput) | post (S1) | uia (S2) | auto (S3).
        post/uia/auto: single left clicks only (norm/xy/ids); reject drag/path/scroll/type/hold.
        uia: Invoke|Toggle|Expand|Select; no silent fallback. auto: probe UIA → chosen_method=uia|input (never post); go runs chosen only.
        Unknown args (e.g. steps=[{action:click}]) are dropped by MCP - if nothing remains, ok=false.
        E3 same-stamp: multi norm with ";" or repeated clicks on one stamp; max_age_sec / strict_stamp
        pass through to stamp_check (ROI/moved stamps fail on go or when strict_stamp).
        """
        specs = parse_steps(
            ids=coerce_ids(ids), xy=_nz_str(xy), norm=_nz_str(norm), text=_nz_str(text),
            key=_nz_str(key), drag=_nz_str(drag), drag_norm=_nz_str(drag_norm),
            scroll=_nz_str(scroll), button=button, hover=hover, step_px=step_px, hold=hold,
            uia_name=_nz_str(uia_name) or None,
        )
        try:
            frame, path = load_frame(stamp=_nz_str(stamp))
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError) as e:
            return {"ok": False, "error": str(e), "dry_run": not go}
        out = run_chain(
            frame,
            specs,
            dry_run=not go,
            hold_key=_nz_str(hold_key),
            method=_nz_str(method) or "input",
            max_age_sec=max_age_sec,
            strict_stamp=bool(strict_stamp),
            stamp_json_path=str(path) if path else None,
        )
        out["json"] = str(path) if path else None
        return out

    @app.tool()
    def launch_app(app: str = "", exe: str = ""):
        """Start a named app from apps.json, or an exe path. Always starts; no dry-run."""
        return launch(app=_nz_str(app), exe=_nz_str(exe))

    @app.tool()
    def apps():
        """List quick-launch names in apps.json."""
        return {"apps": list_apps()}

    import uvicorn
    starlette_app = app.streamable_http_app()
    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info", http="h11")
    uvicorn.Server(config).run()


if __name__ == "__main__":
    serve()
