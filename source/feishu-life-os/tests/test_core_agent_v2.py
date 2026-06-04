from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.adapters.feishu_client import FeishuClient
from app.config import get_settings
from app.core.feishu_native import FeishuOpenApiNativeAdapter, MockFeishuNativeAdapter
from app.core.orchestrator import CoreAgentOrchestrator
from app.core.providers import (
    LmStudioProvider,
    MockAgentProvider,
    ModelIntent,
    OpenAICompatibleChatProvider,
)
from app.core.relative_time import effective_now
from app.core.schemas import AgentResponse, AgentToolCall, CaptureIn, ConfirmationStatus, RiskLevel
from app.core.store import StateStore
from app.database import Repository
from app.dependencies import (
    get_core_feishu_adapter,
    get_core_orchestrator,
    get_core_provider,
    get_core_store,
    get_repo,
)
from app.main import create_app

TZ = ZoneInfo("Asia/Shanghai")


def reset_dependencies():
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_core_store.cache_clear()
    get_core_provider.cache_clear()
    get_core_feishu_adapter.cache_clear()


def build_orchestrator(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    feishu = MockFeishuNativeAdapter()
    orchestrator = CoreAgentOrchestrator(store, MockAgentProvider(TZ), feishu, TZ)
    return orchestrator, store, feishu


async def process(orchestrator, text: str, message_id: str = "mid"):
    return await orchestrator.process_capture(
        CaptureIn(
            source="test",
            source_message_id=message_id,
            sender_id="ou_test",
            chat_id="chat_test",
            raw_text=text,
        )
    )


def contains_value(obj, key: str, value: str) -> bool:
    if isinstance(obj, dict):
        if obj.get(key) == value:
            return True
        return any(contains_value(item, key, value) for item in obj.values())
    if isinstance(obj, list):
        return any(contains_value(item, key, value) for item in obj)
    return False


def test_query_today_does_not_create_items(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    result = asyncio.run(process(orchestrator, "今天还有什么任务？"))
    assert "今天" in result.reply_text
    assert store.list_action_items() == []
    assert store.list_calendar_events() == []


def test_create_task_and_calendar_candidates_then_confirm(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    first = asyncio.run(process(orchestrator, "明天下午3点给小王补课，今晚把资料发家长", "mid_create"))
    assert first.confirmation_id
    assert store.list_action_items() == []
    assert store.list_calendar_events() == []

    second = asyncio.run(process(orchestrator, "确认", "mid_confirm"))
    assert "已确认并创建" in second.reply_text
    assert len(store.list_action_items()) == 1
    assert len(store.list_calendar_events()) == 1
    assert store.list_tool_runs()


def test_confirmation_card_payload_contains_confirmation_id(tmp_path):
    orchestrator, _, feishu = build_orchestrator(tmp_path)
    first = asyncio.run(process(orchestrator, "明天下午3点给小王补课，今晚把资料发家长", "mid_card"))
    assert first.confirmation_id
    card = feishu.sent_cards[-1]["card"]
    assert contains_value(card, "confirmation_id", first.confirmation_id)
    assert contains_value(card, "action", "confirm")
    assert contains_value(card, "action", "cancel")
    assert contains_value(card, "type", "callback")
    assert feishu.sent_texts == []


def test_card_callback_confirm_and_cancel_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "local_user")
    reset_dependencies()
    client = TestClient(create_app())

    first = client.post("/api/v2/agent/messages", json={"raw_text": "明天下午3点给小王补课，今晚把资料发家长"}).json()
    confirmation_id = first["confirmation_id"]
    response = client.post(
        "/api/v2/feishu/card",
        json={
            "action": {"value": {"action": "confirm", "confirmation_id": confirmation_id}},
            "operator": {"open_id": "local_user"},
        },
    )
    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "success"
    store = get_core_store()
    assert len(store.list_action_items()) == 1
    assert len(store.list_calendar_events()) == 1

    second = client.post("/api/v2/agent/messages", json={"raw_text": "明天下午3点给小王补课，今晚把资料发家长", "source_message_id": "cancel_case"}).json()
    cancel_id = second["confirmation_id"]
    response = client.post(
        "/api/v2/feishu/card",
        json={
            "action": {"value": {"action": "cancel", "confirmation_id": cancel_id}},
            "operator": {"open_id": "local_user"},
        },
    )
    assert response.json()["toast"]["type"] == "success"
    assert len(store.list_action_items()) == 1
    assert len(store.list_calendar_events()) == 1


def test_feishu_event_ignores_unauthorized_sender(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_DEFAULT_ASSIGNEE_OPEN_ID", "ou_owner")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_owner")
    reset_dependencies()
    client = TestClient(create_app())

    response = client.post(
        "/api/v2/feishu/events",
        json={
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt_unauth"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_other"}},
                "message": {
                    "message_id": "msg_unauth",
                    "chat_type": "p2p",
                    "chat_id": "chat_other",
                    "message_type": "text",
                    "content": json.dumps({"text": "取消驾校"}, ensure_ascii=False),
                },
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["reason"] == "unauthorized_sender"
    with get_repo().connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM core_captures").fetchone()[0]
    assert count == 0

def test_card_callback_returns_error_toast_instead_of_500(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "local_user")
    reset_dependencies()
    client = TestClient(create_app())
    store = get_core_store()
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})
    confirmation = store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="update",
        proposed_tool_calls_json=[
            AgentToolCall(
                tool_name="update_task",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={"query": "不存在的任务", "title": "新标题"},
            ).model_dump(mode="json")
        ],
        sender_id="local_user",
    )

    response = client.post(
        "/api/v2/feishu/card",
        json={
            "action": {"value": {"action": "confirm", "confirmation_id": confirmation.id}},
            "operator": {"open_id": "local_user"},
        },
    )

    assert response.status_code == 200
    assert response.json()["toast"]["type"] == "error"
    assert "确认处理失败" in response.json()["toast"]["content"]


def test_card_callback_url_verification(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_EVENT_VERIFICATION_TOKEN", "verify-token")
    reset_dependencies()
    client = TestClient(create_app())
    response = client.post(
        "/api/v2/feishu/card",
        json={"type": "url_verification", "token": "verify-token", "challenge": "card-challenge"},
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "card-challenge"}


def test_duplicate_confirm_does_not_create_twice(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    first = asyncio.run(process(orchestrator, "明天下午3点给小王补课，今晚把资料发家长", "mid_dup_card"))
    assert first.confirmation_id
    result1 = asyncio.run(
        orchestrator.router.resolve_confirmation(sender_id="ou_test", confirmation_id=first.confirmation_id)
    )
    result2 = asyncio.run(
        orchestrator.router.resolve_confirmation(sender_id="ou_test", confirmation_id=first.confirmation_id)
    )
    assert result1["status"] == "resolved"
    assert result2["status"] == "resolved"
    assert "已经处理过" in result2["reply_text"]
    assert len(store.list_action_items()) == 1
    assert len(store.list_calendar_events()) == 1


def test_missing_and_expired_confirmation_are_safe(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    missing = asyncio.run(orchestrator.router.resolve_confirmation(sender_id="ou_test", confirmation_id="conf_missing"))
    assert missing["status"] == "not_found"

    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})
    expired = store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="create_candidates",
        proposed_tool_calls_json=[
            AgentToolCall(
                tool_name="create_task_candidate",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={"title": "过期任务"},
            ).model_dump(mode="json")
        ],
        sender_id="ou_test",
        expires_at=datetime.now(TZ) - timedelta(minutes=1),
    )
    result = asyncio.run(orchestrator.router.resolve_confirmation(sender_id="ou_test", confirmation_id=expired.id))
    assert result["status"] == "expired"
    assert store.get_confirmation(expired.id).status == ConfirmationStatus.expired
    assert store.list_action_items() == []


def test_update_calendar_event_requires_confirmation(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    now = datetime.now(TZ)
    store.create_calendar_event(
        {
            "title": "给小王补课",
            "start_at": (now + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0),
            "end_at": (now + timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0),
            "feishu_event_id": "evt_existing",
        }
    )
    result = asyncio.run(process(orchestrator, "把小王补课改到晚上7点", "mid_update"))
    assert result.confirmation_id
    event = store.list_calendar_events()[0]
    assert event.start_at.hour == 15

    confirmed = asyncio.run(process(orchestrator, "确认", "mid_update_confirm"))
    assert "calendar_event_update" in str(confirmed.tool_results)
    updated = store.list_calendar_events()[0]
    assert updated.start_at.hour == 19
    assert feishu.synced_calendar_events[-1]["operation"] == "update"
    assert feishu.synced_calendar_events[-1]["event_id"] == "evt_existing"


def test_cancel_calendar_event_deletes_feishu_event_after_confirmation(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    now = datetime.now(TZ)
    store.create_calendar_event(
        {
            "title": "给小王补课",
            "start_at": (now + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0),
            "end_at": (now + timedelta(days=1)).replace(hour=16, minute=0, second=0, microsecond=0),
            "feishu_event_id": "evt_cancel",
        }
    )

    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})
    event = store.find_calendar_events("小王")[0]
    confirmation = store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="cancel_calendar_event",
        proposed_tool_calls_json=[
            AgentToolCall(
                tool_name="cancel_calendar_event",
                risk_level=RiskLevel.high,
                requires_confirmation=True,
                arguments={"calendar_event_id": event.id},
            ).model_dump(mode="json")
        ],
        sender_id="ou_test",
    )
    assert event.status.value == "active"

    confirmed = asyncio.run(orchestrator.router.resolve_confirmation(sender_id="ou_test", confirmation_id=confirmation.id))
    assert "calendar_event_cancel" in str(confirmed["created"])
    assert store.get_calendar_event(event.id).status.value == "canceled"
    assert feishu.synced_calendar_events[-1]["operation"] == "delete"
    assert feishu.synced_calendar_events[-1]["deleted_event_id"] == "evt_cancel"


