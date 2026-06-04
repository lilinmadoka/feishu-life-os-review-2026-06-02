from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.core.observability.schemas import (
    StateDiff,
    TraceArtifact,
    TraceDetail,
    TraceEvent,
    TraceRecord,
    TraceSpan,
)
from app.database import Repository, parse_dt


def _dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class SQLiteTraceStore:
    def __init__(self, repo: Repository):
        self.repo = repo

    def migrate(self) -> None:
        with self.repo.connect() as conn:
            conn.executescript(OBSERVABILITY_SQL)

    def create_trace(self, trace: TraceRecord) -> None:
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO observability_traces (
                    trace_id, workflow_type, root_entity_type, root_entity_id, capture_id,
                    agent_run_id, sender_hash, status, summary, privacy_mode, started_at,
                    ended_at, duration_ms, attrs_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace.trace_id,
                    trace.workflow_type,
                    trace.root_entity_type,
                    trace.root_entity_id,
                    trace.capture_id,
                    trace.agent_run_id,
                    trace.sender_hash,
                    trace.status,
                    trace.summary,
                    trace.privacy_mode,
                    _iso(trace.started_at),
                    _iso(trace.ended_at),
                    trace.duration_ms,
                    _dumps(trace.attrs),
                ),
            )

    def update_trace(
        self,
        trace_id: str,
        *,
        status: str | None = None,
        ended_at: datetime | None = None,
        duration_ms: int | None = None,
        summary: str | None = None,
        capture_id: str | None = None,
        agent_run_id: str | None = None,
        root_entity_type: str | None = None,
        root_entity_id: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("status", status),
            ("ended_at", _iso(ended_at) if ended_at else None),
            ("duration_ms", duration_ms),
            ("summary", summary),
            ("capture_id", capture_id),
            ("agent_run_id", agent_run_id),
            ("root_entity_type", root_entity_type),
            ("root_entity_id", root_entity_id),
            ("attrs_json", _dumps(attrs) if attrs is not None else None),
        ):
            if value is not None:
                assignments.append(f"{column}=?")
                values.append(value)
        if not assignments:
            return
        values.append(trace_id)
        with self.repo.connect() as conn:
            conn.execute(f"UPDATE observability_traces SET {', '.join(assignments)} WHERE trace_id=?", values)

    def create_span(self, span: TraceSpan) -> None:
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO observability_spans (
                    span_id, trace_id, parent_span_id, name, component, lane, status,
                    started_at, ended_at, duration_ms, attrs_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span.span_id,
                    span.trace_id,
                    span.parent_span_id,
                    span.name,
                    span.component,
                    span.lane,
                    span.status,
                    _iso(span.started_at),
                    _iso(span.ended_at),
                    span.duration_ms,
                    _dumps(span.attrs),
                ),
            )

    def update_span(
        self,
        span_id: str,
        *,
        status: str,
        ended_at: datetime,
        duration_ms: int,
        attrs: dict[str, Any] | None = None,
    ) -> None:
        with self.repo.connect() as conn:
            conn.execute(
                """
                UPDATE observability_spans
                SET status=?, ended_at=?, duration_ms=?, attrs_json=?
                WHERE span_id=?
                """,
                (status, _iso(ended_at), duration_ms, _dumps(attrs), span_id),
            )

    def create_event(self, event: TraceEvent) -> None:
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO observability_events (
                    event_id, trace_id, span_id, level, name, message, attrs_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.trace_id,
                    event.span_id,
                    event.level,
                    event.name,
                    event.message,
                    _dumps(event.attrs),
                    _iso(event.created_at),
                ),
            )

    def create_artifact(self, artifact: TraceArtifact) -> None:
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO observability_artifacts (
                    artifact_id, trace_id, span_id, kind, label, redaction,
                    payload_json, payload_hash, size_bytes, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.trace_id,
                    artifact.span_id,
                    artifact.kind,
                    artifact.label,
                    artifact.redaction,
                    _dumps(artifact.payload_json),
                    artifact.payload_hash,
                    artifact.size_bytes,
                    _iso(artifact.created_at),
                ),
            )

    def create_state_diff(self, diff: StateDiff) -> None:
        with self.repo.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO observability_state_diffs (
                    diff_id, trace_id, span_id, entity_type, entity_id, operation,
                    before_summary_json, after_summary_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    diff.diff_id,
                    diff.trace_id,
                    diff.span_id,
                    diff.entity_type,
                    diff.entity_id,
                    diff.operation,
                    _dumps(diff.before_summary),
                    _dumps(diff.after_summary),
                    _iso(diff.created_at),
                ),
            )

    def list_traces(self, *, limit: int = 50) -> list[TraceRecord]:
        with self.repo.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM observability_traces
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._trace(row) for row in rows]

    def get_trace(self, trace_id: str) -> TraceDetail | None:
        with self.repo.connect() as conn:
            trace_row = conn.execute("SELECT * FROM observability_traces WHERE trace_id=?", (trace_id,)).fetchone()
            if not trace_row:
                return None
            spans = conn.execute(
                "SELECT * FROM observability_spans WHERE trace_id=? ORDER BY started_at",
                (trace_id,),
            ).fetchall()
            events = conn.execute(
                "SELECT * FROM observability_events WHERE trace_id=? ORDER BY created_at",
                (trace_id,),
            ).fetchall()
            artifacts = conn.execute(
                "SELECT * FROM observability_artifacts WHERE trace_id=? ORDER BY created_at",
                (trace_id,),
            ).fetchall()
            diffs = conn.execute(
                "SELECT * FROM observability_state_diffs WHERE trace_id=? ORDER BY created_at",
                (trace_id,),
            ).fetchall()
        return TraceDetail(
            trace=self._trace(trace_row),
            spans=[self._span(row) for row in spans],
            events=[self._event(row) for row in events],
            artifacts=[self._artifact(row) for row in artifacts],
            state_diffs=[self._state_diff(row) for row in diffs],
        )

    def _trace(self, row: Any) -> TraceRecord:
        return TraceRecord(
            trace_id=row["trace_id"],
            workflow_type=row["workflow_type"],
            root_entity_type=row["root_entity_type"],
            root_entity_id=row["root_entity_id"],
            capture_id=row["capture_id"],
            agent_run_id=row["agent_run_id"],
            sender_hash=row["sender_hash"],
            status=row["status"],
            started_at=parse_dt(row["started_at"]),
            ended_at=parse_dt(row["ended_at"]) if row["ended_at"] else None,
            duration_ms=row["duration_ms"],
            summary=row["summary"],
            privacy_mode=row["privacy_mode"],
            attrs=_loads(row["attrs_json"]),
        )

    def _span(self, row: Any) -> TraceSpan:
        return TraceSpan(
            span_id=row["span_id"],
            trace_id=row["trace_id"],
            parent_span_id=row["parent_span_id"],
            name=row["name"],
            component=row["component"],
            lane=row["lane"],
            status=row["status"],
            started_at=parse_dt(row["started_at"]),
            ended_at=parse_dt(row["ended_at"]) if row["ended_at"] else None,
            duration_ms=row["duration_ms"],
            attrs=_loads(row["attrs_json"]),
        )

    def _event(self, row: Any) -> TraceEvent:
        return TraceEvent(
            event_id=row["event_id"],
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            level=row["level"],
            name=row["name"],
            message=row["message"],
            attrs=_loads(row["attrs_json"]),
            created_at=parse_dt(row["created_at"]),
        )

    def _artifact(self, row: Any) -> TraceArtifact:
        return TraceArtifact(
            artifact_id=row["artifact_id"],
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            kind=row["kind"],
            label=row["label"],
            redaction=row["redaction"],
            payload_json=_loads(row["payload_json"]),
            payload_hash=row["payload_hash"],
            size_bytes=row["size_bytes"],
            created_at=parse_dt(row["created_at"]),
        )

    def _state_diff(self, row: Any) -> StateDiff:
        return StateDiff(
            diff_id=row["diff_id"],
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            entity_type=row["entity_type"],
            entity_id=row["entity_id"],
            operation=row["operation"],
            before_summary=_loads(row["before_summary_json"]),
            after_summary=_loads(row["after_summary_json"]),
            created_at=parse_dt(row["created_at"]),
        )


