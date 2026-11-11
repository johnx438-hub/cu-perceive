---
name: cu-perceive
description: >-
  use when looking at or acting on the user's Windows desktop via cu-perceive (CLI
  now, MCP later) — pin one window, read the 0-1000 grid map, then
  click/type/drag by norm or xy. OCR ids optional. Desktop act defaults to
  dry-run; --go only when the user said so. Browser tools run unless
  dry_run=true. Not for the cloud computer screen.
---
# cu-perceive

看或操作 **用户的 Windows 桌面**时用。云电脑屏幕不走这里。

**格子图是地图。** OCR / id 是可选项。桌面 `act` 默认 **不点不拖**；`--go` / `go=true` 只在 用户 当次明确说了才加。网页的 `browser_*` 写操作默认就会执行。

## 浏览器页面 vs 桌面

网页里的事（Chrome / Edge 标签页里的页面）用 `browser_*`。它们调用本机的 `bsk` 命令（BrowserSkill CLI，可选，不随 cu-perceive 安装）。桌面程序，以及把浏览器当普通窗口来移动或点标题栏，仍用 `windows` / `perceive_window` / `act`。

`bsk` 用 `CU_PERCEIVE_BSK` 指定，否则从 PATH 找。Windows 上从 PATH 只认 `bsk.exe`，不认 `.cmd` / `.bat`。找不到时返回 `ok:false`、`reason: missing_dependency` 和安装提示，不会自己下载。

| 工具 | 用途 | 何时真的执行 |
|---|---|---|
| `browser_status` | daemon / 已连接的浏览器 | 立刻 |
| `browser_session` | `list` / `start` / `stop` | `list` 立刻；`start` 和 `stop` 默认执行，`dry_run=true` 只预览 |
| `browser_observe` | `observe` 或 `snapshot`，返回 `@eN` 和 `observation_id` | 立刻（只读） |
| `browser_tabs` | `list` / `create` / `close` / `select` / `borrow` / `return` | `list` 立刻；其余默认执行，`dry_run=true` 只预览 |
| `browser_cooperate` | `status` / `sync` / `wait`：共享标签页的控制权（bsk `cooperate`） | `status` 立刻；`sync` / `wait` 默认执行，`dry_run=true` 只预览 |
| `browser_act` | click / fill / press / navigate 等写操作 | 默认执行；`dry_run=true` 返回 `command`，不启动 `bsk` |

`go` 可以传，但不拦这些调用（沿用桌面 `act` 的 `go=true` 也不会坏）。`CU_PERCEIVE_BSK_REQUIRE_GO=1`（也认 `true` / `yes` / `on`）把写操作改回默认 dry-run、`go=true` 才执行，并且 `dry_run=true` 仍优先。桌面 `act` 仍是默认 dry-run，不读这个开关。

`dry_run=true` 返回将要执行的 `command`（完整 `bsk ... --json` argv）和 `ref`，不启动 `bsk`。`value` / `url` 写成 `--value=<v>` / `--url=<u>`；位置参数形式的 url、key、target 放在 `--` 后面，这样 `-test` 这种值不会被 bsk 当成 flag。`browser_act` 的 ref 必须来自该 session 最近一次 `browser_observe`，并带上那次的 `observation_id`；旧 ref 直接拒绝（`reason: stale_observation`），不会发给 `bsk`。`observe` 带 `cursor` 是同一份采集的下一页，但 bsk 会换掉 ref map，上一页的 `@eN` 不能再用。click / press / fill 的结果若表明页面 URL 变了，本地会清掉已存的 ref。`tab borrow` 执行时可能要用户在浏览器里确认（默认就会执行）。bsk 若返回 `input_outcome_unknown`，不要盲重试，先再 observe。

**共享标签页控制（cooperate）**：人也能用的标签页上，bsk 会以 `cooperation_sync_required` 拒绝写操作，直到 agent sync；context 失效时报 `control_context_invalid`。`browser_cooperate action=sync`（默认 `settle=true`，可选 `policy=auto|target|strict`）把返回的 `context_id` 存到该 session，并把同时返回的页面记成新的 observation；之后该 session 的 `browser_act` 自动带 `--context`（也可自己传 `context=`）。仍碰到这两个 reason 时，`browser_act`（默认 `auto_sync=true`）自动 sync 一次，存新 context，返回 `ok:false` + `synced` + `observation_id` + `refs`：这次什么都没发出去，从新 refs 里重新选目标再发一次；它自己不会重发输入。`browser_session action=stop` 会清掉存的 context。

`bsk` 需要 Windows 上已连接的 Chrome 或 Edge。仓库里的测试用假的可执行文件，不启动浏览器。