def test_update_task_time_and_cancel_task_require_confirmation(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    store.create_action_item({"title": "资料发家长", "due_at": datetime.now(TZ).replace(hour=21, minute=0)})
    update = asyncio.run(process(orchestrator, "把资料发家长任务改到晚上7点", "mid_task_update"))
    assert update.confirmation_id
    assert store.list_action_items()[0].due_at.hour == 21
    asyncio.run(process(orchestrator, "确认", "mid_task_update_confirm"))
    assert store.list_action_items()[0].due_at.hour == 19

    cancel = asyncio.run(process(orchestrator, "取消资料发家长任务", "mid_task_cancel"))
    assert cancel.confirmation_id
    asyncio.run(process(orchestrator, "确认", "mid_task_cancel_confirm"))
    canceled = store.find_action_items("资料发家长", include_done=True)[0]
    assert canceled.status.value == "canceled"


def test_complete_unique_task_and_ambiguous_task_selection(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    store.create_action_item({"title": "整理资料"})
    result = asyncio.run(process(orchestrator, "完成整理资料任务", "mid_done"))
    assert "已完成任务" in result.reply_text
    assert store.list_action_items()[0].status.value == "done"

    store.create_action_item({"title": "阅读论文"})
    store.create_action_item({"title": "阅读论文第二篇"})
    ambiguous = asyncio.run(process(orchestrator, "完成阅读论文任务", "mid_ambiguous"))
    assert "找到" in ambiguous.reply_text
    assert len([item for item in store.find_action_items("阅读论文", include_done=True) if item.status.value == "done"]) == 0


def test_weekly_schedule_becomes_schedule_block_candidate(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    result = asyncio.run(process(orchestrator, "我每周一三五晚上7点到9点固定上课，周二下午2点到5点实验课"))
    assert result.confirmation_id
    assert store.list_schedule_blocks() == []
    asyncio.run(process(orchestrator, "确认", "mid_schedule_confirm"))
    blocks = store.list_schedule_blocks()
    assert len(blocks) == 2
    assert all((block.feishu_event_id or "").startswith("mock_schedule_event_") for block in blocks)
    assert len(feishu.synced_calendar_events) == 2
    assert all("schedule_block" in item for item in feishu.synced_calendar_events)
    assert all(block.title in {"固定上课", "实验课"} for block in blocks)
    assert all(block.reminder_enabled for block in blocks)


def test_disable_fixed_schedule_reminders_keeps_schedule_blocks(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    store.create_schedule_block(
        {
            "title": "周二家教/外出",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU",
            "start_time": "18:00",
            "end_time": "22:30",
            "timezone": "Asia/Shanghai",
            "feishu_event_id": "evt_tue",
        }
    )
    store.create_schedule_block(
        {
            "title": "周一上课",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
            "start_time": "08:00",
            "end_time": "10:00",
            "timezone": "Asia/Shanghai",
            "feishu_event_id": "evt_mon",
        }
    )

    result = asyncio.run(process(orchestrator, "以后每周固定的安排不用提醒我了", "mid_disable_block_reminders"))

    assert not result.confirmation_id
    assert "已关闭 2 个固定安排的提醒" in result.reply_text
    blocks = store.list_schedule_blocks()
    assert len(blocks) == 2
    assert all(block.status.value == "active" for block in blocks)
    assert all(block.reminder_enabled is False for block in blocks)
    assert not any(item.get("operation") == "delete_schedule_block" for item in feishu.synced_calendar_events)


def test_schedule_block_type_change_without_time_asks_clarification(tmp_path):
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="update_schedule_block", confidence=0.8, entities={"query": "驾校", "title": "家教"}),
        {"raw_text": "把这个驾校改成家教"},
    )

    assert response.intent == "unknown"
    assert response.tool_calls[0].tool_name == "send_feishu_reply"
    assert "补充新的开始和结束时间" in response.reply_to_user


def test_schedule_block_update_candidate_and_confirm_with_time(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    store.create_schedule_block(
        {
            "title": "周六驾校",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=SA",
            "start_time": "12:10",
            "end_time": "21:10",
            "timezone": "Asia/Shanghai",
            "feishu_event_id": "evt_block",
        }
    )
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(
            intent="update_schedule_block",
            confidence=0.8,
            entities={"query": "驾校", "title": "家教", "start_time": "13:00", "end_time": "15:00"},
        ),
        {"raw_text": "把这个驾校改成下午1点到3点家教"},
    )
    assert response.tool_calls[0].tool_name == "update_schedule_block"
    assert response.tool_calls[0].arguments["start_time"] == "13:00"
    assert response.tool_calls[0].arguments["end_time"] == "15:00"
    confirmation = store.create_confirmation(
        agent_run_id=None,
        confirmation_type="update",
        proposed_tool_calls_json=[response.tool_calls[0].model_dump(mode="json")],
        sender_id="ou_test",
    )

    result = asyncio.run(orchestrator.router.resolve_confirmation(sender_id="ou_test", confirmation_id=confirmation.id))

    assert result["status"] == "resolved"
    assert store.list_schedule_blocks()[0].title == "家教"
    assert store.list_schedule_blocks()[0].start_time == "13:00"
    assert store.list_schedule_blocks()[0].end_time == "15:00"
    assert store.list_schedule_blocks()[0].feishu_event_id == "evt_block"
    assert feishu.synced_calendar_events[-1]["operation"] == "update_schedule_block"
    assert feishu.synced_calendar_events[-1]["event_id"] == "evt_block"


def test_query_tomorrow_includes_confirmed_schedule_blocks(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    tomorrow = effective_now(TZ) + timedelta(days=1)
    day_code = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][tomorrow.weekday()]
    store.create_schedule_block(
        {
            "title": "明天固定课",
            "recurrence_rule": f"FREQ=WEEKLY;BYDAY={day_code}",
            "start_time": "14:30",
            "end_time": "16:00",
            "timezone": "Asia/Shanghai",
        }
    )
    result = asyncio.run(process(orchestrator, "明天有什么任务吗", "mid_tomorrow_schedule"))
    assert "日程安排" in result.reply_text
    assert "固定安排" not in result.reply_text
    assert "明天固定课" in result.reply_text
    assert "14:30-16:00" in result.reply_text


def test_availability_query_expands_schedule_blocks(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    tomorrow = effective_now(TZ) + timedelta(days=1)
    day_code = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][tomorrow.weekday()]
    store.create_schedule_block(
        {
            "title": "明天固定课",
            "recurrence_rule": f"FREQ=WEEKLY;BYDAY={day_code}",
            "start_time": "14:30",
            "end_time": "16:00",
            "timezone": "Asia/Shanghai",
        }
    )
    result = asyncio.run(process(orchestrator, "明天我都啥时间有空？", "mid_free_time"))
    run = store.list_agent_runs(limit=1)[0]
    assert run.output_json["intent"] == "query_availability"
    assert "空闲时间" in result.reply_text
    assert "14:30-16:00" in result.reply_text
    assert "明天固定课" in result.reply_text


def test_free_time_query_is_not_unknown(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    asyncio.run(process(orchestrator, "明天我什么时候有空？", "mid_not_unknown"))
    run = store.list_agent_runs(limit=1)[0]
    assert run.output_json["intent"] == "query_availability"


def test_weekend_schedule_query_expands_schedule_block(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    store.create_schedule_block(
        {
            "title": "周六驾校",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=SA",
            "start_time": "12:10",
            "end_time": "21:10",
            "timezone": "Asia/Shanghai",
        }
    )
    result = asyncio.run(process(orchestrator, "周六我有什么安排？", "mid_sat"))
    run = store.list_agent_runs(limit=1)[0]
    assert run.output_json["intent"] == "query_availability"
    assert "周六驾校" in result.reply_text
    assert "12:10-21:10" in result.reply_text


def test_sleep_schedule_becomes_schedule_block(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    result = asyncio.run(process(orchestrator, "每晚12点到早上8点睡觉", "mid_sleep"))
    assert result.confirmation_id
    asyncio.run(process(orchestrator, "确认", "mid_sleep_confirm"))
    blocks = store.list_schedule_blocks()
    assert len(blocks) == 1
    assert blocks[0].title == "睡觉"


def test_long_weekly_schedule_is_split_into_day_blocks(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    text = (
        "首先我每周的日程大概是这样的：周一上午早上8:00上课，上到12:00，下午的话没有课，晚上也没有课。"
        "周二的话上午8:00到10:00有课。下午和晚上没有课，但是我晚上6:45到楼下要出发家教然后大概10:30回来"
        "然后周三的话是上午10:00到12:00有课，下午和晚上都没有课周四的话是上午10:00到12:00有课，"
        "下午的话是1:25到6.05有课，然后晚上的话也是6:45去出发，然后10:30回来"
        "周五的话上午没有课下午的话是2:30到4:00有课晚上的话是8:30要去到那个家教的地方，所以说要8:00出发，大概11:00回来"
        "周六的话上午没有课，但是中午12:10要出发去驾校，然后整个下午直到晚上8:30上课，所以说晚上是9:10回来吧"
        "然后周天的话是上午有课，下午的话是1:20出发，然后到晚上7.00回来"
    )
    result = asyncio.run(process(orchestrator, text, "mid_long_weekly"))
    assert result.confirmation_id
    card_text = feishu.sent_cards[-1]["card"]["elements"][0]["text"]["content"]
    assert "1 个固定时间块" not in card_text
    assert "固定安排" not in card_text
    assert "周一上课" in card_text
    assert "周六驾校" in card_text
    assert "08:00-12:00" in card_text
    assert "14:30-16:00" in card_text
    assert "13:20-19:00" in card_text
    assert feishu.sent_texts == []
    asyncio.run(process(orchestrator, "确认", "mid_long_weekly_confirm"))
    blocks = store.list_schedule_blocks()
    assert len(blocks) >= 9
    reply = feishu.sent_texts[-1]["text"]
    assert "schedule_block" not in reply
    assert "日程安排" in reply
    assert "固定安排" not in reply
    assert "08:00-12:00" in reply


def test_calendar_candidate_conflict_with_schedule_block_is_shown(tmp_path):
    orchestrator, _, feishu = build_orchestrator(tmp_path)
    tomorrow = datetime.now(TZ) + timedelta(days=1)
    day_code = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][tomorrow.weekday()]
    asyncio.run(
        orchestrator.router.execute_call(
            AgentToolCall(
                tool_name="confirm_schedule_blocks",
                arguments={
                    "blocks": [
                        {
                            "title": "固定占用",
                            "recurrence_rule": f"FREQ=WEEKLY;BYDAY={day_code}",
                            "start_time": "15:00",
                            "end_time": "16:00",
                            "timezone": "Asia/Shanghai",
                        }
                    ]
                },
            ),
            agent_run_id="manual",
            capture_id="manual",
            sender_id="ou_test",
        )
    )
    result = asyncio.run(process(orchestrator, "明天下午3点给小王补课，今晚把资料发家长", "mid_conflict"))
    assert result.confirmation_id
    card = feishu.sent_cards[-1]["card"]
    assert "冲突" in card["elements"][0]["text"]["content"]


def test_query_tasks_and_pending_confirmation_do_not_create_more_confirmations(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    store.create_action_item({"title": "小王资料整理"})
    related = asyncio.run(process(orchestrator, "小王相关的任务有哪些？", "mid_related"))
    assert "小王" in related.reply_text
    first = asyncio.run(process(orchestrator, "明天下午3点给小王补课，今晚把资料发家长", "mid_pending"))
    assert first.confirmation_id
    before = len(store.list_pending_confirmations("ou_test"))
    pending = asyncio.run(process(orchestrator, "最近待确认项有哪些？", "mid_pending_query"))
    assert "待确认" in pending.reply_text
    assert len(store.list_pending_confirmations("ou_test")) == before


def test_real_feishu_adapter_failure_returns_staged_payload(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    reset_dependencies()
    adapter = FeishuOpenApiNativeAdapter(FeishuClient(get_settings()))
    result = asyncio.run(
        adapter.sync_calendar_event(
            {
                "id": "cal_test",
                "title": "测试日程",
                "description": "测试",
                "start_at": datetime.now(TZ).isoformat(),
                "end_at": (datetime.now(TZ) + timedelta(hours=1)).isoformat(),
                "source_capture_id": "cap_test",
            }
        )
    )
    assert result["status"] == "failed"
    assert result["target"] == "feishu_calendar"
    assert "staged_payload" in result


def test_real_feishu_adapter_adds_calendar_attendee_after_create():
    class FakeFeishuClient:
        def __init__(self):
            self.ensured_event_ids = []

        async def create_core_calendar_event(self, calendar_event):
            return {"code": 0, "data": {"event": {"event_id": "evt_created_0"}}}

        async def ensure_core_calendar_attendees(self, event_id):
            self.ensured_event_ids.append(event_id)
            return {"status": "synced", "event_id": event_id, "added_open_ids": ["ou_owner"]}

        def to_core_calendar_payload(self, calendar_event):
            return calendar_event

    fake = FakeFeishuClient()
    adapter = FeishuOpenApiNativeAdapter(fake)
    result = asyncio.run(
        adapter.sync_calendar_event(
            {
                "id": "cal_test",
                "title": "娴嬭瘯鏃ョ▼",
                "description": "娴嬭瘯",
                "start_at": datetime.now(TZ).isoformat(),
                "end_at": (datetime.now(TZ) + timedelta(hours=1)).isoformat(),
                "source_capture_id": "cap_test",
            }
        )
    )

    assert result["status"] == "synced"
    assert result["event_id"] == "evt_created_0"
    assert result["attendee_sync"]["status"] == "synced"
    assert fake.ensured_event_ids == ["evt_created_0"]


def test_feishu_calendar_payload_assigns_type_colors():
    client = FeishuClient(get_settings())
    base_start = datetime(2026, 6, 1, 10, 0, tzinfo=TZ)
    study = client.to_core_calendar_payload(
        {
            "id": "cal_study",
            "title": "光学学习",
            "description": "长期学习安排拆分",
            "start_at": base_start.isoformat(),
            "end_at": (base_start + timedelta(hours=1)).isoformat(),
        }
    )
    exam = client.to_core_calendar_payload(
        {
            "id": "cal_exam",
            "title": "普通话考试",
            "description": "",
            "start_at": base_start.isoformat(),
            "end_at": (base_start + timedelta(hours=1)).isoformat(),
        }
    )
    driving = client.to_core_schedule_block_payload(
        {
            "id": "blk_drive",
            "title": "周六驾校",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=SA",
            "start_time": "12:10",
            "end_time": "21:10",
            "timezone": "Asia/Shanghai",
        }
    )

    assert isinstance(study["color"], int)
    assert len({study["color"], exam["color"], driving["color"]}) == 3


def test_duplicate_feishu_message_does_not_reply_or_create_second_run(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_dup")
    reset_dependencies()
    client = TestClient(create_app())
    payload = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "evt_dup"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_dup"}},
            "message": {
                "message_id": "msg_dup",
                "chat_id": "chat_dup",
                "chat_type": "p2p",
                "message_type": "text",
                "content": "{\"text\":\"今天有什么任务？\"}",
            },
        },
    }
    first = client.post("/api/v2/feishu/events", json=payload)
    second = client.post("/api/v2/feishu/events", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    store = get_core_store()
    feishu = get_core_feishu_adapter()
    assert len(store.list_agent_runs(limit=10)) == 1
    assert len(feishu.sent_texts) == 1


def test_v2_feishu_image_event_downloads_attachment_for_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ATTACHMENT_STORAGE_DIR", str(tmp_path / "attachments"))
    monkeypatch.setenv("FEISHU_ALLOWED_OPEN_IDS", "ou_img")
    reset_dependencies()

    class CapturingProvider:
        name = "capturing_provider"
        model = "test-model"

        def __init__(self):
            self.requests = []

        def run(self, request):  # noqa: ANN001, ANN202
            self.requests.append(request)
            return AgentResponse(
                intent="unknown",
                confidence=0.6,
                reasoning_summary="captured",
                reply_to_user="已收到图片。",
                tool_calls=[],
            )

    class DownloadingFeishu(MockFeishuNativeAdapter):
        async def download_message_resource(self, message_id, file_key, resource_type):  # noqa: ANN001, ANN202
            return {
                "status": "downloaded",
                "message_id": message_id,
                "file_key": file_key,
                "resource_type": resource_type,
                "content": b"\x89PNG\r\n\x1a\nfake",
                "content_type": "image/png",
                "filename": "schedule.png",
            }

    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    provider = CapturingProvider()
    feishu = DownloadingFeishu()
    orchestrator = CoreAgentOrchestrator(store, provider, feishu, TZ)
    app = create_app()
    app.dependency_overrides[get_core_orchestrator] = lambda: orchestrator
    response = TestClient(app).post(
        "/api/v2/feishu/events",
        json={
            "header": {"event_type": "im.message.receive_v1", "event_id": "evt_img"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_img"}},
                "message": {
                    "message_id": "msg_img",
                    "chat_id": "chat_img",
                    "chat_type": "p2p",
                    "message_type": "image",
                    "content": json.dumps({"image_key": "img_1"}, ensure_ascii=False),
                },
            },
        },
    )

    assert response.status_code == 200
    capture = store.list_recent_captures(limit=1)[0]
    attachment = capture.attachment_refs[0]
    assert attachment["download_status"] == "downloaded"
    assert Path(attachment["local_path"]).exists()
    assert provider.requests[0]["attachment_refs"][0]["local_path"] == attachment["local_path"]


def test_runtime_logging_records_provider_and_fallback(caplog, tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    with caplog.at_level(logging.INFO, logger="lifeos.agent_runtime"):
        asyncio.run(process(orchestrator, "今天有什么任务？", "mid_log"))
    run = store.list_agent_runs(limit=1)[0]
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert run.provider == "mock_provider"
    assert '"provider_name": "mock_provider"' in text
    assert '"used_fallback": true' in text
    assert '"intent": "query_today"' in text


def test_agent_context_pack_excludes_recursive_run_history(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    huge_history = {"recent_agent_runs": [{"input_json": "x" * 50_000}], "recent_tool_runs": [{"input_json": "y" * 50_000}]}
    store.create_agent_run(capture_id=None, provider="test", model=None, input_json=huge_history)
    capture = store.create_capture(
        CaptureIn(source="test", source_message_id="ctx_pack", sender_id="ou_test", chat_id="chat_test", raw_text="明天我都啥时间有空？")
    )

    request = orchestrator._build_agent_request(capture.model_dump(mode="json"))
    dumped = json.dumps(request, ensure_ascii=False)

    assert "recent_agent_runs" not in request
    assert "recent_tool_runs" not in request
    assert "capture" not in request
    assert len(dumped.encode("utf-8")) <= 12_000
    assert request["context_schema_version"] == 1
    assert request["context_v2"]["context_schema_version"] == 2
    assert request["raw_text"] == "明天我都啥时间有空？"


def test_agent_context_pack_includes_small_recent_user_messages(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    store.create_capture(
        CaptureIn(
            source="test",
            source_message_id="ctx_recent_prev",
            sender_id="ou_test",
            chat_id="chat_test",
            raw_text="添加一个长期安排，量子力学学习，7月份前学习不少于25小时",
        )
    )
    capture = store.create_capture(
        CaptureIn(
            source="test",
            source_message_id="ctx_recent_current",
            sender_id="ou_test",
            chat_id="chat_test",
            raw_text="明天开始，六月最后一天截止",
        )
    )

    request = orchestrator._build_agent_request(capture.model_dump(mode="json"))

    assert request["recent_user_messages"][0]["raw_text"] == "添加一个长期安排，量子力学学习，7月份前学习不少于25小时"
    assert len(json.dumps(request, ensure_ascii=False).encode("utf-8")) <= 12_000


def test_agent_context_pack_keeps_current_and_recent_attachment_refs(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    image_path = tmp_path / "schedule.png"
    image_path.write_bytes(b"fake image")
    store.create_capture(
        CaptureIn(
            source="test",
            source_message_id="ctx_image",
            sender_id="ou_test",
            chat_id="chat_test",
            content_type="image",
            raw_text="[image attachment]",
            attachment_refs=[
                {
                    "kind": "image",
                    "image_key": "img_recent",
                    "local_path": str(image_path),
                    "mime_type": "image/png",
                    "download_status": "downloaded",
                }
            ],
        )
    )
    capture = store.create_capture(
        CaptureIn(
            source="test",
            source_message_id="ctx_image_followup",
            sender_id="ou_test",
            chat_id="chat_test",
            raw_text="现在是第13周",
            attachment_refs=[{"kind": "image", "image_key": "img_current", "local_path": str(image_path)}],
        )
    )

    request = orchestrator._build_agent_request(capture.model_dump(mode="json"))

    assert request["attachment_refs"][0]["image_key"] == "img_current"
    assert request["recent_user_messages"][0]["attachment_refs"][0]["image_key"] == "img_recent"


def test_agent_context_pack_includes_recent_assistant_tool_reply_summary(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    run = store.create_agent_run(
        capture_id=None,
        provider="test",
        model=None,
        input_json={"sender_id": "ou_test", "raw_text": "光学学习怎么安排"},
    )
    store.complete_agent_run(
        run.id,
        output_json={
            "intent": "query_today",
            "reply_to_user": "我查一下这个长期学习安排目前有没有拆到日历。",
            "tool_calls": [{"tool_name": "explain_time_budget_plan", "arguments": {"query": "光学学习", "large": "x" * 5000}}],
        },
        tool_calls_json=[{"tool_name": "explain_time_budget_plan", "arguments": {"query": "光学学习", "large": "x" * 5000}}],
        latency_ms=10,
    )
    store.create_tool_run(
        agent_run_id=run.id,
        tool_name="explain_time_budget_plan",
        input_json={"query": "光学学习", "large": "x" * 5000},
        output_json={
            "reply_text": "这个长期学习任务目前只记录了总目标，还没有拆成具体日历时间段。要在日历里看到，需要先把它拆成若干日程安排候选。"
        },
    )
    capture = store.create_capture(
        CaptureIn(source="test", source_message_id="ctx_assistant", sender_id="ou_test", chat_id="chat_test", raw_text="那就拆开成具体时间段吧")
    )

    request = orchestrator._build_agent_request(capture.model_dump(mode="json"))
    dumped = json.dumps(request, ensure_ascii=False)

    assert request["recent_assistant_turns"][0]["tool_names"] == ["explain_time_budget_plan"]
    assert "还没有拆成具体日历时间段" in request["recent_assistant_turns"][0]["reply_text"]
    assert "x" * 100 not in dumped
    assert len(dumped.encode("utf-8")) <= 12_000


def test_agent_context_pack_includes_long_term_task_candidates(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    task = store.create_action_item(
        {
            "title": "量子力学学习（累计不少于25小时）",
            "estimated_minutes": 1500,
            "due_at": datetime(2026, 6, 30, 23, 59, tzinfo=TZ),
        }
    )
    capture = store.create_capture(
        CaptureIn(
            source="test",
            source_message_id="ctx_long_term",
            sender_id="ou_test",
            chat_id="chat_test",
            raw_text="那就拆分呗",
        )
    )

    request = orchestrator._build_agent_request(capture.model_dump(mode="json"))

    assert request["long_term_tasks"][0]["id"] == task.id
    assert request["long_term_tasks"][0]["estimated_minutes"] == 1500
    assert len(json.dumps(request, ensure_ascii=False).encode("utf-8")) <= 12_000


def test_pending_confirmation_context_is_summary_only(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})
    store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="create_candidates",
        proposed_tool_calls_json=[
            AgentToolCall(
                tool_name="create_task_candidate",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={"title": "写一套电学计算题", "description": "x" * 10_000},
            ).model_dump(mode="json")
        ],
        sender_id="ou_test",
    )
    capture = store.create_capture(
        CaptureIn(source="test", source_message_id="ctx_pending", sender_id="ou_test", chat_id="chat_test", raw_text="确认")
    )

    request = orchestrator._build_agent_request(capture.model_dump(mode="json"))
    dumped = json.dumps(request, ensure_ascii=False)

    assert request["pending_confirmations"][0]["candidate_titles"] == ["写一套电学计算题"]
    assert "proposed_tool_calls_json" not in dumped
    assert "x" * 100 not in dumped
    assert len(dumped.encode("utf-8")) <= 12_000


def test_provider_context_includes_compact_capsules():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    request = {
        "raw_text": "confirm",
        "context_v2": {
            "capsules": [
                {
                    "capsule_id": "cap_confirmation_pending",
                    "domain": "confirmation",
                    "purpose": "general",
                    "summary": "There is one pending confirmation.",
                    "facts": [{"large": "x" * 1000}],
                    "missing_info": [],
                    "decision_hints": ["Resolve latest pending confirmation."],
                    "forbidden_actions": ["Do not create new items."],
                    "evidence_refs": [{"kind": "confirmation", "id": "conf_test", "extra": "ignored"}],
                    "confidence": 0.9,
                    "freshness": "live",
                }
            ]
        },
    }

    context = provider._intent_context(request)

    assert context["context_capsules"][0]["domain"] == "confirmation"
    assert "facts" not in context["context_capsules"][0]
    assert context["context_capsules"][0]["evidence_refs"] == [{"kind": "confirmation", "id": "conf_test"}]


def test_model_intent_create_task_maps_to_confirmation_candidate():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(
            intent="create_task",
            confidence=0.86,
            reply="我识别到一个任务。",
            entities={"title": "给学生A出电学计算题", "estimated_minutes": 40},
            needs_confirmation=True,
        ),
        {"raw_text": "周五前给学生A出一套电学计算题，预计40分钟"},
    )

    assert response.intent == "create_candidates"
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.tool_name == "create_task_candidate"
    assert call.requires_confirmation is True
    assert call.arguments["title"] == "给学生A出电学计算题"
    assert call.arguments["estimated_minutes"] == 40


def test_model_intent_create_task_parses_tomorrow_before_rollover():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="create_task", confidence=0.86, entities={"title": "考普通话"}),
        {"raw_text": "我明天下午4点要去考普通话", "now": "2026-05-30T01:25:00+08:00"},
    )

    assert response.tool_calls[0].arguments["due_at"] == "2026-05-30T16:00:00+08:00"


def test_compound_exam_and_reminder_splits_into_event_and_task():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="create_task", confidence=0.86, entities={}),
        {
            "raw_text": "我明天下午4点要去考普通话，明天早上11点提醒我打印准考证",
            "now": "2026-05-30T01:25:00+08:00",
        },
    )

    assert response.intent == "create_candidates"
    assert [call.tool_name for call in response.tool_calls] == ["create_calendar_event_candidate", "create_task_candidate"]
    event_args = response.tool_calls[0].arguments
    task_args = response.tool_calls[1].arguments
    assert event_args["title"] == "考普通话"
    assert event_args["start_at"] == "2026-05-30T16:00:00+08:00"
    assert event_args["end_at"] == "2026-05-30T18:00:00+08:00"
    assert task_args["title"] == "打印准考证"
    assert task_args["due_at"] == "2026-05-30T11:00:00+08:00"


def test_model_intent_query_today_plan_is_read_only():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="query_today_plan", confidence=0.9, entities={}),
        {"raw_text": "今晚该干嘛"},
    )

    assert response.intent == "query_today"
    assert [call.tool_name for call in response.tool_calls] == ["query_today"]
    assert all(not call.requires_confirmation for call in response.tool_calls)


def test_time_budget_followup_does_not_query_tomorrow():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="create_time_budget_plan", confidence=0.8, entities={}),
        {
            "raw_text": "明天开始，六月最后一天截止",
            "now": "2026-05-31T00:31:00+08:00",
            "recent_user_messages": [
                {
                    "id": "cap_prev",
                    "raw_text": "添加一个长期安排，量子力学学习，7月份前学习不少于25小时",
                    "created_at": "2026-05-31T00:30:00+08:00",
                }
            ],
        },
    )

    assert response.intent == "create_candidates"
    assert response.tool_calls == []
    assert response.assistant_proposal
    plan = response.assistant_proposal.candidate_plans[0]
    assert plan["arguments"]["title"] == "量子力学学习（累计不少于25小时）"
    assert plan["arguments"]["estimated_minutes"] == 1500
    assert plan["arguments"]["due_at"] == "2026-06-30T23:59:00+08:00"
    assert "开始：2026-05-31" in plan["arguments"]["description"]


def test_time_budget_single_message_maps_to_long_term_task_candidate():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="create_time_budget_plan", confidence=0.82, entities={}),
        {
            "raw_text": "添加一个长期安排，量子力学学习，7月份前学习不少于25小时",
            "now": "2026-05-31T00:31:00+08:00",
        },
    )

    assert response.intent == "create_candidates"
    assert response.tool_calls == []
    assert response.assistant_proposal
    assert response.assistant_proposal.candidate_plans[0]["arguments"]["due_at"] == "2026-06-30T23:59:00+08:00"


