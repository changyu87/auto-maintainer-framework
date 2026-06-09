"""rabbit_print.py — direct-call renderer for the [rabbit] print convention.

Callers supply text, icon, color, and format inline at every call site. No
registry, no message-id lookup, no named wrappers. The module is the SOLE
place that owns ANSI/brand/bar composition for [rabbit] lines and the SOLE
place that owns the leading newline (via rabbit_block). It returns strings
only — never writes to stdout/stderr.

Public API:
    rabbit_print(text, icon, color, format="compact") -> str
        format="banner"  -> brand icon ━━━ text ━━━ icon
        format="compact" -> brand icon text
    rabbit_subline(text, color="green", icon=None) -> str
        With icon: brand icon text. Without: brand text.
    rabbit_block(*lines) -> str
        '\\n' + '\\n'.join(lines). The SINGLE authoritative location for
        the leading newline that lifts [rabbit] output onto its own row.

Allowed colors: green, red, yellow. Unknown colors raise KeyError.

Version: 2.0.0
Owner: rabbit-workflow team
Deprecation criterion: when the [rabbit] print convention is replaced by a
    structured logging facility
"""

BRAND = "[\U0001f407 rabbit \U0001f407]"
BAR = "━━━"

_COLORS = {
    "green":  ("[32m", "[0m"),
    "red":    ("[31m", "[0m"),
    "yellow": ("[33m", "[0m"),
}

__all__ = ["rabbit_print", "rabbit_subline", "rabbit_block"]


def rabbit_print(text, icon, color, format="compact"):
    ansi, reset = _COLORS[color]
    if format == "banner":
        return f"{ansi}{BRAND} {icon} {BAR} {text} {BAR} {icon}{reset}"
    return f"{ansi}{BRAND} {icon} {text}{reset}"


def rabbit_subline(text, color="green", icon=None):
    ansi, reset = _COLORS[color]
    if icon:
        return f"{ansi}{BRAND} {icon} {text}{reset}"
    return f"{ansi}{BRAND} {text}{reset}"


def rabbit_block(*lines):
    return "\n" + "\n".join(lines)
