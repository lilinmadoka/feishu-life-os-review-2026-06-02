from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_review_service, get_sync_service
from app.models import ReviewResponse
from app.services.review_service import ReviewService
from app.services.sync_service import SyncService

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


@router.get("/daily", response_model=ReviewResponse)
def daily_review(
    date: str | None = Query(default=None, description="YYYY-MM-DD; defaults to today in configured timezone"),
    service: ReviewService = Depends(get_review_service),
):
    target = datetime.fromisoformat(date) if date else None
    return service.daily(target)


@router.post("/daily/send")
async def send_daily_review(
    service: ReviewService = Depends(get_review_service),
    sync: SyncService = Depends(get_sync_service),
):
    review = service.daily()
    event = await sync.send_review(review.markdown)
    return {"review": review, "sync_event": event}
