# cu-perceive Path Draw — Experimental Mode Spec v0

> 2026-09-06 · maintainers  
> **实验模式 only** —— 默认 CU 流程不变；不进日常 `windows → perceive → act` 主路径。  
> 目标：剪影/线稿 → 视觉软件提折线 → `drag-norm` 连播；**禁止** OCR/YOLO 认笔触；**禁止** agent 心算密点。

## 一句话

`path_extract(image) → path.json (0–1000 polylines) → act --path / plan.drag → dry-run 预览 → --go 连播`

观测环（可选第二圈）：截画布 → 与目标差分 → 再提/再播；差分算法另开，本 spec 只钉提取 + 执行。

## 与现有合同的关系

| 层 | 已有 | 本实验新增 |
|---|---|---|
| 单帧手势 | `act` + `drag-norm` / `drag`（按住折线） | 从 **path 文件** 喂折线；实验 flag |
| 跨帧 Plan | `PLAN_V1` `ComputerPlan` | 可选 `op: "draw_path"`（后置）；v0 可先 CLI only |
| 感知 | `perceive` 格子图 / 可选 OCR·YOLO | **不**用 OCR/YOLO；另加 `path_extract` |
| 安全门 | 默认 dry-run；`--go` 才动 | 不变；实验模式仍受 `--go` 约束 |

## 非目标（写死）

- OCR / YOLO / ScreenParser 当笔触提取器
- agent 纯脑力报成百上千个坐标
- 游戏挂机、绕检测、无人值守乱画
- 默认开启或替换现有 `act` 语义
- 云电脑桌面（仍只本机 Windows）

## `path_extract`（视觉软件，非 LLM）

### 输入

```jsonc
{
  "image": "path/to.png",       // 或 stamp 的 png/map；优先高对比剪影/线稿
  "mode": "contour",            // contour | skeleton | edges  （v0 先实现 contour）
  "as_norm": true,              // 输出 0–1000（相对该图宽高）
  "simplify": 1.5,              // Douglas–Peucker 类抽稀强度（实现可选）
  "min_points": 8,
  "max_points": 400,            // 单条折线上限，防炸
  "invert": false,              // 白底黑线时可反相
  "largest_only": true          // v0：只留最长外轮廓
}
```

### 输出 `path.json`

```jsonc
{
  "version": 1,
  "experimental": true,
  "source": { "image": "...", "width": 1920, "height": 1080, "mode": "contour" },
  "coord_space": "norm",        // 0–1000 relative to source image
  "paths": [
    {
      "id": 0,
      "closed": true,
      "points": [[120, 340], [125, 338], "..."]   // [nx, ny] each 0..1000
    }
  ],
  "stats": { "n_paths": 1, "n_points": 86 }
}
```

实现建议（Windows 主机，可放 `cu_perceive/path_extract.py` 或 `scripts/`）：

- OpenCV：灰度 → 阈值/自适应 → `findContours` → 抽稀 → 映射到 0–1000
- **不**依赖 RapidOCR / ScreenParser / YOLO weights
- CLI：`cu-perceive path-extract --image X --out path.json`

## 执行：`act --path`（实验）

```text
cu-perceive act --path path.json [--path-id 0] [--stamp STAMP] [--go]
# dry-run 默认：只解析 screen 折线，不 SendInput
```

语义：

1. 若有 `--stamp`：把 path 的 norm 映射到 **该 stamp 窗客户区**（与现 `drag-norm` 同：norm→window→screen）。  
   无 stamp：视为整桌/`coord_space=screen` 的 norm（少用；文档警告）。
2. 每条 path → 一次 **按住折线**（复用 `drag_path` / `run_chain` 的 `kind:drag`）。
3. `closed: true`：自动把首点再 append 一次闭合（可选 flag `--close`）。
4. `step-px`：实验默认更密（如 `2`），可覆盖；慢放用更大 `gap` 或 path 级 `pace_ms`。

Dry-run 必须返回将走的 `screen_points`（可抽稀预览），**零鼠标按下**。

### 与 Plan v1 的挂钩（可后置）

```jsonc
{
  "op": "draw_path",
  "experimental": true,
  "stamp_ref": "canvas",
  "path_file": "path.json",
  "path_id": 0,
  "step_px": 2
}
```

v0 可不实现；合同先占位。

## 实验模式开关

- CLI/MCP 显式：`path-extract` 子命令；`act --path`  
- 或环境 / 配置：`CU_EXPERIMENTAL_DRAW=1` 才注册 MCP 工具 `path_extract` / `act_path`  
- Skill / 文档标 **Experimental**；日常 skill 主路径不提画画

## 失败语义

| 情况 | 行为 |
|---|---|
| 图读失败 / 无轮廓 | `ok:false`，不生成空 path 冒充成功 |
| points < min_points | `ok:false` 或警告+拒执行 |
| stamp 最小化哨兵 | 与现 `act` 相同：拒点 |
| 无 `--go` | 只预览 |
| path 点超 max | 抽稀到上限并在 stats 标明 `truncated:true` |

## 验收（实验）

1. 一张黑白剪影圆/星 → `path-extract` → 点数在 8–400 → dry-run 折线合理。  
2. 画图软件或空白窗：`--path` + stamp dry-run 出 screen 折线；操作者确认 `--go` 后出现可见笔迹（或拖选残影）。  
3. 故意塞照片给 YOLO/OCR 路径：**不**走本实验；文档写明。  
4. WSL agent 收到 path 文件内只有 norm 数组 + 相对说明；若带绝对图路径，遵守 [godot-mcp-paths](sand-workflow:godot-mcp-paths) 同类纪律（Win↔`/mnt/c`），**不**改 path 点语义。

## 实现顺序

1. Spec 落地（本文）  
2. `path_extract` CLI + 单测（合成圆） — **landed** (`path_extract` CLI + synth circle smoke)  
3. `act --path` 接现有 `drag` — **landed** (`act --path` dry-run → drag-norm / `drag_path`)
4. 可选 MCP 工具（实验 flag）  
5. 一篇短 skill：`cu-path-draw`（experimental）

## 与 Astra / CSS 画画的边界

- **本实验** = 鼠标轨迹执行 + CV 提路径  
- **CSS/代码复刻** = 另一条（写代码 → 截预览 → vision 改代码），**不**塞进 cu-perceive  
- Astra 类优势在观测环；本模块只提供「可回放的手」和「非 LLM 的眼（CV）」
