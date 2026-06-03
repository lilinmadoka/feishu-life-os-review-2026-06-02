from __future__ import annotations

from typing import Any, Protocol

from app.adapters.feishu_client import FeishuClient


class FeishuNativeAdapter(Protocol):
    async def send_text(self, receive_id: str | None, text: str) -> dict[str, Any]:
        ...

    async def send_card(self, receive_id: str | None, card: dict[str, Any]) -> dict[str, Any]:
        ...

    async def sync_task(self, action_item: dict[str, Any]) -> dict[str, Any]:
        ...

    async def sync_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        ...

    async def sync_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        ...

    async def update_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        ...

    async def update_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        ...

    async def delete_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        ...

    async def delete_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        ...

    async def sync_bitable_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    async def download_message_resource(self, message_id: str, file_key: str, resource_type: str) -> dict[str, Any]:
        ...


class MockFeishuNativeAdapter:
    def __init__(self):
        self.sent_texts: list[dict[str, Any]] = []
        self.sent_cards: list[dict[str, Any]] = []
        self.synced_tasks: list[dict[str, Any]] = []
        self.synced_calendar_events: list[dict[str, Any]] = []
        self.synced_audits: list[dict[str, Any]] = []

    async def send_text(self, receive_id: str | None, text: str) -> dict[str, Any]:
        payload = {"receive_id": receive_id, "text": text, "mock": True}
        self.sent_texts.append(payload)
        return payload

    async def send_card(self, receive_id: str | None, card: dict[str, Any]) -> dict[str, Any]:
        payload = {"receive_id": receive_id, "card": card, "card_id": f"mock_card_{len(self.sent_cards) + 1}", "mock": True}
        self.sent_cards.append(payload)
        return payload

    async def sync_task(self, action_item: dict[str, Any]) -> dict[str, Any]:
        payload = {"status": "staged", "task_guid": f"mock_task_{action_item['id']}", "action_item": action_item, "mock": True}
        self.synced_tasks.append(payload)
        return payload

    async def sync_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        payload = {"status": "staged", "event_id": f"mock_event_{calendar_event['id']}", "calendar_event": calendar_event, "mock": True}
        self.synced_calendar_events.append(payload)
        return payload

    async def sync_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "status": "staged",
            "event_id": f"mock_schedule_event_{schedule_block['id']}",
            "schedule_block": schedule_block,
            "mock": True,
        }
        self.synced_calendar_events.append(payload)
        return payload

    async def update_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "status": "staged",
            "operation": "update",
            "event_id": calendar_event.get("feishu_event_id") or f"mock_event_{calendar_event['id']}",
            "calendar_event": calendar_event,
            "mock": True,
        }
        self.synced_calendar_events.append(payload)
        return payload

    async def update_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "status": "staged",
            "operation": "update_schedule_block",
            "event_id": schedule_block.get("feishu_event_id") or f"mock_schedule_event_{schedule_block['id']}",
            "schedule_block": schedule_block,
            "mock": True,
        }
        self.synced_calendar_events.append(payload)
        return payload

    async def delete_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "status": "staged",
            "operation": "delete",
            "deleted_event_id": calendar_event.get("feishu_event_id"),
            "calendar_event": calendar_event,
            "mock": True,
        }
        self.synced_calendar_events.append(payload)
        return payload

    async def delete_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "status": "staged",
            "operation": "delete_schedule_block",
            "deleted_event_id": schedule_block.get("feishu_event_id"),
            "schedule_block": schedule_block,
            "mock": True,
        }
        self.synced_calendar_events.append(payload)
        return payload

    async def sync_bitable_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = {"status": "staged", "audit_id": f"mock_audit_{len(self.synced_audits) + 1}", "payload": payload, "mock": True}
        self.synced_audits.append(result)
        return result

    async def download_message_resource(self, message_id: str, file_key: str, resource_type: str) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "message_id": message_id,
            "file_key": file_key,
            "resource_type": resource_type,
            "error": "mock Feishu adapter has no resource bytes",
            "mock": True,
        }


