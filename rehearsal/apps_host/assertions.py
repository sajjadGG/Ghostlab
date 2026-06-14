"""App-aware assertions for rendered MCP Apps widgets.

A generic "widget rendered" check is not enough (spec P2). Each widget type gets
domain-specific assertions evaluated against a **render summary** — the
browser-free dict the renderer produces:

    {
      "handshake_completed": bool,
      "console_errors": [str, ...],
      "body_text": str,
      "interactive_count": int,
    }

Assertions are pure predicates over that summary, so they unit-test without a
browser and the renderer just feeds it real data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class AppAssertion:
    name: str
    description: str
    check: Callable[[dict], bool]

    def evaluate(self, summary: dict) -> dict[str, Any]:
        try:
            passed = bool(self.check(summary))
        except Exception as exc:  # a malformed summary fails the assertion, not the run
            return {"name": self.name, "passed": False, "description": self.description, "error": str(exc)}
        return {"name": self.name, "passed": passed, "description": self.description}


def _body_contains(*needles: str) -> Callable[[dict], bool]:
    lowered = [n.lower() for n in needles]
    return lambda s: any(n in (s.get("body_text") or "").lower() for n in lowered)


# Generic assertions every widget should satisfy.
GENERIC_ASSERTIONS = [
    AppAssertion(
        "handshake_completed",
        "Widget ran the ui/initialize handshake and the host delivered tool data.",
        lambda s: bool(s.get("handshake_completed")),
    ),
    AppAssertion(
        "body_rendered",
        "Widget rendered visible (non-empty) body content.",
        lambda s: bool((s.get("body_text") or "").strip()),
    ),
    AppAssertion(
        "no_console_errors",
        "Widget produced no console errors while rendering.",
        lambda s: not s.get("console_errors"),
    ),
    AppAssertion(
        "has_interactive_elements",
        "Widget exposed at least one interactive control.",
        lambda s: int(s.get("interactive_count") or 0) > 0,
    ),
]

# Per-widget-type extras, keyed by the `ui://<slug>/...` slug.
_WIDGET_ASSERTIONS: dict[str, list[AppAssertion]] = {
    "sentence-scramble": [
        AppAssertion("scramble_prompt_visible", "Shows the sentence-scramble prompt.",
                     _body_contains("scramble", "reorder", "sentence")),
        AppAssertion("reveal_control_present", "Offers a reveal/check control.",
                     _body_contains("reveal", "check")),
    ],
    "multiple-choice-question": [
        AppAssertion("question_visible", "Shows a question or choices.",
                     _body_contains("question", "choose", "answer")),
    ],
    "fill-in-blank-set": [
        AppAssertion("blanks_visible", "Shows fill-in-the-blank content.",
                     _body_contains("blank", "fill")),
    ],
    "flashcards-set": [
        AppAssertion("cards_visible", "Shows flashcard content.",
                     _body_contains("card", "flip", "front", "back")),
    ],
}


def widget_slug(uri: str) -> str:
    """Extract the widget slug from a `ui://<slug>/v1.html` resource URI."""
    m = re.match(r"ui://([^/]+)/", uri or "")
    return m.group(1) if m else ""


def assertions_for(uri: str) -> list[AppAssertion]:
    """Generic assertions plus any specific to this widget type."""
    return list(GENERIC_ASSERTIONS) + list(_WIDGET_ASSERTIONS.get(widget_slug(uri), []))


def evaluate_assertions(assertions: list[AppAssertion], summary: dict) -> list[dict[str, Any]]:
    return [a.evaluate(summary) for a in assertions]
