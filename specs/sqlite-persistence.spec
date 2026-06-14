# Ghostlab SQLite Persistence Spec

Date: 2026-06-06 America/Vancouver
Status: Proposed
Scope: MCP targets, inspections, generated test cases, runs, full traces, and judgments

## Goal

Ghostlab currently persists its state as inspect/profile JSON, dataset directories,
per-run `events.jsonl`, reports, and verdict files. Those artifacts are readable
and reproducible, but they make cross-run history, filtering, resumability, and
data integrity difficult.

This spec makes SQLite the searchable system of record while preserving the
existing files as portable exports.

The database must answer:

- Which MCPs and server versions have been tested?
- What did a specific inspection expose at that time?
- Which capability profile generated a persona, scenario, dataset, or case?
- Which exact persona + scenario case was run?
- Which exact prompts, models, messages, and tool calls occurred during a run?
- Where in the trace did a tool call, failure, or judgment originate?
- Why did a case pass, partially pass, or fail?
- Which external artifacts belong to an inspection, dataset, run, or judgment?

## Product Model

The primary lineage is:

```text
MCP target
  -> inspection snapshot
    -> capability profile
      -> dataset
        -> personas
        -> scenarios
        -> runnable cases (persona + scenario)
          -> run batch
            -> case run
              -> append-only trace events
              -> messages, prompts, and tool calls
              -> judgment
```

Definitions:

- **Target**: a reusable connection configuration for one MCP.
- **Inspection**: an immutable snapshot of what a target exposed at one moment.
- **Profile**: an immutable capability analysis derived from an inspection.
- **Persona**: the reusable identity and behavior of the emulated user.
- **Scenario**: the task, goal, criteria, failure signals, and expected tools.
- **Dataset**: a generated collection of personas, scenarios, and cases.
- **Case**: one persona paired with one scenario. This is the runnable preset.
- **Run batch**: one request to execute a selected set of cases.
- **Run**: one execution of one case against one target.
- **Trace event**: one immutable chronological fact from a run.
- **Judgment**: one evaluation attempt over a completed or failed run.

## Design Principles

### SQLite is the query source; files remain exports

- New writes go to SQLite first inside transactions.
- JSON, JSONL, and Markdown artifacts are generated only after the database
  transaction commits. Their `artifacts` rows are inserted only after the file
  is fully written and hashed.
- Failed exports are retriable and never roll back an otherwise valid database
  operation.
- Existing CLI file inputs remain supported during migration.
- Users can export a database entity back into the current directory layout.

### Normalize queryable identity; retain canonical JSON

Frequently filtered or joined fields use typed columns and foreign keys.
Flexible MCP schemas, prompts, arguments, results, profiles, and generated
objects also retain canonical JSON text.

This avoids two bad extremes:

- a single opaque JSON blob that cannot support useful history queries
- an excessively rigid schema that breaks whenever MCP metadata evolves

### Immutable snapshots and append-only traces

- Inspections, profiles, personas, scenarios, and completed run events are
  immutable.
- Editing a persona or scenario creates a new revision.
- Case curation status is mutable and auditable.
- Run events are append-only and ordered by a per-run sequence number.
- Judging a run again creates another judgment; it does not overwrite history.

### Stable internal IDs, readable public IDs

Each entity has:

- an internal SQLite integer primary key for efficient joins
- a stable public text ID for CLI/UI/export use
- a human-readable slug or name where useful

Use UUIDv7 or another sortable globally unique string for new public IDs. Keep
existing readable IDs as `slug` or `external_id`.

### Reproducibility over convenience

Every run must point to immutable snapshots of:

- target connection metadata, with secrets redacted
- inspection/profile lineage
- case, persona revision, and scenario revision
- runner configuration and selected models
- exact runtime prompts
- chronological events and tool-call payloads
- judgment prompt, model, and result

## Database Location and Configuration

Default database:

```text
<workspace>/ghostlab.sqlite3
```

