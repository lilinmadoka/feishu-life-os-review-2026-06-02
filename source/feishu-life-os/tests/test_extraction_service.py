from datetime import datetime
from zoneinfo import ZoneInfo

from app.models import ActionIntent, Domain, Priority
from app.services.extraction_service import RuleBasedExtractor

TZ = ZoneInfo("Asia/Singapore")
BASE = datetime(2026, 5, 26, 10, 0, tzinfo=TZ)


def extractor():
    return RuleBasedExtractor(TZ, now_provider=lambda: BASE)


def test_extract_tutoring_and_materials():
    actions = extractor().extract("明天下午3点给学生小王补课，记得今晚把资料发给家长", "cap_1")
    assert len(actions) == 2
    assert actions[0].domain == Domain.tutoring
    assert actions[0].intent == ActionIntent.event
    assert actions[0].due_at.isoformat() == "2026-05-27T15:00:00+08:00"
    assert actions[1].due_at.isoformat() == "2026-05-26T21:00:00+08:00"


def test_extract_school_deadline():
    actions = extractor().extract("周五前提交数据库作业，老师说不要晚交", "cap_2")
    assert len(actions) == 1
    assert actions[0].domain == Domain.school
    assert actions[0].intent == ActionIntent.deadline
    assert actions[0].priority in {Priority.p2, Priority.p1}


def test_extract_project_task_priority():
    actions = extractor().extract("今晚把项目 README 改完，明早让 Codex 接飞书 API", "cap_3")
    assert len(actions) >= 1
    assert actions[0].domain == Domain.project
    assert actions[0].priority in {Priority.p0, Priority.p1}
