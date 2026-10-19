"""Key names for act --key / --hold-key: Windows virtual-key codes, aliases, validation.

No Win32 imports, so the CLI help and the MCP tool description can list the names.
"""
from __future__ import annotations

import re

VK = {
    "enter": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "backspace": 0x08,
    "space": 0x20,
    "delete": 0x2E,
    "ctrl": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "v": 0x56,
}

for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    VK.setdefault(_c, 0x41 + _i)
for _i in range(10):
    VK.setdefault(str(_i), 0x30 + _i)
    VK.setdefault(f"num{_i}", 0x60 + _i)
for _i in range(1, 25):
    VK.setdefault(f"f{_i}", 0x70 + _i - 1)
VK.update({
    "win": 0x5B,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "insert": 0x2D,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "printscreen": 0x2C,
    "pause": 0x13,
    "menu": 0x5D,
    "semicolon": 0xBA,
    "equals": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "slash": 0xBF,
    "backquote": 0xC0,
    "bracketleft": 0xDB,
    "backslash": 0xDC,
    "bracketright": 0xDD,
    "quote": 0xDE,
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
})

# Spellings models reach for; "_", "-" and spaces are dropped first (page_down -> pagedown).
KEY_ALIASES = {
    "return": "enter",
    "escape": "esc",
    "control": "ctrl",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "pgdown": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "bksp": "backspace",
    "windows": "win",
    "meta": "win",
    "super": "win",
    "cmd": "win",
    "apps": "menu",
    "contextmenu": "menu",
    "prtsc": "printscreen",
    "printscr": "printscreen",
    "caps": "capslock",
    "equal": "equals",
    "dash": "minus",
    "dot": "period",
    "grave": "backquote",
    "backtick": "backquote",
    "apostrophe": "quote",
    "lbracket": "bracketleft",
    "rbracket": "bracketright",
}


class UnknownKeyError(ValueError, KeyError):
    """A key name act/tap_key can't send. The message lists the valid names."""

    def __init__(self, name: str, spec: str | None = None):
        self.name = name
        self.spec = spec
        where = f" in {spec!r}" if spec and spec != name else ""
        super().__init__(
            f"unknown key {name!r}{where}. Valid keys: {key_names_summary()}. "
            "Combine with + (ctrl+shift+t); separate several keys with commas (tab,enter)."
        )

    def __str__(self) -> str:
        return str(self.args[0])


def key_names() -> list[str]:
    """Canonical key names, in a stable order for help text and errors."""
    return sorted(VK, key=lambda k: (len(k) > 1, not k.isdigit(), k))


def key_names_summary() -> str:
    """key_names() with the letter/digit/F/numpad runs folded into ranges."""
    ranged = {*"abcdefghijklmnopqrstuvwxyz", *"0123456789", *(f"f{i}" for i in range(1, 25)), *(f"num{i}" for i in range(10))}
    named = [k for k in key_names() if k not in ranged]
    return "a-z, 0-9, f1-f24, num0-num9, " + ", ".join(named)


def normalize_key(name: str) -> str:
    """Canonical key name for name (case, '_'/'-'/space and aliases folded). Raises UnknownKeyError."""
    raw = str(name).strip().lower()
    k = raw if raw in VK else raw.replace("_", "").replace("-", "").replace(" ", "")
    k = KEY_ALIASES.get(k, k)
    if k not in VK:
        raise UnknownKeyError(str(name))
    return k


def check_key_spec(spec: str) -> str:
    """Normalize a key or combo like 'ctrl+Page_Down'. Raises UnknownKeyError naming the bad part."""
    parts = [p for p in str(spec).strip().split("+") if p.strip()]
    if not parts:
        raise UnknownKeyError(str(spec))
    out = []
    for p in parts:
        try:
            out.append(normalize_key(p))
        except UnknownKeyError:
            raise UnknownKeyError(p.strip(), str(spec)) from None
    return "+".join(out)


def split_key_list(raw: str) -> list[str]:
    """'ctrl + end, page down' -> ['ctrl+end', 'page down'] -> normalized by check_key_spec.

    Commas separate keys. Within one entry, spaces around '+' are dropped; an entry with
    spaces that is a single key once they're removed ('page down') stays one key;
    otherwise whitespace separates keys ('tab enter' -> tab, enter).
    """
    out: list[str] = []
    for chunk in str(raw).split(","):
        chunk = re.sub(r"\s*\+\s*", "+", chunk.strip())
        if not chunk:
            continue
        if " " not in chunk:
            out.append(chunk)
            continue
        try:
            check_key_spec(chunk.replace(" ", ""))
            out.append(chunk.replace(" ", ""))
        except UnknownKeyError:
            out.extend(p for p in chunk.split() if p)
    return out
