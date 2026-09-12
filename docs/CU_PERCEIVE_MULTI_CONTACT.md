# cu-perceive 多场景接触（设计）

> 2026-09-06 · Chitose / Shawn  
> **设计合同；S1 `method=post` + S2 `method=uia` + S3 `method=auto` + S4 `perceive --uia` 已落地（实验）。**  
> 输入：cu-perceive 现合同 · path-draw 实验 · open-cu 非侵入点击调研 · Codex `computer_use` 策略旁证  
> 档案：`open-cu-windows-vs-cu-perceive.md` · `open-cu-noninvasive-click.md` · `PATH_EXTRACT_PRECISION.md`

## 1. 目标

让 **同一个** cu-perceive 入口，按窗口/控件形态选接触方式，而不是「永远 SendInput」或「另起一个 MCP」。

硬约束（不变）：

- 默认 **dry-run**；真动手要当次 `--go` / `go=true`
- 钉一窗 + 格子图仍是主地图（无障碍树是**可选增强**，不是替换）
- 不写游戏挂机 / 绕检测
- 失败 **fail-closed**；一种 method 失败 **不静默**退回另一种（可显式 `fallback=` 另议）

## 2. 已知模式（积木）

| 积木 | 我们已有 / 已看清 | 擅长 | 苦手 |
|---|---|---|---|
| **A. 格子 + SendInput** | cu-perceive 主路径 | 画布、自绘、CEF/游戏感、无 UIA | 抢光标；多 agent 抢桌面 |
| **B. 路径笔画** | `path_extract` + `act --path` + ROI | 连续轨迹、剪影/线稿 | 依赖 A 或将来的 post-drag |
| **C. 非侵入点击** | open-cu：UIA pattern → HWND `PostMessage` | 原声控件、Office、经典 Win32 | Chromium/Electron/DX/画布 |
| **D. 可选视觉 id** | OCR / YOLO | 有字按钮、粗图标 | 牌面/重叠画 |
| **E. 策略门** | Codex `computer_use` allowlist 旁证 | 按 exe/AUMID 限权 | 不是执行器本身 |

## 3. 场景 → 推荐接触（决策表）

| 场景例子 | 先感知 | 默认 act | 备选 |
|---|---|---|---|
| 记事本 / 资源管理器 / 标准对话框 | 格子（可加粗 UIA 节点叠 id） | **`method=uia`** 或 `post` | `input` 若无控件 |
| Office / 重 UIA 套件 | 同上 | **`uia`** | `post` → `input` |
| Paint 画布 / 设计工具客户区 | 格子 + 可选 ROI | **`input`**（点/拖）· path 用 B | `post` 多半无效 |
| Chrome / Edge / 内嵌 CEF | 格子为主；**不要**指望 post | 默认 **`input`**；页面 a11y 好时可 `perceive --uia` 点有名节点 | 壳按钮常见；**正文 UIA 取决于页面**（非保证） |
| Electron 壳 | 格子 | **`input`** | 壳上菜单偶发 UIA |
| 游戏 / DirectX | 格子（够切）；YOLO 谨慎 | **`input`** + 人工 `--go` | 非侵入基本放弃 |

「浏览器最佳」不成立；「系统原声」≈ **UIA 友好控件**，不是凡是微软图标都灵。Chrome **正文**偶有完整 a11y 树（可 Invoke/Select），仍按页碰运气，别当默认。

## 4. 统一 act 合同（草案）

保持现有定位字段：`--stamp` / `norm` / `xy` / `ids` / `drag-*` / `--path`。

新增（实验）：

```text
--method input|post|uia|auto     # 默认 input（兼容现状）
# auto 建议语义（可讨论）：
#   若 stamp 上命中 UIA 可 Invoke 的元素 → uia
#   elif 窗类/进程在「post 白名单启发」→ post
#   else input
```

规则：

1. **`input`**：现 SendInput（唯一默认，行为零惊喜）。
2. **`post`**：对钉住 hwnd（或显式 child）`PostMessage` 鼠标序列；坐标 client 相对；**不** `SetCursorPos`。
3. **`uia`**：先按点/ id 解析 AutomationElement，再 Invoke/Toggle/Select；解析失败 → `ok:false`（除非用户写了 `fallback=input` 且 `--go`）。
4. dry-run：三种都只回报「将用的 method + 目标描述 + screen/client 点」，零副作用。
5. **path / drag**：v0 仍仅 `input`（连续轨迹 PostMessage 极不可靠）；单点 click 才开 post/uia。

MCP 对齐同名扁平字段 `method`。

## 5. 感知增强（可选，不挡主路径）

- **`perceive --uia`（实验）**：在格子图旁返回精简 UIA 节点（name/control_type/bbox→norm），供 `ids` 或 `method=uia` 使用。
- 默认仍关；体量大、慢，和 OCR/YOLO 同级「按需」。
- 与 D 的关系：有字优先 OCR id；结构按钮优先 UIA；自绘仍看格子。

## 6. 策略门（可后置）

从 Codex 旁证借形状，不抄实现：

```toml
# 示意，非现配置
[cu_perceive.access]
default = "allow"  # or deny
# exes: [{ path_glob, methods=["uia","post","input"] }]
```

用途：某 exe 禁止 `input`（只许非侵入）、或整类拒绝。与 `--go` 正交：门开了仍要 `--go`。

## 7. 落地顺序（建议）

