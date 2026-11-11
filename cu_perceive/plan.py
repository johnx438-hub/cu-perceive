"""ComputerPlan: a bounded, data-only script over perceive / act / wait / conditions.

One call runs several frames: perceive a window, act on that stamp, wait for a
condition, branch on what is on screen, re-perceive, act again. A plan is JSON,
never code: conditions are a fixed set of read-only checks (window, foreground,
text, uia, last) joined with all / any / not.

Ops: perceive, act, wait, wait_for, assert, if, repeat, launch, stop.

The live gate is on the call (``go``), never in the plan. A dry run still
perceives and evaluates conditions (both only read), plans every act and sends
nothing; its ``perceive focus`` is planned rather than performed
(``focus_planned``), and the operator abort switch stops it like a live run. Once
an act was planned but not sent, later checks describe a screen the live run would
have changed: they are marked ``speculative`` (row and condition) and a failing
check does not stop the dry run. An ``if`` there plans both branches
(``branch: "both"``, ``branch_now`` for the branch the current screen would take); a
``stop`` inside one ends that branch only (``branch_stopped``, ``would_stop``,
``stop_ok``) — each branch is one future of the live run. The
run lists what failed in ``would_fail`` with the reason (the same value as
``would_fail_now`` on the row) and what would stop it in ``would_stop``, so a typoed
``ocr_text`` is not hidden behind ``ok: true`` and a plan that ends in
``stop ok:false`` is not reported as clean. ``planned_acts`` counts the acts sent
whichever branch the live run takes; when a branch is unsure, ``planned_acts_at_most``
gives the total planned. A ``repeat`` cut by such a stop reports ``would_stop`` and
the rounds it planned.

Bounds: MAX_OPS executed ops, MAX_DEPTH nesting, MAX_REPEAT iterations, a wall
clock ``timeout_ms`` and the operator abort switch. The op budget is checked
between ops, so the op that reaches the cap still runs to its end. Abort and the
deadline are also read inside a running ``wait`` / ``wait_for`` at least every
``INTERRUPT_POLL_S``, which then report ``interrupted`` (``aborted`` or
``deadline``) instead of the full sleep; a ``wait_for`` that spends its own
``timeout_ms`` reports ``wait_timeout``. An op the gate refuses still gets a row in
``results`` (``gated``, ``ran: false``) so ``stopped_at`` always names a result.
A live ``wait`` reports the time it really slept (``waited_sec``, next to
``requested_sec`` and ``waited_short_by_sec``).

A ``perceive`` op may crop (``roi`` L,T,W,H in client pixels or ``roi_norm``
CX,CY[,HALF] in 0-1000): on a browser the OCR items otherwise include the address
bar, so a text condition can match a URL instead of the page, and an ``act`` then
lands on a page that is still loading. A refresh keeps the stamp's crop, a cropped
stamp is verify-only, and ``act`` refuses it in both modes.

``wait_until_stable`` samples the window until its pixels hold still
(``stable_ms``), which is the honest "loaded" signal: a spinner reports
``never_settled`` instead of a guessed yes. Human-verification / interstitial pages
are recognised by ``CHALLENGE_TOKENS``: ``{"challenge": true}`` is a condition, and
``plan.stop_on_challenge`` (default true) makes a perceive or an act that sees one
stop with ``reason: "challenge"`` and the matched token, so the plan never types at
a challenge page.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .chain import STEP_FIELDS, validate_steps

PLAN_VERSION = 1
MAX_OPS = 64
MAX_DEPTH = 4
MAX_REPEAT = 20
MAX_WAIT_S = 30.0
MAX_WAIT_FOR_MS = 60_000
DEFAULT_WAIT_FOR_MS = 5_000
DEFAULT_INTERVAL_MS = 300
MIN_INTERVAL_MS = 100
MAX_INTERVAL_MS = 1_000  # a longer check interval would hide an operator stop for that long
INTERRUPT_POLL_S = 0.25  # the abort switch is read at least this often inside a long sleep
DEFAULT_STABLE_MS = 800  # how long the pixels must hold still
MAX_STABLE_MS = 5_000
DEFAULT_STABLE_TIMEOUT_MS = 8_000
DEFAULT_STABLE_TOLERANCE = 1.5  # mean grey difference (0-255) still counted as the same screen
MAX_STABLE_TIMEOUT_MS = 60_000
# Text that only a human-verification / interstitial page shows. Deliberately narrow; the row names
# what matched and plan.stop_on_challenge: false opts out.
CHALLENGE_TOKENS = (
    "just a moment",
    "performing security verification",
    "verify you are human",
    "verifying you are human",
    "checking your browser",
    "checking if the site connection is secure",
    "enable javascript and cookies to continue",
    "attention required",
    "are you a robot",
    "i'm not a robot",
    "turnstile",
    "captcha",
)
DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000

PLAN_FIELDS = frozenset({"version", "goal", "stamps", "timeout_ms", "stop_on_challenge", "steps"})
COMMON_FIELDS = frozenset({"op", "on_fail", "note"})
PERCEIVE_FLAGS = ("ocr", "uia", "yolo", "focus")
ACT_CALL_FIELDS = frozenset({"focus", "ime", "method", "hold_key", "max_age_sec", "strict_stamp",
                             "new_window_wait_ms", "check_change", "change_min_ratio"})
ACT_STEP_FIELDS = STEP_FIELDS | {"ocr_text"}
OP_FIELDS: dict[str, frozenset] = {
    "perceive": frozenset({"hwnd", "title", "exe", "from", "as", "capture", "roi", "roi_norm",
                           *PERCEIVE_FLAGS}),
    "act": frozenset({"stamp", "stamp_ref", "steps"}) | ACT_CALL_FIELDS | ACT_STEP_FIELDS,
    "wait": frozenset({"sec"}),
    "wait_for": frozenset({"cond", "timeout_ms", "interval_ms"}),
    "wait_until_stable": frozenset({"stamp", "stamp_ref", "stable_ms", "timeout_ms", "interval_ms",
                                   "tolerance"}),
    "assert": frozenset({"cond", "message"}),
    "if": frozenset({"cond", "then", "else"}),
    "repeat": frozenset({"times", "until", "steps"}),
    "launch": frozenset({"app", "exe", "args", "user_data_dir"}),
    "stop": frozenset({"ok", "message"}),
}
# Ops whose result a later {"last": {...}} condition reads.
LEAF_OPS = frozenset({"perceive", "act", "wait", "wait_for", "wait_until_stable", "assert", "launch"})
COND_FIELDS: dict[str, frozenset] = {
    "window": frozenset(),
    "foreground": frozenset(),
    "text": frozenset({"stamp_ref", "source", "match", "fresh"}),
    "uia": frozenset({"stamp_ref", "match", "fresh"}),
    "challenge": frozenset({"stamp_ref", "fresh"}),
    "last": frozenset(),
    "all": frozenset(),
    "any": frozenset(),
    "not": frozenset(),
}
LATEST = "latest"
ACT_KEEP = ("ok", "reason", "error", "hint", "dry_run", "step_count", "completed_count", "stopped_at",
            "stop_reason", "stop_reasons", "remaining_steps", "verification", "evidence", "new_windows", "after",
            "screen_changed", "changed_ratio", "changed_bbox_norm", "layout_reflow_suspected", "expected_change",
            "expect_change_unmet", "blocking_window", "suggested_hwnd", "warnings", "stamp_stale", "aborted",
            "planned_steps", "foreground_click_count")
STOP_HINT = ("read results[] up to stopped_at; ops after it were not run. Perceive the window again and plan "
             "the rest from what is on screen now")


class PlanError(Exception):
    """A plan that is refused before anything runs."""

    def __init__(self, path: str, msg: str, hint: str = ""):
        super().__init__(msg)
        self.path, self.hint = path, hint

    def payload(self) -> dict:
        out = {"ok": False, "reason": "bad_plan", "error": f"{self.path or 'plan'}: {self}", "path": self.path,
               "plan": True}
        if self.hint:
            out["hint"] = self.hint
        return out


class _BranchEnd(Exception):
    """Dry run only: a stop inside a branch the live run may or may not take ends that branch."""


def _launch_refusal(e: BaseException) -> str:
    """The guard's reason code (profile_refused, debug_address_refused, ...) or launch_failed."""
    r = getattr(e, "reason", None)
    return r if isinstance(r, str) and r.endswith("_refused") else "launch_failed"