def test_create_time_budget_plan_prefers_model_entities():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(
            intent="create_time_budget_plan",
            confidence=0.9,
            entities={"title": "光学学习", "estimated_minutes": 1500, "due_at": "2026-06-30"},
        ),
        {"raw_text": "请记录这个长期安排", "now": "2026-05-31T12:00:00+08:00"},
    )

    assert response.tool_calls == []
    assert response.assistant_proposal
    args = response.assistant_proposal.candidate_plans[0]["arguments"]
    assert args["title"] == "光学学习（累计不少于25小时）"
    assert args["estimated_minutes"] == 1500
    assert args["due_at"].startswith("2026-06-30T23:59")


def test_create_time_budget_plan_canonicalizes_deadline_from_raw_text():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(
            intent="create_time_budget_plan",
            confidence=0.9,
            entities={"title": "量子力学学习", "estimated_minutes": 1500, "due_at": "2026-07-31"},
        ),
        {"raw_text": "添加一个长期安排，量子力学学习，7月份前学习不少于25小时", "now": "2026-05-31T12:00:00+08:00"},
    )

    assert response.tool_calls == []
    assert response.assistant_proposal
    assert response.assistant_proposal.candidate_plans[0]["arguments"]["due_at"].startswith("2026-06-30T23:59")


