from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

CHINESE_NUMBERS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}

WEEKDAY_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
    "5": 4,
    "6": 5,
    "7": 6,
}


@dataclass(frozen=True)
class ParsedDateTime:
    value: datetime
    all_day: bool
    confidence: float
    matched_text: str


def _num(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    return CHINESE_NUMBERS.get(value)


def _next_weekday(base: datetime, target_weekday: int, next_week: bool = False) -> datetime:
    days_ahead = target_weekday - base.weekday()
    if next_week:
        days_ahead += 7 if days_ahead <= 0 else 7
    elif days_ahead < 0:
        days_ahead += 7
    return base + timedelta(days=days_ahead)


def _extract_date(text: str, base: datetime) -> tuple[datetime | None, str, float]:
    current_year = base.year

    absolute = re.search(r"(?:(20\d{2})[年\-/\.])?(\d{1,2})[月\-/\.](\d{1,2})[日号]?", text)
    if absolute:
        year = int(absolute.group(1) or current_year)
        month = int(absolute.group(2))
        day = int(absolute.group(3))
        try:
            candidate = base.replace(year=year, month=month, day=day)
            if not absolute.group(1) and candidate.date() < base.date():
                candidate = candidate.replace(year=year + 1)
            return candidate, absolute.group(0), 0.95
        except ValueError:
            return None, absolute.group(0), 0.0

    md_short = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", text)
    if md_short:
        month = int(md_short.group(1))
        day = int(md_short.group(2))
        try:
            candidate = base.replace(month=month, day=day)
            if candidate.date() < base.date():
                candidate = candidate.replace(year=base.year + 1)
            return candidate, md_short.group(0), 0.86
        except ValueError:
            return None, md_short.group(0), 0.0

    relative_map = [
        ("大后天", 3),
        ("後天", 2),
        ("后天", 2),
        ("明天", 1),
        ("明早", 1),
        ("明晚", 1),
        ("今天", 0),
        ("今晚", 0),
        ("今早", 0),
    ]
    for token, delta in relative_map:
        if token in text:
            return base + timedelta(days=delta), token, 0.9

    weekday = re.search(r"(下周|下星期|下礼拜|本周|这周|这星期|这礼拜|周|星期|礼拜)([一二三四五六日天1-7])", text)
    if weekday:
        prefix, day_token = weekday.groups()
        target = WEEKDAY_MAP[day_token]
        next_week = prefix in {"下周", "下星期", "下礼拜"}
        candidate = _next_weekday(base, target, next_week=next_week)
        return candidate, weekday.group(0), 0.86

    return None, "", 0.0


def _extract_time(text: str, base: datetime) -> tuple[time | None, str, float]:
    hm = re.search(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", text)
    if hm:
        return time(hour=int(hm.group(1)), minute=int(hm.group(2))), hm.group(0), 0.95

    cn_or_digit = r"(\d{1,2}|一|二|两|三|四|五|六|七|八|九|十|十一|十二)"
    point = re.search(rf"(凌晨|早上|上午|中午|下午|傍晚|晚上|今晚|明早)?\s*{cn_or_digit}\s*点(半|[一二三四五六七八九十\d]刻|[0-5]?\d分?)?", text)
    if point:
        period = point.group(1) or ""
        hour_raw = point.group(2)
        minute_raw = point.group(3) or ""
        hour = _num(hour_raw)
        if hour is None:
            return None, point.group(0), 0.0
        minute = 0
        if "半" in minute_raw:
            minute = 30
        elif "一刻" in minute_raw:
            minute = 15
        elif "三刻" in minute_raw:
            minute = 45
        else:
            minute_digits = re.search(r"(\d{1,2})", minute_raw)
            if minute_digits:
                minute = int(minute_digits.group(1))
        if period in {"下午", "傍晚", "晚上", "今晚"} and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None, point.group(0), 0.0
        return time(hour=hour, minute=minute), point.group(0), 0.88

    fuzzy_defaults = [
        ("明早", time(9, 0), 0.65),
        ("今早", time(9, 0), 0.65),
        ("今晚", time(21, 0), 0.6),
        ("明晚", time(21, 0), 0.6),
        ("上午", time(10, 0), 0.5),
        ("中午", time(12, 0), 0.5),
        ("下午", time(15, 0), 0.5),
        ("晚上", time(20, 0), 0.5),
    ]
    for token, default_time, conf in fuzzy_defaults:
        if token in text:
            return default_time, token, conf

    return None, "", 0.0


def parse_datetime(text: str, tz: ZoneInfo, base: datetime | None = None) -> ParsedDateTime | None:
    base = base or datetime.now(tz)
    if base.tzinfo is None:
        base = base.replace(tzinfo=tz)
    date_part, date_match, date_conf = _extract_date(text, base)
    time_part, time_match, time_conf = _extract_time(text, base)

    if not date_part and not time_part:
        return None

    all_day = time_part is None
    if date_part is None:
        assert time_part is not None
        candidate = datetime.combine(base.date(), time_part, tzinfo=tz)
        if candidate < base:
            candidate += timedelta(days=1)
        confidence = time_conf * 0.7
    else:
        chosen_time = time_part or time(23, 59)
        candidate = datetime.combine(date_part.date(), chosen_time, tzinfo=tz)
        confidence = max(date_conf, (date_conf + time_conf) / 2 if time_part else date_conf * 0.85)

    matched_text = " ".join(part for part in [date_match, time_match] if part).strip()
    return ParsedDateTime(
        value=candidate.replace(microsecond=0),
        all_day=all_day,
        confidence=round(confidence, 2),
        matched_text=matched_text,
    )
