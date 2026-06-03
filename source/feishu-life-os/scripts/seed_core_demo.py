from __future__ import annotations

from datetime import datetime, timedelta

from app.config import get_settings
from app.core.store import StateStore
from app.database import Repository


def main() -> None:
    settings = get_settings()
    repo = Repository(settings.database_path, database_url=settings.database_url)
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    now = datetime.now(settings.tzinfo)
    store.create_action_item(
        {
            "title": "整理本周学习计划",
            "description": "demo seed",
            "due_at": now.replace(hour=20, minute=0, second=0, microsecond=0),
            "priority": "P2",
            "confidence": 1.0,
        }
    )
    store.create_calendar_event(
        {
            "title": "明天下午给小王补课",
            "start_at": (now + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0),
            "end_at": (now + timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0),
            "confidence": 1.0,
        }
    )
    print("Seeded core demo data")


if __name__ == "__main__":
    main()
