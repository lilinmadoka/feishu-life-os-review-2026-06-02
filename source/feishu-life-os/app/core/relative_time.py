from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DAY_ROLLOVER_HOUR = 4


def effective_now(tz: ZoneInfo, now: datetime | None = None) -> datetime:
    current = now or datetime.now(tz)
    current = current.replace(tzinfo=tz) if current.tzinfo is None else current.astimezone(tz)
    if current.hour < DAY_ROLLOVER_HOUR:
        return current - timedelta(days=1)
    return current


def effective_day_start(tz: ZoneInfo, now: datetime | None = None) -> datetime:
    base = effective_now(tz, now)
    return base.replace(hour=0, minute=0, second=0, microsecond=0)
