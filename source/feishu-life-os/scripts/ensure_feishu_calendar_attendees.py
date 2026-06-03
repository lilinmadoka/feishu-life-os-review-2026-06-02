from __future__ import annotations

import argparse
import asyncio

from app.adapters.feishu_client import FeishuClient
from app.config import get_settings
from app.core.store import StateStore
from app.database import Repository


async def main() -> None:
    parser = argparse.ArgumentParser(description="Ensure synced Feishu Calendar events are visible to configured attendees.")
    parser.add_argument("--dry-run", action="store_true", help="Only list events that would be checked.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum events to process.")
    parser.add_argument("--title", default="", help="Optional title substring filter.")
    parser.add_argument("--kind", choices=["all", "events", "schedule-blocks"], default="all")
    args = parser.parse_args()

    settings = get_settings()
    store = StateStore(Repository(settings.database_path, database_url=settings.database_url))
    store.migrate()
    client = FeishuClient(settings)
    attendee_ids = client.calendar_attendee_open_ids()
    if not attendee_ids:
        print("no_attendee_open_ids_configured")
        return

    items: list[tuple[str, str, str]] = []
    if args.kind in {"all", "events"}:
        for event in store.list_calendar_events():
            if event.feishu_event_id and (not args.title or args.title in event.title):
                items.append(("event", event.feishu_event_id, event.title))
    if args.kind in {"all", "schedule-blocks"}:
        for block in store.list_schedule_blocks():
            if block.feishu_event_id and (not args.title or args.title in block.title):
                items.append(("schedule_block", block.feishu_event_id, block.title))
    items = items[: max(args.limit, 0)]

    print(f"calendar_items={len(items)} attendee_open_ids={','.join(attendee_ids)} dry_run={args.dry_run}")
    for kind, event_id, title in items:
        print(f"- {kind} {event_id} {title}")
        if args.dry_run:
            continue
        try:
            result = await client.ensure_core_calendar_attendees(event_id, attendee_ids)
        except Exception as exc:  # noqa: BLE001
            print(f"  failed error={exc}")
            continue
        status = result.get("status")
        added = ",".join(result.get("added_open_ids") or [])
        suffix = f" added={added}" if added else ""
        print(f"  attendee_sync={status}{suffix}")


if __name__ == "__main__":
    asyncio.run(main())
