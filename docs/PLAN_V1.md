# cu-perceive Plan v1 — 链式手势合同

> 2026-09-05 · maintainers  
> 目标：从「一步一截一点」升到「感知出点位 → 规划出链 → 执行吃一条路径」  
> 第一刀：**只加 plan 文件合同 + `act --plan` 批跑/dry-run**；不重写感知，不加挂机，不加默认 multimodal。

## 与现状的关系

| 层 | 今天已有 | v1 新增 |
|---|---|---|
| 单帧手势链 | `act --norm/--xy/--ids/--key/--type/--drag/--scroll` → `parse_steps` → `plan_steps` → `run_chain` | 不变；仍是 **一个 stamp** 上的 steps |
| 跨帧任务链 | 无 | **ComputerPlan** JSON：可含 `perceive` / `act` / `wait` / `assert` / `reperceive` |
| 真动手门 | 默认 dry-run；`--go` / `go=true` 才点 | **整份 plan 共用一道门**：缺 `go` 则整链预演、零输入 |

实现入口建议（实现时合同相同）：

```text
cu-perceive act --plan path\to\plan.json          # 默认 dry-run
cu-perceive act --plan path\to\plan.json --go     # 真跑
# MCP: act(plan=<json或路径>, go=false)
```

单 stamp 的旧 flags 与 `--plan` **互斥**：有 `--plan` 时忽略 `--ids/--norm/...`（避免两套语义打架）。

## ComputerPlan schema

```jsonc
{
  "version": 1,
  "goal": "optional human string — for logs only",
  "go": false,                 // optional; CLI/MCP --go 覆盖文件内 go
  "defaults": {
    "gap": 0.12,               // act 步间间隔（秒）；动画 UI 可 0.4
    "hold_key": null,          // "ctrl"|"shift"|"alt"|null — 仅作用于该 act 步内 run_chain
    "on_error": "stop"         // "stop" | "continue"（v1 实现 stop；continue 留给 v1.1）
  },
  "steps": [ /* PlanStep[] */ ]
}
```

### PlanStep 判别（`op` 必填）

#### 1. `perceive` — 钉窗再看，产出/刷新 stamp

```jsonc
{
  "op": "perceive",
  "hwnd": 123456,              // 与 title/exe 三选一；0/省略 = 主屏（少用）
  "title": "",
  "exe": "",
  "focus": true,               // 默认 true（restore/activate 双拍纪律仍走现有 core）
  "ocr": false,
  "yolo": false,
  "as": "main"                 // 可选别名；后续 act 可用 stamp_ref
}
```

结果：写入运行时 `stamps[as|latest]`。失败 → plan 失败（窗找不到 / 最小化哨兵等沿用现语义）。

#### 2. `act` — 在某个 stamp 上跑一条手势链（复用现有 `run_chain`）

```jsonc
{
  "op": "act",
  "stamp": "20260905-203000",  // 显式 stamp；与 stamp_ref 二选一
  "stamp_ref": "main",         // 指向先前 perceive 的 as；都缺则用 latest
  "gap": 0.12,                 // 覆盖 defaults.gap
  "hold_key": "ctrl",          // 覆盖 defaults；本步链内按住
  "double": false,
  "actions": [
    { "kind": "norm", "norm": [177, 752], "button": "left", "hold": 0, "hover": false },
    { "kind": "key", "key": "ctrl+a" },
    { "kind": "type", "text": "hello", "input_via": "paste" },
    { "kind": "key", "key": "enter" },
    { "kind": "scroll", "notches": -3 },
    { "kind": "drag", "space": "norm", "points": [[200,300],[450,280]], "step_px": 4 },
    { "kind": "wait", "sec": 0.5 },
    { "kind": "xy", "xy": [183, 542] },
    { "kind": "id", "id": 14 }
  ]
}
```

`actions[]` 字段与现有 `parse_steps` / `plan_steps` 的 spec **同构**（kind 名对齐）。  
scroll+位置仍隐含 hover（现纪律）。  
输入框改字：拆 `key:ctrl+a` 再 `type`（现 skill 坑，写入 plan 示例，不靠隐式）。

#### 3. `wait` — 纯等待（不绑 stamp）

```jsonc
{ "op": "wait", "sec": 1.0 }
```

#### 4. `assert` — 轻量闸门（v1 最小集）

```jsonc
{
  "op": "assert",
  "stamp_ref": "main",         // 或 stamp
  "hwnd_alive": true,          // 可选：Win32 查 stamp.hwnd 仍存在且非最小化哨兵
  "title_contains": "minimal", // 可选：当前窗标题子串
  "ocr_contains": ""           // 可选：需该 stamp 曾 ocr=true；否则 skip+warn（不静默当通过）
}
```

