"""Map a ``bsk --json`` result onto cu-perceive's refusal shape.

Exit classes: 0 ok, 1 user, 2 transport, 3 browser, 4 timeout, 5 version.
``data.reason`` passes through when bsk sent one (``ref_not_found``,
``input_outcome_unknown``, ...). Outcome-unknown reasons keep a hint not to
retry blindly.
"""

from __future__ import annotations

import json
import os

from .backend import BskRun


INSTALL_HINT = (
    "Install the BrowserSkill CLI (the bsk binary) and put it on PATH, "
    "or set CU_PERCEIVE_BSK to that executable. "
    "Project page: https://github.com/Tencent/BrowserSkill"
)

# data.reason values where another attempt without looking is unsafe.
_OUTCOME_UNKNOWN = frozenset({"input_outcome_unknown", "transfer_outcome_unknown"})
_OUTCOME_HINT = (
    "do not retry blindly; observe the page again and decide from what it shows before another attempt"
)

_EXIT_REASON = {1: "user_error", 2: "transport", 3: "browser", 4: "timeout", 5: "version"}
_CODE_REASON = {
    "unknown_method": "version",
    "version_too_old": "version",
    "unsupported": "unsupported",
    "invalid_params": "invalid_params",
    "not_found": "not_found",
    "permission_denied": "permission_denied",
    "timeout": "timeout",
    "cdp_failed": "browser",
    "protocol_error": "transport",
    "cancelled": "cancelled",
    "user_aborted": "user_aborted",
    "multiple_browsers_online": "multiple_browsers_online",
    "no_browser_connected": "no_browser_connected",
}
_EXIT_HINT = {
    1: "the browser command was refused; read reason and fix the arguments or the page state",
    2: "bsk could not complete the exchange; try browser_status",
    3: "the browser rejected the command; observe the page before acting again",
    4: "the browser command timed out; observe before retrying",
    5: "bsk and the browser extension disagree on protocol version; update or reload the extension",
}


def missing_dependency() -> dict:
    configured = (os.environ.get("CU_PERCEIVE_BSK") or "").strip()
    where = configured or "bsk on PATH"
    return {
        "ok": False,
        "reason": "missing_dependency",
        "error": f"browser tools need the bsk CLI ({where}), and it was not found",
        "missing_dependency": "bsk",
        "install": INSTALL_HINT,
        "hint": INSTALL_HINT,
        "dry_run": True,
    }


