from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.models import AgentToolCall, AgentToolName, AgentToolResult
from app.database import Repository
from app.models import (
    ActionCreate,
    ActionIntent,
    ActionRecord,
    ActionStatus,
    ActionUpdate,
    Domain,
    Energy,
    Priority,
    SyncEvent,
    SyncTarget,
)
from app.services.review_service import ReviewService
from app.services.sync_service import SyncService


class AgentToolExecutor:
    def __init__(
        self,
        repo: Repository,
        sync: SyncService,
        review_service: ReviewService,
        tz: ZoneInfo,
    ):
        self.repo = repo
        self.sync = sync
        self.review_service = review_service
        self.tz = tz

    async def execute_all(
        self,
        calls: list[AgentToolCall],
        *,
        capture_id: str | None,
    ) -> list[AgentToolResult]:
        created_action_ids: list[str] = []
        results: list[AgentToolResult] = []
        for call in calls:
            result = await self.execute(call, capture_id=capture_id, created_action_ids=created_action_ids)
            action_id = result.result.get("action_id")
            action_ids = result.result.get("action_ids")
            if action_id:
                created_action_ids.append(action_id)
            if isinstance(action_ids, list):
                created_action_ids.extend(action_ids)
            results.append(result)
        return results

    async def execute(
        self,
        call: AgentToolCall,
        *,
        capture_id: str | None,
        created_action_ids: list[str],
    ) -> AgentToolResult:
        try:
            if call.name == AgentToolName.create_task:
                return self._create_task(call.arguments, capture_id)
            if call.name == AgentToolName.query_today:
                return self._query_section(call.name, "today", "今天没有到期任务。")
            if call.name == AgentToolName.query_tomorrow:
                return self._query_tomorrow()
            if call.name == AgentToolName.query_overdue:
                return self._query_section(call.name, "overdue", "目前没有逾期任务。")
            if call.name == AgentToolName.query_next_7_days:
                return self._query_section(call.name, "next_7_days", "未来 7 天没有已安排任务。")
            if call.name == AgentToolName.update_task_status:
                return self._update_task_status(call.arguments)
            if call.name == AgentToolName.update_task_time:
                return self._update_task_time(call.arguments)
            if call.name == AgentToolName.ask_confirmation:
                return self._ask_confirmation(call.arguments)
            if call.name == AgentToolName.sync_bitable:
                return await self._sync_bitable(call.arguments, capture_id, created_action_ids)
            if call.name == AgentToolName.sync_feishu_task:
                return await self._sync_feishu_native(
                    call.name,
                    SyncTarget.task,
                    call.arguments,
                    created_action_ids,
                )
            if call.name == AgentToolName.sync_feishu_calendar:
                return await self._sync_feishu_native(
                    call.name,
                    SyncTarget.calendar,
                    call.arguments,
                    created_action_ids,
                )
            if call.name == AgentToolName.send_feishu_reply:
                text = str(call.arguments.get("text") or "")
                return AgentToolResult(name=call.name, ok=True, reply_text=text, result={"text": text})
            return AgentToolResult(name=call.name, ok=False, error=f"unsupported tool: {call.name}")
        except _ConfirmationRequired as exc:
            return exc.result
        except Exception as exc:  # noqa: BLE001 - tool failures are persisted in agent_runs
            return AgentToolResult(name=call.name, ok=False, error=str(exc))

    def _create_task(self, args: dict[str, Any], capture_id: str | None) -> AgentToolResult:
        title = str(args.get("title") or "").strip()
        if not title:
            return AgentToolResult(name=AgentToolName.create_task, ok=False, error="title is required")
        action = self.repo.create_action(
            ActionCreate(
                capture_id=args.get("capture_id") or capture_id,
                title=title,
                description=args.get("description"),
                intent=self._enum(ActionIntent, args.get("intent"), ActionIntent.task),
                domain=self._enum(Domain, args.get("domain"), Domain.other),
                status=self._enum(ActionStatus, args.get("status"), ActionStatus.inbox),
                priority=self._enum(Priority, args.get("priority"), Priority.p3),
                energy=self._enum(Energy, args.get("energy"), Energy.medium),
                due_at=self._parse_dt(args.get("due_at")),
                start_at=self._parse_dt(args.get("start_at")),
                remind_at=self._parse_dt(args.get("remind_at")),
                estimated_minutes=args.get("estimated_minutes"),
                people=self._str_list(args.get("people")),
                projects=self._str_list(args.get("projects")),
                labels=self._str_list(args.get("labels")),
                evidence_text=args.get("evidence_text"),
                confidence=float(args.get("confidence", 0.7)),
                metadata={"agent_created": True, **self._dict(args.get("metadata"))},
            )
        )
        return AgentToolResult(
            name=AgentToolName.create_task,
            ok=True,
            result={"action_id": action.id, "title": action.title},
        )

    def _query_section(self, tool_name: AgentToolName, section: str, empty_text: str) -> AgentToolResult:
        review = self.review_service.daily()
        actions = review.sections.get(section, [])
        reply = self._format_actions(actions, empty_text)
        return AgentToolResult(
            name=tool_name,
            ok=True,
            result={"count": len(actions), "actions": [a.model_dump(mode="json") for a in actions]},
            reply_text=reply,
        )

    def _query_tomorrow(self) -> AgentToolResult:
        target_date = (datetime.now(self.tz) + timedelta(days=1)).date()
        actions = self._actions_on_date(target_date)
        return AgentToolResult(
            name=AgentToolName.query_tomorrow,
            ok=True,
            result={"count": len(actions), "actions": [a.model_dump(mode="json") for a in actions]},
            reply_text=self._format_actions(actions, "明天没有已安排任务。"),
        )

    def _update_task_status(self, args: dict[str, Any]) -> AgentToolResult:
        action, confirmation = self._resolve_single_action(args)
        if confirmation:
            return confirmation
        status = self._enum(ActionStatus, args.get("status"), ActionStatus.done)
        updated = self.repo.update_action(action.id, ActionUpdate(status=status))
        return AgentToolResult(
            name=AgentToolName.update_task_status,
            ok=True,
            result={"action_id": updated.id, "status": updated.status.value},
            reply_text=f"已更新任务状态：{updated.title} -> {updated.status.value}",
        )

    def _update_task_time(self, args: dict[str, Any]) -> AgentToolResult:
        action, confirmation = self._resolve_single_action(args)
        if confirmation:
            return confirmation
        due_at = self._parse_dt(args.get("due_at"))
        start_at = self._parse_dt(args.get("start_at"))
        remind_at = self._parse_dt(args.get("remind_at"))
        if due_at is None and start_at is None and remind_at is None:
            return AgentToolResult(
                name=AgentToolName.update_task_time,
                ok=False,
                error="due_at, start_at or remind_at is required",
            )
        patch_data: dict[str, Any] = {}
        if due_at is not None:
            patch_data["due_at"] = due_at
        if start_at is not None:
            patch_data["start_at"] = start_at
        if remind_at is not None:
            patch_data["remind_at"] = remind_at
            patch_data["metadata"] = {
                **action.metadata,
                "reminder_sent_at": None,
                "reminder_error": None,
                "reminder_error_at": None,
            }
        updated = self.repo.update_action(action.id, ActionUpdate(**patch_data))
        changes: list[str] = []
        if start_at is not None:
            changes.append(f"开始时间设为 {self._display_dt(updated.start_at)}")
        if due_at is not None:
            changes.append(f"截止/结束时间设为 {self._display_dt(updated.due_at)}")
        if remind_at is not None:
            changes.append(f"提醒时间设为 {self._display_dt(updated.remind_at)}")
        return AgentToolResult(
            name=AgentToolName.update_task_time,
            ok=True,
            result={
                "action_id": updated.id,
                "due_at": updated.due_at.isoformat() if updated.due_at else None,
                "start_at": updated.start_at.isoformat() if updated.start_at else None,
                "remind_at": updated.remind_at.isoformat() if updated.remind_at else None,
            },
            reply_text=f"已把「{updated.title}」" + "，".join(changes) + "。",
        )

    def _ask_confirmation(self, args: dict[str, Any]) -> AgentToolResult:
        text = str(args.get("text") or args.get("prompt") or "这一步需要你确认后我再执行。")
        return AgentToolResult(
            name=AgentToolName.ask_confirmation,
            ok=True,
            reply_text=text,
            result={"prompt": text, "candidates": args.get("candidates", [])},
            needs_confirmation=True,
        )

    async def _sync_bitable(
        self,
        args: dict[str, Any],
        capture_id: str | None,
        created_action_ids: list[str],
    ) -> AgentToolResult:
        events: list[dict[str, Any]] = []
        entity_type = str(args.get("entity_type") or "actions")
        if entity_type in {"capture", "all"} and capture_id:
            capture = self.repo.get_capture(capture_id)
            events.extend(event.model_dump(mode="json") for event in await self.sync.sync_capture(capture))
        action_ids = args.get("action_ids")
        if not isinstance(action_ids, list):
            action_ids = created_action_ids
        if entity_type in {"action", "actions", "all"}:
            for action_id in action_ids:
                action = self.repo.get_action(str(action_id))
                events.extend(event.model_dump(mode="json") for event in await self.sync.sync_action(action))
        return AgentToolResult(
            name=AgentToolName.sync_bitable,
            ok=True,
            result={"events": events, "count": len(events)},
        )

    async def _sync_feishu_native(
        self,
        tool_name: AgentToolName,
        target: SyncTarget,
        args: dict[str, Any],
        created_action_ids: list[str],
    ) -> AgentToolResult:
        action_ids = args.get("action_ids")
        if not isinstance(action_ids, list) or not action_ids:
            action_id = args.get("action_id")
            action_ids = [action_id] if action_id else created_action_ids
        events: list[SyncEvent] = []
        for action_id in action_ids:
            if not action_id:
                continue
            action = self.repo.get_action(str(action_id))
            events.append(await self.sync.sync_action_target(action, target))
        ok_count = sum(1 for event in events if event.status in {"success", "dry_run"})
        errors = [event.error for event in events if event.error]
        target_name = "飞书任务" if target == SyncTarget.task else "飞书日历"
        if errors:
            reply = f"{target_name}同步遇到问题：" + "；".join(errors[:3])
        elif events:
            reply = f"已同步 {ok_count} 个事项到{target_name}。"
        else:
            reply = f"没有可同步到{target_name}的事项。"
        return AgentToolResult(
            name=tool_name,
            ok=not errors,
            result={"events": [event.model_dump(mode="json") for event in events], "count": len(events)},
            reply_text=reply,
            error="; ".join(errors) if errors else None,
        )

    def _resolve_single_action(
        self, args: dict[str, Any]
    ) -> tuple[ActionRecord, AgentToolResult | None]:
        action_id = args.get("action_id")
        if action_id:
            return self.repo.get_action(str(action_id)), None
        query = str(args.get("query") or args.get("title") or "").strip()
        matches = self._find_matches(query)
        if len(matches) == 1:
            return matches[0], None
        if not matches:
            result = AgentToolResult(
                name=AgentToolName.ask_confirmation,
                ok=True,
                reply_text="我没有找到明确匹配的任务，请告诉我要修改哪一条。",
                result={"query": query, "candidates": []},
                needs_confirmation=True,
            )
            raise _ConfirmationRequired(result)
        candidates = [
            {"id": action.id, "title": action.title, "due_at": action.due_at.isoformat() if action.due_at else None}
            for action in matches[:5]
        ]
        lines = ["我找到多条可能的任务，请回复要修改哪一条："]
        for index, action in enumerate(matches[:5], start=1):
            lines.append(f"{index}. {action.title}（{self._display_dt(action.due_at)}，ID: {action.id}）")
        result = AgentToolResult(
            name=AgentToolName.ask_confirmation,
            ok=True,
            reply_text="\n".join(lines),
            result={"query": query, "candidates": candidates},
            needs_confirmation=True,
        )
        raise _ConfirmationRequired(result)

    def _find_matches(self, query: str) -> list[ActionRecord]:
        if not query:
            return []
        normalized_query = query.replace(" ", "").lower()
        actions = self.repo.list_actions(limit=100)
        scored: list[tuple[int, ActionRecord]] = []
        for action in actions:
            normalized_title = action.title.replace(" ", "").lower()
            score = 0
            if normalized_query in normalized_title:
                score += 10
            if normalized_title in normalized_query:
                score += 8
            for char in normalized_query:
                if char in normalized_title:
                    score += 1
            if score >= max(3, len(normalized_query) // 2):
                scored.append((score, action))
        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) > 1 and scored[0][0] >= scored[1][0] + 5:
            return [scored[0][1]]
        return [item[1] for item in scored[:5]]

    def _format_actions(self, actions: list[ActionRecord], empty_text: str) -> str:
        if not actions:
            return empty_text
        lines = [f"共 {len(actions)} 个任务："]
        for index, action in enumerate(actions[:12], start=1):
            time_text = self._display_action_time(action)
            lines.append(f"{index}. [{action.priority.value}] {action.title}（{time_text}）")
        if len(actions) > 12:
            lines.append(f"还有 {len(actions) - 12} 个未显示。")
        return "\n".join(lines)

    def _actions_on_date(self, target_date: date) -> list[ActionRecord]:
        actions = self.repo.list_actions(
            statuses=[
                ActionStatus.inbox,
                ActionStatus.planned,
                ActionStatus.doing,
                ActionStatus.waiting,
                ActionStatus.snoozed,
            ],
            limit=500,
        )
        matched: list[ActionRecord] = []
        for action in actions:
            candidates = [action.start_at, action.due_at, action.remind_at]
            if any(value and value.astimezone(self.tz).date() == target_date for value in candidates):
                matched.append(action)
        return sorted(
            matched,
            key=lambda action: (
                action.start_at or action.due_at or action.remind_at or datetime.max.replace(tzinfo=self.tz)
            ),
        )

    def _display_action_time(self, action: ActionRecord) -> str:
        if action.start_at and action.due_at:
            start = action.start_at.astimezone(self.tz)
            end = action.due_at.astimezone(self.tz)
            if start.date() == end.date():
                return f"{self._display_dt(action.start_at)}-{end:%H:%M}"
            return f"{self._display_dt(action.start_at)} 到 {self._display_dt(action.due_at)}"
        if action.due_at:
            return self._display_dt(action.due_at)
        if action.start_at:
            return self._display_dt(action.start_at)
        if action.remind_at:
            return f"提醒 {self._display_dt(action.remind_at)}"
        return "未设时间"

    def _parse_dt(self, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.tz)
        return parsed

    def _display_dt(self, value: datetime | None) -> str:
        if value is None:
            return "未设时间"
        local = value.astimezone(self.tz)
        now = datetime.now(self.tz)
        if local.date() == now.date():
            return f"今天 {local:%H:%M}"
        if local.date() == (now + timedelta(days=1)).date():
            return f"明天 {local:%H:%M}"
        return local.strftime("%m-%d %H:%M")

    def _enum(self, enum_cls, value: Any, default):
        if value in (None, ""):
            return default
        try:
            return enum_cls(value)
        except ValueError:
            return default

    def _str_list(self, value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return [str(value)]

    def _dict(self, value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}


class _ConfirmationRequired(Exception):
    def __init__(self, result: AgentToolResult):
        super().__init__("confirmation required")
        self.result = result
