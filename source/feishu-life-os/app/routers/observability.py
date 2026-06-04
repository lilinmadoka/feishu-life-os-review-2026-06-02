from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.config import get_settings
from app.core.observability import SQLiteTraceStore
from app.dependencies import get_observability_store

router = APIRouter(prefix="/api/v2/observability", tags=["observability"])


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_API_TOKEN is not configured")
    if x_admin_token != expected:
        raise HTTPException(status_code=403, detail="invalid admin token")


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
    detail = store.get_trace(trace_id)
    if not detail:
        raise HTTPException(status_code=404, detail="trace not found")
    return detail.model_dump(mode="json")
