# cu-perceive

Private dogfood on ARCHER. Pin one window, read the 0-1000 grid map, then act.

One core, two faces:

- CLI: `python -m cu_perceive perceive|windows|act`
- MCP: `python -m cu_perceive mcp` -> http://127.0.0.1:8771/mcp (loopback only; do not bind 0.0.0.0)

8766 may still listen as a leftover instance. Testers use **8771**.

## Contract

- The grid map is the map. Click / type / drag by `norm` (0-1000) or window `xy`.
- OCR and YOLO are optional and off by default.
- Actions default to dry-run. `--go` / `go=true` only when Shawn said so this turn.
- Hardcoded `C:\Users\jawn\...` paths are expected on this machine for now.

## OCR / YOLO

- OCR uses Enikk RapidOCR `UIParser` from the sibling checkout `C:\Users\jawn\src\enikk` (not vendored here).
- YOLO is an optional extra (`--yolo` / `yolo=true`) via ScreenParser (`ultralytics` YOLO11-L). That extra is AGPL; keep it optional.
- Weights are not in git. See `weights/README.md`.

## Run (ARCHER)

    PYTHONPATH=C:\Users\jawn\src\cu-perceive;C:\Users\jawn\src\enikk
    C:\Users\jawn\miniconda3\python.exe -m cu_perceive windows
    C:\Users\jawn\miniconda3\python.exe -m cu_perceive perceive --hwnd N
    C:\Users\jawn\miniconda3\python.exe -m cu_perceive act --stamp STAMP

Default shots: `C:\Users\jawn\agent-bus\archive\shots\perceive\`

WSL wrapper: `bin/cu-perceive.sh`

See `SKILL.md` for the operator contract.
