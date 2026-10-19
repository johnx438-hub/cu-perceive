"""MCP handlers. Read verbs run immediately. Acting verbs run unless ``dry_run=true``.

``go`` is accepted and ignored, so a caller that uses the desktop ``act`` style
still runs. ``CU_PERCEIVE_BSK_REQUIRE_GO=1`` restores dry-run unless ``go=true``
(``dry_run=true`` still wins). Desktop ``act`` is a different tool and is unchanged.
"""

from __future__ import annotations

import os

from . import catalog
from .backend import command_argv, get_backend, set_backend, timeout_sec
from .errors import interpret_run, missing_dependency
from .ledger import LEDGER, stale_refusal


def reset_browser_state() -> None:
    """Drop the in-memory observe ledger and any test backend."""
    set_backend(None)
    LEDGER.reset()


def register(decorate) -> None:
    """Register the five browser tools with the MCP server's tool decorator."""
    for fn in (browser_status, browser_session, browser_observe, browser_tabs, browser_act):
        decorate(fn)


def _warnings(*groups: list[str] | None) -> list[str]:
    out: list[str] = []
    for group in groups:
        for note in group or []:
            if note and note not in out:
                out.append(note)
    return out


_REQUIRE_GO_ON = frozenset({"1", "true", "yes", "on"})


def bsk_require_go() -> bool:
    """True when browser acting verbs dry-run unless ``go=true``."""
    return os.environ.get("CU_PERCEIVE_BSK_REQUIRE_GO", "").strip().lower() in _REQUIRE_GO_ON


def _act_send(go: bool, dry_run: bool | None) -> tuple[bool, list[str]]:
    """(run the command, notes). ``dry_run=true`` always previews. ``go`` gates only under the env switch."""
    if dry_run:
        if bsk_require_go() and go:
            return False, ["dry_run=true overrides go=true: this was a dry run, nothing was sent"]
        return False, ["dry_run=true: preview only, nothing was sent"]
    if bsk_require_go():
        notes = []
        if dry_run is False and not go:
            notes.append("dry_run=false does not send input; pass go=true to send")
        return bool(go), notes
    return True, []


def _with_plan(payload: dict, plan: catalog.Plan, argv: list[str] | None, exe: str | None) -> dict:
    payload["verb"] = plan.verb
    payload["session"] = plan.session or None
    payload["ref"] = plan.ref
    if plan.selector:
        payload["selector"] = plan.selector
    if plan.observation_id:
        payload["observation_id"] = plan.observation_id
    if argv is not None:
        payload["command"] = argv
    if exe:
        payload["bsk"] = exe
    payload["backend"] = get_backend().name
    return payload


def _execute(plan: catalog.Plan, *, go: bool, dry_run: bool | None) -> dict:
    """Shared path for every browser tool after the argv exists."""
    if not plan.read:
        refusal = stale_refusal(
            session=plan.session,
            ref=plan.ref,
            observation_id=plan.observation_id,
            latest=LEDGER.latest(plan.session) if plan.session else None,
            verb=plan.verb,
        )
        if refusal is not None:
            if plan.selector:
                refusal["selector"] = plan.selector
            return refusal
    if plan.read:
        send = True
        notes = []
        if go or dry_run is not None:
            notes.append("this call is read-only and runs immediately; go and dry_run do not change it")
    else:
        send, notes = _act_send(bool(go), dry_run)
    notes.extend(plan.notes)
    if plan.selector and not plan.ref and not plan.read:
        notes.append(
            "selector targets are not tied to an observe generation; prefer a ref from browser_observe"
        )
    backend = get_backend()
    exe = backend.executable()
    if not exe:
        out = missing_dependency()
        out = _with_plan(out, plan, command_argv("bsk", plan.args), None)
        extra = _warnings(notes, [plan.warning] if (plan.warning and not send) else None)
        if extra:
            out["warnings"] = extra
        return out
    argv = command_argv(exe, plan.args)
    if not send:
        out = {
            "ok": True,
            "dry_run": True,
            "verb": plan.verb,
            "session": plan.session or None,
            "ref": plan.ref,
            "command": argv,
            "bsk": exe,
            "backend": backend.name,
        }
        if plan.selector:
            out["selector"] = plan.selector
        if plan.observation_id:
            out["observation_id"] = plan.observation_id
        extra = _warnings(notes, [plan.warning] if plan.warning else None)
        if extra:
            out["warnings"] = extra
        return out
    run = backend.run(plan.args, timeout=timeout_sec(plan.timeout_ms))
    # The binary can disappear between the lookup and the spawn.
    if run.missing and run.exit_code is None and not run.timed_out and not run.spawn_error:
        out = _with_plan(missing_dependency(), plan, run.argv, exe)
        extra = _warnings(notes)
        if extra:
            out["warnings"] = extra
        return out
    interpreted = interpret_run(run)
    interpreted = _with_plan(interpreted, plan, run.argv or argv, exe)
    result = interpreted.get("result")
    if interpreted.get("ok") and isinstance(result, dict):
        started = result.get("session_id")
        if isinstance(started, str) and started.strip() and not plan.session:
            interpreted["session"] = started.strip()
    if interpreted.get("ok") and plan.records and plan.session:
        obs = LEDGER.record(plan.session, interpreted.get("result"))
        interpreted["observation_id"] = obs.id
        interpreted["refs"] = list(obs.refs)
    elif interpreted.get("ok") and plan.invalidates and plan.session:
        LEDGER.forget(plan.session)
    extra = _warnings(notes, interpreted.get("warnings"))
    if extra:
        interpreted["warnings"] = extra
    return interpreted