**影子浏览器 profile**：`launch_app` 和 plan 的 `launch` 可带 `args`（字符串列表，最多 64 个），chrome.exe / msedge.exe 还可带 `user_data_dir`。浏览器绝不用主 profile：没给 user-data-dir 就用影子目录（`CU_PERCEIVE_SHADOW_PROFILE_CHROME` / `_EDGE`，默认 `~/.cu-perceive-trial-chrome-profile` / `~/.cu-perceive-trial-edge-profile`）；指向 `%LOCALAPPDATA%\Google\Chrome*\User Data`（或 Edge 的）本身或其子目录、裸 `--user-data-dir`（不带 `=`）、或两个不同目录都报 `reason: profile_refused`。比较前两边都做文件系统规范化：展开 8.3 短名、去掉 `\\?\` / `\\.\` 前缀、跟随 junction / 符号链接（不存在的路径按最近的已存在父目录解析）、折叠 `..`、斜杠、尾部点和空格、大小写；UNC / 网络路径、卷 GUID 等设备路径、`C:x`、ADS、解析不了的链接一律拒绝。`-user-data-dir` 和 `/user-data-dir` 也算；`chrome_proxy.exe` 也受守卫；`cmd` / `powershell` / `explorer` 等参数里带 chrome / msedge 的直接拒绝。远程调试只留在本机：`--remote-debugging-address`（`--` / `-` / `/` 任一写法）不是 127.0.0.1 / localhost 就报 `reason: debug_address_refused`（裸开关、端口不是 0–65535 的数字、两个不同端口、`--` 之后的调试开关同样拒绝）；只要给了端口，argv 自动补 `--remote-debugging-address=127.0.0.1`，返回带 `debug_port` / `debug_address`。自带 app `chrome-shadow` 和 `edge-shadow`（都是 `--no-first-run --no-default-browser-check --disable-sync --new-window about:blank`；`edge-shadow` 用 `~/.cu-perceive-trial-edge-profile`，Edge 主 User Data 一律拒绝）。`launch_app(dry_run=true)` / CLI `launch --resolve-only` 只返回最终 argv 不启动。真启动返回 `pid`、`window`（新出现的顶层窗口，优先同 pid）和 `new_window`；没等到新窗口只给警告，不会把旧窗口当成它。之后用 `perceive_window` / `act` 按 hwnd 驱动；bsk 只是可选的 DOM 读取，前提是该 profile 里装了扩展。品牌版 Chrome 137+ 忽略 `--load-extension`。常驻做法：在影子 profile 里一次性手动加载（`chrome-shadow` 窗口里打开 `chrome://extensions` → 开发者模式 → 加载已解压的扩展 → 选 bsk 文件夹），之后每次启动都在，不需要任何参数；出现权限 / 安全 / 确认类对话框就停下交给人点。兜底（临时 profile 或无法手动加载时）：启动加 `--arg=--remote-debugging-port=<port> --arg=--enable-unsafe-extension-debugging`，再跑 `scripts/shadow_bsk_install.py`（CDP `Extensions.loadUnpacked`，只连 127.0.0.1），只对这次浏览器进程有效。bsk 只读自己 Agent Window 里的标签页；外面的标签页要 `tab borrow`（会弹确认，不要点）。要“同一窗口”既驱动又读 DOM：用 `act` 驱动 Agent Window 的 hwnd，用 `browser_observe` 读。

CLI 和 MCP 是同一套合同。字段名对齐，不要另编一套。唯一例外：输入文字 CLI 是 `--type` 或 `--text`，MCP 字段叫 `text`。

MCP 的 `windows` / `perceive_window` 默认带一张压缩格子图（长边 1280 JPEG）。0–1000 仍相对整张图；点击用 stamp 里的原窗宽高，不看缩略像素。`map_inline=false` 只返路径。磁盘上仍是全分辨率 PNG。空字符串 / hwnd 0 = 省略。

**MCP `map_inline=true` 返回形态**：FastMCP 是两个 content block——`text`=JSON，`image`=JPEG base64。标准 client 必须分 block 读；若把整段 content 拼成一串再 `JSON.parse`，会在 JSON 与 `{"data":...}` 拼接处炸。这不是服务端少包一层。

## 跑法（CLI）

```
cu-perceive <cmd>            # pip 安装后
python -m cu_perceive <cmd>  # 源码目录
```

出图：CLI 默认 `%LOCALAPPDATA%\cu-perceive\shots\perceive\`（`CU_PERCEIVE_OUT` 可改）。MCP server 可能用另一个用户或另一个 `CU_PERCEIVE_OUT`，目录不一定相同：一律用返回的 `json` / `map` / `png` 路径，act 时显式传 `stamp`。给人看时读返回的 `map` 再附上。

OCR / UIA / YOLO 是可选包（`[ocr]` / `[uia]` / `[yolo]`），第一次用到时才加载。缺包时返回 `ok:false` + `install`（确切的 pip 命令），**不会自动安装**。OCR / YOLO 在单独的子进程里跑：onnxruntime 一旦进了抓图进程，下一次 WGC 抓图会让整个进程崩（MCP server 直接退出）。子进程挂了只影响那一次 perceive（给 warning，下次自动重启）。进程里已有 onnxruntime 时 auto 不再用 WGC，直接 PrintWindow（`capture.attempts` 里 WGC 是 `skipped: "unsafe"`）。

## 坐标

- `norm`：当前这张 `map` 的 0–1000（先看图再报点）
- `xy`：窗口客户区像素（`coord_space=window`）
- 桌面标尺图的 `norm` / `xy` 是整桌（`coord_space=screen`），用来选窗或拖窗口
- `id`：只有 `--ocr` / `ocr=true` 才有
- 规划用 id / stamp xy / norm。执行时才加 origin 变屏幕点
- `uia.bbox` 是 **screen** 像素；点击优先用 `norm` / `screen` 字段；有 `bbox_window` 时那是客户区

## 步骤

1. **选窗** — `windows`（MCP 同名）。默认出桌面标尺图：格子 + `#id hwnd`。看 `map` 选。`--title` / `--exe` 返回匹配数组 `windows[]` + `window`（picked）+ `pick_rule`（非最小化 → EnumWindows z-top → 面积；非保证前台）。像悬浮窗的窗口（`WS_EX_LAYERED`/`TOOLWINDOW`/`NOACTIVATE`、小于 120x120、或一半以上不在任何显示器上；行里有 `overlay`）排在普通窗口后面，此时有 `pick_reason: overlay_deprioritized`；选中的仍是悬浮窗时有 `pick_warning: overlay_candidate` + `suggested_hwnd`，改用那个 hwnd。同 exe 多窗再加 title/hwnd。`--hwnd` 仍一行。`--no-map` / `map=false` 只出 JSON。
2. **钉住再看** — `perceive --hwnd N`（MCP：`perceive_window`）。默认原图 + `.grid.png`，字段 `map`。不要默认开 OCR。
   **默认不抢前台**：按窗口本身抓（WGC → PrintWindow），被别的窗口挡住也能抓；返回 `capture.backend` / `capture.attempts`。`--capture dwm` 是实验性的（Win11 上恒为全黑），不在 auto 里。**最小化的窗口不还原**，返回 `ok:false, minimized:true, capture.skipped="minimized"`、不出 stamp；停在屏幕外（-32000、没最小化的"幽灵窗"）或客户区小于 8 px（比如 144x1）也 `ok:false` + `degenerate_client`、不出 stamp（`--focus` 也一样，没用：从任务栏还原或选中后 Win+Shift+←/→ 把窗口挪回屏幕，再 perceive；返回带 `reason: offscreen|tiny_client` 和 `hint`）；要还原并拉到前台显式加 `--focus` / `focus=true`。stamp 带 `identity {hwnd,pid,ctime}`，名字是 `YYYYMMDD-HHMMSS-mmm`（同名再加 `-2`…，不会互相覆盖）。不要再 launch。
