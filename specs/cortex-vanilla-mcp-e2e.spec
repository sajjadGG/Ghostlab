# Cortex Vanilla MCP E2E Spec

Date: 2026-05-16 America/Vancouver
Target: `https://pogo-galley-turbofan.ngrok-free.dev/mcp`
Server: `cortex@0.1.0`
Transport: streamable HTTP / SSE

## Scope

This spec covers **vanilla MCP behavior only**:

- protocol reachability
- tool/resource discovery
- tool selection by a coding agent
- tool call success/failure
- structured learner state consistency
- textual assistant/user transcript quality

This spec does **not** claim that MCP App UIs rendered or were interactively tested. That is covered separately in `specs/cortex-mcp-apps-e2e.spec`.

## Test Assets Added

- `targets/cortex-ngrok.json`
- `scenarios/cortex-onboarding-status.json`
- `scenarios/cortex-practice-generation.json`
- `runners/codex-cortex-aut.json`
- `runners/codex-user-emulator.json`

## Live Rehearsal Run

Run report:

`runs/2026-05-17T044627.105453Z-cortex-language-learning-ngrok-cortex-onboarding-status/report.md`

Event log:

`runs/2026-05-17T044627.105453Z-cortex-language-learning-ngrok-cortex-onboarding-status/events.jsonl`

Scenario:

`cortex-onboarding-status`

User goal:

> I just opened the app. Am I already set up? If not, set me up for IELTS: I am Persian-speaking, studying English, around B1, and I can study 30 minutes per day. Then give me one short activity to start.

Observed assistant result:

- The agent called `memory_get` and `student_get_status`.
- It detected inconsistent exam state.
- It attempted `student_complete_onboarding`; first attempt failed because the level payload shape was not accepted.
- It retried using `student_complete_onboarding`, `student_set_level`, `student_update_goals`, and `views_generate_sentence_scramble`.
- It produced textual confirmation that a sentence scramble activity was started.

## Capability Summary

The server exposes:

- Learning activity tools: flashcards, fill-in-blank, multiple-choice, sentence scramble, writing task, reading, listening, speaking, vocab, mock exam, initial setup.
- Learner state tools: `student_update_profile`, `student_set_level`, `student_update_goals`, `student_get_status`, `student_complete_onboarding`.
- Placement tools: `placement_create_test`, `placement_submit_test`.
- Memory tools: `memory_get`, `memory_put`.
- UI resources for learning widgets.

Notably absent:

- Tool descriptions reference `kb_find`, `kb_read`, and `kb_read_skill`, but those tools are not currently exposed by `tools/list`.

## Product Findings

### P1: `student_get_status` and `memory_get` disagree about the active exam goal

After the live run normalized the learner to IELTS, `memory_get` reported:

- `profile.exam`: `ielts`
- `targetLanguage`: `en`
- `nativeLanguage`: `fa`
- `dailyMinutes`: `30`
- `levelEstimate`: `B1`
- recent `goals.updated` archived old goal `0bbed903-23ab-4eb8-8d24-5fd19afcd7ce` and upserted a new IELTS goal.

But `student_get_status` still reported:

- `goals[0].description`: `CELPIP`
- same old goal id: `0bbed903-23ab-4eb8-8d24-5fd19afcd7ce`

Expected:

`student_get_status` should source the same active goal/profile state as `memory_get`, or clearly distinguish stale historical goals from the current goal.

Impact:

The assistant can tell the learner contradictory exam state. In the run, Codex noticed the inconsistency and tried to normalize it, but the status endpoint still surfaced stale CELPIP state afterward.

### P1: `student_get_status` treats tool telemetry as learner mistakes

`student_get_status` returned:

- headline: `Today: memory get`
- rationale: `You've hit "memory_get" 3 times this week.`
- recent mistakes with taxonomies such as `memory_get`, `views_create_initial_setup`, `student.complete_onboarding`.

Expected:

