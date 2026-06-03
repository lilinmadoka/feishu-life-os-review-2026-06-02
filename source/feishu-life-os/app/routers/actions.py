from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.database import Repository
from app.dependencies import get_repo, get_sync_service
from app.models import ActionRecord, ActionStatus, ActionUpdate
from app.services.sync_service import SyncService

router = APIRouter(prefix="/api/actions", tags=["actions"])


@router.get("", response_model=list[ActionRecord])
def list_actions(
    status: list[ActionStatus] | None = Query(default=None),
    include_done: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    repo: Repository = Depends(get_repo),
):
    return repo.list_actions(statuses=status, include_done=include_done, limit=limit)


@router.get("/{action_id}", response_model=ActionRecord)
def get_action(action_id: str, repo: Repository = Depends(get_repo)):
    try:
        return repo.get_action(action_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{action_id}", response_model=ActionRecord)
async def update_action(
    action_id: str,
    patch: ActionUpdate,
    repo: Repository = Depends(get_repo),
    sync: SyncService = Depends(get_sync_service),
):
    try:
        action = repo.update_action(action_id, patch)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await sync.sync_action(action)
    return action


@router.post("/{action_id}/complete", response_model=ActionRecord)
def complete_action(action_id: str, repo: Repository = Depends(get_repo)):
    try:
        return repo.complete_action(action_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
