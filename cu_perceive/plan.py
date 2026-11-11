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

Bounds: MAX_OPS executed ops per call, MAX_DEPTH nesting, MAX_REPEAT iterations, a wall
clock ``timeout_ms`` and the operator abort switch. The op budget is checked
between ops, so the op that reaches the cap still runs to its end. Validation also
counts ops statically (``static_ops``: at least / at most, if every op succeeds): a
plan that runs more than MAX_OPS ops even on its shortest path is refused before
anything runs, unless it says ``resumable: true``. A run that stops at the cap or at
its deadline returns a ``continuation`` (``resume_from`` = the op that did not run,
``stamps`` = every named frame plus ``latest``); passing those two fields back with
the same steps resumes there, so ``frames.latest`` and named stamps carry across calls. Abort and the
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

import difflib
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .chain import STEP_FIELDS, validate_steps

PLAN_VERSION = 1
MAX_OPS = 64  # per call; a longer plan resumes with the continuation of the previous call
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
MAX_CONFIRM_FRAMES = 3  # confirm_frames: how many consecutive frames must agree on an OCR match
CONFIRM_TOLERANCE_NORM = 20  # how far (0-1000) a confirmed ocr_text match may move between frames
WINDOW_MATCH = ("contains", "exact", "regex")
MAX_PATTERN_LEN = 256
MAX_REGEX_HAY = 512  # regex mode reads the first 512 characters of a title / class
# a repeated group whose body repeats too: the classic catastrophic-backtracking shape, refused at validation
_NESTED_QUANT = re.compile(r"\((?:[^()\\]|\\.)*(?:[*+]|\{\d*,\d*\})(?:[^()\\]|\\.)*\)(?:[*+]|\{\d*,\d*\})")
RESUME_REASONS = ("max_ops", "timeout")  # stops a continuation can pick up from

PLAN_FIELDS = frozenset({"version", "goal", "stamps", "timeout_ms", "stop_on_challenge", "steps", "resumable",
                         "from_dry_run", "speculative",
                         "resume_from"})
COMMON_FIELDS = frozenset({"op", "on_fail", "note"})
PERCEIVE_FLAGS = ("ocr", "uia", "yolo", "focus")
ACT_CALL_FIELDS = frozenset({"focus", "ime", "method", "hold_key", "max_age_sec", "strict_stamp",
                             "new_window_wait_ms", "check_change", "change_min_ratio"})
ACT_STEP_FIELDS = STEP_FIELDS | {"ocr_text", "confirm_frames"}
PLAN_STEP_FIELDS = frozenset({"ocr_text", "confirm_frames"})  # resolved by the plan, never passed to act
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
    "window": frozenset({"match"}),
    "foreground": frozenset(),
    "text": frozenset({"stamp_ref", "source", "match", "fresh", "confirm_frames"}),
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
CONTINUE_HINT = ("merge continuation (resume_from, stamps, resumable) into the same plan and call plan again; "
                 "it goes on at the op that did not run, with the same named stamps and latest frame. A last "
                 "condition starts empty there, and the ops already run are not repeated")
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


def _check_confirm(v, path: str) -> None:
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= MAX_CONFIRM_FRAMES:
        raise PlanError(path, f"confirm_frames must be an integer in 1..{MAX_CONFIRM_FRAMES}",
                        "2 = the match must hold on this frame and on one re-perceived right after it (1 = off)")


