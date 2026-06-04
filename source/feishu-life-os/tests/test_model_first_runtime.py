from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings
from app.core.decision_provider import ModelDecisionProvider
from app.core.decision_schemas import (
    AssistantDecision,
    ConcreteOperation,
    ConfirmationAction,
    ProposalPatch,
)
from app.core.feishu_native import MockFeishuNativeAdapter
from app.core.observability import SQLiteTraceEmitter, SQLiteTraceStore
from app.core.orchestrator import CoreAgentOrchestrator
from app.core.planner_runtime import PlannerRuntime
from app.core.schemas import (
    AgentResponse,
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


class DecisionScriptProvider:
    name = "decision_script_provider"
    model = "scripted-decision"
    last_used_legacy_adapter = False

    def __init__(self, decisions: AssistantDecision | list[AssistantDecision]):
        self.decisions = decisions if isinstance(decisions, list) else [decisions]
        self.run_called = False

    def run(self, request: dict) -> AgentResponse:
        self.run_called = True
        raise AssertionError("model_first runtime must not call legacy provider.run")

    def run_decision(self, request: dict) -> AssistantDecision:
        self.last_used_legacy_adapter = False
        return self.decisions.pop(0)


class LegacyOnlyProvider:
    name = "legacy_only_provider"
    model = "legacy-script"

    def run(self, request: dict) -> AgentResponse:
        return AgentResponse(
            intent="smalltalk",
            confidence=0.72,
            reasoning_summary="legacy provider output",
            reply_to_user="legacy reply",
            tool_calls=[],
        )


class FailingLegacyAdapter:
    async def plan_response(self, *args, **kwargs):
        raise AssertionError("PlannerRuntime must not call legacy raw-text parser for native decisions")


def build_store(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    return repo, store


def build_model_first_orchestrator(tmp_path, decision: AssistantDecision, *, trace_enabled: bool = False):
    repo, store = build_store(tmp_path)
    feishu = MockFeishuNativeAdapter()
    trace_store = SQLiteTraceStore(repo)
    trace_store.migrate()
    trace = SQLiteTraceEmitter(trace_store) if trace_enabled else None
    provider = DecisionScriptProvider(decision)
    orchestrator = CoreAgentOrchestrator(
        store,
        provider,
        feishu,
        TZ,
        trace_emitter=trace,
        runtime_mode="model_first",
        decision_provider=provider,
    )
    return orchestrator, store, feishu, provider, trace_store


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


def create_active_proposal(store: StateStore) -> str:
    proposal = AssistantProposal(
        kind=PlanDraftKind.long_term_schedule,
        status=PlanDraftStatus.refining,
        user_goal="长期复习数学",
        context_summary="用户想要长期复习。",
        ai_assumptions=["确认前不写入日历"],
        missing_info=["频率", "每次时长"],
        candidate_plans=[{"title": "复习草案", "details": {"subject": "数学"}}],
        risks=["需要确认时间"],
        next_step_suggestion="请补充频率和时长。",
        confidence=0.63,
    )
    draft = store.create_plan_draft(
        kind=PlanDraftKind.long_term_schedule.value,
        title=proposal.user_goal,
        payload={"assistant_proposal": proposal.model_dump(mode="json"), "raw_text_history": ["长期复习数学"]},
        missing_fields=list(proposal.missing_info),
        status=PlanDraftStatus.refining.value,
        sender_id="ou_test",
        confidence=proposal.confidence,
    )
    return draft.id


def test_core_agent_runtime_mode_defaults_to_legacy():
    assert Settings(_env_file=None).core_agent_runtime_mode == "legacy"


def test_model_first_reply_sends_text_without_state_writes(tmp_path):
    decision = AssistantDecision(action="reply", confidence=0.9, reply_to_user="只是回复，不写入。")
    orchestrator, store, feishu, provider, _ = build_model_first_orchestrator(tmp_path, decision)

    result = asyncio.run(process(orchestrator, "聊一下系统状态"))

    assert result.reply_text == "只是回复，不写入。"
    assert feishu.sent_texts[-1]["text"] == "只是回复，不写入。"
    assert provider.run_called is False
    assert store.list_action_items() == []
    assert store.list_calendar_events() == []
    assert store.list_plan_drafts() == []
    run = store.get_agent_run(result.agent_run_id)
    assert run.output_json["runtime_mode"] == "model_first"
    assert run.output_json["assistant_decision"]["action"] == "reply"


def test_model_first_explain_proposal_does_not_mutate_active_draft(tmp_path):
    decision = AssistantDecision(
        action="explain_proposal",
        confidence=0.91,
        reply_to_user="这张卡只是草案，我可以逐项解释。",
        referenced_context=["plan_draft:active"],
    )
    orchestrator, store, _feishu, _provider, _trace_store = build_model_first_orchestrator(tmp_path, decision)
    plan_id = create_active_proposal(store)
    before = store.get_plan_draft(plan_id).model_dump(mode="json")

    result = asyncio.run(process(orchestrator, "你这个候选计划我看不懂", "explain"))
    after = store.get_plan_draft(plan_id).model_dump(mode="json")

    assert result.reply_text == "这张卡只是草案，我可以逐项解释。"
    assert after["payload"] == before["payload"]
    assert after["missing_fields"] == before["missing_fields"]
    assert after["status"] == before["status"]
    assert after["confidence"] == before["confidence"]


def test_model_first_refine_proposal_applies_only_explicit_patch(tmp_path):
    orchestrator, store, _feishu, _provider, _trace_store = build_model_first_orchestrator(
        tmp_path,
        AssistantDecision(
            action="refine_proposal",
            confidence=0.89,
            proposal_patch=ProposalPatch(
                plan_draft_id="placeholder",
                patch_type="merge_fields",
                fields={"details": {"preferred_time": "20:00", "session_minutes": 30}},
                missing_info=[],
                user_visible_summary="已按显式字段更新。",
                confidence=0.89,
            ),
        ),
    )
    plan_id = create_active_proposal(store)
    orchestrator.decision_provider.decisions[0].proposal_patch.plan_draft_id = plan_id

    result = asyncio.run(process(orchestrator, "每天晚上8点30分钟，先一个月", "refine"))

    assert result.proposal_id == plan_id
    draft = store.get_plan_draft(plan_id)
    proposal = draft.payload["assistant_proposal"]
    details = proposal["candidate_plans"][0]["details"]
    assert details["preferred_time"] == "20:00"
    assert details["session_minutes"] == 30
    dumped = str(draft.payload)
    assert "latest_user_reply" not in dumped
    assert "每天晚上8点30分钟" not in dumped
    assert draft.status == PlanDraftStatus.ready_for_schedule


def test_model_first_prepare_tool_confirmation_creates_confirmation_without_writes(tmp_path):
    start_at = datetime.now(TZ).replace(microsecond=0) + timedelta(days=1)
    end_at = start_at + timedelta(hours=1)
    decision = AssistantDecision(
        action="prepare_tool_confirmation",
        confidence=0.92,
        reply_to_user="请确认后再写入。",
        candidate_operations=[
            ConcreteOperation(
                operation="create_calendar_event",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={
                    "title": "复习数学",
                    "start_at": start_at.isoformat(),
                    "end_at": end_at.isoformat(),
                    "confidence": 0.9,
                },
            )
        ],
    )
    orchestrator, store, _feishu, _provider, _trace_store = build_model_first_orchestrator(tmp_path, decision)

    result = asyncio.run(process(orchestrator, "安排明天复习数学", "confirmable"))

    assert result.confirmation_id
    assert store.list_calendar_events() == []
    pending = store.list_pending_confirmations(sender_id="ou_test")
    assert pending[0].id == result.confirmation_id
    assert pending[0].proposed_tool_calls_json[0]["tool_name"] == "create_calendar_event_candidate"


def test_model_first_resolve_confirmation_uses_existing_toolrouter_boundary(tmp_path):
    repo, store = build_store(tmp_path)
    feishu = MockFeishuNativeAdapter()
    call = AgentToolCall(
        tool_name="create_task_candidate",
        risk_level=RiskLevel.medium,
        requires_confirmation=True,
        arguments={"title": "确认后创建的任务", "confidence": 0.8},
    )
    confirmation = store.create_confirmation(
        agent_run_id=None,
        confirmation_type="task_candidate",
        proposed_tool_calls_json=[call.model_dump(mode="json")],
        sender_id="ou_test",
    )
    decision = AssistantDecision(
        action="resolve_confirmation",
        confidence=0.93,
        confirmation_action=ConfirmationAction(action="confirm", confirmation_id=confirmation.id),
    )
    provider = DecisionScriptProvider(decision)
    orchestrator = CoreAgentOrchestrator(
        store,
        provider,
        feishu,
        TZ,
        runtime_mode="model_first",
        decision_provider=provider,
    )

    result = asyncio.run(process(orchestrator, "确认", "resolve"))

    assert result.confirmation_id is None
    assert len(store.list_action_items()) == 1
    assert store.get_confirmation(confirmation.id).status.value == "resolved"
    assert result.tool_results[0]["tool_name"] == "resolve_confirmation"


def test_model_first_query_with_write_operation_is_rejected(tmp_path):
    decision = AssistantDecision(
        action="query",
        confidence=0.81,
        candidate_operations=[
            ConcreteOperation(
                operation="create_task",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={"title": "不应该创建"},
            )
        ],
    )
    orchestrator, store, feishu, _provider, _trace_store = build_model_first_orchestrator(tmp_path, decision)

    result = asyncio.run(process(orchestrator, "查一下今天", "bad_query"))

    assert result.reply_text == "模型决策无法安全执行，已记录但不会写入任何数据。"
    assert feishu.sent_texts[-1]["text"] == result.reply_text
    assert store.list_action_items() == []
    assert store.list_calendar_events() == []
    run = store.get_agent_run(result.agent_run_id)
    assert run.status.value == "failed"


def test_model_first_trace_has_semantic_authority_attrs(tmp_path):
    decision = AssistantDecision(action="reply", confidence=0.9, reply_to_user="ok")
    orchestrator, _store, _feishu, _provider, trace_store = build_model_first_orchestrator(
        tmp_path,
        decision,
        trace_enabled=True,
    )

    asyncio.run(process(orchestrator, "hello", "trace_native"))

    traces = trace_store.list_traces()
    assert len(traces) == 1
    attrs = traces[0].attrs
    assert attrs["semantic_authority"] == "model"
    assert attrs["decision.action"] == "reply"
    assert attrs["backend_semantic_fallback_used"] is False
    assert attrs["legacy_planner_adapter_used"] is False
    detail = trace_store.get_trace(traces[0].trace_id)
    assert {"model_planner.run", "decision_policy.validate", "planner_runtime.apply"}.issubset(
        {span.name for span in detail.spans}
    )


def test_model_first_wrapper_fallback_marks_legacy_adapter_used(tmp_path):
    repo, store = build_store(tmp_path)
    feishu = MockFeishuNativeAdapter()
    trace_store = SQLiteTraceStore(repo)
    trace_store.migrate()
    provider = LegacyOnlyProvider()
    orchestrator = CoreAgentOrchestrator(
        store,
        provider,
        feishu,
        TZ,
        trace_emitter=SQLiteTraceEmitter(trace_store),
        runtime_mode="model_first",
        decision_provider=ModelDecisionProvider(provider),
    )

    result = asyncio.run(process(orchestrator, "hello", "trace_legacy"))

    assert result.reply_text == "legacy reply"
    trace = trace_store.list_traces()[0]
    assert trace.attrs["semantic_authority"] == "model"
    assert trace.attrs["backend_semantic_fallback_used"] is False
    assert trace.attrs["legacy_planner_adapter_used"] is True


def test_planner_runtime_does_not_call_legacy_raw_text_parsers(tmp_path):
    _repo, store = build_store(tmp_path)
    feishu = MockFeishuNativeAdapter()
    runtime = PlannerRuntime(store, feishu, TZ, legacy_adapter=FailingLegacyAdapter())
    plan_id = create_active_proposal(store)
    decision = AssistantDecision(
        action="refine_proposal",
        confidence=0.88,
        proposal_patch=ProposalPatch(
            plan_draft_id=plan_id,
            patch_type="merge_fields",
            fields={"details": {"preferred_time": "07:30"}},
            missing_info=["每次时长"],
            user_visible_summary="只更新偏好时间。",
            confidence=0.88,
        ),
    )

    outcome = asyncio.run(
        runtime.apply_decision(
            decision,
            {"raw_text": "每天晚上8点跑步30分钟，先一个月"},
            agent_run_id="arun_test",
            capture_id="cap_test",
            sender_id="ou_test",
        )
    )

    draft = store.get_plan_draft(plan_id)
    assert outcome.proposal_id == plan_id
    assert draft.payload["assistant_proposal"]["candidate_plans"][0]["details"]["preferred_time"] == "07:30"
    assert "每天晚上8点跑步30分钟" not in str(draft.payload)
