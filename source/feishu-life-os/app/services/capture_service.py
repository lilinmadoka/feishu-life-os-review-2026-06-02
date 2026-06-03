from __future__ import annotations

from app.database import Repository
from app.models import CaptureCreate, CaptureResponse, CaptureStatus
from app.services.dedupe_service import DedupeService
from app.services.extraction_service import RuleBasedExtractor
from app.services.normalizer import normalize_text


class CaptureService:
    def __init__(self, repo: Repository, extractor: RuleBasedExtractor):
        self.repo = repo
        self.extractor = extractor
        self.dedupe = DedupeService(repo)

    def capture(self, payload: CaptureCreate) -> CaptureResponse:
        normalized = normalize_text(payload.raw_text)
        capture = self.repo.create_capture(payload, normalized_text=normalized)
        extracted = self.extractor.extract(normalized, capture_id=capture.id)

        created = []
        duplicate_ids: list[str] = []
        max_confidence = 0.0
        for action in extracted:
            duplicates = self.dedupe.find_duplicates(action)
            if duplicates:
                duplicate_ids.extend(item.id for item in duplicates)
                # Keep a low-confidence inbox copy only when the new text contains extra evidence.
                if action.confidence < 0.55:
                    continue
                action.metadata["possible_duplicate_ids"] = [item.id for item in duplicates]
                action.labels.append("可能重复")
            record = self.repo.create_action(action)
            created.append(record)
            max_confidence = max(max_confidence, record.confidence)

        status = CaptureStatus.parsed if created else CaptureStatus.needs_review
        capture = self.repo.update_capture_status(capture.id, status=status, confidence=max_confidence)
        return CaptureResponse(capture=capture, actions=created, duplicate_action_ids=duplicate_ids)
