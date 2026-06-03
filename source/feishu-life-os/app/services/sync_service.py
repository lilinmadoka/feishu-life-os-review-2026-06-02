from __future__ import annotations

from typing import Any

from app.adapters.feishu_client import FeishuClient
from app.database import Repository
from app.models import (
    ActionIntent,
    ActionRecord,
    ActionUpdate,
    CaptureRecord,
    SyncEvent,
    SyncTarget,
)


class SyncService:
    def __init__(self, repo: Repository, feishu: FeishuClient, sync_mode: str = "dry_run"):
        self.repo = repo
        self.feishu = feishu
        self.sync_mode = sync_mode

    async def sync_capture(self, capture: CaptureRecord) -> list[SyncEvent]:
        events: list[SyncEvent] = []
        if self._enabled("bitable"):
            payload = self.feishu.to_capture_record(capture)
            table_id = self.feishu.settings.feishu_bitable_capture_table_id
            if not table_id:
                events.append(self._skipped(SyncTarget.bitable, "capture", capture.id, payload, "capture table id missing"))
            else:
                events.append(
                    await self._execute_or_dry_run(
                        SyncTarget.bitable,
                        "capture",
                        capture.id,
                        payload,
                        lambda: self.feishu.bitable_batch_create_records(table_id, [payload]),
                    )
                )
        return events

    async def sync_action(self, action: ActionRecord) -> list[SyncEvent]:
        events: list[SyncEvent] = []
        if self._enabled("bitable"):
            events.append(await self.sync_action_target(action, SyncTarget.bitable))
        if self._enabled("task") and action.intent != ActionIntent.event:
            events.append(await self.sync_action_target(action, SyncTarget.task))
        if self._enabled("calendar") and action.intent == ActionIntent.event:
            events.append(await self.sync_action_target(action, SyncTarget.calendar))
        return events

    async def sync_action_target(self, action: ActionRecord, target: SyncTarget) -> SyncEvent:
        if target == SyncTarget.bitable:
            payload = self.feishu.to_action_record(action)
            table_id = self.feishu.settings.feishu_bitable_action_table_id
            if not table_id:
                return self._skipped(SyncTarget.bitable, "action", action.id, payload, "action table id missing")
            event = await self._execute_or_dry_run(
                SyncTarget.bitable,
                "action",
                action.id,
                payload,
                lambda: self.feishu.bitable_batch_create_records(table_id, [payload]),
            )
            record_id = self._first_bitable_record_id(event.response_payload)
            if event.status == "success" and record_id:
                self.repo.update_action(action.id, ActionUpdate(feishu_record_id=record_id))
            return event
        if target == SyncTarget.task:
            payload = self.feishu.to_task_payload(action)
            event = await self._execute_or_dry_run(
                SyncTarget.task,
                "action",
                action.id,
                payload,
                lambda: self.feishu.create_task(action),
            )
            task_guid = self._task_guid(event.response_payload)
            if event.status == "success" and task_guid:
                self.repo.update_action(action.id, ActionUpdate(feishu_task_guid=task_guid))
            return event
        if target == SyncTarget.calendar:
            payload = self.feishu.to_calendar_payload(action)
            event = await self._execute_or_dry_run(
                SyncTarget.calendar,
                "action",
                action.id,
                payload,
                lambda: self.feishu.create_calendar_event(action),
            )
            if event.status == "success":
                self.repo.update_action(
                    action.id,
                    ActionUpdate(
                        metadata={
                            **action.metadata,
                            "feishu_calendar_event": event.response_payload,
                        }
                    ),
                )
            return event
        raise ValueError(f"unsupported sync target: {target}")

    async def send_review(self, markdown: str) -> SyncEvent:
        payload = {"text": markdown}
        return await self._execute_or_dry_run(
            SyncTarget.webhook,
            "review",
            "daily",
            payload,
            lambda: self.feishu.send_webhook_text(markdown),
        )

    def _enabled(self, target: str) -> bool:
        mode = (self.sync_mode or "dry_run").lower()
        return mode in {target, "all"}

    def _skipped(self, target, entity_type, entity_id, payload, error: str):
        return self.repo.create_sync_event(
            target=target,
            entity_type=entity_type,
            entity_id=entity_id,
            status="skipped",
            request_payload=payload,
            error=error,
        )

    async def _execute_or_dry_run(self, target, entity_type, entity_id, payload, call):
        mode = (self.sync_mode or "dry_run").lower()
        if mode == "dry_run":
            return self.repo.create_sync_event(
                target=target,
                entity_type=entity_type,
                entity_id=entity_id,
                status="dry_run",
                request_payload=payload,
            )
        if call is None:
            return self.repo.create_sync_event(
                target=target,
                entity_type=entity_type,
                entity_id=entity_id,
                status="skipped",
                request_payload=payload,
                error="target table or config missing",
            )
        try:
            result = call()
            if hasattr(result, "__await__"):
                result = await result
            return self.repo.create_sync_event(
                target=target,
                entity_type=entity_type,
                entity_id=entity_id,
                status="success",
                request_payload=payload,
                response_payload=result if isinstance(result, dict) else {"result": str(result)},
            )
        except Exception as exc:  # noqa: BLE001 - stored for runbook visibility
            event = self.repo.create_sync_event(
                target=target,
                entity_type=entity_type,
                entity_id=entity_id,
                status="error",
                request_payload=payload,
                error=str(exc),
            )
            self.repo.create_sync_error_review_job(
                event,
                self._sync_error_prompt(event, payload, str(exc)),
            )
            return event

    def _first_bitable_record_id(self, response_payload: dict[str, Any]) -> str | None:
        records = response_payload.get("data", {}).get("records", [])
        if not records:
            return None
        first = records[0]
        if not isinstance(first, dict):
            return None
        return first.get("record_id") or first.get("id")

    def _task_guid(self, response_payload: dict[str, Any]) -> str | None:
        data = response_payload.get("data", {})
        task = data.get("task") if isinstance(data, dict) else None
        if isinstance(task, dict):
            return task.get("guid") or task.get("id")
        return data.get("guid") or data.get("task_guid") or data.get("id") if isinstance(data, dict) else None

    def _sync_error_prompt(self, event: SyncEvent, payload: dict[str, Any], error: str) -> str:
        return (
            "你是这个飞书个人任务管理系统的运行审核员。请分析一次同步失败，"
            "判断是配置、权限、字段 schema、网络还是代码问题。只输出符合 schema 的 JSON。\n\n"
            f"target: {event.target.value}\n"
            f"entity_type: {event.entity_type}\n"
            f"entity_id: {event.entity_id}\n"
            f"request_payload: {payload}\n"
            f"error: {error}\n"
        )
