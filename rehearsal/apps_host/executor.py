"""Translate structured UI intents into low-level widget actions.

The user emulator emits :class:`~rehearsal.mcp_apps.UiIntent`s (``reorder`` /
``choose`` / ``type`` / ``reveal`` / ``submit`` / ``rate`` / ``mark``). This
module maps each intent to an ordered list of :class:`Action`s — generic,
text-based operations a browser driver can execute against the rendered widget.
Kept browser-free so the mapping is unit-testable; :mod:`renderer` runs the plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..mcp_apps import UiIntent

# Common control labels widgets use, in priority order.
_SUBMIT_LABELS = ("Check", "Submit", "Done", "Continue")
_REVEAL_LABELS = ("Reveal answer", "Reveal", "Show answer")


@dataclass
class Action:
    """One low-level widget operation."""

    op: str                         # "click_text" | "fill" | "click"
    text: str = ""                  # visible label (click_text) or value (fill)
    selector: str = ""             # CSS selector (click / fill target)
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"op": self.op}
        for key in ("text", "selector", "note"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out


@dataclass
class IntentPlan:
    """The action plan for a single UI intent."""

    intent: UiIntent
    actions: list[Action] = field(default_factory=list)
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "intent": self.intent.to_json(),
            "actions": [a.to_json() for a in self.actions],
            "error": self.error,
        }


def plan_intent(intent: UiIntent) -> IntentPlan:
    """Map one UI intent to an ordered action plan."""
    plan = IntentPlan(intent=intent)
    t = intent.type
    if t == "choose":
        label = intent.target or (intent.value if isinstance(intent.value, str) else "")
        if not label:
            plan.error = "choose intent needs a target label"
        else:
            plan.actions.append(Action(op="click_text", text=str(label)))
    elif t == "reorder":
        # Tap-to-build widgets: click each element in the desired order.
        order = intent.value
        if not isinstance(order, list) or not order:
            plan.error = "reorder intent needs a list value of element labels"
        else:
            for element in order:
                plan.actions.append(Action(op="click_text", text=str(element)))
    elif t == "type":
        if intent.value is None:
            plan.error = "type intent needs a value"
        else:
            selector = intent.target or "textarea, input[type='text']"
            plan.actions.append(Action(op="fill", selector=selector, text=str(intent.value)))
    elif t == "reveal":
        plan.actions.append(_click_one_of(_REVEAL_LABELS, intent.target))
    elif t == "submit":
        plan.actions.append(_click_one_of(_SUBMIT_LABELS, intent.target))
    elif t == "rate":
        label = intent.target or (str(intent.value) if intent.value is not None else "")
        if not label:
            plan.error = "rate intent needs a target label or value"
        else:
            plan.actions.append(Action(op="click_text", text=label))
    elif t == "mark":
        label = intent.target or (str(intent.value) if intent.value is not None else "")
        if not label:
            plan.error = "mark intent needs a difficulty label (e.g. too_hard)"
        else:
            plan.actions.append(Action(op="click_text", text=label))
    else:  # pragma: no cover - UiIntent validation prevents this
        plan.error = "unsupported intent type %r" % t
    return plan


def _click_one_of(labels: tuple, override: Any) -> Action:
    """Click an explicit override label, else the first known control label.

    The driver resolves the first label that exists; the remaining labels ride
    along as a fallback note so a single rename doesn't break the plan.
    """
    if override:
        return Action(op="click_text", text=str(override))
    return Action(op="click_text", text=labels[0], note="fallbacks: " + ", ".join(labels[1:]))


def plan_intents(intents: list[UiIntent]) -> list[IntentPlan]:
    """Map a sequence of intents to their action plans."""
    return [plan_intent(intent) for intent in intents]