def _window_mode(cond: dict, path: str) -> str:
    body = cond.get("window") if isinstance(cond.get("window"), dict) else {}
    if "match" in body and "match" in cond and body["match"] != cond["match"]:
        raise PlanError(path, "window match given twice with different values")
    mode = body.get("match", cond.get("match", "contains"))
    if mode not in WINDOW_MATCH:
        raise PlanError(path, "window match is contains, exact or regex",
                        "contains (default) is a substring; exact is the whole title; regex is re.search, "
                        "case-insensitive")
    return mode


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
        if not isinstance(body, dict) or not (body.get("title") or body.get("exe") or body.get("class")):
            raise PlanError(path, "window needs title, exe and/or class")
        _check_fields(body, {"title", "exe", "class", "minimized", "match"}, path, "window")
        for k in ("title", "exe", "class"):
            if k in body and not isinstance(body[k], str):
                raise PlanError(path, f"window {k} must be a string")
        if "minimized" in body and not isinstance(body["minimized"], bool):
            raise PlanError(path, "window minimized must be true or false")
        if _window_mode(cond, path) == "regex":
            for k in ("title", "class"):
                if body.get(k):
                    if len(body[k]) > MAX_PATTERN_LEN:
                        raise PlanError(path, f"window {k} pattern is longer than {MAX_PATTERN_LEN}")
                    try:
                        re.compile(body[k], re.IGNORECASE)
                    except re.error as e:
                        raise PlanError(path, f"window {k} is not a valid regex: {e}") from e
                    if _NESTED_QUANT.search(body[k]):
                        raise PlanError(path, f"window {k} repeats a group that repeats inside ((a+)* and the like)",
                                        "a page sets the title, and such a pattern can backtrack for minutes; "
                                        "write it without the outer repeat")
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
        if "confirm_frames" in cond:
            _check_confirm(cond["confirm_frames"], path)
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


def _names_uia_id(q) -> bool:
    """Whether a uia_name (a string, a comma list or a list, as act parses it) names a node by its per-frame
    id: "#4", "uia:#4", "uia:4" or a bare "4" (uia.find_uia_target reads all of these as node ids)."""
    parts = q if isinstance(q, (list, tuple)) else str(q).split(",") if isinstance(q, (str, int)) else []
    for part in parts:
        t = str(part).strip()
        if t.lower().startswith("uia:"):
            t = t[4:].strip()
        if t.startswith("#") or _DIGITS.fullmatch(t):
            return True
    return False


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
        if isinstance(s, dict) and "confirm_frames" in s:
            if "ocr_text" not in s:
                raise PlanError(f"{path}.steps.{i}", "confirm_frames goes with ocr_text",
                                "it re-reads the window and checks the OCR match holds on the next frame too")
            _check_confirm(s["confirm_frames"], f"{path}.steps.{i}")
        if isinstance(s, dict) and "ocr_text" in s:
            if not isinstance(s["ocr_text"], str) or not s["ocr_text"].strip():
                raise PlanError(f"{path}.steps.{i}", "ocr_text must be a non-empty string")
            clash = sorted(k for k in ("ids", "norm", "xy", "uia_name") if s.get(k))
            if clash:
                raise PlanError(f"{path}.steps.{i}", f"ocr_text cannot be combined with {', '.join(clash)}")
            s = {k: v for k, v in s.items() if k not in PLAN_STEP_FIELDS} | {"ids": "1"}
        probe.append(s)
    if any(isinstance(s, dict) and _is_num(s.get("confirm_frames")) and s["confirm_frames"] > 1 for s in steps):
        for i, s in enumerate(steps):
            if not isinstance(s, dict):
                continue
            byid = (["ids"] if s.get("ids") not in (None, "", []) else []) + (
                ["uia_name"] if _names_uia_id(s.get("uia_name")) else [])
            if byid:
                raise PlanError(f"{path}.steps.{i}",
                                f"confirm_frames acts on a newer frame, where {byid[0]} numbers other items",
                                "target the other steps by ocr_text, norm, xy or a uia_name by name, "
                                "or confirm in a step of its own act")
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


# Static op count. (least, most, always_stops, may_stop) for one op or a sequence, counted the way
# the runner counts (an if / repeat is one op, plus what runs inside it). "least" assumes every op
# succeeds, every until is met at its first check and every if takes its shorter branch; a stop that
# may fire ends the count of "least", a stop that always fires ends "most" too.
def _count_seq(ops) -> tuple[int, int, bool, bool]:
    acc = (0, 0, False, False)
    for op in ops if isinstance(ops, list) else []:
        acc = _then(acc, _count_op(op))
        if acc[2]:
            break
    return acc


def _then(a, b):
    return (a[0] + (0 if a[3] else b[0]), a[1] + (0 if a[2] else b[1]), a[2] or b[2], a[3] or b[3])


