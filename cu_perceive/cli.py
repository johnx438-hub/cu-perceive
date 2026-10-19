from __future__ import annotations

import argparse
import json
import sys

from .capture import CAPTURE_CHOICES
from .keys import key_names_summary


def _act_type_key_order(argv: list[str] | None) -> list[dict] | None:
    """Keep --key/--type/--text in the order they were typed so the first key is not dropped."""
    src = list(argv if argv is not None else sys.argv[1:])
    out: list[dict] = []
    i = 0
    while i < len(src):
        a = src[i]
        if a in ("--key", "--type", "--text") and i + 1 < len(src) and not str(src[i + 1]).startswith("-"):
            val = src[i + 1]
            if a == "--key":
                out.append({"kind": "key", "key": val})
            else:
                out.append({"kind": "type", "text": val, "input_via": "paste"})
            i += 2
            continue
        i += 1
    return out or None



def _slim_win(w: dict) -> dict:
    return {
        "id": w.get("id"),
        "hwnd": w.get("hwnd"),
        "title": w.get("title"),
        "exe": w.get("exe"),
        "pid": w.get("pid"),
        "ctime": w.get("ctime"),
        "rect": w.get("rect"),
        "minimized": w.get("minimized"),
    }


def utf8_stdio() -> None:
    """Emit UTF-8 on stdout/stderr whatever the console code page is (cp1252 cannot
    encode CJK window titles). Consumers should decode the output as UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(prog="cu-perceive")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("perceive", help="window/screen -> 0-1000 grid map; OCR optional")
    p.add_argument("--image", help="existing png; default grab primary screen")
    p.add_argument("--out", help="output directory")
    p.add_argument("--hwnd", type=int, help="capture this window client area")
    p.add_argument("--title", help="find window by title substring")
    p.add_argument("--exe", help="find window by exe name substring")
    p.add_argument(
        "--focus",
        action="store_true",
        help="restore + raise the window before capture. Default: capture in place (works behind other windows; minimized -> ok=false)",
    )
    p.add_argument(
        "--capture",
        choices=CAPTURE_CHOICES,
        default=None,
        help="auto = WGC -> PrintWindow (default, or CU_PERCEIVE_CAPTURE); one backend to force it; screen = legacy grab, needs --focus; dwm = experimental, returns black on Windows 11",
    )
    p.add_argument("--ocr", action="store_true", help="also run OCR and write .anno.png + item ids")
    p.add_argument("--yolo", action="store_true", help="ScreenParser boxes as items (class name as text). Default off")
    p.add_argument("--uia", action="store_true", help="S4: also walk UIA tree under pinned window; additive uia_nodes with 0-1000 norm. Default off")
    roi_g = p.add_mutually_exclusive_group()
    roi_g.add_argument(
        "--roi",
        default=None,
        help="E2 light verify: crop L,T,W,H in client/image pixels (clamped); writes crop png+map",
    )
    roi_g.add_argument(
        "--roi-norm",
        dest="roi_norm",
        default=None,
        help="E2 light verify: CX,CY[,HALF] in full-window 0-1000 (HALF default 150); crop around center",
    )

    w = sub.add_parser("windows", help="list windows and write a desktop ruler map")
    w.add_argument("--title", help="filter by title substring")
    w.add_argument("--exe", help="filter by exe substring")
    w.add_argument("--hwnd", type=int, help="look up one hwnd")
    w.add_argument("--no-map", action="store_true", help="json only, skip the desktop ruler map")

    a = sub.add_parser(
        "act",
        help="click ids from a perceive frame (chain, one shot)",
        epilog=(
            "perceive does not focus; act --go needs the window in front or --focus. "
            f"--key names: {key_names_summary()} (case-insensitive; page_down, PgDn, Return, Escape, del also work; "
            "chords with +, several keys with commas). --scroll: negative or down:N = wheel down (toward the page "
            "bottom), positive or up:N = wheel up. --hold-key needs a click, drag or scroll in the same call."
        ),
    )
    a.add_argument(
        "--stamp",
        help="perceive stamp name or the json path perceive returned; default latest perceive json (--go then needs a window stamp with identity)",
    )
    a.add_argument("--json", help="path to perceive json")
    a.add_argument(
        "--hwnd",
        type=int,
        default=None,
        help="the window you mean: without --stamp use its newest stamp; with --stamp refuse a stamp of another window",
    )
    a.add_argument("--ids", help="comma ids from anno, e.g. 14,9,16")
    a.add_argument("--xy", help="window pixels: 183,542;400,300")
    a.add_argument("--norm", help="0-1000: 177,752;500,200")
    a.add_argument("--steps", help="mixed chain: id:14,xy:183:542,norm:177:752,uia:File,uia:#12")
    a.add_argument(
        "--uia-name",
        dest="uia_name",
        action="append",
        default=None,
        help="E4: click UIA node by name from stamp uia_nodes (repeatable or comma-list); maps to norm, still method=auto|uia|input",
    )
    a.add_argument("--go", action="store_true", help="actually click; default is dry-run")
    a.add_argument(
        "--focus",
        action="store_true",
        help="with --go, bring the stamp window to the front (identity re-checked) when a click/drag/scroll "
        "point is covered by another window or a --type/--key would go to another window; without it those "
        "steps are refused",
    )
    a.add_argument(
        "--method",
        default="input",
        help="contact method: input (SendInput, default) | post (HWND PostMessage, S1) | uia (UIA patterns, S2) | auto (S3: uia if hit+pattern else input; left click only)",
    )
    a.add_argument("--double", action="store_true")
    a.add_argument("--type", "--text", dest="text", help="type text. Default pastes (clipboard + Ctrl+V) and restores the previous clipboard text. CLI: --type or --text; MCP field is text")
    a.add_argument(
        "--via",
        choices=["paste", "keys"],
        default=None,
        help='how --type/--text is entered: paste (default, clipboard + Ctrl+V) or keys (SendInput unicode per character; use this when a grid cell ignores Ctrl+V)',
    )
    a.add_argument(
        "--replace",
        action="store_true",
        help="clear the focused edit before each --type (ValuePattern, EM_SETSEL, or Home+Shift+End+Delete — not Ctrl+A, which a classic WinForms TextBox ignores) and check it is empty",
    )
    a.add_argument("--slow-type", action="store_true", help="per-char SendInput instead of paste (same as --via keys)")
    a.add_argument(
        "--ime",
        choices=["off"],
        default=None,
        help='off: for --go, switch the target window to an English layout and close its IME, then restore both. An active IME is always reported (ime_active warning)',
    )
    a.add_argument("--key", help="tap keys: enter,tab,pagedown,f5,1, or chords like ctrl+c / ctrl+shift+t (all names: see below)")
    a.add_argument("--drag", help="hold-drag window pixels polyline: x1,y1,x2,y2;x3,y3")
    a.add_argument("--drag-norm", dest="drag_norm", help="hold-drag 0-1000 polyline: 200,300;400,280")
    a.add_argument("--path", help="experimental: path.json (0-1000 polylines) -> one press-and-drag")
    a.add_argument("--path-id", dest="path_id", type=int, default=0, help="which paths[].id to draw (default 0)")
    a.add_argument("--close", action="store_true", help="force-close polyline (append first point again)")
    a.add_argument("--step-px", dest="step_px", type=int, default=None, help="approx pixels between stroke samples (default 4; experimental --path default 2)")
    a.add_argument("--scroll", help="wheel notches: 3 / -3 / down:3 / up:3 (negative or down = wheel down, toward the page bottom; positive or up = toward the top)")
    a.add_argument("--button", default="left", help="left|right|middle")
    a.add_argument("--hover", action="store_true", help="move onto target, do not click")
    a.add_argument("--hold", type=float, default=0, help="seconds to hold click before release; with xy/norm/ids = long-press")
    a.add_argument("--hold-key", dest="hold_key", choices=["ctrl", "control", "shift", "alt"], default=None, help="hold modifier across the chain's clicks/drags/scrolls (ctrl/control/shift/alt); needs one of those in the same call; not --hold mouse long-press")
    a.add_argument("--wait", help="seconds to wait at end of chain")
    a.add_argument("--gap", type=float, default=0.12)
    a.add_argument(
        "--max-age",
        dest="max_age",
        type=float,
        default=None,
        help="E3: fail stamp if older than SEC (stamp YYYYMMDD-HHMMSS or json mtime); omit = no age limit",
    )
    a.add_argument(
        "--strict-stamp",
        dest="strict_stamp",
        action="store_true",
        help="E3: dry-run also fails closed when stamp_check is bad (moved/ROI/age/hwnd)",
    )

    L = sub.add_parser("launch", help="start an app from apps.json or an exe path")
    L.add_argument("--app", help="name in apps.json")
    L.add_argument("--exe", help="full exe path")

    pe = sub.add_parser(
        "path-extract",
        help="experimental: image silhouette -> 0-1000 path.json (OpenCV contour)",
    )
    pe.add_argument("--image", required=True, help="input image (png/jpg)")
    pe.add_argument("--out", required=True, help="output path.json")
    pe.add_argument("--mode", default="contour", help="v0: contour only")
    pe.add_argument("--simplify", type=float, default=1.5, help="approxPolyDP epsilon (px); ignored if --simplify-frac > 0")
    pe.add_argument(
        "--simplify-frac",
        dest="simplify_frac",
        type=float,
        default=0.0,
        help="relative DP eps = F * perimeter (wins over --simplify when F>0)",
    )
    pe.add_argument(
        "--spacing-px",
        dest="spacing_px",
        type=float,
        default=None,
        help="arc-length resample spacing in px (sets method=arc_resample; omit for approx_poly)",
    )
    pe.add_argument(
        "--overlay",
        default=None,
        help="write QA overlay PNG (polyline on source) only on success",
    )
    pe.add_argument("--min-points", dest="min_points", type=int, default=8)
    pe.add_argument("--max-points", dest="max_points", type=int, default=400)
    pe.add_argument("--invert", action="store_true", help="invert before threshold (white-bg black ink)")
    pe.add_argument(
        "--largest-only",
        dest="largest_only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep only longest outer contour (default true; --no-largest-only for all)",
    )
    pe.add_argument(
        "--roi",
        default=None,
        help="optional canvas ROI L,T,W,H in image pixels (top-left=0,0); norm relative to ROI",
    )

    m = sub.add_parser("mcp", help="serve streamable-http MCP on this machine")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8771)
    m.add_argument(
        "--allow-host",
        dest="allow_host",
        action="append",
        default=None,
        help="extra Host name to accept besides loopback (repeatable; also CU_PERCEIVE_ALLOWED_HOSTS=a,b)",
    )

    args = parser.parse_args(argv)
    if args.cmd == "perceive":
        from .core import perceive
        from .extras import MissingExtraError
        try:
            out = perceive(
                image=args.image,
                out_dir=args.out,
                hwnd=args.hwnd,
                title=args.title,
                exe=args.exe,
                focus=args.focus,
                ocr=args.ocr,
                yolo=args.yolo,
                uia=bool(getattr(args, "uia", False)),
                roi=getattr(args, "roi", None),
                roi_norm=getattr(args, "roi_norm", None),
                capture=getattr(args, "capture", None),
            )
        except MissingExtraError as e:
            print(json.dumps(e.to_dict(), ensure_ascii=False))
            print(str(e), file=sys.stderr)
            return 3
        if out.get("ok") is False and not out.get("stamp"):
            print(json.dumps(out, ensure_ascii=False))
            print(out.get("error") or "perceive failed", file=sys.stderr)
            return 1
        slim = {
            "stamp": out["stamp"],
            "count": len(out["items"]),
            "ocr": out.get("ocr"),
            "yolo": out.get("yolo"),
            "uia": out.get("uia"),
            "source": out["source"],
            "coord_space": out["coord_space"],
            "hwnd": out["hwnd"],
            "window": out.get("window"),
            "origin": out.get("origin"),
            "width": out.get("width"),
            "height": out.get("height"),
            "map": out.get("map") or out.get("grid"),
            "grid": out.get("grid"),
            "anno": out.get("anno"),
            "json": out["json"],
        }
        for k in ("identity", "capture", "focused", "warnings"):
            if k in out:
                slim[k] = out[k]
        if out.get("verify_light"):
            slim["verify_light"] = True
            slim["roi"] = out.get("roi")
            slim["roi_norm"] = out.get("roi_norm")
            slim["full_width"] = out.get("full_width")
            slim["full_height"] = out.get("full_height")
        if out.get("ocr_ms") is not None:
            slim["ocr_ms"] = out["ocr_ms"]
        if out.get("uia"):
            slim["uia_nodes"] = out.get("uia_nodes") or []
            slim["uia_count"] = out.get("uia_count", len(slim["uia_nodes"]))
            if "caps" in out:
                slim["caps"] = out["caps"]
            if "uia_truncated" in out:
                slim["uia_truncated"] = out["uia_truncated"]
            if "uia_ms" in out:
                slim["uia_ms"] = out["uia_ms"]
            slim["ok"] = out.get("ok", True)
            if out.get("error"):
                slim["error"] = out["error"]
        print(json.dumps(slim, ensure_ascii=False))
        if out.get("uia") and out.get("ok") is False:
            return 1
        return 0
    if args.cmd == "windows":
        from .windows import PICK_RULE, find_windows, list_windows, windows_map
        if args.hwnd or args.title or args.exe:
            rows = find_windows(title=args.title or "", exe=args.exe or "", hwnd=args.hwnd)
            slim = [_slim_win(w) for w in rows if w]
            out = {
                "windows": slim,
                "window": slim[0] if slim else None,
                "count": len(slim),
                "pick_rule": PICK_RULE,
            }
            print(json.dumps(out, ensure_ascii=False))
            return 0 if slim else 1
        if args.no_map:
            rows = [_slim_win(x) for x in list_windows()]
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        out = windows_map()
        print(json.dumps({
            "stamp": out["stamp"],
            "count": len(out["windows"]),
            "origin": out["origin"],
            "map": out["map"],
            "json": out["json"],
            "windows": [_slim_win(x) for x in out["windows"]],
        }, ensure_ascii=False))
        return 0
    if args.cmd == "act":
        from .act import (
            MethodError,
            PathActError,
            StampWindowMismatch,
            load_frame,
            load_path_json,
            parse_steps,
            path_to_drag_specs,
            run_chain,
            subsample_points,
        )
        extras = _act_type_key_order(argv)
        warnings: list[str] = []
        path_specs: list[dict] = []
        step_px = args.step_px
        if args.path:
            # Experimental draw: denser default step-px unless overridden.
            if step_px is None:
                step_px = 2
            try:
                pdata = load_path_json(args.path)
                path_specs = path_to_drag_specs(
                    pdata,
                    path_id=int(args.path_id),
                    close=bool(args.close),
                    step_px=int(step_px),
                )
            except PathActError as e:
                err = {
                    "ok": False,
                    "error": str(e),
                    "dry_run": not args.go,
                    "experimental": True,
                    "path_file": args.path,
                    "path_id": int(args.path_id),
                }
                print(json.dumps(err, ensure_ascii=False))
                print(str(e), file=sys.stderr)
                return 1
            if not args.stamp and not args.json:
                warnings.append(
                    "act --path without --stamp/--json: norm maps via latest perceive "
                    "frame (same convention as drag-norm); prefer --stamp for a canvas window"
                )
        else:
            if step_px is None:
                step_px = 4
        uia_names = getattr(args, "uia_name", None)
        if uia_names:
            flat = []
            for chunk in uia_names:
                flat.extend(str(chunk).split(","))
            uia_names = [x.strip() for x in flat if x.strip()]
        specs = parse_steps(
            steps=args.steps,
            ids=args.ids,
            xy=args.xy,
            norm=args.norm,
            text=None if extras else args.text,
            key=None if extras else args.key,
            drag=None if args.path else args.drag,
            drag_norm=None if args.path else args.drag_norm,
            scroll=args.scroll,
            button=args.button,
            hover=args.hover,
            wait=args.wait,
            step_px=step_px,
            hold=args.hold,
            extras=extras,
            uia_name=uia_names or None,
            replace=bool(args.replace),
            input_via=getattr(args, "via", None),
        )
        if path_specs:
            specs = path_specs + specs
        if args.text and args.slow_type:
            for s in specs:
                if s.get("kind") == "type":
                    s["input_via"] = "keys"
        if not specs and args.hold_key:
            from .act import HOLD_KEY_HINT, hold_key_scope_error

            raise SystemExit(f"{hold_key_scope_error(specs, args.hold_key)}; {HOLD_KEY_HINT}")
        if not specs:
            raise SystemExit(
                "act needs a click, --uia-name, --type/--text, --key, --drag, --drag-norm, --path, or --scroll"
            )
        try:
            frame, path = load_frame(stamp=args.stamp, json_path=args.json, hwnd=args.hwnd)
            out = run_chain(
                frame,
                specs,
                dry_run=not args.go,
                double=args.double,
                gap=args.gap,
                hold_key=args.hold_key,
                method=getattr(args, "method", None) or "input",
                max_age_sec=getattr(args, "max_age", None),
                strict_stamp=bool(getattr(args, "strict_stamp", False)),
                stamp_json_path=str(path) if path else None,
                focus=bool(args.focus),
                stamp_source=("explicit" if (args.stamp or args.json) else "latest") if path else None,
                ime=getattr(args, "ime", None),
            )
        except (MethodError, RuntimeError, FileNotFoundError, OSError, KeyError, ValueError) as e:
            err = {
                "ok": False,
                "error": str(e),
                "dry_run": not args.go,
            }
            if isinstance(e, StampWindowMismatch):
                err["reason"] = "hwnd_mismatch"
            if args.path:
                err["experimental"] = True
                err["path_file"] = args.path
                err["path_id"] = int(args.path_id)
            print(json.dumps(err, ensure_ascii=False))
            print(str(e), file=sys.stderr)
            return 1
        out["json"] = str(path) if path else None
        if out.get("missing_extra"):
            print(json.dumps(out, ensure_ascii=False))
            print(out.get("error") or "missing extra", file=sys.stderr)
            return 3
        if warnings:
            out["warnings"] = (out.get("warnings") or []) + warnings
        if args.path:
            out["experimental"] = True
            out["path_file"] = str(args.path)
            out["path_id"] = int(args.path_id)
            out["step_px"] = int(step_px)
            # Surface ROI from path specs / planned drag if present (E2).
            for ps in path_specs:
                if ps.get("roi") is not None:
                    out["path_roi"] = ps["roi"]
                    break
            for s in out.get("steps") or []:
                if s.get("via") == "drag" and s.get("screen_points"):
                    sp = s["screen_points"]
                    out["screen_points"] = sp
                    out["screen_points_preview"] = subsample_points(sp, 24)
                    if s.get("roi") is not None:
                        out["path_roi"] = s["roi"]
                    break
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok", True) else 1
    if args.cmd == "launch":
        from .launch import launch
        print(json.dumps(launch(app=args.app, exe=args.exe), ensure_ascii=False))
        return 0
    if args.cmd == "path-extract":
        from .path_extract import path_extract_cli_result
        out, code = path_extract_cli_result(
            image=args.image,
            out=args.out,
            mode=args.mode,
            simplify=args.simplify,
            simplify_frac=args.simplify_frac,
            spacing_px=args.spacing_px,
            min_points=args.min_points,
            max_points=args.max_points,
            invert=args.invert,
            largest_only=args.largest_only,
            overlay=args.overlay,
            roi=args.roi,
        )
        print(json.dumps(out, ensure_ascii=False))
        if not out.get("ok"):
            print(out.get("error") or "path-extract failed", file=sys.stderr)
        return code
    if args.cmd == "mcp":
        from .mcp_server import serve
        serve(host=args.host, port=args.port, allowed_hosts=args.allow_host)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