Recommended connection initialization:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA temp_store = MEMORY;
```

Use one short-lived connection per CLI operation. Streamlit may keep a
connection factory, but must not share one connection concurrently across
threads.

Timestamps are UTC ISO-8601 text with microseconds. JSON is canonical UTF-8 text
with sorted keys when hashing.

## Core Schema

The following DDL is the proposed v1 schema. Names may change slightly during
implementation, but the relationships and ownership boundaries should remain.

### Schema migrations

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
```

Migrations are ordered SQL files shipped in `rehearsal/storage/migrations/`.
Startup applies pending migrations inside an exclusive transaction.

### Targets and target revisions

```sql
CREATE TABLE targets (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT,
    created_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE target_revisions (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    target_id INTEGER NOT NULL REFERENCES targets(id),
    revision INTEGER NOT NULL,
    transport TEXT NOT NULL,
    connection_json TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    startup_json TEXT NOT NULL DEFAULT '{}',
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(target_id, revision),
    UNIQUE(target_id, content_sha256)
);
```

`connection_json` must be redacted before persistence. Secret header values,
tokens, passwords, and sensitive environment values must never be stored in
plain text. Store references such as `${ENV_VAR}` when a reproducible pointer is
needed.

### Inspection snapshots

```sql
CREATE TABLE inspections (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    target_revision_id INTEGER NOT NULL REFERENCES target_revisions(id),
    server_name TEXT,
    server_version TEXT,
    protocol_version TEXT,
    instructions TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL,
    tool_count INTEGER NOT NULL DEFAULT 0,
    resource_count INTEGER NOT NULL DEFAULT 0,
    resource_template_count INTEGER NOT NULL DEFAULT 0,
    prompt_count INTEGER NOT NULL DEFAULT 0,
    lint_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    error_text TEXT
);

CREATE TABLE inspection_tools (
    id INTEGER PRIMARY KEY,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    name TEXT NOT NULL,
    title TEXT,
    description TEXT,
    input_schema_json TEXT NOT NULL DEFAULT '{}',
    output_schema_json TEXT,
    annotations_json TEXT NOT NULL DEFAULT '{}',
    meta_json TEXT NOT NULL DEFAULT '{}',
    ui_resource_uri TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE(inspection_id, name)
);

CREATE TABLE inspection_resources (
    id INTEGER PRIMARY KEY,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('resource', 'resource_template')),
    ordinal INTEGER NOT NULL,
    uri TEXT NOT NULL,
    name TEXT,
    description TEXT,
    mime_type TEXT,
    raw_json TEXT NOT NULL,
    UNIQUE(inspection_id, kind, uri)
);

CREATE TABLE inspection_prompts (
    id INTEGER PRIMARY KEY,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    arguments_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL,
    UNIQUE(inspection_id, name)
);

CREATE TABLE inspection_findings (
    id INTEGER PRIMARY KEY,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    referenced TEXT,
    source TEXT,
    detail TEXT,
    raw_json TEXT NOT NULL
);
```

An inspection is immutable once completed. Re-inspecting the same target creates
a new row so server changes can be diffed.

### Capability profiles

```sql
CREATE TABLE capability_profiles (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    inspection_id INTEGER NOT NULL REFERENCES inspections(id),
    model TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    domain_summary TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(inspection_id, content_sha256)
);

CREATE TABLE profile_tool_categories (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES capability_profiles(id) ON DELETE CASCADE,
    category_key TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT,
    tool_names_json TEXT NOT NULL,
    UNIQUE(profile_id, category_key)
);

CREATE TABLE profile_workflows (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES capability_profiles(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    steps_json TEXT NOT NULL
);
```

The full profile JSON remains canonical; category/workflow tables support
coverage and UI queries.

### Datasets, personas, scenarios, and cases