def _event_id_from_response(response: dict[str, Any]) -> str | None:
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    event = data.get("event")
    if isinstance(event, dict) and event.get("event_id"):
        return str(event["event_id"])
    event_id = data.get("event_id")
    return str(event_id) if event_id else None


class FeishuOpenApiNativeAdapter:
    def __init__(self, client: FeishuClient):
        self.client = client

    async def _ensure_attendees(self, event_id: str | None) -> dict[str, Any]:
        if not event_id:
            return {"status": "skipped", "reason": "missing_event_id"}
        try:
            return await self.client.ensure_core_calendar_attendees(event_id)
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc), "event_id": event_id}

    async def send_text(self, receive_id: str | None, text: str) -> dict[str, Any]:
        if not receive_id:
            return {"ok": False, "error": "missing receive_id", "text": text}
        try:
            return {"status": "sent", "response": await self.client.send_app_text(receive_id, text)}
        except Exception as exc:  # noqa: BLE001 - Feishu failures must not break local state
            return {"status": "failed", "error": str(exc), "text": text}

    async def send_card(self, receive_id: str | None, card: dict[str, Any]) -> dict[str, Any]:
        if not receive_id:
            return {"ok": False, "error": "missing receive_id", "card": card}
        try:
            return {"status": "sent", "response": await self.client.send_interactive_card(receive_id, card)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "error": str(exc), "card": card}

    async def download_message_resource(self, message_id: str, file_key: str, resource_type: str) -> dict[str, Any]:
        try:
            data = await self.client.download_message_resource(message_id, file_key, resource_type)
            return {"status": "downloaded", "message_id": message_id, "file_key": file_key, **data}
        except Exception as exc:  # noqa: BLE001 - image download failure should not drop the message
            return {
                "status": "failed",
                "message_id": message_id,
                "file_key": file_key,
                "resource_type": resource_type,
                "error": str(exc),
            }

    async def sync_task(self, action_item: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.create_core_task(action_item)
            return {"status": "synced", "target": "feishu_task", "response": response, "action_item_id": action_item["id"]}
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "target": "feishu_task",
                "error": str(exc),
                "staged_payload": self.client.to_core_task_payload(action_item),
                "action_item_id": action_item["id"],
            }

    async def sync_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.create_core_calendar_event(calendar_event)
            event_id = _event_id_from_response(response)
            return {
                "status": "synced",
                "target": "feishu_calendar",
                "event_id": event_id,
                "response": response,
                "attendee_sync": await self._ensure_attendees(event_id),
                "calendar_event_id": calendar_event["id"],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "target": "feishu_calendar",
                "error": str(exc),
                "staged_payload": self.client.to_core_calendar_payload(calendar_event),
                "calendar_event_id": calendar_event["id"],
            }

    async def sync_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.create_core_schedule_block_event(schedule_block)
            event_id = _event_id_from_response(response)
            return {
                "status": "synced",
                "target": "feishu_calendar",
                "operation": "create_schedule_block",
                "event_id": event_id,
                "response": response,
                "attendee_sync": await self._ensure_attendees(event_id),
                "schedule_block_id": schedule_block["id"],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "target": "feishu_calendar",
                "operation": "create_schedule_block",
                "error": str(exc),
                "staged_payload": self.client.to_core_schedule_block_payload(schedule_block),
                "schedule_block_id": schedule_block["id"],
            }

    async def update_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        try:
            if calendar_event.get("feishu_event_id"):
                response = await self.client.update_core_calendar_event(calendar_event)
                event_id = str(calendar_event["feishu_event_id"])
                return {
                    "status": "synced",
                    "target": "feishu_calendar",
                    "operation": "update",
                    "event_id": event_id,
                    "response": response,
                    "attendee_sync": await self._ensure_attendees(event_id),
                    "calendar_event_id": calendar_event["id"],
                }
            response = await self.client.create_core_calendar_event(calendar_event)
            event_id = _event_id_from_response(response)
            return {
                "status": "synced",
                "target": "feishu_calendar",
                "operation": "create",
                "event_id": event_id,
                "response": response,
                "attendee_sync": await self._ensure_attendees(event_id),
                "calendar_event_id": calendar_event["id"],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "target": "feishu_calendar",
                "operation": "update",
                "error": str(exc),
                "staged_payload": self.client.to_core_calendar_payload(calendar_event),
                "calendar_event_id": calendar_event["id"],
            }

    async def update_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        try:
            if schedule_block.get("feishu_event_id"):
                response = await self.client.update_core_schedule_block_event(schedule_block)
                event_id = str(schedule_block["feishu_event_id"])
                return {
                    "status": "synced",
                    "target": "feishu_calendar",
                    "operation": "update_schedule_block",
                    "event_id": event_id,
                    "response": response,
                    "attendee_sync": await self._ensure_attendees(event_id),
                    "schedule_block_id": schedule_block["id"],
                }
            response = await self.client.create_core_schedule_block_event(schedule_block)
            event_id = _event_id_from_response(response)
            return {
                "status": "synced",
                "target": "feishu_calendar",
                "operation": "create_schedule_block",
                "event_id": event_id,
                "response": response,
                "attendee_sync": await self._ensure_attendees(event_id),
                "schedule_block_id": schedule_block["id"],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "target": "feishu_calendar",
                "operation": "update_schedule_block",
                "error": str(exc),
                "staged_payload": self.client.to_core_schedule_block_payload(schedule_block),
                "schedule_block_id": schedule_block["id"],
            }

    async def delete_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.delete_core_calendar_event(calendar_event)
            return {
                "status": "synced",
                "target": "feishu_calendar",
                "operation": "delete",
                "response": response,
                "calendar_event_id": calendar_event["id"],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "target": "feishu_calendar",
                "operation": "delete",
                "error": str(exc),
                "calendar_event_id": calendar_event["id"],
            }

    async def delete_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.delete_core_schedule_block_event(schedule_block)
            return {
                "status": "synced",
                "target": "feishu_calendar",
                "operation": "delete_schedule_block",
                "response": response,
                "schedule_block_id": schedule_block["id"],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "failed",
                "target": "feishu_calendar",
                "operation": "delete_schedule_block",
                "error": str(exc),
                "schedule_block_id": schedule_block["id"],
            }

    async def sync_bitable_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"status": "staged", "target": "bitable_audit", "payload": payload}