def _count_op(op) -> tuple[int, int, bool, bool]:
    kind = op.get("op") if isinstance(op, dict) else None
    if kind == "stop":
        return 1, 1, True, True
    if kind == "if":
        t = _count_seq(op.get("then") or [])
        e = _count_seq(op.get("else") or [])
        return 1 + min(t[0], e[0]), 1 + max(t[1], e[1]), t[2] and e[2], t[3] or e[3]
    if kind == "repeat":
        return _count_rounds(op, _count_seq(op.get("steps")), int(op.get("times") or 0))
    return 1, 1, False, False


def _count_rounds(op, body, rounds: int, first=None) -> tuple[int, int, bool, bool]:
    """A repeat op (1), then ``first`` (the rest of a resumed round, if any), then up to ``rounds``
    rounds of ``body``. Without until, every round runs; with until, none has to."""
    least, most, always, may = 1, 1, False, False
    if first is not None:
        least, most, always, may = 1 + first[0], 1 + first[1], first[2], first[3]
        if always:
            return least, most, True, True
    if rounds <= 0:
        return least, most, always, may
    until = op.get("until") is not None
    if body[2]:  # the body always stops: at most one more round runs, and it ends the plan
        return (least + (0 if may or until else body[0]), most + body[1], not until, True)
    if not (may or until):
        least += body[0] if body[3] else rounds * body[0]
    return least, most + rounds * body[1], False, may or body[3]


def _resume_parts(plan: dict) -> list[str] | None:
    rf = plan.get("resume_from")
    return None if rf is None else str(rf).split(".")


def _count_from(ops, parts: list[str]) -> tuple[int, int, bool, bool]:
    """Static count of what a run resumed at ``parts`` executes."""
    i = int(parts[0])
    if len(parts) == 1:
        return _count_seq(ops[i:])
    op = ops[i]
    if op["op"] == "if":
        head = _count_from(op[parts[1]], parts[2:])
        head = (1 + head[0], 1 + head[1], head[2], head[3])
    else:
        r = int(parts[1])
        head = _count_rounds(op, _count_seq(op["steps"]), int(op["times"]) - r - 1,
                             first=_count_from(op["steps"], parts[2:]))
    return _then(head, _count_seq(ops[i + 1:]))


def static_ops(plan: dict) -> dict:
    """{"least", "most", "cap"}: ops this plan (or its resumed part) runs if every op succeeds."""
    parts = _resume_parts(plan)
    least, most, _, _ = _count_from(plan["steps"], parts) if parts else _count_seq(plan["steps"])
    return {"least": least, "most": most, "cap": MAX_OPS}


_DIGITS = re.compile(r"[0-9]+")  # str.isdigit() also takes digits int() refuses