| 步 | 内容 | 验收 |
|---|---|---|
| S0 | 本文合同 + skill 一段「场景选路」 | Shawn 点头 |
| S1 | `act --method post` 单点（Notepad 按钮/菜单） | **已落地** dry-run 字段齐；`--go` PostMessage，不 SetCursorPos/不静默退 SendInput |
| S2 | `act --method uia` + 最小元素解析 | **已落地** dry-run 报 name/control_type/patterns；`--go` Invoke/Toggle/Expand/Select，失败不退 post/input |
| S3 | `method=auto` 启发（有 uia hit+pattern → uia else input；永不选 post） | **已落地** dry-run 报 `chosen_method`；`--go` 只跑所选路径 |
| S4 | 可选 `perceive --uia` | **已落地** 格子旁 `uia_nodes`+norm；默认关；缺包/无 hwnd fail-closed |
| S5 | path 仍 input；文档写清 post 不做笔画 | — |

**不做（本阶段）：** 把 open-cu 整仓塞进 cu-perceive；Win 上 sky_click；默认改成 auto 抢行为。


### S1 落地笔记（2026-09-06）

- CLI `--method input|post`（默认 `input`）；MCP 同名 `method`。`auto` 已落地见 S3。
- `post` 仅单点 left/right(/middle) click（`norm`/`xy`/`ids`）。与 drag/path/scroll/type/key/hover/hold/hold_key 组合 → fail-closed，提示用 `input`。
- 实现：`ScreenToClient(hwnd)` → `PostMessage` `WM_MOUSEMOVE` + DOWN/UP；client `lParam`；无 `SetCursorPos`；失败不退 `SendInput`。
- dry-run：回报 `method=post`、hwnd、`client`/`screen`、`post_messages[]`；`--go` 才真正 PostMessage。



### S2 落地笔记（2026-09-06）

- CLI `--method uia` / MCP `method=uia`：单点 **left** click（`norm`/`xy`/`ids`）。right/middle / drag/path/scroll/type/hold → fail-closed。
- 解析：`uiautomation.ControlFromPoint(screen_x, screen_y)`；stamp hwnd 与 element `GetTopLevelControl` 不一致 → ok:false。
- 动作优先：Invoke → Toggle → ExpandCollapse.Expand → SelectionItem.Select。无 pattern → ok:false，**不**退 post/input。
- dry-run：`method=uia` + `uia{name,control_type,patterns,preferred_action,bbox,top_hwnd}`；`--go` 才 Invoke。
- 实现：`cu_perceive/uia.py` + `act.enrich_uia_steps` / `run_chain`。



### S3 落地笔记（2026-09-06）

- CLI `--method auto` / MCP `method=auto`：仅单点 **left** click（`norm`/`xy`/`ids`）。drag/path/scroll/type/hold/right → fail-closed，提示 `method=input`。
- 选择（v0）：UIA hit-test 计划屏幕点 → 若 element 在 stamp hwnd 下（同 S2 top-level 检查）且有 preferred action（Invoke/Toggle/Expand/Select）→ **`chosen_method=uia`**；否则 **`input`**。**永不选 `post`**。
- dry-run：`method=auto` + `chosen_method=uia|input` + 相关 `uia{...}` 探针字段。`--go` 只执行所选路径；若已选 uia 且 Invoke 失败 → `ok:false`（**不**中途退 input）。
- 实现：`uia.choose_auto_method` / `probe_at_screen` + `act.enrich_auto_steps` / `run_chain`。



### S4 落地笔记（2026-09-06）

- CLI `perceive --uia` / MCP `perceive_window` 字段 `uia=true`（默认 false）。关时行为不变，不走 UIA 树。
- 开时：在 stamp hwnd 下走 UIA 树，返回精简 `uia_nodes: [{id,name,control_type,patterns?,norm:[nx1,ny1,nx2,ny2],clickable?}]`；norm 相对窗口 client / 格子图 0-1000（与 grid / `act --norm` 同约定）。
- 有界：默认 max 200 节点、depth 16；跳过 IsOffscreen / 零面积；优先 Invoke/Toggle/Expand/Select 或 Button/MenuItem/Edit/TreeItem/TabItem/ListItem/CheckBox/Hyperlink 等。
- **不**替换格子图；`uia_nodes` 为附加 JSON 字段。缺 `uiautomation` / 无 pinned hwnd → `ok:false`+error（fail-closed）；`uia=false` 不 import 爆炸。
- 实现：`uia.screen_bbox_to_norm` / `collect_uia_nodes` + `core.perceive(uia=...)`。

## 8. 和「灵活」的关系

灵活 = **显式 method + 场景表 + 同一套钉窗/格子/dry-run**，不是自动猜到完美。

agent 侧 skill 写死：

1. 先 `windows` / `perceive` 看是控件 UI 还是画布/网页壳  
2. 查上表选 `method`  
3. dry-run → Shawn `--go`  
4. 画布/轨迹走 A+B；原声控件优先 C  

## 9. 结论

cu-perceive 保持「眼睛=格子、手=可插拔」。**post/uia/auto 单点已落地**，path 与画布继续真鼠标；浏览器别硬吃非侵入（默认 `input`）。**S4 `perceive --uia` 已落地**——页面暴露有名节点时可用，未命中回 `input`；下一步若开工：S5 path 真鼠标 / 可选 UIA id→act。

## S5 (doc)

**landed as documentation only (2026-09-06):** path / drag stay method=input only; post/uia/auto reject them fail-closed. No code change beyond existing S1–S3 guards.
