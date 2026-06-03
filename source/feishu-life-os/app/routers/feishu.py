from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from app.agents.models import AgentMessageType
from app.agents.orchestrator import AgentOrchestrator
from app.config import get_settings
from app.dependencies import get_agent_orchestrator

router = APIRouter(prefix="/api/feishu", tags=["feishu"])


@router.post("/events")
async def feishu_events(
    request: Request,
    background_tasks: BackgroundTasks,
    orchestrator: AgentOrchestrator = Depends(get_agent_orchestrator),
) -> dict[str, Any]:
    payload = await request.json()
    _verify_event_token(payload)

    if payload.get("type") == "url_verification" and "challenge" in payload:
        return {"challenge": payload["challenge"]}

    header = payload.get("header", {}) if isinstance(payload, dict) else {}
    event = payload.get("event", {}) if isinstance(payload, dict) else {}
    event_type = header.get("event_type") or payload.get("type") or "unknown"
    if event_type != "im.message.receive_v1":
        return {"ok": True, "ignored": True, "event_type": event_type, "reason": "unsupported_event"}

    message = event.get("message", {}) if isinstance(event, dict) else {}
    if message.get("chat_type") != "p2p":
        return {"ok": True, "ignored": True, "event_type": event_type, "reason": "not_private_chat"}

    message_id = message.get("message_id") or header.get("event_id")
    raw_text, message_type, attachments = _extract_message_content(message)
    open_id = _extract_open_id(event)
    if not _is_authorized_feishu_user(open_id):
        return {"ok": True, "ignored": True, "event_type": event_type, "reason": "unauthorized_sender"}
    queued = await orchestrator.enqueue_feishu_message(
        raw_text=raw_text,
        message_type=message_type,
        open_id=open_id,
        message_id=message_id,
        attachments=attachments,
        raw_event=payload,
    )
    agent_request = queued.pop("_agent_request", None)
    if agent_request:
        background_tasks.add_task(
            orchestrator.process_agent_run,
            agent_run_id=queued["agent_run_id"],
            capture_id=queued["capture_id"],
            open_id=open_id,
            request=agent_request,
        )
    return queued


def _verify_event_token(payload: dict[str, Any]) -> None:
    settings = get_settings()
    expected = settings.feishu_event_verification_token
    if not expected:
        return
    token = payload.get("token") or payload.get("header", {}).get("token")
    if token != expected:
        raise HTTPException(status_code=403, detail="invalid Feishu event verification token")


def _is_authorized_feishu_user(open_id: str | None) -> bool:
    settings = get_settings()
    configured = settings.feishu_allowed_open_ids or settings.feishu_default_assignee_open_id
    if not configured:
        return True
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    return bool(open_id and open_id in allowed)


def _extract_open_id(event: dict[str, Any]) -> str | None:
    sender = event.get("sender", {}) if isinstance(event, dict) else {}
    sender_id = sender.get("sender_id", {}) if isinstance(sender, dict) else {}
    if isinstance(sender_id, dict):
        return sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id")
    return None


def _extract_message_content(
    message: dict[str, Any],
) -> tuple[str, AgentMessageType, list[dict[str, Any]]]:
    message_type_text = message.get("message_type") or "unknown"
    content = message.get("content") or ""
    parsed = _parse_content(content)
    if message_type_text == "text":
        text = parsed.get("text") if isinstance(parsed, dict) else str(parsed)
        return (text or "").strip(), AgentMessageType.text, []
    if message_type_text == "post":
        return _flatten_post(parsed), AgentMessageType.forwarded, [
            {"kind": "post", "text_hint": json.dumps(parsed, ensure_ascii=False)}
        ]
    if message_type_text in {"image", "file", "audio"}:
        attachment = {"kind": message_type_text, "text_hint": json.dumps(parsed, ensure_ascii=False)}
        if isinstance(parsed, dict):
            attachment.update({key: value for key, value in parsed.items() if isinstance(value, str)})
        mapped = AgentMessageType.image if message_type_text == "image" else AgentMessageType.file
        return f"[{message_type_text} attachment]", mapped, [attachment]
    return f"[unsupported {message_type_text} message]", AgentMessageType.unknown, [
        {"kind": message_type_text, "text_hint": json.dumps(parsed, ensure_ascii=False)}
    ]


def _parse_content(content: Any) -> Any:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content:
        return {}
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"text": content}


def _flatten_post(parsed: Any) -> str:
    if not isinstance(parsed, dict):
        return str(parsed)
    parts: list[str] = []
    for block in parsed.get("content", []):
        if not isinstance(block, list):
            continue
        for item in block:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
    return "\n".join(parts).strip() or "[post message]"
