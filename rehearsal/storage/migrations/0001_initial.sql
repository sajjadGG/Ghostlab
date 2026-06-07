-- Ghostlab SQLite persistence — v1 initial schema.
--
-- SQLite is the system of record for new writes; the existing .md/.json/.jsonl
-- artifacts keep being written as exports and are indexed by path in `artifacts`.
-- Large canonical JSON (inspect raw, profile raw, event payloads) is stored
-- inline as TEXT. Frequently filtered/joined identity uses typed columns.
--
-- Conventions: integer PK `id`; stable `public_id TEXT UNIQUE` for UI/CLI/export;
-- `*_json TEXT` for canonical JSON; `content_sha256` for immutable-snapshot dedup;
-- UTC ISO-8601 text timestamps. Foreign keys are enforced per connection.

-- --------------------------------------------------------------------------- --
-- Targets and content-addressed connection revisions
-- --------------------------------------------------------------------------- --
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
    connection_json TEXT NOT NULL,           -- redacted before persistence
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    startup_json TEXT NOT NULL DEFAULT '{}',
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(target_id, revision),
    UNIQUE(target_id, content_sha256)
);

-- --------------------------------------------------------------------------- --
-- Inspection snapshots (each inspection = a per-target version v1..vN)
-- --------------------------------------------------------------------------- --
CREATE TABLE inspections (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    target_revision_id INTEGER NOT NULL REFERENCES target_revisions(id),
    version INTEGER NOT NULL,                 -- per-target ordinal (v1, v2, ...)
    server_name TEXT,
    server_version TEXT,
    protocol_version TEXT,
    instructions TEXT,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL,
    tool_count INTEGER NOT NULL DEFAULT 0,
    resource_count INTEGER NOT NULL DEFAULT 0,
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

-- --------------------------------------------------------------------------- --
-- Capability profiles (full profile JSON is canonical in raw_json)
-- --------------------------------------------------------------------------- --
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

-- --------------------------------------------------------------------------- --
-- Datasets, personas, scenarios, cases
-- --------------------------------------------------------------------------- --
CREATE TABLE datasets (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    profile_id INTEGER REFERENCES capability_profiles(id),
    name TEXT NOT NULL,
    seed INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    requested_personas INTEGER NOT NULL DEFAULT 0,
    requested_scenarios_per_persona INTEGER NOT NULL DEFAULT 0,
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
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    traits_json TEXT NOT NULL DEFAULT '[]',
    context_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id, external_id)
);

CREATE TABLE scenarios (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    intent TEXT,
    situation TEXT,                           -- the scenario `persona` situational note
    goal TEXT NOT NULL,
    opening_message TEXT NOT NULL,
    max_turns INTEGER NOT NULL,
    success_criteria_json TEXT NOT NULL DEFAULT '[]',
    failure_signals_json TEXT NOT NULL DEFAULT '[]',
    exercises_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(dataset_id, external_id)
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

-- --------------------------------------------------------------------------- --
-- Run batches and runs (a run is self-contained via snapshot JSON)
-- --------------------------------------------------------------------------- --
CREATE TABLE run_batches (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    dataset_id INTEGER REFERENCES datasets(id),
    target_revision_id INTEGER NOT NULL REFERENCES target_revisions(id),
    profile_id INTEGER REFERENCES capability_profiles(id),
    selected_case_count INTEGER NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'running', 'completed', 'partial', 'failed', 'cancelled'
    )),
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
    aut_runner_json TEXT NOT NULL DEFAULT '{}',
    user_runner_json TEXT NOT NULL DEFAULT '{}',
    aut_model TEXT NOT NULL DEFAULT '',
    user_model TEXT NOT NULL DEFAULT '',
    max_turns INTEGER NOT NULL DEFAULT 0,
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

-- Canonical append-only trace ledger; `sequence` is the authoritative order.
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

-- The one projection kept in v1: powers tool-usage/failure queries across runs.
CREATE TABLE tool_calls (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES run_events(id) ON DELETE CASCADE,
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

-- --------------------------------------------------------------------------- --
-- Judgments (full judge output is canonical in raw_json)
-- --------------------------------------------------------------------------- --
CREATE TABLE judgments (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    attempt INTEGER NOT NULL,
    model TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    run_status TEXT NOT NULL,
    verdict TEXT CHECK(verdict IN ('pass', 'partial', 'fail')),
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

-- --------------------------------------------------------------------------- --
-- Artifacts: filesystem path index for the human-readable exports
-- --------------------------------------------------------------------------- --
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
    created_at TEXT NOT NULL,
    UNIQUE(owner_type, owner_public_id, kind, relative_path)
);

-- --------------------------------------------------------------------------- --
-- Indexes
-- --------------------------------------------------------------------------- --
CREATE INDEX idx_inspections_target_time
    ON inspections(target_revision_id, started_at DESC);
CREATE INDEX idx_tools_name
    ON inspection_tools(name);
CREATE INDEX idx_datasets_profile_time
    ON datasets(profile_id, created_at DESC);
CREATE INDEX idx_cases_dataset_status
    ON cases(dataset_id, curation_status, ordinal);
CREATE INDEX idx_runs_target_status_time
    ON runs(target_revision_id, status, created_at DESC);
CREATE INDEX idx_runs_case_time
    ON runs(case_id, created_at DESC);
CREATE INDEX idx_events_run_sequence
    ON run_events(run_id, sequence);
CREATE INDEX idx_tool_calls_run_turn
    ON tool_calls(run_id, turn_index, ordinal);
CREATE INDEX idx_tool_calls_name_status
    ON tool_calls(tool_name, status);
CREATE INDEX idx_judgments_run_attempt
    ON judgments(run_id, attempt DESC);