3. **读 `map`** — 没标上的点（图标、牌面、空白画布）看格子，不要看 `.anno.png`。
4. **可选 OCR / YOLO** — 有字的框用 `--ocr`。没字的图标/按钮加 `--yolo`（ScreenParser，类名当 text，可用 `--ids`）。默认都关。浏览器跳网址仍可先 OCR 地址栏。
   - **可选 UIA 节点(S4) / E4 旁路 OCR** — 有字的 Win32/WPF 按钮/菜单：**优先** `perceive --uia`（不要默认 `--ocr`）。格子图仍是主地图；`uia_nodes` 只是附加。 点仍走 **norm + `method=auto|uia|input`**（`act --uia-name "File"` 或 `--steps uia:File` / `uia:File@MenuItemControl` / `uia:#12` → 解析成 norm 中心）；**不要**把 element_token / raw Invoke 当默认点击路径。 无 hwnd 或 UIA 失败 → ok:false；弱 a11y（`uia_count=0`）退回格子图/OCR（`Chrome_WidgetWin_*` 窗口会带 `uia_fallback: no_accessibility_tree`）。OCR 留给绘制文字 / 浏览器正文。 `perceive --uia` 可带 `uia_ms`；同次 `--ocr` 时可有 `ocr_ms`。
   - **E2 点后轻复验 ROI** — `perceive --roi L,T,W,H` 或 `--roi-norm CX,CY[,HALF]`（HALF 默认 150，全窗 0–1000）：全窗抓图后裁切，写小 png+grid；`verify_light=true`，`width`/`height`=裁切尺寸，`full_width`/`full_height`+`roi` 保留全窗坐标。格子图 0–1000 **相对裁切**（局部复验空间）；下一步点击优先再全窗 perceive，除非自行把 ROI 局部 norm 换算回全窗。`--uia` 与 ROI 互斥（E2：UIA 仅全窗）。默认无 ROI 时行为不变。
