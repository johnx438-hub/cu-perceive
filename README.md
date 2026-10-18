# cu-perceive (Ez-Computer-Use)

Windows computer use. Pin one window, read its 0-1000 grid map, then act.

One core, two faces:

- CLI: `cu-perceive perceive|windows|act` (or `python -m cu_perceive ...`)
- MCP: `cu-perceive mcp` -> http://127.0.0.1:8771/mcp (loopback only; do not bind 0.0.0.0)

## Contract

- The grid map is the map. Click / type / drag by `norm` (0-1000) or window `xy`.
- Perceiving a window does **not** steal focus. The window is captured where it
  is, also when other windows cover it. `--focus` / `focus=true` raises and
  restores it first (old behaviour).
- Minimized windows are reported (`ok=false`, `minimized=true`), not restored,
  unless `--focus` / `focus=true` is passed.
- Every window stamp records `identity = {hwnd, pid, ctime}` (process creation
  time). `act` re-checks it before planning and before every live step. A
  closed window whose HWND was reused by another process fails closed.
- OCR, UIA and YOLO are optional extras, off by default.
- Actions default to dry-run. `--go` / `go=true` only when the user said so.

## Install

Core (grid map, background capture, act with `input` / `post`):

    python -m pip install "cu-perceive @ git+<repo-url>"
    # or from a checkout:
    python -m pip install -e .

Optional extras, each loaded on first use. A missing extra is never
auto-installed; the error prints the exact command for your install:

| Extra | Enables | Command (from a checkout) |
|---|---|---|
| `ocr` | `--ocr` text ids (RapidOCR) | `python -m pip install -e ".[ocr]"` |
| `uia` | `--uia`, `act --method uia|auto`, `--uia-name` | `python -m pip install -e ".[uia]"` |
| `yolo` | `--yolo` ScreenParser icons. **AGPL-3.0**, pulls torch (large) | `python -m pip install -e ".[yolo]"` |

YOLO weights are not in git; see `weights/README.md`.

## Window capture

`perceive --hwnd N` grabs the client area without touching z-order. Backends,
tried in order (`--capture auto`, the default):

1. `wgc`: Windows.Graphics.Capture via the
   [`windows-capture`](https://pypi.org/project/windows-capture/) wheel (x64).
   It asks for a borderless capture on Windows 11 / build 20348+. Older
   builds may flash the yellow capture border.
2. `dwm`: DWM thumbnail rendered into an off-screen, non-activating tool window.
3. `printwindow`: `PrintWindow(PW_RENDERFULLCONTENT)`, then flag 0.

A grab that is all black counts as a miss and the next backend is tried.
`--capture wgc|dwm|printwindow` forces one backend. `--capture screen` is the
legacy screen-region grab and needs `--focus`. The JSON reports which backend
won under `capture.backend`, with every attempt in `capture.attempts`.
Windows on another virtual desktop, or cloaked windows, may come back blank.

## Configuration

Nothing machine-specific is hard-coded. Defaults and overrides:

| Setting | Default | Env var |
|---|---|---|
| Data dir | `%LOCALAPPDATA%\cu-perceive` | `CU_PERCEIVE_HOME` |
| Stamp / shot dir | `<data dir>\shots\perceive` | `CU_PERCEIVE_OUT` |
| Weights dir | `./weights` in a checkout, else `<data dir>\weights` | `CU_PERCEIVE_WEIGHTS` |
| ScreenParser weight | `<weights>\screenparser\best.pt` | `CU_PERCEIVE_YOLO_WEIGHT` |
| Quick-launch list | `<data dir>\apps.json`, else packaged `apps.default.json` | `CU_PERCEIVE_APPS` |
| Extra MCP Host names (e.g. a tailnet name behind a proxy) | none | `CU_PERCEIVE_ALLOWED_HOSTS=a,b` or `mcp --allow-host a` |
| WSL distro for UNC paths | `Ubuntu` | `CU_PERCEIVE_WSL_DISTRO` |
| Capture backend | `auto` | `CU_PERCEIVE_CAPTURE` |

## Run

    cu-perceive windows
    cu-perceive perceive --hwnd N
    cu-perceive act --stamp STAMP --norm 500,500          # dry-run
    cu-perceive mcp --port 8771

WSL wrapper: `bin/cu-perceive.sh`. Set `CU_PERCEIVE_PYTHON` to the Windows
python.exe (WSL path).

See `SKILL.md` for the operator contract and `THIRD_PARTY_NOTICES.md` for
third-party licenses.

## Tests

    python -m pip install -e ".[test]"
    python -m pytest

The tests mock Win32 and run on Linux. The Windows capture paths need a manual
check on a real Windows machine.
