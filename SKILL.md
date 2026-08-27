---
name: cu-perceive
description: >-
  use when looking at or acting on Shawn's Windows desktop via cu-perceive (CLI
  now, MCP later) — pin one window, read the 0-1000 grid map, then
  click/type/drag by norm or xy. OCR ids optional. Default dry-run; --go only
  when Shawn said so. Not for the cloud computer screen.
---
# cu-perceive

看或操作 **Shawn 的 Windows 桌面**（ARCHER）时用。云电脑屏幕不走这里。

**格子图是地图。** OCR / id 是可选项。默认 **不点不拖**；`--go` / `go=true` 只在 Shawn 当次明确说了才加。

CLI 和 MCP 是同一套合同。字段名对齐，不要另编一套。唯一例外：输入文字 CLI 是 `--type` 或 `--text`，MCP 字段叫 `text`。

MCP 的 `windows` / `perceive_window` 默认带一张压缩格子图（长边 1280 JPEG）。0–1000 仍相对整张图；点击用 stamp 里的原窗宽高，不看缩略像素。`map_inline=false` 只返路径。磁盘上仍是全分辨率 PNG。空字符串 / hwnd 0 = 省略。

## 跑法（CLI）

```
PYTHONPATH=C:\Users\jawn\src\cu-perceive;C:\Users\jawn\src\enikk
C:\Users\jawn\miniconda3\python.exe -m cu_perceive <cmd>
```

出图：`C:\Users\jawn\agent-bus\archive\shots\perceive\`。给人看时读返回的 `map` 再附上。

## 坐标

- `norm`：当前这张 `map` 的 0–1000（先看图再报点）
- `xy`：窗口客户区像素（`coord_space=window`）
- 桌面标尺图的 `norm` / `xy` 是整桌（`coord_space=screen`），用来选窗或拖窗口
- `id`：只有 `--ocr` / `ocr=true` 才有
- 规划用 id / window xy / norm。执行时才加 origin 变屏幕点

## 步骤

1. **选窗** — `windows`（MCP 同名）。默认出桌面标尺图：格子 + `#id hwnd`。看 `map` 选。`--title` / `--exe` / `--hwnd` 只要一行。`--no-map` / `map=false` 只出 JSON。
2. **钉住再看** — `perceive --hwnd N`（MCP：`perceive_window`）。默认原图 + `.grid.png`，字段 `map`。不要默认开 OCR。
   **restore / 钉窗后固定双拍，第一张丢掉**（壁纸 / 上面那层 Chrome / CEF PrintWindow 空帧）。settle 0.3s 后取第二帧。未 restore 且未 activate 的静默截图仍单拍。不要再 launch。
3. **读 `map`** — 没标上的点（图标、牌面、空白画布）看格子，不要看 `.anno.png`。
4. **可选 OCR / YOLO** — 有字的框用 `--ocr`。没字的图标/按钮加 `--yolo`（ScreenParser，类名当 text，可用 `--ids`）。默认都关。浏览器跳网址仍可先 OCR 地址栏。
5. **动手** — `act --stamp …`。先 dry-run，Shawn 说了再 `--go`。
   - 点：`--norm 177,752` 或 `--xy 183,542`。长按加 `--hold 1.8`（按下、睡、松开；MCP 字段 `hold`）。`ids` 接受 `"14,9,16"`、单个数字 `5`、或列表 `[5,9]`（MCP 把 int/list coerce 成逗号串）。
   - 链修饰键：`--hold-key ctrl`（MCP `hold_key`，接受 `ctrl`/`control`/`shift`/`alt`）在整条链的 click/hover 前按下，链结束（含失败）再松开。不是 `--hold`（那是鼠标长按）。Explorer/Office/列表多选：`--hold-key ctrl` + 多个 `--norm`/`--ids`，界面要落稳再加 `--gap`。Shift+click 头尾可以两次点击、不设 hold_key；需要时再用 `--hold-key shift`。
   - 会动的选中（动画/弹起）用 `--gap 0.4`；按钮、丝带、浏览器标签保持默认 0.12，不要全局加大。
   - 键：`--type` / `--text` 剪贴板+Ctrl+V 并还原（MCP 字段是 `text`）；`--key` 可写多次并按出现顺序执行，也可 `ctrl+a,enter`
   - **已有字的输入框先 ctrl+a 再 type。** click+type 一次做完会插到原文中间（实测 about:blank 变成 aboubilibili.comblank）。拆两次 act：先 ids + key=ctrl+a，再单独 text + key=enter。
   - `--button right`；`--hover`；`--scroll -3` / `down:3` / `up:3`
   - **正文超链接页用 `scroll` + `xy`/`norm`/`ids`，只定位不点。** 同链有 scroll 又有位置时，位置步是 hover（光标移过去）再滚轮，不左键点。不要先单独 xy 再 scroll（会点进文章）。只传 scroll、不传点：仍在 last_screen 或当前光标滚。`--hover` 仍可用；scroll+xy 已隐含 hover。
   - 笔画 / 拖：一次**按住**的折线。`--drag-norm 200,300;450,280;600,450` 或 `--drag`（窗口像素）。`--step-px` 默认 4。底层是 LEFTDOWN 后连续 `SendInput` MOVE，再 LEFTUP（光标空跑不算拖）。
