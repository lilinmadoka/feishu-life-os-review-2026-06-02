from __future__ import annotations

from typing import Any

from app.core.observability.emitter import TraceEmitter


class ObservedFeishuNativeAdapter:
    def __init__(self, wrapped: Any, trace: TraceEmitter):
        self._wrapped = wrapped
        self._trace = trace

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    async def send_text(self, receive_id: str | None, text: str) -> dict[str, Any]:
        return await self._call("send_text", {"target": receive_id, "text_length": len(text or "")}, receive_id, text)

    async def send_card(self, receive_id: str | None, card: dict[str, Any]) -> dict[str, Any]:
        return await self._call(
            "send_card",
            {"target": receive_id, "card_keys": sorted(card.keys()) if isinstance(card, dict) else []},
            receive_id,
            card,
        )

    async def sync_task(self, action_item: dict[str, Any]) -> dict[str, Any]:
        return await self._call("sync_task", _entity_attrs("action_item", action_item), action_item)

    async def sync_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        return await self._call("sync_calendar_event", _entity_attrs("calendar_event", calendar_event), calendar_event)

    async def sync_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        return await self._call("sync_schedule_block", _entity_attrs("schedule_block", schedule_block), schedule_block)

    async def update_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        return await self._call("update_calendar_event", _entity_attrs("calendar_event", calendar_event), calendar_event)

    async def update_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        return await self._call("update_schedule_block", _entity_attrs("schedule_block", schedule_block), schedule_block)

    async def delete_calendar_event(self, calendar_event: dict[str, Any]) -> dict[str, Any]:
        return await self._call("delete_calendar_event", _entity_attrs("calendar_event", calendar_event), calendar_event)

    async def delete_schedule_block(self, schedule_block: dict[str, Any]) -> dict[str, Any]:
        return await self._call("delete_schedule_block", _entity_attrs("schedule_block", schedule_block), schedule_block)

    async def sync_bitable_audit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._call(
            "sync_bitable_audit",
            {"payload_keys": sorted(payload.keys()) if isinstance(payload, dict) else []},
            payload,
        )

    async def download_message_resource(self, message_id: str, file_key: str, resource_type: str) -> dict[str, Any]:
        return await self._call(
            "download_message_resource",
            {"message_id": message_id, "file_key": file_key, "resource_type": resource_type},
            message_id,
            file_key,
            resource_type,
        )

    async def _call(self, method_name: str, attrs: dict[str, Any], *args: Any) -> dict[str, Any]:
        method = getattr(self._wrapped, method_name)
        with self._trace.span(
            None,
            f"feishu.{method_name}",
            component="feishu",
            lane="external",
            attrs={"operation": method_name, **attrs},
        ) as span:
            try:
                result = await method(*args)
            except Exception as exc:
                span.add_attrs({"status": "raised", "error_class": exc.__class__.__name__})
                raise
            span.add_attrs(_result_attrs(result))
            return result


def _entity_attrs(entity_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": payload.get("id") if isinstance(payload, dict) else None,
        "title": payload.get("title") if isinstance(payload, dict) else None,
    }


def _result_attrs(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"status": "unknown", "result_type": type(result).__name__}
    status = result.get("status") or ("ok" if result.get("ok") else None)
    return {
        "status": status or "unknown",
        "target": result.get("target"),
        "event_id": result.get("event_id") or result.get("deleted_event_id"),
        "task_guid": result.get("task_guid"),
        "card_id": result.get("card_id"),
        "error_class": type(result.get("error")).__name__ if result.get("error") and not isinstance(result.get("error"), str) else None,
        "has_error": bool(result.get("error")),
    }
