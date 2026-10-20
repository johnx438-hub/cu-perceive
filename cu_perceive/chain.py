"""Guarded multi-step act chain (MCP act steps=[...]).

Every step is one flat act call (same fields as the single-step act) run against
the same stamp. A dry run plans every step. A live run stops at the first step
after which the screen may have changed in a way the stamp no longer describes:

- the step failed (any error, focus_outside, typed_mismatch, replace_failed, ...)
- new_windows is non-empty (a dialog / popup / menu window opened)
- the foreground window is no longer the stamp window
- the stamp window's title changed (cheap navigation / mode-change signal)

``expect_change=true`` marks the one step that is meant to change the screen
(submit, OK, Next, Enter). It is only allowed on the last step; there the
change guards are reported but do not count as a stop. Errors always stop.
"""
from __future__ import annotations

from typing import Any, Callable

MAX_STEPS = 20

# Flat act fields a step may carry. Chain-level fields (stamp, hwnd, go, dry_run,
# focus, ime, max_age_sec, strict_stamp) apply to the whole chain.
STEP_FIELDS = frozenset({
    "ids", "xy", "norm", "text", "key", "drag", "drag_norm", "step_px", "scroll",
    "button", "hover", "hold", "hold_key", "method", "uia_name", "replace", "via",
    "expect_change",
})
CHAIN_ONLY_FIELDS = frozenset({"stamp", "hwnd", "go", "dry_run", "focus", "ime", "max_age_sec", "strict_stamp", "steps",
                               "check_change", "new_window_wait_ms", "change_min_ratio"})
ACTION_FIELDS = ("ids", "xy", "norm", "text", "key", "drag", "drag_norm", "scroll", "uia_name")

RE_PERCEIVE_HINT = (
    "re-perceive the window (perceive_window hwnd=...), check what changed, then continue with "
    "remaining_steps against the new stamp (re-target norm/ids from the new map)"
)


def _bad(error: str, hint: str, **extra) -> dict:
    return {"ok": False, "reason": "bad_args", "error": error, "hint": hint, "dry_run": True, **extra}


def validate_steps(steps: Any) -> tuple[list[dict] | None, dict | None]:
    """(normalized steps, None) or (None, bad_args payload)."""
    if not isinstance(steps, list) or not steps:
        return None, _bad("steps must be a non-empty list of objects",
                          'steps=[{"norm": "500,300", "text": "abc", "replace": true}, {"ids": "4"}]')
    if len(steps) > MAX_STEPS:
        return None, _bad(f"steps has {len(steps)} entries; the cap is {MAX_STEPS}",
                          f"split the work: run at most {MAX_STEPS} steps, re-perceive, then continue",
                          max_steps=MAX_STEPS)
    out: list[dict] = []
    last = len(steps) - 1
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            return None, _bad(f"steps[{i}] is not an object", "each step is an object of flat act fields")
        step = {k: v for k, v in raw.items() if not str(k).startswith("_")}
        chain_only = sorted(k for k in step if k in CHAIN_ONLY_FIELDS)
        if chain_only:
            return None, _bad(
                f"steps[{i}] has chain-level field(s) {', '.join(chain_only)}",
                "stamp/hwnd/go/dry_run/focus/ime/max_age_sec/strict_stamp go on the act call, not inside a step",
                step_index=i,
            )
        unknown = sorted(k for k in step if k not in STEP_FIELDS)
        if unknown:
            return None, _bad(f"steps[{i}] has unknown field(s) {', '.join(unknown)}",
                              f"step fields: {', '.join(sorted(STEP_FIELDS))}", step_index=i,
                              valid_step_fields=sorted(STEP_FIELDS))
        if not any(step.get(k) not in (None, "", [], 0) for k in ACTION_FIELDS):
            return None, _bad(f"steps[{i}] has no action (norm/xy/ids/text/key/drag/scroll/uia_name)",
                              "every step needs at least one action field", step_index=i)
        if step.get("expect_change") and i != last:
            return None, _bad(
                f"steps[{i}] has expect_change=true but is not the last step",
                "a step that may open a dialog or navigate (submit/OK/Next/Enter) must be the last step; "
                "end the chain there and re-perceive",
                step_index=i,
            )
        out.append(step)
    return out, None


def _guard(result: dict, stamp_hwnd: int, title_before: str | None, title_now: Callable[[], str | None]) -> list[str]:
    """Change signals after one live step (empty list = screen still matches the stamp)."""
    reasons: list[str] = []
    if result.get("new_windows"):
        reasons.append("new_windows")
    fore = (result.get("after") or {}).get("foreground_hwnd")
    if stamp_hwnd and fore and int(fore) != int(stamp_hwnd):
        reasons.append("foreground_changed")
    if title_before is not None:
        try:
            now = title_now()
        except Exception:
            now = None
        if now is not None and now != title_before:
            reasons.append("title_changed")
    return reasons


