from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.adapters.pushover_client import PushoverClient, pushover_tag_for_target
from app.config import get_settings
from app.core.orchestrator import CoreAgentOrchestrator
from app.core.schemas import CaptureIn
from app.database import new_id, utcnow_iso
from app.dependencies import get_core_orchestrator, get_core_store, get_repo
from app.models import ActionStatus, ActionUpdate

router = APIRouter(prefix="/api/v2", tags=["agent-v2"])
logger = logging.getLogger(__name__)


class LocalMessageRequest(BaseModel):
    raw_text: str
    sender_id: str | None = "local_user"
    chat_id: str | None = "local_chat"
    content_type: str = "text"
    source_message_id: str | None = None
    attachment_refs: list[dict[str, Any]] = Field(default_factory=list)


@router.post("/agent/messages")
async def local_agent_message(
    message: LocalMessageRequest,
    orchestrator: CoreAgentOrchestrator = Depends(get_core_orchestrator),
) -> dict[str, Any]:
    result = await orchestrator.process_capture(
        CaptureIn(
            source="local_api",
            source_message_id=message.source_message_id,
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            content_type=message.content_type,
            raw_text=message.raw_text,
            attachment_refs=message.attachment_refs,
        )
    )
    return result.model_dump(mode="json")


@router.post("/feishu/events")
async def feishu_events_v2(
    request: Request,
    orchestrator: CoreAgentOrchestrator = Depends(get_core_orchestrator),
) -> dict[str, Any]:
    payload = await request.json()
    _verify_event_token(payload)
    if payload.get("type") == "url_verification" and "challenge" in payload:
        return {"challenge": payload["challenge"]}
    header = payload.get("header", {}) if isinstance(payload, dict) else {}
    event = payload.get("event", {}) if isinstance(payload, dict) else {}
    event_type = header.get("event_type") or payload.get("type") or "unknown"
    if event_type != "im.message.receive_v1":
        return {"ok": True, "ignored": True, "reason": "unsupported_event", "event_type": event_type}
    message = event.get("message", {}) if isinstance(event, dict) else {}
    chat_type = message.get("chat_type")
    if chat_type not in {"p2p", "group"}:
        return {"ok": True, "ignored": True, "reason": "unsupported_chat_type"}
    if chat_type == "group" and not _message_mentions_bot(message):
        return {"ok": True, "ignored": True, "reason": "group_message_without_mention"}
    sender_id = _extract_open_id(event)
    if not _is_authorized_feishu_user(sender_id):
        return {"ok": True, "ignored": True, "reason": "unauthorized_sender"}
    raw_text, content_type, attachments = _extract_message(message)
    attachments = await _hydrate_message_attachments(orchestrator.feishu, message.get("message_id"), attachments)
    capture = CaptureIn(
        source="feishu",
        source_message_id=message.get("message_id"),
        source_event_id=header.get("event_id"),
        sender_id=sender_id,
        chat_id=message.get("chat_id"),
        content_type=content_type,
        raw_text=raw_text,
        attachment_refs=attachments,
        received_at=datetime.now(get_settings().tzinfo),
    )
    result = await orchestrator.process_capture(capture)
    return {"ok": True, **result.model_dump(mode="json")}


