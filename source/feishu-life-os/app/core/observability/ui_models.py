from __future__ import annotations

from collections import Counter
from statistics import mean
from typing import Any

from app.core.observability.schemas import TraceDetail, TraceSpan

LANE_ORDER = ["ingest", "context", "model", "guard", "planner", "execute", "state", "external"]
STAGE_DEFINITIONS = [
    {
        "id": "ingest",
        "label": "接收",
        "description": "消息捕获、去重、入库",
        "components": {"orchestrator"},
        "lanes": {"ingest"},
        "span_names": {"capture.lookup", "capture.create"},
    },
    {
        "id": "context",
        "label": "上下文",
        "description": "Context Compiler 与 capsules 渲染",
        "components": {"context"},
        "lanes": {"context"},
        "span_names": {"context.compile"},
    },
    {
        "id": "model",
        "label": "模型",
        "description": "Provider 调用与结构化输出",
        "components": {"provider"},
        "lanes": {"model"},
        "span_names": {"provider.run"},
    },
    {
        "id": "guard",
        "label": "风控",
        "description": "RiskPolicy 校验与确认边界",
        "components": {"policy"},
        "lanes": {"guard"},
        "span_names": {"policy.validate_response"},
    },
    {
        "id": "planner",
        "label": "规划",
        "description": "PlanDraft/AssistantProposal 推进",
        "components": {"planner"},
        "lanes": {"planner"},
        "span_names": {"planner.plan_response"},
    },
    {
        "id": "execute",
        "label": "执行",
        "description": "ToolRouter 执行或生成确认卡",
        "components": {"tool_router"},
        "lanes": {"execute"},
        "span_names": {"tool_router.execute_calls"},
    },
    {
        "id": "reply",
        "label": "外部/回复",
        "description": "飞书发送、最终回复、状态落库",
        "components": {"feishu", "orchestrator"},
        "lanes": {"external", "state"},
        "span_names": {"final_reply.complete_run"},
    },
]


def build_timeline(detail: TraceDetail) -> dict[str, Any]:
    spans = sorted(detail.spans, key=lambda span: span.started_at)
    if not spans:
        return {
            "trace": detail.trace.model_dump(mode="json"),
            "trace_id": detail.trace.trace_id,
            "duration_ms": detail.trace.duration_ms or 0,
            "critical_path_ms": 0,
            "status_counts": {},
            "kpis": build_trace_kpis(detail),
            "lanes": [],
            "stages": _build_stages(detail, [], 1),
        }

    first_started = min(span.started_at for span in spans)
    last_ended = max((span.ended_at or span.started_at) for span in spans)
    computed_total_ms = max(1, int((last_ended - first_started).total_seconds() * 1000))
    total_ms = max(1, detail.trace.duration_ms or computed_total_ms)
    critical_path_ms = max(_span_duration_ms(span) for span in spans)
    lanes: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        start_ms = max(0, int((span.started_at - first_started).total_seconds() * 1000))
        duration_ms = _span_duration_ms(span)
        lanes.setdefault(span.lane, []).append(
            {
                "span_id": span.span_id,
                "name": span.name,
                "component": span.component,
                "lane": span.lane,
                "status": span.status,
                "start_ms": start_ms,
                "duration_ms": duration_ms,
                "relative_start_ms": start_ms,
                "relative_end_ms": start_ms + duration_ms,
                "offset_percent": round(start_ms / total_ms * 100, 2),
                "width_percent": max(0.8, round(max(1, duration_ms) / total_ms * 100, 2)),
                "is_critical_path": duration_ms == critical_path_ms,
                "attrs": span.attrs,
            }
        )

    ordered_lanes = sorted(lanes.items(), key=lambda item: _lane_index(item[0]))
    return {
        "trace": detail.trace.model_dump(mode="json"),
        "trace_id": detail.trace.trace_id,
        "duration_ms": total_ms,
        "critical_path_ms": critical_path_ms,
        "status_counts": dict(Counter(span.status for span in spans)),
        "kpis": build_trace_kpis(detail, critical_path_ms=critical_path_ms),
        "lanes": [{"name": lane, "lane": lane, "spans": lane_spans} for lane, lane_spans in ordered_lanes],
        "stages": _build_stages(detail, spans, total_ms),
    }