class OpError(Exception):
    """An op (or one of its conditions) that cannot run or be evaluated: fail closed."""

    def __init__(self, reason: str, msg: str, **extra):
        super().__init__(msg)
        self.reason, self.extra = reason, extra


# ---------------------------------------------------------------- validation

def load_plan(src: Any) -> Any:
    """dict / list as they are; a str is JSON text or the path of a JSON file."""
    if not isinstance(src, str):
        return src
    text = src.strip()
    if not text:
        raise PlanError("", "empty plan")
    if text[0] not in "{[":
        try:
            text = Path(text).read_text(encoding="utf-8-sig")
        except OSError as e:
            raise PlanError("", f"cannot read plan file: {e}") from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise PlanError("", f"plan is not valid JSON: {e}") from e


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_CROP_SHAPE = {"roi": "L,T,W,H in client pixels", "roi_norm": "CX,CY[,HALF] in 0-1000"}


def _crop_parts(v, count: int | None) -> bool:
    """Shape check for a crop spec: a comma string or a list of numbers (``count`` numbers)."""
    if isinstance(v, str):
        parts = [p for p in v.replace(" ", ",").split(",") if p]
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return False
    elif isinstance(v, (list, tuple)):
        nums = list(v)
        if not all(_is_num(n) for n in nums):
            return False
    else:
        return False
    return len(nums) == count if count else len(nums) in (2, 3)


def _check_fields(obj: dict, allowed, path: str, what: str) -> None:
    unknown = sorted(str(k) for k in obj if k not in allowed)
    if unknown:
        raise PlanError(path, f"unknown {what} field(s) {', '.join(unknown)}",
                        f"{what} fields: {', '.join(sorted(allowed))}")


def _check_ref(name, names: set, path: str) -> None:
    if not isinstance(name, str) or not name:
        raise PlanError(path, "stamp_ref must be a non-empty string")
    if name != LATEST and name not in names:
        raise PlanError(path, f"stamp_ref {name!r} is not defined",
                        f"define it with a perceive op (as={name!r}) or in plan.stamps; known: "
                        f"{', '.join(sorted(names)) or 'none'}")


def _validate_cond(cond, path: str, names: set) -> None:
    if not isinstance(cond, dict):
        raise PlanError(path, "a condition is an object", f"kinds: {', '.join(COND_FIELDS)}")
    kinds = [k for k in cond if k in COND_FIELDS]
    if len(kinds) != 1:
        raise PlanError(path, "a condition has exactly one of " + ", ".join(COND_FIELDS))
    kind = kinds[0]
    _check_fields(cond, COND_FIELDS[kind] | {kind}, path, f"{kind} condition")
    body = cond[kind]
    if kind in ("all", "any"):
        if not isinstance(body, list) or not body:
            raise PlanError(path, f"{kind} takes a non-empty list of conditions")
        for i, c in enumerate(body):
            _validate_cond(c, f"{path}.{kind}.{i}", names)
    elif kind == "not":
        _validate_cond(body, f"{path}.not", names)
    elif kind == "challenge":
        if not isinstance(body, bool):
            raise PlanError(path, "challenge takes true or false",
                            "true: a human-verification page is on screen; false: it is not")
    elif kind == "window":
        if not isinstance(body, dict) or not (body.get("title") or body.get("exe")):
            raise PlanError(path, "window needs title and/or exe")
        _check_fields(body, {"title", "exe", "minimized"}, path, "window")
    elif kind == "foreground":
        if not isinstance(body, dict) or len([k for k in ("title", "hwnd", "stamp_ref") if body.get(k)]) != 1:
            raise PlanError(path, "foreground needs exactly one of title, hwnd, stamp_ref")
        _check_fields(body, {"title", "hwnd", "stamp_ref"}, path, "foreground")
        if body.get("stamp_ref"):
            _check_ref(body["stamp_ref"], names, path)
    elif kind == "text":
        if not isinstance(body, str) or not body.strip():
            raise PlanError(path, "text must be a non-empty string")
        if cond.get("source", "any") not in ("any", "ocr", "uia"):
            raise PlanError(path, "source is any, ocr or uia")
    elif kind == "uia":
        if not isinstance(body, dict) or not any(body.get(k) for k in ("name", "control_type", "value")):
            raise PlanError(path, "uia needs name, control_type and/or value")
        _check_fields(body, {"name", "control_type", "value"}, path, "uia")
    elif kind == "last":
        if not isinstance(body, dict) or not body:
            raise PlanError(path, 'last takes an object of expected result fields, e.g. {"ok": false}')
    if kind in ("text", "uia"):
        if cond.get("match", "contains") not in ("contains", "exact"):
            raise PlanError(path, "match is contains or exact")
        if "stamp_ref" in cond:
            _check_ref(cond["stamp_ref"], names, path)


def _split_act(op: dict) -> tuple[list[dict], dict]:
    """(steps, chain-level call fields) of an act op, flat or steps=[...]."""
    if "steps" in op:
        return [dict(s) if isinstance(s, dict) else s for s in op["steps"]], {
            k: op[k] for k in ACT_CALL_FIELDS if k in op}
    step = {k: op[k] for k in ACT_STEP_FIELDS if k in op}
    return [step], {k: op[k] for k in ACT_CALL_FIELDS - STEP_FIELDS if k in op}


def _validate_act(op: dict, path: str, names: set) -> None:
    if "stamp" in op and "stamp_ref" in op:
        raise PlanError(path, "pass stamp or stamp_ref, not both")
    if "stamp" in op and not (isinstance(op["stamp"], str) and op["stamp"]):
        raise PlanError(path, "stamp must be a stamp name or json path")
    if "stamp_ref" in op:
        _check_ref(op["stamp_ref"], names, path)
    if "steps" in op:
        if not isinstance(op["steps"], list):
            raise PlanError(path, "steps must be a list of step objects")
        mixed = sorted(k for k in op if k in ACT_STEP_FIELDS - {"method", "hold_key"})
        if mixed:
            raise PlanError(path, f"steps cannot be combined with flat action field(s) {', '.join(mixed)}")
    steps, _ = _split_act(op)
    probe = []
    for i, s in enumerate(steps):
        if isinstance(s, dict) and "ocr_text" in s:
            if not isinstance(s["ocr_text"], str) or not s["ocr_text"].strip():
                raise PlanError(f"{path}.steps.{i}", "ocr_text must be a non-empty string")
            clash = sorted(k for k in ("ids", "norm", "xy", "uia_name") if s.get(k))
            if clash:
                raise PlanError(f"{path}.steps.{i}", f"ocr_text cannot be combined with {', '.join(clash)}")
            s = {k: v for k, v in s.items() if k != "ocr_text"} | {"ids": "1"}
        probe.append(s)
    _, err = validate_steps(probe)
    if err:
        raise PlanError(path, err.get("error") or "bad act steps", err.get("hint") or "")


