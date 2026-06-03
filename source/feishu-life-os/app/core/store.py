from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.schemas import (
    ActionItem,
    AgentRun,
    CalendarEvent,
    Capture,
    CaptureIn,
    Confirmation,
    ConfirmationStatus,
    Evidence,
    ItemStatus,
    PlanDraft,
    PlanDraftStatus,
    ProcessedStatus,
    RunStatus,
    ScheduleBlock,
    ToolRun,
)
from app.database import Repository, new_id, parse_dt, utcnow_iso


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


class StateStore:
    def __init__(self, repo: Repository):
        self.repo = repo

    def migrate(self) -> None:
        with self.repo.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS core_captures (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_message_id TEXT,
                    source_event_id TEXT,
                    sender_id TEXT,
                    chat_id TEXT,
                    content_type TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    attachment_refs TEXT NOT NULL DEFAULT '[]',
                    received_at TEXT NOT NULL,
                    processed_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_core_captures_source_message
                    ON core_captures(source, source_message_id);

                CREATE TABLE IF NOT EXISTS evidences (
                    id TEXT PRIMARY KEY,
                    capture_id TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    content_ref TEXT,
                    original_filename TEXT,
                    source_url_or_message_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS action_items (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    due_at TEXT,
                    estimated_minutes INTEGER,
                    project_id TEXT,
                    person_id TEXT,
                    source_capture_id TEXT,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS calendar_events (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    location TEXT,
                    status TEXT NOT NULL,
                    source_capture_id TEXT,
                    feishu_event_id TEXT,
                    plan_draft_id TEXT,
                    plan_item_id TEXT,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedule_blocks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    recurrence_rule TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reminder_enabled INTEGER NOT NULL DEFAULT 1,
                    source_capture_id TEXT,
                    feishu_event_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    remind_at TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS commitments (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    promised_to TEXT,
                    due_at TEXT,
                    linked_action_item_id TEXT,
                    source_capture_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS waiting_for (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    waiting_for_person TEXT,
                    expected_at TEXT,
                    linked_project_id TEXT,
                    source_capture_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS persons (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    aliases TEXT NOT NULL DEFAULT '[]',
                    role TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS core_agent_runs (
                    id TEXT PRIMARY KEY,
                    capture_id TEXT,
                    provider TEXT NOT NULL,
                    model TEXT,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    tool_calls_json TEXT NOT NULL DEFAULT '[]',
                    latency_ms INTEGER,
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_runs (
                    id TEXT PRIMARY KEY,
                    agent_run_id TEXT,
                    tool_name TEXT NOT NULL,
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS confirmations (
                    id TEXT PRIMARY KEY,
                    agent_run_id TEXT,
                    confirmation_type TEXT NOT NULL,
                    proposed_tool_calls_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    expires_at TEXT,
                    feishu_card_id TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    sender_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_confirmations_sender_status
                    ON confirmations(sender_id, status, created_at);

                CREATE TABLE IF NOT EXISTS plan_drafts (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    missing_fields_json TEXT NOT NULL DEFAULT '[]',
                    source_capture_id TEXT,
                    sender_id TEXT,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_plan_drafts_sender_status
                    ON plan_drafts(sender_id, status, created_at);
                """
            )
            with suppress(Exception):
                conn.execute("ALTER TABLE schedule_blocks ADD COLUMN feishu_event_id TEXT")
            with suppress(Exception):
                conn.execute("ALTER TABLE schedule_blocks ADD COLUMN reminder_enabled INTEGER NOT NULL DEFAULT 1")
            with suppress(Exception):
                conn.execute("ALTER TABLE calendar_events ADD COLUMN plan_draft_id TEXT")
            with suppress(Exception):
                conn.execute("ALTER TABLE calendar_events ADD COLUMN plan_item_id TEXT")

    def create_capture(self, item: CaptureIn) -> Capture:
        existing = None
        if item.source_message_id:
            with self.repo.connect() as conn:
                existing = conn.execute(
                    "SELECT * FROM core_captures WHERE source=? AND source_message_id=? LIMIT 1",
                    (item.source, item.source_message_id),
                ).fetchone()
        if existing:
            return self._capture(existing)
        now = utcnow_iso()
        received = item.received_at.isoformat() if item.received_at else now
        data = {
            "id": new_id("cap2"),
            "source": item.source,
            "source_message_id": item.source_message_id,
            "source_event_id": item.source_event_id,
            "sender_id": item.sender_id,
            "chat_id": item.chat_id,
            "content_type": item.content_type,
            "raw_text": item.raw_text,
            "attachment_refs": _dumps(item.attachment_refs),
            "received_at": received,
            "processed_status": ProcessedStatus.new.value,
            "created_at": now,
        }
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT INTO core_captures (
                    id, source, source_message_id, source_event_id, sender_id, chat_id,
                    content_type, raw_text, attachment_refs, received_at, processed_status, created_at
                ) VALUES (
                    :id, :source, :source_message_id, :source_event_id, :sender_id, :chat_id,
                    :content_type, :raw_text, :attachment_refs, :received_at, :processed_status, :created_at
                )
                """,
                data,
            )
        self.create_evidence(
            capture_id=data["id"],
            evidence_type=item.content_type,
            content_ref=item.raw_text,
            source_url_or_message_id=item.source_message_id,
        )
        return self.get_capture(data["id"])

    def get_capture(self, capture_id: str) -> Capture:
        with self.repo.connect() as conn:
            row = conn.execute("SELECT * FROM core_captures WHERE id=?", (capture_id,)).fetchone()
        if not row:
            raise KeyError(capture_id)
        return self._capture(row)

    def find_capture_by_source_message(self, source: str, source_message_id: str | None) -> Capture | None:
        if not source_message_id:
            return None
        with self.repo.connect() as conn:
            row = conn.execute(
                "SELECT * FROM core_captures WHERE source=? AND source_message_id=? LIMIT 1",
                (source, source_message_id),
            ).fetchone()
        return self._capture(row) if row else None

    def list_recent_captures(
        self,
        *,
        sender_id: str | None = None,
        limit: int = 3,
        exclude_id: str | None = None,
    ) -> list[Capture]:
        where: list[str] = []
        params: list[Any] = []
        if sender_id:
            where.append("sender_id=?")
            params.append(sender_id)
        if exclude_id:
            where.append("id!=?")
            params.append(exclude_id)
        sql = "SELECT * FROM core_captures"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.repo.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._capture(row) for row in rows]

    def update_capture_status(self, capture_id: str, status: ProcessedStatus) -> None:
        with self.repo.connect() as conn:
            conn.execute("UPDATE core_captures SET processed_status=? WHERE id=?", (status.value, capture_id))

    def create_evidence(
        self,
        *,
        capture_id: str,
        evidence_type: str,
        content_ref: str | None = None,
        original_filename: str | None = None,
        source_url_or_message_id: str | None = None,
    ) -> Evidence:
        data = {
            "id": new_id("evd"),
            "capture_id": capture_id,
            "evidence_type": evidence_type,
            "content_ref": content_ref,
            "original_filename": original_filename,
            "source_url_or_message_id": source_url_or_message_id,
            "created_at": utcnow_iso(),
        }
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT INTO evidences (
                    id, capture_id, evidence_type, content_ref, original_filename, source_url_or_message_id, created_at
                ) VALUES (
                    :id, :capture_id, :evidence_type, :content_ref, :original_filename, :source_url_or_message_id, :created_at
                )
                """,
                data,
            )
        return Evidence(**self._parse_datetimes(data, {"created_at"}))

    def create_action_item(self, payload: dict[str, Any]) -> ActionItem:
        now = utcnow_iso()
        data = {
            "id": new_id("task"),
            "title": payload["title"],
            "description": payload.get("description"),
            "status": payload.get("status", ItemStatus.active.value),
            "priority": payload.get("priority", "P3"),
            "due_at": self._iso(payload.get("due_at")),
            "estimated_minutes": payload.get("estimated_minutes"),
            "project_id": payload.get("project_id"),
            "person_id": payload.get("person_id"),
            "source_capture_id": payload.get("source_capture_id"),
            "confidence": float(payload.get("confidence", 0.7)),
            "created_at": now,
            "updated_at": now,
        }
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT INTO action_items (
                    id, title, description, status, priority, due_at, estimated_minutes, project_id,
                    person_id, source_capture_id, confidence, created_at, updated_at
                ) VALUES (
                    :id, :title, :description, :status, :priority, :due_at, :estimated_minutes, :project_id,
                    :person_id, :source_capture_id, :confidence, :created_at, :updated_at
                )
                """,
                data,
            )
        return self.get_action_item(data["id"])

    def get_action_item(self, item_id: str) -> ActionItem:
        with self.repo.connect() as conn:
            row = conn.execute("SELECT * FROM action_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise KeyError(item_id)
        return self._action_item(row)

    def update_action_item(self, item_id: str, patch: dict[str, Any]) -> ActionItem:
        allowed = {"title", "description", "status", "priority", "due_at", "estimated_minutes", "project_id", "person_id"}
        data = {k: self._iso(v) if k == "due_at" else v for k, v in patch.items() if k in allowed}
        if not data:
            return self.get_action_item(item_id)
        data["id"] = item_id
        data["updated_at"] = utcnow_iso()
        assignments = ", ".join(f"{key}=:{key}" for key in data if key != "id")
        with self.repo.connect() as conn:
            conn.execute(f"UPDATE action_items SET {assignments} WHERE id=:id", data)
        return self.get_action_item(item_id)

    def create_calendar_event(self, payload: dict[str, Any]) -> CalendarEvent:
        now = utcnow_iso()
        start = self._iso(payload["start_at"])
        end = self._iso(payload["end_at"])
        data = {
            "id": new_id("cal"),
            "title": payload["title"],
            "description": payload.get("description"),
            "start_at": start,
            "end_at": end,
            "location": payload.get("location"),
            "status": payload.get("status", ItemStatus.active.value),
            "source_capture_id": payload.get("source_capture_id"),
            "feishu_event_id": payload.get("feishu_event_id"),
            "plan_draft_id": payload.get("plan_draft_id"),
            "plan_item_id": payload.get("plan_item_id"),
            "confidence": float(payload.get("confidence", 0.7)),
            "created_at": now,
            "updated_at": now,
        }
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT INTO calendar_events (
                    id, title, description, start_at, end_at, location, status, source_capture_id,
                    feishu_event_id, plan_draft_id, plan_item_id, confidence, created_at, updated_at
                ) VALUES (
                    :id, :title, :description, :start_at, :end_at, :location, :status, :source_capture_id,
                    :feishu_event_id, :plan_draft_id, :plan_item_id, :confidence, :created_at, :updated_at
                )
                """,
                data,
            )
        return self.get_calendar_event(data["id"])

    def get_calendar_event(self, event_id: str) -> CalendarEvent:
        with self.repo.connect() as conn:
            row = conn.execute("SELECT * FROM calendar_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            raise KeyError(event_id)
        return self._calendar_event(row)

    def update_calendar_event(self, event_id: str, patch: dict[str, Any]) -> CalendarEvent:
        allowed = {
            "title",
            "description",
            "start_at",
            "end_at",
            "location",
            "status",
            "feishu_event_id",
            "plan_draft_id",
            "plan_item_id",
        }
        data = {k: self._iso(v) if k in {"start_at", "end_at"} else v for k, v in patch.items() if k in allowed}
        if not data:
            return self.get_calendar_event(event_id)
        data["id"] = event_id
        data["updated_at"] = utcnow_iso()
        assignments = ", ".join(f"{key}=:{key}" for key in data if key != "id")
        with self.repo.connect() as conn:
            conn.execute(f"UPDATE calendar_events SET {assignments} WHERE id=:id", data)
        return self.get_calendar_event(event_id)

    def create_schedule_block(self, payload: dict[str, Any]) -> ScheduleBlock:
        now = utcnow_iso()
        data = {
            "id": new_id("blk"),
            "title": payload["title"],
            "recurrence_rule": payload["recurrence_rule"],
            "start_time": payload["start_time"],
            "end_time": payload["end_time"],
            "timezone": payload.get("timezone", "Asia/Shanghai"),
            "status": payload.get("status", ItemStatus.active.value),
            "reminder_enabled": 1 if payload.get("reminder_enabled", True) else 0,
            "source_capture_id": payload.get("source_capture_id"),
            "feishu_event_id": payload.get("feishu_event_id"),
            "created_at": now,
            "updated_at": now,
        }
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT INTO schedule_blocks (
                    id, title, recurrence_rule, start_time, end_time, timezone, status, reminder_enabled, source_capture_id, feishu_event_id, created_at, updated_at
                ) VALUES (
                    :id, :title, :recurrence_rule, :start_time, :end_time, :timezone, :status, :reminder_enabled, :source_capture_id, :feishu_event_id, :created_at, :updated_at
                )
                """,
                data,
            )
        return self.get_schedule_block(data["id"])

    def get_schedule_block(self, block_id: str) -> ScheduleBlock:
        with self.repo.connect() as conn:
            row = conn.execute("SELECT * FROM schedule_blocks WHERE id=?", (block_id,)).fetchone()
        if not row:
            raise KeyError(block_id)
        return self._schedule_block(row)

    def update_schedule_block(self, block_id: str, patch: dict[str, Any]) -> ScheduleBlock:
        allowed = {"title", "recurrence_rule", "start_time", "end_time", "timezone", "status", "feishu_event_id", "reminder_enabled"}
        data = {key: value for key, value in patch.items() if key in allowed and value is not None}
        if not data:
            return self.get_schedule_block(block_id)
        if "reminder_enabled" in data:
            data["reminder_enabled"] = 1 if data["reminder_enabled"] else 0
        data["id"] = block_id
        data["updated_at"] = utcnow_iso()
        assignments = ", ".join(f"{key}=:{key}" for key in data if key != "id")
        with self.repo.connect() as conn:
            conn.execute(f"UPDATE schedule_blocks SET {assignments} WHERE id=:id", data)
        return self.get_schedule_block(block_id)

    def list_action_items(self, *, start: datetime | None = None, end: datetime | None = None) -> list[ActionItem]:
        rows = self._list_time_range("action_items", "due_at", start, end)
        return [self._action_item(row) for row in rows]

    def find_action_items(self, query: str, *, include_done: bool = False) -> list[ActionItem]:
        token = f"%{query.strip()}%"
        where = ["title LIKE ?"]
        params: list[Any] = [token]
        if not include_done:
            where.append("status NOT IN (?, ?)")
            params.extend([ItemStatus.done.value, ItemStatus.canceled.value])
        with self.repo.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM action_items WHERE {' AND '.join(where)} ORDER BY due_at ASC, created_at DESC",
                params,
            ).fetchall()
        return [self._action_item(row) for row in rows]

    def list_calendar_events(self, *, start: datetime | None = None, end: datetime | None = None) -> list[CalendarEvent]:
        rows = self._list_time_range("calendar_events", "start_at", start, end)
        return [self._calendar_event(row) for row in rows]

    def list_schedule_blocks(self) -> list[ScheduleBlock]:
        with self.repo.connect() as conn:
            rows = conn.execute("SELECT * FROM schedule_blocks WHERE status!='canceled' ORDER BY created_at DESC").fetchall()
        return [self._schedule_block(row) for row in rows]

    def find_schedule_blocks(self, query: str) -> list[ScheduleBlock]:
        token = f"%{query.strip()}%"
        with self.repo.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM schedule_blocks WHERE status!='canceled' AND title LIKE ? ORDER BY created_at DESC",
                (token,),
            ).fetchall()
        return [self._schedule_block(row) for row in rows]

    def find_calendar_events(self, query: str) -> list[CalendarEvent]:
        token = f"%{query.strip()}%"
        with self.repo.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM calendar_events WHERE status!='canceled' AND title LIKE ? ORDER BY start_at ASC",
                (token,),
            ).fetchall()
        return [self._calendar_event(row) for row in rows]

    def create_plan_draft(
        self,
        *,
        kind: str,
        title: str,
        payload: dict[str, Any] | None = None,
        missing_fields: list[str] | None = None,
        status: str = PlanDraftStatus.refining.value,
        source_capture_id: str | None = None,
        sender_id: str | None = None,
        confidence: float = 0.5,
    ) -> PlanDraft:
        now = utcnow_iso()
        data = {
            "id": new_id("plan"),
            "kind": kind,
            "status": status,
            "title": title,
            "payload_json": _dumps(payload or {}),
            "missing_fields_json": _dumps(missing_fields or []),
            "source_capture_id": source_capture_id,
            "sender_id": sender_id,
            "confidence": float(confidence),
            "created_at": now,
            "updated_at": now,
        }
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT INTO plan_drafts (
                    id, kind, status, title, payload_json, missing_fields_json,
                    source_capture_id, sender_id, confidence, created_at, updated_at
                ) VALUES (
                    :id, :kind, :status, :title, :payload_json, :missing_fields_json,
                    :source_capture_id, :sender_id, :confidence, :created_at, :updated_at
                )
                """,
                data,
            )
        return self.get_plan_draft(data["id"])

    def get_plan_draft(self, plan_id: str) -> PlanDraft:
        with self.repo.connect() as conn:
            row = conn.execute("SELECT * FROM plan_drafts WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise KeyError(plan_id)
        return self._plan_draft(row)

    def update_plan_draft(self, plan_id: str, patch: dict[str, Any]) -> PlanDraft:
        allowed = {
            "kind",
            "status",
            "title",
            "payload",
            "missing_fields",
            "source_capture_id",
            "sender_id",
            "confidence",
        }
        data: dict[str, Any] = {"id": plan_id, "updated_at": utcnow_iso()}
        for key, value in patch.items():
            if key not in allowed:
                continue
            if key == "payload":
                data["payload_json"] = _dumps(value or {})
            elif key == "missing_fields":
                data["missing_fields_json"] = _dumps(value or [])
            else:
                data[key] = value
        if len(data) == 2:
            return self.get_plan_draft(plan_id)
        assignments = ", ".join(f"{key}=:{key}" for key in data if key != "id")
        with self.repo.connect() as conn:
            conn.execute(f"UPDATE plan_drafts SET {assignments} WHERE id=:id", data)
        return self.get_plan_draft(plan_id)

    def list_plan_drafts(
        self,
        *,
        sender_id: str | None = None,
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        limit: int = 10,
    ) -> list[PlanDraft]:
        where: list[str] = []
        params: list[Any] = []
        if sender_id:
            where.append("sender_id=?")
            params.append(sender_id)
        if kinds:
            where.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if statuses:
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        sql = "SELECT * FROM plan_drafts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.repo.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._plan_draft(row) for row in rows]

    def get_latest_plan_draft(
        self,
        *,
        sender_id: str | None = None,
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> PlanDraft | None:
        drafts = self.list_plan_drafts(sender_id=sender_id, kinds=kinds, statuses=statuses, limit=1)
        return drafts[0] if drafts else None

    def create_agent_run(
        self,
        *,
        capture_id: str | None,
        provider: str,
        model: str | None,
        input_json: dict[str, Any],
    ) -> AgentRun:
        data = {
            "id": new_id("arun"),
            "capture_id": capture_id,
            "provider": provider,
            "model": model,
            "input_json": _dumps(input_json),
            "output_json": _dumps({}),
            "tool_calls_json": _dumps([]),
            "latency_ms": None,
            "status": RunStatus.running.value,
            "error": None,
            "created_at": utcnow_iso(),
        }
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT INTO core_agent_runs (
                    id, capture_id, provider, model, input_json, output_json, tool_calls_json,
                    latency_ms, status, error, created_at
                ) VALUES (
                    :id, :capture_id, :provider, :model, :input_json, :output_json, :tool_calls_json,
                    :latency_ms, :status, :error, :created_at
                )
                """,
                data,
            )
        return self.get_agent_run(data["id"])

    def complete_agent_run(
        self,
        run_id: str,
        *,
        output_json: dict[str, Any],
        tool_calls_json: list[dict[str, Any]],
        latency_ms: int,
    ) -> None:
        with self.repo.connect() as conn:
            conn.execute(
                """
                UPDATE core_agent_runs
                SET output_json=?, tool_calls_json=?, latency_ms=?, status=?, error=NULL
                WHERE id=?
                """,
                (_dumps(output_json), _dumps(tool_calls_json), latency_ms, RunStatus.done.value, run_id),
            )

    def fail_agent_run(self, run_id: str, error: str, latency_ms: int | None = None) -> None:
        with self.repo.connect() as conn:
            conn.execute(
                "UPDATE core_agent_runs SET status=?, error=?, latency_ms=? WHERE id=?",
                (RunStatus.failed.value, error, latency_ms, run_id),
            )

    def get_agent_run(self, run_id: str) -> AgentRun:
        with self.repo.connect() as conn:
            row = conn.execute("SELECT * FROM core_agent_runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return self._agent_run(row)

    def list_agent_runs(self, limit: int = 20) -> list[AgentRun]:
        with self.repo.connect() as conn:
            rows = conn.execute("SELECT * FROM core_agent_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._agent_run(row) for row in rows]

    def create_tool_run(
        self,
        *,
        agent_run_id: str | None,
        tool_name: str,
        input_json: dict[str, Any],
        output_json: dict[str, Any] | None = None,
        status: RunStatus = RunStatus.done,
        error: str | None = None,
    ) -> ToolRun:
        data = {
            "id": new_id("trun"),
            "agent_run_id": agent_run_id,
            "tool_name": tool_name,
            "input_json": _dumps(input_json),
            "output_json": _dumps(output_json or {}),
            "status": status.value,
            "error": error,
            "created_at": utcnow_iso(),
        }
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT INTO tool_runs (id, agent_run_id, tool_name, input_json, output_json, status, error, created_at)
                VALUES (:id, :agent_run_id, :tool_name, :input_json, :output_json, :status, :error, :created_at)
                """,
                data,
            )
        return self._tool_run(data)

    def list_tool_runs(self, limit: int = 20) -> list[ToolRun]:
        with self.repo.connect() as conn:
            rows = conn.execute("SELECT * FROM tool_runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._tool_run(row) for row in rows]

    def create_confirmation(
        self,
        *,
        agent_run_id: str | None,
        confirmation_type: str,
        proposed_tool_calls_json: list[dict[str, Any]],
        sender_id: str | None,
        expires_at: datetime | None = None,
        feishu_card_id: str | None = None,
    ) -> Confirmation:
        data = {
            "id": new_id("conf"),
            "agent_run_id": agent_run_id,
            "confirmation_type": confirmation_type,
            "proposed_tool_calls_json": _dumps(proposed_tool_calls_json),
            "status": ConfirmationStatus.pending.value,
            "expires_at": self._iso(expires_at or (datetime.now(UTC) + timedelta(hours=24))),
            "feishu_card_id": feishu_card_id,
            "created_at": utcnow_iso(),
            "resolved_at": None,
            "sender_id": sender_id,
        }
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT INTO confirmations (
                    id, agent_run_id, confirmation_type, proposed_tool_calls_json, status,
                    expires_at, feishu_card_id, created_at, resolved_at, sender_id
                ) VALUES (
                    :id, :agent_run_id, :confirmation_type, :proposed_tool_calls_json, :status,
                    :expires_at, :feishu_card_id, :created_at, :resolved_at, :sender_id
                )
                """,
                data,
            )
        return self.get_confirmation(data["id"])

    def update_confirmation_card_id(self, confirmation_id: str, feishu_card_id: str | None) -> Confirmation:
        with self.repo.connect() as conn:
            conn.execute("UPDATE confirmations SET feishu_card_id=? WHERE id=?", (feishu_card_id, confirmation_id))
        return self.get_confirmation(confirmation_id)

    def update_confirmation_payload(
        self,
        confirmation_id: str,
        proposed_tool_calls_json: list[dict[str, Any]],
        *,
        confirmation_type: str | None = None,
    ) -> Confirmation:
        with self.repo.connect() as conn:
            if confirmation_type:
                conn.execute(
                    "UPDATE confirmations SET proposed_tool_calls_json=?, confirmation_type=? WHERE id=?",
                    (_dumps(proposed_tool_calls_json), confirmation_type, confirmation_id),
                )
            else:
                conn.execute(
                    "UPDATE confirmations SET proposed_tool_calls_json=? WHERE id=?",
                    (_dumps(proposed_tool_calls_json), confirmation_id),
                )
        return self.get_confirmation(confirmation_id)

    def list_pending_confirmations(self, sender_id: str | None = None, limit: int = 10) -> list[Confirmation]:
        with self.repo.connect() as conn:
            if sender_id:
                rows = conn.execute(
                    """
                    SELECT * FROM confirmations
                    WHERE sender_id=? AND status=?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (sender_id, ConfirmationStatus.pending.value, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM confirmations WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (ConfirmationStatus.pending.value, limit),
                ).fetchall()
        return [self._confirmation(row) for row in rows]

    def get_pending_confirmation(self, sender_id: str | None) -> Confirmation | None:
        with self.repo.connect() as conn:
            if sender_id:
                row = conn.execute(
                    """
                    SELECT * FROM confirmations
                    WHERE sender_id=? AND status=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (sender_id, ConfirmationStatus.pending.value),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM confirmations WHERE status=? ORDER BY created_at DESC LIMIT 1",
                    (ConfirmationStatus.pending.value,),
                ).fetchone()
        return self._confirmation(row) if row else None

    def resolve_confirmation(self, confirmation_id: str) -> Confirmation:
        with self.repo.connect() as conn:
            conn.execute(
                "UPDATE confirmations SET status=?, resolved_at=? WHERE id=?",
                (ConfirmationStatus.resolved.value, utcnow_iso(), confirmation_id),
            )
        return self.get_confirmation(confirmation_id)

    def cancel_confirmation(self, confirmation_id: str) -> Confirmation:
        with self.repo.connect() as conn:
            conn.execute(
                "UPDATE confirmations SET status=?, resolved_at=? WHERE id=?",
                (ConfirmationStatus.canceled.value, utcnow_iso(), confirmation_id),
            )
        return self.get_confirmation(confirmation_id)

    def expire_confirmation(self, confirmation_id: str) -> Confirmation:
        with self.repo.connect() as conn:
            conn.execute(
                "UPDATE confirmations SET status=?, resolved_at=? WHERE id=?",
                (ConfirmationStatus.expired.value, utcnow_iso(), confirmation_id),
            )
        return self.get_confirmation(confirmation_id)

    def get_confirmation(self, confirmation_id: str) -> Confirmation:
        with self.repo.connect() as conn:
            row = conn.execute("SELECT * FROM confirmations WHERE id=?", (confirmation_id,)).fetchone()
        if not row:
            raise KeyError(confirmation_id)
        return self._confirmation(row)

    def _list_time_range(self, table: str, column: str, start: datetime | None, end: datetime | None):
        where = ["status!='canceled'"]
        params: list[Any] = []
        if start:
            where.append(f"{column}>=?")
            params.append(start.isoformat())
        if end:
            where.append(f"{column}<?")
            params.append(end.isoformat())
        sql = f"SELECT * FROM {table} WHERE {' AND '.join(where)} ORDER BY {column} ASC"
        with self.repo.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def _iso(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _parse_datetimes(self, data: dict[str, Any], fields: set[str]) -> dict[str, Any]:
        out = dict(data)
        for field in fields:
            if out.get(field):
                out[field] = parse_dt(out[field])
        return out

    def _capture(self, row) -> Capture:
        data = dict(row)
        data["attachment_refs"] = _loads(data.get("attachment_refs"), [])
        return Capture(**self._parse_datetimes(data, {"received_at", "created_at"}))

    def _action_item(self, row) -> ActionItem:
        return ActionItem(**self._parse_datetimes(dict(row), {"due_at", "created_at", "updated_at"}))

    def _calendar_event(self, row) -> CalendarEvent:
        return CalendarEvent(**self._parse_datetimes(dict(row), {"start_at", "end_at", "created_at", "updated_at"}))

    def _schedule_block(self, row) -> ScheduleBlock:
        data = self._parse_datetimes(dict(row), {"created_at", "updated_at"})
        data["reminder_enabled"] = bool(data.get("reminder_enabled", True))
        return ScheduleBlock(**data)

    def _plan_draft(self, row) -> PlanDraft:
        data = dict(row)
        data["payload"] = _loads(data.pop("payload_json", None), {})
        data["missing_fields"] = _loads(data.pop("missing_fields_json", None), [])
        return PlanDraft(**self._parse_datetimes(data, {"created_at", "updated_at"}))

    def _agent_run(self, row) -> AgentRun:
        data = dict(row)
        data["input_json"] = _loads(data.get("input_json"), {})
        data["output_json"] = _loads(data.get("output_json"), {})
        data["tool_calls_json"] = _loads(data.get("tool_calls_json"), [])
        return AgentRun(**self._parse_datetimes(data, {"created_at"}))

    def _tool_run(self, row) -> ToolRun:
        data = dict(row)
        data["input_json"] = _loads(data.get("input_json"), {})
        data["output_json"] = _loads(data.get("output_json"), {})
        return ToolRun(**self._parse_datetimes(data, {"created_at"}))

    def _confirmation(self, row) -> Confirmation:
        data = dict(row)
        data["proposed_tool_calls_json"] = _loads(data.get("proposed_tool_calls_json"), [])
        return Confirmation(**self._parse_datetimes(data, {"expires_at", "created_at", "resolved_at"}))