def build_graph(detail: TraceDetail) -> dict[str, Any]:
    spans = sorted(detail.spans, key=lambda span: span.started_at)
    nodes = [
        {
            "id": span.span_id,
            "label": span.name,
            "kind": "span",
            "lane": span.lane,
            "component": span.component,
            "status": span.status,
            "duration_ms": span.duration_ms,
            "attrs": span.attrs,
        }
        for span in spans
    ]
    edges: list[dict[str, Any]] = []
    for index, span in enumerate(spans):
        source = span.parent_span_id
        label = "parent"
        if source is None and index > 0:
            source = spans[index - 1].span_id
            label = "next"
        if source:
            edge = {"source": source, "target": span.span_id, "label": label}
            edge["from"] = source
            edge["to"] = span.span_id
            edges.append(edge)
    return {"trace_id": detail.trace.trace_id, "nodes": nodes, "edges": edges}


def build_artifacts(detail: TraceDetail) -> dict[str, Any]:
    return {
        "trace_id": detail.trace.trace_id,
        "artifacts": [artifact.model_dump(mode="json") for artifact in detail.artifacts],
        "state_diffs": [diff.model_dump(mode="json") for diff in detail.state_diffs],
        "events": [event.model_dump(mode="json") for event in detail.events],
    }


def build_trace_kpis(detail: TraceDetail, *, critical_path_ms: int | None = None) -> dict[str, Any]:
    attrs = detail.trace.attrs or {}
    if critical_path_ms is None and detail.spans:
        critical_path_ms = max(_span_duration_ms(span) for span in detail.spans)
    return {
        "trace_id": detail.trace.trace_id,
        "workflow": detail.trace.workflow_type,
        "status": detail.trace.status,
        "duration_ms": detail.trace.duration_ms,
        "critical_path_ms": critical_path_ms or 0,
        "capture_id": detail.trace.capture_id,
        "agent_run_id": detail.trace.agent_run_id,
        "provider": attrs.get("provider_name"),
        "model": attrs.get("model"),
        "intent": attrs.get("intent"),
        "confidence": attrs.get("confidence"),
        "capsule_count": attrs.get("capsule_count"),
        "confirmation_id": attrs.get("confirmation_id"),
        "proposal_id": attrs.get("proposal_id"),
        "tool_call_count": attrs.get("tool_call_count"),
    }


def build_summary(details: list[TraceDetail]) -> dict[str, Any]:
    durations = [detail.trace.duration_ms for detail in details if detail.trace.duration_ms is not None]
    provider_latencies = [
        _span_duration_ms(span)
        for detail in details
        for span in detail.spans
        if span.name == "provider.run" and _span_duration_ms(span) is not None
    ]
    events = [event for detail in details for event in detail.events]
    state_diffs = [diff for detail in details for diff in detail.state_diffs]
    spans = [span for detail in details for span in detail.spans]
    return {
        "recent_trace_count": len(details),
        "failed_trace_count": sum(1 for detail in details if detail.trace.status == "failed"),
        "avg_duration_ms": round(mean(durations), 2) if durations else None,
        "provider_latency_avg_ms": round(mean(provider_latencies), 2) if provider_latencies else None,
        "policy_block_count": sum(1 for event in events if event.name == "policy.violation")
        + sum(1 for detail in details if detail.trace.status == "blocked"),
        "confirmation_created_count": sum(
            1
            for diff in state_diffs
            if diff.entity_type == "confirmation" and diff.operation in {"create", "create_or_resolve"}
        ),
        "feishu_failure_count": sum(1 for span in spans if span.component == "feishu" and span.status == "failed")
        + sum(1 for event in events if "feishu" in event.name.lower() and event.level in {"warn", "error"}),
    }