```sql
CREATE TABLE datasets (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    profile_id INTEGER NOT NULL REFERENCES capability_profiles(id),
    name TEXT NOT NULL,
    seed INTEGER NOT NULL,
    model TEXT NOT NULL,
    generation_prompt_json TEXT NOT NULL,
    requested_personas INTEGER NOT NULL,
    requested_scenarios_per_persona INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('generating', 'ready', 'failed', 'archived')),
    created_at TEXT NOT NULL,
    finished_at TEXT,
    error_text TEXT
);

CREATE TABLE personas (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    traits_json TEXT NOT NULL DEFAULT '[]',
    context_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id, external_id, revision)
);

CREATE TABLE scenarios (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL,
    intent TEXT,
    situation TEXT,
    goal TEXT NOT NULL,
    opening_message TEXT NOT NULL,
    max_turns INTEGER NOT NULL,
    success_criteria_json TEXT NOT NULL DEFAULT '[]',
    failure_signals_json TEXT NOT NULL DEFAULT '[]',
    exercises_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id, external_id, revision)
);

CREATE TABLE cases (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    persona_id INTEGER NOT NULL REFERENCES personas(id),
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id),
    ordinal INTEGER NOT NULL,
    intent TEXT,
    max_turns INTEGER NOT NULL,
    expected_tools_json TEXT NOT NULL DEFAULT '[]',
    curation_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(curation_status IN ('pending', 'approved', 'rejected', 'needs-edit')),
    curation_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(dataset_id, external_id)
);

CREATE TABLE case_curation_events (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    old_status TEXT,
    new_status TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE dataset_findings (
    id INTEGER PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    case_id INTEGER REFERENCES cases(id),
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    detail TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

A dataset owns its generated persona/scenario revisions. Reusable global persona
libraries can be added later, but v1 should preserve current dataset-scoped
behavior and lineage.

### Run batches and runs

```sql
CREATE TABLE run_batches (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    dataset_id INTEGER REFERENCES datasets(id),
    target_revision_id INTEGER NOT NULL REFERENCES target_revisions(id),
    profile_id INTEGER REFERENCES capability_profiles(id),
    selected_case_count INTEGER NOT NULL,
    options_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'partial', 'failed', 'cancelled')),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    batch_id INTEGER REFERENCES run_batches(id),
    case_id INTEGER REFERENCES cases(id),
    target_revision_id INTEGER NOT NULL REFERENCES target_revisions(id),
    inspection_id INTEGER REFERENCES inspections(id),
    profile_id INTEGER REFERENCES capability_profiles(id),
    persona_snapshot_json TEXT,
    scenario_snapshot_json TEXT NOT NULL,
    target_snapshot_json TEXT NOT NULL,
    aut_runner_json TEXT NOT NULL,
    user_runner_json TEXT NOT NULL,
    aut_model TEXT NOT NULL,
    user_model TEXT NOT NULL,
    max_turns INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'running', 'completed', 'max_turns_reached',
        'aut_failed', 'user_emulator_failed', 'cancelled'
    )),
    turns_completed INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error_text TEXT,
    created_at TEXT NOT NULL
);
```

Snapshot JSON on `runs` intentionally duplicates immutable case inputs. This
guarantees that a run remains reproducible even if a dataset is later archived
or imported incorrectly.

### Full chronological trace

`run_events` is the canonical append-only trace ledger.

```sql
CREATE TABLE run_events (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    turn_index INTEGER,
    actor TEXT CHECK(actor IN ('system', 'user_emulator', 'agent_under_test', 'tool', 'judge')),
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);
```

Required v1 event types:

```text
run_started
user_message
aut_prompt
aut_result
user_emulator_prompt
user_emulator_result
tool_call_started          future when streaming supports it
tool_call_finished         future when streaming supports it
run_finished
evaluation_started
evaluation_finished
run_failed
```

The existing JSONL event format maps directly into this table. `sequence` is
assigned transactionally and is the authoritative order; timestamps are
informational and may collide.

The following projection tables make common trace queries efficient. They are
written in the same transaction as their source event.

```sql
CREATE TABLE run_prompts (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL UNIQUE REFERENCES run_events(id) ON DELETE CASCADE,
    turn_index INTEGER,
    actor TEXT NOT NULL CHECK(actor IN ('agent_under_test', 'user_emulator', 'judge')),
    model TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    stateful_resume INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE run_messages (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL UNIQUE REFERENCES run_events(id) ON DELETE CASCADE,
    turn_index INTEGER NOT NULL,
    actor TEXT NOT NULL CHECK(actor IN ('user_emulator', 'agent_under_test')),
    content TEXT NOT NULL,
    exit_code INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE tool_calls (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES run_events(id) ON DELETE CASCADE,
    assistant_message_id INTEGER REFERENCES run_messages(id),
    turn_index INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    server_name TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('started', 'completed', 'failed', 'unknown')),
    arguments_json TEXT,
    result_json TEXT,
    error_json TEXT,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE(run_id, turn_index, ordinal)
);
```

Tool calls must reference the assistant message/turn that caused them. This is
the relationship required by the chronological trace viewer.

### Judgments and criterion evidence

```sql
CREATE TABLE judgments (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    model TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    run_status TEXT NOT NULL,
    verdict TEXT CHECK(verdict IN ('pass', 'partial', 'fail')),
    judge_verdict TEXT CHECK(judge_verdict IN ('pass', 'partial', 'fail')),
    summary TEXT,
    gates_json TEXT NOT NULL DEFAULT '[]',
    deterministic_json TEXT NOT NULL DEFAULT '{}',
    hallucinated_tools_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_text TEXT,
    UNIQUE(run_id, attempt)
);

CREATE TABLE judgment_items (
    id INTEGER PRIMARY KEY,
    judgment_id INTEGER NOT NULL REFERENCES judgments(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('success_criterion', 'failure_signal')),
    item_index INTEGER NOT NULL,
    expectation_text TEXT,
    outcome INTEGER NOT NULL,
    evidence_text TEXT NOT NULL,
    UNIQUE(judgment_id, kind, item_index)
);

CREATE TABLE judgment_evidence_links (
    id INTEGER PRIMARY KEY,
    judgment_item_id INTEGER NOT NULL REFERENCES judgment_items(id) ON DELETE CASCADE,
    run_event_id INTEGER REFERENCES run_events(id),
    message_id INTEGER REFERENCES run_messages(id),
    tool_call_id INTEGER REFERENCES tool_calls(id),
    confidence REAL,
    reason TEXT,
    CHECK (
        (run_event_id IS NOT NULL) +
        (message_id IS NOT NULL) +
        (tool_call_id IS NOT NULL) = 1
    )
);
```

`judgment_evidence_links` should eventually be populated by the judge as
structured evidence IDs. Until then, Ghostlab may store heuristic links with a
lower confidence value.

### Artifacts and exports

```sql
CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    owner_type TEXT NOT NULL CHECK(owner_type IN (
        'inspection', 'profile', 'dataset', 'case', 'run_batch', 'run', 'judgment'
    )),
    owner_public_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    relative_path TEXT,
    mime_type TEXT,
    byte_size INTEGER,
    sha256 TEXT,
    storage TEXT NOT NULL CHECK(storage IN ('filesystem', 'inline')),
    content_blob BLOB,
    created_at TEXT NOT NULL,
    UNIQUE(owner_type, owner_public_id, kind, relative_path)
);
```

Large or user-facing artifacts stay on disk and are indexed here. Small
generated text may be stored inline. v1 should not duplicate large files into
SQLite by default.

Examples:

- inspection: `inspect.json`, `inspect.md`
- profile: `capabilities.json`, `capabilities.md`
- dataset: `dataset.json`, persona/scenario JSON exports, review reports
- run: `events.jsonl`, `report.md`, `target.mcp.json`
- judgment: `verdict.json`, `verdict.md`
- future MCP Apps: screenshots, DOM snapshots, console/network logs

Artifact export protocol:

1. Commit the owning entity or event transaction.
2. Write the export to a temporary file in the destination directory.
3. Flush and atomically rename the temporary file to its final path.
4. Calculate its hash and insert or update the `artifacts` index row.
5. If any export step fails, retain the database state and retry later.

`owner_public_id` is a deliberate polymorphic reference. SQLite cannot enforce
its foreign key, so repository methods must verify that the matching
`owner_type` entity exists before inserting an artifact.

## Recommended Indexes

```sql
CREATE INDEX idx_inspections_target_time
    ON inspections(target_revision_id, started_at DESC);