def test_first_stage_prompt_omits_large_schedule_state():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    messages = provider._messages(
        {
            "raw_text": "添加一个长期安排，量子力学学习，7月份前学习不少于25小时",
            "now": "2026-05-31T12:00:00+08:00",
            "schedule_blocks": [
                {"id": f"blk_{index}", "title": f"固定安排{index}", "display_time": "08:00-12:00"}
                for index in range(30)
            ],
            "available_intents": ["create_time_budget_plan", "query_time_budget_plan", "schedule_time_budget_plan"],
        }
    )
    prompt = "\n".join(message["content"] for message in messages)

    assert "schedule_blocks" not in prompt
    assert "固定安排29" not in prompt
    assert len(prompt) < 2500


def test_model_intent_query_time_budget_plan_maps_to_explain_tool():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="query_time_budget_plan", confidence=0.86, entities={"query": "量子力学学习"}),
        {"raw_text": "我这个量子力学的学习任务的25h你是怎么给我安排的"},
    )

    assert response.intent == "query_today"
    assert response.tool_calls[0].tool_name == "explain_time_budget_plan"
    assert response.tool_calls[0].arguments["query"] == "量子力学学习"
    assert response.tool_calls[0].requires_confirmation is False


def test_model_intent_schedule_time_budget_plan_maps_to_calendar_planner():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(
            intent="schedule_time_budget_plan",
            confidence=0.86,
            entities={"query": "量子力学学习", "session_minutes": 120, "daily_minutes": 120},
        ),
        {"raw_text": "按计划接入日历"},
    )

    assert response.intent == "create_candidates"
    assert response.tool_calls == []
    assert response.assistant_proposal
    plan = response.assistant_proposal.candidate_plans[0]
    assert plan["type"] == "time_budget_schedule"
    assert plan["arguments"]["query"] == "量子力学学习"
    assert plan["arguments"]["session_minutes"] == 120