5. **动手** — `act --stamp …`（stamp 名字或 perceive 返回的 `json` 路径都行）。先 dry-run，用户 说了再 `--go`。多个窗口时加 `--hwnd` / MCP `hwnd`：不给 stamp 就用这个窗口最新的 stamp，给了别的窗口的 stamp 就拒绝（`reason: hwnd_mismatch`）。MCP 会拒绝不认识的参数（`reason: "unknown_args"`），所以用 `hwnd` 指明窗口。不传 `--stamp` 时用最新的 perceive stamp（`stamp_source: "latest"`，永远不会选 `windows` 桌面图）；此时 `--go` 要求它是带 identity 的窗口 stamp，屏幕 stamp 请显式 `--stamp`。
   - 点：`--norm 177,752` 或 `--xy 183,542`。有字控件且 stamp 带 `uia_nodes` 时可用 `--uia-name "File"` / `--steps uia:File@MenuItemControl`（解析成 norm，不另开 Invoke 默认路径）；默认 `method=input` 只按 stamp 里节点的坐标点，不核对名字，stamp 过期时点到的可能是别的控件。Chromium/Electron 应用（`Chrome_WidgetWin_*` 窗口，如迅雷）按 UIA 节点点击时用 `method=auto`，它会核对名字，对不上就拒绝。长按加 `--hold 1.8`（按下、睡、松开；MCP 字段 `hold`）。`ids` 接受 `"14,9,16"`、单个数字 `5`、或列表 `[5,9]`（MCP 把 int/list coerce 成逗号串）。

   - **E3 同 stamp 多步** — 稳定 UI：一次 `perceive` 后用**同一 stamp** 规划多步 `act`（先 dry-run 列出，用户 一次批准再串行 `--go`）。已有 `--steps "norm:a:b,norm:c:d,..."` 或 `--norm "a,b;c,d"` 即可链式；不要另写点击引擎。动画/弹层场景不要硬套。
     - 每次 act（含 dry）返回 `stamp_check` / `stamp_reuse` / `step_count`。`--go` 时 stamp 坏了（窗挪了、最小化、hwnd 没了、**hwnd 被别的进程复用 / pid 或进程创建时间不符 / 旧 stamp 没有 identity**、超 `--max-age`、或 `verify_light` ROI stamp）→ `ok=false` **不点**，强制重 perceive。`--go` 时每一步执行前还会再核一次 identity。
     - **窗口没了或 identity 不符（hwnd 复用 / pid / 创建时间 / 旧 stamp 无 identity）时 dry-run 也 `ok=false`。** 其它坏 stamp（挪了、改大小、超龄）dry-run 仍可 ok，但顶层总有 `stamp_ok`，坏时有 `stamp_error`；要 dry 也 fail-closed 加 `--strict-stamp`。
     - `--go` 时 `foreground_click_count` 只数真正发出的 SendInput 点击（被拒为 0）；dry-run 也是 0，计划值在 `planned_foreground_click_count`。同一进程里并发的 `go=true` 会排队，不会交错。live 在锁内记下调用前的顶层窗。只有可能开窗的步（点击、Enter、Esc、F 键、Alt 组合）结束后才轮询 1200ms（`CU_PERCEIVE_NEW_WINDOW_WAIT_MS`），一出现新顶层窗就停，放进 `new_windows`（`{hwnd, title, class}`）。链里非最后一步只轮询 250ms。打字、滚动、拖动不等待。`after` 是结束时的前台窗。
     - **ROI / `verify_light` stamp 只用于点后复验，不能拿来多步规划**（须全窗 stamp）。
     - CLI：`act --max-age SEC`（可选）、`act --strict-stamp`。MCP：`max_age_sec` / `strict_stamp` 已透传。

   - **实验 `method=post|uia|auto`**：`post`=HWND PostMessage 单点；`uia`=UIA Invoke/Toggle/Expand/Select（命中元素没有 pattern 时最多往上找 4 层祖先，同一顶层窗、包含该点、不落到 Document/Pane/Window；结果 `steps[i].uia` 带 `ancestor_hops` 和原命中 `hit`；仍找不到则 fail-closed，不退 post/input；按 `--uia-name`/`uia:#N` 规划的步，解析出的控件或原命中元素（祖先时）的名字（忽略图标字形和空白）要和节点一致，类型按名字对上的那个核；节点没有名字时只核类型，否则 `reason: uia_target_mismatch` + `expected_uia`/`resolved_uia`，什么都不 Invoke；命中元素没 pattern 时 `reason: uia_no_pattern`）；`auto`=命中 stamp 上可 Invoke 等 → uia，否则 input（按有名字的节点规划的步：名字/类型对不上、命中 Document/Pane/Window、命中元素没 pattern 且它自己的名字或类型不是节点的、点上没元素、落到别的顶层窗或 UIA 探测失败，都 `uia_target_mismatch` 拒绝，`uia_unverified` 说明原因，dry-run `planned_foreground_click_count` 为 0，`--go` 什么都不发；只有没 pattern 的命中元素自己名字和类型都对上时才 input 点；纯 `--norm`/`--xy` 步和没名字的节点照旧退回 input）（永不选 post；软 miss 同点再探一次；dry-run 报 `chosen_method` + `uia_reprobe`/`uia_first` + `foreground_used` / `foreground_click_count`）。均仅 left 单点 norm/xy/ids；drag/path 等请显式 `method=input`。没装 `uia` extra 时 `--method uia` 退出码 3 并给 `missing_extra` / `install`（同 `perceive --uia`）；`auto` 退回 input 并在 `warnings` 里说明（按有名字节点规划的步改为拒绝）。
   - 链修饰键：`--hold-key ctrl`（MCP `hold_key`，接受 `ctrl`/`control`/`shift`/`alt`）在链的第一个点击/拖/滚时按下，链结束（含失败）再松开；同一次调用里必须有 norm/xy/ids 点击、拖动或滚轮（只有 hover 也不行），否则 `reason: hold_key_needs_pointer_step`（组合键用 `--key ctrl+pagedown`）。`--go` 时只在 stamp 窗在前台时才按（见下方检查），不在前台加 `--focus`。不是 `--hold`（那是鼠标长按）。Explorer/Office/列表多选：`--hold-key ctrl` + 多个 `--norm`/`--ids`，界面要落稳再加 `--gap`。Shift+click 头尾可以两次点击、不设 hold_key；需要时再用 `--hold-key shift`。
   - 会动的选中（动画/弹起）用 `--gap 0.4`；按钮、丝带、浏览器标签保持默认 0.12，不要全局加大。
   - 键：`--type` / `--text` 默认剪贴板+Ctrl+V，并尽量还原原来的 Unicode 剪贴板；原来是空的就再清空，避免粘贴内容留在剪贴板（MCP 字段是 `text`，`via=paste|keys`；`keys` / `--slow-type` / `--via keys` 是逐字符 SendInput，表格没进编辑态时 Ctrl+V 会被丢掉，用这个）。剪贴板失败返回 `ok:false` + `reason:clipboard_failed`。读写在一个常驻子进程里，原生崩溃不会打死 MCP 服务，也不会每次粘贴都新起进程。打完读的是目标窗口里焦点控件的值，`typed_verified` 为 true/false/null；对不上是 `reason:typed_mismatch`，不会报 ok。密码框（UIA IsPassword 或 ES_PASSWORD）跳过读回：`typed_verified` 为 null，`typed_note` 说明跳过了。`--key` 可写多次并按出现顺序执行，也可 `ctrl+a,enter`。键名：`a`-`z`、`0`-`9`、`f1`-`f24`、`num0`-`num9`、`enter` `tab` `esc` `space` `backspace` `delete` `insert` `home` `end` `pageup` `pagedown` `up` `down` `left` `right` `ctrl` `shift` `alt` `win` `menu` `capslock` `numlock` `scrolllock` `printscreen` `pause`、标点 `comma` `period` `minus` `equals` `semicolon` `slash` `backslash` `quote` `backquote` `bracketleft` `bracketright`、`volumeup` `volumedown` `volumemute`。不分大小写，`_`/`-` 忽略，`page_down`/`PgDn`/`Return`/`Escape`/`del` 也认；`+` 两边的空格忽略（`ctrl + end`），`page down` 算一个键，`tab enter` 是两个。未知键名连 dry-run 都 `ok:false` + `reason: unknown_key` + `valid_keys`。
   - **已有字的输入框用 `--replace`（MCP `replace=true`）。** 它在每次 `--type` 前清空焦点上的 Edit / MaskedTextBox：先 End、Shift+Home、Delete（掩码框的 SetValue("") 只清显示，内部日期还在），普通 Edit 再试 EM_SETSEL+WM_CLEAR。WinForms MaskedTextBox 的类名是 `WindowsForms10.EDIT...`，没有 "masked"；没有字母或数字的值（`____-__-__`）算空。UIA/legacy 文本里带 masked 也按掩码框处理。**不发 Ctrl+A**（经典 TextBox 不认，会变成追加，`WEB-02WEB-02`）。粘贴会先写好剪贴板再清空，剪贴板失败不会把原内容清掉。读得回内容且仍非空则 `reason:replace_failed`，不再打字。打完读回对不上是 `typed_mismatch`。不加 replace 时 click+type 会插到光标处。一次做完：`act --norm 491,63 --type URL --replace --key enter --go`。
   - **输入法**：结果里的 `ime` 是目标窗口线程的键盘布局（`hkl_hex` / `ime_open` / `ime_active`）。微软拼音这类 IME 开着时，按键会被吞掉但每步仍是 `tapped:true`，这时有 `ime_active` 警告。`ime=off`（CLI `--ime off`）只在这一次 act 里关掉那个窗口的 IME，并从**已经安装**的布局里挑一个英文/拉丁布局切过去（`GetKeyboardLayoutList`，不会 LoadKeyboardLayout 装一个新的）；没有拉丁布局就只关 IME。切完会再读布局，没切成则 `reason:ime_failed`。结束再还原，不改系统默认。`ime=off` 已经处理过的这次不再另报 `ime_active`。清空或打字前，键盘焦点所在顶层窗（hwndFocus 的 GA_ROOT）必须就是 stamp hwnd。子编辑框算在里面；盖在主窗上的模态对话框不算，`reason:focus_outside`，对话框里的内容不会被清掉。
   - **被挡住的窗口可以 perceive，但 `--go` 不会把输入送进别的窗口。** 窗口 stamp 上每一步发送前都核：点击 / 拖动起点和终点 / 滚轮的那个屏幕点，最上层窗口必须是 stamp 窗（或它拥有的对话框、弹窗、菜单），否则 `ok=false` + `pointer_refused_step` + `point_window`（真正在那里的 hwnd 和标题）；每个 `--type`/`--key` 前 stamp 窗（或它拥有的窗，比如刚点开的对话框）必须在前台（点击之后也核），并且键盘焦点的顶层窗必须是 stamp hwnd，否则 `keyboard_refused_step`。`--hold-key` 也算按键：在第一个点击/拖/滚的点通过后、且 stamp 窗（或它拥有的窗）在前台时才按下，否则 `hold_key_refused_step`，一个键都不按。被拒那步及之后都不发。要先把它拉到前台加 `--focus`（MCP `focus=true`）：拉前台后再核 identity 和点/前台，仍被挡（比如置顶窗）照样拒绝。dry-run 的 `occluded_points` 列出现在被挡的点并给警告。点还会对 stamp 窗的进程核一次：点上没有窗口、点在 stamp 窗矩形外且不是它拥有的窗（只有 stamp 窗拥有的对话框/弹窗/菜单可以在矩形外，同进程的其它顶层窗也拒绝）、点上的窗属于别的进程、或 stamp 窗是透明悬浮窗让点击穿到别的程序 → `reason: point_not_on_target`（`point_window.why` = `no_window`/`outside_window`/`other_process`/`transparent_overlay`，带 hwnd/title/pid），不试 `--focus`，一个输入都不发；dry-run 列在 `points_not_on_target` 并警告。只管 `method=input` 和 auto 里选了 input 的步；uia / post 不变。屏幕 stamp 没有窗口可核：键必须同一次 act 里先点，且只在那次点到的窗口在前台时发送。
   - **ids 不是静默聚焦**：同一次 `act` 里 `ids`/`norm`/`xy` 会先真正点击，再 `text` 粘贴。点的必须是可编辑框本身；点到标签/按钮再 type 不会自动聚焦输入框。`scroll`+位置会变成 hover 不点击，此时同链 `text` 也不会靠 ids 聚焦。
   - `--button right`；`--hover`；`--scroll down:3` / `up:3`（优先这样写；MCP `scroll` 可以是数字或字符串）。裸数字带符号：负数 = 往下（页面底部），**正数 = 往上**，裸正数会给 warning
   - 被拒时结果带 `reason`（如 `point_covered`、`not_foreground`、`identity_changed`）、`hint`（下一步，比如 "retry with focus=true"）和挡路窗口 `blocking_window {hwnd, title}`。
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
| 打字 | `--type` / `--text` / `--via paste\|keys` | `text` + `via=paste\|keys`（默认 paste） |
| 清空再打字 | `--replace` | `replace=true`（Edit/掩码：End+Shift+Home+Delete，不是 Ctrl+A，也不是 SetValue("")） |
| 输入法 | `--ime off` | `ime=off`（只用已安装的英文/拉丁布局；没有就只关 IME；切完要核实） |
| 读哪张图 | `map` | `map`（默认 jpeg-1280 内联，`map_inline=false` 只路径） |
| 出图目录 | `--out` | `out_dir`。写进 WSL workspace 用 UNC `\\wsl.localhost\Ubuntu\home\<user>\...`，不要 `/home/<user>`（会落到 `C:\home\<user>`）。`/mnt/c/...` 仍可用。另有 `map_wsl` |
| 拉前台再抓 | `--focus` | `focus=true`（默认 false，不抢前台） |
| 采集后端 | `--capture auto|wgc|printwindow|screen|dwm` | `capture`（`screen` 需 focus；`dwm` 实验性） |
| 跨帧脚本 | `plan FILE [--go]` | `plan`（`go` 只在调用上；见下） |
| 操作员停止 | —（只有 MCP） | `abort`（默认拉起停止；`status=true` 只读；`clear=true` 解除——那是操作员的事） |

