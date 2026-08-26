# Weights (not in git)

Download separately. Do not commit `*.pt`, `*.onnx`, `*.onnx.bad`, or `*.bin`.

Expected local layout:

- `rapidocr/` — RapidOCR ONNX (det / rec / cls). Used when OCR is on, via Enikk UIParser.
- `screenparser/best.pt` — optional ScreenParser YOLO11-L. Only needed for `--yolo`.
- `icon_detect/` — leftover / quarantined Enikk icon detector; not used.

cu-perceive does not vendor Enikk. Keep `C:\Users\jawn\src\enikk` as a sibling checkout.
