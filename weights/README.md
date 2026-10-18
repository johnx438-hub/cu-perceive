# Weights (not in git)

Download separately. Do not commit `*.pt`, `*.onnx`, `*.onnx.bad`, or `*.bin`.

Location: `./weights` in a source checkout, else `%LOCALAPPDATA%\cu-perceive\weights`,
or wherever `CU_PERCEIVE_WEIGHTS` points.

- `rapidocr/`: optional RapidOCR ONNX (`ch_PP-OCRv4_det_infer.onnx`,
  `ch_PP-OCRv4_rec_infer.onnx`, `ch_ppocr_mobile_v2.0_cls_infer.onnx`). Without
  them the `ocr` extra uses the models bundled in the rapidocr-onnxruntime wheel.
- `screenparser/best.pt`: ScreenParser YOLO11-L, only needed for `--yolo`
  (Hugging Face `docling-project/ScreenParser`). The model card says Apache-2.0,
  but the weight's embedded metadata says AGPL-3.0 (Ultralytics). Override the
  path with `CU_PERCEIVE_YOLO_WEIGHT`.

The `icon_detect/` detector from Enikk is not used and not shipped.
