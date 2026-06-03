from __future__ import annotations

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from app.core.context import ContextCompiler
from app.core.context.budget import fit_provider_request, json_size
from app.core.providers import OpenAICompatibleChatProvider
from app.core.relative_time import effective_day_start
from app.core.schemas import (
    AgentToolCall,
    AssistantProposal,
    CaptureIn,
    PlanDraftKind,
    PlanDraftStatus,
    RiskLevel,
)
from app.core.store import StateStore
from app.database import Repository

TZ = ZoneInfo("Asia/Shanghai")


def build_store(tmp_path) -> StateStore:
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    return store


def compile_request(store: StateStore, raw_text: str = "confirm") -> dict:
    capture = store.create_capture(
        CaptureIn(
            source="test",
            source_message_id=f"msg_{raw_text[:8]}",
            sender_id="ou_test",
            chat_id="chat_test",
            raw_text=raw_text,
        )
    )
    return ContextCompiler(store, TZ).compile(capture.model_dump(mode="json")).provider_request(max_bytes=12_000)


def capsule_by_domain(request: dict, domain: str) -> dict:
    for capsule in request["context_v2"]["capsules"]:
        if capsule["domain"] == domain:
            return capsule
    raise AssertionError(f"missing capsule domain {domain}")


def test_compiler_generates_legacy_and_v2_request(tmp_path):
    store = build_store(tmp_path)

    request = compile_request(store, "what is free tomorrow")

    assert request["context_schema_version"] == 1
    assert request["context_v2"]["context_schema_version"] == 2
    assert request["context_v2"]["current_message"]["raw_text"] == "what is free tomorrow"
    assert request["context_v2"]["capsules"]
    assert json_size(request) <= 12_000


def test_pending_confirmation_capsule_is_summary_only(tmp_path):
    store = build_store(tmp_path)
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})
    store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="create_candidates",
        proposed_tool_calls_json=[
            AgentToolCall(
                tool_name="create_task_candidate",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={"title": "Write exercise set", "description": "x" * 10_000},
            ).model_dump(mode="json")
        ],
        sender_id="ou_test",
    )

    request = compile_request(store, "confirm")
    dumped = json.dumps(request, ensure_ascii=False)
    capsule = capsule_by_domain(request, "confirmation")

    assert "facts" not in capsule
    assert "Write exercise set" in capsule["summary"]
    assert "proposed_tool_calls_json" not in dumped
    assert "x" * 100 not in dumped


def test_ordinary_confirm_includes_confirmation_but_no_schedule_capsule(tmp_path):
    store = build_store(tmp_path)
    run = store.create_agent_run(capture_id=None, provider="test", model=None, input_json={})
    store.create_confirmation(
        agent_run_id=run.id,
        confirmation_type="create_candidates",
        proposed_tool_calls_json=[
            AgentToolCall(
                tool_name="create_task_candidate",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={"title": "Write report"},
            ).model_dump(mode="json")
        ],
        sender_id="ou_test",
    )
    store.create_schedule_block(
        {
            "title": "Fixed class",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
            "start_time": "08:00",
            "end_time": "10:00",
            "timezone": "Asia/Shanghai",
        }
    )

    request = compile_request(store, "confirm")
    domains = {capsule["domain"] for capsule in request["context_v2"]["capsules"]}

    assert "confirmation" in domains
    assert "schedule" not in domains


def test_active_plan_draft_capsule_contains_proposal_summary(tmp_path):
    store = build_store(tmp_path)
    proposal = AssistantProposal(
        kind=PlanDraftKind.habit,
        status=PlanDraftStatus.refining,
        user_goal="exercise for health",
        missing_info=["duration", "preferred time"],
        next_step_suggestion="Ask for duration and preferred time.",
    )
    draft = store.create_plan_draft(
        kind=PlanDraftKind.habit.value,
        status=PlanDraftStatus.refining.value,
        title="Health habit",
        payload={"assistant_proposal": proposal.model_dump(mode="json"), "planned_events": [{"title": "preview"}]},
        missing_fields=["duration"],
        sender_id="ou_test",
    )

    request = compile_request(store, "30 minutes at night")
    capsule = capsule_by_domain(request, "plan_draft")

    assert capsule["facts"][0]["plan_draft_id"] == draft.id
    assert capsule["facts"][0]["kind"] == PlanDraftKind.habit.value
    assert capsule["facts"][0]["assistant_proposal"]["user_goal"] == "exercise for health"
    assert "duration" in capsule["missing_info"]
    assert capsule["facts"][0]["planned_event_count"] == 1


