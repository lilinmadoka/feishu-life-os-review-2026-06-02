from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.adapters.feishu_client import FeishuClient
from app.agents.models import AgentIntent, AgentRequest, AgentResponse, AgentToolCall, AgentToolName
from app.agents.orchestrator import AgentOrchestrator
from app.agents.providers.base import AgentProviderUnavailable
from app.config import get_settings
from app.database import Repository
from app.dependencies import get_agent_orchestrator, get_feishu_client, get_repo
from app.main import create_app
from app.models import (
    ActionCreate,
    ActionIntent,
    CaptureCreate,
    Domain,
    Energy,
    Priority,
    ReviewJobType,
    SourceType,
)
from app.services.review_service import ReviewService
from app.services.sync_service import SyncService

TZ = ZoneInfo("Asia/Singapore")


class FakeProvider:
    name = "fake"

    def __init__(self, responder: Callable[[AgentRequest], AgentResponse]):
        self.responder = responder
        self.requests: list[AgentRequest] = []

    def run(self, request: AgentRequest) -> AgentResponse:
        self.requests.append(request)
        return self.responder(request)


class BrokenProvider:
    name = "broken"

    def run(self, _request: AgentRequest) -> AgentResponse:
        raise AgentProviderUnavailable("codex missing")


class ShouldNotRunProvider:
    name = "should_not_run"

    def run(self, _request: AgentRequest) -> AgentResponse:
        raise AssertionError("provider should not run for fast-path trivial queries")


class FakeFeishu(FeishuClient):
    def __init__(self):
        self.sent: list[dict[str, str]] = []
        self.created_tasks = []
        self.created_calendar_events = []

    async def send_app_text(self, receive_id: str, text: str, receive_id_type: str = "open_id"):
        self.sent.append({"receive_id": receive_id, "text": text, "receive_id_type": receive_id_type})
        return {"message_id": "reply_1"}

    def to_capture_record(self, capture):
        return {"fields": {"text": capture.raw_text}}

    def to_action_record(self, action):
        return {"fields": {"title": action.title}}

    def to_task_payload(self, action):
        return {"summary": action.title}

    def to_calendar_payload(self, action):
        return {"summary": action.title}

    async def create_task(self, action):
        self.created_tasks.append(action.id)
        return {"data": {"task": {"guid": f"task_{action.id}"}}}

    async def create_calendar_event(self, action):
        self.created_calendar_events.append(action.id)
        return {"data": {"event": {"event_id": f"event_{action.id}"}}}


def reset_settings():
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_feishu_client.cache_clear()


def build_app(monkeypatch, tmp_path, provider, token: str = "verify-token", sync_mode: str = "dry_run"):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("FEISHU_SYNC_MODE", sync_mode)
    monkeypatch.setenv("FEISHU_EVENT_VERIFICATION_TOKEN", token)
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_test")
    monkeypatch.setenv("ADMIN_API_TOKEN", "admin-token")
    reset_settings()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    fake_feishu = FakeFeishu()
    review_service = ReviewService(repo, TZ)
    sync = SyncService(repo, fake_feishu, sync_mode=sync_mode)
    orchestrator = AgentOrchestrator(repo, provider, review_service, sync, fake_feishu, TZ)
    app = create_app()
    app.dependency_overrides[get_agent_orchestrator] = lambda: orchestrator
    return app, repo, fake_feishu, provider


def feishu_payload(
    text: str = "今天还有什么任务？",
    message_id: str = "mid_1",
    chat_type: str = "p2p",
    token: str = "verify-token",
    message_type: str = "text",
):
    content = '{"text":"' + text + '"}' if message_type == "text" else '{"image_key":"img_1"}'
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_1",
            "event_type": "im.message.receive_v1",
            "token": token,
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_test"}},
            "message": {
                "message_id": message_id,
                "chat_type": chat_type,
                "message_type": message_type,
                "content": content,
            },
        },
    }


