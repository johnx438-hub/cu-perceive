"""Verb allowlist and the argv a backend will run.

Read verbs pass through. Act verbs build the same argv; the tool layer runs it
unless ``dry_run=true`` (or ``CU_PERCEIVE_BSK_REQUIRE_GO`` is set and ``go`` is
not). The argv is what ``bsk`` would be given, minus ``--json`` (the backend
appends that). It is also what a future in-process backend accepts.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


# browser_act verbs. The tool layer runs these unless dry_run=true.
ACT_VERBS = (
    "click",
    "hover",
    "wheel",
    "scroll_to",
    "focus",
    "blur",
    "fill",
    "select",
    "press",
    "navigate",
    "navigate_back",
    "navigate_forward",
    "reload",
)
ACT_ALIASES = {
    "type": "fill",
    "scroll": "wheel",
    "key": "press",
    "back": "navigate_back",
    "forward": "navigate_forward",
}

# browser_observe kinds. Both run immediately.
OBSERVE_KINDS = ("observe", "snapshot")

# action -> "read" (always runs) or "act" (runs unless dry_run=true).
SESSION_ACTIONS = {"list": "read", "start": "act", "stop": "act"}
TAB_ACTIONS = {
    "list": "read",
    "create": "act",
    "close": "act",
    "select": "act",
    "borrow": "act",
    "return": "act",
}

# Successful runs of these drop the session's observe, because the page they
# addressed is no longer the one those @eN refs named.
INVALIDATES = frozenset({
    "navigate",
    "navigate_back",
    "navigate_forward",
    "reload",
    "tab_create",
    "tab_close",
    "tab_select",
    "tab_borrow",
    "tab_return",
    "session_stop",
})

_REF = re.compile(r"^@?e\d+$")
_BUTTONS = frozenset({"left", "middle", "right"})
_MODIFIERS = frozenset({"alt", "ctrl", "meta", "shift"})
_WAIT = frozenset({"load", "domcontentloaded", "networkidle", "commit"})
_SCOPES = frozenset({"user", "agent", "all"})

_BORROW_WARNING = (
    "tab borrow may ask the user to confirm in the browser and can wait for that answer"
)


class PlanError(Exception):
    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__(payload.get("error") or "bad browser call")


def plan_error(reason: str, error: str, hint: str, **extra) -> PlanError:
    body = {"ok": False, "reason": reason, "error": error, "hint": hint, "dry_run": True}
    body.update(extra)
    return PlanError(body)


@dataclass
class Spec:
    verb: str = ""
    session: str = ""
    ref: str = ""
    selector: str = ""
    url: str = ""
    value: str = ""
    key: str = ""
    button: str = ""
    click_count: int = 0
    modifiers: str = ""
    delta_x: float = 0.0
    delta_y: float = 0.0
    tab_id: int = 0
    timeout_ms: int = 0
    wait_until: str = ""
    clear: bool = True
    hard: bool = False
    observation_id: str = ""
    browser: str = ""
    focus: bool = False
    cursor: str = ""
    max_tokens: int = 0
    max_depth: int = 0
    probe_hover: bool = False
    scope: str = ""
    active: bool = True


@dataclass
class Plan:
    verb: str
    args: list[str]
    session: str
    ref: str | None
    selector: str | None
    read: bool
    records: bool
    invalidates: bool
    observation_id: str = ""
    timeout_ms: int = 0
    alias_from: str | None = None
    warning: str | None = None
    notes: list[str] = field(default_factory=list)


def normalize_ref(ref: str) -> str | None:
    text = (ref or "").strip()
    if not _REF.fullmatch(text):
        return None
    return text if text.startswith("@") else f"@{text}"


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise plan_error("bad_args", f"{name} must be a finite number", f"pass {name} as a number")
    number = float(value)
    if not math.isfinite(number):
        raise plan_error("bad_args", f"{name} must be a finite number", f"pass {name} as a number")
    return number


def _pos_int(value, name: str) -> int | None:
    """0 / empty means omitted. Negatives and non-integers are bad_args."""
    if isinstance(value, bool) or value in (None, "", 0):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            raise plan_error("bad_args", f"{name} must be a positive integer", f"pass {name} as an integer")
        value = int(value)
    if not isinstance(value, int):
        raise plan_error("bad_args", f"{name} must be a positive integer", f"pass {name} as an integer")
    if value < 0:
        raise plan_error("bad_args", f"{name} must be a positive integer", f"pass {name} greater than 0, or omit it")
    if value == 0:
        return None
    return value


def _no_injection(value: str, name: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise plan_error("bad_args", f"{name} must be a single argument", f"remove newlines from {name}")
    return value


def _optional_used(spec: Spec) -> set[str]:
    used = set()
    if _text(spec.ref):
        used.add("ref")
    if _text(spec.selector):
        used.add("selector")
    if _text(spec.url):
        used.add("url")
    if _text(spec.value):
        used.add("value")
    if _text(spec.key):
        used.add("key")
    if _text(spec.button):
        used.add("button")
    if spec.click_count:
        used.add("click_count")
    if _text(spec.modifiers):
        used.add("modifiers")
    if spec.delta_x:
        used.add("delta_x")
    if spec.delta_y:
        used.add("delta_y")
    if spec.tab_id:
        used.add("tab_id")
    if spec.timeout_ms:
        used.add("timeout_ms")
    if _text(spec.wait_until):
        used.add("wait_until")
    if spec.clear is False:
        used.add("clear")
    if spec.hard:
        used.add("hard")
    if _text(spec.browser):
        used.add("browser")
    if spec.focus:
        used.add("focus")
    if _text(spec.cursor):
        used.add("cursor")
    if spec.max_tokens:
        used.add("max_tokens")
    if spec.max_depth:
        used.add("max_depth")
    if spec.probe_hover:
        used.add("probe_hover")
    if _text(spec.scope):
        used.add("scope")
    if spec.active is False:
        used.add("active")
    return used


def _check_fields(verb: str, spec: Spec, allow: set[str], *, session: str) -> str:
    extra = sorted(_optional_used(spec) - allow)
    if extra:
        shown = ", ".join(extra)
        valid = ", ".join(sorted(allow)) or "(none)"
        raise plan_error(
            "bad_args",
            f"{verb} does not use {shown}; nothing was run",
            f"valid fields for {verb}: {valid}",
            unused_args=extra,
        )
    sid = _text(spec.session)
    if session == "required" and not sid:
        raise plan_error(
            "bad_args",
            f"{verb} needs a session id",
            "browser_session action=start, then pass that session id",
        )
    if session == "forbid" and sid:
        raise plan_error(
            "bad_args",
            f"{verb} does not take a session id; nothing was run",
            "omit session for this call",
        )
    if sid:
        _no_injection(sid, "session")
        if sid.startswith("-"):
            raise plan_error("bad_args", "session must not look like a flag", "pass the session id from browser_session")
    return sid


def _reject_unknown(kind: str, value: str, valid: list[str], *, empty_hint: str) -> None:
    if not value.strip():
        raise plan_error("bad_args", f"{kind} is required", empty_hint, **{f"valid_{kind}s": valid})
    key = "valid_verbs" if kind == "verb" else "valid_actions"
    reason = "unknown_verb" if kind == "verb" else "unknown_action"
    raise plan_error(
        reason,
        f"unknown {kind} {value!r}; nothing was run",
        f"use one of: {', '.join(valid)}",
        **{key: valid},
    )


def _target(spec: Spec, *, required: bool) -> tuple[str | None, str | None]:
    """(normalized ref or None, selector or None)."""
    ref = _text(spec.ref)
    selector = _text(spec.selector)
    if ref and selector:
        raise plan_error(
            "bad_args",
            "pass ref or selector, not both",
            "use the @eN ref from the latest browser_observe",
        )
    if ref:
        norm = normalize_ref(ref)
        if norm is None:
            raise plan_error(
                "bad_args",
                f"ref must look like @e3, got {spec.ref!r}",
                "copy an @eN ref from the latest browser_observe",
            )
        return norm, None
    if selector:
        _no_injection(selector, "selector")
        if selector.startswith("-"):
            raise plan_error(
                "bad_args",
                "selector must not start with -",
                "pass a CSS selector, or an @eN ref",
            )
        return None, selector
    if required:
        raise plan_error(
            "bad_args",
            "this verb needs a ref or a selector",
            'browser_observe, then pass ref="@eN" and that observation_id',
        )
    return None, None


def _modifiers(spec: Spec) -> str | None:
    raw = _text(spec.modifiers)
    if not raw:
        return None
    parts = [part.strip().lower() for part in raw.split(",") if part.strip()]
    bad = [part for part in parts if part not in _MODIFIERS]
    if not parts or bad:
        raise plan_error(
            "bad_args",
            f"modifiers must be alt, ctrl, meta, shift; got {spec.modifiers!r}",
            'pass modifiers="ctrl,shift"',
        )
    return ",".join(parts)


def _button(spec: Spec) -> str | None:
    button = _text(spec.button).lower()
    if not button:
        return None
    if button not in _BUTTONS:
        raise plan_error(
            "bad_args",
            f"button must be left, middle, or right; got {spec.button!r}",
            'pass button="left"|"right"|"middle"',
        )
    return button


def _wait(spec: Spec) -> str | None:
    wait = _text(spec.wait_until).lower()
    if not wait:
        return None
    if wait not in _WAIT:
        raise plan_error(
            "bad_args",
            f"wait_until must be load, domcontentloaded, networkidle, or commit; got {spec.wait_until!r}",
            "omit wait_until or pass one of those four",
        )
    return wait


def _append_common(spec: Spec, args: list[str]) -> int:
    """tab id and timeout. Returns timeout_ms (0 if omitted)."""
    tab = _pos_int(spec.tab_id, "tab_id")
    if tab is not None:
        args += ["--tab-id", str(tab)]
    timeout = _pos_int(spec.timeout_ms, "timeout_ms")
    if timeout is not None:
        args += ["--timeout", f"{timeout}ms"]
        return timeout
    return 0


def _append_target(spec: Spec, args: list[str], *, required: bool) -> tuple[str | None, str | None]:
    ref, selector = _target(spec, required=required)
    if ref:
        args.append(ref)
    elif selector:
        args.append(selector)
    return ref, selector


def _finish(
    verb: str,
    args: list[str],
    session: str,
    ref: str | None,
    selector: str | None,
    *,
    read: bool,
    records: bool = False,
    timeout_ms: int = 0,
    warning: str | None = None,
    observation_id: str = "",
) -> Plan:
    return Plan(
        verb=verb,
        args=args,
        session=session,
        ref=ref,
        selector=selector,
        read=read,
        records=records,
        invalidates=verb in INVALIDATES,
        observation_id=_text(observation_id),
        timeout_ms=timeout_ms,
        warning=warning,
    )


def _pointer(spec: Spec, verb: str, bsk: str, *, modifiers: bool, button: bool, count: bool, required: bool) -> Plan:
    allow = {"ref", "selector", "tab_id", "timeout_ms"}
    if modifiers:
        allow.add("modifiers")
    if button:
        allow.add("button")
    if count:
        allow.add("click_count")
    session = _check_fields(verb, spec, allow, session="required")
    args = [bsk, "--session", session]
    if button:
        chosen = _button(spec)
        if chosen:
            args += ["--button", chosen]
    if count:
        clicks = _pos_int(spec.click_count, "click_count")
        if clicks is not None:
            args += ["--click-count", str(clicks)]
    if modifiers:
        mods = _modifiers(spec)
        if mods:
            args += ["--modifiers", mods]
    timeout = _append_common(spec, args)
    ref, selector = _append_target(spec, args, required=required)
    return _finish(verb, args, session, ref, selector, read=False, timeout_ms=timeout, observation_id=spec.observation_id)


def plan_status(spec: Spec) -> Plan:
    _check_fields("status", spec, set(), session="forbid")
    return _finish("status", ["status"], "", None, None, read=True)


def plan_observe(spec: Spec) -> Plan:
    kind = _text(spec.verb).lower().replace("-", "_") or "observe"
    if kind not in OBSERVE_KINDS:
        raise plan_error(
            "bad_args",
            f"kind must be observe or snapshot, got {spec.verb!r}",
            "pass kind=observe (semantic) or kind=snapshot (aria tree)",
            valid_kinds=list(OBSERVE_KINDS),
        )
    allow = {"tab_id", "max_tokens", "max_depth"}
    if kind == "observe":
        allow |= {"cursor", "probe_hover"}
    session = _check_fields(kind, spec, allow, session="required")
    args = [kind, "--session", session]
    if kind == "observe" and _text(spec.cursor):
        args += ["--cursor", _no_injection(_text(spec.cursor), "cursor")]
    depth = _pos_int(spec.max_depth, "max_depth")
    if depth is not None:
        args += ["--max-depth", str(depth)]
    tokens = _pos_int(spec.max_tokens, "max_tokens")
    if tokens is not None:
        args += ["--max-tokens", str(tokens)]
    if spec.probe_hover:
        args.append("--probe-hover")
    _append_common(spec, args)
    return _finish(kind, args, session, None, None, read=True, records=True)


def plan_session(spec: Spec) -> Plan:
    action = _text(spec.verb).lower()
    if action not in SESSION_ACTIONS:
        _reject_unknown(
            "action",
            spec.verb,
            sorted(SESSION_ACTIONS),
            empty_hint="pass action=start, stop, or list",
        )
    verb = f"session_{action}"
    if action == "list":
        _check_fields(verb, spec, set(), session="forbid")
        return _finish(verb, ["session", "list"], "", None, None, read=True)
    if action == "start":
        _check_fields(verb, spec, {"browser", "focus"}, session="forbid")
        args = ["session", "start"]
        browser = _text(spec.browser)
        if browser:
            args += ["--browser", _no_injection(browser, "browser")]
        if spec.focus:
            args.append("--focus")
        return _finish(
            verb,
            args,
            "",
            None,
            None,
            read=False,
            warning="session start opens an Agent Window in the connected browser",
        )
    session = _check_fields(verb, spec, set(), session="required")
    return _finish(
        verb,
        ["session", "stop", session],
        session,
        None,
        None,
        read=False,
        warning="session stop closes the Agent Window and returns borrowed tabs",
    )


def plan_tabs(spec: Spec) -> Plan:
    action = _text(spec.verb).lower()
    if action not in TAB_ACTIONS:
        _reject_unknown(
            "action",
            spec.verb,
            sorted(TAB_ACTIONS),
            empty_hint="pass action=list, create, close, select, borrow, or return",
        )
    verb = f"tab_{action}"
    if action == "list":
        session = _check_fields(verb, spec, {"scope"}, session="required")
        args = ["tab", "list", "--session", session]
        scope = _text(spec.scope).lower()
        if scope:
            if scope not in _SCOPES:
                raise plan_error(
                    "bad_args",
                    f"scope must be user, agent, or all; got {spec.scope!r}",
                    "omit scope or pass one of those three",
                )
            args += ["--scope", scope]
        return _finish(verb, args, session, None, None, read=True)
    if action == "create":
        session = _check_fields(verb, spec, {"url", "active"}, session="required")
        args = ["tab", "create", "--session", session]
        url = _text(spec.url)
        if url:
            args += ["--url", _no_injection(url, "url")]
        if spec.active is False:
            args.append("--no-active")
        return _finish(verb, args, session, None, None, read=False)
    session = _check_fields(verb, spec, {"tab_id"}, session="required")
    tab = _pos_int(spec.tab_id, "tab_id")
    if tab is None:
        raise plan_error("bad_args", f"tab {action} needs tab_id", "pass tab_id from browser_tabs action=list")
    warning = _BORROW_WARNING if action == "borrow" else None
    return _finish(verb, ["tab", action, str(tab), "--session", session], session, None, None, read=False, warning=warning)


def _plan_wheel(spec: Spec) -> Plan:
    session = _check_fields(
        "wheel", spec, {"ref", "selector", "delta_x", "delta_y", "modifiers", "tab_id", "timeout_ms"}, session="required"
    )
    dx = _finite(spec.delta_x, "delta_x")
    dy = _finite(spec.delta_y, "delta_y")
    if dx == 0 and dy == 0:
        raise plan_error(
            "bad_args",
            "wheel needs a non-zero delta_x or delta_y",
            "pass delta_y (positive is down the page) or delta_x",
        )
    args = ["wheel", "--session", session, "--delta-x", _num(dx), "--delta-y", _num(dy)]
    mods = _modifiers(spec)
    if mods:
        args += ["--modifiers", mods]
    timeout = _append_common(spec, args)
    ref, selector = _append_target(spec, args, required=False)
    return _finish("wheel", args, session, ref, selector, read=False, timeout_ms=timeout, observation_id=spec.observation_id)


def _num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _plan_fill(spec: Spec) -> Plan:
    session = _check_fields("fill", spec, {"ref", "selector", "value", "clear", "tab_id", "timeout_ms"}, session="required")
    value = spec.value if isinstance(spec.value, str) else str(spec.value)
    if value == "":
        raise plan_error("bad_args", "fill needs value", 'pass value="the text to write"')
    _no_injection(value, "value")
    args = ["fill", "--session", session, "--value", value]
    if spec.clear is False:
        args.append("--no-clear")
    timeout = _append_common(spec, args)
    ref, selector = _append_target(spec, args, required=True)
    return _finish("fill", args, session, ref, selector, read=False, timeout_ms=timeout, observation_id=spec.observation_id)


def _plan_select(spec: Spec) -> Plan:
    session = _check_fields("select", spec, {"ref", "selector", "value", "tab_id", "timeout_ms"}, session="required")
    value = spec.value if isinstance(spec.value, str) else str(spec.value)
    if value == "":
        raise plan_error("bad_args", "select needs value (the option value, not its label)", "pass value=")
    _no_injection(value, "value")
    args = ["select", "--session", session, "--value", value]
    timeout = _append_common(spec, args)
    ref, selector = _append_target(spec, args, required=True)
    return _finish("select", args, session, ref, selector, read=False, timeout_ms=timeout, observation_id=spec.observation_id)


def _plan_press(spec: Spec) -> Plan:
    session = _check_fields("press", spec, {"ref", "selector", "key", "tab_id", "timeout_ms"}, session="required")
    key = _text(spec.key)
    if not key:
        raise plan_error("bad_args", "press needs key", 'pass key="Enter" or key="Ctrl+A"')
    _no_injection(key, "key")
    args = ["press", "--session", session]
    ref, selector = _target(spec, required=False)
    if ref:
        args += ["--ref", ref]
    elif selector:
        args += ["--selector", selector]
    timeout = _append_common(spec, args)
    args.append(key)
    return _finish("press", args, session, ref, selector, read=False, timeout_ms=timeout, observation_id=spec.observation_id)


def _plan_navigate(spec: Spec) -> Plan:
    session = _check_fields("navigate", spec, {"url", "wait_until", "tab_id", "timeout_ms"}, session="required")
    url = _text(spec.url)
    if not url:
        raise plan_error("bad_args", "navigate needs url", "pass url=https://...")
    _no_injection(url, "url")
    args = ["navigate", "--session", session]
    wait = _wait(spec)
    if wait:
        args += ["--wait-until", wait]
    timeout = _append_common(spec, args)
    args.append(url)
    return _finish("navigate", args, session, None, None, read=False, timeout_ms=timeout, observation_id=spec.observation_id)


def _plan_history(spec: Spec, verb: str, bsk: str) -> Plan:
    session = _check_fields(verb, spec, {"wait_until", "tab_id", "timeout_ms"}, session="required")
    args = [bsk, "--session", session]
    wait = _wait(spec)
    if wait:
        args += ["--wait-until", wait]
    timeout = _append_common(spec, args)
    return _finish(verb, args, session, None, None, read=False, timeout_ms=timeout, observation_id=spec.observation_id)


def _plan_reload(spec: Spec) -> Plan:
    session = _check_fields("reload", spec, {"wait_until", "hard", "tab_id", "timeout_ms"}, session="required")
    args = ["reload", "--session", session]
    wait = _wait(spec)
    if wait:
        args += ["--wait-until", wait]
    timeout = _append_common(spec, args)
    if spec.hard:
        args.append("--hard")
    return _finish("reload", args, session, None, None, read=False, timeout_ms=timeout, observation_id=spec.observation_id)


def plan_act(spec: Spec) -> Plan:
    raw = _text(spec.verb)
    key = raw.lower().replace("-", "_")
    alias = None
    if key in ACT_ALIASES:
        alias = key
        key = ACT_ALIASES[key]
    if key not in ACT_VERBS:
        hint_aliases = ", ".join(f"{src}={dst}" for src, dst in ACT_ALIASES.items())
        if not raw:
            raise plan_error(
                "bad_args",
                "verb is required",
                f"pass a verb ({', '.join(ACT_VERBS)}); aliases: {hint_aliases}",
                valid_verbs=list(ACT_VERBS),
            )
        raise plan_error(
            "unknown_verb",
            f"unknown verb {raw!r}; nothing was run",
            f"use one of: {', '.join(ACT_VERBS)}. aliases: {hint_aliases}. "
            "page reads use browser_observe; sessions use browser_session; tabs use browser_tabs",
            valid_verbs=list(ACT_VERBS),
        )
    builder = _ACT[key]
    plan = builder(spec)
    if alias:
        plan.alias_from = alias
        plan.notes.append(f"verb {alias} runs as {key}")
    return plan


def _click(spec: Spec) -> Plan:
    return _pointer(spec, "click", "click", modifiers=True, button=True, count=True, required=True)


def _hover(spec: Spec) -> Plan:
    return _pointer(spec, "hover", "hover", modifiers=True, button=False, count=False, required=True)


def _scroll_to(spec: Spec) -> Plan:
    return _pointer(spec, "scroll_to", "scroll-to", modifiers=False, button=False, count=False, required=True)


def _focus(spec: Spec) -> Plan:
    return _pointer(spec, "focus", "focus", modifiers=False, button=False, count=False, required=True)


def _blur(spec: Spec) -> Plan:
    return _pointer(spec, "blur", "blur", modifiers=False, button=False, count=False, required=True)


def _back(spec: Spec) -> Plan:
    return _plan_history(spec, "navigate_back", "navigate-back")


def _forward(spec: Spec) -> Plan:
    return _plan_history(spec, "navigate_forward", "navigate-forward")


_ACT = {
    "click": _click,
    "hover": _hover,
    "wheel": _plan_wheel,
    "scroll_to": _scroll_to,
    "focus": _focus,
    "blur": _blur,
    "fill": _plan_fill,
    "select": _plan_select,
    "press": _plan_press,
    "navigate": _plan_navigate,
    "navigate_back": _back,
    "navigate_forward": _forward,
    "reload": _plan_reload,
}


def allowlist() -> dict:
    """Read vs act, in the names the tools accept."""
    return {
        "read": [
            "browser_status",
            "browser_observe kind=observe",
            "browser_observe kind=snapshot",
            "browser_session action=list",
            "browser_tabs action=list",
        ],
        "act": [
            *[f"browser_act verb={name}" for name in ACT_VERBS],
            "browser_session action=start",
            "browser_session action=stop",
            *[
                f"browser_tabs action={name}"
                for name in ("create", "close", "select", "borrow", "return")
            ],
        ],
        "act_aliases": dict(ACT_ALIASES),
    }
