from datetime import datetime
from zoneinfo import ZoneInfo

from app.core.relative_time import effective_now
from app.services.time_parser import parse_datetime

TZ = ZoneInfo("Asia/Singapore")
BASE = datetime(2026, 5, 26, 10, 0, tzinfo=TZ)  # Tuesday


def test_parse_tomorrow_afternoon():
    parsed = parse_datetime("明天下午3点补课", TZ, BASE)
    assert parsed is not None
    assert parsed.value.isoformat() == "2026-05-27T15:00:00+08:00"
    assert not parsed.all_day


def test_parse_friday_deadline():
    parsed = parse_datetime("周五前提交数据库作业", TZ, BASE)
    assert parsed is not None
    assert parsed.value.isoformat() == "2026-05-29T23:59:00+08:00"
    assert parsed.all_day


def test_parse_next_monday():
    parsed = parse_datetime("下周一上午开会", TZ, BASE)
    assert parsed is not None
    assert parsed.value.isoformat() == "2026-06-01T10:00:00+08:00"


def test_early_morning_tomorrow_uses_pre_sleep_day_context():
    actual_now = datetime(2026, 5, 30, 1, 25, tzinfo=TZ)
    parsed = parse_datetime("明天下午4点考普通话", TZ, effective_now(TZ, actual_now))
    assert parsed is not None
    assert parsed.value.isoformat() == "2026-05-30T16:00:00+08:00"
