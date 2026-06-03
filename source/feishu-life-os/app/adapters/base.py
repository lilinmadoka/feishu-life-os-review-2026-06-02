from __future__ import annotations

from typing import Any, Protocol

from app.models import ActionRecord, CaptureRecord


class Notifier(Protocol):
    async def send_text(self, text: str) -> dict[str, Any]: ...


class TaskSink(Protocol):
    async def create_task(self, action: ActionRecord) -> dict[str, Any]: ...


class CalendarSink(Protocol):
    async def create_event(self, action: ActionRecord) -> dict[str, Any]: ...


class RecordSink(Protocol):
    async def upsert_capture(self, capture: CaptureRecord) -> dict[str, Any]: ...

    async def upsert_action(self, action: ActionRecord) -> dict[str, Any]: ...
