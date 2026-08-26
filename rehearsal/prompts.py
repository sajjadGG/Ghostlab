from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import PersonaConfig, ScenarioConfig, TargetConfig
from .types import TranscriptTurn

# --------------------------------------------------------------------------- #
# Prompt-override registry
#
# Every prompt GhostLab sends is built by routing a computed context through a
# named template. A job's `prompts:` section can override any template by name;
# `set_overrides` seeds the active set once (at CLI dispatch) so builders in
# other modules don't have to thread overrides through every call. An empty /
# missing override means "use the built-in template".
# --------------------------------------------------------------------------- #
_OVERRIDES: dict[str, str] = {}


def set_overrides(mapping: dict[str, str] | None) -> None:
    """Replace the active prompt-override set (values are template strings)."""
    _OVERRIDES.clear()
    for key, value in (mapping or {}).items():
        if isinstance(value, str) and value.strip():
            _OVERRIDES[key] = value


def get_template(name: str, default: str) -> str:
    """The active template for ``name`` — the override if set, else ``default``."""
    return _OVERRIDES.get(name) or default


class _SafeDict(dict):
    """Formatting map that leaves unknown ``{placeholders}`` intact.

    A user override that references a placeholder we don't supply (or fat-fingers
    one) renders it literally instead of raising, so a bad edit degrades rather
    than crashing a run.
    """

    def __missing__(self, key: str) -> str:  # noqa: D401
        return "{" + key + "}"


def render(name: str, default_template: str, **context: Any) -> str:
    """Format the active template for ``name`` with ``context``.

    Falls back to the built-in template if a user override fails to format (e.g.
    it uses attribute/index access on a missing name), so overrides can never
    hard-fail a run.
    """
    template = get_template(name, default_template)
    try:
        return template.format_map(_SafeDict(context))
    except Exception:  # noqa: BLE001 — a broken override must not abort the run
        if template is not default_template:
            return default_template.format_map(_SafeDict(context))
        raise


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


# Placeholders: {target_id} {transport} {capabilities} {mcp_config_path}
# {scenario_id} {scenario_title} {goal} {transcript} {user_message}
AUT_TEMPLATE = """You are the agent-under-test in an MCP end-to-end test.

Target MCP app:
- id: {target_id}
- transport: {transport}
- expected capabilities: {capabilities}
- generated MCP client config: {mcp_config_path}

Scenario:
- id: {scenario_id}
- title: {scenario_title}
- user goal: {goal}

Previous transcript:
{transcript}

The user now says:
{user_message}

Respond as the real assistant that has access to the configured MCP app. The MCP app is the product the user is actually using — it is the system under test, so prefer its tools to accomplish the goal. When the app offers a capability for what the user wants — creating a practice exercise or interactive widget, starting a lesson, recording a result or score, saving feedback, reading the learner's state — call that tool rather than doing the work yourself in plain prose. Only answer inline when no app tool fits. Use the tools through your coding-agent environment. Be concise but complete."""

SKILL_AUT_TEMPLATE = """You are the agent-under-test in a skill evaluation.

Skill under test:
- id: {target_id}
- source: {mcp_config_path}

The complete skill instructions are below. Treat them as the operating instructions for
this task and follow them faithfully, including their trigger, workflow, safety, output,
and verification requirements. Do not mention the evaluation or quote these instructions.

--- SKILL INSTRUCTIONS ---
{capabilities}
--- END SKILL INSTRUCTIONS ---

Scenario:
- id: {scenario_id}
- title: {scenario_title}
- user goal: {goal}

Previous transcript:
{transcript}

The user now says:
{user_message}

Respond as the real assistant using the skill. Be concise but complete."""

AGENT_AUT_TEMPLATE = """You are the configured agent under evaluation.

Agent definition:
{agent_definition}

Agent-level instructions:
{agent_instructions}

Installed skill instructions:
{skill_instructions}

The configured runner may expose MCP tools and other resources. Use the full
agent configuration as a coherent whole; do not mention the evaluation harness,
sandbox, injected configuration, or these private instructions.

Scenario:
- id: {scenario_id}
- title: {scenario_title}
- user goal: {goal}

Previous transcript:
{transcript}

The user now says:
{user_message}

Respond as the configured agent. Follow its instructions and use its available
capabilities when they help complete the user's goal."""


