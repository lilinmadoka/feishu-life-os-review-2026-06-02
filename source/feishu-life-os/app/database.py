from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.models import (
    ActionCreate,
    ActionRecord,
    ActionStatus,
    ActionUpdate,
    AgentRunCreate,
    AgentRunRecord,
    AgentRunStatus,
    CaptureCreate,
    CaptureRecord,
    CaptureStatus,
    ReviewJobCreate,
    ReviewJobRecord,
    ReviewJobStatus,
    ReviewJobType,
    SyncEvent,
    SyncTarget,
)

try:  # Optional at runtime for local SQLite-only development.
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - only relevant before deps are installed
    psycopg = None
    dict_row = None

JSON_FIELDS_CAPTURE = {"attachments", "metadata"}
JSON_FIELDS_ACTION = {"people", "projects", "labels", "metadata"}
JSON_FIELDS_SYNC = {"request_payload", "response_payload"}
JSON_FIELDS_REVIEW_JOB = {"action_ids", "result_json"}
JSON_FIELDS_AGENT_RUN = {"request_json", "response_json", "tool_results_json"}
DATETIME_FIELDS_CAPTURE = {"created_at", "updated_at"}
DATETIME_FIELDS_ACTION = {"due_at", "start_at", "remind_at", "created_at", "updated_at"}
DATETIME_FIELDS_SYNC = {"created_at"}
DATETIME_FIELDS_REVIEW_JOB = {"created_at", "updated_at"}
DATETIME_FIELDS_AGENT_RUN = {"created_at", "updated_at"}


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def loads(value: str | None, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


class Repository:
    """Small sqlite repository.

    This project intentionally avoids a heavyweight ORM so Codex can easily port the
    model to Feishu Bitable, PostgreSQL, or another database later.
    """

    def __init__(self, db_path: str, database_url: str | None = None):
        self.db_path = db_path
        self.database_url = database_url
        self.is_postgres = bool(database_url)
        if not self.is_postgres:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        if self.is_postgres:
            if psycopg is None or dict_row is None:
                raise RuntimeError("psycopg is required when DATABASE_URL is set")
            conn = psycopg.connect(self.database_url, row_factory=dict_row)
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ph(self) -> str:
        return "%s" if self.is_postgres else "?"

    def _sql(self, sql: str) -> str:
        if not self.is_postgres:
            return sql
        return re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", sql)

    def migrate(self) -> None:
        with self.connect() as conn:
            if self.is_postgres:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS captures (
                        id TEXT PRIMARY KEY,
                        raw_text TEXT NOT NULL,
                        normalized_text TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        source_ref TEXT,
                        attachments TEXT NOT NULL DEFAULT '[]',
                        metadata TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL,
                        confidence REAL NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS actions (
                        id TEXT PRIMARY KEY,
                        capture_id TEXT,
                        title TEXT NOT NULL,
                        description TEXT,
                        intent TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        status TEXT NOT NULL,
                        priority TEXT NOT NULL,
                        energy TEXT NOT NULL,
                        due_at TEXT,
                        start_at TEXT,
                        remind_at TEXT,
                        estimated_minutes INTEGER,
                        people TEXT NOT NULL DEFAULT '[]',
                        projects TEXT NOT NULL DEFAULT '[]',
                        labels TEXT NOT NULL DEFAULT '[]',
                        evidence_text TEXT,
                        confidence REAL NOT NULL DEFAULT 0.5,
                        metadata TEXT NOT NULL DEFAULT '{}',
                        feishu_task_guid TEXT,
                        feishu_record_id TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(capture_id) REFERENCES captures(id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_actions_status_due ON actions(status, due_at);
                    CREATE INDEX IF NOT EXISTS idx_actions_capture ON actions(capture_id);
                    CREATE INDEX IF NOT EXISTS idx_captures_source ON captures(source_type, source_ref);

                    CREATE TABLE IF NOT EXISTS sync_events (
                        id TEXT PRIMARY KEY,
                        target TEXT NOT NULL,
                        entity_type TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_payload TEXT NOT NULL DEFAULT '{}',
                        response_payload TEXT NOT NULL DEFAULT '{}',
                        error TEXT,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS review_jobs (
                        id TEXT PRIMARY KEY,
                        job_type TEXT NOT NULL,
                        capture_id TEXT,
                        action_ids TEXT NOT NULL DEFAULT '[]',
                        source_ref TEXT,
                        status TEXT NOT NULL,
                        prompt TEXT NOT NULL,
                        result_json TEXT NOT NULL DEFAULT '{}',
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(capture_id) REFERENCES captures(id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_review_jobs_status_created
                        ON review_jobs(status, created_at);

                    CREATE TABLE IF NOT EXISTS agent_runs (
                        id TEXT PRIMARY KEY,
                        capture_id TEXT,
                        source_ref TEXT,
                        provider TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        response_json TEXT NOT NULL DEFAULT '{}',
                        tool_results_json TEXT NOT NULL DEFAULT '[]',
                        error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(capture_id) REFERENCES captures(id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_agent_runs_capture_created
                        ON agent_runs(capture_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_agent_runs_status_created
                        ON agent_runs(status, created_at);
                    """
                )
            else:
                conn.executescript(
                    """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS captures (
                    id TEXT PRIMARY KEY,
                    raw_text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_ref TEXT,
                    attachments TEXT NOT NULL DEFAULT '[]',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY,
                    capture_id TEXT,
                    title TEXT NOT NULL,
                    description TEXT,
                    intent TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    energy TEXT NOT NULL,
                    due_at TEXT,
                    start_at TEXT,
                    remind_at TEXT,
                    estimated_minutes INTEGER,
                    people TEXT NOT NULL DEFAULT '[]',
                    projects TEXT NOT NULL DEFAULT '[]',
                    labels TEXT NOT NULL DEFAULT '[]',
                    evidence_text TEXT,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    feishu_task_guid TEXT,
                    feishu_record_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(capture_id) REFERENCES captures(id)
                );

                CREATE INDEX IF NOT EXISTS idx_actions_status_due ON actions(status, due_at);
                CREATE INDEX IF NOT EXISTS idx_actions_capture ON actions(capture_id);
                CREATE INDEX IF NOT EXISTS idx_captures_source ON captures(source_type, source_ref);

                CREATE TABLE IF NOT EXISTS sync_events (
                    id TEXT PRIMARY KEY,
                    target TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_payload TEXT NOT NULL DEFAULT '{}',
                    response_payload TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS review_jobs (
                    id TEXT PRIMARY KEY,
                    job_type TEXT NOT NULL,
                    capture_id TEXT,
                    action_ids TEXT NOT NULL DEFAULT '[]',
                    source_ref TEXT,
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(capture_id) REFERENCES captures(id)
                );

                CREATE INDEX IF NOT EXISTS idx_review_jobs_status_created
                    ON review_jobs(status, created_at);

                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    capture_id TEXT,
                    source_ref TEXT,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL DEFAULT '{}',
                    response_json TEXT NOT NULL DEFAULT '{}',
                    tool_results_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(capture_id) REFERENCES captures(id)
                );

                CREATE INDEX IF NOT EXISTS idx_agent_runs_capture_created
                    ON agent_runs(capture_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_status_created
                    ON agent_runs(status, created_at);
                """
                )

    def create_capture(self, capture: CaptureCreate, normalized_text: str) -> CaptureRecord:
        now = utcnow_iso()
        data = {
            "id": new_id("cap"),
            "raw_text": capture.raw_text,
            "normalized_text": normalized_text,
            "source_type": capture.source_type.value,
            "source_ref": capture.source_ref,
            "attachments": dumps([a.model_dump(exclude_none=True) for a in capture.attachments]),
            "metadata": dumps(capture.metadata),
            "status": CaptureStatus.new.value,
            "confidence": 0.0,
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            conn.execute(
                self._sql(
                    """
                INSERT INTO captures (id, raw_text, normalized_text, source_type, source_ref, attachments, metadata,
                                      status, confidence, created_at, updated_at)
                VALUES (:id, :raw_text, :normalized_text, :source_type, :source_ref, :attachments, :metadata,
                        :status, :confidence, :created_at, :updated_at)
                """
                ),
                data,
            )
        return self.get_capture(data["id"])

    def update_capture_status(
        self, capture_id: str, status: CaptureStatus, confidence: float | None = None
    ) -> CaptureRecord:
        fields = {"status": status.value, "updated_at": utcnow_iso(), "id": capture_id}
        sql = "UPDATE captures SET status=:status, updated_at=:updated_at"
        if confidence is not None:
            fields["confidence"] = confidence
            sql += ", confidence=:confidence"
        sql += " WHERE id=:id"
        with self.connect() as conn:
            conn.execute(self._sql(sql), fields)
        return self.get_capture(capture_id)

    def get_capture(self, capture_id: str) -> CaptureRecord:
        with self.connect() as conn:
            row = conn.execute(f"SELECT * FROM captures WHERE id={self._ph()}", (capture_id,)).fetchone()
        if row is None:
            raise KeyError(f"capture not found: {capture_id}")
        return self._capture_from_row(row)

    def list_captures(self, status: CaptureStatus | None = None, limit: int = 100) -> list[CaptureRecord]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    f"SELECT * FROM captures WHERE status={self._ph()} ORDER BY created_at DESC LIMIT {self._ph()}",
                    (status.value, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM captures ORDER BY created_at DESC LIMIT {self._ph()}", (limit,)
                ).fetchall()
        return [self._capture_from_row(row) for row in rows]

    def get_capture_by_source(self, source_type: str, source_ref: str) -> CaptureRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM captures WHERE source_type={self._ph()} AND source_ref={self._ph()} LIMIT 1",
                (source_type, source_ref),
            ).fetchone()
        return self._capture_from_row(row) if row else None

    def create_action(self, action: ActionCreate) -> ActionRecord:
        now = utcnow_iso()
        payload = action.model_dump(mode="json")
        data = {
            **payload,
            "id": new_id("act"),
            "created_at": now,
            "updated_at": now,
            "people": dumps(payload.get("people", [])),
            "projects": dumps(payload.get("projects", [])),
            "labels": dumps(payload.get("labels", [])),
            "metadata": dumps(payload.get("metadata", {})),
        }
        for key in ["due_at", "start_at", "remind_at"]:
            value = data.get(key)
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        with self.connect() as conn:
            conn.execute(
                self._sql(
                    """
                INSERT INTO actions (id, capture_id, title, description, intent, domain, status, priority, energy,
                                     due_at, start_at, remind_at, estimated_minutes, people, projects, labels,
                                     evidence_text, confidence, metadata, created_at, updated_at)
                VALUES (:id, :capture_id, :title, :description, :intent, :domain, :status, :priority, :energy,
                        :due_at, :start_at, :remind_at, :estimated_minutes, :people, :projects, :labels,
                        :evidence_text, :confidence, :metadata, :created_at, :updated_at)
                """
                ),
                data,
            )
        return self.get_action(data["id"])

    def get_action(self, action_id: str) -> ActionRecord:
        with self.connect() as conn:
            row = conn.execute(f"SELECT * FROM actions WHERE id={self._ph()}", (action_id,)).fetchone()
        if row is None:
            raise KeyError(f"action not found: {action_id}")
        return self._action_from_row(row)

    def update_action(self, action_id: str, patch: ActionUpdate) -> ActionRecord:
        incoming = patch.model_dump(exclude_unset=True, mode="json")
        if not incoming:
            return self.get_action(action_id)
        assignments: list[str] = []
        params: dict[str, Any] = {"id": action_id, "updated_at": utcnow_iso()}
        for key, value in incoming.items():
            assignments.append(f"{key}=:{key}")
            if key in JSON_FIELDS_ACTION:
                params[key] = dumps(value)
            elif isinstance(value, datetime):
                params[key] = value.isoformat()
            else:
                params[key] = value
        assignments.append("updated_at=:updated_at")
        sql = f"UPDATE actions SET {', '.join(assignments)} WHERE id=:id"
        with self.connect() as conn:
            conn.execute(self._sql(sql), params)
        return self.get_action(action_id)

    def list_actions(
        self,
        statuses: list[ActionStatus] | None = None,
        limit: int = 200,
        include_done: bool = False,
    ) -> list[ActionRecord]:
        with self.connect() as conn:
            params: list[Any] = []
            where = []
            if statuses:
                placeholders = ",".join(self._ph() for _ in statuses)
                where.append(f"status IN ({placeholders})")
                params.extend(s.value for s in statuses)
            elif not include_done:
                where.append("status NOT IN ('done', 'canceled')")
            sql = "SELECT * FROM actions"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += f" ORDER BY COALESCE(due_at, '9999-12-31'), created_at DESC LIMIT {self._ph()}"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
        return [self._action_from_row(row) for row in rows]

    def list_actions_by_capture(self, capture_id: str) -> list[ActionRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM actions WHERE capture_id={self._ph()} ORDER BY created_at ASC",
                (capture_id,),
            ).fetchall()
        return [self._action_from_row(row) for row in rows]

    def complete_action(self, action_id: str) -> ActionRecord:
        return self.update_action(action_id, ActionUpdate(status=ActionStatus.done))

    def find_similar_actions(self, normalized_title: str, limit: int = 20) -> list[ActionRecord]:
        token = normalized_title[:16]
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM actions
                WHERE status NOT IN ('done', 'canceled') AND title LIKE {self._ph()}
                ORDER BY created_at DESC LIMIT {self._ph()}
                """,
                (f"%{token}%", limit),
            ).fetchall()
        return [self._action_from_row(row) for row in rows]

    def create_sync_event(
        self,
        target: SyncTarget,
        entity_type: str,
        entity_id: str,
        status: str,
        request_payload: dict[str, Any] | None = None,
        response_payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> SyncEvent:
        data = {
            "id": new_id("sync"),
            "target": target.value,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "status": status,
            "request_payload": dumps(request_payload or {}),
            "response_payload": dumps(response_payload or {}),
            "error": error,
            "created_at": utcnow_iso(),
        }
        with self.connect() as conn:
            conn.execute(
                self._sql(
                    """
                INSERT INTO sync_events (id, target, entity_type, entity_id, status, request_payload,
                                         response_payload, error, created_at)
                VALUES (:id, :target, :entity_type, :entity_id, :status, :request_payload,
                        :response_payload, :error, :created_at)
                """
                ),
                data,
            )
        return self.get_sync_event(data["id"])

    def get_sync_event(self, sync_id: str) -> SyncEvent:
        with self.connect() as conn:
            row = conn.execute(f"SELECT * FROM sync_events WHERE id={self._ph()}", (sync_id,)).fetchone()
        if row is None:
            raise KeyError(f"sync event not found: {sync_id}")
        return self._sync_from_row(row)

    def create_review_job(self, job: ReviewJobCreate) -> ReviewJobRecord:
        now = utcnow_iso()
        payload = job.model_dump(mode="json")
        data = {
            **payload,
            "id": new_id("job"),
            "status": ReviewJobStatus.pending.value,
            "action_ids": dumps(payload.get("action_ids", [])),
            "result_json": dumps({}),
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            conn.execute(
                self._sql(
                    """
                INSERT INTO review_jobs (id, job_type, capture_id, action_ids, source_ref, status,
                                         prompt, result_json, error, created_at, updated_at)
                VALUES (:id, :job_type, :capture_id, :action_ids, :source_ref, :status,
                        :prompt, :result_json, :error, :created_at, :updated_at)
                """
                ),
                data,
            )
        return self.get_review_job(data["id"])

    def get_review_job(self, job_id: str) -> ReviewJobRecord:
        with self.connect() as conn:
            row = conn.execute(f"SELECT * FROM review_jobs WHERE id={self._ph()}", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"review job not found: {job_id}")
        return self._review_job_from_row(row)

    def get_next_review_job(self) -> ReviewJobRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM review_jobs
                WHERE status={self._ph()}
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (ReviewJobStatus.pending.value,),
            ).fetchone()
            if row is None:
                return None
            job_id = dict(row)["id"]
            conn.execute(
                self._sql(
                    """
                    UPDATE review_jobs
                    SET status=:status, updated_at=:updated_at
                    WHERE id=:id
                    """
                ),
                {"status": ReviewJobStatus.running.value, "updated_at": utcnow_iso(), "id": job_id},
            )
        return self.get_review_job(job_id)

    def complete_review_job(self, job_id: str, result_json: dict[str, Any]) -> ReviewJobRecord:
        with self.connect() as conn:
            conn.execute(
                self._sql(
                    """
                    UPDATE review_jobs
                    SET status=:status, result_json=:result_json, error=NULL, updated_at=:updated_at
                    WHERE id=:id
                    """
                ),
                {
                    "status": ReviewJobStatus.done.value,
                    "result_json": dumps(result_json),
                    "updated_at": utcnow_iso(),
                    "id": job_id,
                },
            )
        return self.get_review_job(job_id)

    def fail_review_job(
        self, job_id: str, error: str, result_json: dict[str, Any] | None = None
    ) -> ReviewJobRecord:
        with self.connect() as conn:
            conn.execute(
                self._sql(
                    """
                    UPDATE review_jobs
                    SET status=:status, result_json=:result_json, error=:error, updated_at=:updated_at
                    WHERE id=:id
                    """
                ),
                {
                    "status": ReviewJobStatus.failed.value,
                    "result_json": dumps(result_json or {}),
                    "error": error,
                    "updated_at": utcnow_iso(),
                    "id": job_id,
                },
            )
        return self.get_review_job(job_id)

    def create_sync_error_review_job(self, event: SyncEvent, prompt: str) -> ReviewJobRecord:
        existing = self._find_pending_review_job(
            ReviewJobType.sync_error.value, f"sync:{event.id}"
        )
        if existing:
            return existing
        return self.create_review_job(
            ReviewJobCreate(
                job_type=ReviewJobType.sync_error,
                capture_id=None,
                action_ids=[event.entity_id] if event.entity_type == "action" else [],
                source_ref=f"sync:{event.id}",
                prompt=prompt,
            )
        )

    def create_agent_run(self, run: AgentRunCreate) -> AgentRunRecord:
        now = utcnow_iso()
        payload = run.model_dump(mode="json")
        data = {
            **payload,
            "id": new_id("agent"),
            "status": AgentRunStatus.running.value,
            "request_json": dumps(payload.get("request_json", {})),
            "response_json": dumps({}),
            "tool_results_json": dumps([]),
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            conn.execute(
                self._sql(
                    """
                INSERT INTO agent_runs (id, capture_id, source_ref, provider, status, request_json,
                                        response_json, tool_results_json, error, created_at, updated_at)
                VALUES (:id, :capture_id, :source_ref, :provider, :status, :request_json,
                        :response_json, :tool_results_json, :error, :created_at, :updated_at)
                """
                ),
                data,
            )
        return self.get_agent_run(data["id"])

    def get_agent_run(self, run_id: str) -> AgentRunRecord:
        with self.connect() as conn:
            row = conn.execute(f"SELECT * FROM agent_runs WHERE id={self._ph()}", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"agent run not found: {run_id}")
        return self._agent_run_from_row(row)

    def complete_agent_run(
        self,
        run_id: str,
        response_json: dict[str, Any],
        tool_results_json: list[dict[str, Any]],
    ) -> AgentRunRecord:
        with self.connect() as conn:
            conn.execute(
                self._sql(
                    """
                    UPDATE agent_runs
                    SET status=:status, response_json=:response_json,
                        tool_results_json=:tool_results_json, error=NULL, updated_at=:updated_at
                    WHERE id=:id
                    """
                ),
                {
                    "status": AgentRunStatus.done.value,
                    "response_json": dumps(response_json),
                    "tool_results_json": dumps(tool_results_json),
                    "updated_at": utcnow_iso(),
                    "id": run_id,
                },
            )
        return self.get_agent_run(run_id)

    def fail_agent_run(
        self,
        run_id: str,
        error: str,
        response_json: dict[str, Any] | None = None,
        tool_results_json: list[dict[str, Any]] | None = None,
    ) -> AgentRunRecord:
        with self.connect() as conn:
            conn.execute(
                self._sql(
                    """
                    UPDATE agent_runs
                    SET status=:status, response_json=:response_json,
                        tool_results_json=:tool_results_json, error=:error, updated_at=:updated_at
                    WHERE id=:id
                    """
                ),
                {
                    "status": AgentRunStatus.failed.value,
                    "response_json": dumps(response_json or {}),
                    "tool_results_json": dumps(tool_results_json or []),
                    "error": error,
                    "updated_at": utcnow_iso(),
                    "id": run_id,
                },
            )
        return self.get_agent_run(run_id)

    def list_agent_runs(self, status: AgentRunStatus | None = None, limit: int = 100) -> list[AgentRunRecord]:
        with self.connect() as conn:
            if status:
                rows = conn.execute(
                    f"SELECT * FROM agent_runs WHERE status={self._ph()} ORDER BY created_at DESC LIMIT {self._ph()}",
                    (status.value, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT {self._ph()}",
                    (limit,),
                ).fetchall()
        return [self._agent_run_from_row(row) for row in rows]

    def _find_pending_review_job(self, job_type: str, source_ref: str) -> ReviewJobRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM review_jobs
                WHERE job_type={self._ph()} AND source_ref={self._ph()}
                  AND status IN ('pending', 'running', 'done')
                LIMIT 1
                """,
                (job_type, source_ref),
            ).fetchone()
        return self._review_job_from_row(row) if row else None

    def _capture_from_row(self, row: sqlite3.Row) -> CaptureRecord:
        data = dict(row)
        data["attachments"] = loads(data.get("attachments"), [])
        data["metadata"] = loads(data.get("metadata"), {})
        for field in DATETIME_FIELDS_CAPTURE:
            data[field] = parse_dt(data.get(field))
        return CaptureRecord(**data)

    def _action_from_row(self, row: sqlite3.Row) -> ActionRecord:
        data = dict(row)
        for field in JSON_FIELDS_ACTION:
            data[field] = loads(data.get(field), [] if field != "metadata" else {})
        for field in DATETIME_FIELDS_ACTION:
            data[field] = parse_dt(data.get(field))
        return ActionRecord(**data)

    def _sync_from_row(self, row: sqlite3.Row) -> SyncEvent:
        data = dict(row)
        for field in JSON_FIELDS_SYNC:
            data[field] = loads(data.get(field), {})
        for field in DATETIME_FIELDS_SYNC:
            data[field] = parse_dt(data.get(field))
        return SyncEvent(**data)

    def _review_job_from_row(self, row: sqlite3.Row) -> ReviewJobRecord:
        data = dict(row)
        for field in JSON_FIELDS_REVIEW_JOB:
            data[field] = loads(data.get(field), [] if field == "action_ids" else {})
        for field in DATETIME_FIELDS_REVIEW_JOB:
            data[field] = parse_dt(data.get(field))
        return ReviewJobRecord(**data)

    def _agent_run_from_row(self, row: sqlite3.Row) -> AgentRunRecord:
        data = dict(row)
        for field in JSON_FIELDS_AGENT_RUN:
            data[field] = loads(data.get(field), [] if field == "tool_results_json" else {})
        for field in DATETIME_FIELDS_AGENT_RUN:
            data[field] = parse_dt(data.get(field))
        return AgentRunRecord(**data)
