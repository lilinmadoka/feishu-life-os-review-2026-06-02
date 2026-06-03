from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProcessedStatus(str, Enum):
    new = "new"
    processed = "processed"
    needs_review = "needs_review"
    failed = "failed"


class ItemStatus(str, Enum):
    candidate = "candidate"
    active = "active"
    done = "done"
    canceled = "canceled"


class ConfirmationStatus(str, Enum):
    pending = "pending"
    resolved = "resolved"
    expired = "expired"
    canceled = "canceled"


class PlanDraftStatus(str, Enum):
    refining = "refining"
    ready_for_schedule = "ready_for_schedule"
    schedule_pending = "schedule_pending"
    confirmed = "confirmed"
    canceled = "canceled"


class PlanDraftKind(str, Enum):
    habit = "habit"
    course_timetable = "course_timetable"
    long_term_schedule = "long_term_schedule"


class RunStatus(str, Enum):
    running = "running"
    done = "done"
    failed = "failed"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


AgentIntent = Literal[
    "query_today",
    "query_tomorrow",
    "query_week",
    "query_availability",
    "create_candidates",
    "update_existing",
    "complete_item",
    "schedule_blocks",
    "smalltalk",
    "unknown",
]

ToolName = Literal[
    "send_feishu_reply",
    "send_feishu_card",
    "ask_confirmation",
    "resolve_confirmation",
    "create_task_candidate",
    "confirm_task",
    "update_task",
    "complete_task",
    "cancel_task",
    "cancel_calendar_event",
    "query_tasks",
    "query_today",
    "query_tomorrow",
    "query_week",
    "query_pending_confirmations",
    "query_availability",
    "explain_time_budget_plan",
    "schedule_time_budget_plan",
    "start_plan_refinement",
    "refine_plan_draft",
    "generate_plan_schedule_confirmation",
    "confirm_plan_schedule",
    "start_habit_refinement",
    "refine_habit_plan",
    "schedule_habit_plan",
    "create_calendar_event_candidate",
    "confirm_calendar_event",
    "update_calendar_event",
    "check_conflicts",
    "create_schedule_block_candidates",
    "confirm_schedule_blocks",
    "update_schedule_block",
    "disable_schedule_block_reminders",
    "cancel_schedule_block",
    "query_schedule_blocks",
    "sync_feishu_task",
    "sync_feishu_calendar",
    "sync_bitable_audit",
    "record_agent_run",
    "record_tool_run",
    "list_recent_agent_runs",
    "list_recent_tool_runs",
]


class AgentToolCall(BaseModel):
    tool_name: ToolName
    risk_level: RiskLevel = RiskLevel.low
    requires_confirmation: bool = False
    arguments: dict[str, Any] = Field(default_factory=dict)


class AssistantProposal(BaseModel):
    kind: PlanDraftKind | str = PlanDraftKind.long_term_schedule
    status: PlanDraftStatus | str = PlanDraftStatus.refining
    user_goal: str
    context_summary: str = ""
    ai_assumptions: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    candidate_plans: list[dict[str, Any]] = Field(default_factory=list)
    schedule_preview: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_step_suggestion: str = ""
    confidence: float = Field(default=0.5, ge=0, le=1)


class AgentResponse(BaseModel):
    intent: AgentIntent
    confidence: float = Field(ge=0, le=1)
    reasoning_summary: str
    reply_to_user: str = ""
    assistant_proposal: AssistantProposal | None = None
    tool_calls: list[AgentToolCall] = Field(default_factory=list)


class CaptureIn(BaseModel):
    source: str
    source_message_id: str | None = None
    source_event_id: str | None = None
    sender_id: str | None = None
    chat_id: str | None = None
    content_type: str = "text"
    raw_text: str
    attachment_refs: list[dict[str, Any]] = Field(default_factory=list)
    received_at: datetime | None = None


class Capture(CaptureIn):
    id: str
    processed_status: ProcessedStatus
    created_at: datetime


class Evidence(BaseModel):
    id: str
    capture_id: str
    evidence_type: str
    content_ref: str | None = None
    original_filename: str | None = None
    source_url_or_message_id: str | None = None
    created_at: datetime


class ActionItem(BaseModel):
    id: str
    title: str
    description: str | None = None
    status: ItemStatus
    priority: str = "P3"
    due_at: datetime | None = None
    estimated_minutes: int | None = None
    project_id: str | None = None
    person_id: str | None = None
    source_capture_id: str | None = None
    confidence: float = 0.5
    created_at: datetime
    updated_at: datetime


class CalendarEvent(BaseModel):
    id: str
    title: str
    description: str | None = None
    start_at: datetime
    end_at: datetime
    location: str | None = None
    status: ItemStatus
    source_capture_id: str | None = None
    feishu_event_id: str | None = None
    plan_draft_id: str | None = None
    plan_item_id: str | None = None
    confidence: float = 0.5
    created_at: datetime
    updated_at: datetime


class ScheduleBlock(BaseModel):
    id: str
    title: str
    recurrence_rule: str
    start_time: str
    end_time: str
    timezone: str
    status: ItemStatus
    reminder_enabled: bool = True
    source_capture_id: str | None = None
    feishu_event_id: str | None = None
    created_at: datetime
    updated_at: datetime


class Reminder(BaseModel):
    id: str
    target_type: str
    target_id: str
    remind_at: datetime
    channel: str
    status: ItemStatus
    created_at: datetime


class Commitment(BaseModel):
    id: str
    title: str
    promised_to: str | None = None
    due_at: datetime | None = None
    linked_action_item_id: str | None = None
    source_capture_id: str | None = None
    status: ItemStatus
    created_at: datetime


class WaitingFor(BaseModel):
    id: str
    title: str
    waiting_for_person: str | None = None
    expected_at: datetime | None = None
    linked_project_id: str | None = None
    source_capture_id: str | None = None
    status: ItemStatus
    created_at: datetime


class Project(BaseModel):
    id: str
    name: str
    description: str | None = None
    status: ItemStatus
    created_at: datetime
    updated_at: datetime


class Person(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    role: str | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


class AgentRun(BaseModel):
    id: str
    capture_id: str | None = None
    provider: str
    model: str | None = None
    input_json: dict[str, Any] = Field(default_factory=dict)
    output_json: dict[str, Any] = Field(default_factory=dict)
    tool_calls_json: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int | None = None
    status: RunStatus
    error: str | None = None
    created_at: datetime


class ToolRun(BaseModel):
    id: str
    agent_run_id: str | None = None
    tool_name: str
    input_json: dict[str, Any] = Field(default_factory=dict)
    output_json: dict[str, Any] = Field(default_factory=dict)
    status: RunStatus
    error: str | None = None
    created_at: datetime


class Confirmation(BaseModel):
    id: str
    agent_run_id: str | None = None
    confirmation_type: str
    proposed_tool_calls_json: list[dict[str, Any]] = Field(default_factory=list)
    status: ConfirmationStatus
    expires_at: datetime | None = None
    feishu_card_id: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    sender_id: str | None = None


class PlanDraft(BaseModel):
    id: str
    kind: PlanDraftKind
    status: PlanDraftStatus
    title: str
    payload: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    source_capture_id: str | None = None
    sender_id: str | None = None
    confidence: float = 0.5
    created_at: datetime
    updated_at: datetime


class OrchestratorResult(BaseModel):
    capture_id: str
    agent_run_id: str
    reply_text: str
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    confirmation_id: str | None = None
    proposal_id: str | None = None
