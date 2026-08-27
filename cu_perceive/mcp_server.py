"""HTTP MCP face. Must run on ARCHER. Bind a specific host, never 0.0.0.0."""

from io import BytesIO
from pathlib import Path

INLINE_MAX_SIDE = 1280


def _nz_int(v: int):
    return v if v else None


def _nz_str(v: str):
    return v if v else None


def serve(host: str = "127.0.0.1", port: int = 8771) -> None:
    import json
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.utilities.types import Image
    from mcp.server.transport_security import TransportSecuritySettings
    from PIL import Image as PILImage

    from .act import coerce_ids, load_frame, parse_steps, run_chain
    from .core import perceive
    from .launch import launch, list_apps
    from .windows import find_window, list_windows, windows_map

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
        text = json.dumps(payload, ensure_ascii=False)
        if not map_inline:
            return text
        if map_path and Path(map_path).exists():
            return [text, _thumb_image(map_path)]
        return text

    ts_host = "archer.tailca07d9.ts.net"
    app = FastMCP(
        "cu-perceive",
        host=host,
        port=port,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", f"{ts_host}:*", ts_host],
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                "http://[::1]:*",
                f"https://{ts_host}:*",
                f"https://{ts_host}",
            ],
        ),
    )
    app.settings.host = host
    app.settings.port = port

    @app.tool()
    def windows(title: str = "", exe: str = "", hwnd: int = 0, map: bool = True, map_inline: bool = True):
        """Desktop window list + ruler map (0-1000, screen space). Read the image to pick hwnd.
        Returns JSON plus a compressed map image (full PNG still on disk). hwnd 0 / empty title+exe = all windows.
        map_inline=false = JSON only. title/exe/hwnd set = one row, no image. Does not click."""
        hwnd_n = _nz_int(hwnd)
        if hwnd_n or title or exe:
            one = find_window(title=title or "", exe=exe or "", hwnd=hwnd_n)
            return {"window": one}
        if not map:
            rows = list_windows()
            slim = [{k: w.get(k) for k in ("hwnd", "title", "exe", "pid", "minimized", "rect")} for w in rows]
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
        map_inline: bool = True,
        out_dir: str = "",
    ):
        """Pin one window (or primary screen). Writes raw + 0-1000 map. Default ocr=false.
        hwnd 0 and empty title/exe = primary screen. Returns JSON plus a compressed map (0-1000 still valid; clicks use stamp width/height).
        map_inline=false = JSON/path only. ocr=true only for text ids. yolo=true runs ScreenParser (class names as item text). Default both off. No click.
        out_dir optional: Windows path or /mnt/c/... ; response also has map_wsl / png_wsl (lowercase drive)."""
        hwnd_n = _nz_int(hwnd)
        title_n = _nz_str(title)
        exe_n = _nz_str(exe)
        # Pin then look: hwnd/title/exe always activate so capture_hwnd double-shots.
        pin = bool(hwnd_n or title_n or exe_n)
        out = perceive(image=_nz_str(image), hwnd=hwnd_n, title=title_n, exe=exe_n, focus=pin, ocr=ocr, yolo=yolo, out_dir=_nz_str(out_dir))
        payload = {
            "stamp": out["stamp"],
            "count": len(out["items"]),
            "ocr": out.get("ocr"),
            "yolo": out.get("yolo"),
            "source": out["source"],
            "coord_space": out["coord_space"],
            "hwnd": out["hwnd"],
            "origin": out.get("origin"),
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
    ):
        """Plan/run against a stamp. go default false. Prefer norm (map 0-1000) or xy.
        Empty string = omitted. drag_norm / drag = one left-hold polyline. step_px default 4.
        ids: comma string, a single int, or a list (5 / '5' / [5,9] / '14,9,16'); needs a prior ocr perceive. text=paste+restore (CLI flag is --type or --text). key like ctrl+c.
        scroll: 3, -3, down:3, or up:3 (negative/down = wheel down). scroll + xy/norm/ids hovers then wheels (no left-click). hold>0 with xy/norm/ids = press, sleep, release. key may be comma-separated.
        hold_key=ctrl|control|shift|alt holds that modifier across the chain (Ctrl+click multi-select). Not hold (mouse long-press)."""
        specs = parse_steps(
            ids=coerce_ids(ids), xy=_nz_str(xy), norm=_nz_str(norm), text=_nz_str(text),
            key=_nz_str(key), drag=_nz_str(drag), drag_norm=_nz_str(drag_norm),
            scroll=_nz_str(scroll), button=button, hover=hover, step_px=step_px, hold=hold,
        )
        frame, path = load_frame(stamp=_nz_str(stamp))
        out = run_chain(frame, specs, dry_run=not go, hold_key=_nz_str(hold_key))
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