def _validate_ops(ops, path: str, depth: int, names: set) -> None:
    if not isinstance(ops, list) or not ops:
        raise PlanError(path.rstrip("."), "expected a non-empty list of ops")
    if depth > MAX_DEPTH:
        raise PlanError(path.rstrip("."), f"if/repeat nest deeper than {MAX_DEPTH}")
    for i, op in enumerate(ops):
        p = f"{path}{i}"
        if not isinstance(op, dict) or op.get("op") not in OP_FIELDS:
            raise PlanError(p, "each op is an object with op = " + " | ".join(OP_FIELDS))
        kind = op["op"]
        _check_fields(op, OP_FIELDS[kind] | COMMON_FIELDS, p, f"{kind} op")
        if op.get("on_fail", "stop") not in ("stop", "continue"):
            raise PlanError(p, "on_fail is stop or continue")
        if kind == "perceive":
            if "from" in op:
                if any(k in op for k in ("hwnd", "title", "exe")):
                    raise PlanError(p, "from re-perceives that stamp's window; drop hwnd/title/exe")
                _check_ref(op["from"], names, p)
            elif not (op.get("hwnd") or op.get("title") or op.get("exe")):
                raise PlanError(p, "perceive needs hwnd, title, exe or from", "a plan always pins a window")
            if op.get("as") == LATEST or ("as" in op and not (isinstance(op["as"], str) and op["as"])):
                raise PlanError(p, f"as must be a non-empty name other than {LATEST!r}")
            if "roi" in op and "roi_norm" in op:
                raise PlanError(p, "pass roi or roi_norm, not both",
                                "roi is L,T,W,H in client pixels; roi_norm is CX,CY[,HALF] in 0-1000")
            for k, count in (("roi", 4), ("roi_norm", None)):
                if k in op and not _crop_parts(op[k], count):
                    raise PlanError(p, f"{k} must be {_CROP_SHAPE[k]}",
                                    "a region lets a plan read the page body and ignore the browser chrome")
            for k in PERCEIVE_FLAGS:
                if k in op and not isinstance(op[k], bool):
                    raise PlanError(p, f"{k} must be true or false")
        elif kind == "act":
            _validate_act(op, p, names)
        elif kind == "wait":
            if not _is_num(op.get("sec")) or not 0 < op["sec"] <= MAX_WAIT_S:
                raise PlanError(p, f"wait needs sec in (0, {MAX_WAIT_S:g}]", "use wait_for to wait on a condition")
        elif kind == "wait_for":
            _validate_cond(op.get("cond"), f"{p}.cond", names)
            t = op.get("timeout_ms", DEFAULT_WAIT_FOR_MS)
            if not _is_num(t) or not 0 < t <= MAX_WAIT_FOR_MS:
                raise PlanError(p, f"timeout_ms must be in (0, {MAX_WAIT_FOR_MS}]")
            iv = op.get("interval_ms", DEFAULT_INTERVAL_MS)
            if not _is_num(iv) or not MIN_INTERVAL_MS <= iv <= MAX_INTERVAL_MS:
                raise PlanError(p, f"interval_ms must be in {MIN_INTERVAL_MS}..{MAX_INTERVAL_MS}",
                                "a longer check interval would hide an operator stop")
        elif kind == "wait_until_stable":
            if "stamp" in op and "stamp_ref" in op:
                raise PlanError(p, "pass stamp or stamp_ref, not both")
            if "stamp_ref" in op:
                _check_ref(op["stamp_ref"], names, p)
            st = op.get("stable_ms", DEFAULT_STABLE_MS)
            if not _is_num(st) or not 0 < st <= MAX_STABLE_MS:
                raise PlanError(p, f"stable_ms must be in (0, {MAX_STABLE_MS}]")
            t = op.get("timeout_ms", DEFAULT_STABLE_TIMEOUT_MS)
            if not _is_num(t) or not 0 < t <= MAX_STABLE_TIMEOUT_MS:
                raise PlanError(p, f"timeout_ms must be in (0, {MAX_STABLE_TIMEOUT_MS}]")
            iv = op.get("interval_ms", DEFAULT_INTERVAL_MS)
            if not _is_num(iv) or not MIN_INTERVAL_MS <= iv <= MAX_INTERVAL_MS:
                raise PlanError(p, f"interval_ms must be in {MIN_INTERVAL_MS}..{MAX_INTERVAL_MS}")
            tol = op.get("tolerance", DEFAULT_STABLE_TOLERANCE)
            if not _is_num(tol) or not 0 <= tol <= 32:
                raise PlanError(p, "tolerance is the mean grey difference still counted as the same screen (0..32)")
        elif kind == "assert":
            _validate_cond(op.get("cond"), f"{p}.cond", names)
        elif kind == "if":
            _validate_cond(op.get("cond"), f"{p}.cond", names)
            if not (op.get("then") or op.get("else")):
                raise PlanError(p, "if needs then and/or else")
            for branch in ("then", "else"):
                if op.get(branch) is not None:
                    _validate_ops(op[branch], f"{p}.{branch}.", depth + 1, names)
        elif kind == "repeat":
            n = op.get("times")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_REPEAT:
                raise PlanError(p, f"repeat needs times in 1..{MAX_REPEAT}")
            if "until" in op:
                _validate_cond(op["until"], f"{p}.until", names)
            _validate_ops(op.get("steps"), f"{p}.steps.", depth + 1, names)
        elif kind == "launch":
            if len([k for k in ("app", "exe") if isinstance(op.get(k), str) and op[k]]) != 1:
                raise PlanError(p, "launch needs exactly one of app, exe")
            if "args" in op:
                from .launch import normalize_args

                if not isinstance(op["args"], list):
                    raise PlanError(p, "args must be a list of strings")
                try:
                    normalize_args(op["args"])
                except ValueError as e:
                    raise PlanError(p, str(e)) from e
            if "user_data_dir" in op and not (isinstance(op["user_data_dir"], str) and op["user_data_dir"].strip()):
                raise PlanError(p, "user_data_dir must be a non-empty string")
        elif kind == "stop":
            if "ok" in op and not isinstance(op["ok"], bool):
                raise PlanError(p, "ok must be true or false")


def _collect_names(ops, names: set) -> None:
    for op in ops if isinstance(ops, list) else []:
        if not isinstance(op, dict):
            continue
        if op.get("op") == "perceive" and isinstance(op.get("as"), str):
            names.add(op["as"])
        for k in ("then", "else", "steps"):
            if op.get("op") in ("if", "repeat"):
                _collect_names(op.get(k), names)