def test_model_intent_schedule_time_budget_plan_can_use_default_task():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="schedule_time_budget_plan", confidence=0.86, entities={}),
        {
            "raw_text": "按计划接入日历",
            "long_term_tasks": [{"id": "task_default", "title": "光学学习（累计不少于25小时）"}],
        },
    )

    assert response.tool_calls == []
    assert response.assistant_proposal
    assert response.assistant_proposal.candidate_plans[0]["arguments"]["action_item_id"] == "task_default"


def test_openai_provider_uses_second_stage_entity_extraction():
    class TwoStageProvider(OpenAICompatibleChatProvider):
        def __init__(self):
            super().__init__(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
            self.calls = 0

        def _post_chat_completion(self, payload, *, fallback_payload=None):  # noqa: ANN001, ANN202
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "intent": "schedule_time_budget_plan",
                                        "confidence": 0.9,
                                        "entities": {},
                                        "needs_confirmation": True,
                                        "reasoning_summary": "intent only",
                                    }
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "schedule_time_budget_plan",
                                    "confidence": 0.88,
                                    "entities": {"action_item_id": "task_123", "session_minutes": 60},
                                    "needs_confirmation": True,
                                    "reasoning_summary": "selected candidate id",
                                }
                            )
                        }
                    }
                ]
            }

    provider = TwoStageProvider()
    response = provider.run(
        {
            "raw_text": "那就拆分呗",
            "long_term_tasks": [
                {
                    "id": "task_123",
                    "kind": "task",
                    "title": "量子力学学习（累计不少于25小时）",
                    "estimated_minutes": 1500,
                    "due_at": "2026-06-30T23:59:00+08:00",
                }
            ],
            "available_intents": ["schedule_time_budget_plan"],
        }
    )

    assert provider.calls == 2
    assert response.tool_calls == []
    assert response.assistant_proposal
    args = response.assistant_proposal.candidate_plans[0]["arguments"]
    assert args["action_item_id"] == "task_123"
    assert args["session_minutes"] == 60


def test_openai_provider_sends_visual_attachments_as_image_parts(tmp_path):
    image_path = tmp_path / "capture.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")

    messages = provider._messages(
        {
            "raw_text": "把图里的内容安排一下",
            "content_type": "image",
            "attachment_refs": [{"kind": "image", "local_path": str(image_path), "mime_type": "image/png"}],
            "available_intents": ["create_schedule_block", "clarify"],
        }
    )

    content = messages[1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_attachment_only_message_cannot_resolve_confirmation():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="confirm", confidence=0.95, entities={}, reasoning_summary="bad confirmation"),
        {
            "raw_text": "[image attachment]",
            "content_type": "image",
            "attachment_refs": [{"kind": "image", "image_key": "img_1"}],
            "pending_confirmations": [{"id": "conf_1", "confirmation_type": "create_candidates"}],
        },
    )

    assert all(call.tool_name != "resolve_confirmation" for call in response.tool_calls)
    assert "附件" in response.reply_to_user


def test_unreadable_image_only_message_does_not_plan_from_old_context():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="schedule_time_budget_plan", confidence=0.98, entities={}, reasoning_summary="bad context"),
        {
            "raw_text": "[image attachment]",
            "content_type": "image",
            "attachment_refs": [
                {
                    "kind": "image",
                    "image_key": "img_1",
                    "download_status": "failed",
                    "download_error": "missing im:message:readonly",
                }
            ],
            "long_term_tasks": [{"id": "task_old", "title": "光学学习（累计不少于25小时）"}],
        },
    )

    assert all(call.tool_name != "schedule_time_budget_plan" for call in response.tool_calls)
    assert "读不到内容" in response.reply_to_user


def test_schedule_time_budget_requires_specific_long_term_target():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="schedule_time_budget_plan", confidence=0.9, entities={}, reasoning_summary="bad empty target"),
        {"raw_text": "现在是第13周", "content_type": "text"},
    )

    assert all(call.tool_name != "schedule_time_budget_plan" for call in response.tool_calls)
    assert "长期任务" in response.reply_to_user


def test_explicit_course_timetable_intent_starts_plan_draft():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(
            intent="start_plan_refinement",
            confidence=0.95,
            entities={"period_map": {"1-2": {"start_time": "08:00", "end_time": "09:40"}}},
            reasoning_summary="model selected course timetable planning",
        ),
        {"raw_text": "把课表安排进日程", "content_type": "text"},
    )

    assert response.tool_calls[0].tool_name == "start_plan_refinement"
    assert response.tool_calls[0].arguments["kind"] == "course_timetable"
    assert all(call.tool_name != "create_schedule_block_candidates" for call in response.tool_calls)
    assert "课程表草案" in response.reply_to_user


def test_active_course_draft_does_not_hijack_ordinary_schedule_change():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(
            intent="create_calendar_event",
            confidence=0.78,
            entities={},
            reasoning_summary="model did not choose plan refinement",
        ),
        {
            "raw_text": "今天下午的课调到了下下周周日早上",
            "content_type": "text",
            "active_plan_drafts": [{"id": "plan_course", "kind": "course_timetable", "status": "refining"}],
        },
    )

    assert all(call.tool_name != "refine_plan_draft" for call in response.tool_calls)
    assert "课程表草案" not in response.reply_to_user


def test_schedule_block_candidates_normalize_time_only_fields_when_complete():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(
            intent="create_schedule_block",
            confidence=0.95,
            entities={
                "blocks": [
                    {
                        "title": "高等数学",
                        "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
                        "start_at": "08:00",
                        "end_at": "09:40",
                    }
                ]
            },
            reasoning_summary="complete block",
        ),
        {"raw_text": "每周一上午高等数学", "content_type": "text"},
    )

    call = response.tool_calls[0]
    assert call.tool_name == "create_schedule_block_candidates"
    block = call.arguments["blocks"][0]
    assert block["start_time"] == "08:00"
    assert block["end_time"] == "09:40"
    assert "start_at" not in block


def test_openai_provider_repairs_partial_second_stage_intent():
    class PartialSecondStageProvider(OpenAICompatibleChatProvider):
        def __init__(self):
            super().__init__(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
            self.calls = 0

        def _post_chat_completion(self, payload, *, fallback_payload=None):  # noqa: ANN001, ANN202
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "intent": "schedule_time_budget_plan",
                                        "confidence": 0.9,
                                        "entities": {},
                                    }
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"entities":{"action_item_id":"task_123","session_minutes":60}'
                        }
                    }
                ]
            }

    provider = PartialSecondStageProvider()
    response = provider.run({"raw_text": "量子力学学习计划接入日历"})

    assert provider.calls == 2
    assert response.tool_calls == []
    assert response.assistant_proposal
    args = response.assistant_proposal.candidate_plans[0]["arguments"]
    assert args["action_item_id"] == "task_123"
    assert args["session_minutes"] == 60


def test_openai_provider_keeps_first_stage_when_second_stage_is_invalid():
    class InvalidSecondStageProvider(OpenAICompatibleChatProvider):
        def __init__(self):
            super().__init__(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
            self.calls = 0

        def _post_chat_completion(self, payload, *, fallback_payload=None):  # noqa: ANN001, ANN202
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "intent": "schedule_time_budget_plan",
                                        "confidence": 0.9,
                                        "entities": {"action_item_id": "task_123"},
                                    }
                                )
                            }
                        }
                    ]
                }
            return {"choices": [{"message": {"content": "{not-json"}}]}

    provider = InvalidSecondStageProvider()
    response = provider.run({"raw_text": "按计划接入日历"})

    assert provider.calls == 2
    assert response.tool_calls == []
    assert response.assistant_proposal
    assert response.assistant_proposal.candidate_plans[0]["arguments"]["action_item_id"] == "task_123"
    assert "Entity refinement skipped" in response.reasoning_summary


