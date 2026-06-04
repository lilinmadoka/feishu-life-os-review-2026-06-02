from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.core.observability import SQLiteTraceStore
from app.core.observability.schemas import TraceDetail
from app.dependencies import get_observability_store

router = APIRouter(prefix="/api/v2/observability", tags=["observability"])
STATIC_ROOT = Path(__file__).resolve().parents[1] / "static" / "observability"


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_API_TOKEN is not configured")
    if x_admin_token != expected:
        raise HTTPException(status_code=403, detail="invalid admin token")


def _get_detail_or_404(store: SQLiteTraceStore, trace_id: str) -> TraceDetail:
    detail = store.get_trace(trace_id)
    if not detail:
        raise HTTPException(status_code=404, detail="trace not found")
    return detail


@router.get("/traces")
def list_traces(
    limit: int = Query(default=50, ge=1, le=200),
    _: None = Depends(require_admin_token),
    store: SQLiteTraceStore = Depends(get_observability_store),
) -> dict[str, Any]:
    return {"items": [trace.model_dump(mode="json") for trace in store.list_traces(limit=limit)]}


@router.get("/traces/{trace_id}")
def get_trace(
    trace_id: str,
    _: None = Depends(require_admin_token),
    store: SQLiteTraceStore = Depends(get_observability_store),
) -> dict[str, Any]:
    detail = _get_detail_or_404(store, trace_id)
    return detail.model_dump(mode="json")


@router.get("/traces/{trace_id}/timeline")
def get_trace_timeline(
    trace_id: str,
    _: None = Depends(require_admin_token),
    store: SQLiteTraceStore = Depends(get_observability_store),
) -> dict[str, Any]:
    detail = _get_detail_or_404(store, trace_id)
    spans = detail.spans
    if not spans:
        return {"trace_id": trace_id, "lanes": [], "duration_ms": detail.trace.duration_ms or 0}
    first = min(span.started_at for span in spans)
    last = max((span.ended_at or span.started_at) for span in spans)
    total_ms = max(1, int((last - first).total_seconds() * 1000))
    lanes: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        start_ms = max(0, int((span.started_at - first).total_seconds() * 1000))
        duration_ms = span.duration_ms or max(0, int(((span.ended_at or span.started_at) - span.started_at).total_seconds() * 1000))
        lanes.setdefault(span.lane, []).append(
            {
                "span_id": span.span_id,
                "name": span.name,
                "component": span.component,
                "status": span.status,
                "start_ms": start_ms,
                "duration_ms": duration_ms,
                "offset_percent": round(start_ms / total_ms * 100, 2),
                "width_percent": max(0.8, round(max(1, duration_ms) / total_ms * 100, 2)),
                "attrs": span.attrs,
            }
        )
    return {
        "trace_id": trace_id,
        "duration_ms": detail.trace.duration_ms or total_ms,
        "lanes": [{"name": lane, "spans": lane_spans} for lane, lane_spans in lanes.items()],
    }


@router.get("/traces/{trace_id}/graph")
def get_trace_graph(
    trace_id: str,
    _: None = Depends(require_admin_token),
    store: SQLiteTraceStore = Depends(get_observability_store),
) -> dict[str, Any]:
    detail = _get_detail_or_404(store, trace_id)
    nodes = [
        {
            "id": span.span_id,
            "label": span.name,
            "lane": span.lane,
            "component": span.component,
            "status": span.status,
            "duration_ms": span.duration_ms,
        }
        for span in detail.spans
    ]
    edges = [
        {"from": span.parent_span_id, "to": span.span_id}
        for span in detail.spans
        if span.parent_span_id
    ]
    return {"trace_id": trace_id, "nodes": nodes, "edges": edges}


@router.get("/traces/{trace_id}/artifacts")
def get_trace_artifacts(
    trace_id: str,
    _: None = Depends(require_admin_token),
    store: SQLiteTraceStore = Depends(get_observability_store),
) -> dict[str, Any]:
    detail = _get_detail_or_404(store, trace_id)
    return {
        "trace_id": trace_id,
        "artifacts": [artifact.model_dump(mode="json") for artifact in detail.artifacts],
        "state_diffs": [diff.model_dump(mode="json") for diff in detail.state_diffs],
        "events": [event.model_dump(mode="json") for event in detail.events],
    }


@router.get("/ui", response_class=HTMLResponse)
def observability_ui(_: None = Depends(require_admin_token)) -> HTMLResponse:
    settings = get_settings()
    if not settings.observability_ui_enabled:
        raise HTTPException(status_code=404, detail="observability UI is disabled")
    try:
        html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        css = (STATIC_ROOT / "observability.css").read_text(encoding="utf-8")
        js = (STATIC_ROOT / "observability.js").read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="observability UI assets missing") from exc
    html = html.replace("/*__OBSERVABILITY_CSS__*/", css)
    html = html.replace("//__OBSERVABILITY_JS__", js)
    return HTMLResponse(html)