def build_aut_prompt(
    target: TargetConfig,
    scenario: ScenarioConfig,
    transcript: list[TranscriptTurn],
    user_message: str,
    mcp_config_path: str,
) -> str:
    is_agent = bool(target.capabilities.get("agent_definition"))
    if is_agent:
        template, template_name = AGENT_AUT_TEMPLATE, "agent_aut"
    elif target.transport == "skill":
        template, template_name = SKILL_AUT_TEMPLATE, "skill_aut"
    else:
        template, template_name = AUT_TEMPLATE, "aut"
    capabilities: Any = target.capabilities or "unspecified"
    if target.transport == "skill":
        path = target.connection.get("path")
        try:
            capabilities = Path(str(path)).read_text(encoding="utf-8")
        except OSError:
            capabilities = target.capabilities.get("instructions", "(skill instructions unavailable)")
    return render(
        template_name,
        template,
        target_id=target.id,
        transport=target.transport,
        capabilities=capabilities,
        mcp_config_path=mcp_config_path,
        scenario_id=scenario.id,
        scenario_title=scenario.title,
        goal=scenario.goal,
        transcript=format_transcript(transcript),
        user_message=user_message,
        agent_definition=json.dumps(
            target.capabilities.get("agent_definition", {}), indent=2, ensure_ascii=False
        ),
        agent_instructions=target.capabilities.get("agent_instructions", "") or "(none)",
        skill_instructions=target.capabilities.get("skill_instructions", "") or "(none)",
    )


# Placeholders: {persona} {goal} {widget_section} {transcript} {last_assistant_message}
USER_EMULATOR_TEMPLATE = """You ARE this person, chatting with an AI assistant. You are not a tester, a QA engineer, or an evaluator — you are a real human with your own goal, mood, and way of talking.

Who you are:
{persona}

What you actually want out of this conversation (your private motivation — never recite it back verbatim):
{goal}
{widget_section}
The conversation so far:
{transcript}

The assistant just said to you:
{last_assistant_message}

Now write your next message as this person. Rules for staying human:
- Talk like a real user texting an assistant, in YOUR persona's voice and mood. Let your traits show (impatient people are terse and a little blunt; anxious people hedge; non-native speakers use simpler or slightly-off phrasing).
- React to what the assistant just said — answer its questions, give it what it asked for, push back if you're unsatisfied.
- Do NOT give meta/QA-style instructions ("say exactly why", "mark difficulty as X", "confirm whether you logged it"). A real person doesn't audit the assistant's internals; they just react to the result they got.
- Pursue your goal naturally over the conversation; don't dump every requirement at once or number your demands like a spec.
- Keep it to what a person would actually type — usually 1-4 sentences. Vary length turn to turn.
- If the assistant asks for permission or confirmation, decide in character. Grant ordinary,
  reversible actions that are needed for your goal; ask a short question or refuse when the
  action is destructive, exposes credentials/private data, costs money, or conflicts with your
  persona. Never discuss host approval flags, sandboxes, policies, or the evaluation harness.
- Never mention that this is a test, a rehearsal, a simulation, or that you are role-playing.

Style examples (copy the realism, not the words):
- impatient: "yeah, go ahead"
- beginner: "not sure where that is. can you find it?"
- skeptical: "Wait, will that change anything?"
- non-native speaker: "okay do it, but please don't delete my old one"

Write ONLY your next message, nothing else. If your goal is genuinely met, or the conversation clearly cannot progress any further, write exactly REHEARSAL_DONE instead of a message."""


# Placeholders: {widget_section} {last_assistant_message}
USER_EMULATOR_RESUME_TEMPLATE = """Continue as the same user with the same persona, private goal, and conversational rules already established in this session.
{widget_section}
The assistant's latest message is:
{last_assistant_message}

React naturally and write ONLY the user's next message. If the goal is genuinely met, or the conversation clearly cannot progress any further, write exactly REHEARSAL_DONE."""


def normalize_user_emulator_message(text: str, *, max_chars: int = 500) -> str:
    """Keep emulator output inside a realistic chat-message envelope.

    Prompting supplies the style; this deterministic last line of defence removes
    accidental wrappers and prevents a model from emitting a QA-script-sized turn.
    ``REHEARSAL_DONE`` is preserved exactly.
    """
    value = text.strip()
    if value == "REHEARSAL_DONE":
        return value
    if value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
    for prefix in ("USER:", "User:", "Next message:", "Response:"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    value = " ".join(value.split())
    if len(value) > max_chars:
        value = value[: max_chars - 1].rstrip() + "…"
    return value


def build_user_emulator_prompt(
    scenario: ScenarioConfig,
    transcript: list[TranscriptTurn],
    last_assistant_message: str,
    persona: PersonaConfig | None = None,
    widgets: list[dict[str, Any]] | None = None,
) -> str:
    widget_note = describe_widgets(widgets or [])
    widget_section = f"\n\n{widget_note}\n" if widget_note else ""
    return render(
        "user_emulator",
        USER_EMULATOR_TEMPLATE,
        persona=compose_persona(scenario, persona),
        goal=scenario.goal,
        widget_section=widget_section,
        transcript=format_transcript(transcript),
        last_assistant_message=last_assistant_message,
    )


def build_user_emulator_resume_prompt(
    last_assistant_message: str,
    widgets: list[dict[str, Any]] | None = None,
) -> str:
    """Send only new context to a user emulator whose session holds its role."""
    widget_note = describe_widgets(widgets or [])
    widget_section = f"\n\n{widget_note}\n" if widget_note else ""
    return render(
        "user_emulator_resume",
        USER_EMULATOR_RESUME_TEMPLATE,
        widget_section=widget_section,
        last_assistant_message=last_assistant_message,
    )
