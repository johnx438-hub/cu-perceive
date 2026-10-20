# cu-perceive (Ez-Computer-Use)

Windows computer use. Pin one window, read its 0-1000 grid map, then act.

One core, two faces:

- CLI: `cu-perceive perceive|windows|act` (or `python -m cu_perceive ...`)
- MCP: `cu-perceive mcp` -> http://127.0.0.1:8771/mcp (loopback only; do not bind 0.0.0.0)

## Contract

- The grid map is the map. Click / type / drag by `norm` (0-1000) or window `xy`.
- Perceiving a window does **not** steal focus. The window is captured where it
  is, also when other windows cover it. `--focus` / `focus=true` raises and
  restores it first (old behaviour).
- Minimized windows are reported (`ok=false`, `minimized=true`), not restored,
  unless `--focus` / `focus=true` is passed.
- Every window stamp records `identity = {hwnd, pid, ctime}` (process creation
  time). `act` re-checks it before planning and before every live step. A
  closed window whose HWND was reused by another process fails closed.
- OCR, UIA and YOLO are optional extras, off by default.
- Desktop `act` defaults to dry-run. `--go` / `go=true` only when the user said so.
- Browser acting verbs run unless `dry_run=true`. `go` is accepted and does not
  gate them. `CU_PERCEIVE_BSK_REQUIRE_GO=1` dry-runs those calls unless `go=true`
  (`dry_run=true` still wins). Desktop `act` ignores that switch.
- A web page inside Chrome or Edge is a browser task. Those tools call the
  external `bsk` CLI. Other apps, and the browser window as a desktop window,
  stay on `windows` / `perceive_window` / `act`.

## Install

Core (grid map, background capture, act with `input` / `post`):

    python -m pip install "cu-perceive @ git+<repo-url>"
    # or from a checkout:
    python -m pip install -e .

Optional extras, each loaded on first use. A missing extra is never
auto-installed; the error prints the exact command for your install:

| Extra | Enables | Command (from a checkout) |
|---|---|---|
| `ocr` | `--ocr` text ids (RapidOCR) | `python -m pip install -e ".[ocr]"` |
| `uia` | `--uia`, `act --method uia|auto`, `--uia-name` | `python -m pip install -e ".[uia]"` |
| `yolo` | `--yolo` ScreenParser icons. **AGPL-3.0**, pulls torch (large) | `python -m pip install -e ".[yolo]"` |

YOLO weights are not in git; see `weights/README.md`.

OCR and YOLO run in a separate worker process, started on first use and reused
afterwards. Loading onnxruntime into the process that captures windows makes
the next Windows.Graphics.Capture grab crash that whole process (an access
violation in `windows_capture`), which used to take the MCP server down after
any `ocr=true` call. The worker keeps onnxruntime out of the server, and if the
worker dies only that perceive loses its detections (with a warning); the next
call starts a new one. As a second guard, capture never starts WGC in a process
where onnxruntime is loaded: `auto` goes straight to PrintWindow and
`capture.attempts` shows WGC as `skipped: "unsafe"`; an explicit `--capture wgc`
fails instead of crashing, with `reason: "wgc_refused_onnxruntime_guard"` and an
error that says the onnxruntime guard refused it. `CU_PERCEIVE_DETECT_INPROC=1`
runs OCR/YOLO in-process (for debugging), which then turns this guard on.

Each OCR/YOLO call has a timeout, `CU_PERCEIVE_DETECT_TIMEOUT` seconds (default
30; `0` waits forever). The first call of a new worker, which loads the models,
gets three times that. A call that runs past it kills the worker and starts a
fresh one, and the perceive still returns its stamp, without detections and
with a warning. Only one detection call runs at a time. A second one waits for
the worker at most that long, then returns its stamp with a "busy" warning.
Perceive calls without `ocr`/`yolo` never wait on the worker. The MCP server
runs every tool call in a worker thread, so a slow OCR call doesn't hold up
other tool calls either. Live `act` chains still run one at a time.

## Window capture

`perceive --hwnd N` grabs the client area without touching z-order. Backends,
tried in order (`--capture auto`, the default):