@router.post("/feishu/card")
async def feishu_card_callback_v2(
    request: Request,
    orchestrator: CoreAgentOrchestrator = Depends(get_core_orchestrator),
) -> dict[str, Any]:
    payload = await request.json()
    _verify_event_token(payload)
    if payload.get("type") == "url_verification" and "challenge" in payload:
        return {"challenge": payload["challenge"]}
    value = _extract_card_action_value(payload)
    confirmation_id = value.get("confirmation_id")
    action = str(value.get("action") or "confirm")
    identity_values = _extract_card_identity_values(payload)
    sender_id = _extract_card_open_id(payload) or _preferred_ou_id(identity_values)
    if not _is_authorized_feishu_user(sender_id, identity_values):
        logger.warning("Unauthorized Feishu card callback action=%s identities=%s", action, sorted(identity_values))
        return {"toast": {"type": "error", "content": "当前账号没有权限操作这个助手。"}}
    if action == "ack_pre_strong_reminder":
        result = await _ack_pre_strong_reminder(value, sender_id)
        await _send_card_action_reply(orchestrator, sender_id, result)
        toast_type = "error" if result.get("status") == "error" else "success"
        return {"toast": {"type": toast_type, "content": str(result.get("reply_text") or "已处理")[:120]}}
    if action == "ack_daily_review":
        result = await _ack_daily_review(value, sender_id)
        await _send_card_action_reply(orchestrator, sender_id, result)
        toast_type = "error" if result.get("status") == "error" else "success"
        return {"toast": {"type": toast_type, "content": str(result.get("reply_text") or "已处理")[:120]}}
    if action in {"reschedule_reminder", "snooze_reminder"}:
        result = await _reschedule_reminder(value, sender_id, orchestrator.feishu)
        await _send_card_action_reply(orchestrator, sender_id, result)
        toast_type = "error" if result.get("status") == "error" else "success"
        return {"toast": {"type": toast_type, "content": str(result.get("reply_text") or "已处理")[:120]}}
    if action == "cancel_reminder_target":
        result = await _cancel_reminder_target(value, sender_id, orchestrator.feishu)
        await _send_card_action_reply(orchestrator, sender_id, result)
        toast_type = "error" if result.get("status") == "error" else "success"
        return {"toast": {"type": toast_type, "content": str(result.get("reply_text") or "已处理")[:120]}}
    try:
        result = await orchestrator.router.resolve_confirmation(
            sender_id=sender_id,
            confirmation_id=confirmation_id,
            action=action,
        )
    except Exception as exc:  # noqa: BLE001 - Feishu card callbacks must still get a toast response.
        result = {
            "status": "error",
            "reply_text": f"确认处理失败：{exc}",
            "created": [],
        }
    reply = result.get("reply_text") or "已处理。"
    if sender_id:
        await orchestrator.feishu.send_text(sender_id, str(reply))
    toast_type = "error" if result.get("status") in {"forbidden", "not_found", "expired", "error"} else "success"
    return {"toast": {"type": toast_type, "content": str(reply)[:120]}}


@router.get("/agent/runs")
def list_agent_runs(limit: int = 20) -> dict[str, Any]:
    store = get_core_store()
    return {"items": [run.model_dump(mode="json") for run in store.list_agent_runs(limit=limit)]}


@router.get("/tool/runs")
def list_tool_runs(limit: int = 20) -> dict[str, Any]:
    store = get_core_store()
    return {"items": [run.model_dump(mode="json") for run in store.list_tool_runs(limit=limit)]}


def _verify_event_token(payload: dict[str, Any]) -> None:
    expected = get_settings().feishu_event_verification_token
    if not expected:
        return
    token = payload.get("token") or payload.get("header", {}).get("token")
    if token != expected:
        raise HTTPException(status_code=403, detail="invalid Feishu event verification token")


async def _send_card_action_reply(orchestrator: CoreAgentOrchestrator, sender_id: str | None, result: dict[str, Any]) -> None:
    reply = result.get("reply_text")
    if not sender_id or not reply:
        return
    try:
        await orchestrator.feishu.send_text(sender_id, str(reply))
    except Exception:  # noqa: BLE001 - card callbacks must still return a Feishu toast.
        logger.exception("Failed to send card action reply")


def _is_authorized_feishu_user(open_id: str | None, identity_values: set[str] | None = None) -> bool:
    settings = get_settings()
    configured_values = [
        settings.feishu_allowed_open_ids,
        settings.feishu_default_assignee_open_id,
        settings.feishu_calendar_attendee_open_ids,
        settings.feishu_video_meeting_owner_open_id,
    ]
    configured = ",".join(value for value in configured_values if value)
    if not configured:
        return True
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    presented = {open_id} if open_id else set()
    presented.update(identity_values or set())
    return bool(presented & allowed)


def _extract_open_id(event: dict[str, Any]) -> str | None:
    sender = event.get("sender", {}) if isinstance(event, dict) else {}
    sender_id = sender.get("sender_id", {}) if isinstance(sender, dict) else {}
    if isinstance(sender_id, dict):
        return sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id")
    return None


def _extract_card_action_value(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        payload.get("action", {}).get("value"),
        payload.get("event", {}).get("action", {}).get("value"),
        payload.get("value"),
    ]
    for value in candidates:
        if isinstance(value, dict):
            return value
    return {}