`launch` / `launch_app` 默认真启动；`launch_app(dry_run=true)` 只解析 argv 和 profile 守卫（plan 里不带 go 的 launch 也只预演，并报 `would_args`）。

### act 结果怎么读（verification / abort）

- 每个 MCP `act` 结果都有 `verification` + `evidence`（`steps_sent` 等）+ `abort_active`。
- `verification`：`observed`（有改动被测到）/ `sent`（发出了，没确认落地）/ `partial`（发了一部分后失败，含逐字打字中途失败）/ `refused`（什么都没发）/ `aborted`（操作员停止）/ `null`（dry run）。
- `partial` 时先 perceive 看现场再决定，不要整段重发（可能重复输入）。
- **硬规则**：看到 `verification="aborted"` 或 `abort_active=true` → 停手，向人汇报；不要重试，不要自己调 `abort(clear=true)`。停止只管桌面 `act`，不管 `browser_act`。

### 跨帧脚本：`plan`

单帧里的多步用 `act` 的 `steps=[...]`。要跨帧（点了以后等结果、看情况再点）用 `plan`：一次调用里 perceive → act → 等条件 → 分支 → 再 perceive。CLI `cu-perceive plan plan.json [--go] [--validate]`（`-` 读 stdin），MCP `plan(plan=..., go=false)`。

