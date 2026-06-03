from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.adapters.pushover_client import PushoverClient
from app.config import Settings, get_settings
from app.core.schemas import ConfirmationStatus
from app.core.store import StateStore
from app.database import Repository
from app.dependencies import get_core_feishu_adapter, get_core_store, get_repo
from app.main import create_app
from app.models import (
    ActionCreate,
    ActionIntent,
    CaptureCreate,
    Domain,
    Energy,
    Priority,
    SourceType,
)
from app.workers.reminder_worker import ReminderWorker

TZ = ZoneInfo("Asia/Singapore")
BASE = datetime(2026, 5, 28, 9, 30, tzinfo=TZ)


class FakeFeishu:
    def __init__(self):
        self.sent: list[dict[str, str]] = []
        self.sent_cards: list[dict[str, object]] = []
        self.created_meetings: list[dict[str, object]] = []

    async def send_app_text(self, receive_id: str, text: str, receive_id_type: str = "open_id"):
        self.sent.append({"receive_id": receive_id, "text": text, "receive_id_type": receive_id_type})
        return {"message_id": f"msg_{len(self.sent)}"}

    async def send_interactive_card(
        self,
        receive_id: str,
        card: dict[str, object],
        receive_id_type: str = "open_id",
    ):
        self.sent_cards.append({"receive_id": receive_id, "card": card, "receive_id_type": receive_id_type})
        return {"message_id": f"card_{len(self.sent_cards)}"}

    async def create_video_meeting_reminder(self, receive_id: str, topic: str, *, end_at=None):
        payload = {"receive_id": receive_id, "topic": topic, "end_at": end_at}
        self.created_meetings.append(payload)
        return {"data": {"reserve": {"meeting_url": "https://vc.example.test/join"}}}


class FakePush:
    configured = True

    def __init__(self):
        self.sent: list[dict[str, object]] = []

    async def send_emergency(
        self,
        title: str,
        message: str,
        *,
        url: str | None = None,
        url_title: str | None = None,
        tags: str | None = None,
    ):
        payload = {"title": title, "message": message, "url": url, "url_title": url_title, "tags": tags}
        self.sent.append(payload)
        return {"status": 1, "receipt": f"receipt_{len(self.sent)}"}