def _validate_resume(plan: dict) -> None:
    rf = plan["resume_from"]
    if not isinstance(rf, str) or not rf:
        raise PlanError("resume_from", "resume_from is an op path such as \"12\" or \"3.then.0\"",
                        "copy it from the continuation of the previous run")
    parts = rf.split(".")
    ops, where = plan["steps"], ""
    while True:
        head = parts.pop(0)
        if not _DIGITS.fullmatch(head) or int(head) >= len(ops):
            raise PlanError("resume_from", f"resume_from {rf!r}: no op {where}{head}")
        op, where = ops[int(head)], f"{where}{head}."
        if not parts:
            return
        if op["op"] == "if" and parts[0] in ("then", "else") and op.get(parts[0]):
            ops, where = op[parts[0]], f"{where}{parts.pop(0)}."
        elif op["op"] == "repeat" and _DIGITS.fullmatch(parts[0]) and int(parts[0]) < int(op["times"]):
            ops, where = op["steps"], f"{where}{parts.pop(0)}."
        else:
            raise PlanError("resume_from", f"resume_from {rf!r}: {where.rstrip('.')} has no {parts[0]!r}",
                            "an if takes then / else, a repeat takes a round number below times")
        if not parts:
            raise PlanError("resume_from", f"resume_from {rf!r} names a branch, not an op")


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
                isinstance(k, str) and k and isinstance(v, str) and v for k, v in stamps.items()):
            raise PlanError("stamps", "stamps maps a name to a stamp name or json path",
                            f"{LATEST!r} seeds the latest frame (a continuation carries it)")
        if "resumable" in plan and not isinstance(plan["resumable"], bool):
            raise PlanError("resumable", "resumable must be true or false")
        for key in ("from_dry_run", "speculative"):
            if key in plan and not isinstance(plan[key], bool):
                raise PlanError(key, f"{key} must be true or false")
        if (plan.get("from_dry_run") or plan.get("speculative")) and "resume_from" not in plan:
            raise PlanError("from_dry_run", "from_dry_run / speculative only come with a continuation (resume_from)")
        if plan.get("speculative") and not plan.get("from_dry_run"):
            raise PlanError("speculative", "speculative comes from a dry run's continuation (from_dry_run: true)")
        t = plan.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        if not _is_num(t) or not 0 < t <= MAX_TIMEOUT_MS:
            raise PlanError("timeout_ms", f"timeout_ms must be in (0, {MAX_TIMEOUT_MS}]")
        if "stop_on_challenge" in plan and not isinstance(plan["stop_on_challenge"], bool):
            raise PlanError("stop_on_challenge", "stop_on_challenge must be true or false",
                            "true (default): a human-verification page stops the plan instead of taking input")
        names = set(stamps)
        _collect_names(plan.get("steps"), names)
        _validate_ops(plan.get("steps"), "", 1, names)
        if "resume_from" in plan:
            _validate_resume(plan)
        count = static_ops(plan)
        if count["least"] > MAX_OPS and not plan.get("resumable"):
            err = PlanError("steps", f"the plan runs at least {count['least']} ops; one call runs at most {MAX_OPS}",
                            "split it, or set resumable: true and call again with the continuation the run "
                            "returns at the cap").payload()
            err.update(over_cap=True, static_ops=count)
            return None, err
        return plan, None
    except PlanError as e:
        return None, e.payload()


def validate_report(src: Any) -> dict:
    """What validate_only / --validate return: bad_plan, or validated with the static op count."""
    plan, err = validate_plan(src)
    if err:
        return err
    count = static_ops(plan)
    out = {"ok": True, "plan": True, "validated": True, "static_ops": count}
    if count["most"] > MAX_OPS:
        out["warnings"] = [f"up to {count['most']} ops (cap {MAX_OPS} per call): a run that reaches the cap "
                           "stops with a continuation; call again with its resume_from and stamps"]
    if plan.get("resume_from"):
        out["resume_from"] = plan["resume_from"]
    return out


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

    def top_windows(self) -> list[dict]:
        """Every top-level window (title, class, exe, minimized); the window condition filters them."""
        from .windows import list_windows

        return [w for w in list_windows(include_minimized=True) if w]

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
    if mode == "regex":
        # a caller's pattern against page-made titles: patterns that nest repeats are refused at validation,
        # and only the first MAX_REGEX_HAY characters are read
        return re.search(needle, str(hay)[:MAX_REGEX_HAY], re.IGNORECASE) is not None
    return _cf(hay) == _cf(needle) if mode == "exact" else _cf(needle) in _cf(hay)


def _near(a, b) -> bool:
    ca, cb = _center(a.get("norm")), _center(b.get("norm"))
    if ca is None or cb is None:
        return ca == cb
    return max(abs(ca[0] - cb[0]), abs(ca[1] - cb[1])) <= CONFIRM_TOLERANCE_NORM


def _brief_item(it: dict) -> dict:
    return {"id": it.get("id"), "text": it.get("text"), "center_norm": _center(it.get("norm"))}


