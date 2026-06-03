from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.feishu_native import FeishuNativeAdapter
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
from app.core.tools import ToolRouter

PLANNING_ONLY_TOOLS = {
    "schedule_time_budget_plan",
    "start_plan_refinement",
    "refine_plan_draft",
    "generate_plan_schedule_confirmation",
    "start_habit_refinement",
    "refine_habit_plan",
}

CONFIRM_TEXTS = {"确认", "是的", "可以", "确定", "OK", "ok"}
CANCEL_TEXTS = {"取消", "不用了", "算了"}


@dataclass
class PlannerOutcome:
    tool_calls: list[AgentToolCall] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    reply_text: str = ""
    confirmation_id: str | None = None
    proposal_id: str | None = None
    card_sent: bool = False


class PlannerService:
    def __init__(
        self,
        store: StateStore,
        feishu: FeishuNativeAdapter,
        tz: ZoneInfo,
        planning_support: ToolRouter | None = None,
    ):
        self.store = store
        self.feishu = feishu
        self.tz = tz
        self.support = planning_support or ToolRouter(store, feishu, tz)

    async def plan_response(
        self,
        response: AgentResponse,
        request: dict[str, Any],
        *,
        agent_run_id: str,
        capture_id: str,
        sender_id: str | None,
    ) -> PlannerOutcome:
        raw_text = str(request.get("raw_text") or "").strip()
        if response.assistant_proposal:
            return await self.save_or_refine_proposal(
                response.assistant_proposal,
                request,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )

        if self._should_refine_active_proposal(raw_text, response, sender_id):
            return await self.refine_active_proposal(
                raw_text,
                request,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )

        planning_calls = [call for call in response.tool_calls if call.tool_name in PLANNING_ONLY_TOOLS]
        if not planning_calls:
            return PlannerOutcome(tool_calls=response.tool_calls, reply_text=response.reply_to_user)

        if len(planning_calls) != len(response.tool_calls):
            passthrough = [call for call in response.tool_calls if call.tool_name not in PLANNING_ONLY_TOOLS]
            return PlannerOutcome(tool_calls=passthrough, reply_text=response.reply_to_user)

        return await self.execute_planning_call(
            planning_calls[0],
            request,
            agent_run_id=agent_run_id,
            capture_id=capture_id,
            sender_id=sender_id,
        )

    async def execute_planning_call(
        self,
        call: AgentToolCall,
        request: dict[str, Any],
        *,
        agent_run_id: str | None,
        capture_id: str,
        sender_id: str | None,
    ) -> PlannerOutcome:
        args = dict(call.arguments)
        raw_text = str(args.get("raw_text") or args.get("text") or request.get("raw_text") or "").strip()
        if call.tool_name in {"start_habit_refinement", "refine_habit_plan"}:
            proposal = self._proposal_from_text(raw_text, request, kind=PlanDraftKind.habit.value, confidence=0.82)
            return await self.save_or_refine_proposal(
                proposal,
                request,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if call.tool_name == "schedule_time_budget_plan":
            proposal = self._proposal_for_time_budget_schedule(args, request)
            outcome = await self.save_or_refine_proposal(
                proposal,
                request,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
                send_refinement_card=False,
            )
            legacy = await self.support._schedule_time_budget_plan(
                args,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
            if outcome.proposal_id and legacy.get("planned_events"):
                draft = self.store.get_plan_draft(outcome.proposal_id)
                payload = dict(draft.payload)
                payload["schedule_preview"] = list(legacy.get("planned_events") or [])
                payload["assistant_proposal"] = {
                    **dict(payload.get("assistant_proposal") or {}),
                    "schedule_preview": list(legacy.get("planned_events") or []),
                    "status": PlanDraftStatus.schedule_pending.value,
                }
                self.store.update_plan_draft(
                    draft.id,
                    {"payload": payload, "status": PlanDraftStatus.schedule_pending.value, "missing_fields": []},
                )
            return PlannerOutcome(
                tool_results=[{"tool_name": call.tool_name, "ok": True, **legacy}],
                reply_text=str(legacy.get("reply_text") or ""),
                confirmation_id=legacy.get("confirmation_id"),
                proposal_id=outcome.proposal_id,
                card_sent=bool(legacy.get("feishu")),
            )
        if call.tool_name in {"start_plan_refinement", "refine_plan_draft", "generate_plan_schedule_confirmation"}:
            legacy = await self._execute_legacy_plan_call(
                call,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
            proposal_id = self._proposal_id_from_legacy_result(legacy)
            if proposal_id:
                self._attach_proposal_to_legacy_draft(proposal_id, raw_text or str(request.get("raw_text") or ""), legacy)
            return PlannerOutcome(
                tool_results=[{"tool_name": call.tool_name, "ok": True, **legacy}],
                reply_text=str(legacy.get("reply_text") or ""),
                confirmation_id=legacy.get("confirmation_id"),
                proposal_id=proposal_id,
                card_sent=bool(legacy.get("feishu")),
            )
        return PlannerOutcome(tool_calls=[call])

    async def save_or_refine_proposal(
        self,
        proposal: AssistantProposal,
        request: dict[str, Any],
        *,
        agent_run_id: str | None,
        capture_id: str,
        sender_id: str | None,
        send_refinement_card: bool = True,
    ) -> PlannerOutcome:
        raw_text = str(request.get("raw_text") or "").strip()
        active = self._latest_active_proposal(sender_id)
        draft = self._upsert_proposal_draft(
            proposal,
            raw_text=raw_text,
            capture_id=capture_id,
            sender_id=sender_id,
            active=active,
        )
        proposal = AssistantProposal.model_validate(draft.payload["assistant_proposal"])
        time_budget_args = self._time_budget_schedule_args(proposal)
        if time_budget_args is not None:
            legacy = await self.support._schedule_time_budget_plan(
                time_budget_args,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
            self._store_legacy_schedule_preview(draft.id, proposal, list(legacy.get("planned_events") or []))
            return PlannerOutcome(
                tool_results=[{"tool_name": "schedule_time_budget_plan", "ok": True, **legacy}],
                reply_text=str(legacy.get("reply_text") or ""),
                confirmation_id=legacy.get("confirmation_id"),
                proposal_id=draft.id,
                card_sent=bool(legacy.get("feishu")),
            )
        if proposal.missing_info:
            if send_refinement_card:
                card = self._proposal_card(proposal, draft.id)
                card_result = await self.feishu.send_card(sender_id, card)
                self.store.create_tool_run(
                    agent_run_id=agent_run_id,
                    tool_name="assistant_proposal",
                    input_json={"plan_draft_id": draft.id},
                    output_json={"card": card_result},
                )
            return PlannerOutcome(
                reply_text=self._proposal_reply(proposal),
                proposal_id=draft.id,
                card_sent=send_refinement_card,
            )

        tool_calls = self._tool_calls_for_ready_proposal(draft, proposal, request, capture_id=capture_id)
        if not tool_calls:
            card = self._proposal_card(proposal, draft.id)
            card_result = await self.feishu.send_card(sender_id, card)
            self.store.create_tool_run(
                agent_run_id=agent_run_id,
                tool_name="assistant_proposal",
                input_json={"plan_draft_id": draft.id},
                output_json={"card": card_result},
            )
            return PlannerOutcome(
                reply_text="计划草案已更新，但还没有可创建的未来日程。请补充时间范围或频率。",
                proposal_id=draft.id,
                card_sent=True,
            )
        return PlannerOutcome(
            tool_calls=tool_calls,
            reply_text="计划草案已更新，下面是日程预览，确认后才会写入任务和日历。",
            proposal_id=draft.id,
        )

    def _time_budget_schedule_args(self, proposal: AssistantProposal) -> dict[str, Any] | None:
        candidate = self._first_candidate_plan(proposal.model_dump(mode="json"))
        if candidate.get("type") != "time_budget_schedule":
            return None
        args = candidate.get("arguments")
        return dict(args) if isinstance(args, dict) else {}

    def _store_legacy_schedule_preview(self, plan_id: str, proposal: AssistantProposal, planned_events: list[dict[str, Any]]) -> None:
        try:
            draft = self.store.get_plan_draft(plan_id)
        except KeyError:
            return
        proposal.schedule_preview = planned_events
        proposal.status = PlanDraftStatus.schedule_pending.value if planned_events else PlanDraftStatus.refining.value
        payload = dict(draft.payload)
        payload["assistant_proposal"] = proposal.model_dump(mode="json")
        payload["schedule_preview"] = planned_events
        self.store.update_plan_draft(
            draft.id,
            {
                "payload": payload,
                "missing_fields": [] if planned_events else ["可用日程候选"],
                "status": PlanDraftStatus.schedule_pending.value if planned_events else PlanDraftStatus.refining.value,
            },
        )

    async def refine_active_proposal(
        self,
        raw_text: str,
        request: dict[str, Any],
        *,
        agent_run_id: str | None,
        capture_id: str,
        sender_id: str | None,
    ) -> PlannerOutcome:
        draft = self._latest_active_proposal(sender_id)
        if not draft:
            proposal = self._proposal_from_text(raw_text, request)
        else:
            previous = AssistantProposal.model_validate(draft.payload["assistant_proposal"])
            proposal = self._merge_proposal_from_text(previous, raw_text, request)
            self._cancel_stale_schedule_confirmations(sender_id)
        return await self.save_or_refine_proposal(
            proposal,
            request,
            agent_run_id=agent_run_id,
            capture_id=capture_id,
            sender_id=sender_id,
        )

    def _upsert_proposal_draft(
        self,
        proposal: AssistantProposal,
        *,
        raw_text: str,
        capture_id: str,
        sender_id: str | None,
        active: PlanDraft | None,
    ) -> PlanDraft:
        proposal = self._normalize_proposal(proposal, raw_text)
        status = self._draft_status_for_proposal(proposal)
        payload = {
            "assistant_proposal": proposal.model_dump(mode="json"),
            "raw_text_history": [raw_text] if raw_text else [],
        }
        if active and "assistant_proposal" in active.payload:
            history = list(active.payload.get("raw_text_history") or [])
            if raw_text:
                history.append(raw_text)
            payload = {**active.payload, "assistant_proposal": proposal.model_dump(mode="json"), "raw_text_history": history[-8:]}
            return self.store.update_plan_draft(
                active.id,
                {
                    "kind": self._kind_value(proposal.kind),
                    "title": self._proposal_title(proposal),
                    "payload": payload,
                    "missing_fields": proposal.missing_info,
                    "status": status,
                    "confidence": proposal.confidence,
                },
            )
        return self.store.create_plan_draft(
            kind=self._kind_value(proposal.kind),
            title=self._proposal_title(proposal),
            payload=payload,
            missing_fields=proposal.missing_info,
            status=status,
            source_capture_id=capture_id,
            sender_id=sender_id,
            confidence=proposal.confidence,
        )

    def _tool_calls_for_ready_proposal(
        self,
        draft: PlanDraft,
        proposal: AssistantProposal,
        request: dict[str, Any],
        *,
        capture_id: str,
    ) -> list[AgentToolCall]:
        kind = self._kind_value(proposal.kind)
        if kind in {PlanDraftKind.habit.value, PlanDraftKind.long_term_schedule.value}:
            events = self._build_proposal_events(proposal, request, plan_draft_id=draft.id, capture_id=capture_id)
            if not events:
                return []
            proposal.schedule_preview = events
            proposal.status = PlanDraftStatus.schedule_pending.value
            payload = dict(draft.payload)
            payload["assistant_proposal"] = proposal.model_dump(mode="json")
            payload["schedule_preview"] = events
            self.store.update_plan_draft(
                draft.id,
                {
                    "payload": payload,
                    "missing_fields": [],
                    "status": PlanDraftStatus.schedule_pending.value,
                },
            )
            title = self._proposal_title(proposal)
            total_minutes = self._session_minutes(proposal) * len(events)
            task_call = AgentToolCall(
                tool_name="create_task_candidate",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={
                    "title": title,
                    "description": self._proposal_description(proposal),
                    "estimated_minutes": total_minutes,
                    "due_at": events[-1]["end_at"],
                    "source_capture_id": capture_id,
                    "confidence": proposal.confidence,
                },
            )
            event_calls = [
                AgentToolCall(
                    tool_name="create_calendar_event_candidate",
                    risk_level=RiskLevel.medium,
                    requires_confirmation=True,
                    arguments=event,
                )
                for event in events
            ]
            return [task_call, *event_calls]
        return []

    def _normalize_proposal(self, proposal: AssistantProposal, raw_text: str) -> AssistantProposal:
        data = proposal.model_dump(mode="json")
        if not data.get("user_goal"):
            data["user_goal"] = raw_text or "长期计划"
        merged = self._merge_proposal_from_text(AssistantProposal.model_validate(data), raw_text, {}) if raw_text else proposal
        missing = self._missing_info_for_proposal(merged)
        merged.missing_info = missing
        merged.status = PlanDraftStatus.refining.value if missing else PlanDraftStatus.ready_for_schedule.value
        if not merged.next_step_suggestion:
            merged.next_step_suggestion = self._next_step_for_missing(missing)
        return merged

    def _proposal_from_text(
        self,
        raw_text: str,
        request: dict[str, Any],
        *,
        kind: str | None = None,
        confidence: float = 0.72,
    ) -> AssistantProposal:
        inferred_kind = kind or self._infer_kind(raw_text)
        proposal = AssistantProposal(
            kind=inferred_kind,
            status=PlanDraftStatus.refining.value,
            user_goal=self._goal_from_text(raw_text),
            context_summary=self._context_summary(request),
            ai_assumptions=self._default_assumptions(inferred_kind, raw_text),
            missing_info=[],
            candidate_plans=self._candidate_plans(inferred_kind, raw_text),
            schedule_preview=[],
            risks=self._default_risks(inferred_kind),
            next_step_suggestion="",
            confidence=confidence,
        )
        return self._merge_proposal_from_text(proposal, raw_text, request)

    def _proposal_for_time_budget_schedule(self, args: dict[str, Any], request: dict[str, Any]) -> AssistantProposal:
        goal = str(args.get("query") or args.get("title") or request.get("raw_text") or "长期学习计划")
        return AssistantProposal(
            kind=PlanDraftKind.long_term_schedule.value,
            status=PlanDraftStatus.ready_for_schedule.value,
            user_goal=goal,
            context_summary=self._context_summary(request),
            ai_assumptions=["根据当前空闲时间拆分为若干日历候选。", "确认前不会写入日历。"],
            missing_info=[],
            candidate_plans=[{"title": goal, "strategy": "按空闲时间拆分日程候选", "arguments": args}],
            schedule_preview=[],
            risks=["空闲时间可能不足，生成的候选需要人工确认。"],
            next_step_suggestion="请确认候选日程，确认后才会写入日历。",
            confidence=0.78,
        )

    def _merge_proposal_from_text(
        self,
        proposal: AssistantProposal,
        raw_text: str,
        request: dict[str, Any],
    ) -> AssistantProposal:
        text = raw_text.strip()
        data = proposal.model_dump(mode="json")
        if text and len(text) > len(str(data.get("user_goal") or "")) and not self._looks_like_parameter_only(text):
            data["user_goal"] = self._goal_from_text(text)
        details = dict(self._first_candidate_plan(data).get("details") or {})
        if text:
            details["latest_user_reply"] = text
        method = self._method_from_text(text)
        if method:
            details["method"] = method
        session_minutes = self._minutes_from_text(text)
        if session_minutes:
            details["session_minutes"] = session_minutes
        preferred_time = self._time_from_text(text)
        if preferred_time:
            details["preferred_time"] = preferred_time
        byday = self._byday_from_text(text)
        if byday:
            details["byday"] = byday
            details["frequency"] = "weekly" if byday != "MO,TU,WE,TH,FR,SA,SU" else "daily"
        duration_days = self._duration_days_from_text(text, request)
        if duration_days:
            details["duration_days"] = duration_days
        if "frequency" not in details and any(token in text for token in ("每天", "每日", "天天")):
            details["frequency"] = "daily"
            details["byday"] = "MO,TU,WE,TH,FR,SA,SU"
        if "frequency" not in details and any(token in text for token in ("每周", "周一", "周二", "周三", "周四", "周五", "周六", "周日")):
            details["frequency"] = "weekly"
        if not details.get("method") and self._kind_value(data.get("kind")) == PlanDraftKind.long_term_schedule.value:
            details["method"] = "复习"
        candidate = self._first_candidate_plan(data)
        candidate["details"] = details
        candidate.setdefault("title", self._proposal_title(AssistantProposal.model_validate(data)))
        data["candidate_plans"] = [candidate]
        merged = AssistantProposal.model_validate(data)
        merged.missing_info = self._missing_info_for_proposal(merged)
        merged.status = PlanDraftStatus.refining.value if merged.missing_info else PlanDraftStatus.ready_for_schedule.value
        merged.next_step_suggestion = self._next_step_for_missing(merged.missing_info)
        return merged

    def _build_proposal_events(
        self,
        proposal: AssistantProposal,
        request: dict[str, Any],
        *,
        plan_draft_id: str,
        capture_id: str,
    ) -> list[dict[str, Any]]:
        details = self._proposal_details(proposal)
        session_minutes = int(details.get("session_minutes") or 0)
        preferred_time = str(details.get("preferred_time") or "")
        duration_days = int(details.get("duration_days") or 0)
        if not session_minutes or not preferred_time or not duration_days:
            return []
        now = self._now_from_request(request)
        hour, minute = [int(part) for part in preferred_time.split(":", 1)]
        byday = str(details.get("byday") or "MO,TU,WE,TH,FR,SA,SU")
        allowed_weekdays = self._weekday_indexes(byday)
        events: list[dict[str, Any]] = []
        first_day = now.date()
        if time(hour, minute) <= now.time():
            first_day += timedelta(days=1)
        title = self._event_title(proposal)
        for offset in range(duration_days):
            day = first_day + timedelta(days=offset)
            if day.weekday() not in allowed_weekdays:
                continue
            start = datetime.combine(day, time(hour, minute), tzinfo=self.tz)
            end = start + timedelta(minutes=session_minutes)
            events.append(
                {
                    "title": title,
                    "description": self._proposal_description(proposal),
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                    "source_capture_id": capture_id,
                    "plan_draft_id": plan_draft_id,
                    "plan_item_id": f"proposal_{len(events) + 1}",
                    "confidence": proposal.confidence,
                }
            )
        return events

    async def _execute_legacy_plan_call(
        self,
        call: AgentToolCall,
        *,
        agent_run_id: str | None,
        capture_id: str,
        sender_id: str | None,
    ) -> dict[str, Any]:
        if call.tool_name == "start_plan_refinement":
            return await self.support._start_plan_refinement(
                call.arguments,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if call.tool_name == "refine_plan_draft":
            return await self.support._refine_plan_draft(
                call.arguments,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if call.tool_name == "generate_plan_schedule_confirmation":
            return await self.support._generate_plan_schedule_confirmation(
                call.arguments,
                agent_run_id=agent_run_id,
                sender_id=sender_id,
            )
        raise ValueError(f"unsupported planning call: {call.tool_name}")

    def _attach_proposal_to_legacy_draft(self, plan_id: str, raw_text: str, legacy: dict[str, Any]) -> None:
        try:
            draft = self.store.get_plan_draft(plan_id)
        except KeyError:
            return
        if "assistant_proposal" in draft.payload:
            return
        proposal = AssistantProposal(
            kind=draft.kind.value,
            status=draft.status.value,
            user_goal=raw_text or draft.title,
            context_summary="由 PlannerService 从既有计划草案生成。",
            ai_assumptions=["先保留结构化草案，确认前不写入日历。"],
            missing_info=list(draft.missing_fields),
            candidate_plans=[{"title": draft.title, "payload": draft.payload}],
            schedule_preview=list(legacy.get("planned_events") or draft.payload.get("planned_events") or []),
            risks=["图片或表格识别可能存在错行错列，需要用户确认。"],
            next_step_suggestion="请确认草案中的课程、周次和节次。",
            confidence=draft.confidence,
        )
        payload = {**draft.payload, "assistant_proposal": proposal.model_dump(mode="json")}
        self.store.update_plan_draft(draft.id, {"payload": payload})

    def _proposal_card(self, proposal: AssistantProposal, plan_id: str) -> dict[str, Any]:
        lines = [
            f"**目标**：{proposal.user_goal}",
            f"**状态**：{self._status_text(proposal)}",
        ]
        if proposal.ai_assumptions:
            lines.append("**AI 假设**：" + "；".join(proposal.ai_assumptions[:5]))
        if proposal.missing_info:
            lines.append("**还缺**：" + "、".join(proposal.missing_info))
        if proposal.candidate_plans:
            lines.append("**候选计划**：")
            for index, plan in enumerate(proposal.candidate_plans[:3], start=1):
                title = plan.get("title") or f"方案 {index}"
                details = plan.get("details") if isinstance(plan.get("details"), dict) else {}
                detail_parts = []
                for key, value in details.items():
                    if value is None or value == "":
                        continue
                    detail_parts.append(f"{key}={value}")
                detail_text = "，".join(detail_parts)
                lines.append(f"{index}. {title}" + (f"（{detail_text}）" if detail_text else ""))
        if proposal.schedule_preview:
            lines.append("**日程预览**：")
            for event in proposal.schedule_preview[:8]:
                lines.append(f"- {event.get('start_at')} {event.get('title')}")
        if proposal.risks:
            lines.append("**风险**：" + "；".join(proposal.risks[:4]))
        if proposal.next_step_suggestion:
            lines.append("**下一步**：" + proposal.next_step_suggestion)
        return {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": "计划草案"}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(lines)}}],
            "_mvp_meta": {"plan_draft_id": plan_id, "assistant_proposal": proposal.model_dump(mode="json")},
        }

    def _proposal_reply(self, proposal: AssistantProposal) -> str:
        if proposal.missing_info:
            return f"我先生成了一份计划草案，还需要补充：{'、'.join(proposal.missing_info)}。"
        return "计划草案已完整，我会生成日程预览，确认后才写入。"

    def _latest_active_proposal(self, sender_id: str | None) -> PlanDraft | None:
        for draft in self.store.list_plan_drafts(
            sender_id=sender_id,
            statuses=[
                PlanDraftStatus.refining.value,
                PlanDraftStatus.ready_for_schedule.value,
                PlanDraftStatus.schedule_pending.value,
            ],
            limit=5,
        ):
            if isinstance(draft.payload, dict) and "assistant_proposal" in draft.payload:
                return draft
        return None

    def _should_refine_active_proposal(self, raw_text: str, response: AgentResponse, sender_id: str | None) -> bool:
        if not raw_text or raw_text in CONFIRM_TEXTS or raw_text in CANCEL_TEXTS:
            return False
        if not self._latest_active_proposal(sender_id):
            return False
        if any(call.tool_name not in {"send_feishu_reply"} | PLANNING_ONLY_TOOLS for call in response.tool_calls):
            return False
        return self._looks_like_proposal_followup(raw_text) or response.intent in {"unknown", "create_candidates"}

    def _cancel_stale_schedule_confirmations(self, sender_id: str | None) -> None:
        for confirmation in self.store.list_pending_confirmations(sender_id=sender_id, limit=10):
            if confirmation.confirmation_type in {"create_candidates", "habit_schedule", "plan_schedule", "time_budget_calendar"}:
                self.store.cancel_confirmation(confirmation.id)

    def _proposal_id_from_legacy_result(self, result: dict[str, Any]) -> str | None:
        plan = result.get("plan_draft")
        if isinstance(plan, dict) and plan.get("id"):
            return str(plan["id"])
        return None

    def _draft_status_for_proposal(self, proposal: AssistantProposal) -> str:
        if proposal.missing_info:
            return PlanDraftStatus.refining.value
        return self._status_value(proposal.status)

    def _missing_info_for_proposal(self, proposal: AssistantProposal) -> list[str]:
        if self._first_candidate_plan(proposal.model_dump(mode="json")).get("type") == "time_budget_schedule":
            return []
        details = self._proposal_details(proposal)
        missing: list[str] = []
        if not proposal.user_goal:
            missing.append("目标")
        if not details.get("method"):
            missing.append("执行方式")
        if not details.get("session_minutes"):
            missing.append("每次时长")
        if not details.get("preferred_time"):
            missing.append("偏好时间")
        if not details.get("frequency") and not details.get("byday"):
            missing.append("频率")
        if not details.get("duration_days"):
            missing.append("持续周期")
        return missing

    def _proposal_details(self, proposal: AssistantProposal) -> dict[str, Any]:
        return dict(self._first_candidate_plan(proposal.model_dump(mode="json")).get("details") or {})

    def _first_candidate_plan(self, data: dict[str, Any]) -> dict[str, Any]:
        plans = data.get("candidate_plans") if isinstance(data.get("candidate_plans"), list) else []
        if plans and isinstance(plans[0], dict):
            return dict(plans[0])
        return {"title": data.get("user_goal") or "计划草案", "details": {}}

    def _infer_kind(self, text: str) -> str:
        if any(token in text for token in ("课表", "课程表", "上课", "节次")):
            return PlanDraftKind.course_timetable.value
        if any(token in text for token in ("锻炼", "健身", "健康", "习惯", "跑步")):
            return PlanDraftKind.habit.value
        return PlanDraftKind.long_term_schedule.value

    def _goal_from_text(self, text: str) -> str:
        cleaned = text.strip(" ，。！？")
        cleaned = re.sub(r"^(我想|希望|帮我|请帮我|添加一个|安排一个|制定一个)", "", cleaned).strip(" ，。")
        return cleaned or text or "长期计划"

    def _context_summary(self, request: dict[str, Any]) -> str:
        now = request.get("now")
        return f"消息时间：{now}" if now else ""

    def _default_assumptions(self, kind: str, text: str) -> list[str]:
        if kind == PlanDraftKind.habit.value:
            return ["这是一个需要逐步澄清的习惯养成目标。", "确认前不会创建任务或日历。"]
        if kind == PlanDraftKind.course_timetable.value:
            return ["这是课程表导入，需要先确认识别结果。", "确认前不会写入日历。"]
        return ["这是长期计划，需要先明确频率、时长和周期。", "确认前不会创建任务或日历。"]

    def _candidate_plans(self, kind: str, text: str) -> list[dict[str, Any]]:
        title = self._goal_from_text(text)
        if kind == PlanDraftKind.habit.value:
            return [{"title": title, "details": {"method": self._method_from_text(text) or ""}}]
        return [{"title": title, "details": {"method": self._method_from_text(text) or "复习"}}]

    def _default_risks(self, kind: str) -> list[str]:
        if kind == PlanDraftKind.course_timetable.value:
            return ["图片识别可能有错列或漏识别。"]
        return ["目标过粗时直接排日程容易不符合真实偏好。"]

    def _next_step_for_missing(self, missing: list[str]) -> str:
        if not missing:
            return "请确认日程预览；确认后才会写入任务和日历。"
        return "请补充" + "、".join(missing[:4]) + "。"

    def _proposal_title(self, proposal: AssistantProposal) -> str:
        details = self._proposal_details(proposal)
        method = str(details.get("method") or "").strip()
        goal = proposal.user_goal.strip() or "长期计划"
        if method and method not in goal:
            return f"{goal}：{method}"
        return goal

    def _event_title(self, proposal: AssistantProposal) -> str:
        title = self._proposal_title(proposal)
        if self._kind_value(proposal.kind) == PlanDraftKind.long_term_schedule.value and "复习" not in title:
            return f"{title}复习"
        return title

    def _proposal_description(self, proposal: AssistantProposal) -> str:
        lines = [f"来源计划草案：{proposal.user_goal}"]
        if proposal.ai_assumptions:
            lines.append("AI 假设：" + "；".join(proposal.ai_assumptions[:3]))
        return "\n".join(lines)

    def _status_text(self, proposal: AssistantProposal) -> str:
        status = self._status_value(proposal.status)
        return {
            PlanDraftStatus.refining.value: "完善中",
            PlanDraftStatus.ready_for_schedule.value: "可生成日程预览",
            PlanDraftStatus.schedule_pending.value: "待确认写入",
            PlanDraftStatus.confirmed.value: "已确认",
            PlanDraftStatus.canceled.value: "已取消",
        }.get(status, status)

    def _looks_like_parameter_only(self, text: str) -> bool:
        return bool(
            any(token in text for token in ("每天", "每周", "分钟", "小时", "早上", "晚上", "上午", "下午", "改成", "先"))
            and len(text) <= 40
        )

    def _looks_like_proposal_followup(self, text: str) -> bool:
        return any(token in text for token in ("每天", "每周", "分钟", "小时", "早上", "晚上", "上午", "下午", "改成", "先", "到月底", "复习", "跑步"))

    def _method_from_text(self, text: str) -> str | None:
        for token in ("跑步", "快走", "力量训练", "复习", "背单词", "阅读", "健身"):
            if token in text:
                return token
        if "锻炼" in text:
            return "锻炼"
        return None

    def _minutes_from_text(self, text: str) -> int | None:
        minute_match = re.search(r"(\d{1,3})\s*分钟", text)
        if minute_match:
            return int(minute_match.group(1))
        hour_match = re.search(r"(\d(?:\.\d+)?)\s*小时", text)
        if hour_match:
            return int(float(hour_match.group(1)) * 60)
        return None

    def _time_from_text(self, text: str) -> str | None:
        match = re.search(r"(早上|上午|中午|下午|晚上|今晚)?\s*(\d{1,2})(?::(\d{2}))?\s*点?", text)
        if not match:
            return None
        period = match.group(1) or ""
        hour = int(match.group(2))
        minute = int(match.group(3) or 0)
        if period in {"下午", "晚上", "今晚"} and hour < 12:
            hour += 12
        if period in {"早上", "上午"} and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute:02d}"

    def _byday_from_text(self, text: str) -> str | None:
        if any(token in text for token in ("每天", "每日", "天天")):
            return "MO,TU,WE,TH,FR,SA,SU"
        mapping = {
            "周一": "MO",
            "星期一": "MO",
            "周二": "TU",
            "星期二": "TU",
            "周三": "WE",
            "星期三": "WE",
            "周四": "TH",
            "星期四": "TH",
            "周五": "FR",
            "星期五": "FR",
            "周六": "SA",
            "星期六": "SA",
            "周日": "SU",
            "周天": "SU",
            "星期日": "SU",
            "星期天": "SU",
        }
        found = [code for label, code in mapping.items() if label in text]
        return ",".join(dict.fromkeys(found)) if found else None

    def _duration_days_from_text(self, text: str, request: dict[str, Any]) -> int | None:
        if "一个月" in text or "1个月" in text:
            return 30
        if "一周" in text or "1周" in text:
            return 7
        if "到月底" in text or "本月底" in text:
            now = self._now_from_request(request)
            last_day = calendar.monthrange(now.year, now.month)[1]
            return max(1, (datetime(now.year, now.month, last_day, tzinfo=self.tz).date() - now.date()).days + 1)
        match = re.search(r"(\d{1,3})\s*天", text)
        if match:
            return int(match.group(1))
        return None

    def _session_minutes(self, proposal: AssistantProposal) -> int:
        return int(self._proposal_details(proposal).get("session_minutes") or 0)

    def _weekday_indexes(self, byday: str) -> set[int]:
        mapping = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
        values = {mapping[item] for item in byday.split(",") if item in mapping}
        return values or set(mapping.values())

    def _now_from_request(self, request: dict[str, Any]) -> datetime:
        raw = request.get("now")
        if isinstance(raw, str) and raw:
            try:
                parsed = datetime.fromisoformat(raw)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=self.tz)
            except ValueError:
                pass
        return datetime.now(self.tz)

    def _kind_value(self, value: Any) -> str:
        return value.value if hasattr(value, "value") else str(value or PlanDraftKind.long_term_schedule.value)

    def _status_value(self, value: Any) -> str:
        return value.value if hasattr(value, "value") else str(value or PlanDraftStatus.refining.value)