def parse_json_blob(text: str):
    """Parse a JSON object or array, including one pretty-printed or wrapped in a log line."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    candidates: list[tuple[int, str]] = []
    start_obj = raw.find("{")
    end_obj = raw.rfind("}")
    if start_obj >= 0 and end_obj > start_obj:
        candidates.append((start_obj, raw[start_obj : end_obj + 1]))
    start_arr = raw.find("[")
    end_arr = raw.rfind("]")
    if start_arr >= 0 and end_arr > start_arr:
        candidates.append((start_arr, raw[start_arr : end_arr + 1]))
    for _, blob in sorted(candidates):
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            continue
    return None


def _clip(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _outcome_hint(reason: str, bsk_hint: str | None) -> str | None:
    if reason not in _OUTCOME_UNKNOWN:
        return bsk_hint or None
    if bsk_hint and _OUTCOME_HINT in bsk_hint:
        return bsk_hint
    if bsk_hint:
        return f"{bsk_hint} {_OUTCOME_HINT}"
    return _OUTCOME_HINT


def _usage_error(text: str) -> str | None:
    """The short clap line from a bsk usage dump, or None when this is not one.

    Clap prints ``error: ...`` plus a ``Usage:`` block and exits 1. The dump is
    not the message callers should retry against.
    """
    raw = text or ""
    lowered = raw.lower()
    looks_like_clap = (
        "unexpected argument" in lowered
        or "unexpected value" in lowered
        or ("invalid value" in lowered and "error:" in lowered)
        or ("error:" in lowered and "usage:" in lowered)
        or "for more information, try '--help'" in lowered
    )
    if not looks_like_clap:
        return None
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("error:"):
            message = stripped.split(":", 1)[1].strip()
            return message or stripped
    for line in raw.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("unexpected argument") or low.startswith("unexpected value") or low.startswith("invalid value"):
            return stripped
    return "bsk rejected the command line"


def interpret_run(run: BskRun) -> dict:
    """Partial tool payload from one run. Caller adds verb, command, and session fields.

    ``ok`` true with ``result`` is a success payload (including a navigation wait
    that exited 4 with ``reached=timeout``). Anything else is a refusal.
    """
    if run.missing:
        return missing_dependency()
    if run.timed_out:
        return {
            "ok": False,
            "reason": "timeout",
            "error": "bsk timed out before it exited",
            "hint": "the bsk process exceeded the wait; observe the page before retrying a browser action",
            "dry_run": False,
            "exit_code": None,
        }
    if run.spawn_error and run.exit_code is None:
        return {
            "ok": False,
            "reason": "transport",
            "error": run.spawn_error,
            "hint": "could not start bsk; check CU_PERCEIVE_BSK or the install",
            "dry_run": False,
            "exit_code": None,
        }
    parsed = parse_json_blob(run.stdout)
    if parsed is None:
        parsed = parse_json_blob(run.stderr)
    if run.exit_code == 0:
        if isinstance(parsed, dict):
            return {"ok": True, "dry_run": False, "exit_code": 0, "result": parsed}
        if isinstance(parsed, list):
            # session list --json prints the sessions array, not an object.
            return {"ok": True, "dry_run": False, "exit_code": 0, "result": {"items": parsed}}
        return {
            "ok": False,
            "reason": "bsk_failed",
            "error": "bsk exited 0 but did not return JSON",
            "hint": "retry browser_status; a working bsk --json prints JSON",
            "dry_run": False,
            "exit_code": 0,
        }
    usage = _usage_error(run.stderr) or _usage_error(run.stdout)
    if usage and not isinstance(parsed, dict):
        return {
            "ok": False,
            "reason": "bad_args",
            "error": usage,
            "hint": "bsk rejected the command line; values that start with - are passed as --flag=value or after --",
            "dry_run": False,
            "exit_code": run.exit_code,
        }
    # Navigate / reload can print a success-shaped body and exit 4 when the
    # lifecycle wait expires. The page may still be usable; surface it as data.
    if (
        run.exit_code == 4
        and isinstance(parsed, dict)
        and parsed.get("reached") == "timeout"
        and not parsed.get("code")
    ):
        return {
            "ok": True,
            "dry_run": False,
            "exit_code": 4,
            "result": parsed,
            "warnings": [
                "navigation wait ended with reached=timeout; the page may still be loading; observe before acting"
            ],
        }
    return _error_envelope(run, parsed if isinstance(parsed, dict) else None)


def _error_envelope(run: BskRun, parsed: dict | None) -> dict:
    bsk_code = None
    message = None
    bsk_hint = None
    data = None
    if parsed:
        code = parsed.get("code")
        if isinstance(code, str) and code:
            bsk_code = code
        if isinstance(parsed.get("message"), str):
            message = parsed["message"]
        if isinstance(parsed.get("hint"), str) and parsed["hint"]:
            bsk_hint = parsed["hint"]
        if isinstance(parsed.get("data"), dict):
            data = parsed["data"]
    reason = None
    if data and isinstance(data.get("reason"), str) and data["reason"].strip():
        reason = data["reason"].strip()
    elif bsk_code:
        reason = _CODE_REASON.get(bsk_code, bsk_code)
    else:
        reason = _EXIT_REASON.get(run.exit_code, "bsk_failed")
    if not message:
        message = _clip(run.stderr) or _clip(run.stdout) or f"bsk exited {run.exit_code}"
    hint = _outcome_hint(reason, bsk_hint) or _EXIT_HINT.get(run.exit_code) or "try browser_status"
    out = {
        "ok": False,
        "reason": reason,
        "error": message,
        "hint": hint,
        "dry_run": False,
        "exit_code": run.exit_code,
    }
    if bsk_code:
        out["bsk_code"] = bsk_code
    if data is not None:
        out["data"] = data
        if "effect_state" in data:
            out["effect_state"] = data["effect_state"]
    stderr = _clip(run.stderr)
    if stderr and stderr != message:
        out["stderr"] = stderr
    return out