def test_openai_provider_adjudicates_time_budget_followup_to_schedule():
    class AdjudicatingProvider(OpenAICompatibleChatProvider):
        def __init__(self):
            super().__init__(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
            self.calls = 0

        def _post_chat_completion(self, payload, *, fallback_payload=None):  # noqa: ANN001, ANN202
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "intent": "query_time_budget_plan",
                                        "confidence": 0.83,
                                        "entities": {"action_item_id": "task_optics"},
                                    }
                                )
                            }
                        }
                    ]
                }
            if self.calls == 2:
                return {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "intent": "schedule_time_budget_plan",
                                        "confidence": 0.91,
                                        "entities": {"action_item_id": "task_optics"},
                                        "reasoning_summary": "Follow-up asks to proceed with calendar slots.",
                                    }
                                )
                            }
                        }
                    ]
                }
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "intent": "schedule_time_budget_plan",
                                    "confidence": 0.9,
                                    "reply": "请问要拆哪个任务？",
                                    "entities": {},
                                }
                            )
                        }
                    }
                ]
            }

    provider = AdjudicatingProvider()
    response = provider.run(
        {
            "raw_text": "那就拆开成具体时间段吧",
            "recent_assistant_turns": [
                {
                    "intent": "query_today",
                    "reply_text": "还没有拆成具体日历时间段。要在日历里看到，需要先把它拆成若干日程安排候选，确认后再写入日历。",
                    "tool_names": ["explain_time_budget_plan"],
                }
            ],
            "long_term_tasks": [
                {
                    "id": "task_optics",
                    "kind": "task",
                    "title": "光学学习（累计不少于25小时）",
                    "estimated_minutes": 1500,
                    "due_at": "2026-06-30T23:59:00+08:00",
                }
            ],
        }
    )

    assert provider.calls == 3
    assert response.tool_calls == []
    assert response.assistant_proposal
    assert response.assistant_proposal.candidate_plans[0]["arguments"]["action_item_id"] == "task_optics"


def test_lm_studio_native_chat_uses_context_length_and_output_budget():
    class NativeProvider(LmStudioProvider):
        def __init__(self):
            super().__init__(
                base_url="http://127.0.0.1:1234/v1",
                model="test-model",
                response_format="none",
                max_tokens=256,
                context_length=32768,
                use_native_chat=True,
            )
            self.requests = []

        def _request_json(self, method, url, payload=None):  # noqa: ANN001, ANN202
            self.requests.append((method, url, payload))
            return {
                "output": [
                    {
                        "type": "message",
                        "content": json.dumps({"intent": "confirm", "confidence": 0.9}),
                    }
                ],
                "stats": {"input_tokens": 12, "output_tokens": 8},
            }

    provider = NativeProvider()
    response = provider.run({"raw_text": "确认"})

    assert response.tool_calls[0].tool_name == "resolve_confirmation"
    method, url, payload = provider.requests[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:1234/api/v1/chat"
    assert payload["context_length"] == 32768
    assert payload["max_output_tokens"] == 256
    assert payload["store"] is False
    assert "确认" in payload["input"]


def test_lm_studio_provider_auto_loads_configured_context_instance():
    class AutoLoadProvider(LmStudioProvider):
        def __init__(self):
            super().__init__(
                base_url="http://127.0.0.1:1234/v1",
                model="gemma-4-e4b-it:2",
                response_format="none",
                max_tokens=128,
                context_length=32768,
                use_native_chat=False,
            )
            self.requests = []

        def _request_json(self, method, url, payload=None):  # noqa: ANN001, ANN202
            self.requests.append((method, url, payload))
            if url.endswith("/api/v1/models"):
                return {
                    "models": [
                        {
                            "loaded_instances": [
                                {"id": "gemma-4-e4b-it", "config": {"context_length": 4096}}
                            ]
                        }
                    ]
                }
            if url.endswith("/api/v1/models/load"):
                return {"instance_id": "gemma-4-e4b-it:2", "status": "loaded"}
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"intent": "confirm", "confidence": 0.9})
                        }
                    }
                ]
            }

    provider = AutoLoadProvider()
    response = provider.run({"raw_text": "确认"})
    chat_payload = provider.requests[-1][2]

    assert response.tool_calls[0].tool_name == "resolve_confirmation"
    assert provider.requests[1][1] == "http://127.0.0.1:1234/api/v1/models/load"
    assert provider.requests[1][2]["context_length"] == 32768
    assert chat_payload["model"] == "gemma-4-e4b-it:2"


def test_explain_time_budget_plan_reports_unscheduled_total_goal(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    store.create_action_item(
        {
            "title": "量子力学学习（累计不少于25小时）",
            "estimated_minutes": 1500,
            "due_at": datetime(2026, 6, 30, 23, 59, tzinfo=TZ),
        }
    )
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})

    result = asyncio.run(
        orchestrator.router.execute_call(
            AgentToolCall(
                tool_name="explain_time_budget_plan",
                risk_level=RiskLevel.low,
                arguments={"query": "量子力学学习"},
            ),
            agent_run_id=run.id,
            capture_id="cap_test",
            sender_id="ou_test",
        )
    )

    assert result["ok"] is True
    assert "还没有拆成具体日历时间段" in result["reply_text"]
    assert "不能在日历中看到" in result["reply_text"]
    assert "25小时" in result["reply_text"]
    assert "2026-06-30 23:59" in result["reply_text"]
    assert store.list_calendar_events() == []


def test_explain_time_budget_plan_lists_calendar_slots(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    store.create_action_item(
        {
            "title": "量子力学学习（累计不少于25小时）",
            "estimated_minutes": 1500,
            "due_at": datetime(2026, 6, 30, 23, 59, tzinfo=TZ),
        }
    )
    store.create_calendar_event(
        {
            "title": "量子力学学习",
            "start_at": datetime(2026, 6, 1, 19, 0, tzinfo=TZ),
            "end_at": datetime(2026, 6, 1, 21, 0, tzinfo=TZ),
        }
    )
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})

    result = asyncio.run(
        orchestrator.router.execute_call(
            AgentToolCall(
                tool_name="explain_time_budget_plan",
                risk_level=RiskLevel.low,
                arguments={"query": "量子力学学习"},
            ),
            agent_run_id=run.id,
            capture_id="cap_test",
            sender_id="ou_test",
        )
    )

    assert result["ok"] is True
    assert "已经有 1 个相关日程安排" in result["reply_text"]
    assert "2026-06-01 19:00-21:00" in result["reply_text"]


def test_vague_long_term_review_goal_generates_proposal_without_calendar_write(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)

    result = asyncio.run(process(orchestrator, "我想长期复习数学", "mid_review_proposal"))

    assert result.proposal_id
    assert result.confirmation_id is None
    drafts = store.list_plan_drafts(sender_id="ou_test", kinds=["long_term_schedule"])
    assert len(drafts) == 1
    proposal = drafts[0].payload["assistant_proposal"]
    assert proposal["user_goal"]
    assert "每次时长" in proposal["missing_info"]
    assert store.list_action_items() == []
    assert store.list_calendar_events() == []
    assert feishu.sent_cards
    assert feishu.sent_texts == []


