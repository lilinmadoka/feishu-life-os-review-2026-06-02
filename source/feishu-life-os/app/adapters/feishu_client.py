from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.models import ActionRecord, CaptureRecord


class FeishuConfigError(RuntimeError):
    pass


def _feishu_rgb(red: int, green: int, blue: int) -> int:
    value = (255 << 24) | (red << 16) | (green << 8) | blue
    return value - (1 << 32) if value >= (1 << 31) else value


EVENT_COLORS = {
    "study": _feishu_rgb(139, 92, 246),
    "course": _feishu_rgb(59, 130, 246),
    "tutoring": _feishu_rgb(245, 158, 11),
    "driving": _feishu_rgb(239, 68, 68),
    "exam": _feishu_rgb(220, 38, 38),
    "meeting": _feishu_rgb(34, 197, 94),
    "default": _feishu_rgb(20, 184, 166),
    "fixed": _feishu_rgb(100, 116, 139),
}


def _filename_from_disposition(value: str | None) -> str | None:
    if not value:
        return None
    for part in value.split(";"):
        key, _, raw = part.strip().partition("=")
        if key.lower() in {"filename", "filename*"} and raw:
            return raw.strip().strip('"')
    return None


class FeishuClient:
    """Minimal Feishu OpenAPI client.

    The methods are intentionally thin wrappers around HTTP endpoints so Codex can
    compare payloads with the current Feishu docs during final integration.
    Default application mode is dry-run via SyncService; this client only executes
    calls when explicitly used.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._tenant_token: str | None = None
        self._tenant_token_expire_at: float = 0

    async def tenant_access_token(self) -> str:
        if self._tenant_token and time.time() < self._tenant_token_expire_at - 300:
            return self._tenant_token
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret:
            raise FeishuConfigError("FEISHU_APP_ID / FEISHU_APP_SECRET are required")
        payload = {
            "app_id": self.settings.feishu_app_id,
            "app_secret": self.settings.feishu_app_secret,
        }
        data = await self._raw_post("/auth/v3/tenant_access_token/internal", json=payload)
        token = data.get("tenant_access_token")
        if not token:
            raise RuntimeError(f"tenant_access_token missing in Feishu response: {data}")
        self._tenant_token = token
        self._tenant_token_expire_at = time.time() + int(data.get("expire", 7200))
        return token

    async def _raw_post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.feishu_open_api_base.rstrip('/')}{path}"
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(url, json=json)
            data = response.json()
            if response.is_error:
                raise RuntimeError(f"Feishu HTTP error for {path}: {response.status_code} {data}")
        if data.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error for {path}: {data}")
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self.tenant_access_token()
        url = f"{self.settings.feishu_open_api_base.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.request(method, url, headers=headers, json=json, params=params)
            data = response.json()
            if response.is_error:
                raise RuntimeError(f"Feishu HTTP error for {method} {path}: {response.status_code} {data}")
        if data.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error for {method} {path}: {data}")
        return data

    async def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str,
    ) -> dict[str, Any]:
        token = await self.tenant_access_token()
        safe_message_id = quote(message_id, safe="")
        safe_file_key = quote(file_key, safe="")
        url = (
            f"{self.settings.feishu_open_api_base.rstrip('/')}"
            f"/im/v1/messages/{safe_message_id}/resources/{safe_file_key}"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.get(url, headers=headers, params={"type": resource_type})
            if response.is_error:
                raise RuntimeError(
                    f"Feishu HTTP error for GET message resource: {response.status_code} {response.text}"
                )
        content_type = response.headers.get("content-type")
        if content_type:
            content_type = content_type.split(";", 1)[0].strip().lower()
        return {
            "content": response.content,
            "content_type": content_type,
            "filename": _filename_from_disposition(response.headers.get("content-disposition")),
        }

    async def send_webhook_text(self, text: str) -> dict[str, Any]:
        if not self.settings.feishu_bot_webhook:
            raise FeishuConfigError("FEISHU_BOT_WEBHOOK is required")
        payload: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        if self.settings.feishu_bot_secret:
            timestamp = str(int(time.time()))
            sign = self._sign_webhook(timestamp, self.settings.feishu_bot_secret)
            payload.update({"timestamp": timestamp, "sign": sign})
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(self.settings.feishu_bot_webhook, json=payload)
            response.raise_for_status()
            return response.json()

    async def send_app_text(
        self,
        receive_id: str,
        text: str,
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        return await self._request(
            "POST",
            "/im/v1/messages",
            json=payload,
            params={"receive_id_type": receive_id_type},
        )

    async def send_interactive_card(
        self,
        receive_id: str,
        card: dict[str, Any],
        receive_id_type: str = "open_id",
    ) -> dict[str, Any]:
        payload = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        return await self._request(
            "POST",
            "/im/v1/messages",
            json=payload,
            params={"receive_id_type": receive_id_type},
        )

    async def create_video_meeting_reminder(
        self,
        receive_id: str,
        topic: str,
        *,
        end_at: datetime | None = None,
    ) -> dict[str, Any]:
        if end_at is None:
            end_at = datetime.now().astimezone() + timedelta(minutes=self.settings.feishu_video_meeting_ttl_minutes)
        payload = self.to_video_meeting_reminder_payload(receive_id, topic, end_at)
        return await self._request(
            "POST",
            "/vc/v1/reserves/apply",
            json=payload,
            params={"user_id_type": "open_id"},
        )

    def _sign_webhook(self, timestamp: str, secret: str) -> str:
        string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
        digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    async def bitable_batch_create_records(
        self, table_id: str, records: list[dict[str, Any]], client_token: str | None = None
    ) -> dict[str, Any]:
        if not self.settings.feishu_bitable_app_token:
            raise FeishuConfigError("FEISHU_BITABLE_APP_TOKEN is required")
        params = {"client_token": client_token} if client_token else None
        path = (
            f"/bitable/v1/apps/{self.settings.feishu_bitable_app_token}"
            f"/tables/{table_id}/records/batch_create"
        )
        return await self._request("POST", path, json={"records": records}, params=params)

    async def create_task(self, action: ActionRecord) -> dict[str, Any]:
        payload = self.to_task_payload(action)
        return await self._request("POST", "/task/v2/tasks", json=payload)

    async def create_calendar_event(self, action: ActionRecord) -> dict[str, Any]:
        calendar_id = self.settings.feishu_calendar_id
        payload = self.to_calendar_payload(action)
        return await self._request("POST", f"/calendar/v4/calendars/{calendar_id}/events", json=payload)

    async def create_core_task(self, action_item: dict[str, Any]) -> dict[str, Any]:
        payload = self.to_core_task_payload(action_item)
        return await self._request("POST", "/task/v2/tasks", json=payload)

    async def create_core_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        calendar_id = self.settings.feishu_calendar_id
        payload = self.to_core_calendar_payload(calendar_event)
        return await self._request("POST", f"/calendar/v4/calendars/{calendar_id}/events", json=payload)

    async def create_core_schedule_block_event(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        calendar_id = self.settings.feishu_calendar_id
        payload = self.to_core_schedule_block_payload(schedule_block)
        return await self._request("POST", f"/calendar/v4/calendars/{calendar_id}/events", json=payload)

    async def update_core_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        calendar_id = self.settings.feishu_calendar_id
        event_id = calendar_event.get("feishu_event_id")
        if not event_id:
            raise FeishuConfigError("calendar_event.feishu_event_id is required")
        payload = self.to_core_calendar_payload(calendar_event)
        return await self._request("PATCH", f"/calendar/v4/calendars/{calendar_id}/events/{event_id}", json=payload)

    async def delete_core_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        calendar_id = self.settings.feishu_calendar_id
        event_id = calendar_event.get("feishu_event_id")
        if not event_id:
            return {"status": "skipped", "reason": "missing_feishu_event_id"}
        return await self._request("DELETE", f"/calendar/v4/calendars/{calendar_id}/events/{event_id}")

    async def update_core_schedule_block_event(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        calendar_id = self.settings.feishu_calendar_id
        event_id = schedule_block.get("feishu_event_id")
        if not event_id:
            raise FeishuConfigError("schedule_block.feishu_event_id is required")
        payload = self.to_core_schedule_block_payload(schedule_block)
        return await self._request("PATCH", f"/calendar/v4/calendars/{calendar_id}/events/{event_id}", json=payload)

    async def delete_core_schedule_block_event(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        calendar_id = self.settings.feishu_calendar_id
        event_id = schedule_block.get("feishu_event_id")
        if not event_id:
            return {"status": "skipped", "reason": "missing_feishu_event_id"}
        return await self._request("DELETE", f"/calendar/v4/calendars/{calendar_id}/events/{event_id}")

    async def list_core_calendar_attendees(self, event_id: str) -> dict[str, Any]:
        calendar_id = self.settings.feishu_calendar_id
        return await self._request(
            "GET",
            f"/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees",
            params={"user_id_type": "open_id", "page_size": 100},
        )

    async def add_core_calendar_attendees(
        self,
        event_id: str,
        attendee_open_ids: list[str] | None = None,
        *,
        need_notification: bool = False,
    ) -> dict[str, Any]:
        calendar_id = self.settings.feishu_calendar_id
        target_open_ids = attendee_open_ids if attendee_open_ids is not None else self.calendar_attendee_open_ids()
        attendees = [{"type": "user", "user_id": open_id, "is_optional": False} for open_id in target_open_ids]
        if not attendees:
            return {"status": "skipped", "reason": "no_calendar_attendees"}
        return await self._request(
            "POST",
            f"/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees",
            json={"attendees": attendees, "need_notification": need_notification},
            params={"user_id_type": "open_id"},
        )

    async def ensure_core_calendar_attendees(
        self,
        event_id: str,
        attendee_open_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        target_open_ids = attendee_open_ids if attendee_open_ids is not None else self.calendar_attendee_open_ids()
        target_open_ids = list(dict.fromkeys(target_open_ids))
        if not target_open_ids:
            return {"status": "skipped", "reason": "no_calendar_attendees"}
        attendee_list = await self.list_core_calendar_attendees(event_id)
        existing = {
            item.get("user_id")
            for item in attendee_list.get("data", {}).get("items", [])
            if isinstance(item, dict) and item.get("type") == "user"
        }
        missing = [open_id for open_id in target_open_ids if open_id not in existing]
        if not missing:
            return {"status": "unchanged", "event_id": event_id, "attendee_open_ids": target_open_ids}
        response = await self.add_core_calendar_attendees(event_id, missing, need_notification=False)
        return {
            "status": "synced",
            "event_id": event_id,
            "added_open_ids": missing,
            "response": response,
        }

    def calendar_attendee_open_ids(self) -> list[str]:
        raw = (
            self.settings.feishu_calendar_attendee_open_ids
            or self.settings.feishu_default_assignee_open_id
            or self.settings.feishu_allowed_open_ids
            or ""
        )
        normalized = raw.replace(";", ",").replace("\n", ",")
        return [item.strip() for item in normalized.split(",") if item.strip()]

    def to_capture_record(self, capture: CaptureRecord) -> dict[str, Any]:
        if self.settings.feishu_bitable_schema == "personal_base":
            return {
                "fields": {
                    "输入内容": capture.raw_text,
                    "捕获时间": self._dt_ms(capture.created_at),
                }
            }
        return {
            "fields": {
                "捕获ID": capture.id,
                "原始内容": capture.raw_text,
                "规范内容": capture.normalized_text,
                "来源类型": capture.source_type.value,
                "来源引用": capture.source_ref or "",
                "状态": capture.status.value,
                "置信度": capture.confidence,
                "创建时间": self._dt_ms(capture.created_at),
            }
        }

    def to_action_record(self, action: ActionRecord) -> dict[str, Any]:
        if self.settings.feishu_bitable_schema == "personal_base":
            return {
                "fields": {
                    "行动标题": action.title,
                    "行动详情": self._action_description(action),
                    "截止日期": self._dt_ms(action.due_at),
                    "状态": action.status.value,
                    "优先级": action.priority.value,
                }
            }
        return {
            "fields": {
                "行动ID": action.id,
                "标题": action.title,
                "描述": action.description or "",
                "意图": action.intent.value,
                "领域": action.domain.value,
                "状态": action.status.value,
                "优先级": action.priority.value,
                "能量": action.energy.value,
                "截止时间": self._dt_ms(action.due_at),
                "提醒时间": self._dt_ms(action.remind_at),
                "预计分钟": action.estimated_minutes or 0,
                "人物": ", ".join(action.people),
                "项目": ", ".join(action.projects),
                "标签": action.labels,
                "证据": action.evidence_text or "",
                "捕获ID": action.capture_id or "",
                "置信度": action.confidence,
                "创建时间": self._dt_ms(action.created_at),
            }
        }

    def to_task_payload(self, action: ActionRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "summary": action.title,
            "description": self._action_description(action),
        }
        if action.due_at:
            # Codex should validate final Task v2 schema in the current docs.
            payload["due"] = {
                "time": action.due_at.isoformat(),
                "timezone": self.settings.timezone,
                "is_all_day": False,
            }
        if self.settings.feishu_default_assignee_open_id:
            payload["assignees"] = [{"id": self.settings.feishu_default_assignee_open_id}]
        return payload

    def to_core_task_payload(self, action_item: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "summary": action_item["title"],
            "description": action_item.get("description") or f"来源捕获：{action_item.get('source_capture_id') or 'unknown'}",
        }
        due_at = action_item.get("due_at")
        if due_at:
            payload["due"] = {
                "time": str(due_at),
                "timezone": self.settings.timezone,
                "is_all_day": False,
            }
        if self.settings.feishu_default_assignee_open_id:
            payload["assignees"] = [{"id": self.settings.feishu_default_assignee_open_id}]
        return payload

    def to_calendar_payload(self, action: ActionRecord) -> dict[str, Any]:
        start = action.start_at or action.due_at
        if start is None:
            start = datetime.now().astimezone() + timedelta(hours=1)
        duration = action.estimated_minutes or 60
        end = start + timedelta(minutes=duration)
        return {
            "summary": action.title,
            "description": self._action_description(action),
            "start_time": {"timestamp": str(int(start.timestamp())), "timezone": self.settings.timezone},
            "end_time": {"timestamp": str(int(end.timestamp())), "timezone": self.settings.timezone},
            "color": self.calendar_color_for_text(action.title, self._action_description(action)),
        }

    def to_core_calendar_payload(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        start = self._parse_datetime(calendar_event["start_at"])
        end = self._parse_datetime(calendar_event["end_at"])
        return {
            "summary": calendar_event["title"],
            "description": calendar_event.get("description") or f"来源捕获：{calendar_event.get('source_capture_id') or 'unknown'}",
            "start_time": {"timestamp": str(int(start.timestamp())), "timezone": self.settings.timezone},
            "end_time": {"timestamp": str(int(end.timestamp())), "timezone": self.settings.timezone},
            "color": self.calendar_color_for_text(
                str(calendar_event.get("title") or ""),
                str(calendar_event.get("description") or ""),
                str(calendar_event.get("location") or ""),
            ),
            **({"location": {"name": calendar_event["location"]}} if calendar_event.get("location") else {}),
        }

    def to_core_schedule_block_payload(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        start, end = self._schedule_block_datetimes(schedule_block)
        return {
            "summary": schedule_block["title"],
            "description": f"固定日程安排\n来源捕获：{schedule_block.get('source_capture_id') or 'unknown'}",
            "start_time": {"timestamp": str(int(start.timestamp())), "timezone": schedule_block.get("timezone") or self.settings.timezone},
            "end_time": {"timestamp": str(int(end.timestamp())), "timezone": schedule_block.get("timezone") or self.settings.timezone},
            "recurrence": str(schedule_block["recurrence_rule"]),
            "color": self.calendar_color_for_text(
                str(schedule_block.get("title") or ""),
                "固定日程安排",
                item_type="schedule_block",
            ),
        }

    def calendar_color_for_text(self, *parts: str, item_type: str = "calendar_event") -> int:
        text = " ".join(part for part in parts if part).lower()
        if any(token in text for token in ("驾校", "驾驶", "练车", "driving")):
            return EVENT_COLORS["driving"]
        if any(token in text for token in ("考试", "考证", "准考证", "普通话", "exam", "test")):
            return EVENT_COLORS["exam"]
        if any(token in text for token in ("家教", "外出", "补课", "tutor")):
            return EVENT_COLORS["tutoring"]
        if any(token in text for token in ("上课", "课程", "实验课", "class", "course")):
            return EVENT_COLORS["course"]
        if any(token in text for token in ("学习", "复习", "长期学习", "长期累计时间计划", "长期学习安排拆分", "光学", "量子", "study")):
            return EVENT_COLORS["study"]
        if any(token in text for token in ("会议", "会面", "meeting")):
            return EVENT_COLORS["meeting"]
        if item_type == "schedule_block":
            return EVENT_COLORS["fixed"]
        return EVENT_COLORS["default"]

    def _schedule_block_datetimes(self, schedule_block: dict[str, Any]) -> tuple[datetime, datetime]:
        tz = ZoneInfo(str(schedule_block.get("timezone") or self.settings.timezone))
        day = self._next_recurrence_day(str(schedule_block.get("recurrence_rule") or ""), tz)
        start = self._datetime_on_day(day, str(schedule_block["start_time"]))
        end = self._datetime_on_day(day, str(schedule_block["end_time"]))
        if end <= start:
            end += timedelta(days=1)
        return start, end

    def _next_recurrence_day(self, recurrence_rule: str, tz: ZoneInfo) -> datetime:
        now = datetime.now(tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        byday = self._rrule_days(recurrence_rule)
        if not byday:
            return today
        codes = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
        for offset in range(14):
            day = today + timedelta(days=offset)
            if codes[day.weekday()] in byday:
                return day
        return today

    def _rrule_days(self, recurrence_rule: str) -> set[str]:
        for part in recurrence_rule.split(";"):
            if part.startswith("BYDAY="):
                return {item.strip() for item in part.removeprefix("BYDAY=").split(",") if item.strip()}
        return set()

    def _datetime_on_day(self, day: datetime, time_value: str) -> datetime:
        if time_value == "24:00":
            return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        hour, minute = time_value.split(":", 1)
        return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)

    def to_video_meeting_reminder_payload(self, receive_id: str, topic: str, end_at: datetime) -> dict[str, Any]:
        owner_id = (
            self.settings.feishu_video_meeting_owner_open_id
            or self.settings.feishu_default_assignee_open_id
            or receive_id
        )
        meeting_settings: dict[str, Any] = {
            "topic": topic,
            "meeting_initial_type": 1,
        }
        return {
            "end_time": str(int(end_at.timestamp())),
            "owner_id": owner_id,
            "meeting_settings": meeting_settings,
        }

    def _action_description(self, action: ActionRecord) -> str:
        parts = [action.description or ""]
        parts.append(f"来源捕获：{action.capture_id or 'manual'}")
        parts.append(f"领域/优先级：{action.domain.value}/{action.priority.value}")
        if action.evidence_text:
            parts.append(f"原始证据：{action.evidence_text}")
        return "\n".join(part for part in parts if part)

    def _dt_ms(self, value: datetime | None) -> int | None:
        if not value:
            return None
        return int(value.timestamp() * 1000)

    def _parse_datetime(self, value: datetime | str) -> datetime:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value)
