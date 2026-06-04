from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.core.observability import SQLiteTraceStore
from app.core.observability.schemas import TraceDetail
from app.core.observability.ui_models import (
    build_artifacts,
    build_graph,
    build_summary,
    build_timeline,
)
from app.dependencies import get_observability_store

router = APIRouter(prefix="/api/v2/observability", tags=["observability"])
STATIC_ROOT = Path(__file__).resolve().parents[1] / "static" / "observability"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_API_TOKEN is not configured")
    if x_admin_token != expected:
        raise HTTPException(status_code=403, detail="invalid admin token")


def require_ui_admin_token(
    x_admin_token: str | None = Header(default=None),
    admin_token: str | None = Query(default=None),
) -> None:
    expected = get_settings().admin_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_API_TOKEN is not configured")
    if (x_admin_token or admin_token) != expected:
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


@router.get("/summary")
def get_observability_summary(
    limit: int = Query(default=50, ge=1, le=200),
    _: None = Depends(require_admin_token),
    store: SQLiteTraceStore = Depends(get_observability_store),
) -> dict[str, Any]:
    details = [
        detail
        for trace in store.list_traces(limit=limit)
        if (detail := store.get_trace(trace.trace_id)) is not None
    ]
    return build_summary(details)


@router.get("/system")
def get_observability_system(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    settings = get_settings()
    lm_studio = _check_lm_studio(settings.lm_studio_base_url)
    provider_ready = settings.core_agent_provider != "lm_studio_provider" or lm_studio["status"] == "ok"
    return {
        "fastapi": {"status": "ok", "url": "http://127.0.0.1:8000"},
        "provider": {
            "status": "ok" if provider_ready else "unavailable",
            "name": settings.core_agent_provider,
            "model": settings.lm_studio_model if settings.core_agent_provider == "lm_studio_provider" else None,
        },
        "lm_studio": lm_studio,
        "observability": {
            "enabled": settings.observability_enabled,
            "capture_full_payload": settings.observability_capture_full_payload,
            "ui_enabled": settings.observability_ui_enabled,
        },
        "processes": {
            name: _tracked_process_status(name)
            for name in ("fastapi", "cloudflared", "reminder_worker", "codex_worker")
        },
    }


@router.get("/traces/{trace_id}/timeline")
def get_trace_timeline(
    trace_id: str,
    _: None = Depends(require_admin_token),
    store: SQLiteTraceStore = Depends(get_observability_store),
) -> dict[str, Any]:
    detail = _get_detail_or_404(store, trace_id)
    return build_timeline(detail)


@router.get("/traces/{trace_id}/graph")
def get_trace_graph(
    trace_id: str,
    _: None = Depends(require_admin_token),
    store: SQLiteTraceStore = Depends(get_observability_store),
) -> dict[str, Any]:
    detail = _get_detail_or_404(store, trace_id)
    return build_graph(detail)


@router.get("/traces/{trace_id}/artifacts")
def get_trace_artifacts(
    trace_id: str,
    _: None = Depends(require_admin_token),
    store: SQLiteTraceStore = Depends(get_observability_store),
) -> dict[str, Any]:
    detail = _get_detail_or_404(store, trace_id)
    return build_artifacts(detail)


@router.get("/ui", response_class=HTMLResponse)
def observability_ui(_: None = Depends(require_ui_admin_token)) -> HTMLResponse:
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


def _check_lm_studio(base_url: str) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/models"
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=1.5) as response:
            raw = response.read().decode("utf-8")
        data = json.loads(raw)
        models = data.get("data") if isinstance(data, dict) else None
        model_count = len(models) if isinstance(models, list) else 0
        loaded_models = [
            str(item.get("id"))
            for item in models[:10]
            if isinstance(item, dict) and item.get("id")
        ] if isinstance(models, list) else []
        return {
            "status": "ok",
            "base_url": base_url,
            "model_count": model_count,
            "loaded_models": loaded_models,
        }
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {
            "status": "unavailable",
            "base_url": base_url,
            "error_class": exc.__class__.__name__,
            "message": str(exc)[:180],
        }


def _tracked_process_status(name: str) -> dict[str, Any]:
    pid_path = PROJECT_ROOT / ".data" / "pids" / f"{name}.pid"
    if not pid_path.exists():
        return {"status": "stopped"}
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return {"status": "unknown", "reason": "invalid_pid_file"}
    return {"status": "running" if _pid_exists(pid) else "stopped", "pid": pid}


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return str(pid) in result.stdout
