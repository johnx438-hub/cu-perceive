---
name: cu-perceive
description: >-
  use when looking at or acting on the user's Windows desktop via cu-perceive (CLI
  now, MCP later) — pin one window, read the 0-1000 grid map, then
  click/type/drag by norm or xy. OCR ids optional. Default dry-run; --go only
  when the user said so. Not for the cloud computer screen.
---
# cu-perceive

看或操作 **用户的 Windows 桌面**时用。云电脑屏幕不走这里。

**格子图是地图。** OCR / id 是可选项。默认 **不点不拖**；`--go` / `go=true` 只在 用户 当次明确说了才加。

CLI 和 MCP 是同一套合同。字段名对齐，不要另编一套。唯一例外：输入文字 CLI 是 `--type` 或 `--text`，MCP 字段叫 `text`。

MCP 的 `windows` / `perceive_window` 默认带一张压缩格子图（长边 1280 JPEG）。0–1000 仍相对整张图；点击用 stamp 里的原窗宽高，不看缩略像素。`map_inline=false` 只返路径。磁盘上仍是全分辨率 PNG。空字符串 / hwnd 0 = 省略。

**MCP `map_inline=true` 返回形态**：FastMCP 是两个 content block——`text`=JSON，`image`=JPEG base64。标准 client 必须分 block 读；若把整段 content 拼成一串再 `JSON.parse`，会在 JSON 与 `{"data":...}` 拼接处炸。这不是服务端少包一层。

## 跑法（CLI）

```
cu-perceive <cmd>            # pip 安装后
python -m cu_perceive <cmd>  # 源码目录
```

出图：默认 `%LOCALAPPDATA%\cu-perceive\shots\perceive\`（`CU_PERCEIVE_OUT` 可改）。给人看时读返回的 `map` 再附上。

OCR / UIA / YOLO 是可选包（`[ocr]` / `[uia]` / `[yolo]`），第一次用到时才加载。缺包时返回 `ok:false` + `install`（确切的 pip 命令），**不会自动安装**。

## 坐标

- `norm`：当前这张 `map` 的 0–1000（先看图再报点）
- `xy`：窗口客户区像素（`coord_space=window`）
- 桌面标尺图的 `norm` / `xy` 是整桌（`coord_space=screen`），用来选窗或拖窗口
- `id`：只有 `--ocr` / `ocr=true` 才有
- 规划用 id / stamp xy / norm。执行时才加 origin 变屏幕点
- `uia.bbox` 是 **screen** 像素；点击优先用 `norm` / `screen` 字段；有 `bbox_window` 时那是客户区

## 步骤

1. **选窗** — `windows`（MCP 同名）。默认出桌面标尺图：格子 + `#id hwnd`。看 `map` 选。`--title` / `--exe` 返回匹配数组 `windows[]` + `window`（picked）+ `pick_rule`（非最小化 → EnumWindows z-top → 面积；非保证前台）。同 exe 多窗再加 title/hwnd。`--hwnd` 仍一行。`--no-map` / `map=false` 只出 JSON。
2. **钉住再看** — `perceive --hwnd N`（MCP：`perceive_window`）。默认原图 + `.grid.png`，字段 `map`。不要默认开 OCR。
   **默认不抢前台**：按窗口本身抓（WGC → PrintWindow），被别的窗口挡住也能抓；返回 `capture.backend` / `capture.attempts`。`--capture dwm` 是实验性的（Win11 上恒为全黑），不在 auto 里。**最小化的窗口不还原**，返回 `ok:false, minimized:true, capture.skipped="minimized"`、不出 stamp；停在屏幕外（-32000、没最小化的"幽灵窗"）或客户区小于 8 px（比如 144x1）也 `ok:false` + `degenerate_client`、不出 stamp（`--focus` 也一样，先把窗口挪回屏幕）；要还原并拉到前台显式加 `--focus` / `focus=true`。stamp 带 `identity {hwnd,pid,ctime}`，名字是 `YYYYMMDD-HHMMSS-mmm`（同名再加 `-2`…，不会互相覆盖）。不要再 launch。
