"""Minimal, dependency-free ANSI coloring for CLI output.

Semantic (dual-agent) runs are the flagship view, and a wall of same-colored
text makes it hard to tell the emulated user apart from the agent-under-test at
a glance. This module gives the runner a small, role-aware palette.

Coloring is disabled automatically when stdout is not a TTY, when ``NO_COLOR``
is set (https://no-color.org/), or when ``GHOSTLAB_COLOR=0``. Set
``GHOSTLAB_COLOR=1`` to force it on (e.g. when piping into a pager that groks
ANSI).
"""
from __future__ import annotations

import os
import sys

_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "italic": "3",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "bright_black": "90",
    "bright_red": "91",
    "bright_green": "92",
    "bright_yellow": "93",
    "bright_blue": "94",
    "bright_magenta": "95",
    "bright_cyan": "96",
}


def color_enabled() -> bool:
    force = os.environ.get("GHOSTLAB_COLOR")
    if force is not None:
        return force not in ("0", "", "false", "no")
    if os.environ.get("NO_COLOR") is not None:
        return False
    return sys.stdout.isatty()


def colorize(text: str, *styles: str) -> str:
    """Wrap ``text`` in the given styles (names from ``_CODES``).

    A no-op when coloring is disabled, so callers can wrap unconditionally.
    """
    if not styles or not color_enabled():
        return text
    codes = ";".join(_CODES[s] for s in styles if s in _CODES)
    if not codes:
        return text
    return f"\033[{codes}m{text}\033[0m"


# Role-aware helpers so call sites read intent, not raw color names. Kept in one
# place so the semantic transcript stays visually consistent across the CLI.
def user(text: str) -> str:
    return colorize(text, "bright_cyan")


def assistant(text: str) -> str:
    return colorize(text, "bright_green")


def tool(text: str) -> str:
    return colorize(text, "yellow")


def widget(text: str) -> str:
    return colorize(text, "bright_magenta")


def muted(text: str) -> str:
    return colorize(text, "dim")


def heading(text: str) -> str:
    return colorize(text, "bold")


def verdict(text: str, status: str) -> str:
    """Color a judge/status token by outcome."""
    palette = {
        "pass": ("bright_green",),
        "partial": ("yellow",),
        "fail": ("bright_red",),
        "error": ("bright_red", "bold"),
        "skip": ("dim",),
    }
    return colorize(text, *palette.get(status, ("white",)))