def confirmation_card(prompt: str, confirmation_id: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_lines = []
    for index, candidate in enumerate(candidates, start=1):
        conflict_hint = ""
        if candidate.get("conflicts"):
            conflict_hint = f"（冲突 {len(candidate['conflicts'])} 项）"
        candidate_lines.append(f"{index}. **{candidate['type']}**：{candidate['title']}{conflict_hint}")
        for detail in candidate.get("details", [])[:20]:
            candidate_lines.append(f"   - {detail}")
    body = prompt
    if candidate_lines:
        body = f"{prompt}\n\n" + "\n".join(candidate_lines)
    confirm_value = {"action": "confirm", "confirmation_id": confirmation_id}
    cancel_value = {"action": "cancel", "confirmation_id": confirmation_id}
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "私人助理确认"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "name": f"confirm_{confirmation_id}",
                        "text": {"tag": "plain_text", "content": "确认"},
                        "type": "primary",
                        "value": confirm_value,
                        "behaviors": [{"type": "callback", "value": confirm_value}],
                    },
                    {
                        "tag": "button",
                        "name": f"cancel_{confirmation_id}",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "type": "default",
                        "value": cancel_value,
                        "behaviors": [{"type": "callback", "value": cancel_value}],
                    },
                ],
            },
        ],
        "_mvp_meta": {"confirmation_id": confirmation_id, "candidates": candidates},
    }
