from __future__ import annotations

import argparse
import asyncio
from typing import Any

from app.adapters.feishu_client import FeishuClient
from app.config import get_settings
from app.core.feishu_native import FeishuOpenApiNativeAdapter
from app.core.store import StateStore
from app.database import Repository


def _extract_external_event_id(sync_result: dict[str, Any]) -> str | None:
    external_id = sync_result.get("event_id")
    response = sync_result.get("response")
    if external_id or not isinstance(response, dict):
        return str(external_id) if external_id else None
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    event = data.get("event")
    if isinstance(event, dict):
        external_id = event.get("event_id")
    external_id = external_id or data.get("event_id")
    return str(external_id) if external_id else None


def _sync_error(sync_result: dict[str, Any]) -> str | None:
    if sync_result.get("status") == "failed":
        return str(sync_result.get("error") or "unknown")
    return None


async def main() -> None:
    parser = argparse.ArgumentParser(description="Sync local unsynced calendar items to Feishu Calendar.")
    parser.add_argument("--dry-run", action="store_true", help="Only list events that would be synced.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum events to process.")
    parser.add_argument("--title", default="", help="Optional title substring filter.")
    parser.add_argument(
        "--kind",
        choices=["all", "events", "schedule-blocks"],
        default="all",
        help="Calendar item kind to sync.",
    )
    args = parser.parse_args()

    settings = get_settings()
    store = StateStore(Repository(settings.database_path, database_url=settings.database_url))
    store.migrate()
    adapter = FeishuOpenApiNativeAdapter(FeishuClient(settings))

    remaining = max(args.limit, 0)

    if args.kind in {"all", "events"} and remaining > 0:
        events = [
            event
            for event in store.list_calendar_events()
            if not event.feishu_event_id and event.status.value != "canceled"
        ]
        if args.title:
            events = [event for event in events if args.title in event.title]
        events = events[:remaining]
        remaining -= len(events)

        print(f"unsynced_calendar_events={len(events)} dry_run={args.dry_run}")
        for event in events:
            print(f"- event {event.id} {event.start_at.isoformat()}-{event.end_at.strftime('%H:%M')} {event.title}")
            if args.dry_run:
                continue
            result = await adapter.sync_calendar_event(event.model_dump(mode="json"))
            error = _sync_error(result)
            if error:
                print(f"  failed error={error}")
                continue
            external_id = _extract_external_event_id(result)
            if external_id:
                store.update_calendar_event(event.id, {"feishu_event_id": external_id})
                print(f"  synced feishu_event_id={external_id}")
            else:
                print(f"  failed status={result.get('status')} no event_id in response")

    if args.kind in {"all", "schedule-blocks"} and remaining > 0:
        blocks = [
            block
            for block in store.list_schedule_blocks()
            if not block.feishu_event_id and block.status.value != "canceled"
        ]
        if args.title:
            blocks = [block for block in blocks if args.title in block.title]
        blocks = blocks[:remaining]

        print(f"unsynced_schedule_blocks={len(blocks)} dry_run={args.dry_run}")
        for block in blocks:
            print(f"- schedule_block {block.id} {block.recurrence_rule} {block.start_time}-{block.end_time} {block.title}")
            if args.dry_run:
                continue
            result = await adapter.sync_schedule_block(block.model_dump(mode="json"))
            error = _sync_error(result)
            if error:
                print(f"  failed error={error}")
                continue
            external_id = _extract_external_event_id(result)
            if external_id:
                store.update_schedule_block(block.id, {"feishu_event_id": external_id})
                print(f"  synced feishu_event_id={external_id}")
            else:
                print(f"  failed status={result.get('status')} no event_id in response")


if __name__ == "__main__":
    asyncio.run(main())