3. **读 `map`** — 没标上的点（图标、牌面、空白画布）看格子，不要看 `.anno.png`。
4. **可选 OCR / YOLO** — 有字的框用 `--ocr`。没字的图标/按钮加 `--yolo`（ScreenParser，类名当 text，可用 `--ids`）。默认都关。浏览器跳网址仍可先 OCR 地址栏。
   - **可选 UIA 节点(S4) / E4 旁路 OCR** — 有字的 Win32/WPF 按钮/菜单：**优先** `perceive --uia`（不要默认 `--ocr`）。格子图仍是主地图；`uia_nodes` 只是附加。 点仍走 **norm + `method=auto|uia|input`**（`act --uia-name "File"` 或 `--steps uia:File` / `uia:File@MenuItemControl` / `uia:#12` → 解析成 norm 中心）；**不要**把 element_token / raw Invoke 当默认点击路径。 无 hwnd 或 UIA 失败 → ok:false；弱 a11y（`uia_count=0`）退回格子图/OCR。OCR 留给绘制文字 / 浏览器正文。 `perceive --uia` 可带 `uia_ms`；同次 `--ocr` 时可有 `ocr_ms`。
   - **E2 点后轻复验 ROI** — `perceive --roi L,T,W,H` 或 `--roi-norm CX,CY[,HALF]`（HALF 默认 150，全窗 0–1000）：全窗抓图后裁切，写小 png+grid；`verify_light=true`，`width`/`height`=裁切尺寸，`full_width`/`full_height`+`roi` 保留全窗坐标。格子图 0–1000 **相对裁切**（局部复验空间）；下一步点击优先再全窗 perceive，除非自行把 ROI 局部 norm 换算回全窗。`--uia` 与 ROI 互斥（E2：UIA 仅全窗）。默认无 ROI 时行为不变。
