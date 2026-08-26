from __future__ import annotations

import argparse
import json
import sys


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
        "rect": w.get("rect"),
        "minimized": w.get("minimized"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cu-perceive")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("perceive", help="window/screen -> 0-1000 grid map; OCR optional")
    p.add_argument("--image", help="existing png; default grab primary screen")
    p.add_argument("--out", help="output directory")
    p.add_argument("--hwnd", type=int, help="capture this window client area")
    p.add_argument("--title", help="find window by title substring")
    p.add_argument("--exe", help="find window by exe name substring")
    p.add_argument("--focus", action="store_true", help="bring the window to front before capture")
    p.add_argument("--ocr", action="store_true", help="also run OCR and write .anno.png + item ids")
    p.add_argument("--yolo", action="store_true", help="ScreenParser boxes as items (class name as text). Default off")

    w = sub.add_parser("windows", help="list windows and write a desktop ruler map")
    w.add_argument("--title", help="filter by title substring")
    w.add_argument("--exe", help="filter by exe substring")
    w.add_argument("--hwnd", type=int, help="look up one hwnd")
    w.add_argument("--no-map", action="store_true", help="json only, skip the desktop ruler map")

    a = sub.add_parser("act", help="click ids from a perceive frame (chain, one shot)")
    a.add_argument("--stamp", help="perceive stamp, default latest json")
    a.add_argument("--json", help="path to perceive json")
    a.add_argument("--ids", help="comma ids from anno, e.g. 14,9,16")
    a.add_argument("--xy", help="window pixels: 183,542;400,300")
    a.add_argument("--norm", help="0-1000: 177,752;500,200")
    a.add_argument("--steps", help="mixed chain: id:14,xy:183:542,norm:177:752")
    a.add_argument("--go", action="store_true", help="actually click; default is dry-run")
    a.add_argument("--double", action="store_true")
    a.add_argument("--type", "--text", dest="text", help="paste text (clipboard + Ctrl+V). CLI: --type or --text; MCP field is text")
    a.add_argument("--slow-type", action="store_true", help="per-char SendInput instead of paste")
    a.add_argument("--key", help="tap keys: enter,tab,esc,backspace, or chords like ctrl+c")
    a.add_argument("--drag", help="hold-drag window pixels polyline: x1,y1,x2,y2;x3,y3")
    a.add_argument("--drag-norm", dest="drag_norm", help="hold-drag 0-1000 polyline: 200,300;400,280")
    a.add_argument("--step-px", dest="step_px", type=int, default=4, help="approx pixels between stroke samples")
    a.add_argument("--scroll", help="wheel: 3 / -3 / down:3 / up:3 (negative or down = wheel down)")
    a.add_argument("--button", default="left", help="left|right|middle")
    a.add_argument("--hover", action="store_true", help="move onto target, do not click")
    a.add_argument("--hold", type=float, default=0, help="seconds to hold click before release; with xy/norm/ids = long-press")
    a.add_argument("--hold-key", dest="hold_key", choices=["ctrl", "control", "shift", "alt"], default=None, help="hold modifier across the chain (ctrl/control/shift/alt); not --hold mouse long-press")
    a.add_argument("--wait", help="seconds to wait at end of chain")
    a.add_argument("--gap", type=float, default=0.12)

    L = sub.add_parser("launch", help="start an app from apps.json or an exe path")
    L.add_argument("--app", help="name in apps.json")
    L.add_argument("--exe", help="full exe path")

    m = sub.add_parser("mcp", help="serve streamable-http MCP on this machine")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8771)

    args = parser.parse_args(argv)
    if args.cmd == "perceive":
        from .core import perceive
        out = perceive(
            image=args.image,
            out_dir=args.out,
            hwnd=args.hwnd,
            title=args.title,
            exe=args.exe,
            focus=args.focus,
            ocr=args.ocr,
            yolo=args.yolo,
        )
        print(json.dumps({
            "stamp": out["stamp"],
            "count": len(out["items"]),
            "ocr": out.get("ocr"),
            "yolo": out.get("yolo"),
            "source": out["source"],
            "coord_space": out["coord_space"],
            "hwnd": out["hwnd"],
            "window": out.get("window"),
            "origin": out.get("origin"),
            "map": out.get("map") or out.get("grid"),
            "grid": out.get("grid"),
            "anno": out.get("anno"),
            "json": out["json"],
        }, ensure_ascii=False))
        return 0
    if args.cmd == "windows":
        from .windows import find_window, list_windows, windows_map
        if args.hwnd or args.title or args.exe:
            one = find_window(title=args.title or "", exe=args.exe or "", hwnd=args.hwnd)
            print(json.dumps(_slim_win(one) if one else None, ensure_ascii=False))
            return 0 if one else 1
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
        from .act import load_frame, run_chain
        from .act import parse_steps
        extras = _act_type_key_order(argv)
        specs = parse_steps(steps=args.steps, ids=args.ids, xy=args.xy, norm=args.norm, text=None if extras else args.text, key=None if extras else args.key, drag=args.drag, drag_norm=args.drag_norm, scroll=args.scroll, button=args.button, hover=args.hover, wait=args.wait, step_px=args.step_px, hold=args.hold, extras=extras)
        if args.text and args.slow_type:
            for s in specs:
                if s.get("kind") == "type":
                    s["input_via"] = "keys"
        if not specs:
            raise SystemExit("act needs a click, --type/--text, --key, --drag, or --scroll")
        frame, path = load_frame(stamp=args.stamp, json_path=args.json)
        out = run_chain(frame, specs, dry_run=not args.go, double=args.double, gap=args.gap, hold_key=args.hold_key)
        out["json"] = str(path)
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if args.cmd == "launch":
        from .launch import launch
        print(json.dumps(launch(app=args.app, exe=args.exe), ensure_ascii=False))
        return 0
    if args.cmd == "mcp":
        from .mcp_server import serve
        serve(host=args.host, port=args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