OBSERVABILITY_SQL = """
CREATE TABLE IF NOT EXISTS observability_traces (
  trace_id TEXT PRIMARY KEY,
  workflow_type TEXT NOT NULL,
  root_entity_type TEXT,
  root_entity_id TEXT,
  capture_id TEXT,
  agent_run_id TEXT,
  sender_hash TEXT,
  status TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  privacy_mode TEXT NOT NULL DEFAULT 'redacted',
  started_at TEXT NOT NULL,
  ended_at TEXT,
  duration_ms INTEGER,
  attrs_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS observability_spans (
  span_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  parent_span_id TEXT,
  name TEXT NOT NULL,
  component TEXT NOT NULL,
  lane TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  duration_ms INTEGER,
  attrs_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS observability_events (
  event_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  span_id TEXT,
  level TEXT NOT NULL,
  name TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  attrs_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observability_artifacts (
  artifact_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  span_id TEXT,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  redaction TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  payload_hash TEXT,
  size_bytes INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observability_state_diffs (
  diff_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  span_id TEXT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  before_summary_json TEXT NOT NULL DEFAULT '{}',
  after_summary_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observability_traces_started_at ON observability_traces(started_at);
CREATE INDEX IF NOT EXISTS idx_observability_traces_capture ON observability_traces(capture_id);
CREATE INDEX IF NOT EXISTS idx_observability_spans_trace ON observability_spans(trace_id, started_at);
CREATE INDEX IF NOT EXISTS idx_observability_events_trace ON observability_events(trace_id, created_at);
"""
