from __future__ import annotations

from copy import deepcopy
from typing import Any
from zoneinfo import ZoneInfo

from app.core.decision_schemas import AssistantDecision, ConcreteOperation, ProposalPatch
from app.core.feishu_native import FeishuNativeAdapter
from app.core.planner import PlannerOutcome
from app.core.schemas import (
    AgentResponse,
    AgentToolCall,
    AssistantProposal,
    PlanDraft,
    PlanDraftKind,
    PlanDraftStatus,
    RiskLevel,
)
from app.core.store import StateStore

DETAIL_PATCH_FIELDS = {"method", "preferred_time", "session_minutes", "frequency", "byday", "duration_days"}
PROPOSAL_TOP_LEVEL_FIELDS = {
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
}


class PlannerRuntime:
    """Applies validated AssistantDecision objects without parsing raw user text."""

    def __init__(
        self,
        store: StateStore,
        feishu: FeishuNativeAdapter,
        tz: ZoneInfo,
        *,
        legacy_adapter: Any | None = None,
    ):
        self.store = store
        self.feishu = feishu
        self.tz = tz
        self.legacy_adapter = legacy_adapter

    async def apply_decision(
        self,
        decision: AssistantDecision,
        request: dict[str, Any],
        *,
        agent_run_id: str,
        capture_id: str,
        sender_id: str | None,
    ) -> PlannerOutcome:
        legacy_response = self._legacy_response(decision)
        if legacy_response is not None:
            if self.legacy_adapter is None:
                return self._with_decision_meta(
                    PlannerOutcome(reply_text=legacy_response.reply_to_user),
                    decision,
                    legacy_adapter_used=True,
                )
            outcome = await self.legacy_adapter.plan_response(
                legacy_response,
                request,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
            return self._with_decision_meta(outcome, decision, legacy_adapter_used=True)

        if decision.action in {"reply", "query"}:
            return self._with_decision_meta(
                PlannerOutcome(reply_text=decision.reply_to_user or "已收到。"),
                decision,
            )
        if decision.action == "ask_clarification":
            reply = decision.reply_to_user
            if not reply and decision.proposal_patch:
                reply = decision.proposal_patch.user_visible_summary
            return self._with_decision_meta(
                PlannerOutcome(reply_text=reply or "我需要再确认一些细节。"),
                decision,
            )
        if decision.action in {"explain_proposal", "regenerate_proposal_card"}:
            return self._with_decision_meta(
                PlannerOutcome(reply_text=decision.reply_to_user or "这只是草案，不会在确认前写入日历或任务。"),
                decision,
            )
        if decision.action == "create_proposal" and decision.proposal is not None:
            return self._with_decision_meta(
                await self._create_proposal(decision.proposal, agent_run_id=agent_run_id, capture_id=capture_id, sender_id=sender_id),
                decision,
            )
        if decision.action == "refine_proposal" and decision.proposal_patch is not None:
            return self._with_decision_meta(
                await self._refine_proposal(decision.proposal_patch, agent_run_id=agent_run_id, sender_id=sender_id),
                decision,
            )
        if decision.action == "prepare_tool_confirmation":
            return self._with_decision_meta(
                PlannerOutcome(
                    tool_calls=[self._operation_to_tool_call(operation) for operation in decision.candidate_operations],
                    reply_text=decision.reply_to_user,
                ),
                decision,
            )
        if decision.action == "resolve_confirmation" and decision.confirmation_action is not None:
            args = {
                "action": decision.confirmation_action.action,
                "confirmation_id": decision.confirmation_action.confirmation_id,
                "agent_run_id": agent_run_id,
            }
            return self._with_decision_meta(
                PlannerOutcome(
                    tool_calls=[
                        AgentToolCall(
                            tool_name="resolve_confirmation",
                            risk_level=RiskLevel.low,
                            requires_confirmation=False,
                            arguments=args,
                        )
                    ],
                    reply_text=decision.reply_to_user,
                ),
                decision,
            )
        return self._with_decision_meta(
            PlannerOutcome(reply_text=decision.reply_to_user or "当前决策无法安全执行，已停止写操作。"),
            decision,
        )

    def _legacy_response(self, decision: AssistantDecision) -> AgentResponse | None:
        if not decision.query or not isinstance(decision.query, dict):
            return None
        payload = decision.query.get("legacy_agent_response")
        if not isinstance(payload, dict):
            return None
        return AgentResponse.model_validate(payload)

    async def _create_proposal(
        self,
        proposal: AssistantProposal,
        *,
        agent_run_id: str,
        capture_id: str,
        sender_id: str | None,
    ) -> PlannerOutcome:
        status = self._status_for_proposal(proposal)
        proposal = AssistantProposal.model_validate(
            {
                **proposal.model_dump(mode="json"),
                "status": status,
            }
        )
        draft = self.store.create_plan_draft(
            kind=self._draft_kind_value(proposal.kind),
            title=self._proposal_title(proposal),
            payload={"assistant_proposal": proposal.model_dump(mode="json"), "raw_text_history": []},
            missing_fields=list(proposal.missing_info),
            status=status,
            source_capture_id=capture_id,
            sender_id=sender_id,
            confidence=proposal.confidence,
        )
        card_result = await self.feishu.send_card(sender_id, self._proposal_card(proposal, draft.id))
        self.store.create_tool_run(
            agent_run_id=agent_run_id,
            tool_name="assistant_proposal",
            input_json={"plan_draft_id": draft.id, "runtime": "model_first"},
            output_json={"card": card_result},
        )
        return PlannerOutcome(
            reply_text=self._proposal_reply(proposal),
            proposal_id=draft.id,
            card_sent=True,
        )

    async def _refine_proposal(
        self,
        patch: ProposalPatch,
        *,
        agent_run_id: str,
        sender_id: str | None,
    ) -> PlannerOutcome:
        draft = self.store.get_plan_draft(patch.plan_draft_id)
        if patch.patch_type == "explain_only":
            return PlannerOutcome(reply_text=patch.user_visible_summary or "这条只是解释，不会修改草案。", proposal_id=draft.id)
        if patch.patch_type == "cancel":
            updated = self.store.update_plan_draft(
                draft.id,
                {"status": PlanDraftStatus.canceled.value, "payload": self._payload_with_status(draft, PlanDraftStatus.canceled.value)},
            )
            return PlannerOutcome(reply_text=patch.user_visible_summary or "已取消这个草案。", proposal_id=updated.id)
        if patch.patch_type == "pause":
            payload = deepcopy(draft.payload)
            payload["paused"] = True
            if patch.user_visible_summary:
                payload["pause_summary"] = patch.user_visible_summary
            updated = self.store.update_plan_draft(draft.id, {"payload": payload, "confidence": patch.confidence})
            return PlannerOutcome(reply_text=patch.user_visible_summary or "已暂停这个草案。", proposal_id=updated.id)

        proposal = self._proposal_from_draft(draft)
        proposal_data = proposal.model_dump(mode="json")
        if patch.patch_type == "replace":
            proposal_data = self._replacement_proposal_data(proposal_data, patch.fields)
        else:
            proposal_data = self._merged_proposal_data(proposal_data, patch.fields)
        proposal_data["missing_info"] = list(patch.missing_info)
        if patch.confidence:
            proposal_data["confidence"] = patch.confidence
        proposal_data["status"] = self._status_for_proposal_data(proposal_data)
        updated_proposal = AssistantProposal.model_validate(proposal_data)
        payload = deepcopy(draft.payload)
        payload["assistant_proposal"] = updated_proposal.model_dump(mode="json")
        if "planned_events" in patch.fields:
            payload["planned_events"] = patch.fields["planned_events"]
        updated = self.store.update_plan_draft(
            draft.id,
            {
                "kind": self._draft_kind_value(updated_proposal.kind),
                "title": self._proposal_title(updated_proposal),
                "payload": payload,
                "missing_fields": list(updated_proposal.missing_info),
                "status": self._status_value(updated_proposal.status),
                "confidence": updated_proposal.confidence,
            },
        )
        self.store.create_tool_run(
            agent_run_id=agent_run_id,
            tool_name="refine_proposal",
            input_json={"plan_draft_id": draft.id, "patch_fields": sorted(patch.fields.keys()), "runtime": "model_first"},
            output_json={"plan_draft_id": updated.id, "status": updated.status},
        )
        return PlannerOutcome(
            reply_text=patch.user_visible_summary or "草案已按你明确提供的字段更新。",
            proposal_id=updated.id,
        )

    def _draft_kind_value(self, value: Any) -> str:
        raw = str(value.value if hasattr(value, "value") else value or "")
        allowed = {item.value for item in PlanDraftKind}
        return raw if raw in allowed else PlanDraftKind.long_term_schedule.value

    def _status_value(self, value: Any) -> str:
        raw = str(value.value if hasattr(value, "value") else value or "")
        allowed = {item.value for item in PlanDraftStatus}
        return raw if raw in allowed else PlanDraftStatus.refining.value

    def _operation_to_tool_call(self, operation: ConcreteOperation) -> AgentToolCall:
        arguments = deepcopy(operation.arguments)
        tool_name = self._operation_tool_name(operation.operation)
        if operation.operation == "create_schedule_block" and "blocks" not in arguments:
            arguments = {"blocks": [arguments]}
        return AgentToolCall(
            tool_name=tool_name,
            risk_level=operation.risk_level,
            requires_confirmation=operation.requires_confirmation,
            arguments=arguments,
        )

    def _operation_tool_name(self, operation: str) -> str:
        mapping = {
            "create_task": "create_task_candidate",
            "update_task": "update_task",
            "complete_task": "complete_task",
            "cancel_task": "cancel_task",
            "create_calendar_event": "create_calendar_event_candidate",
            "update_calendar_event": "update_calendar_event",
            "cancel_calendar_event": "cancel_calendar_event",
            "create_schedule_block": "create_schedule_block_candidates",
            "update_schedule_block": "update_schedule_block",
            "disable_schedule_block_reminders": "disable_schedule_block_reminders",
            "cancel_schedule_block": "cancel_schedule_block",
            "sync_feishu_task": "sync_feishu_task",
            "sync_feishu_calendar": "sync_feishu_calendar",
        }
        return mapping[operation]

    def _proposal_from_draft(self, draft: PlanDraft) -> AssistantProposal:
        payload = draft.payload if isinstance(draft.payload, dict) else {}
        proposal = payload.get("assistant_proposal")
        if isinstance(proposal, dict):
            return AssistantProposal.model_validate(proposal)
        return AssistantProposal(
            kind=draft.kind,
            status=draft.status,
            user_goal=draft.title,
            missing_info=list(draft.missing_fields),
            confidence=draft.confidence,
        )

    def _payload_with_status(self, draft: PlanDraft, status: str) -> dict[str, Any]:
        payload = deepcopy(draft.payload)
        if isinstance(payload.get("assistant_proposal"), dict):
            payload["assistant_proposal"] = {**payload["assistant_proposal"], "status": status}
        return payload

    def _replacement_proposal_data(self, existing: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
        replacement = {key: deepcopy(value) for key, value in fields.items() if key in PROPOSAL_TOP_LEVEL_FIELDS}
        return {**existing, **replacement}

    def _merged_proposal_data(self, existing: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
        data = deepcopy(existing)
        for key, value in fields.items():
            if key in PROPOSAL_TOP_LEVEL_FIELDS:
                data[key] = deepcopy(value)
        details_patch = fields.get("details") if isinstance(fields.get("details"), dict) else {}
        for key in DETAIL_PATCH_FIELDS:
            if key in fields:
                details_patch[key] = fields[key]
        if details_patch:
            candidates = data.get("candidate_plans")
            if not isinstance(candidates, list) or not candidates:
                candidates = [{"title": data.get("user_goal") or "计划草案", "details": {}}]
            first = candidates[0] if isinstance(candidates[0], dict) else {}
            first_details = first.get("details") if isinstance(first.get("details"), dict) else {}
            candidates[0] = {**first, "details": {**first_details, **deepcopy(details_patch)}}
            data["candidate_plans"] = candidates
        if "planned_events" in fields and "schedule_preview" not in fields:
            data["schedule_preview"] = deepcopy(fields["planned_events"])
        return data

    def _status_for_proposal(self, proposal: AssistantProposal) -> str:
        return self._status_for_proposal_data(proposal.model_dump(mode="json"))

    def _status_for_proposal_data(self, proposal: dict[str, Any]) -> str:
        explicit = proposal.get("status")
        if explicit in {
            PlanDraftStatus.schedule_pending.value,
            PlanDraftStatus.confirmed.value,
            PlanDraftStatus.canceled.value,
        }:
            return str(explicit)
        if proposal.get("schedule_preview"):
            return PlanDraftStatus.schedule_pending.value
        if proposal.get("missing_info"):
            return PlanDraftStatus.refining.value
        return PlanDraftStatus.ready_for_schedule.value

    def _proposal_title(self, proposal: AssistantProposal) -> str:
        return str(proposal.user_goal or "计划草案")[:80]

    def _proposal_reply(self, proposal: AssistantProposal) -> str:
        if proposal.missing_info:
            return proposal.next_step_suggestion or "我先保留为计划草案，需要补充信息后再生成日程确认。"
        if proposal.schedule_preview:
            return "计划草案已整理出日程预览，确认前不会写入日历。"
        return "计划草案已整理好，确认前不会写入日历或任务。"

    def _proposal_card(self, proposal: AssistantProposal, plan_id: str) -> dict[str, Any]:
        lines = [
            f"目标：{proposal.user_goal}",
            f"状态：{proposal.status}",
        ]
        if proposal.ai_assumptions:
            lines.append("AI 假设：" + "；".join(str(item) for item in proposal.ai_assumptions[:3]))
        if proposal.missing_info:
            lines.append("还缺：" + "、".join(str(item) for item in proposal.missing_info[:6]))
        candidate_titles = [
            str(item.get("title") or item.get("name") or item.get("type") or "候选计划")
            for item in proposal.candidate_plans[:3]
            if isinstance(item, dict)
        ]
        if candidate_titles:
            lines.append("候选计划：" + "；".join(candidate_titles))
        if proposal.schedule_preview:
            lines.append(f"日程预览：{len(proposal.schedule_preview)} 条，确认前不会写入。")
        if proposal.risks:
            lines.append("风险：" + "；".join(str(item) for item in proposal.risks[:3]))
        if proposal.next_step_suggestion:
            lines.append("下一步：" + proposal.next_step_suggestion)
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "计划草案"}},
            "elements": [
                {"tag": "markdown", "content": "\n\n".join(lines)},
                {"tag": "note", "elements": [{"tag": "plain_text", "content": f"plan_draft_id: {plan_id}"}]},
            ],
        }

    def _with_decision_meta(
        self,
        outcome: PlannerOutcome,
        decision: AssistantDecision,
        *,
        legacy_adapter_used: bool = False,
    ) -> PlannerOutcome:
        outcome.assistant_decision_action = decision.action
        outcome.referenced_context = list(decision.referenced_context)
        outcome.legacy_adapter_used = legacy_adapter_used
        outcome.backend_semantic_fallback_used = False
        return outcome
