from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import get_settings
from app.database import Repository
from app.dependencies import get_repo
from app.models import ReviewJobComplete, ReviewJobFail, ReviewJobRecord

router = APIRouter(prefix="/api/codex", tags=["codex"])


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_API_TOKEN is not configured")
    if x_admin_token != expected:
        raise HTTPException(status_code=403, detail="invalid admin token")


@router.get("/jobs/next", response_model=ReviewJobRecord | None)
def next_job(
    _: None = Depends(require_admin_token),
    repo: Repository = Depends(get_repo),
):
    return repo.get_next_review_job()


@router.post("/jobs/{job_id}/complete", response_model=ReviewJobRecord)
def complete_job(
    job_id: str,
    payload: ReviewJobComplete,
    _: None = Depends(require_admin_token),
    repo: Repository = Depends(get_repo),
):
    try:
        return repo.complete_review_job(job_id, payload.result_json)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/fail", response_model=ReviewJobRecord)
def fail_job(
    job_id: str,
    payload: ReviewJobFail,
    _: None = Depends(require_admin_token),
    repo: Repository = Depends(get_repo),
):
    try:
        return repo.fail_review_job(job_id, payload.error, payload.result_json)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