def _dig(obj, dotted: str):
    for part in dotted.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def _center(box):
    return [(box[0] + box[2]) // 2, (box[1] + box[3]) // 2] if isinstance(box, (list, tuple)) and len(box) == 4 else None


class _Run:
    def __init__(self, plan: dict, send: bool, deps: Deps):
        self.plan, self.send, self.deps = plan, send, deps
        self.frames: dict[str, dict] = {}
        stamps = plan.get("stamps") or {}
        latest = {"json": stamps[LATEST], "data": None} if LATEST in stamps else None
        for n, s in stamps.items():
            # a name that carried the latest frame (a continuation's "latest" and "main") stays that one
            # frame, so a refresh through either moves both, as before the cut; other names that share a
            # json stay apart (a caller's "baseline" and "main" are two frames)
            self.frames[n] = latest if latest is not None and s == latest["json"] else {"json": s, "data": None}
        self.resume: dict[str, list[str]] = {}  # container path -> where inside it a resumed run starts
        self.results: list[dict] = []
        self.last: dict = {}
        self.ops_run = 0
        # acts / launches planned in a dry run: the screen a live run would see differs. A dry run that
        # resumes a speculative dry run starts speculative too.
        self.unsent = 0
        self.resumed_speculative = bool(plan.get("speculative")) and not send
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
        return not self.send and (self.unsent > 0 or self.resumed_speculative)

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
            met = (all if kind == "all" else any)(p["met"] for p in parts)
            out = {"met": met, kind: parts}
            # an unstable part (a match that did not hold across frames) is unmet but not proven false; when
            # the result rests on it (any: nothing met; all: it is the part that failed) the result is unstable
            if not met and (any(p.get("unstable") for p in parts) if kind == "any" else parts[-1].get("unstable")):
                out["unstable"] = True
            return out
        if kind == "not":
            # "not" holds the inner evidence with inner_met = the inner condition and met = the negation,
            # so not.met always agrees with the outer met (it used to be the inner one).
            inner = self.eval(body, fresh)
            # an inner text check that did not hold across frames proves neither side: not is unmet too
            met = not inner["met"] and not inner.get("unstable")
            out = {"met": met, "not": {"inner_met": inner["met"], "met": met,
                                       **{k: v for k, v in inner.items() if k != "met"}}}
            if inner.get("unstable"):
                out["not"]["undetermined"] = True
                out["unstable"] = True  # undetermined: an outer not / any / all reads it the same way
            return out
        if kind == "window":
            mode = body.get("match", cond.get("match", "contains"))
            title, exe, cls = body.get("title") or "", body.get("exe") or "", body.get("class") or ""
            if mode == "contains" and not cls:
                rows = self.deps.find_windows(title=title, exe=exe)  # unchanged: substring, like windows(title=)
            else:
                rows = [w for w in self.deps.top_windows()
                        if (not title or _matches(w.get("title") or "", title, mode))
                        and (not cls or _matches(w.get("class") or "", cls, mode))
                        and (not exe or _cf(exe) in _cf(f"{w.get('exe') or ''} {w.get('exe_path') or ''}"))]
            if "minimized" in body:
                rows = [w for w in rows if bool(w.get("minimized")) == bool(body["minimized"])]
            out = {"met": bool(rows), "window": body, "match": mode, "count": len(rows)}
            if rows:
                out["first"] = {k: rows[0].get(k) for k in ("hwnd", "title", "exe", "class") if k in rows[0]}
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
            out = {"met": bool(hits), "text": body, "stamp": frame.get("stamp"), "count": len(hits), "hits": hits[:5]}
            need = int(cond.get("confirm_frames") or 1)
            if need > 1 and hits:
                # OCR item sets flicker between frames: a match only counts when the next frame(s) agree.
                seen = [{"stamp": frame.get("stamp"), "count": len(hits)}]
                for _ in range(need - 1):
                    self.refresh(cond.get("stamp_ref"))
                    again = self.eval({k: v for k, v in cond.items() if k not in ("confirm_frames", "fresh")})
                    seen.append({"stamp": again["stamp"], "count": again["count"]})
                    if not again["met"]:
                        out.update(met=False, unstable=True)
                        break
                out["confirm_frames"] = seen
            return out
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
    def find_text(self, want: str, entry: dict, frame_no: int = 1) -> dict:
        """The one OCR item ``want`` names on that stamp: exact (case-insensitive) beats substring.
        Zero matches is target_not_found (with the closest texts as ``similar``), more than one is
        ambiguous_target (with ``candidates``); both fail closed."""
        frame = self.data(entry)
        items = [it for it in frame.get("items") or [] if it.get("text")]
        by = "exact"
        hits = [it for it in items if _matches(it["text"], want, "exact")]
        if not hits:
            by, hits = "contains", [it for it in items if _matches(it["text"], want, "contains")]
        if len(hits) == 1:
            return hits[0]
        extra = {"stamp": frame.get("stamp"), "ocr_text": want, "match_count": len(hits), "matched_by": by,
                 "ocr_items": len(items),
                 "candidates": [_brief_item(it) for it in hits[:8]]}
        if frame_no > 1:
            extra["frame"] = frame_no
        if not hits:
            ranked = sorted(((difflib.SequenceMatcher(None, _cf(want), _cf(it["text"])).ratio(), it) for it in items),
                            key=lambda p: -p[0])
            extra["similar"] = [_brief_item(it) | {"score": round(r, 2)} for r, it in ranked[:5] if r >= 0.5]
            raise OpError("target_not_found",
                          f"ocr_text {want!r} matches 0 items on stamp {frame.get('stamp')}",
                          hint=("OCR may have read it differently: see similar[]; or perceive with ocr=true "
                                "and wait for the page to settle"), **extra)
        raise OpError("ambiguous_target",
                      f"ocr_text {want!r} matches {len(hits)} items on stamp {frame.get('stamp')}",
                      hint="use a longer ocr_text, or ids / norm from the map; candidates[] lists them", **extra)

    def resolve(self, step: dict, entry: dict, hit: dict | None = None) -> dict:
        """ocr_text -> ids, from the stamp the act runs on. One match or it fails closed."""
        if "ocr_text" not in step:
            return step
        hit = hit or self.find_text(step["ocr_text"], entry)
        return {k: v for k, v in step.items() if k not in PLAN_STEP_FIELDS} | {"ids": str(hit["id"])}

    def confirm_targets(self, steps: list[dict], entry: dict, res: dict) -> tuple[dict, dict]:
        """confirm_frames: resolve every ocr_text step, re-perceive, resolve again; each step's match must
        keep its text and stay within CONFIRM_TOLERANCE_NORM between consecutive frames. Returns the
        last frame's entry and {step index: item}. Re-perceiving only reads, so a dry run does it too."""
        need = max(int(s.get("confirm_frames") or 1) for s in steps if isinstance(s, dict))
        want = {i: s["ocr_text"] for i, s in enumerate(steps) if isinstance(s, dict) and "ocr_text" in s}
        prev = {i: self.find_text(t, entry) for i, t in want.items()}
        seen = [{"stamp": self.data(entry).get("stamp"), "ids": {str(i): it.get("id") for i, it in prev.items()}}]
        for n in range(2, need + 1):
            entry = self.refresh_entry(entry)
            cur = {i: self.find_text(t, entry, frame_no=n) for i, t in want.items()}
            seen.append({"stamp": self.data(entry).get("stamp"), "ids": {str(i): it.get("id") for i, it in cur.items()}})
            moved = [i for i in want if _cf(cur[i]["text"]) != _cf(prev[i]["text"]) or not _near(cur[i], prev[i])]
            if moved:
                i = moved[0]
                raise OpError("unstable_target",
                              f"ocr_text {want[i]!r} did not hold between frames {n - 1} and {n}",
                              hint="the page is still changing: wait_until_stable first, or target by norm; the act's "
                                   "highest confirm_frames applies to every ocr_text step of it",
                              step=i, frames=[{"stamp": seen[-2]["stamp"], **_brief_item(prev[i])},
                                              {"stamp": seen[-1]["stamp"], **_brief_item(cur[i])}])
            prev = cur
        res["confirm_frames"] = seen
        return entry, prev

    # -- ops
    def run_ops(self, ops: list[dict], path: str) -> bool:
        return all(self.run_op(op, f"{path}{i}") for i, op in enumerate(ops))

    def run_from(self, ops: list[dict], path: str, parts: list[str]) -> bool:
        """Run ``ops`` from the op ``parts`` names (a continuation's resume_from, split on dots): the
        containers on the way get a row with ``resumed`` and run only what is left inside them."""
        i = int(parts[0])
        if len(parts) > 1:
            self.resume[f"{path}{i}"] = parts[1:]
        return all(self.run_op(ops[j], f"{path}{j}") for j in range(i, len(ops)))

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
        hits: dict = {}
        if any(isinstance(s, dict) and int(s.get("confirm_frames") or 1) > 1 for s in steps):
            entry, hits = self.confirm_targets(steps, entry, res)  # the act runs on the frame that confirmed
            frame = self.data(entry)
            if self.plan.get("stop_on_challenge", True) and (ch := _challenge_hits(frame)):
                raise OpError("challenge", "refusing to act: the confirming frame shows a human-verification page",
                              hint="a human has to pass it; nothing was sent", hits=ch[:5], token=ch[0]["token"])
        steps = [self.resolve(s, entry, hits.get(i)) for i, s in enumerate(steps)]
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
        rest = self.resume.pop(path, None)
        if rest:
            # resumed inside this if: the branch was chosen in the previous call; do not re-evaluate
            res.update(ok=True, branch=rest[0], resumed=True)
            self.run_from(op[rest[0]], f"{path}.{rest[0]}.", rest[1:])
            return
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
        rest = self.resume.pop(path, None)
        one_round = False
        if rest:
            # resumed inside round n: finish that round, then the rounds left (until is checked as usual)
            n = int(rest[0])
            res.update(resumed=True, resumed_round=n, iterations=n)
            try:
                done = self.run_from(op["steps"], f"{path}.{n}.", rest[1:])
            except _BranchEnd:
                res.update(ok=None, would_stop=True, iterations=n)
                raise
            if not done:
                return
            n += 1
            res["iterations"] = n
            if not self.send:
                res["would_repeat_up_to"] = times
                one_round = True  # a dry run plans one round
        while n < times and not one_round:
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
        if self.plan.get("resume_from"):
            out["resumed_from"] = self.plan["resume_from"]
        if info and info["stop_reason"] in RESUME_REASONS:
            # Everything a next call needs to go on from the op that did not run (or was cut short):
            # the same steps plus these two fields. Stamps are json paths, so they outlive this call.
            # It holds plan fields only, so a caller merges it into the plan as it is.
            out["continuation"] = {
                "resume_from": info["stopped_at"],
                "stamps": {n: e["json"] for n, e in self.frames.items() if e.get("json")},
                "resumable": True,
            }
            if not self.send:
                # a dry run's continuation resumes a dry run only (its earlier acts were never sent)
                out["continuation"]["from_dry_run"] = True
                if self.unsent or self.resumed_speculative:
                    out["continuation"]["speculative"] = True
        out["elapsed_ms"] = round((self.deps.clock() - self.t0) * 1000)
        out.update(self.deps.abort_state())
        aborted = bool(info and info["stop_reason"] == "aborted")
        if not out["ok"]:
            from .act import ABORT_HINT

            out["hint"] = ABORT_HINT if aborted else STOP_HINT
            if info and info["stop_reason"] in RESUME_REASONS:
                out["hint"] = (CONTINUE_HINT + ("; this was a dry run: its continuation (from_dry_run) resumes "
                                                "a dry run only and is refused with go" if not self.send else ""))
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
    if not err and go and plan.get("from_dry_run"):
        err = PlanError("from_dry_run", "this continuation came from a dry run: its earlier acts were never sent",
                        "run the whole plan live from the start (without resume_from), or resume it as a dry "
                        "run").payload()
    if err:
        err["dry_run"] = not go
        return err
    run = _Run(plan, bool(go), deps or Deps())
    parts = _resume_parts(plan)

    def body() -> None:
        if parts:
            run.run_from(plan["steps"], "", parts)
        else:
            run.run_ops(plan["steps"], "")

    if not go:
        body()
        return run.finish()
    from .act import abort_epoch

    with abort_epoch():  # a stop that is on when the plan starts holds for the whole plan
        body()
        return run.finish()
