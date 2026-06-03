from __future__ import annotations

import argparse
import asyncio

from app.adapters.feishu_client import FeishuClient
from app.config import get_settings
from app.core.store import StateStore
from app.database import Repository


async def main() -> None:
    parser = argparse.ArgumentParser(description="Update Feishu Calendar event colors from local item types.")
    parser.add_argument("--dry-run", action="store_true", help="Only list events that would be updated.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum events to process.")
    parser.add_argument("--title", default="", help="Optional title substring filter.")
    parser.add_argument("--kind", choices=["all", "events", "schedule-blocks"], default="all")
    args = parser.parse_args()

    settings = get_settings()
    store = StateStore(Repository(settings.database_path, database_url=settings.database_url))
    store.migrate()
    client = FeishuClient(settings)

    items: list[tuple[str, str, str, dict]] = []
    if args.kind in {"all", "events"}:
        for event in store.list_calendar_events():
            if event.feishu_event_id and (not args.title or args.title in event.title):
                payload = event.model_dump(mode="json")
                items.append(("event", event.feishu_event_id, event.title, payload))
    if args.kind in {"all", "schedule-blocks"}:
        for block in store.list_schedule_blocks():
            if block.feishu_event_id and (not args.title or args.title in block.title):
                payload = block.model_dump(mode="json")
                items.append(("schedule_block", block.feishu_event_id, block.title, payload))
    items = items[: max(args.limit, 0)]

    print(f"calendar_items={len(items)} dry_run={args.dry_run}")
    for kind, event_id, title, payload in items:
        color = (
            client.to_core_schedule_block_payload(payload)["color"]
            if kind == "schedule_block"
            else client.to_core_calendar_payload(payload)["color"]
        )
        print(f"- {kind} {event_id} color={color} {title}")
        if args.dry_run:
            continue
        try:
            if kind == "schedule_block":
                result = await client.update_core_schedule_block_event(payload)
            else:
                result = await client.update_core_calendar_event(payload)
        except Exception as exc:  # noqa: BLE001
            print(f"  failed error={exc}")
            continue
        print(f"  updated code={result.get('code')} msg={result.get('msg')}")


if __name__ == "__main__":
    asyncio.run(main())
