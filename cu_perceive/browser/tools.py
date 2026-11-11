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
from .ledger import LEDGER, action_changed_url, stale_refusal


def reset_browser_state() -> None:
    """Drop the in-memory observe ledger and any test backend."""
    set_backend(None)
    LEDGER.reset()


def register(decorate) -> None:
    """Register the six browser tools with the MCP server's tool decorator."""
    for fn in (browser_status, browser_session, browser_observe, browser_tabs, browser_cooperate, browser_act):
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
    from ..settings import get as _cfg

    return bool(_cfg("bsk.require_go"))


_GO_NOTE = "pass go=true to run this command"


def _act_send(go: bool, dry_run: bool | None) -> tuple[bool, list[str]]:
    """(run the command, notes). ``dry_run=true`` always previews. ``go`` gates only under the env switch."""
    if dry_run:
        if bsk_require_go() and go:
            return False, ["dry_run=true overrides go=true: this was a dry run, nothing was sent"]
        notes = ["dry_run=true: preview only, nothing was sent"]
        if bsk_require_go():
            notes.append(_GO_NOTE)
        return False, notes
    if bsk_require_go():
        if not go:
            if dry_run is False:
                return False, ["dry_run=false does not send input; pass go=true to send"]
            return False, [_GO_NOTE]
        return True, []
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
    elif interpreted.get("ok") and plan.verb in {"click", "press", "fill"} and plan.session:
        # Future bsk only. 0.3.3 click/press/fill JSON has no page URL, so this
        # comparison does not clear refs on that release.
        latest = LEDGER.latest(plan.session)
        previous = None if latest is None else latest.page_url
        if action_changed_url(previous, interpreted.get("result")):
            LEDGER.forget(plan.session)
            notes.append(
                "the page URL changed; stored refs were cleared; call browser_observe before using a ref"
            )
    elif (
        not interpreted.get("ok")
        and plan.session
        and interpreted.get("reason") in {"ref_not_found", "visual_target_changed"}
    ):
        LEDGER.forget(plan.session)
        hint = (interpreted.get("hint") or "").strip()
        if "observe again" not in hint.lower():
            interpreted["hint"] = f"{hint} observe again".strip() if hint else "observe again"
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
    perceive_window / act. Also reports cu_perceive.integrity_level (medium/high): apps
    running above it (as administrator) cannot be driven. A missing bsk returns ok=false, reason=missing_dependency,
    and an install hint. Does not click.
    """
    out = _guard(catalog.plan_status, catalog.Spec(verb="status"))
    if isinstance(out, dict):
        out["cu_perceive"] = _self_integrity_brief()
    return out


def _self_integrity_brief() -> dict:
    """cu-perceive's own integrity level: targets above it (elevated apps) cannot receive input."""
    try:
        from ..windows import self_integrity

        info = self_integrity()
    except Exception as e:
        return {"integrity_level": None, "elevated": None, "error": str(e)}
    out = {"integrity_level": info.get("level"), "elevated": info.get("elevated")}
    if info.get("error"):
        out["error"] = info["error"]
    if info.get("level") and not info.get("elevated"):
        out["note"] = "windows running as administrator cannot be driven (act refuses with reason=elevated_target)"
    return out


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
    out = _guard(
        catalog.plan_session,
        catalog.Spec(verb=action, session=session, browser=browser, focus=bool(focus)),
        go=bool(go),
        dry_run=dry_run,
    )
    if (action or "").strip().lower() == "stop" and session and out.get("ok") and not out.get("dry_run"):
        LEDGER.clear_context(session.strip())
    return out


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

    Returns observation_id and refs (@eN). A fresh observe replaces those refs.
    cursor continues the same capture as the next page; bsk replaces the ref map
    on that page too, so earlier pages' refs are not kept. Pass both the ref and
    this observation_id to browser_act. cursor, max_tokens,
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
    context: str = "",
    auto_sync: bool = True,
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
    Shared-tab control: the act carries the control context from the last browser_cooperate sync of
    this session (or context= when given) as bsk --context. When bsk answers cooperation_sync_required
    or control_context_invalid and auto_sync=true (default), the server runs one cooperate sync
    (--settle), stores the new context, records the page it returns as a new observation, and replies
    ok=false with synced, observation_id and refs: nothing was sent, so pick the ref again from those
    refs and repeat the act (the context is then passed for you). It never repeats the input itself.
    """
    sess = (session or "").strip()
    ctx = (context or "").strip() or (LEDGER.context(sess) if sess else None)
    out = _guard(
        lambda spec: _with_context(catalog.plan_act(spec), ctx),
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
    return _after_act(out, sess, bool(auto_sync))


COOP_SYNC_REASONS = frozenset({"cooperation_sync_required", "control_context_invalid"})


def _with_context(plan: catalog.Plan, ctx: str | None) -> catalog.Plan:
    if ctx and not plan.read and "--context" not in plan.args:
        plan.args = list(plan.args) + ["--context", ctx]
        plan.notes.append("passing the stored cooperate control context (--context)")
    return plan


def _after_act(out: dict, session: str, auto_sync: bool) -> dict:
    """Recover from a missing / invalid control context with one sync; never re-send the input."""
    reason = out.get("reason") if isinstance(out, dict) else None
    if not session or reason not in COOP_SYNC_REASONS or out.get("dry_run"):
        return out
    LEDGER.clear_context(session)
    if not auto_sync:
        out["hint"] = ("call browser_cooperate action=sync for this session, observe the returned refs, "
                       "then repeat the act (the context is passed for you)")
        return out
    sync = _cooperate(action="sync", session=session, policy="", settle=True, timeout_ms=0, tab_id=0,
                      dry_run=None, go=False)
    out["synced"] = bool(sync.get("ok"))
    out["sync"] = {k: sync.get(k) for k in ("ok", "reason", "error", "context_stored", "observation_id")
                   if sync.get(k) is not None}
    if sync.get("ok"):
        out["observation_id"] = sync.get("observation_id")
        out["refs"] = sync.get("refs")
        out["hint"] = ("control synced and the page re-observed; nothing was sent. Choose the target again from "
                       "refs and repeat browser_act with this observation_id (context is passed automatically)")
    else:
        out["hint"] = "cooperate sync failed; a person may hold the tab. Read sync.reason; do not retry blindly"
    return out


def _cooperate(*, action: str, session: str, policy: str, settle: bool, timeout_ms: int, tab_id: int,
               dry_run: bool | None, go: bool) -> dict:
    act = (action or "").strip().lower()
    sess = (session or "").strip()
    if act not in {"status", "sync", "wait"}:
        return {"ok": False, "reason": "bad_args", "error": f"unknown action {action!r}",
                "hint": "action is status, sync or wait", "dry_run": True}
    if not sess:
        return {"ok": False, "reason": "bad_args", "error": "session is required",
                "hint": "pass the session id from browser_session action=start", "dry_run": True}
    argv = ["cooperate", act, "--session", sess, "--json"]
    pol = (policy or "").strip().lower()
    if pol:
        if pol not in {"auto", "target", "strict"}:
            return {"ok": False, "reason": "bad_args", "error": f"unknown policy {policy!r}",
                    "hint": "policy is auto, target or strict", "dry_run": True}
        if act != "sync":
            return {"ok": False, "reason": "bad_args", "error": "policy applies to sync only", "dry_run": True}
        argv += ["--policy", pol]
    if settle and act == "sync":
        argv.append("--settle")
    if timeout_ms:
        argv += ["--timeout-ms", str(min(max(int(timeout_ms), 1), 25000))]
    if tab_id:
        argv += ["--tab-id", str(int(tab_id))]
    plan = catalog.Plan(verb=f"cooperate_{act}", args=argv, session=sess, ref=None, selector=None,
                        read=(act == "status"), records=(act == "sync"), invalidates=False,
                        timeout_ms=int(timeout_ms or 0) + (30000 if act != "status" else 0))
    out = _execute(plan, go=go, dry_run=dry_run)
    result = out.get("result") if isinstance(out, dict) else None
    if out.get("ok") and not out.get("dry_run") and isinstance(result, dict):
        ctx = result.get("context_id")
        if not ctx and isinstance(result.get("post"), dict):
            ctx = result["post"].get("context_id")
        if isinstance(ctx, str) and ctx.strip():
            LEDGER.set_context(sess, ctx.strip())
            out["context_stored"] = True
        elif act == "sync":
            out["context_stored"] = False
            out.setdefault("warnings", []).append(
                "sync returned no context_id (a person may hold the tab); acts will need another sync")
    return out


def browser_cooperate(
    action: str = "status",
    session: str = "",
    policy: str = "",
    settle: bool = True,
    timeout_ms: int = 0,
    tab_id: int = 0,
    go: bool = False,
    dry_run: bool | None = None,
):
    """Shared-tab control for a browser session (bsk cooperate). action=status (read-only), sync, or wait.

    sync takes control under the session's policy (policy=auto|target|strict; empty keeps bsk's default)
    and, with settle=true (default), retries transient read-only refusals. Its context_id is stored for
    the session and passed to every later browser_act as --context; the page it returns is recorded as a
    new observation (observation_id, refs) for browser_act. wait waits for a person to hand the tab back
    (timeout_ms up to 25000). sync and wait run unless dry_run=true; go is accepted and does not gate the
    call. browser_act already syncs once by itself on cooperation_sync_required.
    """
    return _cooperate(action=action, session=session, policy=policy, settle=bool(settle),
                      timeout_ms=timeout_ms, tab_id=tab_id, dry_run=dry_run, go=bool(go))
