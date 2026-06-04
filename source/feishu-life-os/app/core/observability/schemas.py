from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class TraceRecord(BaseModel):
    trace_id: str
    workflow_type: str
    root_entity_type: str | None = None
    root_entity_id: str | None = None
    capture_id: str | None = None
    agent_run_id: str | None = None
    sender_hash: str | None = None
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None
    summary: str = ""
    privacy_mode: str = "redacted"
    attrs: dict[str, Any] = Field(default_factory=dict)


class TraceSpan(BaseModel):
    span_id: str
    trace_id: str
    parent_span_id: str | None = None
    name: str
    component: str
    lane: str
    status: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)


class TraceEvent(BaseModel):
    event_id: str
    trace_id: str
    span_id: str | None = None
    level: str
    name: str
    message: str = ""
    attrs: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TraceArtifact(BaseModel):
    artifact_id: str
    trace_id: str
    span_id: str | None = None
    kind: str
    label: str
    redaction: str
    payload_json: dict[str, Any] = Field(default_factory=dict)
    payload_hash: str | None = None
    size_bytes: int | None = None
    created_at: datetime


class StateDiff(BaseModel):
    diff_id: str
    trace_id: str
    span_id: str | None = None
    entity_type: str
    entity_id: str
    operation: str
    before_summary: dict[str, Any] = Field(default_factory=dict)
    after_summary: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TraceDetail(BaseModel):
    trace: TraceRecord
    spans: list[TraceSpan] = Field(default_factory=list)
    events: list[TraceEvent] = Field(default_factory=list)
    artifacts: list[TraceArtifact] = Field(default_factory=list)
    state_diffs: list[StateDiff] = Field(default_factory=list)