def validate_plan(src: Any) -> tuple[dict | None, dict | None]:
    """(plan, None) or (None, bad_plan payload). Nothing is perceived or sent."""
    try:
        plan = load_plan(src)
        if isinstance(plan, list):
            plan = {"steps": plan}
        if not isinstance(plan, dict):
            raise PlanError("", "a plan is an object with steps=[...] (or a bare list of ops)")
        if "go" in plan:
            raise PlanError("", "go is not a plan field",
                            "pass go on the call (CLI --go / MCP go=true): a plan cannot authorize live input")
        _check_fields(plan, PLAN_FIELDS, "", "plan")
        if plan.get("version", PLAN_VERSION) != PLAN_VERSION:
            raise PlanError("", f"unsupported version {plan.get('version')!r}; this build runs version {PLAN_VERSION}")
        stamps = plan.get("stamps") or {}
        if not isinstance(stamps, dict) or not all(
                isinstance(k, str) and k and k != LATEST and isinstance(v, str) and v for k, v in stamps.items()):
            raise PlanError("stamps", "stamps maps a name to a stamp name or json path")
        t = plan.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        if not _is_num(t) or not 0 < t <= MAX_TIMEOUT_MS:
            raise PlanError("timeout_ms", f"timeout_ms must be in (0, {MAX_TIMEOUT_MS}]")
        if "stop_on_challenge" in plan and not isinstance(plan["stop_on_challenge"], bool):
            raise PlanError("stop_on_challenge", "stop_on_challenge must be true or false",
                            "true (default): a human-verification page stops the plan instead of taking input")
        names = set(stamps)
        _collect_names(plan.get("steps"), names)
        _validate_ops(plan.get("steps"), "", 1, names)
        return plan, None
    except PlanError as e:
        return None, e.payload()


# ----------------------------------------------------------------- execution

class Deps:
    """The plan's hands and eyes. Tests replace this with a fake."""

    sleep = staticmethod(time.sleep)
    clock = staticmethod(time.monotonic)

    def perceive(self, **kw) -> dict:
        from .core import perceive

        return perceive(**kw)

    def read_frame(self, stamp: str) -> dict:
        from .act import load_frame

        return load_frame(stamp=stamp)[0]

    def fingerprint(self, json_path: str | None) -> bytes | None:
        """Thumbnail of the capture (128x128 grey). Pixels alone are too sensitive: a blinking caret
        or a 1-px animation would make every window look busy forever."""
        if not json_path:
            return None
        png = Path(str(json_path)).with_suffix(".png")
        try:
            from PIL import Image

            with Image.open(png) as im:
                return im.convert("L").resize((128, 128), Image.BILINEAR).tobytes()
        except (OSError, ValueError):
            return None

    def images_match(self, a: bytes | None, b: bytes | None, tolerance: float = 1.5) -> bool:
        """Same screen? Mean grey difference per thumbnail pixel at or below ``tolerance`` (0-255)."""
        if not a or not b or len(a) != len(b):
            return False
        return sum(abs(x - y) for x, y in zip(a, b)) / len(a) <= tolerance

    def act(self, steps: list[dict], call: dict) -> dict:
        from .act import annotate_verification, load_frame
        from .chain import act_chain

        call = dict(call)
        check = call.pop("check_change", False)
        min_ratio = call.pop("change_min_ratio", None)
        if not (check and call.get("go")):
            return annotate_verification(act_chain(steps, call))
        from .change import focus_baseline_hook, run_with_change_check

        try:
            target = int(load_frame(stamp=call.get("stamp"))[0].get("hwnd") or 0) or None
        except Exception:
            target = None  # act_chain reports the stamp problem
        return annotate_verification(run_with_change_check(
            target, lambda: act_chain(steps, call), min_ratio=min_ratio,
            before_baseline=focus_baseline_hook(target, bool(call.get("focus")))))

    def find_windows(self, title: str = "", exe: str = "") -> list[dict]:
        from .windows import find_windows

        return [w for w in find_windows(title=title, exe=exe) if w]

    def foreground(self) -> dict | None:
        from .windows import foreground_brief

        return foreground_brief()

    def launch(self, app: str | None = None, exe: str | None = None, args=None,
               user_data_dir: str | None = None) -> dict:
        from .launch import launch

        return launch(app=app, exe=exe, args=args, user_data_dir=user_data_dir)

    def launch_plan(self, app: str | None = None, exe: str | None = None, args=None,
                    user_data_dir: str | None = None) -> dict:
        from .launch import resolve

        return resolve(app=app, exe=exe, args=args, user_data_dir=user_data_dir)

    def abort_state(self) -> dict:
        from .act import abort_state

        return abort_state()


def _cf(v) -> str:
    return str(v).strip().casefold()


def _digest(fp) -> str:
    raw = fp if isinstance(fp, (bytes, bytearray)) else str(fp).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _challenge_hits(frame: dict) -> list[dict]:
    """Every place the frame's text looks like a human-verification / interstitial page."""
    hits: list[dict] = []
    seen: set[tuple] = set()

    def look(source: str, ident, text) -> None:
        if not text:
            return
        low = _cf(text)
        for token in CHALLENGE_TOKENS:
            if token in low and (source, ident, token) not in seen:
                seen.add((source, ident, token))
                hits.append({"source": source, "id": ident, "token": token, "text": str(text)[:80]})
                return

    for it in frame.get("items") or []:
        look("ocr", it.get("id"), it.get("text"))
    for node in frame.get("uia_nodes") or []:
        for key in ("name", "value"):
            look("uia", node.get("id"), node.get(key))
    return hits


def _matches(hay, needle: str, mode: str) -> bool:
    if hay is None:
        return False
    return _cf(hay) == _cf(needle) if mode == "exact" else _cf(needle) in _cf(hay)


def _dig(obj, dotted: str):
    for part in dotted.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def _center(box):
    return [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2] if isinstance(box, (list, tuple)) and len(box) == 4 else None


