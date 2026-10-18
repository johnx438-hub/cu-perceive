# Third-party notices

cu-perceive (Ez-Computer-Use) includes or is derived in part from the
following third-party works. Their licenses are reproduced below.

## Piik — window capture approach (MIT)

- Source: https://github.com/TNTcraftHIM/Piik, `native/capture/windows/`
  at commit `6cb0ca1a349f0a62f08ebe11cbf9ca88b13f23f5`
  (`capture_target.cpp`: `CaptureWindowPreview`, `BitmapHasContent`,
  `ValidateWindowTarget`, `ValidateProcessTarget`, `ReadProcessCreationTime`).
- Used in: `cu_perceive/_win32.py` (DWM-thumbnail and PrintWindow fallbacks,
  process creation time), `cu_perceive/capture.py` (WGC -> DWM -> PrintWindow
  order, all-black-frame check, minimized windows refused rather than
  restored), `cu_perceive/windows.py` (`verify_window_identity`:
  HWND + PID + process creation time).
- What was taken: the design and Win32 call sequences, rewritten in Python
  ctypes. No Piik source file is copied verbatim. Piik's 320x180 preview
  scaling was not carried over; captures are full resolution.
- Not used: Piik's `main.cpp`, its encoders (`adaptive_encoder.cpp`,
  `h264_*`) and `vp8_*` files. Those link WebRTC / libvpx.

```
MIT License

Copyright (c) 2026 TNTcraft and Piik contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Enikk — vendored modules (MIT)

- Source: https://github.com/gtt116/enikk at commit `3d7a3f0`.
- Vendored unchanged except for a provenance header, in
  `cu_perceive/_vendor/enikk/`: `ui_parser.py` (OCR pipeline, optional `ocr`
  extra), `mem_track.py`, `game/window.py` (as `window.py`: client region,
  foreground helper). A copy of the license is in
  `cu_perceive/_vendor/enikk/LICENSE`.

```
MIT License

Copyright (c) 2026 Enikk Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Dependencies installed by pip (not bundled)

These are pulled in by `pip` from their own distributions and keep their own
licenses. They are listed for information. If you build an offline bundle
that ships their wheels, include each wheel's own LICENSE files (for
opencv-python that includes `LICENSE-3RD-PARTY.txt`; its bundled FFmpeg is
LGPL-2.1).

| Package | Install group | License |
|---|---|---|
| mcp | core | MIT |
| pillow | core | MIT-CMU |
| opencv-python | core | Apache-2.0 (bundled FFmpeg: LGPL-2.1) |
| numpy | core | BSD-3-Clause and others |
| pywin32 | core (Windows) | PSF-style |
| psutil | core (Windows) | BSD-3-Clause |
| windows-capture | core (Windows x64) | MIT, Copyright (c) 2023 NiiightmareXD |
| rapidocr-onnxruntime | `ocr` | Apache-2.0 |
| onnxruntime | `ocr` | MIT |
| uiautomation | `uia` | Apache-2.0 |
| comtypes | `uia` | MIT |
| ultralytics (+ torch) | `yolo` | **AGPL-3.0** (torch: BSD-style) |

The ScreenParser YOLO weights used by the `yolo` extra are not distributed
here; see `weights/README.md`.
