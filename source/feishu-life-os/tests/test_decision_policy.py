from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.decision_policy import DecisionPolicy, DecisionPolicyViolation
from app.core.decision_schemas import (
    AssistantDecision,
    ConcreteOperation,
    ConfirmationAction,
    ProposalPatch,
)
from app.core.schemas import (
    AgentToolCall,
    AssistantProposal,
    PlanDraftKind,
    PlanDraftStatus,
    RiskLevel,
)
from app.core.store import StateStore
from app.database import Repository


def build_store(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    return store


def create_draft(store: StateStore, *, status: str = PlanDraftStatus.refining.value) -> str:
    draft = store.create_plan_draft(
        kind=PlanDraftKind.long_term_schedule.value,
        title="长期复习",
        payload={"assistant_proposal": {"user_goal": "长期复习", "missing_info": ["频率"]}},
        missing_fields=["频率"],
        status=status,
        sender_id="ou_test",
        confidence=0.7,
    )
    return draft.id


def test_assistant_decision_reply_is_valid():
    decision = AssistantDecision(
        action="reply",
        confidence=0.9,
        reply_to_user="我可以把这张草案解释得更清楚。",
        referenced_context=["plan_draft:plan_1"],
    )

    DecisionPolicy().validate(decision)


def test_refine_proposal_requires_explicit_patch():
    decision = AssistantDecision(action="refine_proposal", confidence=0.8)

    with pytest.raises(DecisionPolicyViolation, match="proposal_patch"):
        DecisionPolicy().validate(decision)


def test_refine_proposal_with_patch_is_valid():
    decision = AssistantDecision(
        action="refine_proposal",
        confidence=0.86,
        proposal_patch=ProposalPatch(
            plan_draft_id="plan_1",
            patch_type="merge_fields",
            fields={"preferred_time": "08:00"},
            user_visible_summary="改为早上 8 点。",
            confidence=0.86,
        ),
    )

    DecisionPolicy().validate(decision)


def test_query_decision_cannot_carry_write_operations():
    decision = AssistantDecision(
        action="query",
        confidence=0.82,
        candidate_operations=[
            ConcreteOperation(
                operation="create_calendar_event",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={"title": "候选日程"},
            )
        ],
    )

    with pytest.raises(DecisionPolicyViolation, match="query cannot include candidate_operations"):
        DecisionPolicy().validate(decision)


def test_prepare_tool_confirmation_requires_confirmed_boundary():
    decision = AssistantDecision(
        action="prepare_tool_confirmation",
        confidence=0.88,
        candidate_operations=[
            ConcreteOperation(
                operation="create_calendar_event",
                risk_level=RiskLevel.medium,
                requires_confirmation=False,
                arguments={"title": "候选日程"},
            )
        ],
    )

    with pytest.raises(DecisionPolicyViolation, match="must require confirmation"):
        DecisionPolicy().validate(decision)


def test_prepare_tool_confirmation_with_confirmable_operation_is_valid():
    decision = AssistantDecision(
        action="prepare_tool_confirmation",
        confidence=0.88,
        candidate_operations=[
            ConcreteOperation(
                operation="create_calendar_event",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={"title": "候选日程"},
            )
        ],
    )

    DecisionPolicy().validate(decision)


def test_resolve_confirmation_requires_confirmation_action():
    decision = AssistantDecision(action="resolve_confirmation", confidence=0.91)

    with pytest.raises(DecisionPolicyViolation, match="confirmation_action"):
        DecisionPolicy().validate(decision)


def test_resolve_confirmation_with_action_is_valid():
    decision = AssistantDecision(
        action="resolve_confirmation",
        confidence=0.91,
        confirmation_action=ConfirmationAction(action="confirm", confirmation_id="conf_1"),
    )

    DecisionPolicy().validate(decision)


def test_create_proposal_requires_proposal_payload():
    decision = AssistantDecision(action="create_proposal", confidence=0.74)

    with pytest.raises(DecisionPolicyViolation, match="proposal"):
        DecisionPolicy().validate(decision)


def test_schema_rejects_unknown_decision_action():
    with pytest.raises(ValidationError):
        AssistantDecision(action="mutate_from_raw_text", confidence=0.8)


def test_create_proposal_with_payload_is_valid():
    decision = AssistantDecision(
        action="create_proposal",
        confidence=0.8,
        proposal=AssistantProposal(
            user_goal="锻炼身体",
            missing_info=["每次时长"],
            candidate_plans=[{"title": "锻炼身体"}],
        ),
    )

    DecisionPolicy().validate(decision)


def test_refine_proposal_rejects_unknown_patch_fields():
    decision = AssistantDecision(
        action="refine_proposal",
        confidence=0.86,
        proposal_patch=ProposalPatch(
            plan_draft_id="plan_1",
            patch_type="merge_fields",
            fields={"latest_user_reply": "不要把原文存进去"},
            confidence=0.86,
        ),
    )

    with pytest.raises(DecisionPolicyViolation, match="forbidden fields"):
        DecisionPolicy().validate(decision)


def test_refine_proposal_requires_active_draft_when_store_supplied(tmp_path):
    store = build_store(tmp_path)
    plan_id = create_draft(store, status=PlanDraftStatus.canceled.value)
    decision = AssistantDecision(
        action="refine_proposal",
        confidence=0.86,
        proposal_patch=ProposalPatch(
            plan_draft_id=plan_id,
            patch_type="merge_fields",
            fields={"preferred_time": "08:00"},
            confidence=0.86,
        ),
    )

    with pytest.raises(DecisionPolicyViolation, match="active PlanDraft"):
        DecisionPolicy().validate(decision, store=store, sender_id="ou_test")


def test_refine_proposal_accepts_active_draft_when_store_supplied(tmp_path):
    store = build_store(tmp_path)
    plan_id = create_draft(store)
    decision = AssistantDecision(
        action="refine_proposal",
        confidence=0.86,
        proposal_patch=ProposalPatch(
            plan_draft_id=plan_id,
            patch_type="merge_fields",
            fields={"preferred_time": "08:00"},
            confidence=0.86,
        ),
    )

    DecisionPolicy().validate(decision, store=store, sender_id="ou_test")


def test_resolve_confirmation_requires_pending_when_store_supplied(tmp_path):
    store = build_store(tmp_path)
    decision = AssistantDecision(
        action="resolve_confirmation",
        confidence=0.91,
        confirmation_action=ConfirmationAction(action="confirm"),
    )

    with pytest.raises(DecisionPolicyViolation, match="pending confirmation"):
        DecisionPolicy().validate(decision, store=store, sender_id="ou_test")


def test_resolve_confirmation_accepts_pending_when_store_supplied(tmp_path):
    store = build_store(tmp_path)
    call = AgentToolCall(
        tool_name="create_task_candidate",
        risk_level=RiskLevel.medium,
        requires_confirmation=True,
        arguments={"title": "候选任务"},
    )
    confirmation = store.create_confirmation(
        agent_run_id=None,
        confirmation_type="task_candidate",
        proposed_tool_calls_json=[call.model_dump(mode="json")],
        sender_id="ou_test",
    )
    decision = AssistantDecision(
        action="resolve_confirmation",
        confidence=0.91,
        confirmation_action=ConfirmationAction(action="confirm", confirmation_id=confirmation.id),
    )

    DecisionPolicy().validate(decision, store=store, sender_id="ou_test")