class _Run:
    def __init__(self, plan: dict, send: bool, deps: Deps):
        self.plan, self.send, self.deps = plan, send, deps
        self.frames: dict[str, dict] = {n: {"json": s, "data": None} for n, s in (plan.get("stamps") or {}).items()}
        self.results: list[dict] = []
        self.last: dict = {}
        self.ops_run = 0
        self.unsent = 0  # acts / launches planned in a dry run: the screen a live run would see differs
        self.steps_sent = 0
        self.stop_info: dict | None = None
        self.refreshed: set[str] = set()
        self.unsure_branches = 0  # depth of if-branches a dry run plans without knowing which one runs live
        self.unsure_acts = 0  # of the planned acts above, the ones only one branch of those futures sends
        self.t0 = deps.clock()
        self.deadline = self.t0 + float(plan.get("timeout_ms", DEFAULT_TIMEOUT_MS)) / 1000.0

    # -- control
    def stop(self, path: str, reason: str, error: str | None = None, ok: bool = False) -> None:
        if self.stop_info is None:
            self.stop_info = {"stopped_at": path, "stop_reason": reason, "ok": ok, "error": error}

    def abort_hit(self) -> tuple[str, str] | None:
        """The operator switch. A dry run reads it too: a stop holds for every op, planned or sent."""
        if self.deps.abort_state().get("abort_active"):
            return "aborted", "aborted by operator"
        return None

    def interrupt(self) -> tuple[str, str] | None:
        """Operator abort or the plan deadline: the two things that may cut a running op short."""
        if (hit := self.abort_hit()):
            return hit
        if self.deps.clock() >= self.deadline:
            return "timeout", "plan timeout_ms ran out; the rest was not run"
        return None

    def nap(self, sec: float) -> tuple[str, str] | None:
        """Sleep up to ``sec`` seconds, clipped by the plan deadline, reading the abort switch at least
        every INTERRUPT_POLL_S. Returns the interrupt that cut the sleep short, or None."""
        end = min(self.deps.clock() + max(0.0, sec), self.deadline)
        while (left := end - self.deps.clock()) > 0:
            self.deps.sleep(min(INTERRUPT_POLL_S, left))
            if (hit := self.interrupt()):
                return hit
        return None

    def gate(self) -> tuple[str, str] | None:
        """Whether the next op may start. The op budget belongs here only: an op that already
        started (the 64th being a wait) is not cut short by it."""
        if (hit := self.interrupt()):
            return hit
        if self.ops_run >= MAX_OPS:
            return "max_ops", f"the plan ran {MAX_OPS} ops (cap); the rest was not run"
        return None

    @property
    def speculative(self) -> bool:
        return not self.send and self.unsent > 0

    # -- frames
    def entry(self, name: str | None) -> dict:
        got = self.frames.get(name or LATEST)
        if got is None:
            raise OpError("no_stamp", f"no stamp for {name or LATEST!r} yet",
                          hint="perceive the window earlier in the plan, or pass stamp / plan.stamps")
        return got

    def data(self, entry: dict) -> dict:
        if entry.get("data") is None:
            try:
                entry["data"] = self.deps.read_frame(entry["json"])
            except (OSError, ValueError, KeyError) as e:
                raise OpError("no_stamp", f"cannot read stamp {entry['json']!r}: {e}") from e
        return entry["data"]

    def perceive(self, kw: dict, name: str | None) -> dict:
        try:
            out = self.deps.perceive(**kw)
        except Exception as e:  # no such window, missing extra, bad capture choice
            info = e.to_dict() if hasattr(e, "to_dict") else {"error": str(e)}
            return {**info, "ok": False, "reason": info.get("reason") or "perceive_failed"}
        if not out.get("stamp"):
            return {"ok": False, "reason": out.get("reason") or ("minimized" if out.get("minimized") else "perceive_failed"),
                    **{k: out[k] for k in ("error", "hint", "minimized", "suggested_hwnd") if out.get(k) is not None}}
        entry = {"json": out.get("json"), "data": out}
        self.frames[LATEST] = entry
        if name:
            self.frames[name] = entry
        brief = {"ok": out.get("ok", True) is not False, "stamp": out["stamp"], "json": out.get("json"),
                 "map": out.get("map") or out.get("grid"), "hwnd": out.get("hwnd"), "width": out.get("width"),
                 "height": out.get("height"), "count": len(out.get("items") or [])}
        if out.get("uia"):
            brief["uia_count"] = len(out.get("uia_nodes") or [])
        if out.get("roi"):
            brief["roi"] = out["roi"]  # the map and the OCR ids are local to this crop
        for k in ("error", "warnings"):
            if out.get(k):
                brief[k] = out[k]
        if name:
            brief["as"] = name
        if not brief["ok"]:
            brief["reason"] = out.get("reason") or "perceive_failed"
        return brief

    def refresh(self, name: str | None) -> dict:
        """Re-perceive the window of a stamp with the detectors (and crop) that stamp was taken with."""
        return self.refresh_entry(self.entry(name))

    def refresh_entry(self, entry: dict) -> dict:
        old = self.data(entry)
        if not old.get("hwnd"):
            raise OpError("cannot_refresh", "that stamp has no window (screen or image stamp); it cannot be re-perceived")
        kw = {"hwnd": int(old["hwnd"]), **{k: True for k in ("ocr", "uia", "yolo") if old.get(k)}}
        if old.get("roi"):
            kw["roi"] = list(old["roi"])  # a check on a cropped stamp keeps reading that region
        res = self.perceive(kw, None)
        if not res.get("stamp"):
            raise OpError(res.get("reason") or "perceive_failed", res.get("error") or "re-perceive failed")
        new = self.frames[LATEST]
        for alias, e in list(self.frames.items()):
            if e is entry:  # every name of the old stamp now means the new one
                self.frames[alias] = new
        return new

    # -- conditions
    def rows(self, cond: dict, fresh: bool, want_uia: bool) -> tuple[dict, dict]:
        name = cond.get("stamp_ref")
        key = name or LATEST
        if (fresh or cond.get("fresh")) and key not in self.refreshed:
            self.refresh(name)  # once per stamp per check, however many conditions read it
            self.refreshed.add(key)
        entry = self.entry(name)
        frame = self.data(entry)
        has = frame.get("uia") if want_uia else (frame.get("ocr") or frame.get("uia") or frame.get("yolo")
                                                  or frame.get("items") or frame.get("uia_nodes"))
        if not has:
            raise OpError("no_text_source", f"stamp {frame.get('stamp')} was taken without "
                          + ("uia=true" if want_uia else "ocr=true or uia=true"),
                          hint="perceive that window with uia=true (native controls) or ocr=true (drawn text)")
        return entry, frame

    def eval(self, cond: dict, fresh: bool = False) -> dict:
        """{"met": bool, ...evidence}. Raises OpError when it cannot be evaluated (never guessed)."""
        kind = next(k for k in cond if k in COND_FIELDS)
        body = cond[kind]
        if kind in ("all", "any"):
            parts = []
            for c in body:
                parts.append(self.eval(c, fresh))
                if parts[-1]["met"] != (kind == "all"):
                    break  # short-circuit, left to right
            return {"met": (all if kind == "all" else any)(p["met"] for p in parts), kind: parts}
        if kind == "not":
            inner = self.eval(body, fresh)
            return {"met": not inner["met"], "not": inner}
        if kind == "window":
            rows = self.deps.find_windows(title=body.get("title") or "", exe=body.get("exe") or "")
            if "minimized" in body:
                rows = [w for w in rows if bool(w.get("minimized")) == bool(body["minimized"])]
            out = {"met": bool(rows), "window": body, "count": len(rows)}
            if rows:
                out["first"] = {k: rows[0].get(k) for k in ("hwnd", "title", "exe")}
            return out
        if kind == "foreground":
            fg = self.deps.foreground()
            if not fg:
                raise OpError("foreground_unknown", "cannot read the foreground window")
            if body.get("title"):
                met = _matches(fg.get("title"), body["title"], "contains")
            else:
                want = body.get("hwnd") or self.data(self.entry(body["stamp_ref"])).get("hwnd")
                met = bool(want) and int(fg.get("foreground_hwnd") or 0) == int(want)
            return {"met": met, "foreground": {"hwnd": fg.get("foreground_hwnd"), "title": fg.get("title")}}
        if kind == "last":
            got = {k: _dig(self.last, k) for k in body}
            met = all((bool(got[k]) == v) if isinstance(v, bool) else got[k] == v for k, v in body.items())
            return {"met": met, "last": got, "last_op": self.last.get("path")}
        if kind == "challenge":
            _, frame = self.rows(cond, fresh, want_uia=False)
            hits = _challenge_hits(frame)
            return {"met": bool(hits) == bool(body), "challenge": body, "stamp": frame.get("stamp"),
                    "count": len(hits), "hits": hits[:5]}
        mode = cond.get("match", "contains")
        if kind == "text":
            source = cond.get("source", "any")
            _, frame = self.rows(cond, fresh, want_uia=source == "uia")
            hits = []
            if source in ("any", "ocr"):
                hits += [{"source": "ocr", "id": it.get("id"), "text": it.get("text")}
                         for it in frame.get("items") or [] if _matches(it.get("text"), body, mode)]
            if source in ("any", "uia"):
                hits += [{"source": "uia", "id": n.get("id"), "text": n.get(k)}
                         for n in frame.get("uia_nodes") or [] for k in ("name", "value")
                         if _matches(n.get(k), body, mode)]
            return {"met": bool(hits), "text": body, "stamp": frame.get("stamp"), "count": len(hits), "hits": hits[:5]}
        _, frame = self.rows(cond, fresh, want_uia=True)
        hits = [n for n in frame.get("uia_nodes") or []
                if all(_matches(n.get(k), body[k], mode) for k in ("name", "value") if body.get(k))
                and (not body.get("control_type") or _cf(n.get("control_type") or "") == _cf(body["control_type"]))]
        return {"met": bool(hits), "uia": body, "stamp": frame.get("stamp"), "count": len(hits),
                "hits": [{k: n.get(k) for k in ("id", "name", "control_type", "value") if n.get(k) is not None}
                         for n in hits[:5]]}

    def checked(self, cond: dict, fresh: bool = False) -> dict:
        self.refreshed = set()
        ev = self.eval(cond, fresh)
        if self.speculative:
            ev["speculative"] = True
        return ev

    # -- act targets
    def resolve(self, step: dict, entry: dict) -> dict:
        """ocr_text -> ids, from the stamp the act runs on. One match or it fails closed."""
        if "ocr_text" not in step:
            return step
        want = step["ocr_text"]
        items = [it for it in self.data(entry).get("items") or [] if it.get("text")]
        hits = [it for it in items if _matches(it["text"], want, "exact")] or [
            it for it in items if _matches(it["text"], want, "contains")]
        if len(hits) != 1:
            raise OpError(
                "target_not_found" if not hits else "ambiguous_target",
                f"ocr_text {want!r} matches {len(hits)} items on stamp {self.data(entry).get('stamp')}",
                hint=("perceive with ocr=true and check the text" if not hits else
                      "use a longer ocr_text, or ids / norm from the map"),
                candidates=[{"id": it.get("id"), "text": it.get("text"), "center_norm": _center(it.get("norm"))}
                            for it in hits[:8]])
        return {k: v for k, v in step.items() if k != "ocr_text"} | {"ids": str(hits[0]["id"])}

    # -- ops
    def run_ops(self, ops: list[dict], path: str) -> bool:
        return all(self.run_op(op, f"{path}{i}") for i, op in enumerate(ops))

    def run_op(self, op: dict, path: str) -> bool:
        kind = op.get("op")
        blocked = self.gate()
        if blocked:
            # The op never ran, but it gets a row anyway: results[] and stopped_at must name the same
            # op, or a consumer indexing results by stopped_at finds nothing.
            row = {"path": path, "op": kind, "ok": False, "ran": False, "gated": True,
                   "reason": blocked[0], "error": blocked[1]}
            if op.get("note"):
                row["note"] = op["note"]
            self.results.append(row)
            self.stop(path, *blocked)
            return False
        self.ops_run += 1
        res: dict = {"path": path, "op": kind}
        if op.get("note"):
            res["note"] = op["note"]
        self.results.append(res)
        try:
            getattr(self, f"op_{kind}")(op, path, res)
        except OpError as e:
            if self.speculative:  # the live run would have changed the screen first
                res.update(ok=None, speculative=True, reason=e.reason, would_fail_now=e.reason,
                           error=str(e), **e.extra)
            else:
                res.update(ok=False, reason=e.reason, error=str(e), **e.extra)
        if kind in LEAF_OPS:
            self.last = res
        if self.stop_info is not None:
            return False
        if res.get("ok") is False:
            if op.get("on_fail") == "continue":
                res["continued"] = True
                return True
            self.stop(path, res.get("reason") or f"{kind}_failed", res.get("error"))
            return False
        return True

    def op_perceive(self, op, path, res):
        kw = {k: op[k] for k in ("hwnd", "title", "exe", "capture", "roi", "roi_norm", *PERCEIVE_FLAGS)
              if k in op}
        name = op.get("as")
        if "from" in op:
            old = self.data(self.entry(op["from"]))
            if not old.get("hwnd"):
                raise OpError("cannot_refresh", "that stamp has no window; it cannot be re-perceived")
            kw = {"hwnd": int(old["hwnd"]), **{k: True for k in ("ocr", "uia", "yolo") if old.get(k)}, **kw}
            if old.get("roi") and "roi" not in kw and "roi_norm" not in kw:
                kw["roi"] = list(old["roi"])  # keep reading the same region unless the op overrides it
            name = name or (op["from"] if op["from"] != LATEST else None)
        if not self.send and kw.pop("focus", None):
            # focus=true raises a real window (SetForegroundWindow + restore). A dry run only reads, so
            # the focus is planned and reported, not done; the live run of the same plan still focuses.
            res["focus_planned"] = True
        res.update(self.perceive(kw, name))
        entry = self.frames.get(name or LATEST)
        if entry is not None and (hits := _challenge_hits(self.data(entry))):
            # A human-verification page takes whatever the plan sends it, so the default is to stop here.
            res["challenge"] = hits[:5]
            if self.plan.get("stop_on_challenge", True):
                if self.send:
                    raise OpError("challenge", "the window is showing a human-verification page",
                                  hint="a human has to pass it; the plan stopped instead of typing into it",
                                  hits=hits[:5], token=hits[0]["token"])
                res.setdefault("warnings", []).append(
                    "a human-verification page is on screen: a live run would stop here")

    def op_act(self, op, path, res):
        entry = {"json": op["stamp"], "data": None} if op.get("stamp") else self.entry(op.get("stamp_ref"))
        steps, call = _split_act(op)
        frame = self.data(entry)
        if frame.get("verify_light"):
            # A roi/roi_norm stamp is verify-only: its map is local to the crop, so a planned point
            # would mean something else. Refuse in a dry run too, or the preview promises an act the
            # live run refuses.
            raise OpError("verify_only_stamp",
                          "this stamp was taken with roi/roi_norm; a cropped stamp cannot be acted on",
                          hint="perceive the window without roi for act, and keep the cropped stamp for conditions")
        if self.plan.get("stop_on_challenge", True) and (hits := _challenge_hits(frame)):
            raise OpError("challenge", "refusing to act: the stamp shows a human-verification page",
                          hint="a human has to pass it; nothing was sent", hits=hits[:5], token=hits[0]["token"])
        if self.send and not frame.get("hwnd"):
            # act allows an explicit screen stamp; an unattended script does not get to click the bare desktop
            raise OpError("no_window_stamp", "a live plan acts only on window stamps (this one has no hwnd)",
                          hint="perceive the target window in the plan and act on that stamp")
        steps = [self.resolve(s, entry) for s in steps]
        out = self.deps.act(steps, {**call, "stamp": entry["json"], "go": self.send}) or {}
        res.update({k: out[k] for k in ACT_KEEP if k in out})
        res.setdefault("ok", bool(out.get("ok")))
        res["stamp"] = out.get("stamp") or entry["json"]
        done = out.get("completed_steps") or []
        typed = [d["typed_verified"] for d in done if d.get("typed_verified") is not None]
        if typed:
            res["typed_verified"] = all(typed)
        if res["ok"] is False and done:
            res["failed_step"] = done[-1]
        if self.send:
            self.steps_sent += int((out.get("evidence") or {}).get("steps_sent") or 0)
        else:
            self.unsent += 1
            self.unsure_acts += 1 if self.unsure_branches else 0

    def op_wait(self, op, path, res):
        sec = float(op["sec"])
        if not self.send:
            res.update(ok=True, would_wait_sec=sec)
            return
        t0 = self.deps.clock()
        blocked = self.nap(sec)
        waited = max(0.0, self.deps.clock() - t0)
        res.update(waited_sec=round(waited, 3), requested_sec=sec)
        if not blocked and waited + 0.001 < sec:
            # the deadline clipped the sleep without the switch reporting it (clock granularity)
            blocked = "timeout", "the plan deadline cut this wait short"
        if not blocked:
            # a stop can land in the last slice, after which no op may follow to notice it
            blocked = self.abort_hit()
        if blocked:
            # an operator stop or the plan deadline landed inside the wait: say so instead of
            # reporting the full requested sleep
            res.update(ok=False, reason=blocked[0], error=blocked[1],
                       interrupted="deadline" if blocked[0] == "timeout" else blocked[0],
                       waited_short_by_sec=round(sec - waited, 3))
            self.stop(path, *blocked)
        else:
            res["ok"] = True

    def op_wait_until_stable(self, op, path, res):
        """Sample the same window until its pixels hold still for stable_ms."""
        entry = {"json": op["stamp"], "data": None} if op.get("stamp") else self.entry(op.get("stamp_ref"))
        stable = float(op.get("stable_ms", DEFAULT_STABLE_MS)) / 1000.0
        timeout = float(op.get("timeout_ms", DEFAULT_STABLE_TIMEOUT_MS)) / 1000.0
        interval = float(op.get("interval_ms", DEFAULT_INTERVAL_MS)) / 1000.0
        tolerance = float(op.get("tolerance", DEFAULT_STABLE_TOLERANCE))
        if not self.send:
            res.update(ok=True, dry_run=True, would_wait_ms=round(timeout * 1000), stable=None)
            return
        t0 = self.deps.clock()
        prev: bytes | None = None
        held_from = t0
        samples = 0
        blocked = None
        while True:
            entry = self.refresh_entry(entry)
            samples += 1
            frame = self.data(entry)
            fp = self.deps.fingerprint(frame.get("json"))
            if fp is None:
                raise OpError("no_pixels", f"stamp {frame.get('stamp')} has no captured image to compare")
            now = self.deps.clock()
            if self.deps.images_match(prev, fp, tolerance):
                if now - held_from >= stable:
                    res.update(ok=True, stable=True, samples=samples, stamp=frame.get("stamp"),
                               settled_ms=round((now - t0) * 1000),
                               screenshot=_digest(fp))
                    return
            else:
                prev, held_from = fp, now
            if (blocked := self.interrupt()) or now - t0 >= timeout:
                break
            if (blocked := self.nap(interval)):
                break
        res.update(samples=samples, elapsed_ms=round((self.deps.clock() - t0) * 1000),
                   screenshot=_digest(prev) if prev else None)
        if blocked:
            res.update(ok=False, reason=blocked[0], error=blocked[1],
                       interrupted="deadline" if blocked[0] == "timeout" else blocked[0])
            self.stop(path, *blocked)
        else:
            res.update(ok=False, reason="never_settled",
                       error=f"the window kept changing for {round(timeout * 1000)} ms ({samples} samples)",
                       hint="a spinner never settles: wait longer, or read a value with wait_for instead")
            self.stop(path, "never_settled", res["error"])

    def op_wait_for(self, op, path, res):
        timeout = float(op.get("timeout_ms", DEFAULT_WAIT_FOR_MS)) / 1000.0
        interval = float(op.get("interval_ms", DEFAULT_INTERVAL_MS)) / 1000.0
        t0 = self.deps.clock()
        polls = 0
        blocked = None
        while True:
            ev = self.checked(op["cond"], fresh=True)
            polls += 1
            if ev["met"] or not self.send:
                break
            # The stop and the plan deadline outrank the op's own timeout_ms: checking them first keeps
            # an operator stop from being reported as a condition failure when both are true at once.
            if (blocked := self.interrupt()) or self.deps.clock() - t0 >= timeout:
                break
            # one interval, in interrupt-sized slices: a plan may set interval_ms but not blind the switch
            blocked = self.nap(min(interval, max(0.0, timeout - (self.deps.clock() - t0))))
            if blocked:
                break
        res.update(met=ev["met"], polls=polls, elapsed_ms=round((self.deps.clock() - t0) * 1000), cond=ev)
        if ev["met"]:
            res["ok"] = True
        elif not self.send:
            res.update(ok=None, would_wait_ms=round(timeout * 1000))
        elif blocked:
            # cut short by an operator stop or the plan deadline, not by its own timeout_ms
            res.update(ok=False, reason=blocked[0], error=blocked[1],
                       interrupted="deadline" if blocked[0] == "timeout" else blocked[0])
            self.stop(path, *blocked)
        else:
            res.update(ok=False, reason="wait_timeout",
                       error=f"condition not met within {round(timeout * 1000)} ms ({polls} polls)")

    def op_assert(self, op, path, res):
        ev = self.checked(op["cond"])
        res["cond"] = ev
        if ev["met"]:
            res["ok"] = True
        elif self.speculative:
            res.update(ok=None, speculative=True, would_fail_now="assert_failed")
        else:
            res.update(ok=False, reason="assert_failed", error=op.get("message") or "assert: condition not met")

    def op_if(self, op, path, res):
        ev = self.checked(op["cond"])
        branch = "then" if ev["met"] else "else"
        if not self.speculative:
            res.update(ok=True, cond=ev, branch=branch)
            if op.get(branch):
                self.run_ops(op[branch], f"{path}.{branch}.")
            return
        # A dry run past its first planned act reads a screen the live run would have changed, so
        # this condition does not say which branch runs live. Plan both instead of following one
        # (the wrong one would hide the other's acts, or end the dry run on a stop that never fires).
        res.update(ok=True, cond=ev, branch="both", branch_now=branch, speculative=True)
        stopped_in: list[str] = []
        for name in ("then", "else"):
            if not op.get(name) or self.stop_info is not None:
                continue
            self.unsure_branches += 1
            try:
                self.run_ops(op[name], f"{path}.{name}.")
            except _BranchEnd:
                # Each branch is one future of the live run: a stop ends that future only, so the rest of
                # this branch is unreachable while the other branch (and what follows it) still is.
                res["branch_stopped"] = stopped_in = [*stopped_in, name]
            finally:
                self.unsure_branches -= 1

    def op_repeat(self, op, path, res):
        until, times = op.get("until"), int(op["times"])
        met, n = False, 0
        res.update(ok=True, iterations=0)
        while n < times:
            if until is not None:
                ev = res["until"] = self.checked(until, fresh=True)
                if met := ev["met"]:
                    break
            try:
                done = self.run_ops(op["steps"], f"{path}.{n}.")
            except _BranchEnd:
                # a would-stop inside the body of a branch-planned repeat: the round did not finish
                res.update(ok=None, would_stop=True, iterations=n)
                raise
            if not done:
                return
            n += 1
            res["iterations"] = n
            if not self.send:
                res["would_repeat_up_to"] = times
                break
        if until is None:
            return
        if not met and n >= times:
            ev = res["until"] = self.checked(until, fresh=True)
            met = ev["met"]
        res["until_met"] = met
        if not met:
            if self.send:
                res.update(ok=False, reason="repeat_exhausted", error=f"until not met after {n} iteration(s)")
            else:
                res["ok"] = None

    def op_launch(self, op, path, res):
        kw = {"app": op.get("app"), "exe": op.get("exe")}
        if op.get("args"):
            kw["args"] = list(op["args"])
        if op.get("user_data_dir"):
            kw["user_data_dir"] = op["user_data_dir"]
        if not self.send:
            self.unsent += 1
            self.unsure_acts += 1 if self.unsure_branches else 0
            res.update(ok=True, would_launch=op.get("app") or op.get("exe"))
            planner = getattr(self.deps, "launch_plan", None)
            if planner is not None:
                try:
                    pl = planner(**kw)
                except KeyError as e:
                    raise OpError("launch_failed", str(e)) from e
                except ValueError as e:  # LaunchRefused is a ValueError: refuse in the dry run too
                    reason = _launch_refusal(e)
                    raise OpError(reason, str(e)) from e
                res.update(would_args=pl.get("args"),
                           **{k: pl[k] for k in ("browser", "user_data_dir", "profile_guard", "debug_port", "debug_address")
                              if pl.get(k) is not None and pl.get(k) != ""})
            return
        try:
            out = self.deps.launch(**kw)
        except (KeyError, ValueError, OSError) as e:
            reason = _launch_refusal(e)
            raise OpError(reason, str(e)) from e
        win = out.get("window") or {}
        res.update(ok=bool(out.get("ok", True)), exe=out.get("exe"),
                   window={k: win.get(k) for k in ("hwnd", "title", "exe", "pid")} if win else None,
                   **{k: out[k] for k in ("args", "pid", "new_window", "browser", "user_data_dir", "profile_guard",
                                          "debug_port", "debug_address", "warnings") if k in out})

    def op_stop(self, op, path, res):
        ok = bool(op.get("ok", True))
        if self.unsure_branches:
            res.update(ok=None, speculative=True, would_stop=True, stop_ok=ok, message=op.get("message"))
            raise _BranchEnd
        res.update(ok=ok, message=op.get("message"))
        self.stop(path, "stop_op", op.get("message"), ok=ok)

    # -- result
    def finish(self) -> dict:
        info = self.stop_info
        out: dict[str, Any] = {"ok": info["ok"] if info else True, "plan": True, "version": PLAN_VERSION,
                               "dry_run": not self.send}
        if self.plan.get("goal"):
            out["goal"] = self.plan["goal"]
        out.update(ops_run=self.ops_run, stopped=info is not None)
        if info:
            out.update(stopped_at=info["stopped_at"], stop_reason=info["stop_reason"])
            if info["error"]:
                out["error" if not info["ok"] else "message"] = info["error"]
        out["results"] = self.results
        out["frames"] = {n: {"stamp": (e.get("data") or {}).get("stamp"), "json": e["json"],
                             "hwnd": (e.get("data") or {}).get("hwnd")} for n, e in self.frames.items()}
        out["elapsed_ms"] = round((self.deps.clock() - self.t0) * 1000)
        out.update(self.deps.abort_state())
        aborted = bool(info and info["stop_reason"] == "aborted")
        if not out["ok"]:
            from .act import ABORT_HINT

            out["hint"] = ABORT_HINT if aborted else STOP_HINT
            if aborted:
                out.update(aborted=True, reason="aborted")
        if not self.send:
            out["verification"] = None
            certain = self.unsent - self.unsure_acts
            if certain:
                out["planned_acts"] = certain
            if self.unsure_acts:
                # acts planned inside an if whose branch the live run may not take: an upper bound
                out["planned_acts_at_most"] = self.unsent
            would = [{"path": r["path"], "op": r["op"], "reason": r.get("would_fail_now") or r.get("reason"),
                      "error": r.get("error")} for r in self.results
                     if r.get("ran", True) and r.get("ok") is None and (r.get("would_fail_now") or r.get("reason"))]
            if would:
                # Not a verdict on the live run: a target that only appears after an earlier act is
                # listed here just like a typo. A dry run cannot tell them apart; the reader can.
                out["would_fail"] = would
                out["would_fail_count"] = len(would)
                out["warnings"] = [
                    f"{len(would)} op(s) fail on the screen as it is now ("
                    + ", ".join(f"{w['path']} {w['reason']}" for w in would[:5])
                    + "); a dry run cannot tell whether the planned acts before them would change that"]
            wstops = [{"path": r["path"], "stop_ok": r.get("stop_ok"), "message": r.get("message")}
                      for r in self.results if r.get("would_stop")]
            if wstops:
                # A stop inside a branch one of the two futures takes would end the live run; the dry
                # run itself only ends that branch, so it has to say so here.
                out["would_stop"] = wstops
                out["would_stop_count"] = len(wstops)
                if any(w["stop_ok"] is False for w in wstops):
                    out.setdefault("warnings", []).append(
                        f"{len(wstops)} stop op(s) inside a planned branch; one would end the live run "
                        "with ok:false")
            seen = [r["path"] for r in self.results if r.get("challenge")]
            if seen:
                out["challenge"] = [{"path": r["path"], "hits": r.get("challenge")} for r in self.results
                                    if r.get("challenge")]
                out.setdefault("warnings", []).append(
                    f"a human-verification page is on screen at {', '.join(seen[:3])}: a live run would "
                    "stop there")
        elif aborted:
            out["verification"] = "aborted"
        elif not out["ok"]:
            out["verification"] = "partial" if self.steps_sent else "refused"
        elif any(r.get("verification") == "observed" for r in self.results):
            out["verification"] = "observed"
        else:
            out["verification"] = "sent" if self.steps_sent else "no_input"
        out["evidence"] = {"steps_sent": self.steps_sent}
        return out


def run_plan(src: Any, *, go: bool = False, deps: Deps | None = None) -> dict:
    """Validate, then run. ``go`` false (default) is a dry run: perceives and checks, sends nothing."""
    plan, err = validate_plan(src)
    if err:
        err["dry_run"] = not go
        return err
    run = _Run(plan, bool(go), deps or Deps())
    if not go:
        run.run_ops(plan["steps"], "")
        return run.finish()
    from .act import abort_epoch

    with abort_epoch():  # a stop that is on when the plan starts holds for the whole plan
        run.run_ops(plan["steps"], "")
        return run.finish()
