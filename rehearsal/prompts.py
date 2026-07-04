from __future__ import annotations

import json
from typing import Any

from .config import PersonaConfig, ScenarioConfig, TargetConfig
from .types import TranscriptTurn


def format_transcript(transcript: list[TranscriptTurn]) -> str:
    if not transcript:
        return "(no previous turns)"
    return "\n".join(f"{turn.role.upper()}: {turn.content}" for turn in transcript)


def _truncate(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_widgets(widgets: list[dict[str, Any]]) -> str:
    """Turn extracted UI widgets into a first-person 'what you were shown' note.

    The emulator is text-only and never sees the rendered widget, so without this
    it can't know it was just handed (say) a timed-essay form. We describe the
    widget as the user's own screen so they respond by *doing the exercise*
    (writing the essay, choosing the answer), not by narrating the test.
    """
    if not widgets:
        return ""
    lines = [
        "A UI panel just appeared on your screen — the assistant opened an "
        "interactive exercise/widget for you to use. As the user, you can read "
        "and fill it in. Here is what it shows:",
    ]
    for widget in widgets:
        label = widget.get("tool", "widget")
        lines.append(f"\n[{label}]")
        text = widget.get("text")
        if text:
            lines.append(_truncate(text, 500))
        fields = widget.get("fields") or {}
        for key, value in fields.items():
            lines.append(f"- {key}: {_truncate(value, 300)}")
    lines.append(
        "\nRespond the way a real person would when this panel is in front of "
        "them: actually do the exercise (e.g. write the essay, pick the option, "
        "type the answer) in your reply. Do not describe the widget or talk about "
        "the test — just use it."
    )
    return "\n".join(lines)


def compose_persona(scenario: ScenarioConfig, persona: PersonaConfig | None) -> str:
    """Build the persona block for the user-emulator prompt.

    When a reusable persona is supplied it is composed from its summary, traits,
    and domain context; otherwise we fall back to the scenario's inline persona
    string for backward compatibility.
    """
    if persona is None:
        return scenario.persona

    lines = [persona.summary]
    if persona.traits:
        lines.append("Behavioral traits: " + ", ".join(persona.traits))
    if persona.context:
        details = "; ".join(f"{key}: {value}" for key, value in persona.context.items())
        lines.append(f"Context: {details}")
    # The scenario's own persona note, if any, refines the reusable persona.
    if scenario.persona:
        lines.append(f"In this scenario specifically: {scenario.persona}")
    return "\n".join(lines)


def build_aut_prompt(
    target: TargetConfig,
    scenario: ScenarioConfig,
    transcript: list[TranscriptTurn],
    user_message: str,
    mcp_config_path: str,
) -> str:
    return f"""You are the agent-under-test in an MCP end-to-end test.

Target MCP app:
- id: {target.id}
- transport: {target.transport}
- expected capabilities: {target.capabilities or "unspecified"}
- generated MCP client config: {mcp_config_path}

Scenario:
- id: {scenario.id}
- title: {scenario.title}
- user goal: {scenario.goal}

Previous transcript:
{format_transcript(transcript)}

The user now says:
{user_message}

Respond as the real assistant that has access to the configured MCP app. The MCP app is the product the user is actually using — it is the system under test, so prefer its tools to accomplish the goal. When the app offers a capability for what the user wants — creating a practice exercise or interactive widget, starting a lesson, recording a result or score, saving feedback, reading the learner's state — call that tool rather than doing the work yourself in plain prose. Only answer inline when no app tool fits. Use the tools through your coding-agent environment. Be concise but complete."""


def build_user_emulator_prompt(
    scenario: ScenarioConfig,
    transcript: list[TranscriptTurn],
    last_assistant_message: str,
    persona: PersonaConfig | None = None,
    widgets: list[dict[str, Any]] | None = None,
) -> str:
    widget_note = describe_widgets(widgets or [])
    widget_section = f"\n\n{widget_note}\n" if widget_note else ""
    return f"""You ARE this person, chatting with an AI assistant. You are not a tester, a QA engineer, or an evaluator — you are a real human with your own goal, mood, and way of talking.

Who you are:
{compose_persona(scenario, persona)}

What you actually want out of this conversation (your private motivation — never recite it back verbatim):
{scenario.goal}
{widget_section}
The conversation so far:
{format_transcript(transcript)}

The assistant just said to you:
{last_assistant_message}

Now write your next message as this person. Rules for staying human:
- Talk like a real user texting an assistant, in YOUR persona's voice and mood. Let your traits show (impatient people are terse and a little blunt; anxious people hedge; non-native speakers use simpler or slightly-off phrasing).
- React to what the assistant just said — answer its questions, give it what it asked for, push back if you're unsatisfied.
- Do NOT give meta/QA-style instructions ("say exactly why", "mark difficulty as X", "confirm whether you logged it"). A real person doesn't audit the assistant's internals; they just react to the result they got.
- Pursue your goal naturally over the conversation; don't dump every requirement at once or number your demands like a spec.
- Keep it to what a person would actually type — usually 1-4 sentences. Vary length turn to turn.
- Never mention that this is a test, a rehearsal, a simulation, or that you are role-playing.

Write ONLY your next message, nothing else. If your goal is genuinely met, or the conversation clearly cannot progress any further, write exactly REHEARSAL_DONE instead of a message."""
