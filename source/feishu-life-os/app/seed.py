from __future__ import annotations

import argparse
from pathlib import Path

from app.config import get_settings
from app.database import Repository
from app.models import CaptureCreate, SourceType
from app.services.capture_service import CaptureService
from app.services.extraction_service import RuleBasedExtractor
from app.services.review_service import ReviewService

DEMO_ITEMS = [
    "明天下午3点给学生小王补课，记得今晚把资料发给家长",
    "周五前提交数据库作业，老师说不要晚交",
    "今晚把项目 README 改完，明早让 Codex 接飞书 API",
    "等张老师回复选课申请，后天上午如果没回就催一下",
    "5月30日 20:00 学习平台有 quiz 截止",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrate-only", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    repo = Repository(settings.database_path)
    repo.migrate()
    if args.migrate_only:
        print(f"Migrated database: {settings.database_path}")
        return

    service = CaptureService(repo, RuleBasedExtractor(settings.tzinfo))
    for item in DEMO_ITEMS:
        result = service.capture(CaptureCreate(raw_text=item, source_type=SourceType.manual))
        print(f"capture={result.capture.id} actions={[a.id for a in result.actions]}")
    review = ReviewService(repo, settings.tzinfo).daily()
    print(review.markdown)


if __name__ == "__main__":
    main()
