"""Latest observe per bsk session.

bsk replaces ``@eN`` refs on every observe and does not expose a generation.
This ledger is the cheap stand-in: an act whose ref or ``observation_id`` is
not the latest ``browser_observe`` / ``browser_snapshot`` for that session is
refused before ``bsk`` runs. State is in-memory for this process only.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass


_REF = re.compile(r"@e\d+")


@dataclass(frozen=True)
class Observation:
    id: str
    refs: tuple[str, ...]


def extract_refs(payload) -> list[str]:
    """``@eN`` refs in first-seen order, from any string in a JSON-like payload."""
    found: list[str] = []
    seen: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, str):
            for ref in _REF.findall(value):
                if ref not in seen:
                    seen.add(ref)
                    found.append(ref)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(payload)
    return found


class ObservationLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._n = 0
        self._by_session: dict[str, Observation] = {}

    def reset(self) -> None:
        with self._lock:
            self._n = 0
            self._by_session.clear()

    def record(self, session: str, payload) -> Observation:
        refs = tuple(extract_refs(payload))
        with self._lock:
            self._n += 1
            obs = Observation(id=f"obs-{self._n}", refs=refs)
            self._by_session[session] = obs
            return obs

    def forget(self, session: str) -> None:
        with self._lock:
            self._by_session.pop(session, None)

    def latest(self, session: str) -> Observation | None:
        with self._lock:
            return self._by_session.get(session)


LEDGER = ObservationLedger()


def stale_refusal(
    *,
    session: str,
    ref: str | None,
    observation_id: str,
    latest: Observation | None,
    verb: str,
) -> dict | None:
    """None when the act may proceed. Otherwise an ``ok: false`` payload; nothing was run."""
    if not ref and not observation_id:
        return None
    hint = "call browser_observe for this session and pass that observation_id with a ref from its refs"
    latest_id = None if latest is None else latest.id
    if latest is None:
        why = (
            f"ref {ref} has no observe for session {session}"
            if ref
            else f"observation_id {observation_id} does not match an observe for session {session}"
        )
    elif not observation_id and ref:
        why = f"ref {ref} needs observation_id from the latest browser_observe for session {session}"
    elif observation_id != latest.id:
        why = (
            f"observation_id {observation_id or '(none)'} is not the latest observe "
            f"({latest.id}) for session {session}"
        )
    elif ref and ref not in latest.refs:
        why = f"ref {ref} is not in the latest observe ({latest.id}) for session {session}"
    else:
        return None
    return {
        "ok": False,
        "reason": "stale_observation",
        "error": why,
        "hint": hint,
        "dry_run": True,
        "verb": verb,
        "session": session,
        "ref": ref,
        "observation_id": observation_id or None,
        "latest_observation_id": latest_id,
    }
