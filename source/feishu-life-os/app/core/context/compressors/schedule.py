from __future__ import annotations

from datetime import datetime, timedelta

from app.core.context.budget import estimate_tokens, truncate_text
from app.core.context.schemas import ContextCapsule
from app.core.context_builder import AgentContextPack
from app.core.relative_time import effective_day_start
from app.core.store import StateStore

DAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


class ScheduleAvailabilityCompressor:
    domain = "schedule"

    def __init__(self, *, days: int = 7, day_start: str = "08:00", day_end: str = "22:30") -> None:
        self.days = days
        self.day_start = day_start
        self.day_end = day_end

    def compress(self, *, store: StateStore, legacy_pack: AgentContextPack, purpose: str) -> list[ContextCapsule]:
        now = _parse_dt(legacy_pack.now)
        if now is None:
            return []
        start_day = effective_day_start(now.tzinfo, now) if now.tzinfo else now.replace(hour=0, minute=0, second=0, microsecond=0)
        days = []
        evidence_refs = []
        for offset in range(self.days):
            day_start = self._datetime_on_day(start_day + timedelta(days=offset), self.day_start)
            day_end = self._datetime_on_day(start_day + timedelta(days=offset), self.day_end)
            busy = self._busy_ranges(store, day_start, day_end)
            free = self._free_ranges(day_start, day_end, busy)
            days.append(
                {
                    "date": day_start.date().isoformat(),
                    "weekday": DAY_CODES[day_start.weekday()],
                    "busy": [_range_json(item) for item in busy[:8]],
                    "free": [_range_json(item) for item in free[:8]],
                    "busy_count": len(busy),
                    "free_count": len(free),
                }
            )
            for item in busy:
                if item.get("id") and item.get("kind"):
                    evidence_refs.append({"kind": str(item["kind"]), "id": str(item["id"])})
        total_busy = sum(day["busy_count"] for day in days)
        total_free = sum(day["free_count"] for day in days)
        capsule = ContextCapsule(
            capsule_id="cap_schedule_availability_7d",
            domain=self.domain,
            purpose=purpose,
            summary=(
                f"Next {self.days} days availability from {self.day_start} to {self.day_end}: "
                f"{total_busy} busy interval(s), {total_free} free interval(s)."
            ),
            facts=days,
            decision_hints=[
                "Use busy/free facts for availability answers and schedule previews.",
                "Calendar events and schedule blocks are busy time; action item due dates are not hard busy time.",
            ],
            forbidden_actions=[
                "Do not create calendar events from availability facts alone.",
                "Do not assume RRULE fields beyond BYDAY in this version.",
            ],
            evidence_refs=_unique_refs(evidence_refs)[:20],
            relevance_score=0.72,
            confidence=0.78,
            freshness="live",
        )
        capsule.token_estimate = estimate_tokens(capsule.model_dump(mode="json"))
        return [capsule]

    def _busy_ranges(self, store: StateStore, start: datetime, end: datetime) -> list[dict[str, object]]:
        busy: list[dict[str, object]] = []
        for event in store.list_calendar_events(start=start - timedelta(days=1), end=end + timedelta(days=1)):
            if event.end_at > start and event.start_at < end:
                busy.append(
                    {
                        "id": event.id,
                        "kind": "calendar_event",
                        "start": max(event.start_at, start),
                        "end": min(event.end_at, end),
                        "title": truncate_text(event.title, 80),
                    }
                )
        for block in store.list_schedule_blocks():
            if not _schedule_block_matches_date(block.recurrence_rule, start):
                continue
            block_start = self._datetime_on_day(start, block.start_time)
            block_end = self._datetime_on_day(start, block.end_time)
            if block_end <= block_start:
                block_end += timedelta(days=1)
            if block_end > start and block_start < end:
                busy.append(
                    {
                        "id": block.id,
                        "kind": "schedule_block",
                        "start": max(block_start, start),
                        "end": min(block_end, end),
                        "title": truncate_text(block.title, 80),
                        "recurrence_rule": block.recurrence_rule,
                    }
                )
        return self._merge_ranges(sorted(busy, key=lambda item: item["start"]))

    def _free_ranges(self, start: datetime, end: datetime, busy: list[dict[str, object]]) -> list[dict[str, datetime]]:
        free: list[dict[str, datetime]] = []
        cursor = start
        for item in busy:
            item_start = item["start"]
            item_end = item["end"]
            if isinstance(item_start, datetime) and cursor < item_start:
                free.append({"start": cursor, "end": item_start})
            if isinstance(item_end, datetime):
                cursor = max(cursor, item_end)
        if cursor < end:
            free.append({"start": cursor, "end": end})
        return free

    def _merge_ranges(self, ranges: list[dict[str, object]]) -> list[dict[str, object]]:
        merged: list[dict[str, object]] = []
        for item in ranges:
            if not merged or item["start"] > merged[-1]["end"]:
                merged.append(dict(item))
                continue
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
            merged[-1]["title"] = f"{merged[-1].get('title')} / {item.get('title')}"
        return merged

    def _datetime_on_day(self, day: datetime, time_value: str) -> datetime:
        if time_value == "24:00":
            return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        hour, minute = time_value.split(":", 1)
        return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _schedule_block_matches_date(rrule: str, day: datetime) -> bool:
    days = _rrule_days(rrule)
    return not days or DAY_CODES[day.weekday()] in days


def _rrule_days(rrule: str) -> set[str]:
    for part in str(rrule or "").split(";"):
        if part.startswith("BYDAY="):
            return {item.strip() for item in part.removeprefix("BYDAY=").split(",") if item.strip()}
    return set()


def _range_json(item: dict[str, object]) -> dict[str, object]:
    out = dict(item)
    for key in ("start", "end"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat()
    return out


def _unique_refs(value: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    refs = []
    for item in value:
        key = (item.get("kind", ""), item.get("id", ""))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        refs.append(item)
    return refs