def _brief(i: int, step: dict, result: dict) -> dict:
    keep = ("ok", "reason", "error", "hint", "dry_run", "steps", "typed_verified", "typed_note",
            "new_windows", "after", "blocking_window", "foreground_click_count",
            "planned_foreground_click_count", "warnings", "stamp_ok", "ime_skipped",
            "new_window_wait_ms", "new_window_wait_reason", "windows_snapshot_changed", "suggested_hwnd")
    b = {"index": i, "step": step, **{k: result[k] for k in keep if k in result}}
    age = (result.get("stamp_check") or {}).get("age_sec")
    if age is not None:
        b["stamp_age_sec"] = round(float(age), 1)
    if result.get("stamp_stale"):
        b["stamp_stale"] = True
    return b


def _summarize_new_windows(out: dict, done: list[dict]) -> None:
    """Top level: the last run step's wait, plus which steps opened windows."""
    if not done:
        return
    last = done[-1]
    for k in ("new_window_wait_ms", "new_window_wait_reason"):
        if k in last:
            out[k] = last[k]
    opened = [b["index"] for b in done if b.get("new_windows")]
    out["new_windows_steps"] = opened
    if opened:
        out["new_windows"] = [w for b in done for w in (b.get("new_windows") or [])]
    if any(b.get("windows_snapshot_changed") for b in done):
        out["windows_snapshot_changed"] = True
    elif any("windows_snapshot_changed" in b for b in done):
        out["windows_snapshot_changed"] = False
    if last.get("suggested_hwnd") and "suggested_hwnd" not in out:
        out["suggested_hwnd"] = last["suggested_hwnd"]


def run_steps(
    steps: list[dict],
    run_one: Callable[[dict, bool, int], dict],
    *,
    send: bool,
    stamp_hwnd: int = 0,
    title_now: Callable[[], str | None] | None = None,
) -> dict:
    """Run validated ``steps``. ``run_one(step, send, index)`` runs one flat act and returns its payload."""
    total = len(steps)
    title_now = title_now or (lambda: None)
    try:
        title_before = title_now() if send and stamp_hwnd else None
    except Exception:
        title_before = None
    done: list[dict] = []
    failed = False
    out: dict[str, Any] = {"ok": True, "dry_run": not send, "chain": True, "step_count": total}
    for i, step in enumerate(steps):
        result = run_one(step, send, i) or {}
        brief = _brief(i, step, result)
        done.append(brief)
        if not result.get("ok", False):
            failed = True
            out.update(ok=False, stopped=True, stopped_at=i, stop_reason=result.get("reason") or "step_error",
                       error=f"steps[{i}] failed: {result.get('error') or result.get('reason') or 'error'}")
            if result.get("blocking_window"):
                out["blocking_window"] = result["blocking_window"]
            break
        if not send:
            continue
        changes = _guard(result, stamp_hwnd, title_before, title_now)
        last = i == total - 1
        if changes and step.get("expect_change") and last:
            out["expected_change"] = changes
            continue
        if changes and not last:
            # Not an error, but the rest of the chain was NOT run: ok=false so callers notice.
            out.update(ok=False, stopped=True, stopped_at=i, stop_reason=changes[0], stop_reasons=changes,
                       error=f"chain stopped after steps[{i}]: {', '.join(changes)}; remaining steps not run")
            break
        if changes and last:
            out["change_after_last_step"] = changes
    out["completed_steps"] = done
    _summarize_new_windows(out, done)
    stopped = out.get("stopped_at")
    if stopped is not None:
        # A failed step did not complete; a guard stop happens after its step completed.
        start = stopped if failed else stopped + 1
        out["remaining_steps"] = steps[start:]
        out["hint"] = RE_PERCEIVE_HINT
    else:
        out["remaining_steps"] = []
    out["completed_count"] = sum(1 for d in done if d.get("ok"))
    out.setdefault("stopped", False)
    ages = [d["stamp_age_sec"] for d in done if d.get("stamp_age_sec") is not None]
    if ages:
        out["stamp_age_sec"] = max(ages)
    stale = [d["index"] for d in done if d.get("stamp_stale")]
    if stale:
        # One summary instead of making the caller read each step; never a refusal
        # (max_age_sec=N on the call is what refuses, per step).
        out["stamp_stale"] = True
        out["stamp_stale_from_step"] = stale[0]
        out["warnings"] = (out.get("warnings") or []) + [
            f"stamp went stale during the chain (from steps[{stale[0]}], {max(ages):.0f}s old at the end); "
            "the chain was not refused. If the window may have changed, re-perceive; max_age_sec=N refuses instead"
        ]
    if not send:
        out["planned_steps"] = [d.get("steps") for d in done]
    out["foreground_click_count"] = sum(int(d.get("foreground_click_count") or 0) for d in done)
    return out
