from __future__ import annotations

from typing import Any

from app.core.decision_schemas import AssistantDecision
from app.core.schemas import ConfirmationStatus, PlanDraftStatus
from app.core.store import StateStore


class DecisionPolicyViolation(RuntimeError):
    pass


class DecisionPolicy:
    NO_OPERATION_ACTIONS = {
        "reply",
        "ask_clarification",
        "explain_proposal",
        "regenerate_proposal_card",
        "query",
        "create_proposal",
        "refine_proposal",
        "resolve_confirmation",
    }
    ACTIVE_PLAN_STATUSES = {
        PlanDraftStatus.refining.value,
        PlanDraftStatus.ready_for_schedule.value,
        PlanDraftStatus.schedule_pending.value,
    }
    ALLOWED_PROPOSAL_PATCH_FIELDS = {
        "kind",
        "status",
        "user_goal",
        "context_summary",
        "ai_assumptions",
        "missing_info",
        "candidate_plans",
        "schedule_preview",
        "risks",
        "next_step_suggestion",
        "confidence",
        "title",
        "details",
        "method",
        "preferred_time",
        "session_minutes",
        "frequency",
        "byday",
        "duration_days",
        "planned_events",
    }
    FORBIDDEN_PATCH_FIELDS = {"raw_text", "latest_user_reply"}

    def validate(
        self,
        decision: AssistantDecision,
        *,
        store: StateStore | None = None,
        sender_id: str | None = None,
        request: dict[str, Any] | None = None,
    ) -> None:
        if decision.action == "create_proposal" and decision.proposal is None:
            raise DecisionPolicyViolation("create_proposal requires proposal")
        if decision.action == "refine_proposal" and decision.proposal_patch is None:
            raise DecisionPolicyViolation("refine_proposal requires proposal_patch")
        if decision.action == "resolve_confirmation" and decision.confirmation_action is None:
            raise DecisionPolicyViolation("resolve_confirmation requires confirmation_action")
        if decision.action == "prepare_tool_confirmation" and not decision.candidate_operations:
            raise DecisionPolicyViolation("prepare_tool_confirmation requires candidate_operations")

        if decision.action != "prepare_tool_confirmation" and decision.candidate_operations:
            raise DecisionPolicyViolation(f"{decision.action} cannot include candidate_operations")

        for operation in decision.candidate_operations:
            if not operation.requires_confirmation:
                raise DecisionPolicyViolation(f"{operation.operation} must require confirmation")

        if decision.action == "refine_proposal" and decision.proposal_patch is not None:
            self._validate_proposal_patch(decision, store=store, sender_id=sender_id)

        if decision.action == "resolve_confirmation" and decision.confirmation_action is not None:
            self._validate_confirmation_action(decision, store=store, sender_id=sender_id, request=request)

    def _validate_proposal_patch(
        self,
        decision: AssistantDecision,
        *,
        store: StateStore | None,
        sender_id: str | None,
    ) -> None:
        patch = decision.proposal_patch
        if patch is None:
            return
        field_names = set(patch.fields.keys())
        forbidden = field_names & self.FORBIDDEN_PATCH_FIELDS
        if forbidden:
            raise DecisionPolicyViolation(f"proposal_patch contains forbidden fields: {sorted(forbidden)}")
        unknown = field_names - self.ALLOWED_PROPOSAL_PATCH_FIELDS
        if unknown:
            raise DecisionPolicyViolation(f"proposal_patch contains unknown fields: {sorted(unknown)}")

        details = patch.fields.get("details")
        if isinstance(details, dict):
            nested_forbidden = set(details.keys()) & self.FORBIDDEN_PATCH_FIELDS
            if nested_forbidden:
                raise DecisionPolicyViolation(f"proposal_patch contains forbidden fields: {sorted(nested_forbidden)}")

        if store is None:
            return
        try:
            draft = store.get_plan_draft(patch.plan_draft_id)
        except KeyError as exc:
            raise DecisionPolicyViolation("refine_proposal requires existing active PlanDraft") from exc
        draft_status = draft.status.value if hasattr(draft.status, "value") else str(draft.status)
        if draft_status not in self.ACTIVE_PLAN_STATUSES:
            raise DecisionPolicyViolation("refine_proposal requires existing active PlanDraft")
        if sender_id and draft.sender_id and draft.sender_id != sender_id:
            raise DecisionPolicyViolation("refine_proposal PlanDraft sender mismatch")

    def _validate_confirmation_action(
        self,
        decision: AssistantDecision,
        *,
        store: StateStore | None,
        sender_id: str | None,
        request: dict[str, Any] | None,
    ) -> None:
        action = decision.confirmation_action
        if action is None:
            return
        if store is not None:
            if action.confirmation_id:
                try:
                    confirmation = store.get_confirmation(action.confirmation_id)
                except KeyError as exc:
                    raise DecisionPolicyViolation("resolve_confirmation references missing pending confirmation") from exc
                if confirmation.status != ConfirmationStatus.pending:
                    raise DecisionPolicyViolation("resolve_confirmation references non-pending confirmation")
                if sender_id and confirmation.sender_id and confirmation.sender_id != sender_id:
                    raise DecisionPolicyViolation("resolve_confirmation confirmation sender mismatch")
                return
            pending = store.list_pending_confirmations(sender_id=sender_id, limit=1)
            if not pending:
                raise DecisionPolicyViolation("resolve_confirmation requires a pending confirmation")
            return
        if action.confirmation_id:
            return
        if request is None:
            return
        pending_confirmations = request.get("pending_confirmations")
        if not pending_confirmations:
            raise DecisionPolicyViolation("resolve_confirmation requires a pending confirmation")