Learner-facing status should only summarize learning performance, mistakes, skills, streaks, and next activities. Internal tool invocation telemetry should not appear as “mistakes.”

Impact:

This confuses both the LLM and the learner. It may cause the assistant to recommend practice based on backend implementation events rather than language-learning needs.

### P2: Tool descriptions reference missing KB tools

Several view tools say the typical flow is to call `kb_find`, `kb_read`, or `kb_read_skill`, but those tools were not exposed by `tools/list`.

Expected:

Either expose the KB tools, or revise descriptions to say the assistant should generate suitable content when KB retrieval is unavailable.

Impact:

Models may try to call nonexistent tools or incorrectly claim they used course KB content.

### P2: `student_complete_onboarding` level handling is confusing

The live Codex agent first attempted to complete onboarding with a skill-level/CEFR shape that the tool rejected. It recovered by calling `student_set_level`.

Expected:

The onboarding API should make the valid level flow obvious. Options:

- Accept `level: "B1"` directly in `student_complete_onboarding`.
- Document that CEFR must be set only through `student_set_level`.
- Return a structured validation error that says which follow-up tool to call.

Impact:

Models can waste a turn, expose a failed tool attempt, or leave setup partially completed.

### P3: `views_create_initial_setup` current profile can disagree with memory

Before the live run, `memory_get` reported an IELTS learner, while `views_create_initial_setup` returned `current_profile.examType: "CELPIP"`.

Expected:

The setup wizard data should reflect the same canonical profile state as `memory_get`.

Impact:

The learner may reopen onboarding and see a different exam than their saved profile.

## Rehearsal Findings For Vanilla MCP

### P1: Process runner mixes stderr into assistant/user transcripts

Codex emitted plugin/cache warnings on stderr. Rehearsal appended stderr to the assistant output, so the user emulator saw the warnings as if they were part of the assistant response and asked:

> What is all that “stderr” and plugin warning text?

Expected:

Rehearsal should log stdout and stderr separately. Only stdout should be passed to the other agent as the conversational message unless a scenario explicitly enables stderr injection.

### P1: Need an interactive/session runner

The current process runner starts a fresh Codex process per turn. The live run used a lot of tokens and repeated startup/plugin warning noise.

Expected:

Add a long-lived runner that can keep one Codex/Claude session open across turns.

### P2: Need first-class Codex/Claude runner adapters

The initial Codex runner failed twice because option placement differed from the expected CLI shape:

- `codex exec --ask-for-approval never ...` failed.
- `codex exec -a never ...` failed.
- `codex -a never ... exec ...` worked.

Expected:

Rehearsal should ship version-aware runner presets or validation commands for Codex and Claude Code.

### P2: Need automatic MCP discovery/probe mode

Manual curl probes were needed to initialize the MCP server, list tools/resources, call representative tools, and identify missing KB tools.

Expected:

Add a `rehearsal inspect --target ...` command that captures:

- initialize result
- tools list
- resources list
- prompts list
- representative schema summary
- suspicious description references to unavailable tools

### P2: Need structured MCP call capture from agent runs

The Codex output showed tool names but not full arguments or structured results in Rehearsal's own event schema.

Expected:

Rehearsal should parse Codex JSONL mode or MCP host logs so reports include:

- tool name
- arguments
- result/error
- latency
- whether the call was visible to the user

### P3: Need run report cleanup/summarization

The report became noisy because Codex startup warnings and Cloudflare HTML were included in transcript text.

Expected:

Add output filters/redactors for known host noise, with raw logs preserved separately.

## Recommended Next Work

1. Fix Cortex profile/status source-of-truth mismatch.
2. Remove internal tool telemetry from learner-facing `student_get_status`.
3. Expose or remove references to KB tools.
4. Add Rehearsal stderr separation before more agent runs.
5. Add Rehearsal MCP inspect command and structured MCP call capture.
6. Re-run `cortex-practice-generation` after output cleanup.