6. **拖窗口** — 同一套按住折线。perceive 默认只有客户区，标题栏不在里面。用 `windows` 那张桌面 `map` 的 stamp，从标题栏附近 `norm` 拖到目标。还没单独验收，当普通拖。

## MCP 字段（和 CLI 对齐）

| 事 | CLI | MCP |
|---|---|---|
| 列窗 | `windows` | `windows` |
| 看一窗 | `perceive` | `perceive_window` |
| 动手 | `act` | `act` |
| 开 OCR | `--ocr` | `ocr=true` |
| 开 YOLO | `--yolo` | `yolo=true` |
| 真动手 | `--go` | `go=true` |
| 点 | `--norm` `--xy` `--ids` | `norm` `xy` `ids` |
| 拖 | `--drag-norm` `--drag` `--step-px` | `drag_norm` `drag` `step_px` |
| 滚 | `--scroll -3` / `down:3` | `scroll`: `-3` / `down:3` / `up:3` |
| 长按 | `--hold 1.8` | `hold`（秒，配合 xy/norm/ids） |
| 链修饰键 | `--hold-key ctrl` | `hold_key`（`ctrl`/`control`/`shift`/`alt`，不是鼠标长按） |
| 打字 | `--type` / `--text` | `text` |
| 读哪张图 | `map` | `map`（默认 jpeg-1280 内联，`map_inline=false` 只路径） |
| 出图目录 | `--out` | `out_dir`。写进 WSL workspace 用 UNC `\\wsl.localhost\Ubuntu\home\archer\...`，不要 `/home/archer`（会落到 `C:\home\archer`）。`/mnt/c/...` 仍可用。另有 `map_wsl` |

`launch` / `launch_app` 会真启动，没有 dry-run。

## WSL / Grok Build / minimal-agent-ts

从 WSL 调 Windows 上的 Python（不要用 Linux python）：

    /mnt/c/Users/jawn/src/cu-perceive/bin/cu-perceive.sh windows
    /mnt/c/Users/jawn/src/cu-perceive/bin/cu-perceive.sh perceive --hwnd N
    /mnt/c/Users/jawn/src/cu-perceive/bin/cu-perceive.sh act --stamp STAMP --drag-norm ...

MCP 本机 loopback（不碰 8000 的 windows-mcp）：

    http://127.0.0.1:8771/mcp

Grok: ~/.grok/config.toml 里 [mcp_servers.cu-perceive]。
minimal-agent-ts: agent.json 的 mcp_servers 里同名一项。
当前端口是 8771。8766 可能还有旧进程 / Tailscale 反代，别连。
MCP 默认带压缩图；要全分辨率走磁盘路径。CLI 仍用 map 路径。WSL 用返回的 `map_wsl` / `png_wsl`。写进 Linux workspace 用 UNC： `\\wsl.localhost\Ubuntu\home\archer\zerostack-analysis\...`。 不要传 `/home/archer/...`，Win Python 会当相对路径写到 `C:\home\archer`，不报错。 `/mnt/c/...` 仍映射到 NTFS。origin.left <= -10000 是最小化哨兵，丢掉 stamp 重 perceieve。

## 不要

- 云电脑桌面
- 没说 `--go` 就点或拖
- 默认开 OCR 或 YOLO
- 给游戏挂机写方案
- 远程 HTTPS 当已接通（Grok Bot 后端不在 tailnet；先 CLI）
