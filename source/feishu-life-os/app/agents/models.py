from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AgentMessageType(str, Enum):
    text = "text"
    image = "image"
    file = "file"
    forwarded = "forwarded"
    unknown = "unknown"


class AgentIntent(str, Enum):
    capture = "capture"
    query = "query"
    update = "update"
    clarify = "clarify"
    review = "review"
    ignore = "ignore"
    system = "system"


class AgentToolName(str, Enum):
    send_feishu_reply = "send_feishu_reply"
    create_task = "create_task"
    query_today = "query_today"
    query_tomorrow = "query_tomorrow"
    query_overdue = "query_overdue"
    query_next_7_days = "query_next_7_days"
    update_task_status = "update_task_status"
    update_task_time = "update_task_time"
    ask_confirmation = "ask_confirmation"
    sync_bitable = "sync_bitable"
    sync_feishu_task = "sync_feishu_task"
    sync_feishu_calendar = "sync_feishu_calendar"


class AgentToolCall(BaseModel):
    name: AgentToolName
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentRequest(BaseModel):
    raw_text: str
    message_type: AgentMessageType
    open_id: str | None = None
    message_id: str | None = None
    capture_id: str | None = None
    recent_captures: list[dict[str, Any]] = Field(default_factory=list)
    recent_actions: list[dict[str, Any]] = Field(default_factory=list)
    today_summary: list[dict[str, Any]] = Field(default_factory=list)
    overdue_summary: list[dict[str, Any]] = Field(default_factory=list)
    pending_summary: list[dict[str, Any]] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    project_brief: str
    safety_rules: list[str] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class AgentResponse(BaseModel):
    intent: AgentIntent
    reply_text: str = ""
    tool_calls: list[AgentToolCall] = Field(default_factory=list)
    needs_confirmation: bool = False
    confidence: float = Field(default=0.0, ge=0, le=1)
    reason_summary: str = ""


class AgentToolResult(BaseModel):
    name: AgentToolName
    ok: bool
    result: dict[str, Any] = Field(default_factory=dict)
    reply_text: str | None = None
    error: str | None = None
    needs_confirmation: bool = False