1. `wgc`: Windows.Graphics.Capture via the
   [`windows-capture`](https://pypi.org/project/windows-capture/) wheel (x64).
   It asks for a borderless capture on Windows 11 / build 20348+. Older
   builds may flash the yellow capture border.
2. `printwindow`: `PrintWindow(PW_RENDERFULLCONTENT)`, then flag 0.

A grab that is all black counts as a miss and the next backend is tried.
`--capture wgc|printwindow` forces one backend. `--capture screen` is the
legacy screen-region grab and needs `--focus`. The JSON reports which backend
won under `capture.backend`, with every attempt in `capture.attempts`.
Windows on another virtual desktop, or cloaked windows, may come back blank.
A minimized window is reported with `capture.skipped = "minimized"`. A window
that is parked off-screen (left/top at -32000 without being minimized) or whose
client area is under 8 px on a side (for example 144x1) fails with `ok: false`,
no stamp, a `degenerate_client` field, `reason` (`offscreen` or `tiny_client`)
and a `hint`, also with `--focus`. `--focus` doesn't move such a window back:
restore it from the taskbar, or select it and press Win+Shift+Left/Right, then
perceive again. A client area under 80 px on a side is captured with a warning,
because `act` refuses such stamps.

`--capture dwm` is **experimental** and not part of `auto`. It renders a DWM
thumbnail into an off-screen window and reads it back with BitBlt, but DWM
composes thumbnails into the desktop rather than into that window's bitmap, so
on Windows 11 it returns an all-black frame. It is kept only for experiments
and adds a warning to the result.

Stamps are named `YYYYMMDD-HHMMSS-mmm` (milliseconds), with `-2`, `-3`, ... if
that name is already taken, so two perceives never overwrite each other. A stamp's
`.json` only appears once it is complete (it is written to a temporary file and
renamed into place), so `act` never reads a half-written stamp. Perceive also
records each window's newest stamp in `<out_dir>/.latest/hwnd-<N>`, so
`act --hwnd N` without a stamp reads that stamp and any newer files only, not
every json in a large output directory.

## Acting

- `act` runs clicks first, then `--type` / `--key` in the order given on the
  command line. The MCP `act` tool runs clicks, then `text`, then `key`.
- `--key` takes key names or chords: `a`-`z`, `0`-`9`, `f1`-`f24`, `num0`-`num9`,
  `enter`, `tab`, `esc`, `space`, `backspace`, `delete`, `insert`, `home`, `end`,
  `pageup`, `pagedown`, `up`, `down`, `left`, `right`, `ctrl`, `shift`, `alt`,
  `win`, `menu`, `capslock`, `numlock`, `scrolllock`, `printscreen`, `pause`,
  punctuation (`comma`, `period`, `minus`, `equals`, `semicolon`, `slash`,
  `backslash`, `quote`, `backquote`, `bracketleft`, `bracketright`) and
  `volumeup` / `volumedown` / `volumemute`. Case, `_` and `-` are ignored, and
  common spellings work too (`page_down`, `PgDn`, `Return`, `Escape`, `del`).
  Join a chord with `+` (`ctrl+shift+t`) and list several keys with commas
  (`tab,enter`). An unknown name fails the plan, dry run included, with
  `reason: "unknown_key"` and `valid_keys`; `act --help` prints the list.
  Spaces around `+` are ignored (`ctrl + end`), and an entry that is one key
  once its spaces are removed stays one key (`page down`); other spaces separate
  keys (`tab enter`).
- `--scroll` (MCP `scroll`, an int or a string) takes wheel notches. Prefer the
  explicit forms `down:N` (toward the bottom of the page) and `up:N`. A bare
  number is signed: negative scrolls down and **positive scrolls up**, so a
  bare positive number adds a warning that says so. One notch is one wheel
  click (about three lines), not a pixel. More than 50 notches is capped at 50,
  with `scroll_clamped: true` and a warning; repeat the call to go further.
- MCP `act` takes points as `"x,y"` strings (`"177,752;500,200"` for several)
  or as arrays: `norm=[640,500]`, `xy=[[10,20],[30,40]]`, also for `drag` /
  `drag_norm`. Four numbers (the `norm` box of an OCR item or UIA node,
  left,top,right,bottom) click the center of that box, with a warning. The
  mouse button is `button` (`left` / `right` / `middle`). `step_px` only sets
  drag smoothness; passing it without a drag adds a warning.
- A stamp older than 120 s adds `stamp_stale: true` and a warning. Pass
  `max_age_sec` to refuse old stamps instead.
- MCP `perceive_window(compact=true)` returns items as
  `{id, text, kind, center_norm}` and UIA nodes as
  `{id, name, control_type, center_norm}` plus `value` (and `readonly` when
  set). `center_norm` goes straight into `act(norm=...)`. Compact UIA keeps
  Text / DataItem / readonly Edit content (labels, message-box text, grid
  cells), not only clickable controls, and drops nodes whose center is covered
  by another element (occluded MDI children) with `uia_occluded` counting them.
  Occlusion uses `uiautomation.ControlsAreSame`. An empty `GetRuntimeId()` is
  not treated as a different control, so hwnd-less toolbars, menus, grid cells
  and lookup rows stay. A grid cell includes `readonly` when the provider
  confirms it (`true`). DataGridView cells that only report "not readonly"
  come back as `readonly: null`, and a cell value of the literal `(null)` is
  null. A UIA walk
  that runs longer than 20s (`CU_PERCEIVE_UIA_WALK_TIMEOUT`, seconds; `0`
  disables) returns the nodes collected so far plus a warning (`uia_timeout`).
- MCP tools refuse arguments they don't declare instead of dropping them: the
  call returns an error whose text is JSON with `reason: "unknown_args"`,
  `unknown_args` and `valid_args`, and nothing runs (for example `x=` or
  `action=`). `act` does take `steps=[{...}, ...]`: a guarded chain whose steps
  are objects of the flat act fields (max 20). Keys starting with `_` (such as
  `_meta`) are dropped silently. `act` takes `dry_run` as an alias:
  `dry_run=true` forces a dry run even with `go=true`, and `dry_run=false` on
  its own doesn't send anything (only `go=true` sends). Both cases add a warning.
- `--hold-key` (MCP `hold_key`) holds a modifier during the clicks, drags and
  scrolls of the same call, so it needs at least one of them. On its own, with
  only keys, or with only a hover, `act` fails with
  `reason: "hold_key_needs_pointer_step"`. For a key chord use
  `--key ctrl+pagedown` instead.
- `--replace` (MCP `replace=true`) clears the focused edit before each
  `--type`, then types. An edit or masked edit (a WinForms MaskedTextBox is an
  Edit, and `SetValue("")` only blanks its display) is cleared with End,
  Shift+Home, Delete, then EM_SETSEL + WM_CLEAR. Other value patterns use
  SetValue. It does not send Ctrl+A. A value with no letters or digits
  (`____-__-__`) counts as empty, including a WinForms MaskedTextBox whose
  class is `WindowsForms10.EDIT...`. Masked edits are also recognized from
  UIA/legacy text that says masked. If the field can be read back and is
  still not empty, the act stops with `reason: "replace_failed"` and does not
  type. Paste writes the clipboard before that clear, so a clipboard failure
  does not wipe the field. After typing, the focused element **of the target
  window** is read back: `typed_verified` is true, false, or null, and a
  mismatch is `reason: "typed_mismatch"` (never `ok: true`). A password edit
  (UIA IsPassword or `ES_PASSWORD`) skips that check: `typed_verified` is
  null and `typed_note` says the read-back was skipped.
  `act --norm 491,63 --type https://example.org --replace --key enter --go`.
- `--via paste|keys` (MCP `via`) chooses how `--type` / `text` is entered.
  `paste` (default) puts the text on the clipboard and sends Ctrl+V, then
  restores the previous Unicode clipboard text. An empty clipboard is emptied
  again so the pasted text does not linger. Clipboard failures return
  `ok: false` and `reason: "clipboard_failed"`. Get/set run in one long-lived
  helper process so a native clipboard fault cannot kill the server and a
  paste does not pay for a new process each time.
  `keys` (CLI `--slow-type` is the same) sends each character with SendInput
  unicode — use it when a DataGridView cell ignores Ctrl+V because it is not
  in edit mode. A paste whose read-back does not change is `typed_mismatch`.
- `--ime off` (MCP `ime="off"`) makes the target send ASCII for one act. It
  works on the thread that owns the **focused child** window
  (`GetGUIThreadInfo` hwndFocus → `GetWindowThreadProcessId`; WPS's document
  view is on another thread than its top-level window), falling back to the
  stamp window's own thread. It tries each **already installed** English
  layout (en-US, en-GB-US `0xf0010809`, en-GB `0x08090809`), then other Latin
  ones (`GetKeyboardLayoutList`; it never loads a layout), and verifies each on
  that thread by language id. All candidates share one **0.75 s** budget
  (`ime.verify_budget_ms`; the time used is `ime.verify_ms`). If no layout
  sticks but the IME is open, it closes the IME (`IMC_SETOPENSTATUS 0`) and
  counts that as suspended (`ime_closed=true`).
  - Still not verified: a warning plus `ime_unverified=true`, and the act runs
    anyway (keys and text). `ime="strict"` (`--ime strict`) aborts instead with
    `reason: "ime_failed"`, and the error names the mode that was passed.
  - `ime` reports `thread_id` (focus thread), `top_thread_id`, `focus_hwnd`,
    `hkl_hex` / `hkl_before_hex`, `requested_hex`, `tried`, and
    `ime_open_before/after` plus `ime_conversion_before/after`. Conversion bit
    0x1 means native (Chinese) input; Pinyin in English mode has it clear.
    These are read with `ImmGetOpenStatus` / `ImmGetConversionStatus`, or via
    `ImmGetDefaultIMEWnd` + `WM_IME_CONTROL` when the focus window is on
    another thread.
  - `ime_layout_switched=true` only for a verified, real layout change.
    `ime_restored` appears only when a layout switch or an IME close actually
    happened. It is true once that was undone and read back, and false if
    undoing it failed. `ime_suspended` alone never sets it, and nothing is
    restored when nothing changed.
  - Without `ime=off`, an active IME on a chain that types or taps keys adds
    an `ime_active` warning: the IME can swallow keys while each step still
    says `tapped: true`.
- `after` (go) carries `unlisted_foreground=true` when the foreground window is
  not in `windows()`. It also carries `popup_hwnd` and `popup {hwnd, title,
  class, why}` when such an unlisted window is in front, covers the stamp
  window's centre, or is the blocking window. That makes Qt/CEF popups that
  `new_windows` cannot see visible when they appear or disappear.
- Mouse input goes to the window on top at that point, and keys go to the
  window in front, even when the stamped window is behind another one. So with
  `--go` on a window stamp, each step is checked right before it is sent:
  - each click, drag (start and end) and scroll point must be on the stamped
    window or a window it owns (its dialogs, popups and menus). Otherwise `act`
    refuses with `pointer_refused_step` and `point_window` (the hwnd and title
    actually there);
  - each `--type` / `--key` needs the stamped window, or a window it owns (for
    example a dialog the click just opened), in the foreground, also after a
    click. Before any clear or type, the top-level window that owns keyboard
    focus (`GetGUIThreadInfo` hwndFocus, then `GetAncestor` `GA_ROOT`) must be
    the stamp hwnd. A child edit of that window matches. An owned modal dialog
    does not: the act refuses with `keyboard_refused_step` /
    `focus_outside` and does not clear the dialog's field;
  - `--hold-key` counts as a key: it is pressed at the first click, drag or
    scroll, once that step's point has passed and only if the stamped window
    (or one it owns) is in the foreground. Otherwise it refuses with
    `hold_key_refused_step` before anything is pressed.

  A refused step and everything after it are not sent. `--focus` (MCP
  `focus=true`) brings the stamped window to the front instead, re-checks its
  identity, then re-checks the point or foreground, and still refuses if it is
  covered (for example by an always-on-top window). A dry run lists points that
  are covered right now in `occluded_points` and warns. This applies to
  `--method input` and to `--method auto` steps that chose input; `uia` and
  `post` don't need the point to be visible and are unchanged.
- Every refusal also carries `reason` (a stable code such as `point_covered`,
  `not_foreground`, `not_foreground_after_focus`, `point_covered_after_focus`,
  `identity_changed` or `no_window_stamp`), `hint` (what to do next, for example
  "retry with focus=true") and, when another window is in the way,
  `blocking_window {hwnd, title}`.
- Screen-space stamps have no window to compare points with. Keys on them need a
  click first in the same `act` and are sent only while the window that click
  landed on is in front; `--hold-key` needs the window under its first point
  in front.
- `--stamp` / MCP `stamp` takes a stamp name or the `json` path a perceive
  returned.
- Without `--stamp` / `--json`, `act` uses the newest perceive stamp (desktop
  maps from `windows` are never picked) and reports `stamp_source: "latest"`.
  `--go` on it requires a window stamp with identity; for a screen-space stamp,
  pass `--stamp` explicitly. With several windows open, pass `--hwnd` / MCP
  `hwnd` too: without a stamp it uses that window's newest stamp, and with a
  stamp it refuses one taken of another window (`reason: "hwnd_mismatch"`). MCP
  drops parameters it doesn't know, so `hwnd` is the safe way to name the
  window.
- A dry run fails (`ok: false`) when the stamped window is gone or its HWND, PID
  or process creation time no longer match. Other stamp problems (moved,
  resized, too old) only fail a dry run with `--strict-stamp`, but every result
  reports `stamp_ok` and, when false, `stamp_error`.
- On `--go`, `foreground_click_count` counts the SendInput clicks actually sent
  (0 when the run is refused). A dry run also leaves it at 0; the plan is
  `planned_foreground_click_count`. Concurrent `--go` / `go=true` acts in one
  process run one at a time. A live act takes its window snapshot inside that
  lock. After a click, Enter, F-key, Alt combo, or Esc it polls for about
  250ms (`CU_PERCEIVE_NEW_WINDOW_WAIT_MS`) and stops as soon as a new top-level
  window appears, listed in `new_windows` (`hwnd`, `title`, `class`). Typing,
  scrolling, and dragging do not wait. `after` is the foreground window when
  the act returns.
- `--method uia` without the `uia` extra exits with code 3 and prints the same
  `missing_extra` / `install` fields as `perceive --uia`. `--method auto`
  without it falls back to input and says so in `warnings`.

CLI output is always UTF-8 (window titles can contain any script), whatever the
console code page is. Decode it as UTF-8. PowerShell decodes captured output
(`$x = cu-perceive ...` or a pipe) with `[Console]::OutputEncoding`, which is not
UTF-8 by default, so CJK text gets garbled; run
`[Console]::OutputEncoding = [Text.Encoding]::UTF8` first in that session.

## Browser

Page work (the document inside a Chrome or Edge tab) goes through the external
`bsk` CLI from [BrowserSkill](https://github.com/Tencent/BrowserSkill). `bsk`
is optional. pip does not install it, and this repo does not vendor its source.
Point `CU_PERCEIVE_BSK` at the executable, or leave `bsk` on `PATH`. If it is
missing, the browser tools return `ok: false`, `reason: "missing_dependency"`,
and an install hint. They do not download it.

Desktop apps stay on `windows`, `perceive_window`, and `act`. Use those when
the target is the window itself (move it, click its title bar) rather than the
page.

| Tool | What it does | When it runs |
|---|---|---|
| `browser_status` | Daemon and connected browsers | Immediately |
| `browser_session` | `list`, `start`, `stop` | `list` immediately; `start` and `stop` run unless `dry_run=true` |
| `browser_observe` | `observe` or `snapshot`; returns `@eN` refs and an `observation_id` | Immediately |
| `browser_tabs` | `list`, `create`, `close`, `select`, `borrow`, `return` | `list` immediately; the rest run unless `dry_run=true` |
| `browser_act` | click, fill, press, navigate, and the other acting verbs | Runs unless `dry_run=true` |

`dry_run=true` returns `command` (the exact `bsk ... --json` argv) and `ref`,
and does not start `bsk`. `go` is accepted on these tools and does not gate
them, so a caller that passes `go` the way it does for desktop `act` still
runs. `CU_PERCEIVE_BSK_REQUIRE_GO=1` (also `true`, `yes`, `on`) dry-runs acting
verbs unless `go=true`, and `dry_run=true` still wins. Desktop `act` keeps its
own dry-run default and ignores that switch. `browser_act` refuses a ref that
is not from the latest `browser_observe` for that session
(`reason: "stale_observation"`). `value` and `url` are passed as
`--value=<v>` and `--url=<u>`. A positional url, key, or target goes after
`--`, so a value such as `-test` is not parsed as a flag. `browser_observe`
with `cursor` reads the next page of the same capture. bsk replaces the ref
map on that page, so refs from earlier pages are not reused. A click, press,
or fill whose result reports a different page URL clears the stored refs.
`session list --json` is a JSON array; the tool returns it as
`result.items`. `browser_tabs action=borrow` can ask the user to confirm in
the browser when the command runs, which is the default. A result whose
reason is `input_outcome_unknown` means do not retry blindly; observe the
page and decide from what it shows. A bsk usage error is `reason:
"bad_args"` and the short clap line, not the usage dump.

`bsk` runs on Windows with a connected Chromium-family browser. The tests here
use a fake executable.

## Configuration

Nothing machine-specific is hard-coded. Defaults and overrides:

| Setting | Default | Env var |
|---|---|---|
| Data dir | `%LOCALAPPDATA%\cu-perceive` | `CU_PERCEIVE_HOME` |
| Stamp / shot dir | `<data dir>\shots\perceive` | `CU_PERCEIVE_OUT` |
| Weights dir | `<checkout>\weights` for a source checkout / editable install (`pip install -e .`), else `<data dir>\weights` | `CU_PERCEIVE_WEIGHTS` |
| ScreenParser weight | `<weights>\screenparser\best.pt` | `CU_PERCEIVE_YOLO_WEIGHT` |
| Quick-launch list | `<data dir>\apps.json`, else packaged `apps.default.json` | `CU_PERCEIVE_APPS` |
| Extra MCP Host names (e.g. a tailnet name behind a proxy) | none | `CU_PERCEIVE_ALLOWED_HOSTS=a,b` or `mcp --allow-host a` |
| WSL distro for UNC paths | `Ubuntu` | `CU_PERCEIVE_WSL_DISTRO` |
| Capture backend | `auto` | `CU_PERCEIVE_CAPTURE` |
| `bsk` executable (browser tools) | `bsk` on `PATH` (`bsk.exe` only on Windows) | `CU_PERCEIVE_BSK` |
| `bsk` subprocess timeout | 120 seconds | `CU_PERCEIVE_BSK_TIMEOUT` |
| Browser acting verbs | run unless `dry_run=true` (`go` is ignored) | `CU_PERCEIVE_BSK_REQUIRE_GO=1` dry-runs unless `go=true` |

The CLI and the MCP server each resolve the output dir on their own. The CLI
defaults to `%LOCALAPPDATA%\cu-perceive\shots\perceive` for the user running
it, while a server may run as another user or with its own `CU_PERCEIVE_OUT`, so
the two can write stamps to different folders. Use the `json` / `map` / `png`
paths each result returns (and pass `--stamp` / `stamp=` explicitly) instead of
assuming a folder.

## Run

    cu-perceive windows
    cu-perceive perceive --hwnd N
    cu-perceive act --stamp STAMP --norm 500,500          # dry-run
    cu-perceive mcp --port 8771

WSL wrapper: `bin/cu-perceive.sh`. Set `CU_PERCEIVE_PYTHON` to the Windows
python.exe (WSL path).

See `SKILL.md` for the operator contract and `THIRD_PARTY_NOTICES.md` for
third-party licenses.

## Tests

    python -m pip install -e ".[test]"
    python -m pytest

The tests mock Win32 and run on Linux. The Windows capture paths need a manual
check on a real Windows machine. Win11 Notepad opens a file as a tab in an
existing window and asks to save even after that tab was emptied, so a desktop
test should launch its own app or window.

## License

cu-perceive (Ez-Computer-Use) is licensed under the GNU Affero General Public
License v3.0 or later (AGPL-3.0-or-later); see `LICENSE`.

It was iterated from Enikk's modified version of the Hermes agent's computer
use. Third-party notices are in `THIRD_PARTY_NOTICES.md`, including:

- ultralytics (`yolo` extra) is AGPL-3.0.
- The ScreenParser weight `best.pt` has an ambiguous license: the model card
  says Apache-2.0, but the weight's metadata says AGPL-3.0.
- opencv-python bundles FFmpeg, which is LGPL-2.1.
- BrowserSkill (`bsk`) is an optional external program under MIT. Its source
  is not included; the browser tools only execute a binary you install. See
  `THIRD_PARTY_NOTICES.md`.