def _extract_card_open_id(payload: dict[str, Any]) -> str | None:
    candidates = [
        payload.get("operator", {}),
        payload.get("event", {}).get("operator", {}),
        payload.get("user", {}),
    ]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if item.get("open_id"):
            return str(item["open_id"])
        operator_id = item.get("operator_id") or item.get("user_id")
        if isinstance(operator_id, dict):
            open_id = operator_id.get("open_id") or operator_id.get("user_id") or operator_id.get("union_id")
            if open_id:
                return str(open_id)
    return None


def _extract_card_identity_values(payload: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    candidates = [
        payload.get("operator", {}),
        payload.get("event", {}).get("operator", {}),
        payload.get("user", {}),
    ]
    for item in candidates:
        _collect_identity_values(item, values)
    return values


def _collect_identity_values(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"open_id", "user_id", "union_id"} and isinstance(item, str) and item:
                out.add(item)
            elif isinstance(item, dict | list):
                _collect_identity_values(item, out)
        return
    if isinstance(value, list):
        for item in value:
            _collect_identity_values(item, out)


def _preferred_ou_id(identity_values: set[str]) -> str | None:
    for value in sorted(identity_values):
        if value.startswith("ou_"):
            return value
    return None


async def _ack_pre_strong_reminder(value: dict[str, Any], sender_id: str | None) -> dict[str, Any]:
    target_type = value.get("target_type")
    target_id = value.get("target_id")
    if target_type == "daily_review" and target_id:
        return await _ack_daily_review(value, sender_id)
    if target_type and target_id:
        result = _ack_core_pre_strong_reminder(str(target_type), str(target_id), sender_id)
        if result.get("status") != "error":
            await _cancel_pushover_retries(str(target_type), str(target_id))
        return result
    action_id = value.get("action_id")
    if not action_id:
        return {"status": "error", "reply_text": "缺少提醒记录 ID，无法确认。"}
    repo = get_repo()
    try:
        reminder = repo.get_action(str(action_id))
    except KeyError:
        return {"status": "error", "reply_text": "提醒记录不存在或已被清理。"}
    now = utcnow_iso()
    metadata = {
        **reminder.metadata,
        "pre_strong_confirmed_at": now,
        "pre_strong_confirmed_by": sender_id,
        "strong_reminder_suppressed_at": now,
        "strong_reminder_suppressed_reason": "pre_strong_card_confirmed",
    }
    repo.update_action(reminder.id, ActionUpdate(metadata=metadata))
    await _cancel_pushover_retries("legacy_action", reminder.id)
    return {"status": "success", "reply_text": "已确认，本次强提醒已取消。"}


async def _ack_daily_review(value: dict[str, Any], sender_id: str | None) -> dict[str, Any]:
    target_id = value.get("target_id")
    if not target_id:
        target_id = datetime.now(get_settings().tzinfo).date().isoformat()
    target_id = str(target_id)
    _upsert_core_reminder("daily_review", target_id, "summary_confirmed", "done")
    if sender_id:
        _upsert_core_reminder("daily_review", target_id, f"confirmed_by:{sender_id}", "done")
    await _cancel_pushover_retries("daily_review", target_id)
    return {"status": "success", "reply_text": "已确认收到今日任务汇总，本日晨间强提醒已停止。"}


async def _reschedule_reminder(value: dict[str, Any], sender_id: str | None, feishu: Any) -> dict[str, Any]:
    mode = str(value.get("mode") or "after_delay")
    delay_minutes = _bounded_delay_minutes(value.get("delay_minutes") or value.get("minutes"))
    target_type = value.get("target_type")
    target_id = value.get("target_id")
    if target_type and target_id:
        result = await _reschedule_core_reminder(str(target_type), str(target_id), mode, delay_minutes, sender_id, feishu)
        if result.get("status") != "error":
            await _cancel_pushover_retries(str(target_type), str(target_id))
        return result

    action_id = value.get("action_id")
    if not action_id:
        return {"status": "error", "reply_text": "缺少提醒记录 ID，无法重排。"}
    repo = get_repo()
    try:
        reminder = repo.get_action(str(action_id))
    except KeyError:
        return {"status": "error", "reply_text": "提醒记录不存在或已被清理。"}
    base_time = _max_dt(reminder.start_at or reminder.remind_at or reminder.due_at, datetime.now(get_settings().tzinfo))
    if not base_time:
        return {"status": "error", "reply_text": "这条提醒没有可重排的时间。"}
    duration_minutes = _legacy_action_duration_minutes(reminder)
    search_from = _reschedule_search_start(mode, base_time, delay_minutes)
    slot = _find_next_free_slot(duration_minutes, search_from)
    if not slot:
        return {"status": "error", "reply_text": "未来 14 天内没有找到足够空闲的时间段。"}
    new_start, new_end = slot
    patch: dict[str, Any] = {"remind_at": new_start}
    if reminder.start_at and reminder.due_at:
        patch["start_at"] = new_start
        patch["due_at"] = new_end
    elif reminder.due_at:
        patch["due_at"] = new_start
    metadata = _reset_legacy_reminder_metadata(
        reminder.metadata,
        sender_id=sender_id,
        operation="rescheduled",
        mode=mode,
        delay_minutes=delay_minutes,
    )
    repo.update_action(reminder.id, ActionUpdate(**patch, metadata=metadata))
    await _cancel_pushover_retries("legacy_action", reminder.id)
    return {"status": "success", "reply_text": f"已重排到空闲时间：{_display_range(new_start, new_end)}。"}


async def _cancel_reminder_target(value: dict[str, Any], sender_id: str | None, feishu: Any) -> dict[str, Any]:
    target_type = value.get("target_type")
    target_id = value.get("target_id")
    if target_type and target_id:
        result = await _cancel_core_reminder_target(str(target_type), str(target_id), sender_id, feishu)
        if result.get("status") != "error":
            await _cancel_pushover_retries(str(target_type), str(target_id))
        return result
    action_id = value.get("action_id")
    if not action_id:
        return {"status": "error", "reply_text": "缺少提醒记录 ID，无法取消。"}
    repo = get_repo()
    try:
        reminder = repo.get_action(str(action_id))
    except KeyError:
        return {"status": "error", "reply_text": "提醒记录不存在或已被清理。"}
    metadata = _reset_legacy_reminder_metadata(
        reminder.metadata,
        sender_id=sender_id,
        operation="canceled",
        mode="cancel",
        delay_minutes=0,
    )
    repo.update_action(reminder.id, ActionUpdate(status=ActionStatus.canceled, metadata=metadata))
    await _cancel_pushover_retries("legacy_action", reminder.id)
    return {"status": "success", "reply_text": f"已取消安排：{reminder.title}。"}


def _ack_core_pre_strong_reminder(target_type: str, target_id: str, sender_id: str | None) -> dict[str, Any]:
    store = get_core_store()
    if target_type == "action_item":
        lookup_id = target_id
        getter = store.get_action_item
    elif target_type == "calendar_event":
        lookup_id = target_id
        getter = store.get_calendar_event
    elif target_type == "schedule_block":
        lookup_id = target_id.split(":", 1)[0]
        getter = store.get_schedule_block
    else:
        return {"status": "error", "reply_text": "不支持的提醒类型。"}
    try:
        getter(lookup_id)
    except KeyError:
        return {"status": "error", "reply_text": "提醒记录不存在或已被清理。"}
    _upsert_core_reminder(target_type, target_id, "pre_strong_card_confirmed", "done")
    _upsert_core_reminder(target_type, target_id, "strong_reminder_suppressed", "done")
    if sender_id:
        _upsert_core_reminder(target_type, target_id, f"confirmed_by:{sender_id}", "done")
    return {"status": "success", "reply_text": "已确认，本次强提醒已取消。"}


async def _reschedule_core_reminder(
    target_type: str,
    target_id: str,
    mode: str,
    delay_minutes: int,
    sender_id: str | None,
    feishu: Any,
) -> dict[str, Any]:
    store = get_core_store()
    now = datetime.now(get_settings().tzinfo)
    if target_type == "action_item":
        try:
            item = store.get_action_item(target_id)
        except KeyError:
            return {"status": "error", "reply_text": "提醒记录不存在或已被清理。"}
        if not item.due_at:
            return {"status": "error", "reply_text": "这条任务没有可重排的时间。"}
        duration_minutes = int(item.estimated_minutes or 60)
        base_time = _max_dt(item.due_at, now)
        search_from = _reschedule_search_start(mode, base_time, delay_minutes)
        slot = _find_next_free_slot(duration_minutes, search_from)
        if not slot:
            return {"status": "error", "reply_text": "未来 14 天内没有找到足够空闲的时间段。"}
        new_start, new_end = slot
        store.update_action_item(target_id, {"due_at": new_start})
        _reset_core_reminder_marks(target_type, target_id, sender_id, "rescheduled", mode, delay_minutes)
        return {"status": "success", "reply_text": f"已重排到空闲时间：{_display_range(new_start, new_end)}。"}

    if target_type == "calendar_event":
        try:
            event = store.get_calendar_event(target_id)
        except KeyError:
            return {"status": "error", "reply_text": "提醒记录不存在或已被清理。"}
        duration_minutes = max(1, int((event.end_at - event.start_at).total_seconds() // 60))
        base_time = _max_dt(event.start_at, now)
        search_from = _reschedule_search_start(mode, base_time, delay_minutes)
        slot = _find_next_free_slot(duration_minutes, search_from, exclude_calendar_event_id=target_id)
        if not slot:
            return {"status": "error", "reply_text": "未来 14 天内没有找到足够空闲的时间段。"}
        new_start_at, new_end_at = slot
        updated = store.update_calendar_event(target_id, {"start_at": new_start_at, "end_at": new_end_at})
        sync_note = await _sync_calendar_update(feishu, updated.model_dump(mode="json"))
        _reset_core_reminder_marks(target_type, target_id, sender_id, "rescheduled", mode, delay_minutes)
        return {"status": "success", "reply_text": f"已重排到空闲时间：{_display_range(new_start_at, new_end_at)}。{sync_note}"}

    if target_type == "schedule_block":
        return {"status": "error", "reply_text": "固定重复安排暂不支持从提醒卡片重排单次，请直接发送具体修改指令。"}
    return {"status": "error", "reply_text": "不支持的提醒类型。"}


async def _cancel_core_reminder_target(target_type: str, target_id: str, sender_id: str | None, feishu: Any) -> dict[str, Any]:
    store = get_core_store()
    if target_type == "action_item":
        try:
            item = store.update_action_item(target_id, {"status": "canceled"})
        except KeyError:
            return {"status": "error", "reply_text": "提醒记录不存在或已被清理。"}
        _reset_core_reminder_marks(target_type, target_id, sender_id, "canceled", "cancel", 0)
        return {"status": "success", "reply_text": f"已取消任务：{item.title}。"}

    if target_type == "calendar_event":
        try:
            event = store.update_calendar_event(target_id, {"status": "canceled"})
        except KeyError:
            return {"status": "error", "reply_text": "提醒记录不存在或已被清理。"}
        sync_note = await _sync_calendar_delete(feishu, event.model_dump(mode="json"))
        _reset_core_reminder_marks(target_type, target_id, sender_id, "canceled", "cancel", 0)
        return {"status": "success", "reply_text": f"已取消日程：{event.title}。{sync_note}"}

    if target_type == "schedule_block":
        return {"status": "error", "reply_text": "固定重复安排暂不支持从提醒卡片取消单次，请直接发送具体修改指令。"}
    return {"status": "error", "reply_text": "不支持的提醒类型。"}


def _reset_core_reminder_marks(
    target_type: str,
    target_id: str,
    sender_id: str | None,
    operation: str,
    mode: str,
    delay_minutes: int,
) -> None:
    now = utcnow_iso()
    repo = get_repo()
    with repo.connect() as conn:
        conn.execute(
            """
            UPDATE reminders
            SET status='superseded'
            WHERE target_type=? AND target_id=? AND channel IN (
                'pre_strong_card_sent',
                'pre_strong_card_confirmed',
                'strong_reminder_suppressed',
                'strong_reminder_sent'
            )
            """,
            (target_type, target_id),
        )
    _upsert_core_reminder(target_type, target_id, operation, "done")
    _upsert_core_reminder(target_type, target_id, f"{operation}_mode:{mode}", "done")
    if delay_minutes:
        _upsert_core_reminder(target_type, target_id, f"{operation}_delay_minutes:{delay_minutes}", "done")
    if sender_id:
        _upsert_core_reminder(target_type, target_id, f"{operation}_by:{sender_id}", "done")
    _upsert_core_reminder(target_type, target_id, f"{operation}_at:{now}", "done")


def _reset_legacy_reminder_metadata(
    metadata: dict[str, Any],
    *,
    sender_id: str | None,
    operation: str,
    mode: str,
    delay_minutes: int,
) -> dict[str, Any]:
    updated = {**metadata}
    for key in (
        "pre_strong_card_sent_at",
        "pre_strong_card_response",
        "pre_strong_confirmed_at",
        "pre_strong_confirmed_by",
        "strong_reminder_suppressed_at",
        "strong_reminder_suppressed_reason",
        "reminder_sent_at",
        "reminder_response",
    ):
        updated.pop(key, None)
    updated.update(
        {
            f"{operation}_at": utcnow_iso(),
            f"{operation}_by": sender_id,
            f"{operation}_mode": mode,
        }
    )
    if delay_minutes:
        updated[f"{operation}_delay_minutes"] = delay_minutes
    return updated


async def _sync_calendar_update(feishu: Any, calendar_event: dict[str, Any]) -> str:
    try:
        result = await feishu.update_calendar_event(calendar_event)
    except Exception as exc:  # noqa: BLE001 - local reschedule should remain committed
        return f" 飞书日历同步失败：{exc}"
    _store_feishu_event_id(calendar_event["id"], result)
    if result.get("status") == "failed":
        return f" 飞书日历同步失败：{result.get('error') or 'unknown'}"
    return ""


async def _sync_calendar_delete(feishu: Any, calendar_event: dict[str, Any]) -> str:
    try:
        result = await feishu.delete_calendar_event(calendar_event)
    except Exception as exc:  # noqa: BLE001
        return f" 飞书日历同步失败：{exc}"
    if result.get("status") == "failed":
        return f" 飞书日历同步失败：{result.get('error') or 'unknown'}"
    return ""


def _store_feishu_event_id(event_id: str, sync_result: dict[str, Any]) -> None:
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
        get_core_store().update_calendar_event(event_id, {"feishu_event_id": str(external_id)})


async def _cancel_pushover_retries(target_type: str, target_id: str) -> None:
    client = PushoverClient(get_settings())
    if not client.configured:
        return
    try:
        await client.cancel_emergency_by_tag(pushover_tag_for_target(target_type, target_id))
    except Exception:
        return


def _max_dt(value: datetime | None, fallback: datetime) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=fallback.tzinfo)
    return value if value > fallback else fallback


def _bounded_delay_minutes(value: Any) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return min(max(parsed, 0), 24 * 60)


def _legacy_action_duration_minutes(reminder: Any) -> int:
    if reminder.start_at and reminder.due_at:
        return max(1, int((reminder.due_at - reminder.start_at).total_seconds() // 60))
    return max(30, min(int(reminder.estimated_minutes or 60), 120))


def _reschedule_search_start(mode: str, base_time: datetime, delay_minutes: int) -> datetime:
    now = datetime.now(get_settings().tzinfo)
    start = _max_dt(base_time, now) or now
    if mode == "after_delay" and delay_minutes:
        start += timedelta(minutes=delay_minutes)
    return _round_up(start, minutes=5)


def _find_next_free_slot(
    duration_minutes: int,
    search_from: datetime,
    *,
    exclude_calendar_event_id: str | None = None,
    max_days: int = 14,
) -> tuple[datetime, datetime] | None:
    tz = get_settings().tzinfo
    search_from = search_from.astimezone(tz) if search_from.tzinfo else search_from.replace(tzinfo=tz)
    duration = timedelta(minutes=max(1, duration_minutes))
    day = search_from.replace(hour=0, minute=0, second=0, microsecond=0)
    for _ in range(max_days + 1):
        window_start = _datetime_on_day(day, "09:30")
        window_end = _datetime_on_day(day, "24:00")
        start = max(window_start, search_from) if day.date() == search_from.date() else window_start
        start = _round_up(start, minutes=5)
        if start >= window_end:
            day += timedelta(days=1)
            continue
        busy = _buffered_busy_ranges(
            _busy_ranges(start, window_end, exclude_calendar_event_id=exclude_calendar_event_id),
            start=start,
            end=window_end,
            buffer_minutes=20,
        )
        for free_range in _free_ranges(start, window_end, busy):
            candidate_start = _round_up(free_range["start"], minutes=5)
            candidate_end = candidate_start + duration
            if candidate_end <= free_range["end"]:
                return candidate_start, candidate_end
        day += timedelta(days=1)
    return None


def _busy_ranges(start: datetime, end: datetime, *, exclude_calendar_event_id: str | None = None) -> list[dict[str, Any]]:
    store = get_core_store()
    busy: list[dict[str, Any]] = []
    for event in store.list_calendar_events(start=start - timedelta(days=1), end=end + timedelta(days=1)):
        if event.id == exclude_calendar_event_id:
            continue
        if event.end_at > start and event.start_at < end:
            busy.append({"start": max(event.start_at, start), "end": min(event.end_at, end), "title": event.title})
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        for block in store.list_schedule_blocks():
            data = block.model_dump(mode="json")
            if not _schedule_block_matches_date(data, day):
                continue
            block_start = _datetime_on_day(day, str(data["start_time"]))
            block_end = _datetime_on_day(day, str(data["end_time"]))
            if block_end <= block_start:
                block_end += timedelta(days=1)
            if block_end > start and block_start < end:
                busy.append({"start": max(block_start, start), "end": min(block_end, end), "title": data["title"]})
        day += timedelta(days=1)
    return sorted(busy, key=lambda item: item["start"])


def _buffered_busy_ranges(
    busy: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
    buffer_minutes: int,
) -> list[dict[str, Any]]:
    if buffer_minutes <= 0:
        return busy
    buffer = timedelta(minutes=buffer_minutes)
    return [{**item, "start": max(start, item["start"] - buffer), "end": min(end, item["end"] + buffer)} for item in busy]


def _free_ranges(start: datetime, end: datetime, busy: list[dict[str, Any]]) -> list[dict[str, datetime]]:
    free: list[dict[str, datetime]] = []
    cursor = start
    for item in _merge_ranges(busy):
        if cursor < item["start"]:
            free.append({"start": cursor, "end": item["start"]})
        cursor = max(cursor, item["end"])
    if cursor < end:
        free.append({"start": cursor, "end": end})
    return free


def _merge_ranges(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in sorted(ranges, key=lambda value: value["start"]):
        if not merged or item["start"] > merged[-1]["end"]:
            merged.append(dict(item))
        else:
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
    return merged


def _datetime_on_day(day: datetime, time_value: str) -> datetime:
    if time_value == "24:00":
        return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    hour, minute = time_value.split(":", 1)
    return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)


def _schedule_block_matches_date(block: dict[str, Any], day: datetime) -> bool:
    byday = _rrule_days(str(block.get("recurrence_rule") or ""))
    day_code = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][day.weekday()]
    return not byday or day_code in byday


def _rrule_days(rrule: str) -> set[str]:
    for part in rrule.split(";"):
        if part.startswith("BYDAY="):
            return {item.strip() for item in part.removeprefix("BYDAY=").split(",") if item.strip()}
    return set()


def _round_up(value: datetime, *, minutes: int) -> datetime:
    discard = timedelta(minutes=value.minute % minutes, seconds=value.second, microseconds=value.microsecond)
    rounded = value - discard
    if discard:
        rounded += timedelta(minutes=minutes)
    return rounded


def _format_minutes(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60}小时"
    return f"{minutes}分钟"


def _display_dt(value: datetime) -> str:
    local = value.astimezone(get_settings().tzinfo)
    now = datetime.now(get_settings().tzinfo)
    if local.date() == now.date():
        return f"今天 {local:%H:%M}"
    if local.date() == (now + timedelta(days=1)).date():
        return f"明天 {local:%H:%M}"
    return local.strftime("%m-%d %H:%M")


def _display_range(start: datetime, end: datetime) -> str:
    start_local = start.astimezone(get_settings().tzinfo)
    end_local = end.astimezone(get_settings().tzinfo)
    if start_local.date() == end_local.date():
        return f"{_display_dt(start_local)}-{end_local:%H:%M}"
    return f"{_display_dt(start_local)} - {_display_dt(end_local)}"


def _upsert_core_reminder(target_type: str, target_id: str, channel: str, status: str) -> None:
    now = utcnow_iso()
    repo = get_repo()
    with repo.connect() as conn:
        existing = conn.execute(
            """
            SELECT id FROM reminders
            WHERE target_type=? AND target_id=? AND channel=?
            LIMIT 1
            """,
            (target_type, target_id, channel),
        ).fetchone()
        if existing:
            conn.execute("UPDATE reminders SET status=? WHERE id=?", (status, existing["id"]))
            return
        conn.execute(
            """
            INSERT INTO reminders (id, target_type, target_id, remind_at, channel, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("rem"), target_type, target_id, now, channel, status, now),
        )


def _extract_message(message: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    message_type = message.get("message_type") or "unknown"
    content = message.get("content") or ""
    parsed = _parse_content(content)
    if message_type == "text":
        text = parsed.get("text") if isinstance(parsed, dict) else str(parsed)
        return (text or "").strip(), "text", []
    if message_type in {"image", "file", "audio"}:
        attachment = {"kind": message_type, "message_id": message.get("message_id"), "raw": parsed}
        if isinstance(parsed, dict):
            attachment.update({key: value for key, value in parsed.items() if isinstance(value, str)})
        return f"[{message_type} attachment]", message_type, [attachment]
    return json.dumps(parsed, ensure_ascii=False), message_type, [{"kind": message_type, "raw": parsed}]


async def _hydrate_message_attachments(
    feishu: Any,
    message_id: str | None,
    attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not attachments:
        return []
    hydrated: list[dict[str, Any]] = []
    for attachment in attachments:
        item = dict(attachment)
        kind = str(item.get("kind") or "")
        file_key = _attachment_resource_key(item)
        if not message_id or kind not in {"image", "file", "audio"} or not file_key:
            hydrated.append(item)
            continue
        resource_type = "image" if kind == "image" else "file"
        result = await feishu.download_message_resource(str(message_id), file_key, resource_type)
        if result.get("status") != "downloaded":
            item["download_status"] = "failed"
            if result.get("error"):
                item["download_error"] = str(result["error"])[:300]
            hydrated.append(item)
            continue
        content = result.get("content")
        if not isinstance(content, bytes | bytearray):
            item["download_status"] = "failed"
            item["download_error"] = "downloaded resource did not contain bytes"
            hydrated.append(item)
            continue
        mime_type = str(result.get("content_type") or _default_mime_for_kind(kind))
        local_path = _save_attachment_bytes(
            message_id=str(message_id),
            file_key=file_key,
            content=bytes(content),
            mime_type=mime_type,
        )
        item.update(
            {
                "download_status": "downloaded",
                "local_path": str(local_path),
                "mime_type": mime_type,
                "size_bytes": len(content),
            }
        )
        if result.get("filename"):
            item["filename"] = str(result["filename"])
        hydrated.append(item)
    return hydrated


def _attachment_resource_key(attachment: dict[str, Any]) -> str | None:
    for key in ("image_key", "file_key"):
        value = attachment.get(key)
        if isinstance(value, str) and value:
            return value
    raw = attachment.get("raw")
    if isinstance(raw, dict):
        for key in ("image_key", "file_key"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _save_attachment_bytes(*, message_id: str, file_key: str, content: bytes, mime_type: str) -> Path:
    settings = get_settings()
    root = Path(settings.attachment_storage_dir)
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()[:16]
    safe_message_id = _safe_filename_token(message_id)
    safe_file_key = _safe_filename_token(file_key)
    suffix = _suffix_for_mime(mime_type)
    path = root / f"{safe_message_id}_{safe_file_key}_{digest}{suffix}"
    if not path.exists():
        path.write_bytes(content)
    return path


def _safe_filename_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:96] or "resource"


def _default_mime_for_kind(kind: str) -> str:
    return "image/png" if kind == "image" else "application/octet-stream"


def _suffix_for_mime(mime_type: str) -> str:
    normalized = mime_type.split(";", 1)[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
    }.get(normalized, ".bin")


def _parse_content(content: Any) -> Any:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content:
        return {}
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"text": content}


def _message_mentions_bot(message: dict[str, Any]) -> bool:
    mentions = message.get("mentions") or []
    if mentions:
        return True
    parsed = _parse_content(message.get("content") or "")
    if isinstance(parsed, dict) and parsed.get("mentions"):
        return True
    text = str(parsed.get("text") or "") if isinstance(parsed, dict) else str(parsed)
    return "@_user_" in text or "@机器人" in text
