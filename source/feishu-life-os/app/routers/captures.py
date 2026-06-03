from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import Repository
from app.dependencies import get_capture_service, get_repo, get_sync_service
from app.models import CaptureCreate, CaptureResponse, CaptureStatus
from app.services.capture_service import CaptureService
from app.services.sync_service import SyncService

router = APIRouter(prefix="/api/captures", tags=["captures"])


@router.post("", response_model=CaptureResponse)
async def create_capture(
    payload: CaptureCreate,
    service: CaptureService = Depends(get_capture_service),
    sync: SyncService = Depends(get_sync_service),
):
    result = service.capture(payload)
    await sync.sync_capture(result.capture)
    for action in result.actions:
        await sync.sync_action(action)
    return result


@router.get("")
def list_captures(
    status: CaptureStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    repo: Repository = Depends(get_repo),
):
    return repo.list_captures(status=status, limit=limit)


@router.get("/{capture_id}")
def get_capture(capture_id: str, repo: Repository = Depends(get_repo)):
    try:
        return repo.get_capture(capture_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