def test_pushover_client_sends_utf8_json(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        status_code = 200

        def json(self):
            return {"status": 1, "receipt": "ok"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return FakeResponse()

    monkeypatch.setattr("app.adapters.pushover_client.httpx.AsyncClient", FakeAsyncClient)
    settings = Settings(PUSHOVER_USER_KEY="user", PUSHOVER_APP_TOKEN="token")

    result = asyncio.run(
        PushoverClient(settings).send_emergency(
            "强提醒",
            "明天普通话考试，记得打印准考证",
            url="https://vc.example.test/join",
            url_title="进入会议",
            tags="lifeos:calendar_event:cal_test",
        )
    )

    assert result["status"] == 1
    kwargs = captured["kwargs"]
    assert kwargs["headers"]["Content-Type"] == "application/json; charset=utf-8"
    payload = json.loads(kwargs["content"].decode("utf-8"))
    assert payload["title"] == "强提醒"
    assert payload["message"] == "明天普通话考试，记得打印准考证"
    assert payload["url_title"] == "进入会议"
    assert payload["tags"] == "lifeos:calendar_event:cal_test"
    assert "?" not in payload["title"] + payload["message"] + payload["url_title"]


def test_pushover_client_cancels_emergency_by_tag(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        status_code = 200

        def json(self):
            return {"status": 1}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return FakeResponse()

    monkeypatch.setattr("app.adapters.pushover_client.httpx.AsyncClient", FakeAsyncClient)
    settings = Settings(PUSHOVER_USER_KEY="user", PUSHOVER_APP_TOKEN="token")

    result = asyncio.run(PushoverClient(settings).cancel_emergency_by_tag("lifeos:schedule_block:blk_1:2026-06-01"))

    assert result["status"] == 1
    assert captured["url"] == (
        "https://api.pushover.net/1/receipts/cancel_by_tag/"
        "lifeos%3Aschedule_block%3Ablk_1%3A2026-06-01.json"
    )
    assert captured["kwargs"]["data"] == {"token": "token"}


def test_reminder_worker_sends_due_reminder_once(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    capture = repo.create_capture(
        CaptureCreate(
            raw_text="明天上午10点上课",
            source_type=SourceType.feishu_event,
            metadata={"open_id": "ou_test"},
        ),
        "明天上午10点上课",
    )
    action = repo.create_action(
        ActionCreate(
            capture_id=capture.id,
            title="上午上课",
            intent=ActionIntent.event,
            domain=Domain.study,
            priority=Priority.p1,
            energy=Energy.medium,
            start_at=BASE + timedelta(minutes=30),
            due_at=BASE + timedelta(hours=2, minutes=30),
            remind_at=BASE,
            evidence_text="明天上午10点上课",
        )
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 1
    assert feishu.sent[0]["receive_id"] == "ou_test"
    assert "提醒：上午上课" in feishu.sent[0]["text"]
    updated = repo.get_action(action.id)
    assert updated.metadata["reminder_sent_at"]

    assert asyncio.run(worker.run_once()) == 0
    assert len(feishu.sent) == 1


def test_reminder_worker_skips_future_reminders(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    repo.create_action(
        ActionCreate(
            title="下午上课",
            intent=ActionIntent.event,
            domain=Domain.study,
            priority=Priority.p1,
            energy=Energy.medium,
            remind_at=BASE + timedelta(hours=1),
        )
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 0
    assert feishu.sent == []


def test_reminder_worker_creates_video_meeting_for_strong_reminder(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_STRONG_REMINDER_MODE", "video_meeting")
    get_settings.cache_clear()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    repo.create_action(
        ActionCreate(
            title="print ticket",
            intent=ActionIntent.task,
            domain=Domain.other,
            priority=Priority.p2,
            energy=Energy.medium,
            remind_at=BASE,
        )
    )
    feishu = FakeFeishu()
    push = FakePush()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", push=push, now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 1
    assert feishu.created_meetings[0]["receive_id"] == "ou_test"
    assert feishu.sent_cards[-1]["card"]["_mvp_meta"]["mode"] == "video_meeting"
    assert push.sent[0]["title"] == "强提醒"
    assert push.sent[0]["url"] == "https://vc.example.test/join"
    assert push.sent[0]["tags"].startswith("lifeos:legacy_action:")
    card_actions = feishu.sent_cards[-1]["card"]["elements"][1]["actions"]
    assert card_actions[1]["text"]["content"] == "停止强提醒"


def test_reminder_worker_falls_back_to_text_when_video_meeting_fails(monkeypatch, tmp_path):
    class FailingMeetingFeishu(FakeFeishu):
        async def create_video_meeting_reminder(self, receive_id: str, topic: str, *, end_at=None):
            raise RuntimeError("missing vc permission")

    monkeypatch.setenv("FEISHU_STRONG_REMINDER_MODE", "video_meeting")
    get_settings.cache_clear()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    repo.create_action(
        ActionCreate(
            title="print ticket",
            intent=ActionIntent.task,
            domain=Domain.other,
            priority=Priority.p2,
            energy=Energy.medium,
            remind_at=BASE,
        )
    )
    feishu = FailingMeetingFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 1
    assert feishu.sent[0]["receive_id"] == "ou_test"
    assert "视频会议创建失败" in feishu.sent[0]["text"]


def test_reminder_worker_keeps_feishu_reminder_when_push_fails(monkeypatch, tmp_path):
    class FailingPush(FakePush):
        async def send_emergency(
            self,
            title: str,
            message: str,
            *,
            url: str | None = None,
            url_title: str | None = None,
            tags: str | None = None,
        ):
            raise RuntimeError("push failed")

    monkeypatch.setenv("FEISHU_STRONG_REMINDER_MODE", "video_meeting")
    get_settings.cache_clear()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    action = repo.create_action(
        ActionCreate(
            title="print ticket",
            intent=ActionIntent.task,
            domain=Domain.other,
            priority=Priority.p2,
            energy=Energy.medium,
            remind_at=BASE,
        )
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", push=FailingPush(), now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 1
    assert feishu.sent_cards
    updated = repo.get_action(action.id)
    assert updated.metadata["reminder_response"]["pushover"]["status"] == "failed"


def test_reminder_worker_sends_pre_strong_card_three_minutes_before(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    action = repo.create_action(
        ActionCreate(
            title="print ticket",
            intent=ActionIntent.task,
            domain=Domain.other,
            priority=Priority.p2,
            energy=Energy.medium,
            remind_at=BASE + timedelta(minutes=3),
        )
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 1
    assert feishu.sent == []
    assert feishu.sent_cards[0]["receive_id"] == "ou_test"
    card = feishu.sent_cards[0]["card"]
    assert card["_mvp_meta"]["action"] == "ack_pre_strong_reminder"
    assert card["_mvp_meta"]["action_id"] == action.id
    button_texts = [button["text"]["content"] for button in card["elements"][1]["actions"]]
    assert button_texts == ["知道了", "取消安排", "最近空闲", "1小时后再排"]
    updated = repo.get_action(action.id)
    assert updated.metadata["pre_strong_card_sent_at"]

    assert asyncio.run(worker.run_once()) == 0
    assert len(feishu.sent_cards) == 1


def test_reminder_worker_skips_strong_reminder_after_pre_card_confirmed(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    repo.create_action(
        ActionCreate(
            title="print ticket",
            intent=ActionIntent.task,
            domain=Domain.other,
            priority=Priority.p2,
            energy=Energy.medium,
            remind_at=BASE,
            metadata={"pre_strong_confirmed_at": "2026-05-28T09:27:00+08:00"},
        )
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 0
    assert feishu.sent == []
    assert feishu.sent_cards == []


def test_pre_strong_card_callback_marks_strong_reminder_suppressed(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_test")
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_core_store.cache_clear()
    client = TestClient(create_app())
    repo = get_repo()
    action = repo.create_action(
        ActionCreate(
            title="print ticket",
            intent=ActionIntent.task,
            domain=Domain.other,
            priority=Priority.p2,
            energy=Energy.medium,
            remind_at=BASE,
        )
    )

    response = client.post(
        "/api/v2/feishu/card",
        json={
            "action": {"value": {"action": "ack_pre_strong_reminder", "action_id": action.id}},
            "operator": {"open_id": "ou_test"},
        },
    )

    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"
    updated = repo.get_action(action.id)
    assert updated.metadata["pre_strong_confirmed_at"]
    assert updated.metadata["pre_strong_confirmed_by"] == "ou_test"
    assert updated.metadata["strong_reminder_suppressed_at"]


def test_core_action_item_gets_pre_strong_card_and_callback_suppresses_due_reminder(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_test")
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_core_store.cache_clear()
    client = TestClient(create_app())
    repo = get_repo()
    store = StateStore(repo)
    store.migrate()
    item = store.create_action_item({"title": "print ticket", "due_at": BASE + timedelta(minutes=3)})
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 1
    assert feishu.sent == []
    card = feishu.sent_cards[0]["card"]
    assert card["_mvp_meta"]["target_type"] == "action_item"
    assert card["_mvp_meta"]["target_id"] == item.id

    response = client.post(
        "/api/v2/feishu/card",
        json={
            "action": {
                "value": {
                    "action": "ack_pre_strong_reminder",
                    "target_type": "action_item",
                    "target_id": item.id,
                }
            },
            "operator": {"open_id": "ou_test"},
        },
    )
    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"

    due_worker = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: BASE + timedelta(minutes=3),
    )
    assert asyncio.run(due_worker.run_once()) == 0
    assert feishu.sent == []


def test_calendar_event_pre_strong_card_callback_suppresses_due_reminder(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_test")
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_core_store.cache_clear()
    client = TestClient(create_app())
    repo = get_repo()
    store = StateStore(repo)
    store.migrate()
    event = store.create_calendar_event(
        {
            "title": "光学学习",
            "start_at": BASE + timedelta(minutes=3),
            "end_at": BASE + timedelta(minutes=63),
        }
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 1
    card = feishu.sent_cards[0]["card"]
    assert card["_mvp_meta"]["target_type"] == "calendar_event"
    assert card["_mvp_meta"]["target_id"] == event.id

    response = client.post(
        "/api/v2/feishu/card",
        json={
            "action": {
                "value": {
                    "action": "ack_pre_strong_reminder",
                    "target_type": "calendar_event",
                    "target_id": event.id,
                }
            },
            "operator": {"open_id": "ou_test"},
        },
    )
    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"

    due_worker = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: BASE + timedelta(minutes=3),
    )
    assert asyncio.run(due_worker.run_once()) == 0
    assert feishu.sent == []
    with repo.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders WHERE target_type=? AND target_id=? AND channel=? AND status='done'",
            ("calendar_event", event.id, "strong_reminder_suppressed"),
        ).fetchone()
    assert row is not None


def test_calendar_event_reschedule_finds_free_slot_and_rearms_pre_strong_card(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_test")
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_core_store.cache_clear()
    client = TestClient(create_app())
    repo = get_repo()
    store = StateStore(repo)
    store.migrate()
    now = datetime.now(TZ).replace(microsecond=0)
    start_at = now + timedelta(minutes=3)
    event = store.create_calendar_event(
        {
            "title": "光学学习",
            "start_at": start_at,
            "end_at": start_at + timedelta(hours=1),
        }
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: now)

    assert asyncio.run(worker.run_once()) == 1
    card = feishu.sent_cards[0]["card"]
    button_texts = [button["text"]["content"] for button in card["elements"][1]["actions"]]
    assert button_texts == ["知道了", "取消安排", "最近空闲", "1小时后再排"]
    store.create_calendar_event(
        {
            "title": "已有安排",
            "start_at": start_at + timedelta(minutes=20),
            "end_at": start_at + timedelta(minutes=80),
        }
    )

    response = client.post(
        "/api/v2/feishu/card",
        json={
            "action": {
                "value": {
                    "action": "reschedule_reminder",
                    "target_type": "calendar_event",
                    "target_id": event.id,
                    "mode": "next_available",
                }
            },
            "operator": {"open_id": "ou_test"},
        },
    )
    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"
    updated = store.get_calendar_event(event.id)
    assert updated.start_at >= start_at + timedelta(minutes=100)
    assert updated.end_at == updated.start_at + timedelta(hours=1)
    with repo.connect() as conn:
        old_mark = conn.execute(
            "SELECT status FROM reminders WHERE target_type=? AND target_id=? AND channel=?",
            ("calendar_event", event.id, "pre_strong_card_sent"),
        ).fetchone()
    assert old_mark["status"] == "superseded"

    original_due_worker = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: start_at,
    )
    assert asyncio.run(original_due_worker.run_once()) == 0
    rearmed_worker = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: updated.start_at - timedelta(minutes=3),
    )
    assert asyncio.run(rearmed_worker.run_once()) == 1
    assert len(feishu.sent_cards) == 2


def test_calendar_event_reschedule_card_accepts_nested_operator_and_replies(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_test")
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_core_store.cache_clear()
    get_core_feishu_adapter.cache_clear()
    client = TestClient(create_app())
    repo = get_repo()
    store = StateStore(repo)
    store.migrate()
    now = datetime.now(TZ).replace(microsecond=0)
    start_at = now + timedelta(minutes=3)
    event = store.create_calendar_event(
        {
            "title": "光学学习",
            "start_at": start_at,
            "end_at": start_at + timedelta(hours=1),
        }
    )

    response = client.post(
        "/api/v2/feishu/card",
        json={
            "event": {
                "action": {
                    "value": {
                        "action": "reschedule_reminder",
                        "target_type": "calendar_event",
                        "target_id": event.id,
                        "mode": "next_available",
                    }
                },
                "operator": {"user_id": {"open_id": "ou_test", "union_id": "on_test"}},
            }
        },
    )

    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"
    updated = store.get_calendar_event(event.id)
    assert updated.start_at >= start_at
    adapter = get_core_feishu_adapter()
    assert adapter.sent_texts[-1]["receive_id"] == "ou_test"
    assert adapter.sent_texts[-1]["text"]


def test_calendar_event_cancel_button_cancels_event_and_due_reminder(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_test")
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_core_store.cache_clear()
    client = TestClient(create_app())
    repo = get_repo()
    store = StateStore(repo)
    store.migrate()
    event = store.create_calendar_event(
        {
            "title": "光学学习",
            "start_at": BASE + timedelta(minutes=3),
            "end_at": BASE + timedelta(minutes=63),
        }
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)
    assert asyncio.run(worker.run_once()) == 1

    response = client.post(
        "/api/v2/feishu/card",
        json={
            "action": {
                "value": {
                    "action": "cancel_reminder_target",
                    "target_type": "calendar_event",
                    "target_id": event.id,
                }
            },
            "operator": {"open_id": "ou_test"},
        },
    )
    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"
    assert store.get_calendar_event(event.id).status.value == "canceled"

    due_worker = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: BASE + timedelta(minutes=3),
    )
    assert asyncio.run(due_worker.run_once()) == 0
    assert feishu.sent == []


def test_schedule_block_pre_strong_card_callback_suppresses_due_reminder(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_test")
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_core_store.cache_clear()
    client = TestClient(create_app())
    repo = get_repo()
    store = StateStore(repo)
    store.migrate()
    block = store.create_schedule_block(
        {
            "title": "周四上课",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TH",
            "start_time": "09:33",
            "end_time": "12:00",
            "timezone": "Asia/Shanghai",
        }
    )
    target_id = f"{block.id}:2026-05-28"
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 1
    card = feishu.sent_cards[0]["card"]
    assert card["_mvp_meta"]["target_type"] == "schedule_block"
    assert card["_mvp_meta"]["target_id"] == target_id

    response = client.post(
        "/api/v2/feishu/card",
        json={
            "action": {
                "value": {
                    "action": "ack_pre_strong_reminder",
                    "target_type": "schedule_block",
                    "target_id": target_id,
                }
            },
            "operator": {"open_id": "ou_test"},
        },
    )
    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"

    due_worker = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: BASE + timedelta(minutes=3),
    )
    assert asyncio.run(due_worker.run_once()) == 0
    assert feishu.sent == []
    with repo.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders WHERE target_type=? AND target_id=? AND channel=? AND status='done'",
            ("schedule_block", target_id, "strong_reminder_suppressed"),
        ).fetchone()
    assert row is not None


def test_schedule_block_gets_pre_strong_card(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    block = store.create_schedule_block(
        {
            "title": "周四上课",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TH",
            "start_time": "09:33",
            "end_time": "12:00",
            "timezone": "Asia/Shanghai",
        }
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 1
    assert feishu.sent == []
    card = feishu.sent_cards[0]["card"]
    assert card["_mvp_meta"]["target_type"] == "schedule_block"
    assert card["_mvp_meta"]["target_id"] == f"{block.id}:2026-05-28"


def test_schedule_block_reminders_disabled_skip_pre_card_and_strong_reminder(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_STRONG_REMINDER_MODE", "text")
    get_settings.cache_clear()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    store.create_schedule_block(
        {
            "title": "周四上课",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TH",
            "start_time": "09:33",
            "end_time": "12:00",
            "timezone": "Asia/Shanghai",
            "reminder_enabled": False,
        }
    )
    store.create_schedule_block(
        {
            "title": "周四家教",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TH",
            "start_time": "09:30",
            "end_time": "12:00",
            "timezone": "Asia/Shanghai",
            "reminder_enabled": False,
        }
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 0
    assert feishu.sent_cards == []
    assert feishu.sent == []


def test_schedule_block_sends_strong_reminder_at_start(monkeypatch, tmp_path):
    monkeypatch.setenv("FEISHU_STRONG_REMINDER_MODE", "text")
    get_settings.cache_clear()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    block = store.create_schedule_block(
        {
            "title": "周四上课",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TH",
            "start_time": "09:30",
            "end_time": "12:00",
            "timezone": "Asia/Shanghai",
        }
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: BASE)

    assert asyncio.run(worker.run_once()) == 1
    assert feishu.sent[0]["receive_id"] == "ou_test"
    assert "周四上课" in feishu.sent[0]["text"]

    assert asyncio.run(worker.run_once()) == 0
    with repo.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders WHERE target_type=? AND target_id=? AND channel=? AND status='done'",
            ("schedule_block", f"{block.id}:2026-05-28", "strong_reminder_sent"),
        ).fetchone()
    assert row is not None


def test_schedule_block_does_not_send_stale_strong_reminder(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    store.create_schedule_block(
        {
            "title": "周四上课",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TH",
            "start_time": "09:30",
            "end_time": "12:00",
            "timezone": "Asia/Shanghai",
        }
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: BASE + timedelta(minutes=20),
    )

    assert asyncio.run(worker.run_once()) == 0
    assert feishu.sent == []
    assert feishu.sent_cards == []


def test_morning_daily_review_sends_core_summary_card(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFAULT_MORNING_REVIEW_HOUR", "7")
    monkeypatch.setenv("DEFAULT_MORNING_REVIEW_MINUTE", "30")
    monkeypatch.setenv("DAILY_REVIEW_FOLLOWUP_HOURS", "2")
    get_settings.cache_clear()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    now = datetime(2026, 5, 28, 7, 30, tzinfo=TZ)
    store.create_action_item(
        {
            "title": "交实验报告",
            "priority": "P1",
            "due_at": now + timedelta(hours=2),
            "estimated_minutes": 45,
        }
    )
    overdue = store.create_action_item({"title": "补交旧作业", "priority": "P0", "due_at": now - timedelta(days=1)})
    with repo.connect() as conn:
        conn.execute(
            """
            INSERT INTO reminders (id, target_type, target_id, remind_at, channel, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rem_overdue_done",
                "action_item",
                overdue.id,
                now.isoformat(),
                "strong_reminder_sent",
                "done",
                now.isoformat(),
            ),
        )
    store.create_action_item({"title": "整理收件箱"})
    store.create_calendar_event(
        {
            "title": "光学学习",
            "start_at": now.replace(hour=10),
            "end_at": now.replace(hour=11),
        }
    )
    store.create_schedule_block(
        {
            "title": "周四固定课",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TH",
            "start_time": "13:00",
            "end_time": "15:00",
            "timezone": "Asia/Shanghai",
        }
    )
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})
    store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="create_candidates",
        proposed_tool_calls_json=[],
        sender_id="ou_test",
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: now)

    assert asyncio.run(worker.run_once()) == 1
    card = feishu.sent_cards[0]["card"]
    body = card["elements"][0]["text"]["content"]
    assert card["_mvp_meta"] == {
        "action": "ack_daily_review",
        "target_type": "daily_review",
        "target_id": "2026-05-28",
    }
    assert "交实验报告" in body
    assert "补交旧作业" in body
    assert "整理收件箱" in body
    assert "光学学习" in body
    assert "周四固定课" in body
    assert "候选事项待确认" in body
    with repo.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders WHERE target_type=? AND target_id=? AND channel=? AND status='done'",
            ("daily_review", "2026-05-28", "summary_card_sent"),
        ).fetchone()
    assert row is not None

    assert asyncio.run(worker.run_once()) == 0
    assert len(feishu.sent_cards) == 1


def test_morning_daily_review_filters_expired_confirmations(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFAULT_MORNING_REVIEW_HOUR", "7")
    monkeypatch.setenv("DEFAULT_MORNING_REVIEW_MINUTE", "30")
    get_settings.cache_clear()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    now = datetime(2026, 5, 28, 7, 30, tzinfo=TZ)
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})
    expired = store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="schedule_blocks",
        proposed_tool_calls_json=[],
        sender_id="ou_test",
        expires_at=now - timedelta(hours=1),
    )
    active = store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="course_timetable_schedule",
        proposed_tool_calls_json=[
            {
                "tool_name": "confirm_plan_schedule",
                "risk_level": "medium",
                "requires_confirmation": True,
                "arguments": {"kind": "course_timetable", "planned_events": [{"title": "量子力学"}]},
            }
        ],
        sender_id="ou_test",
        expires_at=now + timedelta(hours=1),
    )
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: now)

    assert asyncio.run(worker.run_once()) == 1
    body = feishu.sent_cards[0]["card"]["elements"][0]["text"]["content"]
    assert expired.id not in body
    assert active.id not in body
    assert "schedule_blocks" not in body
    assert "课程表日程待确认" in body
    assert "量子力学" not in body
    assert store.get_confirmation(expired.id).status == ConfirmationStatus.expired
    assert store.get_confirmation(active.id).status == ConfirmationStatus.pending


def test_daily_review_unconfirmed_gets_strong_followup_every_two_hours(monkeypatch, tmp_path):
    monkeypatch.setenv("DEFAULT_MORNING_REVIEW_HOUR", "7")
    monkeypatch.setenv("DEFAULT_MORNING_REVIEW_MINUTE", "30")
    monkeypatch.setenv("DAILY_REVIEW_FOLLOWUP_HOURS", "2")
    monkeypatch.setenv("FEISHU_STRONG_REMINDER_MODE", "text")
    get_settings.cache_clear()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    feishu = FakeFeishu()
    sent_at = datetime(2026, 5, 28, 7, 30, tzinfo=TZ)
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: sent_at)

    assert asyncio.run(worker.run_once()) == 1
    assert len(feishu.sent_cards) == 1

    early = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: sent_at + timedelta(hours=1, minutes=59),
    )
    assert asyncio.run(early.run_once()) == 0
    assert feishu.sent == []

    first_followup = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: sent_at + timedelta(hours=2),
    )
    assert asyncio.run(first_followup.run_once()) == 1
    assert "晨间任务汇总还未确认" in feishu.sent[0]["text"]

    duplicate = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: sent_at + timedelta(hours=2, minutes=30),
    )
    assert asyncio.run(duplicate.run_once()) == 0
    assert len(feishu.sent) == 1

    second_followup = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: sent_at + timedelta(hours=4),
    )
    assert asyncio.run(second_followup.run_once()) == 1
    assert len(feishu.sent) == 2
    with repo.connect() as conn:
        channels = {
            row["channel"]
            for row in conn.execute(
                "SELECT channel FROM reminders WHERE target_type=? AND target_id=? AND status='done'",
                ("daily_review", "2026-05-28"),
            ).fetchall()
        }
    assert {"summary_card_sent", "strong_followup:1", "strong_followup:2"} <= channels


def test_daily_review_ack_callback_stops_followups(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_test")
    monkeypatch.setenv("DEFAULT_MORNING_REVIEW_HOUR", "7")
    monkeypatch.setenv("DEFAULT_MORNING_REVIEW_MINUTE", "30")
    monkeypatch.setenv("DAILY_REVIEW_FOLLOWUP_HOURS", "2")
    monkeypatch.setenv("FEISHU_STRONG_REMINDER_MODE", "text")
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_core_store.cache_clear()
    get_core_feishu_adapter.cache_clear()
    client = TestClient(create_app())
    repo = get_repo()
    sent_at = datetime(2026, 5, 28, 7, 30, tzinfo=TZ)
    feishu = FakeFeishu()
    worker = ReminderWorker(repo, feishu, TZ, fallback_open_id="ou_test", now_provider=lambda: sent_at)
    assert asyncio.run(worker.run_once()) == 1

    response = client.post(
        "/api/v2/feishu/card",
        json={
            "action": {"value": {"action": "ack_daily_review", "target_id": "2026-05-28"}},
            "operator": {"open_id": "ou_test"},
        },
    )

    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"
    with repo.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM reminders WHERE target_type=? AND target_id=? AND channel=? AND status='done'",
            ("daily_review", "2026-05-28", "summary_confirmed"),
        ).fetchone()
    assert row is not None
    followup = ReminderWorker(
        repo,
        feishu,
        TZ,
        fallback_open_id="ou_test",
        now_provider=lambda: sent_at + timedelta(hours=2),
    )
    assert asyncio.run(followup.run_once()) == 0
    assert feishu.sent == []