- 操作：`perceive`（`as` 起名，`from` 按原来的检测器重看同一个窗）、`act`（`stamp_ref` 指名字；字段同 `act`，可带 `steps`）、`wait`、`wait_for`、`wait_until_stable`（等到画面不再变：128x128 灰度缩略图 + 容差比较，光标闪烁不算变化、转圈算；`stable_ms` 默认 800、`tolerance` 默认 1.5，不收敛报 `never_settled`）、`assert`、`if`/`then`/`else`、`repeat`（`times` ≤ 20，可带 `until`）、`launch`、`stop`。每个操作可带 `on_fail: "continue"`。plan 之前已有的 stamp 用 `stamps: {"main": "<stamp>"}` 起名。plan 级 `stop_on_challenge`（默认 `true`）会让 perceive / act 看到人机验证页时以 `reason: "challenge"` 停下，`false` 才放行。
- 条件只有这几种，不能写代码：`window`（`title` / `exe` / `class` / `minimized`；`match`：`contains` 默认子串、和以前一样，`exact` 整个标题或类名相同，`regex` 是不分大小写的 `re.search`，作用于 title 和 class，exe 仍是子串；`match` 写在 window 里面或旁边都行。浏览器标题带后缀（「Hacker News - Google Chrome」「Hacker News - Personal - Microsoft Edge」），所以判「离开了某页」用锚定的 regex：`{"not": {"window": {"title": "^Hacker News( - |$)", "match": "regex"}}}`；用 contains 时文章页「… | Hacker News - Google Chrome」也算命中，永远是 false；对裸页名用 exact 永远不命中浏览器窗口，它的 not 一上来就是 true。regex 只读标题前 512 个字符，并拒绝「重复里套重复」的写法（`(a+)*`、`(\w+\s?)*`，网页标题能让它回溯几分钟）；exact / regex / class 按 z 序筛窗口（`first` 不按 `windows(title=)` 的规则排），没标题的窗口不在列表里）、`foreground`、`text`、`uia`、`challenge`（屏幕上是不是人机验证/拦截页，固定词表：just a moment、performing security verification、verify you are human、checking your browser、turnstile、captcha…；`true`/`false` 互反，命中项在 `hits`）、`last`（上一步结果的字段，如 `{"last": {"ok": false}}`），用 `all` / `any` / `not` 组合。`not` 的结果行是 `{"met": M, "not": {"inner_met": 内层结果, "met": M, …内层证据}}`：`not.met` 就是取反后的结果，跟外层一致（以前 `not.met` 是内层结果，容易读反）。`text` 条件可带 `"confirm_frames": 2`（1..3，默认 1 不开）：命中后立刻再 perceive 一帧，下一帧也命中才算（OCR 的项集合会跨帧闪），结果里有 `confirm_frames` 列出每帧 stamp，第二帧没了记 `unstable: true`；不稳就是「判不了」，永远不算满足：套在 `not` 里也不满足（`not.undetermined: true`），`any` / `all` / `not` 的结果取决于它时也带上 `unstable`，所以 `{"not": {"any": [...]}}` 不会把一次闪烁当成「没有」。`text` / `uia` 读的是 stamp，那个 stamp 要带 OCR 或 UIA；在 `wait_for` 和 `repeat.until` 里每次检查都会重新 perceive，别处要写 `"fresh": true`。条件算不出来（没有文字来源、没有这个 stamp）算失败，不当成 false。
- **浏览器上做文字条件会连地址栏一起读**：条件写 `"claude-code"` 会先命中地址栏里的 URL，`wait_for` 立刻返回，下一步 `act` 就打到还在加载（甚至正是 Cloudflare「Just a moment… / Performing security verification」人机验证）的页面上（反面教材：`docs/examples/plan-browser-challenge-trap.json`）。三条规则（真机踩过，正例 `docs/examples/plan-browser-read-body.json`）：① `perceive` 加 `"roi_norm": "500,560,440"`（或 `"roi": "L,T,W,H"`）只读页面正文，刷新沿用同一裁剪；裁剪 stamp 是 verify-only，只能给条件用，`act` 要用全窗 stamp（否则 `verify_only_stamp`）；② 等只有加载完成才有的值，并让画面先稳：`{"op":"wait_until_stable","stamp_ref":"body","stable_ms":800}`，或 `{"all":[{"text":"2.1.322"},{"not":{"text":"security verification"}}]}`；③ 人机验证页是「停」不是「一步」——`{"challenge": true}` 是条件，默认 `stop_on_challenge` 让 perceive/act 看见它就停（`reason: "challenge"` + 命中词）。**没 settle 的那条分支里不要放 `act`**——挑战页会照单全收你发过去的按键。
- act 的步可以写 `"ocr_text": "保存"`，在这一步用的 stamp 上按文字找 OCR 项：先精确（不分大小写）再包含，不是恰好一个就拒绝：`target_not_found`（带 `similar`：最像的几条 OCR 文字，OCR 把 `|` 读成 `I` 这种就看得出来）或 `ambiguous_target`（带 `candidates`）；两种都带 `stamp`、`match_count`、`ocr_items`。加 `"confirm_frames": 2`（1..3，默认 1 不开）会立刻再 perceive 同一个窗（同样的检测器），只有每个 `ocr_text` 在下一帧文字不变、位置差不超过 20（0-1000）才动手，而且用新那一帧的 stamp（一个 act 里取最大的 `confirm_frames`，对它所有 `ocr_text` 步都生效）；因此确认的 act 里不能再有按 `ids` 或按节点编号的 `uia_name`（`#N`、`uia:#N`、纯数字）指目标的步（它们是第一帧的编号），`norm` / `xy` / `key` / 按名字的 `uia_name` 可以；不稳就 `unstable_target`（带 `frames`），第二帧找不到就是 `target_not_found` + `frame: 2`。多花一次 OCR（约 3–7 s）。
- `go` 只能在调用上给，plan 里写了 `go` 直接拒绝。dry-run 会真的 perceive 和检查条件（只读），act 只规划，不等待；它的 `perceive` `focus: true` 只记录不执行（`focus_planned`，真跑才抬窗），`abort` 开关对 dry-run 同样生效。第一个 act 之后的检查看到的不是真跑时的画面，标 `speculative`（行上和 `cond` 里都有），不通过也只是 `ok: null` + `reason` / `would_fail_now`，不会停。这些行同时汇总在 `would_fail`（`[{path, op, reason, error}]` + `would_fail_count` + 一行 `warnings`）——所以写错 `ocr_text` 或 stamp 过期不会藏在 `ok: true` 后面，dry-run 要看 `would_fail`。它只说明“按现在的画面会失败”，不是对真跑的预测：前一步点完才出现的目标也会列在里面，要自己分辨是写错了还是还没出现。同样的原因，第一个 act 之后的 `if` 在 dry-run 里两个分支都规划（`branch: "both"`，`branch_now` 是按现在画面会走的那支），分支里的 `stop` 只结束那一支（`branch_stopped` / `would_stop` / `stop_ok`），不会让 dry-run 停下——每个分支是真跑的一种走向，另一支和 `if` 之后的操作照样规划；这些行汇总在 `would_stop`（+ `would_stop_count`），其中 `stop_ok: false` 会另加一条 `warnings`（真跑会以 `ok: false` 收场）。`planned_acts` 是“真跑无论走哪支都会发的那些”，分支不确定时另有 `planned_acts_at_most`。真跑只对窗口 stamp 动手（否则 `no_window_stamp`）。
- 上限：一次调用最多 64 个操作（`if` / `repeat` 本身算一个，里面跑的另算），嵌套 4 层，`timeout_ms` 默认 120 秒，`interval_ms` 只允许 100..1000。被闸门挡下的操作也会留一行（`gated: true`、`ran: false`），所以 `stopped_at` 一定能在 `results` 里找到。真跑的 `wait` 报实际睡了多久（`waited_sec`，旁边有 `requested_sec` 和 `waited_short_by_sec` = `sec - waited_sec`）和为什么早停（`interrupted`: `aborted` / `deadline`），不会拿满额 `sec` 糊过去；`abort` 和 deadline 在 `wait` / `wait_for` 里至少每 250 ms 查一次（不管 `interval_ms` 多大），被这样打断的 `wait_for` 也报 `interrupted` 并以 `aborted` / `timeout` 停下，`wait_timeout` 只表示它自己的 `timeout_ms` 用完。64 个操作的上限在运行时只在两步之间检查，第 64 步会完整跑完；另外校验时会静态数一遍（`static_ops`：`least` = 每步都成功、`until` 一上来就满足、`if` 走短的那支时至少跑几个，`most` = 最多跑几个）：`least` 超过 64 在校验阶段就以 `bad_plan` + `over_cap: true` 拒绝（`validate_only` 也一样），除非 plan 写 `"resumable": true`；`most` 超过 64 只给一条 `warnings`。**续跑**：因 64 上限（`max_ops`）或 `timeout_ms`（`timeout`）停下的 run 会返回 `continuation = {"resume_from": "<没跑的那个操作的 path>", "stamps": {所有命名帧 + "latest"}, "resumable": true}`，把它原样合进同一份 plan 再调一次，就从那一步接着跑（在 `if` 里沿用上次选的分支，在 `repeat` 里先跑完那一轮再继续，`until` 照常检查），`frames.latest` 和命名 stamp 都带过去；途经的容器行带 `resumed: true`，结果带 `resumed_from`，`last` 条件在续跑开头是空的。被 `abort` 停下的没有 continuation。dry run 的 continuation 另带 `from_dry_run: true`（规划过 act 的再带 `speculative: true`）：只能接着 dry run，带 `go` 会以 `bad_plan` 拒绝（前面的 act 根本没发）；`speculative` 让续跑的检查照旧进 `would_fail`；断点前两支都规划的 `if` 续跑时只接断点所在那一支。dry run 会把这种 `if` 两支都规划，所以 `most` 不到 64 的 plan 在 dry run 里也可能撞上限。对老调用方的变化：`not.met` 现在是取反后的结果（内层看 `not.inner_met`）；`least` 超 64 的 plan 不再先跑 64 步，而是直接拒绝（dry run 也是）；continuation 不绑定原 steps，续跑时要用同一份 steps。结果里 `results` 每个操作一行（`path` 如 `3.then.0`），停了看 `stopped_at` / `stop_reason`；`verification` 和 `act` 一样，另有 `no_input`（真跑但没发任何输入）。前面做完的不会回滚。
- 先 dry-run 给用户看，用户同意后同一份 plan 加 `go=true`。