def _guard(plan_fn, spec: catalog.Spec, *, go: bool = False, dry_run: bool | None = None) -> dict:
    try:
        plan = plan_fn(spec)
    except catalog.PlanError as exc:
        return exc.payload
    return _execute(plan, go=go, dry_run=dry_run)


def browser_status():
    """Read bsk status (daemon, protocol, connected browsers). Runs immediately.

    Page tasks use the browser_* tools, which shell out to the bsk CLI
    (CU_PERCEIVE_BSK, otherwise PATH). Desktop apps stay on windows /
    perceive_window / act. A missing bsk returns ok=false, reason=missing_dependency,
    and an install hint. Does not click.
    """
    return _guard(catalog.plan_status, catalog.Spec(verb="status"))


def browser_session(
    action: str = "",
    session: str = "",
    browser: str = "",
    focus: bool = False,
    go: bool = False,
    dry_run: bool | None = None,
):
    """Start, stop, or list browser sessions. list is read-only. start and stop run unless dry_run=true.

    action=start opens an Agent Window (browser= when more than one is connected;
    focus=true brings it forward) and returns its session id. action=stop takes that
    session id, closes the window, and returns borrowed tabs. go is accepted and does
    not gate the call. CU_PERCEIVE_BSK_REQUIRE_GO=1 dry-runs start/stop unless go=true
    (dry_run=true still wins). Desktop act is unchanged. Unknown arguments are refused.
    """
    return _guard(
        catalog.plan_session,
        catalog.Spec(verb=action, session=session, browser=browser, focus=bool(focus)),
        go=bool(go),
        dry_run=dry_run,
    )


def browser_observe(
    session: str = "",
    kind: str = "observe",
    cursor: str = "",
    max_tokens: int = 0,
    max_depth: int = 0,
    tab_id: int = 0,
    probe_hover: bool = False,
):
    """Read the session's active tab. kind=observe (default) or snapshot. Runs immediately.

    Returns observation_id and refs (@eN). bsk replaces those refs on the next observe;
    pass both the ref and this observation_id to browser_act. cursor, max_tokens,
    max_depth, tab_id, and probe_hover map onto bsk flags. Needs a session from
    browser_session action=start.
    """
    return _guard(
        catalog.plan_observe,
        catalog.Spec(
            verb=kind or "observe",
            session=session,
            cursor=cursor,
            max_tokens=max_tokens,
            max_depth=max_depth,
            tab_id=tab_id,
            probe_hover=bool(probe_hover),
        ),
    )


def browser_tabs(
    action: str = "",
    session: str = "",
    tab_id: int = 0,
    url: str = "",
    scope: str = "",
    active: bool = True,
    go: bool = False,
    dry_run: bool | None = None,
):
    """List or change tabs. list is read-only. create, close, select, borrow, and return run unless dry_run=true.

    list takes scope=user|agent|all. create takes url. close, select, borrow, and return
    take tab_id. borrow moves a user tab into the Agent Window and may ask the user to
    confirm, and that wait happens when the command runs. go is accepted and does not
    gate the call. CU_PERCEIVE_BSK_REQUIRE_GO=1 dry-runs the acting actions unless
    go=true (dry_run=true still wins). A successful create/close/select/borrow/return
    drops this session's observe refs; call browser_observe again before browser_act.
    """
    return _guard(
        catalog.plan_tabs,
        catalog.Spec(
            verb=action,
            session=session,
            tab_id=tab_id,
            url=url,
            scope=scope,
            active=bool(active),
        ),
        go=bool(go),
        dry_run=dry_run,
    )


def browser_act(
    verb: str = "",
    session: str = "",
    ref: str = "",
    selector: str = "",
    url: str = "",
    value: str = "",
    key: str = "",
    button: str = "",
    click_count: int = 0,
    modifiers: str = "",
    delta_x: float = 0,
    delta_y: float = 0,
    tab_id: int = 0,
    timeout_ms: int = 0,
    wait_until: str = "",
    clear: bool = True,
    hard: bool = False,
    observation_id: str = "",
    go: bool = False,
    dry_run: bool | None = None,
):
    """Act in a browser session. Runs unless dry_run=true. go is accepted and does not gate the call.

    verb: click, hover, wheel, scroll_to, focus, blur, fill, select, press, navigate,
    navigate_back, navigate_forward, reload. Aliases: type=fill, scroll=wheel, key=press,
    back=navigate_back, forward=navigate_forward.
    dry_run=true returns command (the exact bsk argv, including --json) and ref, and does
    not start bsk. CU_PERCEIVE_BSK_REQUIRE_GO=1 restores dry-run unless go=true, and
    dry_run=true still wins. A ref must come from the latest browser_observe for this
    session: pass its observation_id. An older ref is refused with reason=stale_observation
    and bsk is not started.
    Unknown arguments are refused (reason=unknown_args) and nothing runs. bsk errors keep
    data.reason; input_outcome_unknown means do not retry blindly — observe first.
    Desktop clicks stay on act, which still dry-runs unless go=true.
    """
    return _guard(
        catalog.plan_act,
        catalog.Spec(
            verb=verb,
            session=session,
            ref=ref,
            selector=selector,
            url=url,
            value=value,
            key=key,
            button=button,
            click_count=click_count,
            modifiers=modifiers,
            delta_x=delta_x,
            delta_y=delta_y,
            tab_id=tab_id,
            timeout_ms=timeout_ms,
            wait_until=wait_until,
            clear=bool(clear),
            hard=bool(hard),
            observation_id=observation_id,
        ),
        go=bool(go),
        dry_run=dry_run,
    )
