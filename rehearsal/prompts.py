from __future__ import annotations

from .config import ScenarioConfig, TargetConfig
from .types import TranscriptTurn


def format_transcript(transcript: list[TranscriptTurn]) -> str:
    if not transcript:
        return "(no previous turns)"
    return "\n".join(f"{turn.role.upper()}: {turn.content}" for turn in transcript)


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

Respond as the real assistant that has access to the configured MCP app. If you need the MCP tools, use them through your coding-agent environment. Be concise but complete."""


def build_user_emulator_prompt(
    scenario: ScenarioConfig,
    transcript: list[TranscriptTurn],
    last_assistant_message: str,
) -> str:
    return f"""You are role-playing a realistic user for an MCP app test.

Persona:
{scenario.persona}

Goal:
{scenario.goal}

Success criteria:
{chr(10).join(f"- {item}" for item in scenario.success_criteria) or "- unspecified"}

Failure signals to probe for:
{chr(10).join(f"- {item}" for item in scenario.failure_signals) or "- unspecified"}

Previous transcript:
{format_transcript(transcript)}

The assistant-under-test just said:
{last_assistant_message}

Write only the next user message. Stay in character, pursue the goal, and stop by writing exactly REHEARSAL_DONE if the goal has been satisfied or the test can no longer progress."""