## WSL / Grok Build / minimal-agent-ts

从 WSL 调 Windows 上的 Python（不要用 Linux python）：

    CU_PERCEIVE_PYTHON=/mnt/c/<python-dir>/python.exe <checkout>/bin/cu-perceive.sh windows
    <checkout>/bin/cu-perceive.sh perceive --hwnd N
    <checkout>/bin/cu-perceive.sh act --stamp STAMP --drag-norm ...

MCP 本机 loopback（不碰 8000 的 windows-mcp）：

    http://127.0.0.1:8771/mcp

Grok: ~/.grok/config.toml 里 [mcp_servers.cu-perceive]。
minimal-agent-ts: agent.json 的 mcp_servers 里同名一项。
默认端口 8771（`--port` 可改）。
MCP 默认带压缩图；要全分辨率走磁盘路径。CLI 仍用 map 路径。WSL 用返回的 `map_wsl` / `png_wsl`。写进 Linux workspace 用 UNC： `\\wsl.localhost\Ubuntu\home\<user>\...`（发行版名用 `CU_PERCEIVE_WSL_DISTRO` 改）。 不要传 `/home/<user>/...`，Win Python 会当相对路径写到 `C:\home\<user>`，不报错。 `/mnt/c/...` 仍映射到 NTFS。origin.left <= -10000 是最小化哨兵，丢掉 stamp 重 perceieve。