CREATE INDEX idx_tools_name
    ON inspection_tools(name);
CREATE INDEX idx_datasets_profile_time
    ON datasets(profile_id, created_at DESC);
CREATE INDEX idx_cases_dataset_status
    ON cases(dataset_id, curation_status, ordinal);
CREATE INDEX idx_batches_dataset_time
    ON run_batches(dataset_id, created_at DESC);
CREATE INDEX idx_runs_case_time
    ON runs(case_id, created_at DESC);
CREATE INDEX idx_runs_target_status_time
    ON runs(target_revision_id, status, created_at DESC);
CREATE INDEX idx_events_run_sequence
    ON run_events(run_id, sequence);
CREATE INDEX idx_messages_run_turn
    ON run_messages(run_id, turn_index, actor);
CREATE INDEX idx_tool_calls_run_turn
    ON tool_calls(run_id, turn_index, ordinal);
CREATE INDEX idx_tool_calls_name_status
    ON tool_calls(tool_name, status);
CREATE INDEX idx_judgments_run_attempt
    ON judgments(run_id, attempt DESC);
CREATE INDEX idx_judgments_verdict_time
    ON judgments(verdict, finished_at DESC);
```

## Write Lifecycles

### Inspect MCP

1. Upsert `targets` by slug.
2. Create or reuse a content-addressed `target_revisions` row.
3. Insert `inspections(status='running')`.
4. Connect and inspect.
5. In one transaction:
   - insert tools, resources, prompts, and findings
   - save raw canonical JSON
   - mark inspection completed
6. Export `inspect.json` and `inspect.md`, then index them in `artifacts`.
7. On failure, keep the inspection row and mark it failed with `error_text`.

### Analyze capabilities

1. Insert the exact model prompt/model result as a new immutable profile.
2. Insert category and workflow projections.
3. Export profile artifacts and index them.

### Build dataset

1. Insert `datasets(status='generating')`.
2. Generate personas and scenarios.
3. In one transaction:
   - insert immutable persona/scenario revisions
   - insert ordered cases
   - insert quality findings
   - mark dataset ready
4. Export the current directory format.

If generation fails, retain the failed dataset row and error. Partial generated
objects may remain for debugging but must not be runnable until the dataset is
ready.

### Curate cases

Each status change updates `cases.curation_status` and appends a
`case_curation_events` row in one transaction.

### Run selected cases

1. Insert a `run_batches` row and one queued `runs` row per selected case.
2. For each run:
   - mark running and write `run_started`
   - append each event with the next sequence number
   - write prompt/message/tool projections in the same transaction
   - update summary counters on `runs`
   - mark completed/failed and write `run_finished` or `run_failed`
3. Generate filesystem artifacts and index them.
4. Mark the batch completed, partial, failed, or cancelled.

Use small per-event transactions so live traces remain visible while a long run
is still executing.

### Judge a run

1. Insert `judgments(status='running', attempt=N)` and append
   `evaluation_started`.
2. Run deterministic checks and the judge.
3. In one transaction:
   - update judgment result
   - insert judgment items and evidence links
   - append `evaluation_finished`
4. Export and index verdict artifacts.

## Read Models

The UI and CLI should query SQLite instead of scanning directories.

Recommended repository methods:

```text
list_targets()
list_inspections(target_id)
get_inspection(inspection_id)
get_profile(profile_id)
list_datasets(profile_id, status)
get_dataset_cases(dataset_id, curation_status)
create_run_batch(dataset_id, case_ids, options)
list_runs(filters)
get_run_trace(run_id)
get_latest_judgment(run_id)
list_artifacts(owner_type, owner_id)
```

`get_run_trace()` should return ordered messages with inline tool calls by
joining `run_messages` and `tool_calls`, while `run_events` remains available
for raw debugging and future event types.

## JSON and File Compatibility

### Import

Add:

```bash
ghostlab db init
ghostlab db import-workspace ghostlab_workspace/
ghostlab db verify
```

Import rules:

- Use existing IDs/slugs as `external_id`.
- Generate new internal/public IDs.
- Hash canonical JSON for idempotency.
- Import `events.jsonl` in file order as run event sequence.
- Derive messages/tool calls/prompts/judgments from events and verdict files.
- Record every imported source file in `artifacts`.
- Re-running import must not duplicate entities.
- Malformed or incomplete directories produce an import finding, not a crash of
  the entire migration.

### Export

Add:

```bash
ghostlab db export-inspection <id> --out ...
ghostlab db export-dataset <id> --out ...
ghostlab db export-run <id> --out ...
ghostlab db export-workspace --out ...
```

Exports preserve the current layouts and JSON shapes so old tooling remains
usable.

## Security and Privacy

- Never persist secret connection headers, access tokens, API keys, passwords,
  or sensitive environment values.
- Apply redaction before values reach both SQLite and artifact files.
- Treat prompts, transcripts, tool arguments/results, persona context, and
  screenshots as potentially sensitive.
- Support configurable retention by owner type and age.
- Support deleting one run, one dataset, or one target lineage with a preview of
  dependent records/artifacts.
- Database backups inherit workspace sensitivity and must not be uploaded by
  default.

## Integrity and Recovery

- Enable foreign keys on every connection.
- Run `PRAGMA integrity_check` through `ghostlab db verify`.
- Use content hashes for immutable snapshots and artifacts.
- Keep failed/running rows after crashes; mark abandoned work as failed during
  recovery rather than deleting it.
- A repair command may rebuild projection tables from canonical `run_events`.
- Filesystem artifacts can be regenerated when their database source exists.
- Database rows can be re-imported from artifacts when the database is lost,
  except for fields that older artifacts never captured.

## Rollout Plan

### Phase 1: Storage foundation

- Add `rehearsal/storage/` using only the Python standard-library `sqlite3`.
- Add migrations, connection setup, repositories, and schema tests.
- Add `ghostlab db init`, `db verify`, and workspace import.
- Continue filesystem-first writes while shadow-writing SQLite.

Exit criteria:

- Existing workspace imports idempotently.
- SQLite history matches filesystem history.
- No CLI behavior changes.

### Phase 2: Runs and trace source of truth

- Write run batches, runs, events, prompts, messages, tool calls, and judgments
  directly to SQLite.
- Keep generating current run artifacts.
- Change Results/Trace Viewer to query SQLite.
- Add recovery for interrupted runs.

Exit criteria:

- Live trace reads committed events from SQLite.
- Filters no longer scan run directories.
- One run can be fully exported from SQLite.

### Phase 3: Inspect and dataset source of truth

- Write targets/revisions, inspections, profiles, datasets, personas, scenarios,
  cases, and findings directly to SQLite.
- Change UI/CLI listing and selection to use repositories.
- Keep file import/export compatibility.

Exit criteria:

- The full Inspect -> Cases -> Runs -> Results flow works after restarting with
  only the SQLite database and indexed external artifacts.

### Phase 4: Advanced history and maintenance

- Add inspection diffs, dataset/run comparisons, retention, backup, pruning,
  artifact repair, and optional global persona/scenario libraries.
- Add future MCP Apps trace entities only when their host-layer event contracts
  are defined.

## Testing Requirements

- Migration tests from an empty database through every schema version.
- Foreign-key and constraint tests.
- Import idempotency tests using existing workspace fixtures.
- Round-trip export/import tests for inspection, dataset, run, and judgment.
- Crash-recovery tests for interrupted inspection, generation, run, and judge.
- Projection rebuild tests from `run_events`.
- Concurrency tests for one Streamlit reader and one run writer under WAL.
- Redaction tests proving secrets never reach SQLite or exported artifacts.
- Query tests for target/status/verdict/date/tool filters.

## Acceptance Criteria

- SQLite can represent the complete lineage from target revision through
  judgment without relying on directory names.
- A full chronological trace can be reconstructed with each tool call attached
  to the assistant turn that made it.
- Exact runtime prompts and models are queryable for every new run.
- Multiple inspections, dataset generations, run attempts, and judgment attempts
  are retained rather than overwritten.
- Existing filesystem workspaces import idempotently.
- Existing JSON/JSONL/Markdown layouts can be exported.
- UI run-history filters query SQLite rather than globbing directories.
- Secrets are redacted before persistence.
- The database can recover useful state after an interrupted operation.
