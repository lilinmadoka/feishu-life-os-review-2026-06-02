from __future__ import annotations

import json
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

from app.core.context import ContextCompiler
from app.core.feishu_native import FeishuNativeAdapter
from app.core.observability import NullTraceEmitter, TraceEmitter
from app.core.planner import PlannerService
from app.core.policy import PolicyViolation, RiskPolicy
from app.core.providers import (
    CoreAgentProvider,
    CoreAgentProviderError,
    CoreAgentProviderUnavailable,
)
from app.core.schemas import CaptureIn, OrchestratorResult, ProcessedStatus
from app.core.store import StateStore
from app.core.tools import ToolRouter

LOGGER = logging.getLogger("lifeos.agent_runtime")


class CoreAgentOrchestrator:
    def __init__(
        self,
        store: StateStore,
        provider: CoreAgentProvider,
        feishu: FeishuNativeAdapter,
        tz: ZoneInfo,
        trace_emitter: TraceEmitter | None = None,
    ):
        self.store = store
        self.provider = provider
        self.feishu = feishu
        self.tz = tz
        self.trace = trace_emitter or NullTraceEmitter()
        self.router = ToolRouter(store, feishu, tz)
        self.planner = PlannerService(store, feishu, tz, self.router)
        self.policy = RiskPolicy()
        self.context_compiler = ContextCompiler(store, tz)

    async def process_capture(self, capture_input: CaptureIn) -> OrchestratorResult:
        trace = self.trace.start_trace(
            workflow_type=self._workflow_type(capture_input.source),
            sender_id=capture_input.sender_id,
            attrs={
                "source": capture_input.source,
                "content_type": capture_input.content_type,
                "raw_text": capture_input.raw_text,
                "source_message_id": capture_input.source_message_id,
            },
        )
        trace_id = trace.trace_id
        try:
            self.store.migrate()
            with self.trace.span(trace_id, "capture.lookup", component="orchestrator", lane="ingest") as lookup_span:
                existing = self.store.find_capture_by_source_message(capture_input.source, capture_input.source_message_id)
                lookup_span.add_attrs({"found": bool(existing), "source": capture_input.source})
            if existing and existing.processed_status != ProcessedStatus.failed:
                self.trace.update_trace(
                    trace_id,
                    capture_id=existing.id,
                    root_entity_type="capture",
                    root_entity_id=existing.id,
                    attrs={"duplicate_ignored": True},
                )
                self._log_runtime(
                    {
                        "status": "duplicate_ignored",
                        "capture_id": existing.id,
                        "event_id": capture_input.source_event_id,
                        "message_id": capture_input.source_message_id,
                        "provider_name": self.provider.name,
                        "agent_run_id": None,
                        "used_fallback": self._is_fallback_provider(),
                        "tool_calls": [],
                        "reply_text": "",
                    }
                )
                self.trace.end_trace(trace_id, status="ok", summary="duplicate ignored", attrs={"capture_id": existing.id})
                return OrchestratorResult(capture_id=existing.id, agent_run_id="", reply_text="重复消息已忽略。")

            with self.trace.span(trace_id, "capture.create", component="orchestrator", lane="ingest") as capture_span:
                capture = self.store.create_capture(capture_input)
                capture_span.add_attrs({"capture_id": capture.id})
            self.trace.update_trace(trace_id, capture_id=capture.id, root_entity_type="capture", root_entity_id=capture.id)

            with self.trace.span(trace_id, "context.compile", component="context", lane="context") as context_span:
                compiled_context = self.context_compiler.compile(capture.model_dump(mode="json"))
                request = compiled_context.provider_request(max_bytes=self.context_compiler.max_bytes)
                context_v2 = request.get("context_v2") if isinstance(request.get("context_v2"), dict) else {}
                context_summary = self._context_observability_summary(compiled_context, request)
                context_span.add_attrs(context_summary["attrs"])
                self.trace.artifact(
                    trace_id,
                    kind="context_v2",
                    label="Context Lens summary",
                    redaction="summary_only",
                    payload_json=context_summary["artifact"],
                )

            run = self.store.create_agent_run(
                capture_id=capture.id,
                provider=self.provider.name,
                model=getattr(self.provider, "model", None),
                input_json=request,
            )
            self.trace.update_trace(trace_id, agent_run_id=run.id)
            started = time.perf_counter()

            try:
                with self.trace.span(
                    trace_id,
                    "provider.run",
                    component="provider",
                    lane="model",
                    attrs={"provider_name": self.provider.name, "model": getattr(self.provider, "model", None)},
                ) as provider_span:
                    response = self.provider.run(request)
                    provider_span.add_attrs(
                        {
                            "intent": response.intent,
                            "confidence": response.confidence,
                            "tool_call_count": len(response.tool_calls),
                            "tool_names": [call.tool_name for call in response.tool_calls],
                            "has_assistant_proposal": response.assistant_proposal is not None,
                        }
                    )
                    self.trace.artifact(
                        trace_id,
                        kind="provider_output",
                        label="Provider output summary",
                        redaction="summary_only",
                        payload_json=self._provider_output_summary(response),
                    )
                with self.trace.span(trace_id, "policy.validate_response", component="policy", lane="guard") as policy_span:
                    self.policy.validate_response(response)
                    policy_span.add_attrs({"intent": response.intent, "tool_call_count": len(response.tool_calls)})
                    self.trace.event(
                        trace_id,
                        name="policy.response_validated",
                        attrs={"intent": response.intent, "tool_names": [call.tool_name for call in response.tool_calls]},
                    )
            except (CoreAgentProviderUnavailable, CoreAgentProviderError, PolicyViolation) as exc:
                if isinstance(exc, PolicyViolation):
                    self.trace.event(trace_id, name="policy.violation", level="warn", message=str(exc))
                latency_ms = int((time.perf_counter() - started) * 1000)
                self.store.fail_agent_run(run.id, str(exc), latency_ms)
                self.store.update_capture_status(capture.id, ProcessedStatus.needs_review)
                reply = "智能处理器不可用或返回结果不符合安全策略，已记录消息但不会自动处理。"
                with self.trace.span(trace_id, "final_reply.complete_run", component="orchestrator", lane="state"):
                    await self.feishu.send_text(capture.sender_id, reply)
                self._log_runtime(
                    {
                        "status": "failed",
                        "capture_id": capture.id,
                        "event_id": capture.source_event_id,
                        "message_id": capture.source_message_id,
                        "provider_name": self.provider.name,
                        "intent": None,
                        "agent_run_id": run.id,
                        "used_fallback": self._is_fallback_provider(),
                        "tool_calls": [],
                        "reply_text": reply,
                        "error": str(exc),
                    }
                )
                self.trace.end_trace(trace_id, status="failed", summary=str(exc), attrs={"capture_id": capture.id, "agent_run_id": run.id})
                return OrchestratorResult(capture_id=capture.id, agent_run_id=run.id, reply_text=reply)

            with self.trace.span(trace_id, "planner.plan_response", component="planner", lane="planner") as planner_span:
                planning = await self.planner.plan_response(
                    response,
                    request,
                    agent_run_id=run.id,
                    capture_id=capture.id,
                    sender_id=capture.sender_id,
                )
                planner_span.add_attrs(
                    {
                        "proposal_id": planning.proposal_id,
                        "confirmation_id": planning.confirmation_id,
                        "tool_call_count": len(planning.tool_calls),
                        "tool_names": [call.tool_name for call in planning.tool_calls],
                        "card_sent": planning.card_sent,
                    }
                )
                self.trace.artifact(
                    trace_id,
                    kind="planner",
                    label="Planner outcome summary",
                    redaction="summary_only",
                    payload_json={
                        "proposal_id": planning.proposal_id,
                        "confirmation_id": planning.confirmation_id,
                        "tool_calls": self._tool_call_summary(planning.tool_calls),
                        "tool_results": self._tool_result_summary(planning.tool_results),
                        "reply_text": planning.reply_text,
                        "card_sent": planning.card_sent,
                    },
                )
                if planning.proposal_id:
                    self.trace.state_diff(
                        trace_id,
                        entity_type="plan_draft",
                        entity_id=planning.proposal_id,
                        operation="upsert",
                        after_summary={"proposal_id": planning.proposal_id, "confirmation_id": planning.confirmation_id},
                    )
                if planning.confirmation_id:
                    self.trace.state_diff(
                        trace_id,
                        entity_type="confirmation",
                        entity_id=planning.confirmation_id,
                        operation="create",
                        after_summary={"status": "pending", "card_sent": planning.card_sent},
                    )

            tool_results = list(planning.tool_results)
            tool_reply = planning.reply_text
            confirmation_id = planning.confirmation_id
            executed_calls = list(planning.tool_calls)
            with self.trace.span(
                trace_id,
                "tool_router.execute_calls",
                component="tool_router",
                lane="execute",
                status="running" if executed_calls else "skipped",
                attrs={"tool_call_count": len(executed_calls), "tool_names": [call.tool_name for call in executed_calls]},
            ) as router_span:
                if executed_calls:
                    routed_results, routed_reply, routed_confirmation_id = await self.router.execute_calls(
                        executed_calls,
                        agent_run_id=run.id,
                        capture_id=capture.id,
                        sender_id=capture.sender_id,
                    )
                    tool_results.extend(routed_results)
                    tool_reply = routed_reply or tool_reply
                    confirmation_id = routed_confirmation_id or confirmation_id
                    router_span.add_attrs({"result_count": len(routed_results), "confirmation_id": routed_confirmation_id})
                    self.trace.artifact(
                        trace_id,
                        kind="tool_results",
                        label="ToolRouter result summary",
                        redaction="summary_only",
                        payload_json={"results": self._tool_result_summary(routed_results), "reply_text": routed_reply},
                    )
                    if routed_confirmation_id:
                        self.trace.state_diff(
                            trace_id,
                            entity_type="confirmation",
                            entity_id=routed_confirmation_id,
                            operation="create",
                            after_summary={"status": "pending", "tool_call_count": len(executed_calls)},
                        )
                    self._emit_tool_result_state_diffs(trace_id, routed_results)

            final_reply = tool_reply or response.reply_to_user or "已收到。"
            with self.trace.span(trace_id, "final_reply.complete_run", component="orchestrator", lane="state") as final_span:
                direct_reply_sent = any(result.get("tool_name") == "send_feishu_reply" for result in tool_results)
                if not confirmation_id and not direct_reply_sent and not planning.card_sent:
                    await self.feishu.send_text(capture.sender_id, final_reply)
                latency_ms = int((time.perf_counter() - started) * 1000)
                self.store.complete_agent_run(
                    run.id,
                    output_json=response.model_dump(mode="json"),
                    tool_calls_json=[call.model_dump(mode="json") for call in executed_calls],
                    latency_ms=latency_ms,
                )
                self.store.update_capture_status(capture.id, ProcessedStatus.processed)
                final_span.add_attrs(
                    {
                        "latency_ms": latency_ms,
                        "confirmation_id": confirmation_id,
                        "direct_reply_sent": direct_reply_sent,
                        "reply_text": final_reply,
                    }
                )
                self.trace.state_diff(
                    trace_id,
                    entity_type="agent_run",
                    entity_id=run.id,
                    operation="complete",
                    after_summary={
                        "status": "done",
                        "latency_ms": latency_ms,
                        "tool_call_count": len(executed_calls),
                        "confirmation_id": confirmation_id,
                    },
                )

            self._log_runtime(
                {
                    "status": "done",
                    "capture_id": capture.id,
                    "event_id": capture.source_event_id,
                    "message_id": capture.source_message_id,
                    "provider_name": self.provider.name,
                    "intent": response.intent,
                    "agent_run_id": run.id,
                    "used_fallback": self._is_fallback_provider(),
                    "tool_calls": [call.model_dump(mode="json") for call in executed_calls],
                    "proposal_id": planning.proposal_id,
                    "reply_text": final_reply,
                }
            )
            self.trace.end_trace(
                trace_id,
                status="ok",
                summary=f"{response.intent or 'unknown'} processed",
                attrs={
                    "capture_id": capture.id,
                    "agent_run_id": run.id,
                    "provider_name": self.provider.name,
                    "model": getattr(self.provider, "model", None),
                    "intent": response.intent,
                    "confidence": response.confidence,
                    "tool_call_count": len(executed_calls),
                    "confirmation_id": confirmation_id,
                    "proposal_id": planning.proposal_id,
                    "capsule_count": len(context_v2.get("capsules") or []),
                },
            )
            return OrchestratorResult(
                capture_id=capture.id,
                agent_run_id=run.id,
                reply_text=final_reply,
                tool_results=tool_results,
                confirmation_id=confirmation_id,
                proposal_id=planning.proposal_id,
            )
        except Exception as exc:
            self.trace.end_trace(trace_id, status="failed", summary=str(exc))
            raise

    def _is_fallback_provider(self) -> bool:
        return self.provider.name in {"mock_provider", "rules_provider", "rules_fallback"}

    def _workflow_type(self, source: str) -> str:
        if source == "feishu":
            return "feishu_message"
        if source == "local_api":
            return "local_agent_message"
        return f"{source}_message" if source else "agent_message"

    def _build_agent_request(self, capture: dict[str, Any]) -> dict[str, Any]:
        return self.context_compiler.compile(capture).provider_request(max_bytes=self.context_compiler.max_bytes)

    def _log_runtime(self, payload: dict[str, Any]) -> None:
        LOGGER.info("agent_runtime %s", json.dumps(payload, ensure_ascii=False, default=str))

    def _context_observability_summary(self, compiled_context: Any, request: dict[str, Any]) -> dict[str, Any]:
        generated = [capsule.model_dump(mode="json") for capsule in compiled_context.v2_pack.capsules]
        context_v2 = request.get("context_v2") if isinstance(request.get("context_v2"), dict) else {}
        rendered = context_v2.get("capsules") if isinstance(context_v2.get("capsules"), list) else []
        rendered_ids = {str(item.get("capsule_id")) for item in rendered if isinstance(item, dict)}
        provider_request_bytes = len(json.dumps(request, ensure_ascii=False, default=str).encode("utf-8"))
        legacy_bytes = len(
            json.dumps(compiled_context.legacy_pack.model_dump(mode="json"), ensure_ascii=False, default=str).encode("utf-8")
        )
        v2_bytes = len(json.dumps(compiled_context.v2_pack.model_dump(mode="json"), ensure_ascii=False, default=str).encode("utf-8"))
        generated_fact_count = sum(len(item.get("facts") or []) for item in generated if isinstance(item, dict))
        rendered_fact_count = sum(len(item.get("facts") or []) for item in rendered if isinstance(item, dict))
        capsules = []
        for item in generated:
            capsule_id = str(item.get("capsule_id") or "")
            rendered_item = next((capsule for capsule in rendered if isinstance(capsule, dict) and capsule.get("capsule_id") == capsule_id), None)
            facts_total = len(item.get("facts") or [])
            facts_kept = len(rendered_item.get("facts") or []) if isinstance(rendered_item, dict) else 0
            capsules.append(
                {
                    "capsule_id": capsule_id,
                    "domain": item.get("domain"),
                    "generated": True,
                    "rendered": capsule_id in rendered_ids,
                    "trimmed": facts_kept < facts_total,
                    "facts_total": facts_total,
                    "facts_kept": facts_kept,
                    "facts_dropped": max(0, facts_total - facts_kept),
                    "evidence_refs": item.get("evidence_refs") or [],
                    "relevance_score": item.get("relevance_score"),
                    "confidence": item.get("confidence"),
                    "forbidden_actions": item.get("forbidden_actions") or [],
                    "skip_reason": "" if capsule_id in rendered_ids else "render_policy_or_budget",
                }
            )
        artifact = {
            "legacy_bytes": legacy_bytes,
            "context_v2_bytes": v2_bytes,
            "provider_request_bytes": provider_request_bytes,
            "render_policy": context_v2.get("context_trace", {}).get("capsule_render_policy", "provider_compact_v1"),
            "capsules_generated": len(generated),
            "capsules_rendered": len(rendered),
            "facts_kept": rendered_fact_count,
            "facts_dropped": max(0, generated_fact_count - rendered_fact_count),
            "compressors_run": compiled_context.v2_pack.context_trace.get("compressors", []),
            "capsules": capsules,
        }
        return {
            "attrs": {
                "legacy_bytes": legacy_bytes,
                "context_v2_bytes": v2_bytes,
                "provider_request_bytes": provider_request_bytes,
                "context_size_bytes": provider_request_bytes,
                "capsule_count": len(rendered),
                "capsules_generated": len(generated),
                "capsules_rendered": len(rendered),
                "facts_kept": rendered_fact_count,
                "facts_dropped": max(0, generated_fact_count - rendered_fact_count),
                "render_policy": artifact["render_policy"],
                "context_schema_version": request.get("context_schema_version"),
            },
            "artifact": artifact,
        }

    def _provider_output_summary(self, response: Any) -> dict[str, Any]:
        return {
            "intent": response.intent,
            "confidence": response.confidence,
            "reply_to_user": response.reply_to_user,
            "tool_names": [call.tool_name for call in response.tool_calls],
            "tool_call_count": len(response.tool_calls),
            "has_assistant_proposal": response.assistant_proposal is not None,
            "assistant_proposal": self._proposal_summary(response.assistant_proposal) if response.assistant_proposal else None,
        }

    def _proposal_summary(self, proposal: Any) -> dict[str, Any]:
        data = proposal.model_dump(mode="json")
        return {
            "kind": data.get("kind"),
            "status": data.get("status"),
            "missing_info": data.get("missing_info") or [],
            "candidate_count": len(data.get("candidate_plans") or []),
            "schedule_preview_count": len(data.get("schedule_preview") or []),
            "confidence": data.get("confidence"),
        }

    def _tool_call_summary(self, calls: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "tool_name": call.tool_name,
                "risk_level": call.risk_level,
                "requires_confirmation": call.requires_confirmation,
                "argument_keys": sorted(call.arguments.keys()),
            }
            for call in calls
        ]

    def _tool_result_summary(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summaries = []
        for result in results:
            summaries.append(
                {
                    "tool_name": result.get("tool_name"),
                    "ok": result.get("ok"),
                    "confirmation_id": result.get("confirmation_id"),
                    "created": self._created_entities_from_result(result),
                    "reply_text": result.get("reply_text"),
                    "error_class": type(result.get("error")).__name__ if result.get("error") else None,
                }
            )
        return summaries

    def _emit_tool_result_state_diffs(self, trace_id: str, results: list[dict[str, Any]]) -> None:
        for result in results:
            for entity in self._created_entities_from_result(result):
                self.trace.state_diff(
                    trace_id,
                    entity_type=entity["entity_type"],
                    entity_id=entity["entity_id"],
                    operation=entity["operation"],
                    after_summary=entity["summary"],
                )

    def _created_entities_from_result(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        entities: list[dict[str, Any]] = []
        for key, entity_type in (
            ("action_item", "action_item"),
            ("calendar_event", "calendar_event"),
            ("schedule_block", "schedule_block"),
            ("plan_draft", "plan_draft"),
        ):
            value = result.get(key)
            if isinstance(value, dict) and value.get("id"):
                entities.append(
                    {
                        "entity_type": entity_type,
                        "entity_id": str(value["id"]),
                        "operation": "create_or_update",
                        "summary": self._entity_summary(value),
                    }
                )
        confirmation_id = result.get("confirmation_id")
        if confirmation_id:
            entities.append(
                {
                    "entity_type": "confirmation",
                    "entity_id": str(confirmation_id),
                    "operation": "create_or_resolve",
                    "summary": {"confirmation_id": confirmation_id, "status": result.get("status")},
                }
            )
        return entities

    def _entity_summary(self, value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value.get(key)
            for key in (
                "id",
                "title",
                "status",
                "start_at",
                "end_at",
                "due_at",
                "start_time",
                "end_time",
                "recurrence_rule",
                "confidence",
            )
            if value.get(key) not in (None, "", [])
        }