任一失败 → 按 `on_error`（v1=`stop`）。

#### 5. `reperceive` — 同窗再截，换新 stamp（坐标易漂时用）

```jsonc
{
  "op": "reperceive",
  "from": "main",              // 取 hwnd/title/exe；缺则 latest stamp 的 hwnd
  "as": "main",                // 覆盖同名别名
  "ocr": false,
  "yolo": false,
  "focus": false               // 默认 false：已在前台则静默单拍；true 走双拍
}
```

## Dry-run 语义

- **整 plan dry-run**（默认）：
  - `perceive` / `reperceive`：**真截图**（只看不动；否则后续 act 无法解坐标）。返回里标 `"captured": true, "acted": false`。
  - `act`：只跑 `plan_steps`，**不** `click/type/key/drag`；返回解析后的 screen 点。
  - `wait`：跳过实睡，记录 `would_wait_sec`。
  - `assert`：**真检查**（否则 dry-run 假绿）。
- **`--go`**：上述全部真执行；`wait` 实睡。

返回形状（stdout / MCP JSON）：

```jsonc
{
  "ok": true,
  "dry_run": true,
  "version": 1,
  "goal": "...",
  "steps": [
    { "i": 0, "op": "perceive", "ok": true, "stamp": "...", "as": "main", "captured": true },
    { "i": 1, "op": "act", "ok": true, "stamp": "...", "actions": [ /* plan_steps 输出 */ ] },
    { "i": 2, "op": "assert", "ok": true, "checks": { "hwnd_alive": true } }
  ],
  "stopped_at": null,
  "error": null
}
```

## 失败语义（v1）

| 情况 | 行为 |
|---|---|
| schema 非法 / 未知 `op` / `kind` | 启动前拒跑；`ok:false`，零副作用 |
| `--plan` 与旧 click flags 同用 | 拒跑 |
| stamp 缺失 / id 不在帧 / 最小化哨兵 | 该步失败；**已完成步骤不回滚**；写清 `stopped_at` |
| `hold_key` 链中抛错 | `finally` 松开修饰键（沿用 `run_chain`） |
| `on_error: stop`（默认） | 中止后续步 |
| `on_error: continue` | v1 **可解析但实现可先拒**（或 stop+warn），避免半套行为 |

审计：每步结果进返回 JSON；可选落盘 `agent-bus/archive/shots/perceive/plans/<ts>-<slug>.json`（实现时加，非合同硬性）。

## 非目标（v1 明确不做）

- 自然语言 → plan 的自动规划器（那是 Agent/skill 层）
- 默认开 OCR/YOLO / 全页 multimodal
- 游戏挂机、绕检测、无人值守乱点
- 云电脑桌面
- 与 windows-mcp 合并或改端口

## 验收（实现后）

1. **浏览器**：perceive Edge → act 点地址栏区 norm → key ctrl+a → type URL → enter；先 dry-run 看出点，再由操作者确认 `--go` 真跑。
2. **单独的测试窗口**（不要用 Win11 记事本：它把文件开成已有窗口的标签，清空了也会提示保存）：perceive → 点客户区 → type 一行 → assert 标题仍在。
3. 故意用过期 stamp / 最小化窗：必须 `ok:false` + `stopped_at`，不得点到屏外。
4. dry-run 全程无鼠标点击（可用日志/`clicked` 字段证明）。

## 实现顺序（给施工）

1. `ComputerPlan` typed dict + 加载校验
2. `run_plan(plan, *, go: bool)` 调度；`act` 步内部调现有 `run_chain`
3. CLI `--plan` + MCP `plan` 字段（string JSON 或 path）
4. 两例 `docs/examples/plan-browser.json` / `plan-notepad.json`
5. 补 SKILL.md 一小节「跨帧用 plan，单帧继续散装 act」

## 示例（浏览器，dry-run）

```json
{
  "version": 1,
  "goal": "focus address bar and prepare URL paste (dry-run first)",
  "defaults": { "gap": 0.15, "on_error": "stop" },
  "steps": [
    { "op": "perceive", "exe": "msedge", "focus": true, "as": "edge" },
    {
      "op": "act",
      "stamp_ref": "edge",
      "actions": [
        { "kind": "norm", "norm": [400, 40] },
        { "kind": "key", "key": "ctrl+a" },
        { "kind": "type", "text": "https://example.com" },
        { "kind": "wait", "sec": 0.2 }
      ]
    },
    { "op": "assert", "stamp_ref": "edge", "hwnd_alive": true }
  ]
}
```
