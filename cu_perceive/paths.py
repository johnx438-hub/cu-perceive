"""Win <-> WSL path helpers. Drive letter always lowercased on the WSL side."""
from __future__ import annotations

WSL_DISTRO = "Ubuntu"
_UNC_PREFIXES = ("\\\\wsl.localhost\\", "\\\\wsl$\\")


def coerce_out_dir(raw: str | None) -> str | None:
    """Accept Windows, UNC, /mnt/<drive>/..., or /home/... (rewritten to UNC)."""
    if not raw:
        return None
    s = str(raw).strip().strip('"')
    if not s:
        return None
    if s.startswith("/mnt/") and len(s) > 6 and s[5].isalpha() and s[6] == "/":
        drive = s[5].upper()
        rest = s[7:].replace("/", "\\")
        return f"{drive}:\\{rest}"
    if s.startswith("/home/") or s.startswith("/root/"):
        posix = s.replace("/", "\\")
        return f"\\\\wsl.localhost\\{WSL_DISTRO}{posix}"
    if s.startswith("/") and not s.startswith("//"):
        raise ValueError(
            "out_dir looks like a Linux path. Use UNC "
            rf"\\\\wsl.localhost\\{WSL_DISTRO}\\home\\archer\\... "
            "or /mnt/c/.... Bare /home/archer writes to C:\\home\\archer."
        )
    return s


def wsl_path(raw: str | None) -> str | None:
    """Windows or UNC path -> Linux path for WSL readers."""
    if not raw:
        return None
    s = str(raw).replace("/", "\\")
    low = s.lower()
    for prefix in _UNC_PREFIXES:
        if low.startswith(prefix.lower()):
            rest = s[len(prefix):].replace("\\", "/")
            parts = rest.split("/", 1)
            if len(parts) == 2 and parts[1]:
                return "/" + parts[1]
            return None
    if len(s) >= 2 and s[1] == ":":
        return "/mnt/" + s[0].lower() + s[2:].replace("\\", "/")
    return None


def attach_wsl(payload: dict, *keys: str) -> dict:
    for k in keys:
        v = payload.get(k)
        wp = wsl_path(v)
        if wp:
            payload[f"{k}_wsl"] = wp
    return payload