def test_feishu_url_verification(monkeypatch, tmp_path):
    provider = FakeProvider(lambda _: AgentResponse(intent=AgentIntent.ignore))
    app, _, _, _ = build_app(monkeypatch, tmp_path, provider)
    client = TestClient(app)
    response = client.post(
        "/api/feishu/events",
        json={"type": "url_verification", "challenge": "abc", "token": "verify-token"},
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc"}


def test_query_today_does_not_create_task(monkeypatch, tmp_path):
    provider = FakeProvider(
        lambda _: AgentResponse(
            intent=AgentIntent.query,
            reply_text="我来查今天任务。",
            tool_calls=[AgentToolCall(name=AgentToolName.query_today)],
            confidence=0.9,
            reason_summary="用户在查询今天任务。",
        )
    )
    app, repo, feishu, _ = build_app(monkeypatch, tmp_path, provider, sync_mode="bitable")
    now = datetime.now(TZ)
    due_today = now.replace(hour=23, minute=59, second=0, microsecond=0)
    repo.create_action(
        ActionCreate(
            title="今晚把资料发家长",
            intent=ActionIntent.task,
            domain=Domain.tutoring,
            priority=Priority.p1,
            energy=Energy.medium,
            due_at=due_today,
        )
    )
    response = TestClient(app).post("/api/feishu/events", json=feishu_payload("今天还有什么任务？"))
    assert response.status_code == 200
    assert len(repo.list_actions(limit=10)) == 1
    assert any("今晚把资料发家长" in message["text"] for message in feishu.sent)
    assert provider.requests == []


def test_query_today_fast_path_works_when_codex_is_unavailable(monkeypatch, tmp_path):
    app, repo, feishu, _ = build_app(monkeypatch, tmp_path, ShouldNotRunProvider())
    now = datetime.now(TZ)
    due_today = now.replace(hour=23, minute=59, second=0, microsecond=0)
    repo.create_action(
        ActionCreate(
            title="今晚把资料发家长",
            intent=ActionIntent.task,
            domain=Domain.tutoring,
            priority=Priority.p1,
            energy=Energy.medium,
            due_at=due_today,
        )
    )
    response = TestClient(app).post("/api/feishu/events", json=feishu_payload("今天还有啥任务"))
    assert response.status_code == 200
    assert any("今晚把资料发家长" in message["text"] for message in feishu.sent)
    assert len(repo.list_actions(limit=10)) == 1


def test_query_tomorrow_fast_path_filters_to_tomorrow(monkeypatch, tmp_path):
    app, repo, feishu, _ = build_app(monkeypatch, tmp_path, ShouldNotRunProvider())
    now = datetime.now(TZ)
    repo.create_action(
        ActionCreate(
            title="明天上午上课",
            intent=ActionIntent.event,
            domain=Domain.study,
            priority=Priority.p1,
            energy=Energy.medium,
            start_at=(now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0),
            due_at=(now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0),
        )
    )
    repo.create_action(
        ActionCreate(
            title="后天检查资料",
            intent=ActionIntent.task,
            domain=Domain.study,
            priority=Priority.p2,
            energy=Energy.low,
            due_at=(now + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0),
        )
    )
    response = TestClient(app).post("/api/feishu/events", json=feishu_payload("明天有啥任务"))
    assert response.status_code == 200
    reply_text = "\n".join(message["text"] for message in feishu.sent)
    assert "明天上午上课" in reply_text
    assert "后天检查资料" not in reply_text
    assert len(repo.list_actions(limit=10)) == 2


def test_private_capture_message_uses_agent_to_create_two_tasks(monkeypatch, tmp_path):
    tomorrow = datetime.now(TZ) + timedelta(days=1)
    tonight = datetime.now(TZ).replace(hour=21, minute=0, second=0, microsecond=0)
    provider = FakeProvider(
        lambda _: AgentResponse(
            intent=AgentIntent.capture,
            reply_text="已识别 2 个事项：1. 明天下午3点给小王补课；2. 今晚把资料发家长。",
            tool_calls=[
                AgentToolCall(
                    name=AgentToolName.create_task,
                    arguments={
                        "title": "给小王补课",
                        "due_at": tomorrow.replace(hour=15, minute=0, second=0, microsecond=0).isoformat(),
                        "domain": "tutoring",
                        "priority": "P1",
                        "evidence_text": "明天下午3点给小王补课",
                    },
                ),
                AgentToolCall(
                    name=AgentToolName.create_task,
                    arguments={
                        "title": "把资料发家长",
                        "due_at": tonight.isoformat(),
                        "domain": "tutoring",
                        "priority": "P1",
                        "evidence_text": "今晚把资料发家长",
                    },
                ),
            ],
            confidence=0.9,
            reason_summary="用户输入包含两个明确事项。",
        )
    )
    app, repo, feishu, _ = build_app(monkeypatch, tmp_path, provider, sync_mode="bitable")
    response = TestClient(app).post(
        "/api/feishu/events",
        json=feishu_payload("明天下午3点给小王补课，今晚把资料发家长"),
    )
    assert response.status_code == 200
    actions = repo.list_actions(limit=10)
    assert len(actions) == 2
    assert {action.title for action in actions} == {"给小王补课", "把资料发家长"}
    assert any("已识别 2 个事项" in message["text"] for message in feishu.sent)
    assert repo.get_next_review_job() is None


def test_agent_can_sync_created_event_to_feishu_calendar(monkeypatch, tmp_path):
    tomorrow = datetime.now(TZ) + timedelta(days=1)
    provider = FakeProvider(
        lambda _: AgentResponse(
            intent=AgentIntent.capture,
            reply_text="已记录课程并同步到飞书日历。",
            tool_calls=[
                AgentToolCall(
                    name=AgentToolName.create_task,
                    arguments={
                        "title": "上午上课",
                        "intent": "event",
                        "domain": "study",
                        "start_at": tomorrow.replace(hour=10, minute=0, second=0, microsecond=0).isoformat(),
                        "due_at": tomorrow.replace(hour=12, minute=0, second=0, microsecond=0).isoformat(),
                    },
                ),
                AgentToolCall(name=AgentToolName.sync_feishu_calendar),
            ],
            confidence=0.9,
            reason_summary="课程是日程事件。",
        )
    )
    app, repo, feishu, _ = build_app(monkeypatch, tmp_path, provider, sync_mode="bitable")
    response = TestClient(app).post("/api/feishu/events", json=feishu_payload("明天上午10点到12点上课"))
    assert response.status_code == 200
    action = repo.list_actions(limit=10)[0]
    assert feishu.created_calendar_events == [action.id]
    assert repo.get_action(action.id).metadata["feishu_calendar_event"]["data"]["event"]["event_id"]
    assert any("飞书日历" in message["text"] for message in feishu.sent)


def test_agent_can_sync_created_task_to_feishu_task(monkeypatch, tmp_path):
    provider = FakeProvider(
        lambda _: AgentResponse(
            intent=AgentIntent.capture,
            reply_text="已记录待办并同步到飞书任务。",
            tool_calls=[
                AgentToolCall(name=AgentToolName.create_task, arguments={"title": "整理资料"}),
                AgentToolCall(name=AgentToolName.sync_feishu_task),
            ],
            confidence=0.9,
            reason_summary="普通待办适合飞书任务。",
        )
    )
    app, repo, feishu, _ = build_app(monkeypatch, tmp_path, provider, sync_mode="bitable")
    response = TestClient(app).post("/api/feishu/events", json=feishu_payload("整理资料"))
    assert response.status_code == 200
    action = repo.list_actions(limit=10)[0]
    assert feishu.created_tasks == [action.id]
    assert repo.get_action(action.id).feishu_task_guid == f"task_{action.id}"
    assert any("飞书任务" in message["text"] for message in feishu.sent)


def test_update_time_single_match_changes_existing_task(monkeypatch, tmp_path):
    new_time = (datetime.now(TZ) + timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
    provider = FakeProvider(
        lambda _: AgentResponse(
            intent=AgentIntent.update,
            reply_text="我来修改。",
            tool_calls=[
                AgentToolCall(
                    name=AgentToolName.update_task_time,
                    arguments={"query": "小王补课", "due_at": new_time.isoformat()},
                )
            ],
            confidence=0.85,
            reason_summary="用户要修改已有任务时间。",
        )
    )
    app, repo, feishu, _ = build_app(monkeypatch, tmp_path, provider)
    action = repo.create_action(
        ActionCreate(
            title="给小王补课",
            intent=ActionIntent.task,
            domain=Domain.tutoring,
            priority=Priority.p1,
            energy=Energy.medium,
            due_at=(datetime.now(TZ) + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0),
        )
    )
    response = TestClient(app).post("/api/feishu/events", json=feishu_payload("把小王补课改到晚上7点"))
    assert response.status_code == 200
    updated = repo.get_action(action.id)
    assert updated.due_at is not None
    assert updated.due_at.hour == 19
    assert any("截止/结束时间设为" in message["text"] for message in feishu.sent)


def test_update_reminder_time_reply_uses_reminder_time(monkeypatch, tmp_path):
    reminder_time = (datetime.now(TZ) + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
    provider = FakeProvider(
        lambda _: AgentResponse(
            intent=AgentIntent.update,
            reply_text="我来设置提醒。",
            tool_calls=[
                AgentToolCall(
                    name=AgentToolName.update_task_time,
                    arguments={"query": "上午上课", "remind_at": reminder_time.isoformat()},
                )
            ],
            confidence=0.85,
            reason_summary="用户要设置任务提醒。",
        )
    )
    app, repo, feishu, _ = build_app(monkeypatch, tmp_path, provider)
    action = repo.create_action(
        ActionCreate(
            title="上午上课",
            intent=ActionIntent.event,
            domain=Domain.study,
            priority=Priority.p1,
            energy=Energy.medium,
            start_at=(datetime.now(TZ) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0),
            due_at=(datetime.now(TZ) + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0),
        )
    )
    response = TestClient(app).post("/api/feishu/events", json=feishu_payload("上课前30分钟提醒我"))
    assert response.status_code == 200
    updated = repo.get_action(action.id)
    assert updated.remind_at is not None
    assert updated.remind_at.hour == 9
    assert any("提醒时间设为" in message["text"] for message in feishu.sent)
    assert not any("截止/结束时间设为 明天 12:00" in message["text"] for message in feishu.sent)


def test_update_time_multiple_matches_asks_confirmation(monkeypatch, tmp_path):
    provider = FakeProvider(
        lambda _: AgentResponse(
            intent=AgentIntent.update,
            reply_text="我来修改。",
            tool_calls=[
                AgentToolCall(
                    name=AgentToolName.update_task_time,
                    arguments={
                        "query": "小王补课",
                        "due_at": (datetime.now(TZ) + timedelta(days=1)).replace(hour=19).isoformat(),
                    },
                )
            ],
            confidence=0.7,
            reason_summary="用户要修改已有任务时间。",
        )
    )
    app, repo, feishu, _ = build_app(monkeypatch, tmp_path, provider)
    for title in ["给小王补课", "提醒小王补课资料"]:
        repo.create_action(ActionCreate(title=title, domain=Domain.tutoring))
    response = TestClient(app).post("/api/feishu/events", json=feishu_payload("把小王补课改到晚上7点"))
    assert response.status_code == 200
    assert any("找到多条可能的任务" in message["text"] for message in feishu.sent)


def test_confirmation_reply_gets_pending_summary(monkeypatch, tmp_path):
    seen: list[AgentRequest] = []

    def responder(request: AgentRequest) -> AgentResponse:
        seen.append(request)
        if len(seen) == 1:
            return AgentResponse(
                intent=AgentIntent.clarify,
                reply_text="请选择 A 或 B。",
                tool_calls=[
                    AgentToolCall(
                        name=AgentToolName.ask_confirmation,
                        arguments={
                            "prompt": "请选择：A. 全部任务；B. 只处理明天两节课。",
                            "candidates": [
                                {"id": "A", "title": "全部任务", "due_at": None},
                                {"id": "B", "title": "明天两节课", "due_at": None},
                            ],
                        },
                    )
                ],
                needs_confirmation=True,
                confidence=0.7,
                reason_summary="需要确认范围。",
            )
        assert request.raw_text == "B"
        assert request.pending_summary
        assert "只处理明天两节课" in str(request.pending_summary)
        return AgentResponse(
            intent=AgentIntent.clarify,
            reply_text="已按 B 处理。",
            confidence=0.8,
            reason_summary="用户回答上一轮确认。",
        )

    provider = FakeProvider(responder)
    app, _, feishu, _ = build_app(monkeypatch, tmp_path, provider)
    client = TestClient(app)
    assert client.post("/api/feishu/events", json=feishu_payload("帮我处理这些任务", message_id="mid_a")).status_code == 200
    assert client.post("/api/feishu/events", json=feishu_payload("B", message_id="mid_b")).status_code == 200
    assert any("已按 B 处理" in message["text"] for message in feishu.sent)


def test_non_private_message_is_ignored(monkeypatch, tmp_path):
    provider = FakeProvider(lambda _: AgentResponse(intent=AgentIntent.ignore))
    app, repo, _, _ = build_app(monkeypatch, tmp_path, provider)
    response = TestClient(app).post("/api/feishu/events", json=feishu_payload(chat_type="group"))
    assert response.status_code == 200
    assert response.json()["ignored"] is True
    assert repo.list_captures(limit=10) == []


def test_duplicate_message_id_is_idempotent(monkeypatch, tmp_path):
    provider = FakeProvider(
        lambda _: AgentResponse(
            intent=AgentIntent.ignore,
            reply_text="已收到。",
            confidence=0.5,
            reason_summary="测试。",
        )
    )
    app, repo, _, provider = build_app(monkeypatch, tmp_path, provider)
    client = TestClient(app)
    first = client.post("/api/feishu/events", json=feishu_payload(text="hello", message_id="mid_dup")).json()
    second = client.post("/api/feishu/events", json=feishu_payload(text="hello", message_id="mid_dup")).json()
    assert second["duplicate"] is True
    assert second["capture_id"] == first["capture_id"]
    captures = [
        capture
        for capture in repo.list_captures(limit=20)
        if capture.source_type == SourceType.feishu_event and capture.source_ref == "mid_dup"
    ]
    assert len(captures) == 1
    assert len(provider.requests) == 1


def test_wrong_or_missing_verification_token_is_rejected(monkeypatch, tmp_path):
    provider = FakeProvider(lambda _: AgentResponse(intent=AgentIntent.ignore))
    app, _, _, _ = build_app(monkeypatch, tmp_path, provider)
    client = TestClient(app)
    assert client.post("/api/feishu/events", json=feishu_payload(token="wrong")).status_code == 403
    payload = feishu_payload()
    payload["header"].pop("token")
    assert client.post("/api/feishu/events", json=payload).status_code == 403


def test_provider_unavailable_records_message_without_rules_or_bitable(monkeypatch, tmp_path):
    app, repo, feishu, _ = build_app(monkeypatch, tmp_path, BrokenProvider())
    response = TestClient(app).post("/api/feishu/events", json=feishu_payload("明天提交作业"))
    assert response.status_code == 200
    assert response.json()["queued"] is True
    assert any("智能处理器未启动/不可用" in message["text"] for message in feishu.sent)
    assert repo.list_actions(limit=10) == []
    capture = repo.list_captures(limit=10)[0]
    assert capture.status == "needs_review"


def test_image_message_enters_agent_path_without_bitable_dump(monkeypatch, tmp_path):
    provider = FakeProvider(
        lambda _: AgentResponse(
            intent=AgentIntent.clarify,
            reply_text="已收到截图，等待多模态处理器进一步处理。",
            tool_calls=[],
            confidence=0.4,
            reason_summary="图片消息暂未解析。",
        )
    )
    app, repo, feishu, provider = build_app(monkeypatch, tmp_path, provider)
    response = TestClient(app).post(
        "/api/feishu/events",
        json=feishu_payload(message_type="image", message_id="img_mid"),
    )
    assert response.status_code == 200
    capture = repo.list_captures(limit=10)[0]
    assert capture.raw_text == "[image attachment]"
    assert capture.attachments[0].kind == "image"
    assert provider.requests[0].message_type == "image"
    assert any("已收到截图" in message["text"] for message in feishu.sent)


def test_sync_failure_still_creates_sync_error_review_job(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    reset_settings()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    capture = repo.create_capture(
        CaptureCreate(raw_text="明天提交作业", source_type=SourceType.manual), "明天提交作业"
    )

    class BrokenFeishu:
        settings = type("Settings", (), {"feishu_bitable_capture_table_id": "tbl"})()

        def to_capture_record(self, _capture):
            return {"fields": {"输入内容": "明天提交作业"}}

        async def bitable_batch_create_records(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    sync = SyncService(repo, BrokenFeishu(), sync_mode="bitable")
    result = __import__("asyncio").run(sync.sync_capture(capture))
    assert result[0].status == "error"
    job = repo.get_next_review_job()
    assert job is not None
    assert job.job_type == ReviewJobType.sync_error