def test_schedule_availability_capsule_includes_events_and_byday_blocks(tmp_path):
    store = build_store(tmp_path)
    base = effective_day_start(TZ)
    target = base + timedelta(days=1)
    day_code = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][target.weekday()]
    store.create_calendar_event(
        {
            "title": "Math class",
            "start_at": target.replace(hour=9, minute=0),
            "end_at": target.replace(hour=10, minute=0),
        }
    )
    store.create_schedule_block(
        {
            "title": "Fixed family time",
            "recurrence_rule": f"FREQ=WEEKLY;BYDAY={day_code}",
            "start_time": "13:00",
            "end_time": "14:00",
            "timezone": "Asia/Shanghai",
        }
    )

    request = compile_request(store, "free time tomorrow")
    capsule = capsule_by_domain(request, "schedule")
    target_fact = next(item for item in capsule["facts"] if item["date"] == target.date().isoformat())
    busy_titles = " ".join(item["title"] for item in target_fact["busy"])

    assert "Math class" in busy_titles
    assert "Fixed family time" in busy_titles
    assert target_fact["free_count"] >= 1


def test_availability_query_exposes_compact_schedule_facts_to_provider_context(tmp_path):
    store = build_store(tmp_path)
    base = effective_day_start(TZ)
    store.create_calendar_event(
        {
            "title": "Dentist appointment",
            "start_at": base.replace(hour=15, minute=0),
            "end_at": base.replace(hour=16, minute=0),
        }
    )
    request = compile_request(store, "am I free this afternoon")
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")

    context = provider._entity_context(request, "query_availability")
    capsule = next(item for item in context["context_capsules"] if item["domain"] == "schedule")
    today_fact = next(item for item in capsule["facts"] if item["date"] == base.date().isoformat())

    assert today_fact["busy"]
    assert today_fact["free"]
    assert "Dentist appointment" in " ".join(item.get("title", "") for item in today_fact["busy"])


def test_schedule_facts_are_visible_for_allowed_entity_intent():
    provider = OpenAICompatibleChatProvider(base_url="http://127.0.0.1:9/v1", model="test", response_format="none")
    request = {
        "raw_text": "book a meeting tomorrow",
        "context_v2": {
            "capsules": [
                {
                    "capsule_id": "cap_schedule_availability_7d",
                    "domain": "schedule",
                    "purpose": "general",
                    "summary": "Availability summary",
                    "facts": [
                        {
                            "date": "2026-06-04",
                            "weekday": "TH",
                            "busy_count": 1,
                            "free_count": 2,
                            "busy": [{"start": "2026-06-04T10:00:00+08:00", "end": "2026-06-04T11:00:00+08:00", "title": "Class"}],
                            "free": [{"start": "2026-06-04T11:00:00+08:00", "end": "2026-06-04T12:00:00+08:00"}],
                        }
                    ],
                }
            ]
        },
    }

    intent_context = provider._intent_context(request)
    entity_context = provider._entity_context(request, "create_calendar_event")

    assert intent_context["context_capsules"] == []
    assert entity_context["context_capsules"][0]["facts"][0]["busy"][0]["title"] == "Class"


def test_non_schedule_smalltalk_keeps_context_v2_small(tmp_path):
    store = build_store(tmp_path)
    for index in range(20):
        store.create_schedule_block(
            {
                "title": f"Fixed block {index}",
                "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
                "start_time": "08:00",
                "end_time": "09:00",
                "timezone": "Asia/Shanghai",
            }
        )

    request = compile_request(store, "hello")
    domains = {capsule["domain"] for capsule in request["context_v2"]["capsules"]}

    assert "schedule" not in domains
    assert json_size(request["context_v2"]) < 2_500


def test_provider_request_budget_trims_v2_before_legacy():
    request = {
        "context_schema_version": 1,
        "raw_text": "short",
        "context_v2": {
            "context_schema_version": 2,
            "capsules": [
                {
                    "domain": "plan_draft",
                    "purpose": "test",
                    "summary": "x" * 2_000,
                    "facts": [{"payload": "y" * 2_000}],
                    "evidence_refs": [{"kind": "plan_draft", "id": f"plan_{index}"}],
                }
                for index in range(10)
            ],
        },
    }

    fitted = fit_provider_request(request, max_bytes=1_000)

    assert fitted["context_schema_version"] == 1
    assert fitted["raw_text"] == "short"
    assert json_size(fitted) <= 1_000
