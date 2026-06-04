from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.schemas import AssistantProposal, RiskLevel

DecisionAction = Literal[
    "reply",
    "ask_clarification",
    "create_proposal",
    "refine_proposal",
    "explain_proposal",
    "regenerate_proposal_card",
    "prepare_tool_confirmation",
    "resolve_confirmation",
    "query",
]

ProposalPatchType = Literal["merge_fields", "replace", "cancel", "pause", "explain_only"]

ConcreteOperationName = Literal[
    "create_task",
    "update_task",
    "complete_task",
    "cancel_task",
    "create_calendar_event",
    "update_calendar_event",
    "cancel_calendar_event",
    "create_schedule_block",
    "update_schedule_block",
    "disable_schedule_block_reminders",
    "cancel_schedule_block",
    "sync_feishu_task",
    "sync_feishu_calendar",
]

ConfirmationChoice = Literal["confirm", "cancel"]

UIActionName = Literal[
    "show_message",
    "show_proposal_card",
    "regenerate_proposal_card",
    "show_confirmation_card",
]


class ProposalPatch(BaseModel):
    plan_draft_id: str
    patch_type: ProposalPatchType
    fields: dict[str, Any] = Field(default_factory=dict)
    user_visible_summary: str = ""
    missing_info: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


class ConcreteOperation(BaseModel):
    operation: ConcreteOperationName
    risk_level: RiskLevel
    requires_confirmation: bool = True
    arguments: dict[str, Any] = Field(default_factory=dict)


class ConfirmationAction(BaseModel):
    action: ConfirmationChoice
    confirmation_id: str | None = None
    reason: str = ""


class UIAction(BaseModel):
    action: UIActionName
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AssistantDecision(BaseModel):
    decision_schema_version: int = 1
    action: DecisionAction
    confidence: float = Field(ge=0, le=1)
    reasoning_summary: str = ""
    reply_to_user: str = ""
    referenced_context: list[str] = Field(default_factory=list)
    proposal: AssistantProposal | None = None
    proposal_patch: ProposalPatch | None = None
    query: dict[str, Any] | None = None
    confirmation_action: ConfirmationAction | None = None
    candidate_operations: list[ConcreteOperation] = Field(default_factory=list)
    ui_action: UIAction | None = None
    uncertainty: list[str] = Field(default_factory=list)
