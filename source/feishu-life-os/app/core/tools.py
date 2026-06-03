from __future__ import annotations

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo

from app.core.feishu_native import FeishuNativeAdapter, confirmation_card
from app.core.policy import RiskPolicy
from app.core.relative_time import effective_day_start
from app.core.schemas import (
    AgentToolCall,
    ConfirmationStatus,
    ItemStatus,
    PlanDraftStatus,
    RiskLevel,
    RunStatus,
)
from app.core.store import StateStore

PLAN_DRAFT_CONFIRMATION_TYPES = {
    "habit_refinement",
    "habit_schedule",
    "course_timetable_refinement",
    "course_timetable_schedule",
    "plan_refinement",
    "plan_schedule",
}
HABIT_CONFIRMATION_TYPES = {"habit_refinement", "habit_schedule"}
COURSE_TIMETABLE_CONFIRMATION_TYPES = {"course_timetable_refinement", "course_timetable_schedule"}
PLANNING_ONLY_TOOLS = {
    "schedule_time_budget_plan",
    "start_plan_refinement",
    "refine_plan_draft",
    "generate_plan_schedule_confirmation",
    "start_habit_refinement",
    "refine_habit_plan",
}
HABIT_DEFAULT_BYDAY = "MO,TU,WE,TH,FR,SA,SU"
HABIT_DAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
DAY_LABELS = {"MO": "周一", "TU": "周二", "WE": "周三", "TH": "周四", "FR": "周五", "SA": "周六", "SU": "周日"}
DEFAULT_COURSE_PERIOD_MAP = {
    "1-2": {"start_time": "08:00", "end_time": "09:40"},
    "3-4": {"start_time": "10:00", "end_time": "11:40"},
    "5-6": {"start_time": "14:30", "end_time": "16:10"},
    "7-8": {"start_time": "16:25", "end_time": "18:05"},
    "9-10": {"start_time": "19:30", "end_time": "21:00"},
}


def _card_id(card_result: dict[str, Any]) -> str | None:
    if card_result.get("card_id"):
        return str(card_result["card_id"])
    data = card_result.get("response", {}).get("data") if isinstance(card_result.get("response"), dict) else None
    if isinstance(data, dict):
        message_id = data.get("message_id") or data.get("message", {}).get("message_id")
        if message_id:
            return str(message_id)
    return None


