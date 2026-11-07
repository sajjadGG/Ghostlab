"""Evaluate MCP servers end to end, as a Ghostlab pipeline.

    describe   an agent with the MCP attached explores it and reports what it does
    scenarios  a planner turns that description into realistic user goals (fan-out)
    evaluate   the agent under test talks to an emulated user, then a judge scores it
    report     every verdict is gathered into one Markdown report

Run from the repository root (re-running the same command resumes):

    ghostlab run examples/mcp-eval/pipeline.py --input examples/mcp-eval/targets.jsonl

Swap any agent without touching the code:

    ghostlab run examples/mcp-eval/pipeline.py --param assistant=agents/codex.yaml \
        --param scenarios=5
"""
from __future__ import annotations

import json
from pathlib import Path

from ghostlab import Agent, Pipeline, stage

HERE = Path(__file__).parent

DESCRIBE = """\
You have an MCP server named "{name}" attached. Explore it: list its tools and,
where a tool is clearly read-only, call it once to learn what it returns.
Do not call tools that change state.

Reply with JSON only:
{{"summary": "...", "tools": [{{"name": "...", "purpose": "...", "risk": "read-only|mutating|destructive"}}]}}
"""

PLAN = """\
Here is what an MCP server named "{name}" can do:

{capabilities}

Write {count} realistic, varied goals a user might bring to an assistant that
has this server. Include at least one goal the server cannot fully satisfy.

Reply with JSON only:
{{"scenarios": [{{"id": "short-slug", "persona": "...", "goal": "...",
  "opening_message": "...", "success_criteria": ["..."]}}]}}
"""

USER = """\
You are role-playing a user talking to an AI assistant. Stay in character.

Persona: {persona}
Your goal: {goal}

Each message you receive is the assistant's latest reply. Answer the way this
user would, in one or two sentences. When your goal is met, or clearly cannot
be met, reply with exactly DONE.
"""

JUDGE = """\
Judge whether the assistant achieved the user's goal.

Goal: {goal}
Success criteria: {criteria}
Tool calls made: {tools}

Transcript:
{transcript}

Reply with JSON only:
{{"pass": true|false, "score": 0-10, "reasons": ["..."]}}
"""


@stage
def describe(ctx, target):
    explorer = ctx.agent("assistant").with_mcp(target["id"], target["mcp"])
    capabilities = ctx.run(explorer, DESCRIBE.format(name=target["id"])).json()
    return {**target, "capabilities": capabilities}


@stage(fan_out=True)
def scenarios(ctx, target):
    prompt = PLAN.format(name=target["id"], count=ctx.params["scenarios"],
                         capabilities=json.dumps(target["capabilities"], indent=2))
    plan = ctx.run(ctx.agent("helper"), prompt).json()
    return [{"id": s["id"], "target": target, "scenario": s}
            for s in plan["scenarios"][: ctx.params["scenarios"]]]


@stage(concurrency=4, retries=1)
def evaluate(ctx, case):
    target, scenario = case["target"], case["scenario"]
    assistant = ctx.agent("assistant").with_mcp(target["id"], target["mcp"])
    user = ctx.agent("helper").with_(name="user", instructions=USER.format(**scenario))
    transcript, tool_calls = [], []
    message = scenario["opening_message"]
    with ctx.session(assistant) as aut, ctx.session(user) as emulator:
        for _ in range(ctx.params["max_turns"]):
            transcript.append(f"USER: {message}")
            reply = aut.send(message).check()
            transcript.append(f"ASSISTANT: {reply.answer}")
            tool_calls += [f"{c['server']}/{c['tool']} ({c['status']})" for c in reply.tool_calls]
            message = emulator.send(reply.answer or "(no reply)").check().answer.strip()
            if message.upper().startswith("DONE"):
                break
    verdict = ctx.run(ctx.agent("helper"), JUDGE.format(
        goal=scenario["goal"], criteria="; ".join(scenario["success_criteria"]),
        tools=", ".join(tool_calls) or "none", transcript="\n\n".join(transcript),
    )).json()
    return {"target": target["id"], "scenario": scenario["id"], "goal": scenario["goal"],
            "verdict": verdict, "tool_calls": tool_calls, "transcript": transcript}


@stage(gather=True)
def report(ctx, results):
    passed = [r for r in results if r["verdict"].get("pass")]
    lines = [f"# MCP evaluation: {len(passed)}/{len(results)} scenarios passed", ""]
    for r in results:
        mark = "PASS" if r["verdict"].get("pass") else "FAIL"
        lines.append(f"## {mark} {r['target']} / {r['scenario']} "
                     f"(score {r['verdict'].get('score', '?')})")
        lines += [f"Goal: {r['goal']}", ""] + [f"- {why}" for why in r["verdict"].get("reasons", [])]
        lines.append("")
    path = ctx.dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"scenarios": len(results), "passed": len(passed), "report": str(path)}


pipeline = Pipeline(
    "mcp-eval", [describe, scenarios, evaluate, report],
    params={
        "assistant": Agent.load(HERE / "agents" / "assistant.yaml"),
        "helper": Agent.load(HERE / "agents" / "helper.yaml"),
        "scenarios": 3,
        "max_turns": 4,
    },
)
