# Cortex MCP Apps E2E Spec

Date: 2026-05-16 America/Vancouver
Target: `https://pogo-galley-turbofan.ngrok-free.dev/mcp`
Server: `cortex@0.1.0`
Transport: streamable HTTP / SSE

## Scope

This spec covers **MCP Apps UI behavior**:

- whether a tool result points to a UI resource
- whether the resource can be fetched
- whether the host can render the UI
- whether the user can see the expected content
- whether the user can interact with the UI
- whether UI-originated host/tool messages work
- whether UI state, errors, and completion signals are observable

This spec is intentionally separate from vanilla MCP protocol/tool testing in `specs/cortex-vanilla-mcp-e2e.spec`.

## Current Evidence From Rehearsal

The live Rehearsal rollout confirmed only that Codex invoked a UI-producing tool:

- `mcp: cortex/views_generate_sentence_scramble started`
- `mcp: cortex/views_generate_sentence_scramble (completed)`

The rollout did **not** prove that the user saw or interacted with the UI.

Missing from the transcript/logs:

- `_meta.viewUUID`
- `_meta.ui.resourceUri`
- rendered iframe state
- DOM snapshot
- screenshot
- click/typing/reorder/submit events
- widget console errors
- host bridge messages
- widget-originated tool calls
- completion state from the exercise UI

Observed user-emulator behavior:

The user emulator reacted to leaked stderr/debug text, not to a rendered sentence-scramble UI. It asked:

> What is all that “stderr” and plugin warning text?

Conclusion:

The current Codex-based Rehearsal runner can test whether an agent chooses and calls MCP tools. It cannot, by itself, verify that an MCP App UI is rendered correctly or that a user can complete the app interaction.

## MCP App Capability Summary

The server exposes UI resources such as:

- `ui://flashcards-set/v1.html`
- `ui://sentence-scramble/v1.html`
- `ui://multiple-choice-question/v1.html`
- `ui://writing-task/v1.html`
- `ui://fill-in-blank-set/v1.html`
- `ui://initial-setup/v1.html`
- `ui://reading-practice/v1.html`
- `ui://listening-practice/v1.html`
- `ui://speaking-practice/v1.html`
- `ui://vocab-practice/v1.html`
- `ui://mock-exam/v1.html`

The tool metadata commonly includes `_meta.ui.resourceUri`, which is the signal that a compatible host should render an app surface.

## App-Specific Product Findings

### P1: Current E2E run does not validate UI visibility

The assistant said a sentence-scramble activity was started, but the test did not show whether a learner could actually see the sentence-scramble app.

Expected:

An MCP Apps E2E test should prove that the expected widget appears to the user with the correct prompt/content.

Impact:

A test can pass even if the app resource is missing, blank, visually broken, blocked by CSP, or not mounted by the host.

### P1: Current E2E run does not validate UI interaction

No test step filled, clicked, reordered, submitted, revealed, rated, or otherwise interacted with the generated app.

Expected:

An MCP Apps E2E test should verify that a learner can complete the primary interaction for each widget type.

Impact:

The backend/tool call path may work while the actual exercise is unusable.

### P1: Current E2E run does not validate host bridge behavior

The test did not capture widget-to-host messages such as:

- initialize/initialized
- size changed
- call server tool
- send follow-up message
- open link
- display mode request
- teardown request

Expected:

An MCP Apps E2E test should observe the host bridge contract and record which messages were sent, accepted, rejected, or failed.

Impact:

Widgets that rely on host APIs may silently fail after rendering.

### P2: Listening/speaking resources likely need CSP/resource-domain validation

`views_create_listening_practice` accepts an arbitrary `audio_url`, and `views_create_speaking_practice` accepts an optional `image_url`, but the read `ui://listening-practice/v1.html` resource returned:

- `csp.connectDomains: []`
- `csp.resourceDomains: []`

Expected:

The app test should verify whether remote media can load under the resource CSP. If remote media is intentionally restricted, the tool schema and descriptions should make that clear.

Impact:

Listening/speaking widgets may render but fail to load their actual media.

### P2: UI state persistence needs validation

The bundled widget code appears to use per-view state keyed by `viewUUID`.

Expected:

An app test should verify:

- initial state on first render
- state after interaction
- state after reload/reopen
- isolation between multiple widget instances

Impact:

Learners may see stale state, cross-exercise contamination, or lost progress.

### P2: Widget-originated feedback needs validation

Several widgets are expected to call `learning_record_feedback` after user feedback or score submission.

Expected:

An app test should verify that:

- feedback controls appear at the correct time
- score is passed when required
- `too_easy` behavior includes score corroboration
- tool-call errors are visible and recoverable

Impact:

Difficulty adaptation may not work even if the exercise UI appears to function.

## Missing Rehearsal Capabilities For MCP Apps

### P1: Needs an MCP Apps-compatible host layer

Rehearsal needs a host layer that can receive a tool result with a UI resource, fetch the resource, and expose the host APIs expected by the app.

Open design questions:

- Should this host layer be embedded in Rehearsal or run as a separate service?
- Should it emulate the MCP Apps standard only, ChatGPT Apps extensions only, or both?
- How should it handle app resources that depend on host-specific APIs?
- How should it persist per-widget state across turns?

### P1: Needs UI interaction traces

Rehearsal needs to log user-visible UI behavior, not only model text.

Required evidence:

- rendered content snapshot
- visual artifact or equivalent render proof
- interaction events
- state changes
- console/runtime errors
- failed network/media loads
- host bridge messages
- widget-triggered tool calls

### P1: Needs a user-emulator-to-UI action contract

The user emulator should not only write chat messages. It also needs a way to express UI intent.

Examples:

- complete this sentence scramble correctly
- choose the second multiple-choice answer
- reveal the transcript
- type this writing response
- submit the exercise
- mark the session too hard

Open design questions:

- Should the emulator output structured UI intents?
- Should a separate UI executor translate intents into low-level actions?
- Should failures be reported back to the emulator for recovery?
- How should the emulator decide when the UI has satisfied the goal?

### P2: Needs app-aware assertions

A generic “widget rendered” check is not enough. Each learning view needs domain-specific assertions.

Examples:

- sentence scramble: all elements visible, reorder works, correct order can be submitted/checked
- fill-in-blank: blanks visible, answers can be entered, hints work
- reading/listening: questions visible, scoring works, feedback appears
- speaking: prep/record/review flow can progress, transcript editor works
- onboarding: profile choices can be submitted, follow-up message/tool calls happen

### P2: Needs resource and CSP diagnostics

The app test should capture:

- resource fetch result
- MIME type
- `_meta.ui` metadata
- CSP metadata
- blocked domains
- failed images/audio/scripts/styles
- missing host capabilities

### P2: Needs structured report sections for apps

The app report should separate:

- model/tool-call transcript
- tool result payload
- UI resource metadata
- host bridge transcript
- user interaction transcript
- visual/render artifacts
- app console/network errors
- final app state

## Recommended Next Work

1. Define the MCP Apps host contract Rehearsal wants to support.
2. Define a structured UI intent schema for the user emulator.
3. Add app-specific assertions for Cortex widgets.
4. Decide how Rehearsal should capture render proof and interaction traces.
5. Re-run the sentence-scramble flow only after the app host layer can prove visibility and interaction.