5. **动手** — `act --stamp …`。先 dry-run，用户 说了再 `--go`。不传 `--stamp` 时用最新的 perceive stamp（`stamp_source: "latest"`，永远不会选 `windows` 桌面图）；此时 `--go` 要求它是带 identity 的窗口 stamp，屏幕 stamp 请显式 `--stamp`。
   - 点：`--norm 177,752` 或 `--xy 183,542`。有字控件且 stamp 带 `uia_nodes` 时可用 `--uia-name "File"` / `--steps uia:File@MenuItemControl`（解析成 norm，不另开 Invoke 默认路径）。长按加 `--hold 1.8`（按下、睡、松开；MCP 字段 `hold`）。`ids` 接受 `"14,9,16"`、单个数字 `5`、或列表 `[5,9]`（MCP 把 int/list coerce 成逗号串）。

   - **E3 同 stamp 多步** — 稳定 UI：一次 `perceive` 后用**同一 stamp** 规划多步 `act`（先 dry-run 列出，用户 一次批准再串行 `--go`）。已有 `--steps "norm:a:b,norm:c:d,..."` 或 `--norm "a,b;c,d"` 即可链式；不要另写点击引擎。动画/弹层场景不要硬套。
     - 每次 act（含 dry）返回 `stamp_check` / `stamp_reuse` / `step_count`。`--go` 时 stamp 坏了（窗挪了、最小化、hwnd 没了、**hwnd 被别的进程复用 / pid 或进程创建时间不符 / 旧 stamp 没有 identity**、超 `--max-age`、或 `verify_light` ROI stamp）→ `ok=false` **不点**，强制重 perceive。`--go` 时每一步执行前还会再核一次 identity。
     - **窗口没了或 identity 不符（hwnd 复用 / pid / 创建时间 / 旧 stamp 无 identity）时 dry-run 也 `ok=false`。** 其它坏 stamp（挪了、改大小、超龄）dry-run 仍可 ok，但顶层总有 `stamp_ok`，坏时有 `stamp_error`；要 dry 也 fail-closed 加 `--strict-stamp`。
     - `--go` 时 `foreground_click_count` 只数真正发出的 SendInput 点击（被拒为 0）；计划值在 `planned_foreground_click_count`。
     - **ROI / `verify_light` stamp 只用于点后复验，不能拿来多步规划**（须全窗 stamp）。
     - CLI：`act --max-age SEC`（可选）、`act --strict-stamp`。MCP：`max_age_sec` / `strict_stamp` 已透传。

   - **实验 `method=post|uia|auto`**：`post`=HWND PostMessage 单点；`uia`=UIA Invoke/Toggle/Expand/Select（无 pattern 则 fail-closed，不退 post/input）；`auto`=命中 stamp 上可 Invoke 等 → uia，否则 input（永不选 post；软 miss 同点再探一次；dry-run 报 `chosen_method` + `uia_reprobe`/`uia_first` + `foreground_used` / `foreground_click_count`）。均仅 left 单点 norm/xy/ids；drag/path 等请显式 `method=input`。没装 `uia` extra 时 `--method uia` 退出码 3 并给 `missing_extra` / `install`（同 `perceive --uia`）；`auto` 退回 input 并在 `warnings` 里说明。
   - 链修饰键：`--hold-key ctrl`（MCP `hold_key`，接受 `ctrl`/`control`/`shift`/`alt`）在整条链的 click/hover 前按下，链结束（含失败）再松开。`--go` 时只在 stamp 窗在前台时才按（见下方检查），不在前台加 `--focus`。不是 `--hold`（那是鼠标长按）。Explorer/Office/列表多选：`--hold-key ctrl` + 多个 `--norm`/`--ids`，界面要落稳再加 `--gap`。Shift+click 头尾可以两次点击、不设 hold_key；需要时再用 `--hold-key shift`。
   - 会动的选中（动画/弹起）用 `--gap 0.4`；按钮、丝带、浏览器标签保持默认 0.12，不要全局加大。
   - 键：`--type` / `--text` 剪贴板+Ctrl+V 并还原（MCP 字段是 `text`）；`--key` 可写多次并按出现顺序执行，也可 `ctrl+a,enter`
   - **已有字的输入框用 `--replace`（MCP `replace=true`）。** 它在每次 `--type` 前先按 ctrl+a，粘贴就替换原文；不加时 click+type 会插到光标处（实测 about:blank 变成 `chrome://blankhttps//...`）。一次做完：`act --norm 491,63 --type URL --replace --key enter --go`。
   - **被挡住的窗口可以 perceive，但 `--go` 不会把输入送进别的窗口。** 窗口 stamp 上每一步发送前都核：点击 / 拖动起点和终点 / 滚轮的那个屏幕点，最上层窗口必须是 stamp 窗（或它拥有的对话框、弹窗、菜单），否则 `ok=false` + `pointer_refused_step` + `point_window`（真正在那里的 hwnd 和标题）；每个 `--type`/`--key` 前 stamp 窗（或它拥有的窗，比如刚点开的对话框）必须在前台（点击之后也核），否则 `keyboard_refused_step`。`--hold-key` 也算按键：在第一个点击/拖/滚的点通过后、且 stamp 窗（或它拥有的窗）在前台时才按下，否则 `hold_key_refused_step`，一个键都不按。被拒那步及之后都不发。要先把它拉到前台加 `--focus`（MCP `focus=true`）：拉前台后再核 identity 和点/前台，仍被挡（比如置顶窗）照样拒绝。dry-run 的 `occluded_points` 列出现在被挡的点并给警告。只管 `method=input` 和 auto 里选了 input 的步；uia / post 不变。屏幕 stamp 没有窗口可核：键必须同一次 act 里先点，且只在那次点到的窗口在前台时发送。
   - **ids 不是静默聚焦**：同一次 `act` 里 `ids`/`norm`/`xy` 会先真正点击，再 `text` 粘贴。点的必须是可编辑框本身；点到标签/按钮再 type 不会自动聚焦输入框。`scroll`+位置会变成 hover 不点击，此时同链 `text` 也不会靠 ids 聚焦。
   - `--button right`；`--hover`；`--scroll -3` / `down:3` / `up:3`
   - **正文超链接页用 `scroll` + `xy`/`norm`/`ids`，只定位不点。** 同链有 scroll 又有位置时，位置步是 hover（光标移过去）再滚轮，不左键点。不要先单独 xy 再 scroll（会点进文章）。只传 scroll、不传点：仍在 last_screen 或当前光标滚。`--hover` 仍可用；scroll+xy 已隐含 hover。
   - 笔画 / 拖：一次**按住**的折线。`--drag-norm 200,300;450,280;600,450` 或 `--drag`（窗口像素）。`--step-px` 默认 4。底层是 LEFTDOWN 后连续 `SendInput` MOVE，再 LEFTUP（光标空跑不算拖）。
6. **拖窗口** — 同一套按住折线。perceive 默认只有客户区，标题栏不在里面。用 `windows` 那张桌面 `map` 的 stamp（显式 `--stamp <windows 的 stamp>`，会读 `<stamp>.windows.json`；不传 `--stamp` 不会选到它），从标题栏附近 `norm` 拖到目标。还没单独验收，当普通拖。

## MCP 字段（和 CLI 对齐）

