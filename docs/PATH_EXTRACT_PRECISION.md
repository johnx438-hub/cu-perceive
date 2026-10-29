# Path Extract → Norm 精度调研（实验）

> 2026-09-06 · maintainers  
> 接昨晚 Paint 圆 `--go` 通了之后的「轮廓提取映射更精细」。  
> **调研笔记**；**E1 landed**（弧长重采样 + 相对 simplify + overlay）；**E2 landed**（手动 `--roi` + act 认 `source.roi`）。  
> 基线：`cu_perceive/path_extract.py` · 合同：`docs/PATH_DRAW_EXPERIMENTAL_V0.md`

## 0. 基线在干什么

```
imread → gray → (optional invert) → Otsu → findContours(EXTERNAL, NONE)
→ 最长周长轮廓 → approxPolyDP(eps=simplify 默认 1.5px) → 可选均匀抽稀到 max_points
→ 像素 (x/w*1000, y/h*1000) 四舍五入成 int → 连续同点去重 → path.json
```

act 侧：norm → stamp 窗客户区 → screen；`step-px` 默认 2 再插密后拖。

## 1. 误差从哪来（按层拆）

| 层 | 现状 | 症状 / 量级 |
|---|---|---|
| **A. 语义域** | 对**整张图**提轮廓（Paint=整客户区含丝带） | 圆相对「窗」居中，不是相对「画布」；笔会画偏到 chrome |
| **B. 二值化** | 全局 Otsu + 实心前景 | 细线稿 / 灰抗锯齿 / 复杂底容易断或胖一圈 |
| **C. 几何表示** | `approxPolyDP` 按角点抽稀 | 圆变成折线多边形；曲率高处点不够，直线段浪费点 |
| **D. 量化** | 先 round 到 0–1000 int | 窗宽 ~2000px 时 1 norm ≈ 2px；再经 drag 插值，锯齿叠锯齿 |
| **E. 播放** | `step-px` 在 screen 插密 | 只补执行密度，**不**补提取几何；歪了的折线插密仍歪 |
| **F. 闭环** | 无「提完叠回原图」的 QA 图 | 肉眼难分是提取差还是映射/chrome 差 |

昨晚合成圆成功 = A 在空白画布上碰巧还能看、C/D 对大圆够用；要「更精细」必须动 A/C/D（B 看素材）。

## 2. 细化选项（只 OpenCV / 几何，不 OCR·YOLO）

### P0 — 高收益、贴 CU

1. **Canvas ROI（映射域）**  
   - 输入：可选 `--roi left,top,w,h`（窗客户区像素）或后续「自动估画布」（Paint 白区最大矩形，实验另开）。  
   - 提取只在 ROI 内做；norm 相对 **ROI**，act 时 origin = stamp.origin + ROI。  
   - 直接消掉丝带/工具栏污染。

2. **弧长重采样（几何）**  
   - 轮廓先轻度平滑 / 或不 `approxPolyDP` 砍角，再按弧长等距取 N 点（或目标点间距 `spacing_px`）。  
   - 圆/曲线更匀；比「角点 DP + 均匀下标抽稀」更像笔触。

3. **相对 simplify**  
   - `eps = simplify_frac * perimeter`（或 `* diagonal`），替代绝对 1.5px。  
   - 大图小图行为一致。

4. **提取叠图 QA**  
   - 输出 `*.overlay.png`：原图 + 折线；可选再出 act dry-run 的 screen 折线叠桌面/窗图。  
   - 验收「精细」靠图，不靠感觉。

### P1 — 精度轴

5. **延迟量化**  
   - path.json 存 float norm（或 0–10000 int）；act 映射时再 round 到像素。  
   - 减一层提前量化。

6. **亚像素 / 收缩**  
   - `cv2.cornerSubPix` 或形态学后再轮廓；对填充剪影可 `erode` 半笔宽，减轻「轮廓在墨外沿」发胖。

7. **线稿模式 `edges` / `skeleton`**（spec 已占位）  
   - Canny / 阈值 + 细线骨架 → 开轮廓（`closed:false`）。  
   - 现 EXTERNAL+Otsu 吃饱实心，不吃单像素描边。

### P2 — 观测环（Astra 向，另文）

8. 画后截画布 vs 目标差分 → 再提/再补一笔。依赖 ROI+叠图先稳。

## 3. 建议实验顺序（咱俩可跟）

| # | 实验 | 成功标准 | 改动量 |
|---|---|---|---|
| E1 | 弧长重采样 + 相对 eps；合成圆/星 | overlay 更圆；点数可控（如 spacing=4px） | **landed** (`--spacing-px` / `--simplify-frac` / `--overlay`) |
| E2 | 手动 `--roi` 钉 Paint 画布再提/再画 | 笔迹落在画布内，不碰丝带 | **landed** (`--roi L,T,W,H` + `source.roi` → act) |
| E3 | float/细 norm + QA overlay 默认出 | 同图对比 int vs float 残差 | schema 小扩展 |
| E4 | `edges` 线稿一条直线/折线 | 开路径 dry-run 合理 | 新 mode |

**先做 E1+叠图**（不碰鼠标）；E2 要 Paint 时再 `--go`。

## 4. 非目标（维持）

- OCR/YOLO 当笔触  
- agent 心算密点  
- 默认进主 CU 路径  
- 游戏挂机

## 5. 合同（E1 + E2 landed）

```text
cu-perceive path-extract --image X --out path.json \
  [--spacing-px 4] [--simplify-frac 0.002] [--simplify 1.5] \
  [--roi L,T,W,H] [--overlay out.png]
```

Precedence / method（E1 + E2）：

- `--simplify-frac F`（F>0）→ `eps = F * perimeter`；否则用绝对 `--simplify`（默认 1.5；0=跳过 DP）。
- `--spacing-px N` 已设 → 可选轻 DP 后按弧长等距重采样；`stats.method = "arc_resample"`，`stats.spacing_px = N`。
- 未设 `--spacing-px` → 兼容旧路径 `approxPolyDP`；`stats.method = "approx_poly"`。
- `--overlay out.png`：成功时才写（**整图** + 可选 ROI 矩形 + 折线 + 顶点点；折线已换算回全图像素）。
- `--roi L,T,W,H`：**E2 landed** — 图像像素（图左上=0,0）；提轮廓前裁切；norm 相对 ROI w/h；非法/越界/空 ROI fail-closed。

`path.json`：

```jsonc
"source": { "image": "...", "width": Wfull, "height": Hfull, "mode": "contour", "roi": [L,T,W,H] },
"stats": { ..., "spacing_px": 4, "method": "arc_resample" | "approx_poly" }
```

（无 `--roi` 时不写 `source.roi`。）

act：有 `source.roi` 时 `norm → ROI 局部像素 → +ROI offset → stamp client → screen`；无 roi 行为不变。Dry-run `screen_points` / `path_roi` 反映偏移。

## 6. 结论

精细度瓶颈不在「会不会提轮廓」，而在 **(1) 提在哪张语义图上 (ROI)**、**(2) 点怎么沿轮廓分布 (弧长)**、**(3) 何时量化到 0–1000**。  
播放 `step-px` 救不了提取域错误。  

E1 已落地（`--spacing-px` 弧长重采样、`--simplify-frac` 相对 eps、`--overlay` QA；未设 spacing 仍走 `approx_poly`）。**E2 已落地**（`--roi L,T,W,H` → `source.roi`；act `--path` 映射加 ROI 偏移）。下一步默认：**E3 float/细 norm** 或 Paint 上人工估 ROI 再 `--go`（勿默认 `--go`）。