def test_review_proposal_followup_updates_draft_and_waits_for_confirmation(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    first = asyncio.run(process(orchestrator, "我想长期复习数学", "mid_review_start"))

    refined = asyncio.run(process(orchestrator, "每天晚上8点复习90分钟，先到月底", "mid_review_refine"))

    assert refined.proposal_id == first.proposal_id
    assert refined.confirmation_id
    draft = store.get_plan_draft(refined.proposal_id)
    proposal = draft.payload["assistant_proposal"]
    assert proposal["missing_info"] == []
    assert proposal["schedule_preview"]
    confirmation = store.get_confirmation(refined.confirmation_id)
    tool_names = [call["tool_name"] for call in confirmation.proposed_tool_calls_json]
    assert tool_names[0] == "create_task_candidate"
    assert "create_calendar_event_candidate" in tool_names
    assert store.list_action_items() == []
    assert store.list_calendar_events() == []


def test_active_plan_draft_feedback_does_not_mutate_as_refinement(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    first = asyncio.run(process(orchestrator, "我想长期复习数学", "mid_review_feedback_start"))
    draft_before = store.get_plan_draft(first.proposal_id)
    payload_before = draft_before.payload
    missing_before = list(draft_before.missing_fields)
    status_before = draft_before.status
    confidence_before = draft_before.confidence
    card_count_before = len(feishu.sent_cards)

    feedback = asyncio.run(process(orchestrator, "你这个候选计划我看不懂", "mid_review_feedback"))

    draft_after = store.get_plan_draft(first.proposal_id)
    assert feedback.proposal_id is None
    assert feedback.confirmation_id is None
    assert draft_after.payload == payload_before
    assert draft_after.missing_fields == missing_before
    assert draft_after.status == status_before
    assert draft_after.confidence == confidence_before
    assert len(feishu.sent_cards) == card_count_before

    serialized_payload = json.dumps(draft_after.payload, ensure_ascii=False)
    assert "你这个候选计划我看不懂" not in serialized_payload
    assert "byday" not in serialized_payload
    assert "frequency" not in serialized_payload


def test_confirmed_review_proposal_creates_concrete_items(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    asyncio.run(process(orchestrator, "我想长期复习数学", "mid_review_start_confirm"))
    refined = asyncio.run(process(orchestrator, "每天晚上8点复习90分钟，先一个月", "mid_review_ready_confirm"))

    confirmed = asyncio.run(process(orchestrator, "确认", "mid_review_confirm"))

    assert refined.confirmation_id
    assert confirmed.confirmation_id is None
    assert len(store.list_action_items()) == 1
    assert len(store.list_calendar_events()) >= 28
    assert feishu.synced_tasks
    assert feishu.synced_calendar_events


def test_tool_router_rejects_planning_only_tools(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})

    result = asyncio.run(
        orchestrator.router.execute_call(
            AgentToolCall(
                tool_name="schedule_time_budget_plan",
                risk_level=RiskLevel.low,
                arguments={"query": "数学复习"},
            ),
            agent_run_id=run.id,
            capture_id="cap_test",
            sender_id="ou_test",
        )
    )

    assert result["ok"] is False
    assert "PlannerService" in result["error"]
    assert store.list_calendar_events() == []


def test_schedule_time_budget_plan_creates_calendar_confirmation_then_syncs(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    now = datetime.now(TZ)
    store.create_action_item(
        {
            "title": "量子力学学习（累计不少于3小时）",
            "description": f"长期累计时间计划\n开始：{now.date().isoformat()}\n总量：3小时",
            "estimated_minutes": 180,
            "due_at": now + timedelta(days=5),
        }
    )
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})

    result = asyncio.run(
        orchestrator.planner.execute_planning_call(
            AgentToolCall(
                tool_name="schedule_time_budget_plan",
                risk_level=RiskLevel.low,
                arguments={"query": "量子力学学习计划", "session_minutes": 120, "daily_minutes": 120},
            ),
            {},
            agent_run_id=run.id,
            capture_id="cap_test",
            sender_id="ou_test",
        )
    )

    planned = result.tool_results[0]
    assert planned["ok"] is True
    assert result.confirmation_id
    assert "拆成" in result.reply_text
    assert len(planned["planned_events"]) == 2
    assert store.list_calendar_events() == []
    assert feishu.sent_cards
    card_text = feishu.sent_cards[-1]["card"]["elements"][0]["text"]["content"]
    assert "规则：避开已有日程和日程安排" in card_text
    assert "时长：" in card_text
    assert "T" not in card_text

    confirmed = asyncio.run(orchestrator.router.resolve_confirmation(sender_id="ou_test", confirmation_id=result.confirmation_id))

    assert confirmed["status"] == "resolved"
    assert len(store.list_calendar_events()) == 2
    assert len(feishu.synced_calendar_events) == 2


def test_schedule_time_budget_plan_uses_daily_habit_defaults(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    start_day = (datetime.now(TZ) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    store.create_action_item(
        {
            "title": "光学学习（累计不少于4小时10分钟）",
            "description": f"长期累计时间计划\n开始：{start_day.date().isoformat()}\n总量：4小时10分钟",
            "estimated_minutes": 250,
            "due_at": start_day + timedelta(days=5, hours=23, minutes=59),
        }
    )
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})

    result = asyncio.run(
        orchestrator.planner.execute_planning_call(
            AgentToolCall(
                tool_name="schedule_time_budget_plan",
                risk_level=RiskLevel.low,
                arguments={"query": "光学学习"},
            ),
            {},
            agent_run_id=run.id,
            capture_id="cap_test",
            sender_id="ou_test",
        )
    )

    planned = result.tool_results[0]
    assert planned["ok"] is True
    assert result.confirmation_id
    planned_events = planned["planned_events"]
    assert len(planned_events) == 4
    assert planned["remaining_minutes"] == 10
    slots = [
        (datetime.fromisoformat(event["start_at"]), datetime.fromisoformat(event["end_at"]))
        for event in planned_events
    ]
    assert all(start.time() >= time(9, 30) for start, _ in slots)
    assert all(end.time() <= time(23, 59) or end.time() == time(0, 0) for _, end in slots)
    assert all(int((end - start).total_seconds() // 60) == 60 for start, end in slots)
    for day in {start.date() for start, _ in slots}:
        day_slots = [(start, end) for start, end in slots if start.date() == day]
        for previous, current in zip(day_slots, day_slots[1:], strict=False):
            assert current[0] - previous[1] >= timedelta(minutes=20)
    card_text = feishu.sent_cards[-1]["card"]["elements"][0]["text"]["content"]
    assert "09:30-24:00" in card_text
    assert "每次默认 1小时" in card_text
    assert "间隔 20分钟" in card_text


def test_schedule_time_budget_plan_matches_followup_split_phrase(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    now = datetime.now(TZ)
    store.create_action_item(
        {
            "title": "量子力学学习（累计不少于1小时）",
            "description": f"长期累计时间计划\n开始：{now.date().isoformat()}\n总量：1小时",
            "estimated_minutes": 60,
            "due_at": now + timedelta(days=2),
        }
    )
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})

    result = asyncio.run(
        orchestrator.planner.execute_planning_call(
            AgentToolCall(
                tool_name="schedule_time_budget_plan",
                risk_level=RiskLevel.low,
                arguments={"query": "那就拆分呗", "session_minutes": 60, "daily_minutes": 60},
            ),
            {},
            agent_run_id=run.id,
            capture_id="cap_test",
            sender_id="ou_test",
        )
    )

    planned = result.tool_results[0]
    assert planned["ok"] is True
    assert result.confirmation_id
    assert len(planned["planned_events"]) == 1
    assert feishu.sent_cards


def test_model_intent_cancel_schedule_block_maps_to_confirmed_cancel():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="cancel_schedule_block", confidence=0.82, entities={"schedule_block_id": "blk_test", "query": "周六驾校"}),
        {
            "raw_text": "取消周六驾校安排",
            "schedule_blocks": [
                {
                    "id": "blk_test",
                    "kind": "schedule_block",
                    "title": "周六驾校",
                    "summary": "周六驾校 12:10-21:10",
                }
            ],
        },
    )

    assert response.intent == "update_existing"
    assert response.tool_calls[0].tool_name == "cancel_schedule_block"
    assert response.tool_calls[0].requires_confirmation is True
    assert response.tool_calls[0].arguments["schedule_block_id"] == "blk_test"


def test_disable_schedule_block_reminder_text_overrides_cancel_intent():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="cancel_schedule_block", confidence=0.91, entities={}),
        {"raw_text": "以后每周固定的安排不用提醒我了"},
    )

    assert response.intent == "update_existing"
    assert response.tool_calls[0].tool_name == "disable_schedule_block_reminders"
    assert response.tool_calls[0].requires_confirmation is False
    assert response.tool_calls[0].arguments["scope"] == "all"


def test_vague_habit_goal_starts_refinement_before_writing(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)

    result = asyncio.run(process(orchestrator, "我想锻炼身体，保持健康", "mid_habit_start"))

    assert result.proposal_id
    assert result.confirmation_id is None
    assert store.list_action_items() == []
    assert store.list_calendar_events() == []
    card_text = feishu.sent_cards[-1]["card"]["elements"][0]["text"]["content"]
    assert "计划草案" in feishu.sent_cards[-1]["card"]["header"]["title"]["content"]
    assert "还缺" in card_text
    assert "每次时长" in card_text