def _span_duration_ms(span: TraceSpan) -> int:
    if span.duration_ms is not None:
        return max(0, span.duration_ms)
    if span.ended_at:
        return max(0, int((span.ended_at - span.started_at).total_seconds() * 1000))
    return 0


def _build_stages(detail: TraceDetail, spans: list[TraceSpan], total_ms: int) -> list[dict[str, Any]]:
    return [_stage_summary(definition, detail, spans, total_ms) for definition in STAGE_DEFINITIONS]


def _stage_summary(
    definition: dict[str, Any],
    detail: TraceDetail,
    spans: list[TraceSpan],
    total_ms: int,
) -> dict[str, Any]:
    matched = [span for span in spans if _span_belongs_to_stage(span, definition)]
    duration_ms = sum(_span_duration_ms(span) for span in matched)
    status = _stage_status(matched)
    first_error = _stage_error(detail, definition, matched)
    span_names = [span.name for span in matched]
    return {
        "id": definition["id"],
        "label": definition["label"],
        "description": definition["description"],
        "status": status,
        "duration_ms": duration_ms,
        "width_percent": round(duration_ms / max(1, total_ms) * 100, 2),
        "span_count": len(matched),
        "span_ids": [span.span_id for span in matched],
        "span_names": span_names,
        "summary": _stage_text_summary(definition, matched, first_error),
        "error": first_error,
        "events": [
            event.model_dump(mode="json")
            for event in detail.events
            if event.span_id in {span.span_id for span in matched}
        ][:20],
        "artifacts": [
            artifact.model_dump(mode="json")
            for artifact in detail.artifacts
            if artifact.span_id in {span.span_id for span in matched}
            or (definition["id"] == "context" and artifact.kind in {"context_lens", "context_v2"})
            or (definition["id"] == "model" and artifact.kind == "provider_output")
            or (definition["id"] == "planner" and artifact.kind == "planner")
            or (definition["id"] == "execute" and artifact.kind == "tool_results")
        ][:20],
        "state_diffs": [
            diff.model_dump(mode="json")
            for diff in detail.state_diffs
            if diff.span_id in {span.span_id for span in matched}
            or definition["id"] in {"planner", "execute", "reply"}
        ][:30],
    }


def _span_belongs_to_stage(span: TraceSpan, definition: dict[str, Any]) -> bool:
    return (
        span.name in definition["span_names"]
        or span.component in definition["components"]
        and span.lane in definition["lanes"]
    )


def _stage_status(spans: list[TraceSpan]) -> str:
    if not spans:
        return "skipped"
    statuses = {span.status for span in spans}
    for status in ("failed", "blocked", "warn", "running"):
        if status in statuses:
            return status
    if statuses == {"skipped"}:
        return "skipped"
    return "ok"


def _stage_error(detail: TraceDetail, definition: dict[str, Any], spans: list[TraceSpan]) -> str:
    for span in spans:
        if span.status == "failed":
            error = span.attrs.get("error") or span.attrs.get("reason") or span.attrs.get("error_class")
            if error:
                return str(error)
    if definition["id"] == "model" and detail.trace.status == "failed":
        return detail.trace.summary
    return ""


def _stage_text_summary(definition: dict[str, Any], spans: list[TraceSpan], error: str) -> str:
    if error:
        return error
    if not spans:
        return "本次未进入该阶段"
    names = ", ".join(dict.fromkeys(span.name for span in spans))
    return f"{len(spans)} 个 span: {names}"


def _lane_index(lane: str) -> tuple[int, str]:
    try:
        return (LANE_ORDER.index(lane), lane)
    except ValueError:
        return (len(LANE_ORDER), lane)