class ToolRouter:
    def __init__(self, store: StateStore, feishu: FeishuNativeAdapter, tz: ZoneInfo):
        self.store = store
        self.feishu = feishu
        self.tz = tz
        self.policy = RiskPolicy()

    async def execute_calls(
        self,
        calls: list[AgentToolCall],
        *,
        agent_run_id: str,
        capture_id: str,
        sender_id: str | None,
    ) -> tuple[list[dict[str, Any]], str, str | None]:
        normalized = [self.policy.normalize_call(call) for call in calls]
        confirm_calls = [call for call in normalized if call.requires_confirmation]
        if confirm_calls:
            confirmation = self.store.create_confirmation(
                agent_run_id=agent_run_id,
                confirmation_type=self._confirmation_type(confirm_calls),
                proposed_tool_calls_json=[call.model_dump(mode="json") for call in confirm_calls],
                sender_id=sender_id,
            )
            candidates = [self._candidate_summary(call) for call in confirm_calls]
            candidates = self._attach_conflicts(candidates)
            prompt = self._confirmation_prompt(candidates)
            card = confirmation_card(prompt, confirmation.id, candidates)
            card_result = await self.feishu.send_card(sender_id, card)
            self.store.update_confirmation_card_id(confirmation.id, _card_id(card_result))
            self.store.create_tool_run(
                agent_run_id=agent_run_id,
                tool_name="ask_confirmation",
                input_json={"tool_calls": [call.model_dump(mode="json") for call in confirm_calls]},
                output_json={"confirmation_id": confirmation.id, "card": card_result},
            )
            return (
                [{"tool_name": "ask_confirmation", "confirmation_id": confirmation.id, "card": card_result}],
                prompt,
                confirmation.id,
            )

        results: list[dict[str, Any]] = []
        replies: list[str] = []
        generated_confirmation_id: str | None = None
        for call in normalized:
            result = await self.execute_call(
                call,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
            results.append(result)
            if result.get("reply_text"):
                replies.append(str(result["reply_text"]))
            if call.tool_name in {
                "ask_confirmation",
                "schedule_time_budget_plan",
                "start_plan_refinement",
                "refine_plan_draft",
                "generate_plan_schedule_confirmation",
                "start_habit_refinement",
                "refine_habit_plan",
            } and result.get("confirmation_id"):
                generated_confirmation_id = str(result["confirmation_id"])
        return results, "\n".join(replies) if replies else "", generated_confirmation_id

    async def execute_call(
        self,
        call: AgentToolCall,
        *,
        agent_run_id: str,
        capture_id: str,
        sender_id: str | None,
    ) -> dict[str, Any]:
        try:
            output = await self._execute_call(call, agent_run_id=agent_run_id, capture_id=capture_id, sender_id=sender_id)
            self.store.create_tool_run(
                agent_run_id=agent_run_id,
                tool_name=call.tool_name,
                input_json=call.arguments,
                output_json=output,
                status=RunStatus.done,
            )
            return {"tool_name": call.tool_name, "ok": True, **output}
        except Exception as exc:  # noqa: BLE001 - persisted into ToolRun
            self.store.create_tool_run(
                agent_run_id=agent_run_id,
                tool_name=call.tool_name,
                input_json=call.arguments,
                output_json={},
                status=RunStatus.failed,
                error=str(exc),
            )
            return {"tool_name": call.tool_name, "ok": False, "error": str(exc)}

    async def _execute_call(
        self,
        call: AgentToolCall,
        *,
        agent_run_id: str | None,
        capture_id: str,
        sender_id: str | None,
    ) -> dict[str, Any]:
        args = {**call.arguments}
        if call.tool_name in PLANNING_ONLY_TOOLS:
            raise ValueError(f"planning-only tool must be handled by PlannerService: {call.tool_name}")
        if call.tool_name == "send_feishu_reply":
            text = str(args.get("text") or "")
            return {"reply_text": text, "feishu": await self.feishu.send_text(sender_id, text)}
        if call.tool_name == "send_feishu_card":
            card = args.get("card") if isinstance(args.get("card"), dict) else {"content": args}
            return {"feishu": await self.feishu.send_card(sender_id, card), "reply_text": "已发送确认卡片。"}
        if call.tool_name == "ask_confirmation":
            prompt = str(args.get("prompt") or args.get("text") or "请确认后我再执行。")
            confirmation = self.store.create_confirmation(
                agent_run_id=None,
                confirmation_type=str(args.get("confirmation_type") or "manual"),
                proposed_tool_calls_json=list(args.get("proposed_tool_calls") or []),
                sender_id=sender_id,
            )
            card = confirmation_card(prompt, confirmation.id, list(args.get("candidates") or []))
            card_result = await self.feishu.send_card(sender_id, card)
            self.store.update_confirmation_card_id(confirmation.id, _card_id(card_result))
            return {
                "confirmation_id": confirmation.id,
                "feishu": card_result,
                "reply_text": prompt,
            }
        if call.tool_name == "resolve_confirmation":
            return await self.resolve_confirmation(
                sender_id=sender_id,
                agent_run_id=args.get("agent_run_id"),
                confirmation_id=args.get("confirmation_id"),
                action=str(args.get("action") or "confirm"),
            )
        if call.tool_name == "confirm_task":
            item = self.store.create_action_item({**args, "source_capture_id": args.get("source_capture_id") or capture_id})
            return {"action_item": item.model_dump(mode="json"), "reply_text": f"已创建任务：{item.title}"}
        if call.tool_name == "confirm_calendar_event":
            event = self.store.create_calendar_event({**args, "source_capture_id": args.get("source_capture_id") or capture_id})
            sync_result = await self.feishu.sync_calendar_event(event.model_dump(mode="json"))
            self._store_feishu_event_id(event.id, sync_result)
            return {
                "calendar_event": self.store.get_calendar_event(event.id).model_dump(mode="json"),
                "sync": sync_result,
                "reply_text": f"已创建日程：{event.title}",
            }
        if call.tool_name == "confirm_schedule_blocks":
            blocks = []
            synced = []
            for block in args.get("blocks", [args]):
                created = self.store.create_schedule_block({**block, "source_capture_id": block.get("source_capture_id") or capture_id})
                sync_result = await self.feishu.sync_schedule_block(created.model_dump(mode="json"))
                self._store_feishu_schedule_event_id(created.id, sync_result)
                synced.append(sync_result)
                blocks.append(self.store.get_schedule_block(created.id).model_dump(mode="json"))
            return {"schedule_blocks": blocks, "synced": synced, "reply_text": f"已创建 {len(blocks)} 个重复日程安排。"}
        if call.tool_name == "update_schedule_block":
            block = self._resolve_schedule_block_update(args)
            sync_result = await self.feishu.update_schedule_block(block.model_dump(mode="json"))
            self._store_feishu_schedule_event_id(block.id, sync_result)
            return {
                "schedule_block": self.store.get_schedule_block(block.id).model_dump(mode="json"),
                "sync": sync_result,
                "reply_text": f"已更新日程安排：{block.title}",
            }
        if call.tool_name == "disable_schedule_block_reminders":
            blocks = self._disable_schedule_block_reminders(args)
            audit = await self.feishu.sync_bitable_audit(
                {"operation": "disable_schedule_block_reminders", "schedule_blocks": [block.model_dump(mode="json") for block in blocks]}
            )
            if not blocks:
                reply_text = "当前没有可关闭提醒的固定安排。"
            else:
                reply_text = f"已关闭 {len(blocks)} 个固定安排的提醒；安排仍会保留在查询和日历同步中。"
            return {
                "schedule_blocks": [block.model_dump(mode="json") for block in blocks],
                "sync": audit,
                "reply_text": reply_text,
            }
        if call.tool_name == "update_task":
            item = self._resolve_task_update(args)
            return {"action_item": item.model_dump(mode="json"), "reply_text": f"已更新任务：{item.title}"}
        if call.tool_name == "complete_task":
            matches = self._match_action_items(args)
            if len(matches) != 1:
                return {
                    "matches": [item.model_dump(mode="json") for item in matches],
                    "reply_text": self._format_ambiguous_task(matches, "完成"),
                }
            item_id = matches[0].id
            item = self.store.update_action_item(item_id, {"status": ItemStatus.done.value})
            return {"action_item": item.model_dump(mode="json"), "reply_text": f"已完成任务：{item.title}"}
        if call.tool_name == "cancel_task":
            item_id = self._resolve_task_id(args)
            item = self.store.update_action_item(item_id, {"status": ItemStatus.canceled.value})
            return {"action_item": item.model_dump(mode="json"), "reply_text": f"已取消任务：{item.title}"}
        if call.tool_name == "cancel_calendar_event":
            event = self._cancel_calendar_event(args)
            sync_result = await self.feishu.delete_calendar_event(event.model_dump(mode="json"))
            return {"calendar_event": event.model_dump(mode="json"), "sync": sync_result, "reply_text": f"已取消日程：{event.title}"}
        if call.tool_name == "cancel_schedule_block":
            block = self._cancel_schedule_block(args)
            sync_result = await self.feishu.delete_schedule_block(block.model_dump(mode="json"))
            return {"schedule_block": block.model_dump(mode="json"), "sync": sync_result, "reply_text": f"已取消日程安排：{block.title}"}
        if call.tool_name == "query_today":
            return self._query_range("今天", 0)
        if call.tool_name == "query_tomorrow":
            return self._query_range("明天", 1)
        if call.tool_name == "query_week":
            return self._query_week()
        if call.tool_name == "query_tasks":
            return self._query_tasks(args)
        if call.tool_name == "explain_time_budget_plan":
            return self._explain_time_budget_plan(args)
        if call.tool_name == "schedule_time_budget_plan":
            return await self._schedule_time_budget_plan(
                args,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if call.tool_name == "start_plan_refinement":
            return await self._start_plan_refinement(
                args,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if call.tool_name == "refine_plan_draft":
            return await self._refine_plan_draft(
                args,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if call.tool_name == "generate_plan_schedule_confirmation":
            return await self._generate_plan_schedule_confirmation(
                args,
                agent_run_id=agent_run_id,
                sender_id=sender_id,
            )
        if call.tool_name == "start_habit_refinement":
            return await self._start_habit_refinement(
                args,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if call.tool_name == "refine_habit_plan":
            return await self._refine_habit_plan(
                args,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if call.tool_name == "query_pending_confirmations":
            confirmations = [
                item.model_dump(mode="json")
                for item in self.store.list_pending_confirmations(sender_id=sender_id, limit=int(args.get("limit", 10)))
            ]
            return {"confirmations": confirmations, "reply_text": self._format_pending_confirmations(confirmations)}
        if call.tool_name == "query_availability":
            return self._query_availability(args, sender_id=sender_id)
        if call.tool_name == "query_schedule_blocks":
            blocks = [block.model_dump(mode="json") for block in self.store.list_schedule_blocks()]
            return {"schedule_blocks": blocks, "reply_text": self._format_schedule_blocks(blocks)}
        if call.tool_name == "check_conflicts":
            return self._check_conflicts(args)
        if call.tool_name == "sync_feishu_task":
            return await self._sync_feishu_task(args)
        if call.tool_name == "sync_feishu_calendar":
            return await self._sync_feishu_calendar(args)
        if call.tool_name == "sync_bitable_audit":
            payload = {"capture_id": capture_id, **args}
            return {"sync": await self.feishu.sync_bitable_audit(payload), "reply_text": "已写入多维表审计。"}
        if call.tool_name == "list_recent_agent_runs":
            return {"agent_runs": [run.model_dump(mode="json") for run in self.store.list_agent_runs(limit=int(args.get("limit", 20)))]}
        if call.tool_name == "list_recent_tool_runs":
            return {"tool_runs": [run.model_dump(mode="json") for run in self.store.list_tool_runs(limit=int(args.get("limit", 20)))]}
        if call.tool_name == "record_agent_run":
            return {"reply_text": "AgentRun 已由 Orchestrator 自动记录。"}
        if call.tool_name == "record_tool_run":
            return {"reply_text": "ToolRun 已由 ToolRouter 自动记录。"}
        raise ValueError(f"unsupported direct tool or confirmation-required tool: {call.tool_name}")

    async def resolve_confirmation(
        self,
        *,
        sender_id: str | None,
        agent_run_id: str | None = None,
        confirmation_id: str | None = None,
        action: str = "confirm",
    ) -> dict[str, Any]:
        if confirmation_id:
            try:
                confirmation = self.store.get_confirmation(confirmation_id)
            except KeyError:
                return {
                    "status": "not_found",
                    "confirmation_id": confirmation_id,
                    "reply_text": "没有找到这条待确认操作，可能已经失效。",
                    "created": [],
                }
        else:
            confirmation = self.store.get_pending_confirmation(sender_id)
            if not confirmation:
                return {"status": "not_found", "reply_text": "没有找到待确认操作。", "created": []}
        if confirmation.sender_id and sender_id and confirmation.sender_id != sender_id:
            return {
                "status": "forbidden",
                "confirmation_id": confirmation.id,
                "reply_text": "这条确认不属于当前用户，已拒绝执行。",
                "created": [],
            }
        if confirmation.status != ConfirmationStatus.pending:
            return {
                "status": confirmation.status.value,
                "confirmation_id": confirmation.id,
                "reply_text": "这条确认已经处理过，不会重复执行。",
                "created": [],
            }
        if confirmation.expires_at and confirmation.expires_at < datetime.now(confirmation.expires_at.tzinfo or self.tz):
            self.store.expire_confirmation(confirmation.id)
            return {
                "status": "expired",
                "confirmation_id": confirmation.id,
                "reply_text": "这条确认已经过期，没有执行任何操作。",
                "created": [],
            }
        if action == "cancel":
            self.store.cancel_confirmation(confirmation.id)
            self._cancel_plan_draft_for_confirmation(confirmation)
            run_id = agent_run_id or confirmation.agent_run_id
            if run_id:
                self.store.create_tool_run(
                    agent_run_id=run_id,
                    tool_name="resolve_confirmation.cancel",
                    input_json={"confirmation_id": confirmation.id},
                    output_json={"status": "canceled"},
                )
            return {
                "status": "canceled",
                "confirmation_id": confirmation.id,
                "reply_text": "已取消这条候选，没有创建或修改任何事项。",
                "created": [],
            }
        created: list[dict[str, Any]] = []
        synced: list[dict[str, Any]] = []
        for raw_call in confirmation.proposed_tool_calls_json:
            call = AgentToolCall.model_validate(raw_call)
            args = {**call.arguments}
            args.setdefault("source_capture_id", self._capture_from_confirmation(confirmation))
            if call.tool_name == "schedule_habit_plan":
                habit_created, habit_synced = await self._confirm_habit_schedule(args, confirmation)
                created.extend(habit_created)
                synced.extend(habit_synced)
            elif call.tool_name == "confirm_plan_schedule":
                plan_created, plan_synced = await self._confirm_plan_schedule(args, confirmation)
                created.extend(plan_created)
                synced.extend(plan_synced)
            elif call.tool_name == "refine_plan_draft":
                plan_id = args.get("plan_id")
                if plan_id:
                    draft = self.store.get_plan_draft(str(plan_id))
                    created.append({"type": "plan_draft", **draft.model_dump(mode="json")})
            elif call.tool_name in {"create_task_candidate", "confirm_task"}:
                item = self.store.create_action_item(args)
                created.append({"type": "action_item", **item.model_dump(mode="json")})
                synced.append(await self.feishu.sync_task(item.model_dump(mode="json")))
            elif call.tool_name in {"create_calendar_event_candidate", "confirm_calendar_event"}:
                event = self.store.create_calendar_event(args)
                created.append({"type": "calendar_event", **event.model_dump(mode="json")})
                sync_result = await self.feishu.sync_calendar_event(event.model_dump(mode="json"))
                self._store_feishu_event_id(event.id, sync_result)
                synced.append(sync_result)
            elif call.tool_name in {"create_schedule_block_candidates", "confirm_schedule_blocks"}:
                for block_args in args.get("blocks", [args]):
                    block_args.setdefault("source_capture_id", args.get("source_capture_id"))
                    block = self.store.create_schedule_block(block_args)
                    sync_result = await self.feishu.sync_schedule_block(block.model_dump(mode="json"))
                    self._store_feishu_schedule_event_id(block.id, sync_result)
                    synced.append(sync_result)
                    block = self.store.get_schedule_block(block.id)
                    created.append({"type": "schedule_block", **block.model_dump(mode="json")})
                    synced.append(await self.feishu.sync_bitable_audit({"schedule_block": block.model_dump(mode="json")}))
            elif call.tool_name == "update_schedule_block":
                block = self._resolve_schedule_block_update(args)
                sync_result = await self.feishu.update_schedule_block(block.model_dump(mode="json"))
                self._store_feishu_schedule_event_id(block.id, sync_result)
                block = self.store.get_schedule_block(block.id)
                created.append({"type": "schedule_block_update", **block.model_dump(mode="json")})
                synced.append(sync_result)
                synced.append(await self.feishu.sync_bitable_audit({"schedule_block": block.model_dump(mode="json")}))
            elif call.tool_name == "disable_schedule_block_reminders":
                blocks = self._disable_schedule_block_reminders(args)
                created.extend({"type": "schedule_block_reminder_update", **block.model_dump(mode="json")} for block in blocks)
                synced.append(
                    await self.feishu.sync_bitable_audit(
                        {"operation": "disable_schedule_block_reminders", "schedule_blocks": [block.model_dump(mode="json") for block in blocks]}
                    )
                )
            elif call.tool_name == "update_calendar_event":
                event = self._resolve_calendar_update(args)
                created.append({"type": "calendar_event_update", **event.model_dump(mode="json")})
                sync_result = await self.feishu.update_calendar_event(event.model_dump(mode="json"))
                self._store_feishu_event_id(event.id, sync_result)
                synced.append(sync_result)
            elif call.tool_name == "update_task":
                item = self._resolve_task_update(args)
                created.append({"type": "action_item_update", **item.model_dump(mode="json")})
                synced.append(await self.feishu.sync_task(item.model_dump(mode="json")))
            elif call.tool_name == "complete_task":
                item = self.store.update_action_item(self._resolve_task_id(args), {"status": ItemStatus.done.value})
                created.append({"type": "action_item_complete", **item.model_dump(mode="json")})
                synced.append(await self.feishu.sync_task(item.model_dump(mode="json")))
            elif call.tool_name == "cancel_task":
                item = self.store.update_action_item(self._resolve_task_id(args), {"status": ItemStatus.canceled.value})
                created.append({"type": "action_item_cancel", **item.model_dump(mode="json")})
                synced.append(await self.feishu.sync_task(item.model_dump(mode="json")))
            elif call.tool_name == "cancel_calendar_event":
                event = self._cancel_calendar_event(args)
                created.append({"type": "calendar_event_cancel", **event.model_dump(mode="json")})
                synced.append(await self.feishu.delete_calendar_event(event.model_dump(mode="json")))
            elif call.tool_name == "cancel_schedule_block":
                block = self._cancel_schedule_block(args)
                created.append({"type": "schedule_block_cancel", **block.model_dump(mode="json")})
                synced.append(await self.feishu.delete_schedule_block(block.model_dump(mode="json")))
                synced.append(await self.feishu.sync_bitable_audit({"schedule_block": block.model_dump(mode="json")}))
            else:
                raise ValueError(f"confirmation cannot apply tool: {call.tool_name}")
        synced.append(
            await self.feishu.sync_bitable_audit(
                {"confirmation_id": confirmation.id, "confirmation_type": confirmation.confirmation_type, "created": created}
            )
        )
        self.store.resolve_confirmation(confirmation.id)
        run_id = agent_run_id or confirmation.agent_run_id
        if run_id:
            self.store.create_tool_run(
                agent_run_id=run_id,
                tool_name="resolve_confirmation.apply",
                input_json={"confirmation_id": confirmation.id},
                output_json={"created": created, "synced": synced},
            )
        return {
            "status": "resolved",
            "confirmation_id": confirmation.id,
            "created": created,
            "synced": synced,
            "reply_text": self._format_created(created),
        }

    def _capture_from_confirmation(self, confirmation) -> str | None:
        if not confirmation.agent_run_id:
            return None
        try:
            return self.store.get_agent_run(confirmation.agent_run_id).capture_id
        except KeyError:
            return None

    def _resolve_calendar_update(self, args: dict[str, Any]):
        event_id = args.get("event_id")
        if not event_id:
            query = str(args.get("query") or "")
            matches = self.store.find_calendar_events(query)
            if len(matches) != 1:
                raise ValueError("日程匹配不唯一，请先选择要修改哪一条。")
            event_id = matches[0].id
        patch = {key: args[key] for key in ("title", "description", "start_at", "end_at", "location") if args.get(key)}
        return self.store.update_calendar_event(str(event_id), patch)

    def _cancel_calendar_event(self, args: dict[str, Any]):
        event_id = args.get("calendar_event_id") or args.get("event_id")
        if not event_id:
            query = str(args.get("query") or args.get("title") or "")
            matches = self.store.find_calendar_events(query)
            if len(matches) != 1:
                raise ValueError("日程匹配不唯一，请说清楚要取消哪一条日程。")
            event_id = matches[0].id
        return self.store.update_calendar_event(str(event_id), {"status": ItemStatus.canceled.value})

    def _match_action_items(self, args: dict[str, Any]):
        if args.get("action_item_id"):
            return [self.store.get_action_item(str(args["action_item_id"]))]
        query = str(args.get("query") or args.get("title") or "").strip()
        if not query:
            return []
        return self.store.find_action_items(query)

    def _resolve_task_id(self, args: dict[str, Any]) -> str:
        matches = self._match_action_items(args)
        if len(matches) != 1:
            raise ValueError("任务匹配不唯一，请先选择要操作哪一条。")
        return matches[0].id

    def _resolve_task_update(self, args: dict[str, Any]):
        item_id = self._resolve_task_id(args)
        patch = {key: args[key] for key in ("title", "description", "status", "priority", "due_at", "estimated_minutes") if args.get(key)}
        return self.store.update_action_item(item_id, patch)

    def _resolve_schedule_block_update(self, args: dict[str, Any]):
        block_id = args.get("schedule_block_id")
        if not block_id:
            query = str(args.get("query") or "").strip()
            matches = self.store.find_schedule_blocks(query)
            if len(matches) != 1:
                raise ValueError("日程安排匹配不唯一，请先说明要修改哪一条。")
            block_id = matches[0].id
        patch = {key: args[key] for key in ("title", "recurrence_rule", "start_time", "end_time", "timezone", "status") if args.get(key)}
        if "reminder_enabled" in args and args["reminder_enabled"] is not None:
            patch["reminder_enabled"] = bool(args["reminder_enabled"])
        return self.store.update_schedule_block(str(block_id), patch)

    def _disable_schedule_block_reminders(self, args: dict[str, Any]):
        block_id = args.get("schedule_block_id")
        query = str(args.get("query") or args.get("title") or "").strip()
        scope = str(args.get("scope") or "").strip().lower()
        if block_id:
            blocks = [self.store.get_schedule_block(str(block_id))]
        elif scope in {"all", "fixed", "weekly", "all_fixed"} or self._is_all_fixed_schedule_query(query):
            blocks = self.store.list_schedule_blocks()
        elif query:
            blocks = self.store.find_schedule_blocks(query)
            if len(blocks) != 1:
                raise ValueError("日程安排匹配不唯一，请说清楚要关闭哪一条安排的提醒，或说明关闭所有固定安排提醒。")
        else:
            blocks = self.store.list_schedule_blocks()
        updated = []
        for block in blocks:
            if not block.reminder_enabled:
                continue
            updated.append(self.store.update_schedule_block(block.id, {"reminder_enabled": False}))
        return updated

    def _is_all_fixed_schedule_query(self, query: str) -> bool:
        if not query:
            return False
        return any(token in query for token in ("所有", "全部", "以后", "每周固定", "固定的安排", "固定安排", "每周")) and any(
            token in query for token in ("不用提醒", "不需要提醒", "不要提醒", "别提醒", "关闭提醒", "取消提醒")
        )

    def _cancel_schedule_block(self, args: dict[str, Any]):
        block_id = args.get("schedule_block_id")
        if not block_id:
            query = str(args.get("query") or args.get("title") or "").strip()
            matches = self.store.find_schedule_blocks(query)
            if len(matches) != 1:
                raise ValueError("日程安排匹配不唯一，请说清楚要取消哪一条。")
            block_id = matches[0].id
        return self.store.update_schedule_block(str(block_id), {"status": ItemStatus.canceled.value})

    def _query_tasks(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if query:
            tasks = [item.model_dump(mode="json") for item in self.store.find_action_items(query, include_done=True)]
            events = [event.model_dump(mode="json") for event in self.store.find_calendar_events(query)]
            return {
                "tasks": tasks,
                "calendar_events": events,
                "schedule_blocks": [],
                "reply_text": self._format_query(f"和“{query}”相关的事项", tasks, events, []),
            }
        return self._query_week()

    def _explain_time_budget_plan(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or args.get("title") or "").strip()
        tasks = self._find_time_budget_tasks(query, action_item_id=str(args.get("action_item_id") or ""))
        events = self._find_time_budget_events(query, tasks)
        return {
            "query": query,
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "calendar_events": [event.model_dump(mode="json") for event in events],
            "reply_text": self._format_time_budget_plan(query, tasks, events),
        }

    async def _schedule_time_budget_plan(
        self,
        args: dict[str, Any],
        *,
        agent_run_id: str | None,
        capture_id: str,
        sender_id: str | None,
    ) -> dict[str, Any]:
        query = str(args.get("query") or args.get("title") or "").strip()
        tasks = self._find_time_budget_tasks(
            query,
            action_item_id=str(args.get("action_item_id") or ""),
            allow_default=True,
            time_budget_only=True,
        )
        if not tasks:
            target = f"“{query}”" if query else "最近的长期学习任务"
            return {"reply_text": f"我没有找到{target}，还不能生成日历候选。"}
        if len(tasks) > 1:
            titles = "、".join(task.title for task in tasks[:5])
            return {"reply_text": f"找到多个长期学习任务：{titles}。请说明要把哪一个接入日历。"}

        task = tasks[0]
        plan = self._build_time_budget_calendar_plan(task, args, capture_id=capture_id)
        calls = [
            AgentToolCall(
                tool_name="create_calendar_event_candidate",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments=item,
            )
            for item in plan["events"]
        ]
        if not calls:
            if plan["remaining_minutes"] <= 0:
                return {
                    "reply_text": f"{self._time_budget_task_label(task)} 的 {self._format_minutes(task.estimated_minutes)} 已经排进日历，不需要新增日程。"
                }
            return {"reply_text": "我没有找到足够可用的空闲时间段，暂时不能生成日历候选。"}

        confirmation = self.store.create_confirmation(
            agent_run_id=agent_run_id,
            confirmation_type="time_budget_calendar",
            proposed_tool_calls_json=[call.model_dump(mode="json") for call in calls],
            sender_id=sender_id,
        )
        candidates = self._attach_conflicts([self._candidate_summary(call) for call in calls])
        prompt = self._time_budget_schedule_prompt(task, plan, candidates)
        card = confirmation_card(prompt, confirmation.id, candidates)
        card_result = await self.feishu.send_card(sender_id, card)
        self.store.update_confirmation_card_id(confirmation.id, _card_id(card_result))
        return {
            "confirmation_id": confirmation.id,
            "planned_events": plan["events"],
            "planned_minutes": plan["planned_minutes"],
            "remaining_minutes": plan["remaining_minutes"],
            "feishu": card_result,
            "reply_text": prompt,
        }

    async def _start_plan_refinement(
        self,
        args: dict[str, Any],
        *,
        agent_run_id: str | None,
        capture_id: str,
        sender_id: str | None,
    ) -> dict[str, Any]:
        kind = str(args.get("kind") or self._infer_plan_kind(args)).strip() or "long_term_schedule"
        if kind == "habit":
            return await self._start_habit_refinement(
                args,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if kind == "course_timetable":
            self._cancel_conflicting_plan_confirmations(sender_id, args)
            payload = self._course_timetable_payload_from_args(args, capture_id=capture_id)
            missing = self._course_timetable_missing_fields(payload)
            draft = self.store.create_plan_draft(
                kind="course_timetable",
                title=self._course_timetable_title(payload),
                payload=payload,
                missing_fields=missing,
                status=PlanDraftStatus.refining.value if missing else PlanDraftStatus.ready_for_schedule.value,
                source_capture_id=capture_id,
                sender_id=sender_id,
                confidence=float(payload.get("confidence") or args.get("confidence") or 0.65),
            )
            if not missing:
                return await self._create_course_timetable_schedule_confirmation(
                    draft.id,
                    agent_run_id=agent_run_id,
                    sender_id=sender_id,
                )
            return await self._save_plan_refinement_confirmation(
                draft.id,
                agent_run_id=agent_run_id,
                sender_id=sender_id,
            )

        payload = {
            "original_request": str(args.get("raw_text") or args.get("text") or "").strip(),
            "source_capture_id": capture_id,
            "attachment_refs": list(args.get("attachment_refs") or []),
        }
        draft = self.store.create_plan_draft(
            kind=kind,
            title=str(args.get("title") or "长期日程草案"),
            payload=payload,
            missing_fields=["目标细节", "频率", "时间安排"],
            status=PlanDraftStatus.refining.value,
            source_capture_id=capture_id,
            sender_id=sender_id,
            confidence=float(args.get("confidence") or 0.5),
        )
        return await self._save_plan_refinement_confirmation(
            draft.id,
            agent_run_id=agent_run_id,
            sender_id=sender_id,
        )

    async def _refine_plan_draft(
        self,
        args: dict[str, Any],
        *,
        agent_run_id: str | None,
        capture_id: str,
        sender_id: str | None,
    ) -> dict[str, Any]:
        plan_id = str(args.get("plan_id") or "").strip()
        draft = self.store.get_plan_draft(plan_id) if plan_id else self._latest_active_plan_draft(sender_id)
        if not draft:
            return await self._start_plan_refinement(
                args,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if draft.kind.value == "habit":
            args = {**args, "plan_draft_id": draft.id}
            return await self._refine_habit_plan(
                args,
                agent_run_id=agent_run_id,
                capture_id=capture_id,
                sender_id=sender_id,
            )
        if draft.kind.value != "course_timetable":
            payload = {**draft.payload, **(args.get("patch") if isinstance(args.get("patch"), dict) else {})}
            raw_text = str(args.get("raw_text") or args.get("text") or "").strip()
            if raw_text:
                payload["latest_user_reply"] = raw_text
            updated = self.store.update_plan_draft(
                draft.id,
                {"payload": payload, "missing_fields": ["目标细节", "频率", "时间安排"], "status": PlanDraftStatus.refining.value},
            )
            return await self._save_plan_refinement_confirmation(
                updated.id,
                agent_run_id=agent_run_id,
                sender_id=sender_id,
            )

        payload = self._merge_course_timetable_payload(draft.payload, args, capture_id=capture_id)
        missing = self._course_timetable_missing_fields(payload)
        updated = self.store.update_plan_draft(
            draft.id,
            {
                "title": self._course_timetable_title(payload),
                "payload": payload,
                "missing_fields": missing,
                "status": PlanDraftStatus.refining.value if missing else PlanDraftStatus.ready_for_schedule.value,
                "confidence": float(payload.get("confidence") or draft.confidence),
            },
        )
        if not missing:
            self._cancel_latest_plan_confirmation(sender_id, COURSE_TIMETABLE_CONFIRMATION_TYPES)
            return await self._create_course_timetable_schedule_confirmation(
                updated.id,
                agent_run_id=agent_run_id,
                sender_id=sender_id,
            )
        return await self._save_plan_refinement_confirmation(
            updated.id,
            agent_run_id=agent_run_id,
            sender_id=sender_id,
        )

    async def _generate_plan_schedule_confirmation(
        self,
        args: dict[str, Any],
        *,
        agent_run_id: str | None,
        sender_id: str | None,
    ) -> dict[str, Any]:
        plan_id = str(args.get("plan_id") or "").strip()
        if not plan_id:
            draft = self._latest_active_plan_draft(sender_id)
            if not draft:
                return {"reply_text": "我没有找到正在完善的长期日程草案。"}
            plan_id = draft.id
        draft = self.store.get_plan_draft(plan_id)
        if draft.kind.value == "course_timetable":
            return await self._create_course_timetable_schedule_confirmation(
                draft.id,
                agent_run_id=agent_run_id,
                sender_id=sender_id,
            )
        return {"reply_text": f"“{draft.title}”还不能生成日程确认卡，请先补齐草案信息。"}

    async def _save_plan_refinement_confirmation(
        self,
        plan_id: str,
        *,
        agent_run_id: str | None,
        sender_id: str | None,
    ) -> dict[str, Any]:
        draft = self.store.get_plan_draft(plan_id)
        call = AgentToolCall(
            tool_name="refine_plan_draft",
            risk_level=RiskLevel.low,
            requires_confirmation=False,
            arguments={"plan_id": draft.id, "kind": draft.kind.value, "title": draft.title},
        )
        confirmation = self._latest_confirmation_for_plan(sender_id, draft.id, {f"{draft.kind.value}_refinement", "plan_refinement"})
        confirmation_type = f"{draft.kind.value}_refinement"
        if confirmation:
            confirmation = self.store.update_confirmation_payload(
                confirmation.id,
                [call.model_dump(mode="json")],
                confirmation_type=confirmation_type,
            )
        else:
            confirmation = self.store.create_confirmation(
                agent_run_id=agent_run_id,
                confirmation_type=confirmation_type,
                proposed_tool_calls_json=[call.model_dump(mode="json")],
                sender_id=sender_id,
            )
        card = self._plan_refinement_card(draft.model_dump(mode="json"), confirmation.id)
        card_result = await self.feishu.send_card(sender_id, card)
        self.store.update_confirmation_card_id(confirmation.id, _card_id(card_result))
        return {
            "confirmation_id": confirmation.id,
            "plan_draft": draft.model_dump(mode="json"),
            "missing_fields": draft.missing_fields,
            "feishu": card_result,
            "reply_text": self._plan_refinement_reply(draft.model_dump(mode="json")),
        }

    async def _create_course_timetable_schedule_confirmation(
        self,
        plan_id: str,
        *,
        agent_run_id: str | None,
        sender_id: str | None,
    ) -> dict[str, Any]:
        draft = self.store.get_plan_draft(plan_id)
        payload = dict(draft.payload)
        missing = self._course_timetable_missing_fields(payload)
        if missing:
            updated = self.store.update_plan_draft(
                draft.id,
                {"missing_fields": missing, "status": PlanDraftStatus.refining.value},
            )
            return await self._save_plan_refinement_confirmation(
                updated.id,
                agent_run_id=agent_run_id,
                sender_id=sender_id,
            )
        planned_events = self._build_course_timetable_events(payload, plan_draft_id=draft.id)
        if not planned_events:
            payload["generation_note"] = "没有可创建的未来课程事件，请检查教学周或课程周次。"
            updated = self.store.update_plan_draft(
                draft.id,
                {
                    "payload": payload,
                    "missing_fields": ["可创建的未来课程事件"],
                    "status": PlanDraftStatus.refining.value,
                },
            )
            return await self._save_plan_refinement_confirmation(
                updated.id,
                agent_run_id=agent_run_id,
                sender_id=sender_id,
            )
        payload["planned_events"] = planned_events
        draft = self.store.update_plan_draft(
            draft.id,
            {
                "payload": payload,
                "missing_fields": [],
                "status": PlanDraftStatus.schedule_pending.value,
            },
        )
        call = AgentToolCall(
            tool_name="confirm_plan_schedule",
            risk_level=RiskLevel.medium,
            requires_confirmation=True,
            arguments={
                "plan_id": draft.id,
                "kind": "course_timetable",
                "planned_events": planned_events,
            },
        )
        confirmation = self.store.create_confirmation(
            agent_run_id=agent_run_id,
            confirmation_type="course_timetable_schedule",
            proposed_tool_calls_json=[call.model_dump(mode="json")],
            sender_id=sender_id,
        )
        candidates = [self._candidate_summary(call)]
        prompt = self._course_timetable_schedule_prompt(draft.model_dump(mode="json"), planned_events)
        card = confirmation_card(prompt, confirmation.id, candidates)
        card_result = await self.feishu.send_card(sender_id, card)
        self.store.update_confirmation_card_id(confirmation.id, _card_id(card_result))
        return {
            "confirmation_id": confirmation.id,
            "plan_draft": draft.model_dump(mode="json"),
            "planned_events": planned_events,
            "feishu": card_result,
            "reply_text": prompt,
        }

    async def _start_habit_refinement(
        self,
        args: dict[str, Any],
        *,
        agent_run_id: str | None,
        capture_id: str,
        sender_id: str | None,
    ) -> dict[str, Any]:
        raw_text = str(args.get("raw_text") or args.get("text") or args.get("title") or "").strip()
        existing = self._latest_habit_confirmation(sender_id)
        if existing:
            self.store.cancel_confirmation(existing.id)
        plan = self._merge_habit_plan_from_text(
            {
                "title": args.get("title"),
                "goal": args.get("goal"),
                "category": args.get("category"),
                "source_capture_id": capture_id,
            },
            raw_text,
            apply_suggestions=False,
        )
        return await self._save_habit_refinement(
            plan,
            agent_run_id=agent_run_id,
            sender_id=sender_id,
            confirmation=None,
        )

    async def _refine_habit_plan(
        self,
        args: dict[str, Any],
        *,
        agent_run_id: str | None,
        capture_id: str,
        sender_id: str | None,
    ) -> dict[str, Any]:
        raw_text = str(args.get("raw_text") or args.get("text") or "").strip()
        confirmation = self._latest_habit_confirmation(sender_id)
        plan_draft_id = str(args.get("plan_draft_id") or args.get("plan_id") or "").strip()
        if plan_draft_id:
            try:
                plan = dict(self.store.get_plan_draft(plan_draft_id).payload)
            except KeyError:
                plan = {"source_capture_id": capture_id}
        elif confirmation:
            plan = self._habit_plan_from_confirmation(confirmation)
        else:
            plan = {"source_capture_id": capture_id}
        plan.setdefault("source_capture_id", capture_id)
        apply_suggestions = self._accepts_habit_suggestions(raw_text)
        plan = self._merge_habit_plan_from_text(plan, raw_text, apply_suggestions=apply_suggestions)
        missing = self._habit_missing_fields(plan)
        if missing:
            return await self._save_habit_refinement(
                plan,
                agent_run_id=agent_run_id,
                sender_id=sender_id,
                confirmation=confirmation if confirmation and confirmation.confirmation_type == "habit_refinement" else None,
            )
        if confirmation:
            self.store.cancel_confirmation(confirmation.id)
        return await self._create_habit_schedule_confirmation(
            self._finalize_habit_plan(plan),
            agent_run_id=agent_run_id,
            sender_id=sender_id,
        )

    async def _save_habit_refinement(
        self,
        plan: dict[str, Any],
        *,
        agent_run_id: str | None,
        sender_id: str | None,
        confirmation: Any | None,
    ) -> dict[str, Any]:
        plan = self._upsert_habit_plan_draft(plan, sender_id=sender_id, status=PlanDraftStatus.refining.value)
        call = self._habit_state_call(plan, requires_confirmation=False)
        if confirmation:
            confirmation = self.store.update_confirmation_payload(
                confirmation.id,
                [call.model_dump(mode="json")],
                confirmation_type="habit_refinement",
            )
        else:
            confirmation = self.store.create_confirmation(
                agent_run_id=agent_run_id,
                confirmation_type="habit_refinement",
                proposed_tool_calls_json=[call.model_dump(mode="json")],
                sender_id=sender_id,
            )
        missing = self._habit_missing_fields(plan)
        card = self._habit_refinement_card(plan, missing, confirmation.id)
        card_result = await self.feishu.send_card(sender_id, card)
        self.store.update_confirmation_card_id(confirmation.id, _card_id(card_result))
        return {
            "confirmation_id": confirmation.id,
            "habit_plan": plan,
            "plan_draft": self.store.get_plan_draft(plan["plan_draft_id"]).model_dump(mode="json") if plan.get("plan_draft_id") else None,
            "missing_fields": missing,
            "feishu": card_result,
            "reply_text": self._habit_refinement_reply(plan, missing),
        }

    async def _create_habit_schedule_confirmation(
        self,
        plan: dict[str, Any],
        *,
        agent_run_id: str | None,
        sender_id: str | None,
    ) -> dict[str, Any]:
        planned_events = self._build_habit_events(plan)
        if not planned_events:
            plan["duration_days"] = max(int(plan.get("duration_days") or 30), 30)
            planned_events = self._build_habit_events(plan)
        if not planned_events:
            return {
                "habit_plan": plan,
                "reply_text": "养成卡信息已经够了，但未来窗口里没有找到可用空闲时间。请换一个偏好时段。",
            }
        plan = {**plan, "planned_events": planned_events}
        plan = self._upsert_habit_plan_draft(plan, sender_id=sender_id, status=PlanDraftStatus.schedule_pending.value)
        call = self._habit_state_call(plan, requires_confirmation=True)
        confirmation = self.store.create_confirmation(
            agent_run_id=agent_run_id,
            confirmation_type="habit_schedule",
            proposed_tool_calls_json=[call.model_dump(mode="json")],
            sender_id=sender_id,
        )
        candidates = [self._candidate_summary(call)]
        prompt = self._habit_schedule_prompt(plan, planned_events)
        card = confirmation_card(prompt, confirmation.id, candidates)
        card_result = await self.feishu.send_card(sender_id, card)
        self.store.update_confirmation_card_id(confirmation.id, _card_id(card_result))
        return {
            "confirmation_id": confirmation.id,
            "habit_plan": plan,
            "plan_draft": self.store.get_plan_draft(plan["plan_draft_id"]).model_dump(mode="json") if plan.get("plan_draft_id") else None,
            "planned_events": planned_events,
            "feishu": card_result,
            "reply_text": prompt,
        }

    async def _confirm_habit_schedule(
        self,
        args: dict[str, Any],
        confirmation: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        plan = self._finalize_habit_plan(args)
        planned_events = list(args.get("planned_events") or self._build_habit_events(plan))
        if not planned_events:
            raise ValueError("养成计划没有可创建的日历候选。")
        created: list[dict[str, Any]] = []
        synced: list[dict[str, Any]] = []
        task_payload = self._habit_task_payload(plan)
        task = self.store.create_action_item(task_payload)
        created.append({"type": "action_item", **task.model_dump(mode="json")})
        synced.append(await self.feishu.sync_task(task.model_dump(mode="json")))
        for index, event_args in enumerate(planned_events, start=1):
            event = self.store.create_calendar_event(
                {
                    **event_args,
                    "description": f"{event_args.get('description') or ''}\n来源养成任务：{task.id}".strip(),
                    "source_capture_id": plan.get("source_capture_id") or self._capture_from_confirmation(confirmation),
                    "plan_draft_id": plan.get("plan_draft_id"),
                    "plan_item_id": event_args.get("plan_item_id") or f"habit_{index}",
                }
            )
            created.append({"type": "calendar_event", **event.model_dump(mode="json")})
            sync_result = await self.feishu.sync_calendar_event(event.model_dump(mode="json"))
            self._store_feishu_event_id(event.id, sync_result)
            synced.append(sync_result)
        if plan.get("plan_draft_id"):
            self.store.update_plan_draft(
                str(plan["plan_draft_id"]),
                {"status": PlanDraftStatus.confirmed.value, "payload": {**plan, "action_item_id": task.id}},
            )
        return created, synced

    async def _confirm_plan_schedule(
        self,
        args: dict[str, Any],
        confirmation: Any,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        plan_id = str(args.get("plan_id") or "").strip()
        if not plan_id:
            raise ValueError("确认长期日程草案缺少 plan_id。")
        draft = self.store.get_plan_draft(plan_id)
        if draft.kind.value != "course_timetable":
            raise ValueError(f"暂不支持确认 {draft.kind.value} 类型的通用日程草案。")
        planned_events = list(args.get("planned_events") or draft.payload.get("planned_events") or [])
        if not planned_events:
            planned_events = self._build_course_timetable_events(draft.payload, plan_draft_id=draft.id)
        if not planned_events:
            raise ValueError("课程表草案没有可创建的未来日历事件。")
        created: list[dict[str, Any]] = []
        synced: list[dict[str, Any]] = []
        for event_args in planned_events:
            event = self.store.create_calendar_event(
                {
                    **event_args,
                    "source_capture_id": event_args.get("source_capture_id")
                    or draft.source_capture_id
                    or self._capture_from_confirmation(confirmation),
                    "plan_draft_id": draft.id,
                }
            )
            created.append({"type": "calendar_event", **event.model_dump(mode="json")})
            sync_result = await self.feishu.sync_calendar_event(event.model_dump(mode="json"))
            self._store_feishu_event_id(event.id, sync_result)
            synced.append(sync_result)
        self.store.update_plan_draft(
            draft.id,
            {
                "status": PlanDraftStatus.confirmed.value,
                "payload": {**draft.payload, "planned_events": planned_events},
                "missing_fields": [],
            },
        )
        return created, synced

    def _infer_plan_kind(self, args: dict[str, Any]) -> str:
        text = str(args.get("raw_text") or args.get("text") or args.get("title") or "")
        if any(token in text for token in ("课程表", "课表", "上课", "节课", "节次")):
            return "course_timetable"
        if any(token in text for token in ("习惯", "养成", "锻炼", "运动", "保持健康", "长期计划")):
            return "habit"
        return "long_term_schedule"

    def _latest_active_plan_draft(self, sender_id: str | None):
        return self.store.get_latest_plan_draft(
            sender_id=sender_id,
            statuses=[
                PlanDraftStatus.refining.value,
                PlanDraftStatus.ready_for_schedule.value,
                PlanDraftStatus.schedule_pending.value,
            ],
        )

    def _latest_confirmation_for_plan(
        self,
        sender_id: str | None,
        plan_id: str,
        confirmation_types: set[str],
    ):
        for confirmation in self.store.list_pending_confirmations(sender_id=sender_id, limit=10):
            if confirmation.confirmation_type not in confirmation_types:
                continue
            for raw_call in confirmation.proposed_tool_calls_json:
                if (
                    isinstance(raw_call, dict)
                    and isinstance(raw_call.get("arguments"), dict)
                    and str(raw_call["arguments"].get("plan_id") or raw_call["arguments"].get("plan_draft_id") or "") == plan_id
                ):
                    return confirmation
        return None

    def _cancel_latest_plan_confirmation(self, sender_id: str | None, confirmation_types: set[str]) -> None:
        for confirmation in self.store.list_pending_confirmations(sender_id=sender_id, limit=10):
            if confirmation.confirmation_type in confirmation_types:
                self.store.cancel_confirmation(confirmation.id)
                return

    def _cancel_plan_draft_for_confirmation(self, confirmation: Any) -> None:
        for raw_call in confirmation.proposed_tool_calls_json:
            if not isinstance(raw_call, dict) or not isinstance(raw_call.get("arguments"), dict):
                continue
            args = raw_call["arguments"]
            plan_id = args.get("plan_id") or args.get("plan_draft_id")
            if not plan_id:
                continue
            try:
                draft = self.store.get_plan_draft(str(plan_id))
            except KeyError:
                continue
            if draft.status != PlanDraftStatus.confirmed:
                self.store.update_plan_draft(draft.id, {"status": PlanDraftStatus.canceled.value})

    def _cancel_conflicting_plan_confirmations(self, sender_id: str | None, args: dict[str, Any]) -> None:
        raw_text = str(args.get("raw_text") or args.get("text") or "")
        if "不是" not in raw_text:
            return
        if not any(token in raw_text for token in ("课程表", "课表", "日程", "上课")):
            return
        for confirmation in self.store.list_pending_confirmations(sender_id=sender_id, limit=5):
            if confirmation.confirmation_type in {"time_budget_calendar", "schedule_blocks", "create_candidates"}:
                self.store.cancel_confirmation(confirmation.id)

    def _course_timetable_payload_from_args(self, args: dict[str, Any], *, capture_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in ("extracted_payload", "payload", "course_timetable"):
            value = args.get(key)
            if isinstance(value, dict):
                payload.update(value)
        for key in ("period_map", "term_anchor", "courses", "confidence", "title"):
            if key in args and args[key] is not None:
                payload[key] = args[key]
        raw_text = str(args.get("raw_text") or args.get("text") or payload.get("raw_text") or "").strip()
        if raw_text:
            payload["raw_text"] = raw_text
        if args.get("attachment_refs"):
            payload["attachment_refs"] = list(args.get("attachment_refs") or [])
        payload["source_capture_id"] = payload.get("source_capture_id") or capture_id
        return self._normalize_course_timetable_payload(payload, capture_id=capture_id)

    def _merge_course_timetable_payload(
        self,
        current: dict[str, Any],
        args: dict[str, Any],
        *,
        capture_id: str,
    ) -> dict[str, Any]:
        incoming = self._course_timetable_payload_from_args(args, capture_id=capture_id)
        patch = args.get("patch") if isinstance(args.get("patch"), dict) else {}
        merged = {**current, **incoming, **patch}
        if current.get("courses") and not incoming.get("courses") and not patch.get("courses"):
            merged["courses"] = current["courses"]
        if current.get("period_map") and not incoming.get("period_map") and not patch.get("period_map"):
            merged["period_map"] = current["period_map"]
        if current.get("term_anchor") and not incoming.get("term_anchor") and not patch.get("term_anchor"):
            merged["term_anchor"] = current["term_anchor"]
        return self._normalize_course_timetable_payload(merged, capture_id=capture_id)

    def _normalize_course_timetable_payload(self, payload: dict[str, Any], *, capture_id: str) -> dict[str, Any]:
        raw_text = str(payload.get("raw_text") or "")
        period_map = self._normalize_course_period_map(payload.get("period_map"))
        if not period_map:
            period_map = self._course_period_map_from_text(raw_text)
        if not period_map and any(token in raw_text for token in ("一二节", "三四节", "上午", "下午", "晚上", "课表")):
            period_map = dict(DEFAULT_COURSE_PERIOD_MAP)
        term_anchor = self._course_term_anchor(payload.get("term_anchor"), raw_text=raw_text, capture_id=capture_id)
        courses = self._normalize_course_entries(payload.get("courses"), period_map)
        confidence_values = [float(course.get("confidence", 0.5)) for course in courses if course.get("confidence") is not None]
        confidence = float(payload.get("confidence") or (sum(confidence_values) / len(confidence_values) if confidence_values else 0.55))
        return {
            **payload,
            "kind": "course_timetable",
            "title": payload.get("title") or "课程表导入",
            "period_map": period_map,
            "term_anchor": term_anchor,
            "courses": courses,
            "confidence": confidence,
        }

    def _normalize_course_period_map(self, value: Any) -> dict[str, dict[str, str]]:
        if not isinstance(value, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for raw_key, raw_value in value.items():
            key = self._normalize_course_period_label(raw_key)
            if not key:
                continue
            start_time = None
            end_time = None
            if isinstance(raw_value, dict):
                start_time = self._normalize_clock_time(raw_value.get("start_time") or raw_value.get("start") or raw_value.get("begin"), key)
                end_time = self._normalize_clock_time(raw_value.get("end_time") or raw_value.get("end") or raw_value.get("finish"), key)
            elif isinstance(raw_value, str):
                times = re.findall(r"(?<!\d)(\d{1,2})[:：](\d{1,2})(?!\d)", raw_value)
                if len(times) >= 2:
                    start_time = self._normalize_clock_time(":".join(times[0]), key)
                    end_time = self._normalize_clock_time(":".join(times[1]), key)
            if start_time and end_time:
                out[key] = {"start_time": start_time, "end_time": end_time}
        return out

    def _course_period_map_from_text(self, text: str) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        pattern = re.compile(
            r"(?P<label>(?:第)?(?:\d{1,2}\s*[-到至]\s*\d{1,2}|[一二三四五六七八九十]+)节).{0,24}?"
            r"(?P<start>\d{1,2}[:：]\d{1,2}).{0,24}?(?P<end>\d{1,2}[:：]\d{1,2})"
        )
        for match in pattern.finditer(text):
            label = self._normalize_course_period_label(match.group("label"))
            if not label:
                continue
            start_time = self._normalize_clock_time(match.group("start"), label)
            end_time = self._normalize_clock_time(match.group("end"), label)
            if start_time and end_time:
                out[label] = {"start_time": start_time, "end_time": end_time}
        return out

    def _course_term_anchor(self, value: Any, *, raw_text: str, capture_id: str) -> dict[str, Any]:
        anchor = dict(value) if isinstance(value, dict) else {}
        message_dt = self._course_message_datetime(anchor.get("message_date"), capture_id)
        current_week = self._int_or_none(anchor.get("current_teaching_week")) or self._parse_current_teaching_week(raw_text)
        inferred_week1 = anchor.get("inferred_week1_monday")
        if not inferred_week1 and current_week:
            current_monday = message_dt.date() - timedelta(days=message_dt.weekday())
            inferred_week1 = (current_monday - timedelta(days=(current_week - 1) * 7)).isoformat()
        return {
            **anchor,
            "current_teaching_week": current_week,
            "message_date": message_dt.isoformat(),
            "inferred_week1_monday": inferred_week1,
            "needs_confirmation": bool(anchor.get("needs_confirmation", True)) if inferred_week1 else True,
        }

    def _course_message_datetime(self, value: Any, capture_id: str) -> datetime:
        if value:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=self.tz)
                return parsed.astimezone(self.tz)
            except ValueError:
                pass
        try:
            capture = self.store.get_capture(capture_id)
            return capture.received_at.astimezone(self.tz) if capture.received_at.tzinfo else capture.received_at.replace(tzinfo=self.tz)
        except KeyError:
            return datetime.now(self.tz)

    def _parse_current_teaching_week(self, text: str) -> int | None:
        match = re.search(r"第\s*(?P<week>\d{1,2})\s*周", text)
        if not match:
            return None
        week = int(match.group("week"))
        return week if 1 <= week <= 30 else None

    def _normalize_course_entries(self, value: Any, period_map: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        courses: list[dict[str, Any]] = []
        for index, raw in enumerate(value, start=1):
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or raw.get("course_name") or raw.get("name") or "").strip()
            weekday = self._normalize_course_weekday(raw.get("weekday") or raw.get("day") or raw.get("byday"))
            period = self._normalize_course_period_label(raw.get("period") or raw.get("period_label") or raw.get("section"))
            ranges = self._course_week_ranges(raw)
            timing = period_map.get(period or "")
            start_time = self._normalize_clock_time(raw.get("start_time"), period) or (timing or {}).get("start_time")
            end_time = self._normalize_clock_time(raw.get("end_time"), period) or (timing or {}).get("end_time")
            if not title or not weekday or not period or not start_time or not end_time or not ranges:
                courses.append(
                    {
                        **raw,
                        "title": title,
                        "weekday": weekday,
                        "period": period,
                        "week_ranges": ranges,
                        "start_time": start_time,
                        "end_time": end_time,
                        "plan_item_id": str(raw.get("plan_item_id") or raw.get("id") or f"course_{index}"),
                        "confidence": float(raw.get("confidence") or 0.45),
                        "incomplete": True,
                    }
                )
                continue
            courses.append(
                {
                    "plan_item_id": str(raw.get("plan_item_id") or raw.get("id") or f"course_{index}"),
                    "title": title,
                    "weekday": weekday,
                    "weekday_label": DAY_LABELS[weekday],
                    "period": period,
                    "start_time": start_time,
                    "end_time": end_time,
                    "week_ranges": ranges,
                    "weeks_text": raw.get("weeks_text") or raw.get("week_text") or raw.get("weeks"),
                    "location": raw.get("location") or raw.get("room"),
                    "teacher": raw.get("teacher"),
                    "class_name": raw.get("class_name") or raw.get("class"),
                    "evidence_text": raw.get("evidence_text") or raw.get("raw_text") or title,
                    "confidence": float(raw.get("confidence") or 0.75),
                }
            )
        return courses

    def _normalize_course_weekday(self, value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, int):
            if 1 <= value <= 7:
                return HABIT_DAY_CODES[value - 1]
            if 0 <= value <= 6:
                return HABIT_DAY_CODES[value]
        text = str(value).strip().upper()
        if text in DAY_LABELS:
            return text
        mapping = {
            "星期一": "MO",
            "周一": "MO",
            "星期二": "TU",
            "周二": "TU",
            "星期三": "WE",
            "周三": "WE",
            "星期四": "TH",
            "周四": "TH",
            "星期五": "FR",
            "周五": "FR",
            "星期六": "SA",
            "周六": "SA",
            "星期日": "SU",
            "星期天": "SU",
            "周日": "SU",
            "周天": "SU",
            "MONDAY": "MO",
            "TUESDAY": "TU",
            "WEDNESDAY": "WE",
            "THURSDAY": "TH",
            "FRIDAY": "FR",
            "SATURDAY": "SA",
            "SUNDAY": "SU",
        }
        return mapping.get(text)

    def _normalize_course_period_label(self, value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        text = text.replace("第", "").replace("节", "").replace("课", "").replace(" ", "")
        text = text.replace("到", "-").replace("至", "-")
        match = re.search(r"(?P<start>\d{1,2})-(?P<end>\d{1,2})", text)
        if match:
            return f"{int(match.group('start'))}-{int(match.group('end'))}"
        digits = re.findall(r"\d{1,2}", text)
        if len(digits) >= 2:
            return f"{int(digits[0])}-{int(digits[1])}"
        chinese_pairs = {
            "一二": "1-2",
            "二一": "1-2",
            "三四": "3-4",
            "四三": "3-4",
            "五六": "5-6",
            "六五": "5-6",
            "七八": "7-8",
            "八七": "7-8",
            "九十": "9-10",
            "十十一": "10-11",
            "十一十二": "11-12",
        }
        for key, label in chinese_pairs.items():
            if key in text:
                return label
        if text in {"一", "1"}:
            return "1-2"
        return None

    def _normalize_clock_time(self, value: Any, period_label: str | None = None) -> str | None:
        if value is None:
            return None
        match = re.search(r"(?<!\d)(?P<hour>\d{1,2})[:：](?P<minute>\d{1,2})(?!\d)", str(value))
        if not match:
            return None
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        if period_label in {"5-6", "7-8"} and hour < 12:
            hour += 12
        if period_label in {"9-10", "10-11", "11-12"} and hour < 12:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return None

    def _course_week_ranges(self, raw: dict[str, Any]) -> list[list[int]]:
        value = raw.get("week_ranges") or raw.get("weeks") or raw.get("teaching_weeks")
        ranges: list[list[int]] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    start = self._int_or_none(item.get("start") or item.get("from"))
                    end = self._int_or_none(item.get("end") or item.get("to")) or start
                elif isinstance(item, (list, tuple)) and item:
                    start = self._int_or_none(item[0])
                    end = self._int_or_none(item[1] if len(item) > 1 else item[0])
                else:
                    start = self._int_or_none(item)
                    end = start
                if start and end:
                    ranges.append([min(start, end), max(start, end)])
        text = str(raw.get("weeks_text") or raw.get("week_text") or "")
        if text:
            ranges.extend(self._parse_week_ranges_text(text))
        return self._merge_week_ranges(ranges)

    def _parse_week_ranges_text(self, text: str) -> list[list[int]]:
        ranges: list[list[int]] = []
        for start, end in re.findall(r"(\d{1,2})\s*[-到至]\s*(\d{1,2})\s*周?", text):
            ranges.append([int(start), int(end)])
        consumed = re.sub(r"\d{1,2}\s*[-到至]\s*\d{1,2}\s*周?", "", text)
        for single in re.findall(r"(?<!\d)(\d{1,2})\s*周", consumed):
            week = int(single)
            ranges.append([week, week])
        return ranges

    def _merge_week_ranges(self, ranges: list[list[int]]) -> list[list[int]]:
        normalized = sorted([item for item in ranges if len(item) == 2 and item[0] > 0 and item[1] > 0])
        merged: list[list[int]] = []
        for start, end in normalized:
            if not merged or start > merged[-1][1] + 1:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return merged

    def _int_or_none(self, value: Any) -> int | None:
        if value in {None, ""}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _course_timetable_missing_fields(self, payload: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        if not payload.get("period_map"):
            missing.append("节次时间")
        anchor = payload.get("term_anchor") if isinstance(payload.get("term_anchor"), dict) else {}
        if not anchor.get("current_teaching_week") or not anchor.get("inferred_week1_monday"):
            missing.append("教学周锚点")
        courses = list(payload.get("courses") or [])
        valid_courses = [course for course in courses if not course.get("incomplete")]
        if not valid_courses:
            missing.append("课程列表")
        if any(course.get("incomplete") for course in courses):
            missing.append("部分课程字段")
        return list(dict.fromkeys(missing))

    def _course_timetable_title(self, payload: dict[str, Any]) -> str:
        courses = [course for course in payload.get("courses", []) if isinstance(course, dict) and course.get("title")]
        if courses:
            return f"课程表导入（{len(courses)} 门课）"
        return str(payload.get("title") or "课程表导入")

    def _build_course_timetable_events(self, payload: dict[str, Any], *, plan_draft_id: str) -> list[dict[str, Any]]:
        anchor = payload.get("term_anchor") if isinstance(payload.get("term_anchor"), dict) else {}
        week1_text = anchor.get("inferred_week1_monday")
        if not week1_text:
            return []
        try:
            week1_monday = datetime.fromisoformat(str(week1_text)).date()
        except ValueError:
            return []
        reference_now = self._course_reference_now(anchor)
        events: list[dict[str, Any]] = []
        for course in payload.get("courses") or []:
            if not isinstance(course, dict) or course.get("incomplete"):
                continue
            weekday = self._normalize_course_weekday(course.get("weekday"))
            if not weekday:
                continue
            weekday_index = HABIT_DAY_CODES.index(weekday)
            for week in self._expand_week_ranges(course.get("week_ranges")):
                day = week1_monday + timedelta(days=(week - 1) * 7 + weekday_index)
                start_at = self._datetime_on_day(datetime.combine(day, datetime.min.time(), tzinfo=self.tz), str(course["start_time"]))
                end_at = self._datetime_on_day(datetime.combine(day, datetime.min.time(), tzinfo=self.tz), str(course["end_time"]))
                if end_at <= start_at:
                    end_at += timedelta(days=1)
                if end_at <= reference_now:
                    continue
                events.append(
                    {
                        "title": str(course["title"]),
                        "description": self._course_event_description(course, week, payload, plan_draft_id),
                        "start_at": start_at.isoformat(),
                        "end_at": end_at.isoformat(),
                        "location": course.get("location"),
                        "confidence": float(course.get("confidence") or payload.get("confidence") or 0.75),
                        "plan_draft_id": plan_draft_id,
                        "plan_item_id": str(course.get("plan_item_id") or ""),
                        "source_capture_id": payload.get("source_capture_id"),
                    }
                )
        return sorted(events, key=lambda item: item["start_at"])

    def _course_reference_now(self, anchor: dict[str, Any]) -> datetime:
        message_date = anchor.get("message_date")
        if message_date:
            try:
                parsed = datetime.fromisoformat(str(message_date).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=self.tz)
                return parsed.astimezone(self.tz)
            except ValueError:
                pass
        return datetime.now(self.tz)

    def _expand_week_ranges(self, ranges: Any) -> list[int]:
        weeks: list[int] = []
        if not isinstance(ranges, list):
            return weeks
        for item in ranges:
            if not isinstance(item, list) or len(item) != 2:
                continue
            start = self._int_or_none(item[0])
            end = self._int_or_none(item[1])
            if not start or not end:
                continue
            weeks.extend(range(min(start, end), max(start, end) + 1))
        return sorted(set(weeks))

    def _course_event_description(
        self,
        course: dict[str, Any],
        week: int,
        payload: dict[str, Any],
        plan_draft_id: str,
    ) -> str:
        lines = [
            "课程表导入",
            f"教学周：第 {week} 周",
            f"节次：{course.get('period')}（{course.get('start_time')}-{course.get('end_time')}）",
            f"来源草案：{plan_draft_id}",
        ]
        if course.get("teacher"):
            lines.append(f"老师：{course['teacher']}")
        if course.get("class_name"):
            lines.append(f"班级：{course['class_name']}")
        if course.get("evidence_text"):
            lines.append(f"识别证据：{course['evidence_text']}")
        anchor = payload.get("term_anchor") if isinstance(payload.get("term_anchor"), dict) else {}
        if anchor.get("inferred_week1_monday"):
            lines.append(f"第1周周一：{anchor['inferred_week1_monday']}")
        return "\n".join(lines)

    def _plan_refinement_reply(self, draft: dict[str, Any]) -> str:
        if draft.get("kind") == "course_timetable":
            missing = draft.get("missing_fields") or []
            if missing:
                return f"我先建一张课程表草案，还缺：{'、'.join(missing)}。补齐后我再生成日程确认卡。"
            return "课程表草案已经完整，我会生成日程确认卡。"
        missing = draft.get("missing_fields") or []
        if missing:
            return f"我先建一张长期日程草案，还缺：{'、'.join(missing)}。"
        return "长期日程草案已经完整。"

    def _plan_refinement_card(self, draft: dict[str, Any], confirmation_id: str) -> dict[str, Any]:
        cancel_value = {"action": "cancel", "confirmation_id": confirmation_id}
        title = "课程表草案待完善" if draft.get("kind") == "course_timetable" else "长期日程草案待完善"
        return {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": self._plan_refinement_markdown(draft)}},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "name": f"cancel_plan_{confirmation_id}",
                            "text": {"tag": "plain_text", "content": "取消讨论"},
                            "type": "default",
                            "value": cancel_value,
                            "behaviors": [{"type": "callback", "value": cancel_value}],
                        }
                    ],
                },
            ],
            "_mvp_meta": {"confirmation_id": confirmation_id, "plan_draft": draft},
        }

    def _plan_refinement_markdown(self, draft: dict[str, Any]) -> str:
        if draft.get("kind") != "course_timetable":
            return "\n".join(
                [
                    "**长期日程草案**",
                    f"**标题**：{draft.get('title') or '待定'}",
                    f"**还缺**：{'、'.join(draft.get('missing_fields') or []) or '无'}",
                ]
            )
        payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
        anchor = payload.get("term_anchor") if isinstance(payload.get("term_anchor"), dict) else {}
        courses = [course for course in payload.get("courses", []) if isinstance(course, dict)]
        lines = [
            "**课程表草案**",
            f"**第1周周一**：{anchor.get('inferred_week1_monday') or '待确认'}",
            f"**当前教学周**：{anchor.get('current_teaching_week') or '待补充'}",
            f"**节次时间**：{self._course_period_map_text(payload.get('period_map'))}",
            f"**已识别课程**：{len([course for course in courses if not course.get('incomplete')])} 门",
        ]
        for course in courses[:8]:
            lines.append(
                f"- {course.get('weekday_label') or course.get('weekday') or '?'} "
                f"{course.get('period') or '?'} {course.get('start_time') or '?'}-{course.get('end_time') or '?'} "
                f"{course.get('title') or '未命名课程'} "
                f"{self._course_ranges_text(course.get('week_ranges'))}"
            )
        if len(courses) > 8:
            lines.append(f"- 另有 {len(courses) - 8} 门课")
        if draft.get("missing_fields"):
            lines.append(f"**还缺**：{'、'.join(draft['missing_fields'])}")
            lines.append("你可以继续补充缺少的课程、教学周或节次时间。")
        return "\n".join(lines)

    def _course_period_map_text(self, value: Any) -> str:
        if not isinstance(value, dict) or not value:
            return "待补充"
        parts = []
        for key in sorted(value, key=lambda item: self._time_minutes(value[item]["start_time"]) if isinstance(value.get(item), dict) and value[item].get("start_time") else 9999):
            item = value.get(key)
            if isinstance(item, dict):
                parts.append(f"{key}节 {item.get('start_time')}-{item.get('end_time')}")
        return "；".join(parts) if parts else "待补充"

    def _course_ranges_text(self, ranges: Any) -> str:
        if not isinstance(ranges, list) or not ranges:
            return ""
        return "、".join(f"{item[0]}-{item[1]}周" if item[0] != item[1] else f"{item[0]}周" for item in ranges if isinstance(item, list) and len(item) == 2)

    def _course_timetable_schedule_prompt(self, draft: dict[str, Any], events: list[dict[str, Any]]) -> str:
        payload = draft.get("payload") if isinstance(draft.get("payload"), dict) else {}
        anchor = payload.get("term_anchor") if isinstance(payload.get("term_anchor"), dict) else {}
        courses = payload.get("courses") if isinstance(payload.get("courses"), list) else []
        lines = [
            f"课程表草案已解析：**{draft.get('title') or '课程表导入'}**",
            f"第1周周一推断为：{anchor.get('inferred_week1_monday') or '待确认'}；当前教学周：第 {anchor.get('current_teaching_week') or '?'} 周。",
            f"识别到 {len(courses)} 门课，将创建 {len(events)} 个未来日历事件。",
            "确认后我再写入飞书日历；如果周次、节次或课程有误，直接回复修改内容即可。",
        ]
        for event in events[:12]:
            lines.append(f"- {self._format_event_candidate_time(event)} {event.get('title')}")
        if len(events) > 12:
            lines.append(f"- 另有 {len(events) - 12} 个课程事件")
        return "\n".join(lines)

    def _upsert_habit_plan_draft(
        self,
        plan: dict[str, Any],
        *,
        sender_id: str | None,
        status: str,
    ) -> dict[str, Any]:
        title = str(plan.get("title") or "习惯养成")
        missing = self._habit_missing_fields(plan) if status == PlanDraftStatus.refining.value else []
        plan_id = str(plan.get("plan_draft_id") or "").strip()
        payload = {key: value for key, value in plan.items() if key != "plan_draft_id"}
        if plan_id:
            try:
                draft = self.store.update_plan_draft(
                    plan_id,
                    {
                        "title": title,
                        "payload": payload,
                        "missing_fields": missing,
                        "status": status,
                        "confidence": float(plan.get("confidence") or 0.8),
                    },
                )
            except KeyError:
                draft = self.store.create_plan_draft(
                    kind="habit",
                    title=title,
                    payload=payload,
                    missing_fields=missing,
                    status=status,
                    source_capture_id=plan.get("source_capture_id"),
                    sender_id=sender_id,
                    confidence=float(plan.get("confidence") or 0.8),
                )
        else:
            draft = self.store.create_plan_draft(
                kind="habit",
                title=title,
                payload=payload,
                missing_fields=missing,
                status=status,
                source_capture_id=plan.get("source_capture_id"),
                sender_id=sender_id,
                confidence=float(plan.get("confidence") or 0.8),
            )
        updated_plan = {**draft.payload, "plan_draft_id": draft.id}
        if plan.get("planned_events"):
            updated_plan["planned_events"] = plan["planned_events"]
        return updated_plan

    def _habit_state_call(self, plan: dict[str, Any], *, requires_confirmation: bool) -> AgentToolCall:
        return AgentToolCall(
            tool_name="schedule_habit_plan",
            risk_level=RiskLevel.medium if requires_confirmation else RiskLevel.low,
            requires_confirmation=requires_confirmation,
            arguments={key: value for key, value in plan.items() if value is not None},
        )

    def _latest_habit_confirmation(self, sender_id: str | None):
        for confirmation in self.store.list_pending_confirmations(sender_id=sender_id, limit=8):
            if confirmation.confirmation_type in HABIT_CONFIRMATION_TYPES:
                return confirmation
        return None

    def _habit_plan_from_confirmation(self, confirmation: Any) -> dict[str, Any]:
        for raw_call in confirmation.proposed_tool_calls_json:
            if not isinstance(raw_call, dict):
                continue
            if raw_call.get("tool_name") != "schedule_habit_plan":
                continue
            args = raw_call.get("arguments")
            if isinstance(args, dict):
                return dict(args)
        return {}

    def _merge_habit_plan_from_text(
        self,
        plan: dict[str, Any],
        raw_text: str,
        *,
        apply_suggestions: bool,
    ) -> dict[str, Any]:
        out = {key: value for key, value in plan.items() if value is not None and value != ""}
        text = raw_text.strip()
        if text:
            out.setdefault("original_request", text)
        title = self._habit_title(text)
        if title:
            out["title"] = title
        goal = self._habit_goal(text)
        if goal:
            out["goal"] = goal
        method = self._habit_method(text)
        if method:
            out["method"] = method
        minutes = self._habit_session_minutes(text)
        if minutes:
            out["session_minutes"] = minutes
            out["daily_minutes"] = minutes
        frequency = self._habit_frequency(text)
        if frequency:
            out.update(frequency)
        preferred_time = self._habit_preferred_time(text)
        if preferred_time:
            out["preferred_time"] = preferred_time
            out["window_start"] = preferred_time
            start_dt = self._datetime_on_day(datetime.now(self.tz), preferred_time)
            out["window_end"] = (start_dt + timedelta(hours=2)).strftime("%H:%M")
        else:
            window = self._habit_window(text)
            if window:
                out["window_start"], out["window_end"] = window
        duration_days = self._habit_duration_days(text)
        if duration_days:
            out["duration_days"] = duration_days
        start_date = self._habit_start_date(text)
        if start_date:
            out["start_date"] = start_date
        if apply_suggestions:
            out = self._finalize_habit_plan(out)
        return out

    def _finalize_habit_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        out = dict(plan)
        out.setdefault("title", "习惯养成")
        if not out.get("method"):
            out["method"] = "快走 + 拉伸" if self._habit_category(out) == "exercise" else "按计划执行"
        out["session_minutes"] = self._bounded_int(out.get("session_minutes"), default=30, minimum=5, maximum=180)
        out["daily_minutes"] = self._bounded_int(out.get("daily_minutes"), default=out["session_minutes"], minimum=5, maximum=240)
        out.setdefault("byday", HABIT_DEFAULT_BYDAY)
        out.setdefault("frequency", "每天")
        out.setdefault("window_start", "19:30")
        out.setdefault("window_end", "21:30")
        out.setdefault("duration_days", 30)
        out.setdefault("start_date", (datetime.now(self.tz) + timedelta(days=1)).date().isoformat())
        out.setdefault("buffer_minutes", 20)
        return out

    def _habit_missing_fields(self, plan: dict[str, Any]) -> list[str]:
        missing = []
        checks = [
            ("method", "锻炼方式"),
            ("session_minutes", "每次时长"),
            ("byday", "频率"),
            ("window_start", "偏好时段"),
            ("duration_days", "先坚持多久"),
        ]
        for key, label in checks:
            if not plan.get(key):
                missing.append(label)
        return missing

    def _habit_refinement_reply(self, plan: dict[str, Any], missing: list[str]) -> str:
        title = str(plan.get("title") or "这个习惯")
        if missing:
            return f"我先建一张“{title}”养成卡，还缺：{'、'.join(missing)}。补充后我再生成日程确认卡。"
        return f"“{title}”养成卡已经够完整，我会生成日程确认卡。"

    def _habit_refinement_card(self, plan: dict[str, Any], missing: list[str], confirmation_id: str) -> dict[str, Any]:
        cancel_value = {"action": "cancel", "confirmation_id": confirmation_id}
        return {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": "养成卡待完善"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": self._habit_refinement_markdown(plan, missing)}},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "name": f"cancel_habit_{confirmation_id}",
                            "text": {"tag": "plain_text", "content": "取消讨论"},
                            "type": "default",
                            "value": cancel_value,
                            "behaviors": [{"type": "callback", "value": cancel_value}],
                        }
                    ],
                },
            ],
            "_mvp_meta": {"confirmation_id": confirmation_id, "habit_plan": plan, "missing_fields": missing},
        }

    def _habit_refinement_markdown(self, plan: dict[str, Any], missing: list[str]) -> str:
        lines = [
            "**养成卡**",
            f"**目标**：{plan.get('title') or '待定'}",
            f"**目的**：{plan.get('goal') or '待补充'}",
            f"**方式**：{plan.get('method') or '待补充'}",
            f"**频率**：{plan.get('frequency') or '待补充'}",
            f"**每次时长**：{self._format_minutes(plan.get('session_minutes')) if plan.get('session_minutes') else '待补充'}",
            f"**偏好时段**：{self._habit_window_text(plan)}",
            f"**周期**：{plan.get('duration_days') or '待补充'} 天",
            "",
            "**建议**：锻炼类习惯可以先用“快走/慢跑 + 拉伸、每天 30 分钟、晚上 19:30-21:30、先坚持 30 天复盘”。",
        ]
        if missing:
            lines.append(f"**还缺**：{'、'.join(missing)}")
            lines.append("你可以直接回复类似：每天晚上8点跑步30分钟，先一个月。")
        return "\n".join(lines)

    def _habit_schedule_prompt(self, plan: dict[str, Any], events: list[dict[str, Any]]) -> str:
        lines = [
            f"养成卡已完善：**{plan.get('title')}**",
            f"方式：{plan.get('method')}；频率：{plan.get('frequency')}；每次：{self._format_minutes(plan.get('session_minutes'))}",
            f"偏好时段：{self._habit_window_text(plan)}；周期：{plan.get('duration_days')} 天",
            f"我生成了 {len(events)} 个日历候选。确认后会创建长期任务并写入这些日程。",
            "如果要改方式、时间或频率，直接回复修改内容即可。",
        ]
        return "\n".join(lines)

    def _habit_task_payload(self, plan: dict[str, Any]) -> dict[str, Any]:
        start_day = datetime.fromisoformat(str(plan["start_date"])).replace(tzinfo=self.tz)
        due_at = start_day + timedelta(days=int(plan["duration_days"]) - 1)
        due_at = due_at.replace(hour=23, minute=59, second=0, microsecond=0)
        events = list(plan.get("planned_events") or [])
        estimated_minutes = int(plan["session_minutes"]) * max(1, len(events))
        title = f"{plan['title']}（习惯养成：{plan.get('frequency')} {self._format_minutes(plan['session_minutes'])}）"
        description = "\n".join(
            [
                "习惯养成计划",
                f"目标：{plan['title']}",
                f"目的：{plan.get('goal') or ''}",
                f"方式：{plan.get('method')}",
                f"频率：{plan.get('frequency')}",
                f"偏好时段：{self._habit_window_text(plan)}",
                f"开始：{plan['start_date']}",
                f"周期：{plan['duration_days']}天",
            ]
        )
        return {
            "title": title,
            "description": description,
            "priority": "P2",
            "due_at": due_at,
            "estimated_minutes": estimated_minutes,
            "source_capture_id": plan.get("source_capture_id"),
            "confidence": 0.9,
        }

    def _build_habit_events(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        plan = self._finalize_habit_plan(plan)
        start_day = datetime.fromisoformat(str(plan["start_date"])).replace(tzinfo=self.tz)
        start_day = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        duration_days = int(plan["duration_days"])
        session_minutes = int(plan["session_minutes"])
        buffer_minutes = self._bounded_int(plan.get("buffer_minutes"), default=20, minimum=0, maximum=120)
        byday = {item.strip() for item in str(plan.get("byday") or HABIT_DEFAULT_BYDAY).split(",") if item.strip()}
        now = datetime.now(self.tz)
        events: list[dict[str, Any]] = []
        for offset in range(duration_days):
            day = start_day + timedelta(days=offset)
            if HABIT_DAY_CODES[day.weekday()] not in byday:
                continue
            slot = self._habit_slot_for_day(day, session_minutes, buffer_minutes, plan, now)
            if not slot:
                continue
            start_at, end_at = slot
            events.append(
                {
                    "title": f"{plan['title']}：{plan.get('method')}",
                    "description": (
                        f"习惯养成计划\n目标：{plan['title']}\n方式：{plan.get('method')}\n"
                        f"频率：{plan.get('frequency')}\n来源捕获：{plan.get('source_capture_id') or ''}"
                    ),
                    "start_at": start_at.isoformat(),
                    "end_at": end_at.isoformat(),
                    "confidence": 0.9,
                    "source_capture_id": plan.get("source_capture_id"),
                }
            )
        return events

    def _habit_slot_for_day(
        self,
        day: datetime,
        session_minutes: int,
        buffer_minutes: int,
        plan: dict[str, Any],
        now: datetime,
    ) -> tuple[datetime, datetime] | None:
        window_start = self._datetime_on_day(day, str(plan.get("window_start") or "19:30"))
        window_end = self._datetime_on_day(day, str(plan.get("window_end") or "21:30"))
        if window_end <= window_start:
            window_end += timedelta(days=1)
        if window_end <= now:
            return None
        if window_start < now:
            window_start = self._round_up(now, minutes=5)
        preferred = str(plan.get("preferred_time") or "")
        if preferred:
            start_at = self._datetime_on_day(day, preferred)
            end_at = start_at + timedelta(minutes=session_minutes)
            if start_at >= now and end_at <= window_end and not self._has_conflict(start_at, end_at, buffer_minutes):
                return start_at, end_at
        busy_ranges = self._buffered_busy_ranges(
            self._busy_ranges(window_start, window_end),
            start=window_start,
            end=window_end,
            buffer_minutes=buffer_minutes,
        )
        for free_range in self._free_ranges(window_start, window_end, busy_ranges):
            start_at = self._round_up(free_range["start"], minutes=5)
            end_at = start_at + timedelta(minutes=session_minutes)
            if end_at <= free_range["end"]:
                return start_at, end_at
        return None

    def _has_conflict(self, start_at: datetime, end_at: datetime, buffer_minutes: int) -> bool:
        busy = self._buffered_busy_ranges(
            self._busy_ranges(start_at - timedelta(minutes=buffer_minutes), end_at + timedelta(minutes=buffer_minutes)),
            start=start_at - timedelta(minutes=buffer_minutes),
            end=end_at + timedelta(minutes=buffer_minutes),
            buffer_minutes=buffer_minutes,
        )
        return any(start_at < item["end"] and end_at > item["start"] for item in busy)

    def _habit_title(self, text: str) -> str | None:
        if any(token in text for token in ("锻炼", "运动", "健身", "健康")):
            return "锻炼身体"
        if "早睡" in text:
            return "早睡"
        if "早起" in text:
            return "早起"
        if "阅读" in text or "读书" in text:
            return "阅读"
        if "背单词" in text:
            return "背单词"
        if "学英语" in text or "英语学习" in text:
            return "英语学习"
        match = re.search(r"(?:长期任务|长期计划|长期安排|长期日程)[，,：:\s]*(?P<title>[^，,。；;]+)", text)
        if match:
            return match.group("title").strip()
        match = re.search(r"(?:养成|坚持|保持|想要|想)\s*(?P<title>[^，,。；;]+?)(?:习惯|$)", text)
        if match:
            return match.group("title").strip()
        return None

    def _habit_goal(self, text: str) -> str | None:
        if "保持健康" in text:
            return "保持健康"
        match = re.search(r"(?:为了|目标是|希望)\s*(?P<goal>[^，,。；;]+)", text)
        return match.group("goal").strip() if match else None

    def _habit_method(self, text: str) -> str | None:
        methods = ["跑步", "慢跑", "快走", "力量训练", "拉伸", "瑜伽", "游泳", "骑行", "冥想", "阅读", "背单词"]
        found = [method for method in methods if method in text]
        if "跑步" in found and "拉伸" in found:
            return "跑步 + 拉伸"
        return found[0] if found else None

    def _habit_session_minutes(self, text: str) -> int | None:
        match = re.search(r"(?P<num>\d{1,3})\s*(?:分钟|分|min)", text, flags=re.IGNORECASE)
        if match:
            return int(match.group("num"))
        if "半小时" in text:
            return 30
        if "一小时" in text or "1小时" in text:
            return 60
        return None

    def _habit_frequency(self, text: str) -> dict[str, Any] | None:
        if any(token in text for token in ("每天", "每日", "天天")):
            return {"frequency": "每天", "byday": HABIT_DEFAULT_BYDAY}
        explicit_days = self._explicit_byday(text)
        if explicit_days:
            return {"frequency": self._byday_label(explicit_days), "byday": ",".join(explicit_days)}
        match = re.search(r"(?:每周|一周)\s*(?P<count>[1-7一二三四五六七])\s*次", text)
        if match:
            count = self._chinese_count(match.group("count"))
            byday = HABIT_DAY_CODES[:count] if count <= 2 else ["MO", "WE", "FR", "SU"][:count]
            return {"frequency": f"每周{count}次", "byday": ",".join(byday)}
        return None

    def _explicit_byday(self, text: str) -> list[str]:
        mapping = {
            "周一": "MO",
            "周二": "TU",
            "周三": "WE",
            "周四": "TH",
            "周五": "FR",
            "周六": "SA",
            "周日": "SU",
            "周天": "SU",
        }
        days = [code for label, code in mapping.items() if label in text]
        return list(dict.fromkeys(days))

    def _byday_label(self, byday: list[str]) -> str:
        labels = {"MO": "周一", "TU": "周二", "WE": "周三", "TH": "周四", "FR": "周五", "SA": "周六", "SU": "周日"}
        return "、".join(labels[item] for item in byday)

    def _chinese_count(self, value: str) -> int:
        if value in {"一", "二", "三", "四", "五", "六", "七"}:
            return {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}[value]
        return int(value) if value.isdigit() else 3

    def _habit_preferred_time(self, text: str) -> str | None:
        match = re.search(r"(?P<period>早上|上午|中午|下午|晚上|晚)?\s*(?P<hour>\d{1,2})(?:[:：点](?P<minute>\d{1,2})?)\s*(?:开始)?", text)
        if not match:
            return None
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        period = match.group("period") or ""
        if period in {"下午", "晚上", "晚"} and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return None

    def _habit_window(self, text: str) -> tuple[str, str] | None:
        if "早上" in text or "上午" in text:
            return "07:00", "09:00"
        if "中午" in text:
            return "12:00", "13:30"
        if "下午" in text:
            return "15:00", "18:00"
        if "晚上" in text or "晚间" in text:
            return "19:30", "21:30"
        return None

    def _habit_duration_days(self, text: str) -> int | None:
        if "一个月" in text or "1个月" in text or "一月" in text:
            return 30
        if "两个月" in text or "2个月" in text:
            return 60
        if "三个月" in text or "3个月" in text:
            return 90
        if "半年" in text:
            return 180
        match = re.search(r"(?P<num>\d{1,3})\s*天", text)
        if match:
            return int(match.group("num"))
        match = re.search(r"(?P<num>\d{1,2})\s*周", text)
        if match:
            return int(match.group("num")) * 7
        if "一周" in text:
            return 7
        return None

    def _habit_start_date(self, text: str) -> str | None:
        today = datetime.now(self.tz).date()
        if "后天" in text:
            return (today + timedelta(days=2)).isoformat()
        if "明天" in text:
            return (today + timedelta(days=1)).isoformat()
        if "今天" in text:
            return today.isoformat()
        return None

    def _accepts_habit_suggestions(self, text: str) -> bool:
        return any(token in text for token in ("按建议", "用建议", "你建议", "默认", "都行", "可以", "就这样"))

    def _habit_category(self, plan: dict[str, Any]) -> str:
        title = str(plan.get("title") or "")
        method = str(plan.get("method") or "")
        if any(token in f"{title}{method}" for token in ("锻炼", "运动", "健身", "跑步", "快走", "力量", "拉伸")):
            return "exercise"
        return str(plan.get("category") or "habit")

    def _habit_window_text(self, plan: dict[str, Any]) -> str:
        if plan.get("preferred_time"):
            return str(plan["preferred_time"])
        if plan.get("window_start") and plan.get("window_end"):
            return f"{plan['window_start']}-{plan['window_end']}"
        return "待补充"

    def _find_time_budget_tasks(
        self,
        query: str,
        *,
        action_item_id: str = "",
        allow_default: bool = False,
        time_budget_only: bool = False,
    ):
        if action_item_id:
            try:
                item = self.store.get_action_item(action_item_id)
            except KeyError:
                return []
            if time_budget_only and not self._is_time_budget_task(item):
                return []
            return [item]

        query = str(query or "").strip()
        if not query:
            matches = self.store.list_action_items() if allow_default else []
        else:
            matches = self.store.find_action_items(query, include_done=True)
        if matches:
            return [item for item in matches if not time_budget_only or self._is_time_budget_task(item)]

        candidates = [
            item
            for item in self.store.list_action_items()
            if not time_budget_only or self._is_time_budget_task(item)
        ]
        fuzzy_matches = self._fuzzy_match_action_items(query, candidates) if query else []
        if fuzzy_matches:
            return fuzzy_matches
        if allow_default and len(candidates) == 1:
            return candidates
        return []

    def _fuzzy_match_action_items(self, query: str, candidates) -> list[Any]:
        query_key = self._search_key(query)
        if not query_key:
            return []
        scored = []
        for item in candidates:
            labels = [self._search_key(item.title), self._search_key(self._time_budget_task_label(item))]
            score = max(self._similarity(query_key, label) for label in labels if label)
            if score >= 0.55:
                scored.append((score, item))
        scored.sort(key=lambda value: value[0], reverse=True)
        if not scored:
            return []
        best = scored[0][0]
        return [item for score, item in scored if best - score <= 0.08]

    def _search_key(self, value: str) -> str:
        text = re.sub(r"[（(].*?[）)]", "", str(value or ""))
        return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).lower()

    def _similarity(self, left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        if left in right or right in left:
            return max(len(left), len(right)) / max(len(left), len(right), 1)
        return SequenceMatcher(None, left, right).ratio()

    def _is_time_budget_task(self, item) -> bool:
        description = item.description or ""
        return bool(
            item.estimated_minutes
            and item.status not in {ItemStatus.canceled, ItemStatus.done}
            and (
                "累计" in item.title
                or "长期累计时间计划" in description
                or "总量：" in description
            )
        )

    def _build_time_budget_calendar_plan(self, task, args: dict[str, Any], *, capture_id: str) -> dict[str, Any]:
        total_minutes = int(task.estimated_minutes or 0)
        existing_events = self._find_time_budget_events(self._time_budget_task_label(task), [task])
        scheduled_minutes = sum(int((event.end_at - event.start_at).total_seconds() // 60) for event in existing_events)
        remaining_minutes = max(0, total_minutes - scheduled_minutes)
        session_minutes = self._bounded_int(args.get("session_minutes"), default=60, minimum=30, maximum=120)
        daily_minutes = self._bounded_int(args.get("daily_minutes"), default=120, minimum=30, maximum=360)
        min_session_minutes = self._bounded_int(
            args.get("min_session_minutes"),
            default=min(60, session_minutes),
            minimum=30,
            maximum=session_minutes,
        )
        buffer_minutes = self._bounded_int(args.get("buffer_minutes"), default=20, minimum=0, maximum=120)
        window_start = str(args.get("window_start") or "09:30")
        window_end = str(args.get("window_end") or "24:00")
        start_day = self._time_budget_start_day(task)
        due_at = task.due_at.astimezone(self.tz) if task.due_at else start_day + timedelta(days=14)
        now = datetime.now(self.tz)
        day = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        last_day = due_at.replace(hour=0, minute=0, second=0, microsecond=0)
        events: list[dict[str, Any]] = []
        remaining = remaining_minutes

        while day <= last_day and remaining > 0:
            day_window_start = self._datetime_on_day(day, window_start)
            day_window_end = self._datetime_on_day(day, window_end)
            if day_window_end > due_at:
                day_window_end = due_at
            if day_window_end <= now:
                day += timedelta(days=1)
                continue
            if day_window_start < now:
                day_window_start = self._round_up(now, minutes=30)
            day_quota = min(daily_minutes, remaining)
            busy_ranges = self._buffered_busy_ranges(
                self._busy_ranges(day_window_start, day_window_end),
                start=day_window_start,
                end=day_window_end,
                buffer_minutes=buffer_minutes,
            )
            free_ranges = self._free_ranges(day_window_start, day_window_end, busy_ranges)
            for free_range in free_ranges:
                cursor = free_range["start"]
                while cursor < free_range["end"] and day_quota > 0 and remaining > 0:
                    available = int((free_range["end"] - cursor).total_seconds() // 60)
                    if available < min_session_minutes:
                        break
                    slot_minutes = min(session_minutes, day_quota, remaining, available)
                    if slot_minutes < min_session_minutes:
                        break
                    end_at = cursor + timedelta(minutes=slot_minutes)
                    events.append(
                        {
                            "title": self._time_budget_task_label(task),
                            "description": f"长期学习安排拆分\n来源任务：{task.id}\n总目标：{task.title}\n来源捕获：{capture_id}",
                            "start_at": cursor.isoformat(),
                            "end_at": end_at.isoformat(),
                            "confidence": 0.9,
                        }
                    )
                    remaining -= slot_minutes
                    day_quota -= slot_minutes
                    cursor = end_at + timedelta(minutes=buffer_minutes)
            day += timedelta(days=1)

        planned_minutes = remaining_minutes - remaining
        return {
            "events": events,
            "planned_minutes": planned_minutes,
            "remaining_minutes": remaining,
            "already_scheduled_minutes": scheduled_minutes,
            "total_minutes": total_minutes,
            "daily_minutes": daily_minutes,
            "session_minutes": session_minutes,
            "min_session_minutes": min_session_minutes,
            "buffer_minutes": buffer_minutes,
            "window_start": window_start,
            "window_end": window_end,
        }

    def _bounded_int(self, value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return min(max(parsed, minimum), maximum)

    def _time_budget_start_day(self, task) -> datetime:
        description = task.description or ""
        match = re.search(r"开始[:：]\s*(\d{4}-\d{2}-\d{2})", description)
        if match:
            return datetime.fromisoformat(match.group(1)).replace(tzinfo=self.tz)
        return datetime.now(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)

    def _round_up(self, value: datetime, *, minutes: int) -> datetime:
        discard = timedelta(minutes=value.minute % minutes, seconds=value.second, microseconds=value.microsecond)
        rounded = value - discard
        if discard:
            rounded += timedelta(minutes=minutes)
        return rounded

    def _time_budget_task_label(self, task) -> str:
        return re.sub(r"[（(].*?[）)]", "", task.title).strip() or task.title

    def _time_budget_schedule_prompt(self, task, plan: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
        lines = [
            f"我把“{self._time_budget_task_label(task)}”拆成 {len(candidates)} 个日历候选，需要你确认：",
            f"计划写入：{self._format_minutes(plan['planned_minutes'])}",
            (
                "规则：避开已有日程和日程安排，优先填最早空闲时间；"
                f"每天最多 {self._format_minutes(plan['daily_minutes'])}，"
                f"每次默认 {self._format_minutes(plan['session_minutes'])}，"
                f"间隔 {self._format_minutes(plan['buffer_minutes'])}，"
                f"只排 {plan['window_start']}-{plan['window_end']}。"
            ),
        ]
        if plan["already_scheduled_minutes"]:
            lines.append(f"已在日历中：{self._format_minutes(plan['already_scheduled_minutes'])}")
        if plan["remaining_minutes"]:
            lines.append(f"仍未排入：{self._format_minutes(plan['remaining_minutes'])}")
        lines.append("回复“确认”后我再写入飞书日历。")
        return "\n".join(lines)

    def _find_time_budget_events(self, query: str, tasks) -> list[Any]:
        event_queries = [query] if query else []
        for task in tasks:
            base_title = re.sub(r"[（(].*?[）)]", "", task.title).strip()
            if base_title and base_title not in event_queries:
                event_queries.append(base_title)
        events = []
        seen: set[str] = set()
        for event_query in event_queries:
            if not event_query:
                continue
            for event in self.store.find_calendar_events(event_query):
                if event.id not in seen:
                    events.append(event)
                    seen.add(event.id)
        return sorted(events, key=lambda event: event.start_at)

    def _query_tokens(self, query: str) -> list[str]:
        tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", query)
        ignored = {
            "这个",
            "学习",
            "任务",
            "安排",
            "时间",
            "时间段",
            "日历",
            "看到",
            "怎么",
            "哪些",
            "哪里",
            "都在",
        }
        return [token for token in tokens if token not in ignored]

    def _format_time_budget_plan(self, query: str, tasks, events: list[Any]) -> str:
        if not tasks:
            target = f"“{query}”" if query else "这个长期学习任务"
            return f"我没有找到{target}对应的长期学习任务。"
        task = tasks[0]
        total = self._format_minutes(task.estimated_minutes)
        due = task.due_at.strftime("%Y-%m-%d %H:%M") if task.due_at else "未设截止"
        if not events:
            lines = [
                "这个长期学习任务目前只记录了总目标，还没有拆成具体日历时间段。",
                f"- 目标：{task.title}",
                f"- 总量：{total}",
                f"- 截止：{due}",
                "所以现在不能在日历中看到这些时间段。要在日历里看到，需要先把它拆成若干日程安排候选，确认后再写入日历。",
            ]
            return "\n".join(lines)

        scheduled_minutes = sum(int((event.end_at - event.start_at).total_seconds() // 60) for event in events)
        lines = [
            f"这个长期学习任务已经有 {len(events)} 个相关日程安排：",
            f"- 目标：{task.title}",
            f"- 总量：{total}",
            f"- 已排入日历：{self._format_minutes(scheduled_minutes)}",
        ]
        for event in events[:12]:
            lines.append(f"- {event.start_at.strftime('%Y-%m-%d %H:%M')}-{event.end_at.strftime('%H:%M')} {event.title}")
        return "\n".join(lines)

    def _format_minutes(self, minutes: int | None) -> str:
        if not minutes:
            return "未设置"
        hours, rest = divmod(int(minutes), 60)
        if hours and rest:
            return f"{hours}小时{rest}分钟"
        if hours:
            return f"{hours}小时"
        return f"{rest}分钟"

    def _query_range(self, label: str, offset_days: int) -> dict[str, Any]:
        start = effective_day_start(self.tz) + timedelta(days=offset_days)
        end = start + timedelta(days=1)
        tasks = [item.model_dump(mode="json") for item in self.store.list_action_items(start=start, end=end)]
        events = [event.model_dump(mode="json") for event in self.store.list_calendar_events(start=start, end=end)]
        blocks = self._schedule_block_occurrences_for_date(start)
        return {
            "tasks": tasks,
            "calendar_events": events,
            "schedule_blocks": blocks,
            "reply_text": self._format_query(label, tasks, events, blocks),
        }

    def _query_availability(self, args: dict[str, Any], *, sender_id: str | None) -> dict[str, Any]:
        day = self._target_day(str(args.get("day") or "tomorrow"))
        window_start = str(args.get("window_start") or "08:00")
        window_end = str(args.get("window_end") or "24:00")
        start = self._datetime_on_day(day, window_start)
        end = self._datetime_on_day(day, window_end)
        busy = self._busy_ranges(start, end)
        free = self._free_ranges(start, end, busy)
        pending = [item.model_dump(mode="json") for item in self.store.list_pending_confirmations(sender_id=sender_id, limit=5)]
        tasks = [
            item.model_dump(mode="json")
            for item in self.store.list_action_items(
                start=day.replace(hour=0, minute=0, second=0, microsecond=0),
                end=day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1),
            )
        ]
        reply = self._format_availability(
            label=self._day_label(day),
            window=(start, end),
            busy=busy,
            free=free,
            tasks=tasks,
            pending=pending,
            focus=str(args.get("focus") or "free"),
        )
        return {
            "date": day.date().isoformat(),
            "busy": [self._range_json(item) for item in busy],
            "free": [self._range_json(item) for item in free],
            "tasks": tasks,
            "pending_confirmations": pending,
            "reply_text": reply,
        }

    def _query_week(self) -> dict[str, Any]:
        now = datetime.now(self.tz)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        tasks = [item.model_dump(mode="json") for item in self.store.list_action_items(start=start, end=end)]
        events = [event.model_dump(mode="json") for event in self.store.list_calendar_events(start=start, end=end)]
        blocks = []
        for day in range(7):
            blocks.extend(self._schedule_block_occurrences_for_date(start + timedelta(days=day)))
        return {
            "tasks": tasks,
            "calendar_events": events,
            "schedule_blocks": blocks,
            "reply_text": self._format_query("未来 7 天", tasks, events, blocks),
        }

    def _target_day(self, day_name: str) -> datetime:
        today = effective_day_start(self.tz)
        normalized = day_name.lower()
        if normalized in {"today", "今天"}:
            return today
        if normalized in {"tomorrow", "明天"}:
            return today + timedelta(days=1)
        if normalized in {"after_tomorrow", "day_after_tomorrow", "后天"}:
            return today + timedelta(days=2)
        target_weekday = {
            "monday": 0,
            "周一": 0,
            "tuesday": 1,
            "周二": 1,
            "wednesday": 2,
            "周三": 2,
            "thursday": 3,
            "周四": 3,
            "friday": 4,
            "周五": 4,
            "saturday": 5,
            "周六": 5,
            "sunday": 6,
            "周日": 6,
            "周天": 6,
        }.get(normalized)
        if target_weekday is None:
            return today + timedelta(days=1)
        delta = (target_weekday - today.weekday()) % 7
        return today + timedelta(days=delta)

    def _datetime_on_day(self, day: datetime, time_value: str) -> datetime:
        if time_value == "24:00":
            return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        hour, minute = time_value.split(":", 1)
        return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)

    def _busy_ranges(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        busy: list[dict[str, Any]] = []
        for event in self.store.list_calendar_events(start=start - timedelta(days=1), end=end + timedelta(days=1)):
            if event.end_at > start and event.start_at < end:
                busy.append(
                    {
                        "start": max(event.start_at, start),
                        "end": min(event.end_at, end),
                        "title": event.title,
                        "source": "日程",
                    }
                )
        for block in self._schedule_block_occurrences_for_date(start):
            block_start = self._datetime_on_day(start, block["start_time"])
            block_end = self._datetime_on_day(start, block["end_time"])
            if block_end <= block_start:
                block_end += timedelta(days=1)
            if block_end > start and block_start < end:
                busy.append(
                    {
                        "start": max(block_start, start),
                        "end": min(block_end, end),
                        "title": block["title"],
                        "source": "日程安排",
                    }
                )
        return sorted(busy, key=lambda item: item["start"])

    def _buffered_busy_ranges(
        self,
        busy: list[dict[str, Any]],
        *,
        start: datetime,
        end: datetime,
        buffer_minutes: int,
    ) -> list[dict[str, Any]]:
        if buffer_minutes <= 0:
            return busy
        buffer = timedelta(minutes=buffer_minutes)
        return [
            {
                **item,
                "start": max(start, item["start"] - buffer),
                "end": min(end, item["end"] + buffer),
            }
            for item in busy
        ]

    def _free_ranges(self, start: datetime, end: datetime, busy: list[dict[str, Any]]) -> list[dict[str, Any]]:
        free: list[dict[str, Any]] = []
        cursor = start
        for item in self._merge_ranges(busy):
            if cursor < item["start"]:
                free.append({"start": cursor, "end": item["start"]})
            cursor = max(cursor, item["end"])
        if cursor < end:
            free.append({"start": cursor, "end": end})
        return free

    def _merge_ranges(self, ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for item in sorted(ranges, key=lambda value: value["start"]):
            if not merged or item["start"] > merged[-1]["end"]:
                merged.append(dict(item))
            else:
                merged[-1]["end"] = max(merged[-1]["end"], item["end"])
                merged[-1]["title"] = f"{merged[-1]['title']}、{item['title']}"
                merged[-1]["source"] = f"{merged[-1]['source']}、{item['source']}"
        return merged

    def _range_json(self, item: dict[str, Any]) -> dict[str, Any]:
        return {**item, "start": item["start"].isoformat(), "end": item["end"].isoformat()}

    def _day_label(self, day: datetime) -> str:
        today = effective_day_start(self.tz)
        if day.date() == today.date():
            return "今天"
        if day.date() == (today + timedelta(days=1)).date():
            return "明天"
        names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{names[day.weekday()]}({day.date().isoformat()})"

    def _format_availability(
        self,
        *,
        label: str,
        window: tuple[datetime, datetime],
        busy: list[dict[str, Any]],
        free: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        pending: list[dict[str, Any]],
        focus: str,
    ) -> str:
        lines = [f"{label}按 {window[0].strftime('%H:%M')}-{window[1].strftime('%H:%M')} 计算："]
        if busy:
            lines.append("已占用：")
            for item in busy:
                lines.append(f"- {item['start'].strftime('%H:%M')}-{item['end'].strftime('%H:%M')} {item['title']}（{item['source']}）")
        else:
            lines.append("已占用：无")
        if focus != "busy":
            if free:
                lines.append("空闲时间：")
                for item in free:
                    lines.append(f"- {item['start'].strftime('%H:%M')}-{item['end'].strftime('%H:%M')}")
            else:
                lines.append("空闲时间：这个窗口内没有明显空档。")
        if tasks:
            lines.append("任务截止不算硬占用，但需要留意：")
            for task in tasks[:5]:
                lines.append(f"- {task['title']} {task.get('due_at') or '未设截止'}")
        if pending:
            lines.append(f"另有 {len(pending)} 个待确认项未处理。")
        if focus == "can_schedule":
            lines.append("如果要安排新事项，建议选上面的空闲时间段。")
        return "\n".join(lines)

    def _check_conflicts(self, args: dict[str, Any]) -> dict[str, Any]:
        start = datetime.fromisoformat(str(args["start_at"]))
        end = datetime.fromisoformat(str(args["end_at"]))
        conflicts = self._conflicts_for_range(start, end)
        return {"conflicts": conflicts, "reply_text": f"发现 {len(conflicts)} 个冲突。"}

    def _conflicts_for_range(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        events = self.store.list_calendar_events(start=start - timedelta(days=1), end=end + timedelta(days=1))
        conflicts = [
            {"type": "calendar_event", **event.model_dump(mode="json")}
            for event in events
            if event.start_at < end and event.end_at > start
        ]
        for block in self.store.list_schedule_blocks():
            if self._schedule_block_overlaps(block.model_dump(mode="json"), start, end):
                conflicts.append({"type": "schedule_block", **block.model_dump(mode="json")})
        return conflicts

    def _schedule_block_overlaps(self, block: dict[str, Any], start: datetime, end: datetime) -> bool:
        if not self._schedule_block_matches_date(block, start):
            return False
        block_start = self._time_minutes(str(block["start_time"]))
        block_end = self._time_minutes(str(block["end_time"]))
        start_minutes = start.hour * 60 + start.minute
        end_minutes = end.hour * 60 + end.minute
        if block_end <= block_start:
            return start_minutes >= block_start or end_minutes <= block_end
        return start_minutes < block_end and end_minutes > block_start

    def _schedule_block_matches_date(self, block: dict[str, Any], day: datetime) -> bool:
        byday = self._rrule_days(str(block.get("recurrence_rule") or ""))
        day_code = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][day.weekday()]
        return not byday or day_code in byday

    def _schedule_block_occurrences_for_date(self, day: datetime) -> list[dict[str, Any]]:
        occurrences = []
        for block in self.store.list_schedule_blocks():
            data = block.model_dump(mode="json")
            if self._schedule_block_matches_date(data, day):
                occurrences.append(
                    {
                        **data,
                        "date": day.date().isoformat(),
                        "display_time": f"{data['start_time']}-{data['end_time']}",
                    }
                )
        return sorted(occurrences, key=lambda item: item["start_time"])

    def _rrule_days(self, rrule: str) -> set[str]:
        for part in rrule.split(";"):
            if part.startswith("BYDAY="):
                return {item.strip() for item in part.removeprefix("BYDAY=").split(",") if item.strip()}
        return set()

    def _time_minutes(self, value: str) -> int:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)

    def _store_feishu_event_id(self, event_id: str, sync_result: dict[str, Any]) -> None:
        external_id = sync_result.get("event_id")
        response = sync_result.get("response")
        if not external_id and isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict):
                event = data.get("event")
                if isinstance(event, dict):
                    external_id = event.get("event_id")
                external_id = external_id or data.get("event_id")
        if external_id:
            self.store.update_calendar_event(event_id, {"feishu_event_id": str(external_id)})

    def _store_feishu_schedule_event_id(self, block_id: str, sync_result: dict[str, Any]) -> None:
        external_id = sync_result.get("event_id")
        response = sync_result.get("response")
        if not external_id and isinstance(response, dict):
            data = response.get("data")
            if isinstance(data, dict):
                event = data.get("event")
                if isinstance(event, dict):
                    external_id = event.get("event_id")
                external_id = external_id or data.get("event_id")
        if external_id:
            self.store.update_schedule_block(block_id, {"feishu_event_id": str(external_id)})

    async def _sync_feishu_task(self, args: dict[str, Any]) -> dict[str, Any]:
        item = self.store.get_action_item(str(args["action_item_id"]))
        return {"sync": await self.feishu.sync_task(item.model_dump(mode="json")), "reply_text": "已同步到飞书任务。"}

    async def _sync_feishu_calendar(self, args: dict[str, Any]) -> dict[str, Any]:
        event = self.store.get_calendar_event(str(args["calendar_event_id"]))
        event_json = event.model_dump(mode="json")
        if event.feishu_event_id:
            sync_result = await self.feishu.update_calendar_event(event_json)
        else:
            sync_result = await self.feishu.sync_calendar_event(event_json)
            self._store_feishu_event_id(event.id, sync_result)
        return {
            "sync": sync_result,
            "calendar_event": self.store.get_calendar_event(event.id).model_dump(mode="json"),
            "reply_text": "已同步到飞书日历。",
        }

    def _confirmation_type(self, calls: list[AgentToolCall]) -> str:
        if any(call.tool_name == "confirm_plan_schedule" for call in calls):
            for call in calls:
                if call.tool_name == "confirm_plan_schedule" and call.arguments.get("kind") == "course_timetable":
                    return "course_timetable_schedule"
            return "plan_schedule"
        if any(call.tool_name == "schedule_habit_plan" for call in calls):
            return "habit_schedule"
        if any(call.tool_name == "create_schedule_block_candidates" for call in calls):
            return "schedule_blocks"
        if any(call.tool_name == "update_calendar_event" for call in calls):
            return "update"
        if any(call.tool_name == "update_schedule_block" for call in calls):
            return "update"
        if any(call.tool_name == "disable_schedule_block_reminders" for call in calls):
            return "update"
        return "create_candidates"

    def _confirmation_prompt(self, candidates: list[dict[str, Any]]) -> str:
        item_count = len(candidates)
        if item_count == 1 and candidates[0]["type"] == "日程安排":
            detail_count = len(candidates[0].get("details", []))
            lines = [f"我识别到 {detail_count} 个日程安排，需要你确认："]
        else:
            lines = [f"我识别到 {item_count} 个候选，需要你确认："]
        lines.append("回复“确认”后我再创建或修改。")
        return "\n".join(lines)

    def _attach_conflicts(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for candidate in candidates:
            item = {**candidate}
            args = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            if item.get("type") in {"日程", "日程修改"} and args.get("start_at") and args.get("end_at"):
                start = datetime.fromisoformat(str(args["start_at"]))
                end = datetime.fromisoformat(str(args["end_at"]))
                item["conflicts"] = self._conflicts_for_range(start, end)
            out.append(item)
        return out

    def _candidate_summary(self, call: AgentToolCall) -> dict[str, Any]:
        args = call.arguments
        if call.tool_name == "create_schedule_block_candidates":
            blocks = args.get("blocks", [])
            details = [
                f"{block.get('title') or '日程安排'}：{block.get('start_time')}-{block.get('end_time')}，{block.get('recurrence_rule')}"
                for block in blocks
            ]
            return {
                "type": "日程安排",
                "title": f"{len(blocks)} 个日程安排",
                "details": details,
                "arguments": args,
            }
        if call.tool_name == "schedule_habit_plan":
            events = list(args.get("planned_events") or [])
            details = [
                f"方式：{args.get('method') or '未设置'}",
                f"频率：{args.get('frequency') or '未设置'}",
                f"每次：{self._format_minutes(args.get('session_minutes'))}",
                f"时段：{self._habit_window_text(args)}",
                f"周期：{args.get('duration_days') or '未设置'} 天",
            ]
            for event in events[:20]:
                details.append(f"{self._format_event_candidate_time(event)} {event.get('title')}")
            if len(events) > 20:
                details.append(f"另有 {len(events) - 20} 个日程候选")
            return {
                "type": "养成日程",
                "title": f"{args.get('title') or '习惯养成'}：{len(events)} 个日历候选",
                "details": details,
                "arguments": args,
            }
        if call.tool_name == "confirm_plan_schedule":
            events = list(args.get("planned_events") or [])
            details = [f"{self._format_event_candidate_time(event)} {event.get('title')}" for event in events[:20]]
            if len(events) > 20:
                details.append(f"另有 {len(events) - 20} 个日历候选")
            kind = str(args.get("kind") or "")
            title = "课程表导入" if kind == "course_timetable" else "长期日程草案"
            return {
                "type": "计划日程",
                "title": f"{title}：{len(events)} 个日历候选",
                "details": details,
                "arguments": args,
            }
        title = args.get("title") or args.get("query") or call.tool_name
        details = []
        if call.tool_name == "create_calendar_event_candidate" and args.get("start_at") and args.get("end_at"):
            title = f"{self._format_event_candidate_time(args)} {title}"
            details.append(f"时长：{self._format_event_candidate_duration(args)}")
        kind = {
            "create_task_candidate": "任务",
            "create_calendar_event_candidate": "日程",
            "update_calendar_event": "日程修改",
            "update_schedule_block": "日程安排修改",
            "disable_schedule_block_reminders": "关闭固定安排提醒",
            "update_task": "任务修改",
            "cancel_task": "取消任务",
            "cancel_calendar_event": "取消日程",
            "cancel_schedule_block": "取消日程安排",
        }.get(call.tool_name, call.tool_name)
        return {"type": kind, "title": str(title), "details": details, "arguments": args}

    def _format_event_candidate_time(self, args: dict[str, Any]) -> str:
        try:
            start = datetime.fromisoformat(str(args["start_at"]))
            end = datetime.fromisoformat(str(args["end_at"]))
        except (KeyError, ValueError):
            return f"{args.get('start_at')} - {args.get('end_at')}"
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        date_text = f"{start.month}月{start.day}日 {weekdays[start.weekday()]}"
        if start.date() == end.date():
            return f"{date_text} {start.strftime('%H:%M')}-{end.strftime('%H:%M')}"
        end_date_text = f"{end.month}月{end.day}日 {weekdays[end.weekday()]}"
        return f"{date_text} {start.strftime('%H:%M')} - {end_date_text} {end.strftime('%H:%M')}"

    def _format_event_candidate_duration(self, args: dict[str, Any]) -> str:
        try:
            start = datetime.fromisoformat(str(args["start_at"]))
            end = datetime.fromisoformat(str(args["end_at"]))
        except (KeyError, ValueError):
            return "未知"
        return self._format_minutes(int((end - start).total_seconds() // 60))

    def _format_query(
        self,
        label: str,
        tasks: list[dict[str, Any]],
        events: list[dict[str, Any]],
        schedule_blocks: list[dict[str, Any]],
    ) -> str:
        if not tasks and not events and not schedule_blocks:
            return f"{label}没有待办任务或日程安排。"
        schedule_count = len(events) + len(schedule_blocks)
        lines = [f"{label}共有 {len(tasks)} 个任务、{schedule_count} 个日程安排："]
        for event in events[:8]:
            lines.append(f"- 日程安排：{event['title']} {event['start_at']} - {event['end_at']}")
        for block in schedule_blocks[:12]:
            date = f"{block.get('date')} " if block.get("date") else ""
            lines.append(f"- 日程安排：{date}{block['title']} {block['start_time']}-{block['end_time']}")
        for task in tasks[:8]:
            due = task.get("due_at") or "未设截止"
            lines.append(f"- 任务：{task['title']} {due}")
        return "\n".join(lines)

    def _format_schedule_blocks(self, blocks: list[dict[str, Any]]) -> str:
        if not blocks:
            return "还没有重复日程安排。"
        return "\n".join(f"- {block['title']} {block['recurrence_rule']} {block['start_time']}-{block['end_time']}" for block in blocks)

    def _format_pending_confirmations(self, confirmations: list[dict[str, Any]]) -> str:
        if not confirmations:
            return "当前没有待确认项。"
        lines = [f"当前有 {len(confirmations)} 个待确认项："]
        for item in confirmations:
            lines.append(f"- {item['id']}：{item['confirmation_type']}，过期时间 {item.get('expires_at')}")
        return "\n".join(lines)

    def _format_ambiguous_task(self, matches, action: str) -> str:
        if not matches:
            return f"没有找到要{action}的任务。"
        lines = [f"找到 {len(matches)} 个可能的任务，请说清楚要{action}哪一个："]
        for item in matches[:5]:
            due = item.due_at.isoformat() if item.due_at else "未设截止"
            lines.append(f"- {item.title}（{due}）")
        return "\n".join(lines)

    def _format_created(self, created: list[dict[str, Any]]) -> str:
        if not created:
            return "没有创建新的事项。"
        lines = [f"已确认并创建 {len(created)} 项："]
        for item in created:
            lines.append(f"- {self._created_type_label(item)}：{self._created_summary(item)}")
        return "\n".join(lines)

    def _created_type_label(self, item: dict[str, Any]) -> str:
        return {
            "action_item": "任务",
            "calendar_event": "日程",
            "schedule_block": "日程安排",
            "schedule_block_update": "日程安排修改",
            "schedule_block_reminder_update": "固定安排提醒修改",
            "calendar_event_update": "日程修改",
            "action_item_update": "任务修改",
            "action_item_complete": "完成任务",
            "action_item_cancel": "取消任务",
            "calendar_event_cancel": "取消日程",
            "schedule_block_cancel": "取消日程安排",
        }.get(str(item.get("type")), str(item.get("type") or "事项"))

    def _created_summary(self, item: dict[str, Any]) -> str:
        if item.get("type") == "schedule_block":
            return f"{item['title']} {item.get('start_time')}-{item.get('end_time')} {item.get('recurrence_rule')}"
        if item.get("type") == "schedule_block_update":
            return f"{item['title']} {item.get('start_time')}-{item.get('end_time')} {item.get('recurrence_rule')}"
        if item.get("type") == "schedule_block_reminder_update":
            return f"{item['title']} 已关闭提醒"
        if item.get("type") in {"calendar_event", "calendar_event_update"}:
            return f"{item['title']} {item.get('start_at')} - {item.get('end_at')}"
        if item.get("type") in {"action_item", "action_item_update", "action_item_complete", "action_item_cancel"}:
            due = item.get("due_at") or "未设截止"
            return f"{item['title']} {due}"
        return str(item.get("title") or item)
