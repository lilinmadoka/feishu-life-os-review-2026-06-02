from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app.adapters.feishu_client import FeishuClient
from app.adapters.pushover_client import PushoverClient, pushover_tag_for_target
from app.config import get_settings
from app.core.observability import NullTraceEmitter, TraceEmitter
from app.core.store import StateStore
from app.database import Repository, new_id, utcnow_iso
from app.models import ActionRecord, ActionStatus, ActionUpdate

ACTIVE_STATUSES = [
    ActionStatus.inbox,
    ActionStatus.planned,
    ActionStatus.doing,
    ActionStatus.waiting,
    ActionStatus.snoozed,
]

PRE_STRONG_REMINDER_LEAD_TIME = timedelta(minutes=3)
SCHEDULE_STRONG_REMINDER_GRACE = timedelta(minutes=10)
DAILY_REVIEW_SEND_GRACE = timedelta(minutes=15)
DAILY_REVIEW_TARGET_TYPE = "daily_review"
DAILY_REVIEW_SENT_CHANNEL = "summary_card_sent"
DAILY_REVIEW_CONFIRMED_CHANNEL = "summary_confirmed"
DAILY_REVIEW_FOLLOWUP_CHANNEL_PREFIX = "strong_followup"


class ReminderFeishuClient(Protocol):
    async def send_app_text(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        ...

    async def send_interactive_card(
        self,
        receive_id: str,
        card: dict[str, Any],
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        ...

    async def create_video_meeting_reminder(
        self,
        receive_id: str,
        topic: str,
        *,
        end_at: datetime | None = None,
    ) -> dict[str, Any]:
        ...


class StrongPushClient(Protocol):
    @property
    def configured(self) -> bool:
        ...

    async def send_emergency(
        self,
        title: str,
        message: str,
        *,
        url: str | None = None,
        url_title: str | None = None,
        tags: str | None = None,
    ) -> dict[str, Any]:
        ...


class ReminderWorker:
    def __init__(
        self,
        repo: Repository,
        feishu: ReminderFeishuClient,
        tz: ZoneInfo,
        *,
        fallback_open_id: str | None = None,
        push: StrongPushClient | None = None,
        poll_seconds: float = 60,
        now_provider: Callable[[], datetime] | None = None,
        trace_emitter: TraceEmitter | None = None,
    ):
        self.repo = repo
        self.feishu = feishu
        self.tz = tz
        self.fallback_open_id = fallback_open_id
        self.push = push
        self.poll_seconds = poll_seconds
        self.now_provider = now_provider or (lambda: datetime.now(self.tz))
        self.trace = trace_emitter or NullTraceEmitter()
        StateStore(self.repo).migrate()

    async def run_once(self) -> int:
        trace = self.trace.start_trace(
            workflow_type="reminder_worker_run_once",
            attrs={"poll_seconds": self.poll_seconds, "fallback_open_id": self.fallback_open_id},
        )
        trace_id = trace.trace_id
        try:
            now = self.now_provider()
            if now.tzinfo is None:
                now = now.replace(tzinfo=self.tz)
            sent_count = 0
            with self.trace.span(trace_id, "worker.daily_review", component="worker", lane="external") as span:
                daily_count = await self._run_daily_review(now)
                sent_count += daily_count
                span.add_attrs({"sent_count": daily_count})
            with self.trace.span(trace_id, "worker.legacy_action_reminders", component="worker", lane="external") as span:
                legacy_count = 0
                actions = self.repo.list_actions(statuses=ACTIVE_STATUSES, limit=500)
                for action in actions:
                    if self._should_send_pre_strong_card(action, now):
                        open_id = self._open_id_for_action(action)
                        if not open_id:
                            self._mark_pre_strong_card_error(action, "missing open_id")
                            continue
                        try:
                            response = await self.feishu.send_interactive_card(
                                open_id,
                                self._render_pre_strong_card(action),
                            )
                        except Exception as exc:  # noqa: BLE001 - reminder failures are stored per action
                            self._mark_pre_strong_card_error(action, str(exc) or repr(exc))
                            continue
                        self._mark_pre_strong_card_sent(action, response)
                        sent_count += 1
                        legacy_count += 1
                        continue
                    if not self._should_send(action, now):
                        continue
                    open_id = self._open_id_for_action(action)
                    if not open_id:
                        self._mark_error(action, "missing open_id")
                        continue
                    try:
                        response = await self._send_strong_reminder(
                            open_id,
                            action.title,
                            action.remind_at,
                            fallback_text=self._render_message(action),
                            target_type="legacy_action",
                            target_id=action.id,
                        )
                    except Exception as exc:  # noqa: BLE001 - reminder failures are stored per action
                        self._mark_error(action, str(exc) or repr(exc))
                        continue
                    self._mark_sent(action, response)
                    sent_count += 1
                    legacy_count += 1
                span.add_attrs({"due_count": len(actions), "sent_count": legacy_count})
            with self.trace.span(trace_id, "worker.core_action_item_reminders", component="worker", lane="external") as span:
                core_action_count = await self._run_core_action_item_reminders(now)
                sent_count += core_action_count
                span.add_attrs({"sent_count": core_action_count})
            with self.trace.span(trace_id, "worker.core_schedule_reminders", component="worker", lane="external") as span:
                core_schedule_count = await self._run_core_schedule_reminders(now)
                sent_count += core_schedule_count
                span.add_attrs({"sent_count": core_schedule_count})
            self.trace.end_trace(trace_id, status="ok", summary="reminder worker run complete", attrs={"sent_count": sent_count})
            return sent_count
        except Exception as exc:
            self.trace.end_trace(trace_id, status="failed", summary=str(exc))
            raise

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.poll_seconds)

    async def _run_daily_review(self, now: datetime) -> int:
        open_id = self.fallback_open_id
        if not open_id:
            return 0
        local_now = now.astimezone(self.tz)
        review_at = self._daily_review_time(local_now)
        if local_now < review_at:
            return 0
        target_id = review_at.date().isoformat()
        if not self._core_reminder_marked(DAILY_REVIEW_TARGET_TYPE, target_id, DAILY_REVIEW_SENT_CHANNEL):
            if local_now >= review_at + DAILY_REVIEW_SEND_GRACE:
                return 0
            try:
                await self.feishu.send_interactive_card(open_id, self._render_daily_review_card(target_id, review_at))
            except Exception:
                return 0
            self._mark_core_reminder(
                DAILY_REVIEW_TARGET_TYPE,
                target_id,
                DAILY_REVIEW_SENT_CHANNEL,
                "done",
                remind_at=local_now,
            )
            return 1
        if self._core_reminder_marked(DAILY_REVIEW_TARGET_TYPE, target_id, DAILY_REVIEW_CONFIRMED_CHANNEL):
            return 0
        sent_row = self._core_reminder_row(DAILY_REVIEW_TARGET_TYPE, target_id, DAILY_REVIEW_SENT_CHANNEL)
        sent_at = self._parse_core_due_at(sent_row.get("remind_at") if sent_row else None) or review_at
        return await self._send_daily_review_followup(open_id, target_id, sent_at, local_now)

    def _daily_review_time(self, local_now: datetime) -> datetime:
        settings = get_settings()
        hour = min(max(int(settings.default_morning_review_hour), 0), 23)
        minute = min(max(int(settings.default_morning_review_minute), 0), 59)
        return local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    async def _send_daily_review_followup(
        self,
        open_id: str,
        target_id: str,
        sent_at: datetime,
        local_now: datetime,
    ) -> int:
        settings = get_settings()
        followup_delay = timedelta(hours=max(1, int(settings.daily_review_followup_hours)))
        sent_at = sent_at.astimezone(self.tz)
        if local_now < sent_at + followup_delay:
            return 0
        interval_index = int((local_now - sent_at).total_seconds() // followup_delay.total_seconds())
        if interval_index <= 0:
            return 0
        channel = f"{DAILY_REVIEW_FOLLOWUP_CHANNEL_PREFIX}:{interval_index}"
        if self._core_reminder_marked(DAILY_REVIEW_TARGET_TYPE, target_id, channel):
            return 0
        text = (
            f"{target_id} 的晨间任务汇总还未确认。\n"
            "请查看 7:30 任务汇总卡片；如果已经看过，点击本强提醒卡片里的停止按钮即可。"
        )
        try:
            await self._send_strong_reminder(
                open_id,
                "今日任务汇总未确认",
                local_now,
                fallback_text=text,
                target_type=DAILY_REVIEW_TARGET_TYPE,
                target_id=target_id,
            )
        except Exception:
            return 0
        self._mark_core_reminder(DAILY_REVIEW_TARGET_TYPE, target_id, channel, "done", remind_at=local_now)
        return 1

    def _render_daily_review_card(self, target_id: str, review_at: datetime) -> dict[str, Any]:
        value = {"action": "ack_daily_review", "target_type": DAILY_REVIEW_TARGET_TYPE, "target_id": target_id}
        return {
            "config": {"wide_screen_mode": True},
            "header": {"template": "green", "title": {"tag": "plain_text", "content": "今日任务汇总"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": self._render_daily_review_markdown(review_at)}},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "name": f"ack_daily_review_{target_id}",
                            "text": {"tag": "plain_text", "content": "已查看"},
                            "type": "primary",
                            "value": value,
                            "behaviors": [{"type": "callback", "value": value}],
                        }
                    ],
                },
            ],
            "_mvp_meta": value,
        }

    def _render_daily_review_markdown(self, review_at: datetime) -> str:
        sections = self._daily_review_sections(review_at)
        lines = [f"**{review_at.date().isoformat()} 今日任务汇总**", ""]
        self._append_daily_review_section(lines, "逾期任务", sections["overdue_tasks"], self._format_review_task)
        self._append_daily_review_section(lines, "今天任务", sections["today_tasks"], self._format_review_task)
        self._append_daily_review_section(lines, "今日日程", sections["calendar_events"], self._format_review_event)
        self._append_daily_review_section(lines, "固定安排", sections["schedule_blocks"], self._format_review_event)
        self._append_daily_review_section(lines, "待确认", sections["pending_confirmations"], self._format_review_confirmation)
        self._append_daily_review_section(lines, "未排期任务", sections["unscheduled_tasks"], self._format_review_task)
        lines.append("确认已查看后，本日晨间汇总不会再触发 2 小时强提醒。")
        return "\n".join(lines)

    def _daily_review_sections(self, review_at: datetime) -> dict[str, list[dict[str, Any]]]:
        day_start = review_at.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        return {
            "overdue_tasks": self._list_review_tasks(end=review_at, limit=12),
            "today_tasks": self._list_review_tasks(start=review_at, end=day_end, limit=12),
            "unscheduled_tasks": self._list_unscheduled_review_tasks(limit=8),
            "calendar_events": self._list_review_calendar_events(day_start, day_end),
            "schedule_blocks": self._list_review_schedule_blocks(review_at),
            "pending_confirmations": self._list_review_pending_confirmations(limit=8),
        }

    def _list_review_tasks(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int,
    ) -> list[dict[str, Any]]:
        where = ["status NOT IN ('done', 'canceled')", "due_at IS NOT NULL"]
        params: list[Any] = []
        if start:
            where.append("due_at>=?")
            params.append(start.isoformat())
        if end:
            where.append("due_at<?")
            params.append(end.isoformat())
        params.append(limit)
        with self.repo.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, title, priority, due_at, estimated_minutes
                FROM action_items
                WHERE {' AND '.join(where)}
                ORDER BY due_at ASC, priority ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def _list_unscheduled_review_tasks(self, *, limit: int) -> list[dict[str, Any]]:
        with self.repo.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, priority, due_at, estimated_minutes
                FROM action_items
                WHERE status NOT IN ('done', 'canceled')
                  AND due_at IS NULL
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _list_review_calendar_events(self, day_start: datetime, day_end: datetime) -> list[dict[str, Any]]:
        with self.repo.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, title, start_at, end_at
                FROM calendar_events
                WHERE status!='canceled'
                  AND start_at>=?
                  AND start_at<?
                ORDER BY start_at ASC
                LIMIT 20
                """,
                (day_start.isoformat(), day_end.isoformat()),
            ).fetchall()
        return [dict(row) for row in rows]

    def _list_review_schedule_blocks(self, review_at: datetime) -> list[dict[str, Any]]:
        target_date = review_at.date()
        return [
            item
            for item in self._list_core_schedule_occurrences(review_at)
            if item["target_type"] == "schedule_block" and item["start_at"].astimezone(self.tz).date() == target_date
        ][:20]

    def _list_review_pending_confirmations(self, *, limit: int) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ["status='pending'"]
        if self.fallback_open_id:
            where.append("(sender_id=? OR sender_id IS NULL)")
            params.append(self.fallback_open_id)
        params.append(limit * 4)
        with self.repo.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, confirmation_type, expires_at, created_at, proposed_tool_calls_json
                FROM confirmations
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        active: list[dict[str, Any]] = []
        expired_ids: list[str] = []
        now = self.now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=self.tz)
        for row in rows:
            item = dict(row)
            expires_at = self._parse_core_due_at(item.get("expires_at"))
            if expires_at and expires_at.astimezone(self.tz) < now.astimezone(self.tz):
                expired_ids.append(str(item["id"]))
                continue
            active.append(item)
            if len(active) >= limit:
                break
        if expired_ids:
            with self.repo.connect() as conn:
                placeholders = ",".join("?" for _ in expired_ids)
                conn.execute(
                    f"UPDATE confirmations SET status='expired', resolved_at=? WHERE id IN ({placeholders}) AND status='pending'",
                    (utcnow_iso(), *expired_ids),
                )
        return active

    def _append_daily_review_section(
        self,
        lines: list[str],
        title: str,
        items: list[dict[str, Any]],
        formatter: Callable[[dict[str, Any]], str],
    ) -> None:
        lines.append(f"**{title}（{len(items)}）**")
        if not items:
            lines.append("- 无")
        else:
            for item in items[:12]:
                lines.append(f"- {formatter(item)}")
            if len(items) > 12:
                lines.append(f"- 另有 {len(items) - 12} 项")
        lines.append("")

    def _format_review_task(self, item: dict[str, Any]) -> str:
        due_at = self._parse_core_due_at(item.get("due_at"))
        due_text = due_at.astimezone(self.tz).strftime("%H:%M") if due_at else "未排期"
        estimate = f" ｜ {item['estimated_minutes']}分钟" if item.get("estimated_minutes") else ""
        return f"[{item.get('priority') or 'P3'}] {item['title']} ｜ {due_text}{estimate} ｜ {item['id']}"

    def _format_review_event(self, item: dict[str, Any]) -> str:
        start = self._parse_core_due_at(item.get("start_at"))
        end = self._parse_core_due_at(item.get("end_at"))
        if start and end:
            time_text = f"{start.astimezone(self.tz):%H:%M}-{end.astimezone(self.tz):%H:%M}"
        elif start:
            time_text = start.astimezone(self.tz).strftime("%H:%M")
        else:
            time_text = "时间未定"
        return f"{time_text} ｜ {item['title']} ｜ {item.get('target_id') or item['id']}"

    def _format_review_confirmation(self, item: dict[str, Any]) -> str:
        expires_at = self._parse_core_due_at(item.get("expires_at"))
        expires_text = expires_at.astimezone(self.tz).strftime("%H:%M") if expires_at else "未设置过期"
        return f"{self._review_confirmation_title(item)} ｜ {expires_text} 前确认"

    def _review_confirmation_title(self, item: dict[str, Any]) -> str:
        confirmation_type = str(item.get("confirmation_type") or "")
        labels = {
            "habit_refinement": "养成卡待完善",
            "habit_schedule": "养成日程待确认",
            "course_timetable_refinement": "课程表草案待完善",
            "course_timetable_schedule": "课程表日程待确认",
            "plan_refinement": "长期日程草案待完善",
            "plan_schedule": "长期日程待确认",
            "time_budget_calendar": "长期学习日程待确认",
            "schedule_blocks": "固定安排待确认",
            "create_candidates": "候选事项待确认",
            "update": "修改待确认",
            "cancel_schedule_block": "取消固定安排待确认",
            "cancel_calendar_event": "取消日程待确认",
            "cancel_task": "取消任务待确认",
        }
        titles = self._review_confirmation_candidate_titles(item)
        label = labels.get(confirmation_type, confirmation_type or "待确认项")
        if not titles:
            return label
        suffix = "、".join(titles[:3])
        if len(titles) > 3:
            suffix += f" 等 {len(titles)} 项"
        return f"{label}：{suffix}"

    def _review_confirmation_candidate_titles(self, item: dict[str, Any]) -> list[str]:
        raw = item.get("proposed_tool_calls_json")
        if not raw:
            return []
        try:
            calls = json.loads(str(raw))
        except json.JSONDecodeError:
            return []
        titles: list[str] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            blocks = args.get("blocks")
            if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, dict) and block.get("title"):
                        titles.append(str(block["title"]))
                continue
            if args.get("planned_events") and isinstance(args["planned_events"], list):
                titles.append(f"{len(args['planned_events'])} 个日程候选")
                continue
            title = args.get("title") or args.get("query") or args.get("kind")
            if title:
                titles.append(str(title))
        return list(dict.fromkeys(titles))

    def _should_send(self, action: ActionRecord, now: datetime) -> bool:
        if not action.remind_at:
            return False
        if action.metadata.get("reminder_sent_at"):
            return False
        if action.metadata.get("pre_strong_confirmed_at") or action.metadata.get("strong_reminder_suppressed_at"):
            return False
        return action.remind_at.astimezone(self.tz) <= now.astimezone(self.tz)

    def _should_send_pre_strong_card(self, action: ActionRecord, now: datetime) -> bool:
        if not action.remind_at:
            return False
        if action.metadata.get("pre_strong_card_sent_at"):
            return False
        if action.metadata.get("pre_strong_confirmed_at") or action.metadata.get("strong_reminder_suppressed_at"):
            return False
        local_remind_at = action.remind_at.astimezone(self.tz)
        local_now = now.astimezone(self.tz)
        return local_remind_at - PRE_STRONG_REMINDER_LEAD_TIME <= local_now < local_remind_at

    def _open_id_for_action(self, action: ActionRecord) -> str | None:
        if action.capture_id:
            try:
                capture = self.repo.get_capture(action.capture_id)
            except KeyError:
                capture = None
            if capture:
                open_id = capture.metadata.get("open_id")
                if open_id:
                    return str(open_id)
        return self.fallback_open_id

    def _render_message(self, action: ActionRecord) -> str:
        lines = [f"提醒：{action.title}"]
        if action.start_at and action.due_at:
            start = self._display_dt(action.start_at)
            end = action.due_at.astimezone(self.tz)
            lines.append(f"时间：{start}-{end:%H:%M}")
        elif action.due_at:
            lines.append(f"时间：{self._display_dt(action.due_at)}")
        elif action.start_at:
            lines.append(f"时间：{self._display_dt(action.start_at)}")
        if action.evidence_text:
            lines.append(f"来源：{action.evidence_text}")
        return "\n".join(lines)

    def _render_pre_strong_card(self, action: ActionRecord) -> dict[str, Any]:
        remind_at = self._display_dt(action.remind_at) if action.remind_at else "即将到点"
        body_lines = [
            f"3 分钟后将触发强提醒：**{action.title}**",
            f"提醒时间：{remind_at}",
            "如果已经知道或已处理，点击确认后将不会触发强提醒。",
        ]
        if action.evidence_text:
            body_lines.append(f"来源：{action.evidence_text}")
        value = {"action": "ack_pre_strong_reminder", "action_id": action.id}
        actions = [
            {
                            "tag": "button",
                            "name": f"ack_pre_strong_{action.id}",
                            "text": {"tag": "plain_text", "content": "知道了"},
                            "type": "primary",
                            "value": value,
                            "behaviors": [{"type": "callback", "value": value}],
                        },
            *self._reminder_decision_buttons({"action_id": action.id}),
        ]
        return {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": "提醒确认"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(body_lines)}},
                {
                    "tag": "action",
                    "actions": actions,
                },
            ],
            "_mvp_meta": {"action": "ack_pre_strong_reminder", "action_id": action.id},
        }

    def _display_dt(self, value: datetime) -> str:
        local = value.astimezone(self.tz)
        now = self.now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=self.tz)
        if local.date() == now.astimezone(self.tz).date():
            return f"今天 {local:%H:%M}"
        return local.strftime("%m-%d %H:%M")

    def _mark_sent(self, action: ActionRecord, response: dict[str, Any]) -> None:
        metadata = {
            **action.metadata,
            "reminder_sent_at": utcnow_iso(),
            "reminder_response": response,
            "reminder_error": None,
            "reminder_error_at": None,
        }
        self.repo.update_action(action.id, ActionUpdate(metadata=metadata))

    def _mark_error(self, action: ActionRecord, error: str) -> None:
        metadata = {
            **action.metadata,
            "reminder_error": error,
            "reminder_error_at": utcnow_iso(),
        }
        self.repo.update_action(action.id, ActionUpdate(metadata=metadata))

    def _mark_pre_strong_card_sent(self, action: ActionRecord, response: dict[str, Any]) -> None:
        metadata = {
            **action.metadata,
            "pre_strong_card_sent_at": utcnow_iso(),
            "pre_strong_card_response": response,
            "pre_strong_card_error": None,
            "pre_strong_card_error_at": None,
        }
        self.repo.update_action(action.id, ActionUpdate(metadata=metadata))

    def _mark_pre_strong_card_error(self, action: ActionRecord, error: str) -> None:
        metadata = {
            **action.metadata,
            "pre_strong_card_error": error,
            "pre_strong_card_error_at": utcnow_iso(),
        }
        self.repo.update_action(action.id, ActionUpdate(metadata=metadata))

    async def _run_core_action_item_reminders(self, now: datetime) -> int:
        sent_count = 0
        for item in self._list_core_action_items():
            due_at = self._parse_core_due_at(item.get("due_at"))
            if not due_at:
                continue
            if self._core_reminder_marked("action_item", str(item["id"]), "strong_reminder_suppressed"):
                continue
            if self._should_send_core_pre_strong_card(item, due_at, now):
                open_id = self._open_id_for_core_item(item)
                if not open_id:
                    continue
                try:
                    await self.feishu.send_interactive_card(open_id, self._render_core_pre_strong_card(item, due_at))
                except Exception:
                    continue
                self._mark_core_reminder("action_item", str(item["id"]), "pre_strong_card_sent", "done")
                sent_count += 1
                continue
            if self._should_send_core_strong_reminder(item, due_at, now):
                open_id = self._open_id_for_core_item(item)
                if not open_id:
                    continue
                try:
                    await self._send_strong_reminder(
                        open_id,
                        str(item["title"]),
                        due_at,
                        target_type="action_item",
                        target_id=str(item["id"]),
                    )
                except Exception:
                    continue
                self._mark_core_reminder("action_item", str(item["id"]), "strong_reminder_sent", "done")
                sent_count += 1
        return sent_count

    async def _run_core_schedule_reminders(self, now: datetime) -> int:
        sent_count = 0
        for item in self._list_core_schedule_occurrences(now):
            target_type = str(item["target_type"])
            target_id = str(item["target_id"])
            due_at = item["start_at"]
            if target_type == "schedule_block" and not item.get("reminder_enabled", True):
                continue
            if self._core_reminder_marked(target_type, target_id, "strong_reminder_suppressed"):
                continue
            if self._should_send_core_pre_strong_card_for_target(target_type, target_id, due_at, now):
                open_id = self._open_id_for_core_item(item)
                if not open_id:
                    continue
                try:
                    await self.feishu.send_interactive_card(open_id, self._render_core_pre_strong_card(item, due_at))
                except Exception:
                    continue
                self._mark_core_reminder(target_type, target_id, "pre_strong_card_sent", "done")
                sent_count += 1
                continue
            if self._should_send_core_schedule_strong_reminder(target_type, target_id, due_at, now):
                open_id = self._open_id_for_core_item(item)
                if not open_id:
                    continue
                try:
                    await self._send_strong_reminder(
                        open_id,
                        str(item["title"]),
                        due_at,
                        fallback_text=self._render_core_message(item, due_at),
                        target_type=target_type,
                        target_id=target_id,
                    )
                except Exception:
                    continue
                self._mark_core_reminder(target_type, target_id, "strong_reminder_sent", "done")
                sent_count += 1
        return sent_count

    def _list_core_action_items(self) -> list[dict[str, Any]]:
        with self.repo.connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT id, title, description, due_at, source_capture_id
                    FROM action_items
                    WHERE due_at IS NOT NULL
                      AND status NOT IN ('done', 'canceled')
                    ORDER BY due_at ASC
                    LIMIT 500
                    """
                ).fetchall()
            except Exception:
                return []
        return [dict(row) for row in rows]

    def _parse_core_due_at(self, value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            due_at = value
        else:
            try:
                due_at = datetime.fromisoformat(str(value))
            except ValueError:
                return None
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=self.tz)
        return due_at

    def _should_send_core_pre_strong_card(self, item: dict[str, Any], due_at: datetime, now: datetime) -> bool:
        target_id = str(item["id"])
        return self._should_send_core_pre_strong_card_for_target("action_item", target_id, due_at, now)

    def _should_send_core_pre_strong_card_for_target(
        self,
        target_type: str,
        target_id: str,
        due_at: datetime,
        now: datetime,
    ) -> bool:
        if self._core_reminder_marked(target_type, target_id, "pre_strong_card_sent"):
            return False
        local_due_at = due_at.astimezone(self.tz)
        local_now = now.astimezone(self.tz)
        return local_due_at - PRE_STRONG_REMINDER_LEAD_TIME <= local_now < local_due_at

    def _should_send_core_strong_reminder(self, item: dict[str, Any], due_at: datetime, now: datetime) -> bool:
        target_id = str(item["id"])
        if self._core_reminder_marked("action_item", target_id, "strong_reminder_sent"):
            return False
        return due_at.astimezone(self.tz) <= now.astimezone(self.tz)

    def _should_send_core_schedule_strong_reminder(
        self,
        target_type: str,
        target_id: str,
        due_at: datetime,
        now: datetime,
    ) -> bool:
        if self._core_reminder_marked(target_type, target_id, "strong_reminder_sent"):
            return False
        local_due_at = due_at.astimezone(self.tz)
        local_now = now.astimezone(self.tz)
        return local_due_at <= local_now < local_due_at + SCHEDULE_STRONG_REMINDER_GRACE

    def _open_id_for_core_item(self, item: dict[str, Any]) -> str | None:
        capture_id = item.get("source_capture_id")
        if capture_id:
            with self.repo.connect() as conn:
                row = conn.execute("SELECT sender_id FROM core_captures WHERE id=?", (capture_id,)).fetchone()
            if row and row["sender_id"]:
                return str(row["sender_id"])
        return self.fallback_open_id

    def _render_core_pre_strong_card(self, item: dict[str, Any], due_at: datetime) -> dict[str, Any]:
        target_type = str(item.get("target_type") or "action_item")
        target_id = str(item.get("target_id") or item["id"])
        value = {"action": "ack_pre_strong_reminder", "target_type": target_type, "target_id": target_id}
        body_lines = [
            f"3 分钟后将触发强提醒：**{item['title']}**",
            f"提醒时间：{self._display_dt(due_at)}",
            "如果已经知道或已处理，点击确认后将不会触发强提醒。",
        ]
        actions = [
            {
                "tag": "button",
                "name": f"ack_pre_strong_{target_id}",
                "text": {"tag": "plain_text", "content": "知道了"},
                "type": "primary",
                "value": value,
                "behaviors": [{"type": "callback", "value": value}],
            }
        ]
        if target_type in {"action_item", "calendar_event"}:
            actions.extend(self._reminder_decision_buttons({"target_type": target_type, "target_id": target_id}))
        return {
            "config": {"wide_screen_mode": True},
            "header": {"template": "blue", "title": {"tag": "plain_text", "content": "提醒确认"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(body_lines)}},
                {
                    "tag": "action",
                    "actions": actions,
                },
            ],
            "_mvp_meta": value,
        }

    def _reminder_decision_buttons(self, base_value: dict[str, Any]) -> list[dict[str, Any]]:
        name_target = str(base_value.get("target_id") or base_value.get("action_id") or "target").replace(":", "_")
        options = [
            ("cancel", "取消安排", {"action": "cancel_reminder_target", **base_value}),
            ("next_free", "最近空闲", {"action": "reschedule_reminder", "mode": "next_available", **base_value}),
            (
                "after_60",
                "1小时后再排",
                {"action": "reschedule_reminder", "mode": "after_delay", "delay_minutes": 60, **base_value},
            ),
        ]
        return [
            {
                "tag": "button",
                "name": f"reminder_{name}_{name_target}",
                "text": {"tag": "plain_text", "content": text},
                "type": "default",
                "value": value,
                "behaviors": [{"type": "callback", "value": value}],
            }
            for name, text, value in options
        ]

    def _render_core_message(self, item: dict[str, Any], due_at: datetime) -> str:
        return f"提醒：{item['title']}\n时间：{self._display_dt(due_at)}"

    def _list_core_schedule_occurrences(self, now: datetime) -> list[dict[str, Any]]:
        day_start = now.astimezone(self.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = day_start - timedelta(days=1)
        window_end = day_start + timedelta(days=2)
        occurrences: list[dict[str, Any]] = []
        with self.repo.connect() as conn:
            calendar_rows = conn.execute(
                """
                SELECT id, title, start_at, end_at, source_capture_id
                FROM calendar_events
                WHERE status!='canceled'
                  AND start_at IS NOT NULL
                  AND start_at >= ?
                  AND start_at < ?
                ORDER BY start_at ASC
                """,
                (window_start.isoformat(), window_end.isoformat()),
            ).fetchall()
            schedule_rows = conn.execute(
                """
                SELECT id, title, recurrence_rule, start_time, end_time, source_capture_id, reminder_enabled
                FROM schedule_blocks
                WHERE status!='canceled'
                ORDER BY start_time ASC
                """
            ).fetchall()
        for row in calendar_rows:
            start_at = self._parse_core_due_at(row["start_at"])
            if not start_at:
                continue
            occurrences.append(
                {
                    "id": row["id"],
                    "target_type": "calendar_event",
                    "target_id": row["id"],
                    "title": row["title"],
                    "start_at": start_at,
                    "source_capture_id": row["source_capture_id"],
                }
            )
        for offset in (-1, 0, 1):
            day = day_start + timedelta(days=offset)
            for row in schedule_rows:
                if not self._schedule_block_matches_date(dict(row), day):
                    continue
                start_at = self._datetime_on_day(day, str(row["start_time"]))
                end_at = self._datetime_on_day(day, str(row["end_time"]))
                if end_at <= start_at:
                    end_at += timedelta(days=1)
                date_key = day.date().isoformat()
                occurrences.append(
                    {
                        "id": row["id"],
                        "target_type": "schedule_block",
                        "target_id": f"{row['id']}:{date_key}",
                        "title": row["title"],
                        "start_at": start_at,
                        "end_at": end_at,
                        "reminder_enabled": bool(row["reminder_enabled"]),
                        "source_capture_id": row["source_capture_id"],
                    }
                )
        return sorted(occurrences, key=lambda item: item["start_at"])

    def _schedule_block_matches_date(self, block: dict[str, Any], day: datetime) -> bool:
        byday = self._rrule_days(str(block.get("recurrence_rule") or ""))
        day_code = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][day.weekday()]
        return not byday or day_code in byday

    def _rrule_days(self, rrule: str) -> set[str]:
        for part in rrule.split(";"):
            if part.startswith("BYDAY="):
                return {item.strip() for item in part.removeprefix("BYDAY=").split(",") if item.strip()}
        return set()

    def _datetime_on_day(self, day: datetime, time_value: str) -> datetime:
        if time_value == "24:00":
            return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        hour, minute = time_value.split(":", 1)
        return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)

    async def _send_strong_reminder(
        self,
        open_id: str,
        title: str,
        remind_at: datetime | None,
        *,
        fallback_text: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        settings = get_settings()
        if settings.feishu_strong_reminder_mode != "video_meeting":
            text = await self.feishu.send_app_text(open_id, fallback_text or f"强提醒：{title}")
            push = await self._send_pushover_emergency(title, target_type=target_type, target_id=target_id)
            return {"mode": "text", "message": text, "pushover": push}
        try:
            meeting = await self.feishu.create_video_meeting_reminder(
                open_id,
                f"强提醒：{title}",
                end_at=(remind_at or self.now_provider()) + timedelta(minutes=settings.feishu_video_meeting_ttl_minutes),
            )
        except Exception as exc:  # noqa: BLE001 - fallback keeps reminders usable while Feishu permissions are tuned
            error_text = str(exc) or repr(exc)
            text = f"强提醒：{title}\n视频会议创建失败，已降级为文本提醒：{error_text}"
            fallback = await self.feishu.send_app_text(open_id, text)
            push = await self._send_pushover_emergency(title, target_type=target_type, target_id=target_id)
            return {"mode": "text_fallback", "error": error_text, "message": fallback, "pushover": push}
        stop_value = None
        if target_type and target_id:
            stop_value = {"action": "ack_pre_strong_reminder", "target_type": target_type, "target_id": target_id}
        card = self._render_video_meeting_card(title, meeting, stop_value=stop_value)
        message = await self.feishu.send_interactive_card(open_id, card)
        text = await self.feishu.send_app_text(open_id, fallback_text or f"强提醒：{title}\n视频会议已创建，请进入会议。")
        meeting_url = self._extract_meeting_url(meeting)
        push = await self._send_pushover_emergency(
            title,
            url=meeting_url,
            target_type=target_type,
            target_id=target_id,
        )
        return {"mode": "video_meeting", "meeting": meeting, "message": message, "text": text, "pushover": push}

    async def _send_pushover_emergency(
        self,
        title: str,
        *,
        url: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.push or not self.push.configured:
            return None
        tags = pushover_tag_for_target(target_type, target_id) if target_type and target_id else None
        try:
            return await self.push.send_emergency(
                "强提醒",
                title,
                url=url,
                url_title="进入会议" if url else None,
                tags=tags,
            )
        except Exception as exc:  # noqa: BLE001 - Feishu fallback should still be sent if push fails
            return {"status": "failed", "error": str(exc) or repr(exc)}

    def _render_video_meeting_card(
        self,
        title: str,
        meeting: dict[str, Any],
        *,
        stop_value: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meeting_url = self._extract_meeting_url(meeting)
        body = f"强提醒：**{title}**\n已为你创建视频会议。"
        actions: list[dict[str, Any]] = []
        if meeting_url:
            body += "\n点击按钮进入会议。"
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "进入会议"},
                    "type": "primary",
                    "url": meeting_url,
                }
            )
        if stop_value:
            stop_name = f"stop_strong_{stop_value['target_type']}_{stop_value['target_id']}".replace(":", "_")
            actions.append(
                {
                    "tag": "button",
                    "name": stop_name,
                    "text": {"tag": "plain_text", "content": "停止强提醒"},
                    "type": "default",
                    "value": stop_value,
                    "behaviors": [{"type": "callback", "value": stop_value}],
                }
            )
        return {
            "config": {"wide_screen_mode": True},
            "header": {"template": "red", "title": {"tag": "plain_text", "content": "强提醒"}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
                *([{"tag": "action", "actions": actions}] if actions else []),
            ],
            "_mvp_meta": {"mode": "video_meeting", "meeting_url": meeting_url, "stop_value": stop_value},
        }

    def _extract_meeting_url(self, meeting: dict[str, Any]) -> str | None:
        candidates = [
            meeting.get("data", {}).get("reserve", {}).get("meeting_url"),
            meeting.get("data", {}).get("reserve", {}).get("url"),
            meeting.get("data", {}).get("reserve", {}).get("share_url"),
            meeting.get("data", {}).get("reserve", {}).get("app_link"),
            meeting.get("data", {}).get("meeting_url"),
            meeting.get("data", {}).get("url"),
            meeting.get("data", {}).get("share_url"),
            meeting.get("data", {}).get("app_link"),
        ]
        for value in candidates:
            if value:
                return str(value)
        return None

    def _core_reminder_marked(self, target_type: str, target_id: str, channel: str) -> bool:
        return self._core_reminder_row(target_type, target_id, channel) is not None

    def _core_reminder_row(self, target_type: str, target_id: str, channel: str) -> dict[str, Any] | None:
        with self.repo.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM reminders
                WHERE target_type=? AND target_id=? AND channel=? AND status='done'
                LIMIT 1
                """,
                (target_type, target_id, channel),
            ).fetchone()
        return dict(row) if row else None

    def _mark_core_reminder(
        self,
        target_type: str,
        target_id: str,
        channel: str,
        status: str,
        *,
        remind_at: datetime | None = None,
    ) -> None:
        reminder_time = (remind_at.isoformat() if remind_at else utcnow_iso())
        with self.repo.connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM reminders
                WHERE target_type=? AND target_id=? AND channel=?
                LIMIT 1
                """,
                (target_type, target_id, channel),
            ).fetchone()
            if existing:
                conn.execute("UPDATE reminders SET status=?, remind_at=? WHERE id=?", (status, reminder_time, existing["id"]))
                return
            conn.execute(
                """
                INSERT INTO reminders (id, target_type, target_id, remind_at, channel, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (new_id("rem"), target_type, target_id, reminder_time, channel, status, utcnow_iso()),
            )


def main() -> None:
    settings = get_settings()
    repo = Repository(settings.database_path, database_url=settings.database_url)
    repo.migrate()
    worker = ReminderWorker(
        repo,
        FeishuClient(settings),
        settings.tzinfo,
        fallback_open_id=settings.feishu_default_assignee_open_id,
        push=PushoverClient(settings),
        poll_seconds=settings.reminder_worker_poll_seconds,
    )
    asyncio.run(worker.run_forever())


if __name__ == "__main__":
    main()
