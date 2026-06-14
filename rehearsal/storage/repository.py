"""GhostlabStore — the read/write repository over the SQLite schema.

Write methods persist each pipeline stage (inspect, profile, dataset, run,
judgment) as the system of record; the callers keep writing human-readable
artifacts and pass their paths to ``index_artifact``. Read methods back the UI's
per-MCP history and trace views.

Inputs are tolerant: dataclasses (``InspectResult``, ``TargetConfig``) or their
``asdict`` dicts are both accepted, since the CLI holds dataclasses and the
Streamlit UI holds dicts in session state.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..types import utc_now
from . import db as _db
from .hashing import content_sha256
from .ids import public_id
from .redact import redact_connection

JsonDict = Dict[str, Any]


def _as_dict(obj: Any) -> JsonDict:
    if obj is None:
        return {}
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, dict):
        return dict(obj)
    raise TypeError(f"expected dict or dataclass, got {type(obj)!r}")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=False)


def _actor_for(event_type: str) -> Optional[str]:
    if event_type in ("aut_prompt", "aut_result"):
        return "agent_under_test"
    if event_type in ("user_message", "user_emulator_prompt", "user_emulator_result"):
        return "user_emulator"
    if event_type in ("evaluation_started", "evaluation_finished"):
        return "judge"
    if event_type in ("run_started", "run_finished", "run_failed"):
        return "system"
    return None


class GhostlabStore:
    """A repository bound to one SQLite connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(
        cls,
        db: Optional[Union[str, Path]] = None,
        *,
        workspace: Optional[Union[str, Path]] = None,
    ) -> "GhostlabStore":
        return cls(_db.get_connection(db, workspace=workspace))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "GhostlabStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Targets / revisions (shared by inspect and run)
    # ------------------------------------------------------------------ #
    def _upsert_target(self, slug: str, display_name: Optional[str] = None) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM targets WHERE slug = ?", (slug,)).fetchone()
        if row is not None:
            return row
        pid = public_id("target")
        self.conn.execute(
            "INSERT INTO targets(public_id, slug, display_name, created_at) VALUES (?, ?, ?, ?)",
            (pid, slug, display_name or slug, utc_now()),
        )
        return self.conn.execute("SELECT * FROM targets WHERE slug = ?", (slug,)).fetchone()

    def _ensure_target_revision(self, target: Any) -> sqlite3.Row:
        """Create or reuse a content-addressed target revision (secrets redacted)."""
        target = _as_dict(target)
        slug = str(target.get("id") or target.get("slug") or "target")
        transport = str(target.get("transport", ""))
        connection = redact_connection(dict(target.get("connection", {})))
        capabilities = dict(target.get("capabilities", {}))
        startup = dict(target.get("startup", {}))
        content = {
            "transport": transport,
            "connection": connection,
            "capabilities": capabilities,
            "startup": startup,
        }
        sha = content_sha256(content)

        target_row = self._upsert_target(slug)
        target_id = target_row["id"]
        existing = self.conn.execute(
            "SELECT * FROM target_revisions WHERE target_id = ? AND content_sha256 = ?",
            (target_id, sha),
        ).fetchone()
        if existing is not None:
            return existing

        next_rev = self.conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM target_revisions WHERE target_id = ?",
            (target_id,),
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO target_revisions(
                public_id, target_id, revision, transport, connection_json,
                capabilities_json, startup_json, content_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id("target_revision"),
                target_id,
                next_rev,
                transport,
                _dumps(connection),
                _dumps(capabilities),
                _dumps(startup),
                sha,
                utc_now(),
            ),
        )
        return self.conn.execute(
            "SELECT * FROM target_revisions WHERE target_id = ? AND content_sha256 = ?",
            (target_id, sha),
        ).fetchone()

    # ------------------------------------------------------------------ #
    # Inspect
    # ------------------------------------------------------------------ #
    def record_inspection(
        self,
        target: Any,
        result: Any,
        *,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        status: str = "completed",
        error_text: Optional[str] = None,
    ) -> JsonDict:
        """Persist an inspection as a new per-target version. Returns id info."""
        result = _as_dict(result)
        now = utc_now()
        with _db.transaction(self.conn):
            revision = self._ensure_target_revision(target)
            target_id = revision["target_id"]
            version = self.conn.execute(
                """
                SELECT COUNT(*) FROM inspections i
                JOIN target_revisions r ON i.target_revision_id = r.id
                WHERE r.target_id = ?
                """,
                (target_id,),
            ).fetchone()[0] + 1

            server = result.get("server_info", {}) or {}
            tools = result.get("tools", []) or []
            resources = result.get("resources", []) or []
            templates = result.get("resource_templates", []) or []
            prompts = result.get("prompts", []) or []
            lint = result.get("lint", []) or []

            pid = public_id("inspection")
            cur = self.conn.execute(
                """
                INSERT INTO inspections(
                    public_id, target_revision_id, version, server_name, server_version,
                    protocol_version, instructions, capabilities_json, raw_json,
                    tool_count, resource_count, prompt_count, lint_count,
                    started_at, finished_at, status, error_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    revision["id"],
                    version,
                    server.get("name"),
                    server.get("version"),
                    server.get("protocolVersion"),
                    result.get("instructions", ""),
                    _dumps(result.get("capabilities", {})),
                    _dumps(result),
                    len(tools),
                    len(resources),
                    len(prompts),
                    len(lint),
                    started_at or now,
                    finished_at or now,
                    status,
                    error_text,
                ),
            )
            inspection_id = cur.lastrowid

            for ordinal, tool in enumerate(tools):
                meta = tool.get("_meta", {}) or {}
                ui_uri = (meta.get("ui", {}) or {}).get("resourceUri")
                self.conn.execute(
                    """
                    INSERT INTO inspection_tools(
                        inspection_id, ordinal, name, title, description,
                        input_schema_json, output_schema_json, annotations_json,
                        meta_json, ui_resource_uri, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        inspection_id,
                        ordinal,
                        tool.get("name", f"tool-{ordinal}"),
                        tool.get("title"),
                        tool.get("description"),
                        _dumps(tool.get("inputSchema", {})),
                        _dumps(tool["outputSchema"]) if tool.get("outputSchema") is not None else None,
                        _dumps(tool.get("annotations", {})),
                        _dumps(meta),
                        ui_uri,
                        _dumps(tool),
                    ),
                )

            for kind, items in (("resource", resources), ("resource_template", templates)):
                for ordinal, res in enumerate(items):
                    self.conn.execute(
                        """
                        INSERT INTO inspection_resources(
                            inspection_id, kind, ordinal, uri, name, description,
                            mime_type, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            inspection_id,
                            kind,
                            ordinal,
                            res.get("uri") or res.get("uriTemplate", f"{kind}-{ordinal}"),
                            res.get("name"),
                            res.get("description"),
                            res.get("mimeType"),
                            _dumps(res),
                        ),
                    )

            for ordinal, prompt in enumerate(prompts):
                self.conn.execute(
                    """
                    INSERT INTO inspection_prompts(
                        inspection_id, ordinal, name, description, arguments_json, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        inspection_id,
                        ordinal,
                        prompt.get("name", f"prompt-{ordinal}"),
                        prompt.get("description"),
                        _dumps(prompt.get("arguments", [])),
                        _dumps(prompt),
                    ),
                )

            for ordinal, finding in enumerate(lint):
                self.conn.execute(
                    """
                    INSERT INTO inspection_findings(
                        inspection_id, ordinal, kind, severity, referenced, source,
                        detail, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        inspection_id,
                        ordinal,
                        finding.get("kind", "finding"),
                        finding.get("severity", "warning"),
                        finding.get("referenced"),
                        finding.get("in") or finding.get("source"),
                        finding.get("detail"),
                        _dumps(finding),
                    ),
                )

        return {
            "inspection_public_id": pid,
            "inspection_id": inspection_id,
            "version": version,
            "target_public_id": self.conn.execute(
                "SELECT public_id FROM targets WHERE id = ?", (target_id,)
            ).fetchone()[0],
            "target_revision_public_id": revision["public_id"],
        }

    # ------------------------------------------------------------------ #
    # Profile
    # ------------------------------------------------------------------ #
    def record_profile(
        self,
        inspection_public_id: str,
        profile: JsonDict,
        *,
        model: str,
        prompt_text: str,
    ) -> JsonDict:
        inspection_id = self._inspection_id(inspection_public_id)
        sha = content_sha256(profile)
        existing = self.conn.execute(
            "SELECT public_id FROM capability_profiles WHERE inspection_id = ? AND content_sha256 = ?",
            (inspection_id, sha),
        ).fetchone()
        if existing is not None:
            return {"profile_public_id": existing["public_id"], "reused": True}

        pid = public_id("profile")
        with _db.transaction(self.conn):
            self.conn.execute(
                """
                INSERT INTO capability_profiles(
                    public_id, inspection_id, model, prompt_text, domain_summary,
                    raw_json, content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    inspection_id,
                    model or "",
                    prompt_text or "",
                    profile.get("domain_summary", ""),
                    _dumps(profile),
                    sha,
                    utc_now(),
                ),
            )
        return {"profile_public_id": pid, "reused": False}

    # ------------------------------------------------------------------ #
    # Dataset (personas + scenarios + cases)
    # ------------------------------------------------------------------ #
    def record_dataset(
        self,
        dataset: JsonDict,
        *,
        profile_public_id: Optional[str] = None,
        model: str = "",
        params: Optional[JsonDict] = None,
    ) -> JsonDict:
        """Persist a built dataset (build_dataset output: manifest/personas/scenarios)."""
        manifest = dataset.get("manifest", {})
        personas = dataset.get("personas", [])
        scenarios = dataset.get("scenarios", [])
        cases = manifest.get("cases", [])
        now = utc_now()
        profile_id = self._profile_id(profile_public_id) if profile_public_id else None

        pid = public_id("dataset")
        with _db.transaction(self.conn):
            cur = self.conn.execute(
                """
                INSERT INTO datasets(
                    public_id, profile_id, name, seed, model, params_json,
                    requested_personas, requested_scenarios_per_persona, status,
                    created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    profile_id,
                    manifest.get("name", "dataset"),
                    int(manifest.get("seed", 0) or 0),
                    model,
                    _dumps(params or {}),
                    int(manifest.get("n_personas", len(personas)) or 0),
                    int(manifest.get("scenarios_per_persona", 0) or 0),
                    "ready",
                    now,
                    now,
                ),
            )
            dataset_id = cur.lastrowid

            persona_ids: Dict[str, int] = {}
            for persona in personas:
                ext = str(persona.get("id"))
                cur = self.conn.execute(
                    """
                    INSERT INTO personas(
                        public_id, dataset_id, external_id, name, summary,
                        traits_json, context_json, raw_json, content_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        public_id("persona"),
                        dataset_id,
                        ext,
                        persona.get("name", ext),
                        persona.get("summary", ""),
                        _dumps(persona.get("traits", [])),
                        _dumps(persona.get("context", {})),
                        _dumps(persona),
                        content_sha256(persona),
                        now,
                    ),
                )
                persona_ids[ext] = cur.lastrowid

            scenario_ids: Dict[str, int] = {}
            for scenario in scenarios:
                ext = str(scenario.get("id"))
                cur = self.conn.execute(
                    """
                    INSERT INTO scenarios(
                        public_id, dataset_id, external_id, title, intent, situation,
                        goal, opening_message, max_turns, success_criteria_json,
                        failure_signals_json, exercises_json, raw_json, content_sha256,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        public_id("scenario"),
                        dataset_id,
                        ext,
                        scenario.get("title", ext),
                        scenario.get("intent"),
                        scenario.get("persona", ""),  # situational note
                        scenario.get("goal", ""),
                        scenario.get("opening_message", ""),
                        int(scenario.get("max_turns", 4) or 4),
                        _dumps(scenario.get("success_criteria", [])),
                        _dumps(scenario.get("failure_signals", [])),
                        _dumps(scenario.get("exercises", [])),
                        _dumps(scenario),
                        content_sha256(scenario),
                        now,
                    ),
                )
                scenario_ids[ext] = cur.lastrowid

            for ordinal, case in enumerate(cases):
                persona_key = str(case.get("persona"))
                scenario_key = str(case.get("scenario"))
                if persona_key not in persona_ids or scenario_key not in scenario_ids:
                    continue
                self.conn.execute(
                    """
                    INSERT INTO cases(
                        public_id, dataset_id, external_id, persona_id, scenario_id,
                        ordinal, intent, max_turns, expected_tools_json,
                        curation_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        public_id("case"),
                        dataset_id,
                        str(case.get("id")),
                        persona_ids[persona_key],
                        scenario_ids[scenario_key],
                        ordinal,
                        case.get("intent", ""),
                        int(case.get("max_turns", 4) or 4),
                        _dumps(case.get("exercises", [])),
                        case.get("status", "pending"),
                        now,
                        now,
                    ),
                )

        return {"dataset_public_id": pid, "dataset_id": dataset_id}

    def set_curation_status(
        self, dataset_public_id: str, statuses: Dict[str, str], note: Optional[str] = None
    ) -> int:
        """Set curation_status for cases (by external id) in one dataset."""
        dataset_id = self._dataset_id(dataset_public_id)
        updated = 0
        with _db.transaction(self.conn):
            for external_id, status in statuses.items():
                cur = self.conn.execute(
                    """
                    UPDATE cases SET curation_status = ?, curation_note = ?, updated_at = ?
                    WHERE dataset_id = ? AND external_id = ?
                    """,
                    (status, note, utc_now(), dataset_id, external_id),
                )
                updated += cur.rowcount
        return updated

    # ------------------------------------------------------------------ #
    # Runs (event sink)
    # ------------------------------------------------------------------ #
    def create_run_batch(
        self,
        target: Any,
        *,
        dataset_public_id: Optional[str] = None,
        profile_public_id: Optional[str] = None,
        selected_case_count: int = 0,
        options: Optional[JsonDict] = None,
    ) -> JsonDict:
        now = utc_now()
        with _db.transaction(self.conn):
            revision = self._ensure_target_revision(target)
            pid = public_id("run_batch")
            cur = self.conn.execute(
                """
                INSERT INTO run_batches(
                    public_id, dataset_id, target_revision_id, profile_id,
                    selected_case_count, options_json, status, created_at, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    self._dataset_id(dataset_public_id) if dataset_public_id else None,
                    revision["id"],
                    self._profile_id(profile_public_id) if profile_public_id else None,
                    selected_case_count,
                    _dumps(options or {}),
                    "running",
                    now,
                    now,
                ),
            )
            return {"run_batch_public_id": pid, "run_batch_id": cur.lastrowid}

    def finish_run_batch(self, run_batch_id: int, status: str) -> None:
        with _db.transaction(self.conn):
            self.conn.execute(
                "UPDATE run_batches SET status = ?, finished_at = ? WHERE id = ?",
                (status, utc_now(), run_batch_id),
            )

    def start_run(
        self,
        run_public_id: str,
        *,
        target: Any,
        scenario: Any,
        persona: Any = None,
        aut_runner: Any = None,
        user_runner: Any = None,
        aut_model: str = "",
        user_model: str = "",
        max_turns: int = 0,
        batch_id: Optional[int] = None,
        case_public_id: Optional[str] = None,
        inspection_public_id: Optional[str] = None,
        profile_public_id: Optional[str] = None,
    ) -> int:
        """Create the runs row at run start. Returns the internal run id."""
        now = utc_now()
        with _db.transaction(self.conn):
            revision = self._ensure_target_revision(target)
            cur = self.conn.execute(
                """
                INSERT INTO runs(
                    public_id, batch_id, case_id, target_revision_id, inspection_id,
                    profile_id, persona_snapshot_json, scenario_snapshot_json,
                    target_snapshot_json, aut_runner_json, user_runner_json,
                    aut_model, user_model, max_turns, status, started_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_public_id,
                    batch_id,
                    self._case_id(case_public_id) if case_public_id else None,
                    revision["id"],
                    self._inspection_id(inspection_public_id) if inspection_public_id else None,
                    self._profile_id(profile_public_id) if profile_public_id else None,
                    _dumps(_as_dict(persona)) if persona is not None else None,
                    _dumps(_as_dict(scenario)),
                    _dumps(redact_connection(_as_dict(target))),
                    _dumps(_as_dict(aut_runner)),
                    _dumps(_as_dict(user_runner)),
                    aut_model,
                    user_model,
                    int(max_turns or 0),
                    "running",
                    now,
                    now,
                ),
            )
            return cur.lastrowid

    def append_event(self, run_id: int, event: Any) -> int:
        """Append one trace event (and project tool calls). Returns event id.

        ``event`` may be a ``types.Event`` or a ``{type,timestamp,data}`` dict.
        Each call is its own small transaction so live traces stay visible.
        """
        if hasattr(event, "to_json"):
            payload = event.to_json()
        else:
            payload = dict(event)
        event_type = payload.get("type", "unknown")
        timestamp = payload.get("timestamp", utc_now())
        data = payload.get("data", {}) or {}
        turn_index = data.get("turn")

        with _db.transaction(self.conn):
            seq = self.conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            cur = self.conn.execute(
                """
                INSERT INTO run_events(
                    public_id, run_id, sequence, event_type, turn_index, actor,
                    timestamp, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    public_id("run_event"),
                    run_id,
                    seq,
                    event_type,
                    turn_index,
                    _actor_for(event_type),
                    timestamp,
                    _dumps(payload),
                ),
            )
            event_id = cur.lastrowid

            if event_type == "aut_result":
                for ordinal, call in enumerate(data.get("tool_calls", []) or []):
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO tool_calls(
                            public_id, run_id, event_id, turn_index, ordinal,
                            server_name, tool_name, status, arguments_json,
                            result_json, error_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            public_id("tool_call"),
                            run_id,
                            event_id,
                            int(turn_index or 0),
                            int(call.get("index", ordinal + 1) or ordinal + 1),
                            str(call.get("server", "?")),
                            str(call.get("tool", "?")),
                            call.get("status", "unknown"),
                            _dumps(call["arguments"]) if call.get("arguments") is not None else None,
                            _dumps(call["result"]) if call.get("result") is not None else None,
                            _dumps(call["error"]) if call.get("error") is not None else None,
                        ),
                    )
            return event_id

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        turns_completed: int = 0,
        error_text: Optional[str] = None,
    ) -> None:
        with _db.transaction(self.conn):
            tool_count = self.conn.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            self.conn.execute(
                """
                UPDATE runs SET status = ?, turns_completed = ?, tool_call_count = ?,
                    finished_at = ?, error_text = ?
                WHERE id = ?
                """,
                (status, int(turns_completed or 0), tool_count, utc_now(), error_text, run_id),
            )

    # ------------------------------------------------------------------ #
    # Judgments
    # ------------------------------------------------------------------ #
    def record_judgment(
        self,
        run_public_id: str,
        verdict: JsonDict,
        *,
        model: str = "",
        prompt_text: str = "",
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
    ) -> JsonDict:
        """Persist a judge verdict as the next attempt for a run."""
        run_id = self.run_id_by_public(run_public_id)
        if run_id is None:
            return {"judgment_public_id": None, "skipped": True}
        judge = verdict.get("judge", {}) or {}
        now = utc_now()
        pid = public_id("judgment")
        with _db.transaction(self.conn):
            attempt = self.conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM judgments WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            self.conn.execute(
                """
                INSERT INTO judgments(
                    public_id, run_id, attempt, model, prompt_text, run_status,
                    verdict, summary, gates_json, deterministic_json,
                    hallucinated_tools_json, raw_json, status, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    run_id,
                    attempt,
                    model,
                    prompt_text,
                    verdict.get("run_status", ""),
                    verdict.get("verdict"),
                    judge.get("summary", ""),
                    _dumps(verdict.get("gates", [])),
                    _dumps(verdict.get("deterministic", {})),
                    _dumps(judge.get("hallucinated_tools", [])),
                    _dumps(verdict),
                    "completed",
                    started_at or now,
                    finished_at or now,
                ),
            )
        return {"judgment_public_id": pid, "attempt": attempt}

    # ------------------------------------------------------------------ #
    # Artifacts
    # ------------------------------------------------------------------ #
    def index_artifact(
        self,
        owner_type: str,
        owner_public_id: str,
        kind: str,
        path: Union[str, Path],
        *,
        base_dir: Optional[Union[str, Path]] = None,
        mime_type: Optional[str] = None,
    ) -> Optional[str]:
        """Record a filesystem artifact by path. Missing files are skipped."""
        file_path = Path(path)
        if not file_path.exists():
            return None
        try:
            rel = str(file_path.relative_to(base_dir)) if base_dir else str(file_path)
        except ValueError:
            rel = str(file_path)
        data = file_path.read_bytes()
        pid = public_id("artifact")
        with _db.transaction(self.conn):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO artifacts(
                    public_id, owner_type, owner_public_id, kind, relative_path,
                    mime_type, byte_size, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    owner_type,
                    owner_public_id,
                    kind,
                    rel,
                    mime_type,
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                    utc_now(),
                ),
            )
        return pid

    # ------------------------------------------------------------------ #
    # Internal id lookups
    # ------------------------------------------------------------------ #
    def _scalar_id(self, table: str, public_value: str) -> Optional[int]:
        row = self.conn.execute(
            f"SELECT id FROM {table} WHERE public_id = ?", (public_value,)
        ).fetchone()
        return row["id"] if row else None

    def _inspection_id(self, public_value: str) -> int:
        found = self._scalar_id("inspections", public_value)
        if found is None:
            raise KeyError(f"unknown inspection: {public_value}")
        return found

    def _profile_id(self, public_value: str) -> int:
        found = self._scalar_id("capability_profiles", public_value)
        if found is None:
            raise KeyError(f"unknown profile: {public_value}")
        return found

    def _dataset_id(self, public_value: str) -> int:
        found = self._scalar_id("datasets", public_value)
        if found is None:
            raise KeyError(f"unknown dataset: {public_value}")
        return found

    def _case_id(self, public_value: str) -> Optional[int]:
        return self._scalar_id("cases", public_value)

    def run_id_by_public(self, public_value: str) -> Optional[int]:
        return self._scalar_id("runs", public_value)

    def find_inspection_by_mcp(self, mcp: str) -> Optional[str]:
        """Newest inspection public_id for an ``name@version`` string (CLI linkage)."""
        name, _, version = (mcp or "").partition("@")
        if not name:
            return None
        row = self.conn.execute(
            "SELECT public_id FROM inspections WHERE server_name = ? AND server_version = ? "
            "ORDER BY version DESC LIMIT 1",
            (name, version),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT public_id FROM inspections WHERE server_name = ? "
                "ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
        return row["public_id"] if row else None

    def find_profile_by_mcp(self, mcp: str) -> Optional[str]:
        """Newest profile public_id whose inspection matches an ``name@version``."""
        inspection_public = self.find_inspection_by_mcp(mcp)
        if inspection_public is None:
            return None
        found = self.latest_profile_for_inspection(inspection_public)
        return found["profile_public_id"] if found else None

    # ------------------------------------------------------------------ #
    # Read models
    # ------------------------------------------------------------------ #
    def list_targets(self) -> List[JsonDict]:
        rows = self.conn.execute(
            """
            SELECT t.public_id, t.slug, t.display_name, t.created_at,
                   COUNT(DISTINCT i.id) AS inspection_count,
                   MAX(i.started_at) AS last_inspected_at
            FROM targets t
            LEFT JOIN target_revisions r ON r.target_id = t.id
            LEFT JOIN inspections i ON i.target_revision_id = r.id
            GROUP BY t.id
            ORDER BY last_inspected_at DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def list_inspections(self, target_public_id: str) -> List[JsonDict]:
        rows = self.conn.execute(
            """
            SELECT i.public_id, i.version, i.server_name, i.server_version,
                   i.tool_count, i.resource_count, i.prompt_count, i.lint_count,
                   i.status, i.started_at, i.finished_at, r.public_id AS revision_public_id
            FROM inspections i
            JOIN target_revisions r ON i.target_revision_id = r.id
            JOIN targets t ON r.target_id = t.id
            WHERE t.public_id = ?
            ORDER BY i.version DESC
            """,
            (target_public_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_inspection(self, inspection_public_id: str) -> Optional[JsonDict]:
        row = self.conn.execute(
            "SELECT * FROM inspections WHERE public_id = ?", (inspection_public_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["raw"] = json.loads(out["raw_json"])
        inspection_id = out["id"]
        out["tools"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT name, title, description, ui_resource_uri FROM inspection_tools "
                "WHERE inspection_id = ? ORDER BY ordinal",
                (inspection_id,),
            ).fetchall()
        ]
        out["findings"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT kind, referenced, source, detail FROM inspection_findings "
                "WHERE inspection_id = ? ORDER BY ordinal",
                (inspection_id,),
            ).fetchall()
        ]
        return out

    def latest_profile_for_inspection(self, inspection_public_id: str) -> Optional[JsonDict]:
        inspection_id = self._inspection_id(inspection_public_id)
        row = self.conn.execute(
            "SELECT public_id, raw_json FROM capability_profiles WHERE inspection_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (inspection_id,),
        ).fetchone()
        if row is None:
            return None
        return {"profile_public_id": row["public_id"], "profile": json.loads(row["raw_json"])}

    def list_datasets(self, profile_public_id: Optional[str] = None) -> List[JsonDict]:
        if profile_public_id:
            profile_id = self._profile_id(profile_public_id)
            rows = self.conn.execute(
                "SELECT * FROM datasets WHERE profile_id = ? ORDER BY created_at DESC",
                (profile_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM datasets ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_dataset_cases(
        self, dataset_public_id: str, curation_status: Optional[str] = None
    ) -> List[JsonDict]:
        dataset_id = self._dataset_id(dataset_public_id)
        query = (
            "SELECT c.*, p.external_id AS persona_external_id, "
            "s.external_id AS scenario_external_id, s.title AS scenario_title "
            "FROM cases c JOIN personas p ON c.persona_id = p.id "
            "JOIN scenarios s ON c.scenario_id = s.id WHERE c.dataset_id = ?"
        )
        params: List[Any] = [dataset_id]
        if curation_status:
            query += " AND c.curation_status = ?"
            params.append(curation_status)
        query += " ORDER BY c.ordinal"
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def list_runs(self, filters: Optional[JsonDict] = None) -> List[JsonDict]:
        """List runs with target slug and latest verdict, newest first."""
        filters = filters or {}
        rows = self.conn.execute(
            """
            SELECT run.public_id, run.status, run.started_at, run.finished_at,
                   run.turns_completed, run.tool_call_count, run.scenario_snapshot_json,
                   t.slug AS target_slug, t.public_id AS target_public_id,
                   (SELECT j.verdict FROM judgments j WHERE j.run_id = run.id
                    ORDER BY j.attempt DESC LIMIT 1) AS verdict
            FROM runs run
            JOIN target_revisions r ON run.target_revision_id = r.id
            JOIN targets t ON r.target_id = t.id
            ORDER BY run.created_at DESC
            """
        ).fetchall()
        out: List[JsonDict] = []
        for row in rows:
            item = dict(row)
            scenario = json.loads(item.pop("scenario_snapshot_json") or "{}")
            item["scenario_title"] = scenario.get("title") or scenario.get("id", "")
            item["scenario_id"] = scenario.get("id", "")
            item["verdict"] = item["verdict"] or "not evaluated"
            out.append(item)

        def keep(item: JsonDict) -> bool:
            if filters.get("target") and item["target_slug"] != filters["target"]:
                return False
            if filters.get("status") and item["status"] != filters["status"]:
                return False
            if filters.get("verdict") and item["verdict"] != filters["verdict"]:
                return False
            search = (filters.get("search") or "").lower()
            if search:
                hay = f"{item['scenario_title']} {item['target_slug']} {item['public_id']}".lower()
                if search not in hay:
                    return False
            return True

        return [item for item in out if keep(item)]

    def get_run(self, run_public_id: str) -> Optional[JsonDict]:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE public_id = ?", (run_public_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["persona"] = json.loads(out["persona_snapshot_json"]) if out["persona_snapshot_json"] else None
        out["scenario"] = json.loads(out["scenario_snapshot_json"])
        out["target"] = json.loads(out["target_snapshot_json"])
        return out

    def get_run_events(self, run_public_id: str) -> List[JsonDict]:
        """Return events as {type,timestamp,data} dicts (events.jsonl shape)."""
        run_id = self.run_id_by_public(run_public_id)
        if run_id is None:
            return []
        rows = self.conn.execute(
            "SELECT payload_json FROM run_events WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def get_run_trace(self, run_public_id: str) -> Optional[JsonDict]:
        """Reconstruct the full trace, reusing evaluate's events reconstruction."""
        events = self.get_run_events(run_public_id)
        if not events:
            return None
        from ..evaluate import reconstruct_run

        return reconstruct_run(events)

    def get_latest_judgment(self, run_public_id: str) -> Optional[JsonDict]:
        run_id = self.run_id_by_public(run_public_id)
        if run_id is None:
            return None
        row = self.conn.execute(
            "SELECT * FROM judgments WHERE run_id = ? ORDER BY attempt DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["raw"] = json.loads(out["raw_json"]) if out["raw_json"] else {}
        return out

    def list_artifacts(self, owner_type: str, owner_public_id: str) -> List[JsonDict]:
        rows = self.conn.execute(
            "SELECT kind, relative_path, mime_type, byte_size FROM artifacts "
            "WHERE owner_type = ? AND owner_public_id = ? ORDER BY kind",
            (owner_type, owner_public_id),
        ).fetchall()
        return [dict(r) for r in rows]
