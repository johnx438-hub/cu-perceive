"""MCP handlers. Read verbs run immediately; acting verbs dry-run unless ``go``."""

from __future__ import annotations

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


def _dry_notes(go: bool, dry_run: bool | None) -> list[str]:
    if go and dry_run:
        return ["dry_run=true overrides go=true: this was a dry run, nothing was sent"]
    if dry_run is False and not go:
        return ["dry_run=false does not send input; pass go=true to send"]
    return []


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
    send = plan.read or (bool(go) and not dry_run)
    notes = [] if plan.read else _dry_notes(bool(go), dry_run)
    if plan.read and (go or dry_run is not None):
        notes.append("this call is read-only and runs immediately; go and dry_run do not change it")
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
    """Start, stop, or list browser sessions. list runs immediately; start and stop dry-run unless go=true.

    action=start opens an Agent Window (browser= when more than one is connected;
    focus=true brings it forward) and returns its session id. action=stop takes that
    session id, closes the window, and returns borrowed tabs. action=list is read-only.
    dry_run=true beats go. Unknown arguments are refused (reason=unknown_args).
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
    """List or change tabs. list runs immediately; create, close, select, borrow, and return dry-run unless go=true.

    list takes scope=user|agent|all. create takes url. close, select, borrow, and return
    take tab_id. borrow moves a user tab into the Agent Window and may ask the user to
    confirm once go=true. A successful create/close/select/borrow/return drops this
    session's observe refs; call browser_observe again before browser_act.
    dry_run=true beats go.
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
    """Act in a browser session. Dry-run unless go=true; dry_run=true wins and nothing is sent.

    verb: click, hover, wheel, scroll_to, focus, blur, fill, select, press, navigate,
    navigate_back, navigate_forward, reload. Aliases: type=fill, scroll=wheel, key=press,
    back=navigate_back, forward=navigate_forward.
    A dry run returns command (the exact bsk argv, including --json) and ref, and does
    not start bsk. go=true runs that argv. A ref must come from the latest browser_observe
    for this session: pass its observation_id. An older ref is refused with
    reason=stale_observation and bsk is not started.
    Unknown arguments are refused (reason=unknown_args) and nothing runs. bsk errors keep
    data.reason; input_outcome_unknown means do not retry blindly — observe first.
    Desktop clicks stay on act. This tool does not drive a desktop window.
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
