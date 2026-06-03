from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SourceType(str, Enum):
    manual = "manual"
    feishu_bot = "feishu_bot"
    feishu_event = "feishu_event"
    email = "email"
    chat = "chat"
    screenshot = "screenshot"
    learning_platform = "learning_platform"
    voice = "voice"
    notification = "notification"
    api = "api"


class CaptureStatus(str, Enum):
    new = "new"
    parsed = "parsed"
    needs_review = "needs_review"
    archived = "archived"


class ActionStatus(str, Enum):
    inbox = "inbox"
    planned = "planned"
    doing = "doing"
    waiting = "waiting"
    done = "done"
    canceled = "canceled"
    snoozed = "snoozed"


class ActionIntent(str, Enum):
    task = "task"
    event = "event"
    followup = "followup"
    waiting = "waiting"
    note = "note"
    habit = "habit"
    deadline = "deadline"


class Domain(str, Enum):
    school = "school"
    tutoring = "tutoring"
    study = "study"
    project = "project"
    communication = "communication"
    personal = "personal"
    other = "other"


class Priority(str, Enum):
    p0 = "P0"  # 今天必须处理 / 已经临期
    p1 = "P1"  # 24 小时内或强约定
    p2 = "P2"  # 3 天内或重要但可安排
    p3 = "P3"  # 未来/低风险


class Energy(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Attachment(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: str = Field(description="image, file, audio, url, etc.")
    url: str | None = None
    file_token: str | None = None
    name: str | None = None
    mime_type: str | None = None
    text_hint: str | None = None


class CaptureCreate(BaseModel):
    raw_text: str = Field(min_length=1, description="原始文本、OCR 文本、转发消息或口头记录转写")
    source_type: SourceType = SourceType.manual
    source_ref: str | None = Field(default=None, description="外部来源 id，如 message_id、email id")
    attachments: list[Attachment] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaptureRecord(CaptureCreate):
    id: str
    normalized_text: str
    status: CaptureStatus
    confidence: float = 0.0
    created_at: datetime
    updated_at: datetime


class ActionCreate(BaseModel):
    capture_id: str | None = None
    title: str = Field(min_length=1)
    description: str | None = None
    intent: ActionIntent = ActionIntent.task
    domain: Domain = Domain.other
    status: ActionStatus = ActionStatus.inbox
    priority: Priority = Priority.p3
    energy: Energy = Energy.medium
    due_at: datetime | None = None
    start_at: datetime | None = None
    remind_at: datetime | None = None
    estimated_minutes: int | None = None
    people: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    evidence_text: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActionRecord(ActionCreate):
    id: str
    feishu_task_guid: str | None = None
    feishu_record_id: str | None = None
    created_at: datetime
    updated_at: datetime


class ActionUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    intent: ActionIntent | None = None
    domain: Domain | None = None
    status: ActionStatus | None = None
    priority: Priority | None = None
    energy: Energy | None = None
    due_at: datetime | None = None
    start_at: datetime | None = None
    remind_at: datetime | None = None
    estimated_minutes: int | None = None
    people: list[str] | None = None
    projects: list[str] | None = None
    labels: list[str] | None = None
    evidence_text: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    metadata: dict[str, Any] | None = None
    feishu_task_guid: str | None = None
    feishu_record_id: str | None = None


class CaptureResponse(BaseModel):
    capture: CaptureRecord
    actions: list[ActionRecord]
    duplicate_action_ids: list[str] = Field(default_factory=list)


class ReviewResponse(BaseModel):
    date: str
    markdown: str
    sections: dict[str, list[ActionRecord]]


class SyncTarget(str, Enum):
    bitable = "bitable"
    task = "task"
    calendar = "calendar"
    webhook = "webhook"


class SyncEvent(BaseModel):
    id: str
    target: SyncTarget
    entity_type: str
    entity_id: str
    status: str
    request_payload: dict[str, Any] = Field(default_factory=dict)
    response_payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime


class ReviewJobStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class ReviewJobType(str, Enum):
    extraction_review = "extraction_review"
    sync_error = "sync_error"
    system_health = "system_health"


class ReviewJobCreate(BaseModel):
    job_type: ReviewJobType
    capture_id: str | None = None
    action_ids: list[str] = Field(default_factory=list)
    source_ref: str | None = None
    prompt: str


class ReviewJobRecord(ReviewJobCreate):
    id: str
    status: ReviewJobStatus
    result_json: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class ReviewJobComplete(BaseModel):
    result_json: dict[str, Any]


class ReviewJobFail(BaseModel):
    error: str
    result_json: dict[str, Any] = Field(default_factory=dict)


class AgentRunStatus(str, Enum):
    running = "running"
    done = "done"
    failed = "failed"


class AgentRunCreate(BaseModel):
    capture_id: str | None = None
    source_ref: str | None = None
    provider: str
    request_json: dict[str, Any] = Field(default_factory=dict)


class AgentRunRecord(AgentRunCreate):
    id: str
    status: AgentRunStatus
    response_json: dict[str, Any] = Field(default_factory=dict)
    tool_results_json: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime
    updated_at: datetime