def test_habit_refinement_generates_schedule_confirmation_then_confirm_creates_items(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    first = asyncio.run(process(orchestrator, "我想锻炼身体，保持健康", "mid_habit_start"))

    refined = asyncio.run(process(orchestrator, "每天晚上8点跑步30分钟，先一个月", "mid_habit_refine"))

    assert refined.confirmation_id
    assert refined.proposal_id == first.proposal_id
    confirmation = store.get_confirmation(refined.confirmation_id)
    assert confirmation.confirmation_type == "create_candidates"
    assert store.list_action_items() == []
    assert store.list_calendar_events() == []
    planned_events = [call["arguments"] for call in confirmation.proposed_tool_calls_json if call["tool_name"] == "create_calendar_event_candidate"]
    assert len(planned_events) >= 28
    assert "跑步" in planned_events[0]["title"]
    assert datetime.fromisoformat(planned_events[0]["start_at"]).hour == 20

    confirmed = asyncio.run(orchestrator.router.resolve_confirmation(sender_id="ou_test", confirmation_id=refined.confirmation_id))

    assert confirmed["status"] == "resolved"
    assert len(store.list_action_items()) == 1
    assert len(store.list_calendar_events()) == len(planned_events)
    assert store.list_action_items()[0].estimated_minutes == 30 * len(planned_events)
    assert len(feishu.synced_calendar_events) == len(planned_events)


def test_habit_schedule_can_be_modified_before_confirmation(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    asyncio.run(process(orchestrator, "我想锻炼身体，保持健康", "mid_habit_start"))
    scheduled = asyncio.run(process(orchestrator, "每天晚上8点跑步30分钟，先一个月", "mid_habit_refine"))

    modified = asyncio.run(process(orchestrator, "改成早上7点", "mid_habit_modify"))

    assert modified.confirmation_id
    assert modified.confirmation_id != scheduled.confirmation_id
    assert store.get_confirmation(scheduled.confirmation_id).status == ConfirmationStatus.canceled
    calls = store.get_confirmation(modified.confirmation_id).proposed_tool_calls_json
    first_event = next(call["arguments"] for call in calls if call["tool_name"] == "create_calendar_event_candidate")
    assert datetime.fromisoformat(first_event["start_at"]).hour == 7
    assert store.list_action_items() == []
    assert store.list_calendar_events() == []


def test_openai_provider_routes_vague_habit_even_when_model_is_uncertain():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")

    response = provider._intent_to_agent_response(
        ModelIntent(intent="unknown", confidence=0.2, entities={}),
        {"raw_text": "我想锻炼身体，保持健康", "pending_confirmations": []},
    )

    assert response.tool_calls[0].tool_name == "start_habit_refinement"


def test_openai_provider_routes_vague_long_term_plan_to_refinement():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")

    response = provider._intent_to_agent_response(
        ModelIntent(intent="create_task", confidence=0.8, entities={}),
        {"raw_text": "帮我弄一个长期计划，学英语", "pending_confirmations": []},
    )

    assert response.tool_calls[0].tool_name == "start_habit_refinement"


def test_habit_refinement_persists_plan_draft(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)

    result = asyncio.run(process(orchestrator, "我想锻炼身体，保持健康", "mid_habit_draft"))

    assert result.proposal_id
    drafts = store.list_plan_drafts(sender_id="ou_test", kinds=["habit"])
    assert len(drafts) == 1
    assert drafts[0].status.value == "refining"
    assert drafts[0].payload["assistant_proposal"]["user_goal"]
    assert "每次时长" in drafts[0].missing_fields


def test_course_timetable_text_without_extracted_courses_stays_in_refinement(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)

    result = asyncio.run(
        process(
            orchestrator,
            "把这个图片里的课表安排进日程。现在是第13周，一二节8:00-9:40，三四节10:00-11:40。",
            "mid_course_missing",
        )
    )

    assert result.confirmation_id
    confirmation = store.get_confirmation(result.confirmation_id)
    assert confirmation.confirmation_type == "course_timetable_refinement"
    drafts = store.list_plan_drafts(sender_id="ou_test", kinds=["course_timetable"])
    assert len(drafts) == 1
    assert drafts[0].status.value == "refining"
    assert "课程列表" in drafts[0].missing_fields
    assert store.list_calendar_events() == []
    card_text = feishu.sent_cards[-1]["card"]["elements"][0]["text"]["content"]
    assert "课程表草案" in card_text
    assert "第1周周一" in card_text


def test_course_timetable_generates_future_events_from_week_ranges(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    capture = store.create_capture(
        CaptureIn(
            source="test",
            source_message_id="mid_course_payload",
            sender_id="ou_test",
            chat_id="chat_test",
            raw_text="把课表安排进日程，现在是第13周",
            received_at=datetime.fromisoformat("2026-06-02T12:00:00+08:00"),
        )
    )
    run = store.create_agent_run(capture_id=capture.id, provider="test", model=None, input_json={})
    result = asyncio.run(
        orchestrator.planner.execute_planning_call(
            AgentToolCall(
                tool_name="start_plan_refinement",
                risk_level=RiskLevel.low,
                arguments={
                    "kind": "course_timetable",
                    "raw_text": "把课表安排进日程，现在是第13周",
                    "extracted_payload": {
                        "term_anchor": {
                            "current_teaching_week": 13,
                            "message_date": "2026-06-02T12:00:00+08:00",
                        },
                        "period_map": {
                            "7-8": {"start_time": "16:25", "end_time": "18:05"},
                        },
                        "courses": [
                            {
                                "title": "物理教学技能训练",
                                "weekday": "周五",
                                "period": "7-8",
                                "weeks_text": "1-4周, 6-10周, 13-14周",
                                "location": "实验1217",
                                "teacher": "王钰晨",
                                "confidence": 0.9,
                            }
                        ],
                    },
                },
            ),
            {},
            agent_run_id=run.id,
            capture_id=capture.id,
            sender_id="ou_test",
        )
    )

    planned = result.tool_results[0]
    assert planned["ok"] is True
    assert result.confirmation_id
    confirmation = store.get_confirmation(result.confirmation_id)
    assert confirmation.confirmation_type == "course_timetable_schedule"
    draft = store.list_plan_drafts(sender_id="ou_test", kinds=["course_timetable"])[0]
    assert draft.status.value == "schedule_pending"
    assert draft.payload["term_anchor"]["inferred_week1_monday"] == "2026-03-09"
    planned_events = confirmation.proposed_tool_calls_json[0]["arguments"]["planned_events"]
    assert [event["start_at"][:10] for event in planned_events] == ["2026-06-05", "2026-06-12"]
    assert all(event["plan_draft_id"] == draft.id for event in planned_events)

    confirmed = asyncio.run(orchestrator.router.resolve_confirmation(sender_id="ou_test", confirmation_id=result.confirmation_id))

    assert confirmed["status"] == "resolved"
    events = store.list_calendar_events()
    assert len(events) == 2
    assert all(event.plan_draft_id == draft.id for event in events)
    assert all(event.plan_item_id == "course_1" for event in events)
    assert len(feishu.synced_calendar_events) == 2
    assert store.get_plan_draft(draft.id).status.value == "confirmed"


def test_course_timetable_correction_cancels_wrong_pending_confirmation(tmp_path):
    orchestrator, store, _ = build_orchestrator(tmp_path)
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})
    wrong = store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="time_budget_calendar",
        proposed_tool_calls_json=[
            AgentToolCall(
                tool_name="create_calendar_event_candidate",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={
                    "title": "量子力学学习",
                    "start_at": "2026-06-05T08:00:00+08:00",
                    "end_at": "2026-06-05T09:40:00+08:00",
                },
            ).model_dump(mode="json")
        ],
        sender_id="ou_test",
    )

    result = asyncio.run(process(orchestrator, "不是学习任务，这个是我的课程表", "mid_course_correct"))

    assert result.confirmation_id
    assert store.get_confirmation(wrong.id).status == ConfirmationStatus.canceled
    assert store.get_confirmation(result.confirmation_id).confirmation_type == "course_timetable_refinement"


def test_cancel_schedule_block_confirm_sets_status_canceled(tmp_path):
    orchestrator, store, feishu = build_orchestrator(tmp_path)
    block = store.create_schedule_block(
        {
            "title": "周六驾校",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=SA",
            "start_time": "12:10",
            "end_time": "21:10",
            "timezone": "Asia/Shanghai",
            "feishu_event_id": "evt_block_cancel",
        }
    )
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})
    confirmation = store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="cancel_schedule_block",
        proposed_tool_calls_json=[
            AgentToolCall(
                tool_name="cancel_schedule_block",
                risk_level=RiskLevel.high,
                requires_confirmation=True,
                arguments={"schedule_block_id": block.id, "query": "周六驾校"},
            ).model_dump(mode="json")
        ],
        sender_id="ou_test",
    )

    result = asyncio.run(orchestrator.router.resolve_confirmation(sender_id="ou_test", confirmation_id=confirmation.id))

    assert result["status"] == "resolved"
    assert "取消日程安排" in result["reply_text"]
    assert "固定安排" not in result["reply_text"]
    assert store.get_schedule_block(block.id).status.value == "canceled"
    assert feishu.synced_calendar_events[-1]["operation"] == "delete_schedule_block"
    assert feishu.synced_calendar_events[-1]["deleted_event_id"] == "evt_block_cancel"


def test_backend_does_not_override_model_plan_intent_with_availability_keywords():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="query_tomorrow_plan", confidence=0.8, entities={"day": "tomorrow"}),
        {"raw_text": "明天我都啥时间有空？"},
    )

    assert response.intent == "query_tomorrow"
    assert response.tool_calls[0].tool_name == "query_tomorrow"


def test_model_intent_allows_null_optional_fields():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="query_week_plan", confidence=0.76, reply=None, entities=None, reasoning_summary=None),
        {"raw_text": "后天有什么任务"},
    )

    assert response.intent == "query_week"
    assert response.reply_to_user


def test_model_intent_normalizes_common_query_aliases():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="query_plan", confidence=0.76, reply=None, entities={}, reasoning_summary=None),
        {"raw_text": "后天有什么任务"},
    )

    assert response.intent == "query_week"
    assert response.tool_calls[0].tool_name == "query_week"


def test_after_tomorrow_availability_is_supported(tmp_path):
    orchestrator, _, _ = build_orchestrator(tmp_path)
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    response = provider._intent_to_agent_response(
        ModelIntent(intent="query_availability", confidence=0.9, entities={}),
        {"raw_text": "后天我什么时间有空"},
    )
    assert response.tool_calls[0].arguments["day"] == "after_tomorrow"
    assert orchestrator.router._target_day("after_tomorrow").date() == (effective_now(TZ) + timedelta(days=2)).date()


def test_invalid_model_intent_json_does_not_write_state(tmp_path):
    class InvalidJsonProvider(OpenAICompatibleChatProvider):
        name = "invalid_json_provider"

        def _post_chat_completion(self, payload, *, fallback_payload=None):  # noqa: ANN001, ANN202
            return {"choices": [{"message": {"content": "{not-json"}}]}

    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    feishu = MockFeishuNativeAdapter()
    provider = InvalidJsonProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    orchestrator = CoreAgentOrchestrator(store, provider, feishu, TZ)

    result = asyncio.run(process(orchestrator, "周五前给学生A出一套电学计算题，预计40分钟", "mid_bad_model"))
    run = store.list_agent_runs(limit=1)[0]

    assert result.agent_run_id == run.id
    assert run.status.value == "done"
    assert run.output_json["intent"] == "unknown"
    assert store.list_action_items() == []
    assert store.list_pending_confirmations("ou_test") == []
    assert feishu.sent_cards == []
    assert "没有写入任何数据" in feishu.sent_texts[-1]["text"]


def test_v2_router_registered_once():
    app = create_app()
    paths = [getattr(route, "path", "") for route in app.routes]
    assert paths.count("/api/v2/feishu/events") == 1
    assert paths.count("/api/v2/feishu/card") == 1


def test_project_state_declares_lm_studio_provider_is_active():
    text = Path("docs/PROJECT_STATE.md").read_text(encoding="utf-8")
    assert "CORE_AGENT_PROVIDER=lm_studio_provider" in text
    assert "LM Studio" in text
    assert "mock_provider" in text


def test_core_provider_can_select_lm_studio_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "lm_studio_provider")
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("LM_STUDIO_MODEL", "local-test-model")
    monkeypatch.setenv("LM_STUDIO_RESPONSE_FORMAT", "none")
    reset_dependencies()
    provider = get_core_provider()
    assert isinstance(provider, LmStudioProvider)
    assert provider.model == "local-test-model"


def test_v2_local_endpoint_works_with_mock_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    reset_dependencies()
    app = create_app()
    client = TestClient(app)
    response = client.post("/api/v2/agent/messages", json={"raw_text": "今天还有什么任务？"})
    assert response.status_code == 200
    assert "reply_text" in response.json()