| 事 | CLI | MCP |
|---|---|---|
| 列窗 | `windows` | `windows` |
| 看一窗 | `perceive` | `perceive_window` |
| 动手 | `act` | `act` |
| 开 OCR | `--ocr` | `ocr=true` |
| 开 YOLO | `--yolo` | `yolo=true` |
| 开 UIA 节点(S4) | `--uia` | `uia=true` |
| E2 轻复验 ROI | `--roi` / `--roi-norm` | `roi` / `roi_norm`（互斥；`verify_light`） |
| E3 stamp 守卫 | `--max-age` / `--strict-stamp` | `max_age_sec` / `strict_stamp`（`stamp_check`） |
| 真动手 | `--go` | `go=true` |
| 被挡/不在前台时先拉 stamp 窗前台 | `--focus` | `focus=true` |
| 接触方式（实验） | `--method input|post|uia|auto` | `method`（默认 input；auto→chosen_method=uia|input） |
| 点 | `--norm` `--xy` `--ids` | `norm` `xy` `ids` |
| E4 UIA 名→norm | `--uia-name` / `--steps uia:Name` | `uia_name`（需 `perceive --uia` stamp） |
| 拖 | `--drag-norm` `--drag` `--step-px` | `drag_norm` `drag` `step_px` |
| 滚 | `--scroll -3` / `down:3` | `scroll`: `-3` / `down:3` / `up:3` |
| 长按 | `--hold 1.8` | `hold`（秒，配合 xy/norm/ids） |
| 链修饰键 | `--hold-key ctrl` | `hold_key`（`ctrl`/`control`/`shift`/`alt`，不是鼠标长按） |
| 打字 | `--type` / `--text` | `text` |
| 先全选再打字 | `--replace` | `replace=true` |
| 读哪张图 | `map` | `map`（默认 jpeg-1280 内联，`map_inline=false` 只路径） |
| 出图目录 | `--out` | `out_dir`。写进 WSL workspace 用 UNC `\\wsl.localhost\Ubuntu\home\<user>\...`，不要 `/home/<user>`（会落到 `C:\home\<user>`）。`/mnt/c/...` 仍可用。另有 `map_wsl` |
| 拉前台再抓 | `--focus` | `focus=true`（默认 false，不抢前台） |
| 采集后端 | `--capture auto|wgc|printwindow|screen|dwm` | `capture`（`screen` 需 focus；`dwm` 实验性） |

`launch` / `launch_app` 会真启动，没有 dry-run。

## WSL / Grok Build / minimal-agent-ts

从 WSL 调 Windows 上的 Python（不要用 Linux python）：

    CU_PERCEIVE_PYTHON=/mnt/c/<python-dir>/python.exe <checkout>/bin/cu-perceive.sh windows
    <checkout>/bin/cu-perceive.sh perceive --hwnd N
    <checkout>/bin/cu-perceive.sh act --stamp STAMP --drag-norm ...

MCP 本机 loopback（不碰 8000 的 windows-mcp）：

    http://127.0.0.1:8771/mcp

Grok: ~/.grok/config.toml 里 [mcp_servers.cu-perceive]。
minimal-agent-ts: agent.json 的 mcp_servers 里同名一项。
当前端口是 8771。8766 可能还有旧进程 / Tailscale 反代，别连。
MCP 默认带压缩图；要全分辨率走磁盘路径。CLI 仍用 map 路径。WSL 用返回的 `map_wsl` / `png_wsl`。写进 Linux workspace 用 UNC： `\\wsl.localhost\Ubuntu\home\<user>\...`（发行版名用 `CU_PERCEIVE_WSL_DISTRO` 改）。 不要传 `/home/<user>/...`，Win Python 会当相对路径写到 `C:\home\<user>`，不报错。 `/mnt/c/...` 仍映射到 NTFS。origin.left <= -10000 是最小化哨兵，丢掉 stamp 重 perceieve。


## Agent 易踩坑（K3 / 88yr 2026-09-05）

- MCP `act` 只有**扁平字段**（`norm`/`xy`/`ids`/`text`/`key`…），**没有** `steps=[{...}]` JSON 数组；乱传会被静默丢掉。若解析后零步骤，现在返回 `ok:false` + error（不再假绿）。
- 点击例：`act(stamp="...", norm="935,69", go=true)`；坐标逗号（也认冒号）。只有键：`act(stamp="...", text="hi", replace=true, go=true, focus=true)`。
- 完整回路：`windows` → `perceive_window` →（稳定 UI）同 stamp 多步 dry `act` → 用户 批后再 `--go` → 再 `perceive_window` 验证（点后优先 `--roi-norm` 轻复验，首次选点/规划仍全窗）。窗挪了或 ROI stamp → 重 perceive，勿盲点。

## 不要

- 云电脑桌面
- 没说 `--go` 就点或拖
- 默认开 OCR 或 YOLO
- 给游戏挂机写方案
- 远程 HTTPS 当已接通（Grok Bot 后端不在 tailnet；先 CLI）
