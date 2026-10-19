# Weights (not in git)

Download separately. Do not commit `*.pt`, `*.onnx`, `*.onnx.bad`, or `*.bin`.

Location: this folder (`<checkout>\weights`) for a source checkout or editable
install (`pip install -e .`); `%LOCALAPPDATA%\cu-perceive\weights` (really
`<CU_PERCEIVE_HOME>\weights`) for a regular install; or wherever
`CU_PERCEIVE_WEIGHTS` points. The YOLO weight defaults to
`<weights>\screenparser\best.pt`.

- `rapidocr/`: optional RapidOCR ONNX (`ch_PP-OCRv4_det_infer.onnx`,
  `ch_PP-OCRv4_rec_infer.onnx`, `ch_ppocr_mobile_v2.0_cls_infer.onnx`). Without
  them the `ocr` extra uses the models bundled in the rapidocr-onnxruntime wheel.
- `screenparser/best.pt`: ScreenParser YOLO11-L, only needed for `--yolo`
  (Hugging Face `docling-project/ScreenParser`). The model card says Apache-2.0,
  but the weight's embedded metadata says AGPL-3.0 (Ultralytics). Override the
  path with `CU_PERCEIVE_YOLO_WEIGHT`.

The `icon_detect/` detector from Enikk is not used and not shipped.