## Agent 易踩坑（K3 / 88yr 2026-09-05）

- MCP `act` 的字段是**扁平字段**（`norm`/`xy`/`ids`/`text`/`key`…）；多步用 `steps=[{...}]`，每步是同样的扁平字段（PR #9 起，最多 20 步，带守卫）。未声明的参数（`x`、`action`…）一律拒绝：调用报错，错误文本是 JSON（`reason: "unknown_args"`、`unknown_args`、`valid_args`），什么都不执行。若解析后零步骤，返回 `ok:false` + error。
- 桌面 `act`：`dry_run=true` 优先于 `go=true`（只做 dry run，带 warning）；只传 `dry_run=false` 不会发送，要发送就传 `go=true`。
- 浏览器写操作（`browser_act`，以及 `browser_session` 的 start/stop、`browser_tabs` 的 create/close/select/borrow/return）默认执行。`dry_run=true` 只返回计划的 `bsk` 命令。`go` 不拦。要恢复「默认 dry-run、`go=true` 才执行」，设 `CU_PERCEIVE_BSK_REQUIRE_GO=1`。
- 坐标可以是字符串 `"640,500"` / `"177,752;500,200"`，也可以是数组 `norm=[640,500]`、`xy=[[10,20],[30,40]]`。给 4 个数（OCR item / UIA node 的 `norm` 框 left,top,right,bottom）就点框中心并带 warning。鼠标键是 `button="left|right|middle"`，没有 `click=`。`step_px` 只管拖动平滑度，跟滚动距离无关。
- `perceive_window(compact=true)` 的 item 是 `{id, text, kind, center_norm}`，UIA 节点同样短，但带 `value`（只读格子、标签、对话框正文都留着，过长会截断）。被别的 MDI 子窗挡住的节点不列出（`uia_occluded` 计数）。`center_norm` 直接给 `act(norm=...)`。stamp 超过 120 秒会有 `stamp_stale` warning，要硬拒绝就传 `max_age_sec`。
- 参数里以 `_` 开头的键（如 `_meta`）直接忽略，不报错。
- `scroll` 单位是滚轮格数（一格约三行），不是像素；超过 50 格按 50 格发，返回 `scroll_clamped: true` + warning。
- OCR/YOLO 每次调用有超时（`CU_PERCEIVE_DETECT_TIMEOUT`，默认 30 秒，新 worker 第一次 3 倍）。超时就杀掉 worker 重开，perceive 照样返回 stamp，只是没有识别结果并带 warning。不开 ocr/yolo 的 perceive 不会等 worker。
- 点击例：`act(stamp="...", norm="935,69", go=true)`；坐标逗号（也认冒号）。只有键：`act(stamp="...", text="hi", replace=true, go=true, focus=true)`。
- 完整回路：`windows` → `perceive_window` →（稳定 UI）同 stamp 多步 dry `act` → 用户 批后再 `--go` → 再 `perceive_window` 验证（点后优先 `--roi-norm` 轻复验，首次选点/规划仍全窗）。窗挪了或 ROI stamp → 重 perceive，勿盲点。

## 不要

- 云电脑桌面
- 没说 `--go` 就点或拖桌面（`act`）。网页的 `browser_act` 默认就会执行；只预览要传 `dry_run=true`
- 用 `act` 去点网页里的链接或表单；那是 `browser_act`，先 `browser_observe`
- 用 Win11 记事本当桌面测试窗口。它把文件开成已有窗口里的标签，清空了也会问要不要保存。桌面测试用自己的程序或窗口
- 默认开 OCR 或 YOLO
- 给游戏挂机写方案
- 远程 HTTPS 当已接通（Grok Bot 后端不在 tailnet；先 CLI）
