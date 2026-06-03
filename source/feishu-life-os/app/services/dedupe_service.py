from __future__ import annotations

from difflib import SequenceMatcher

from app.database import Repository
from app.models import ActionCreate, ActionRecord
from app.services.normalizer import compact_fingerprint


class DedupeService:
    def __init__(self, repo: Repository):
        self.repo = repo

    def find_duplicates(self, action: ActionCreate) -> list[ActionRecord]:
        fingerprint = compact_fingerprint(action.title)
        if not fingerprint:
            return []
        candidates = self.repo.find_similar_actions(fingerprint)
        duplicates: list[ActionRecord] = []
        for candidate in candidates:
            score = SequenceMatcher(None, fingerprint, compact_fingerprint(candidate.title)).ratio()
            same_due = bool(action.due_at and candidate.due_at and action.due_at.date() == candidate.due_at.date())
            if score >= 0.88 or (score >= 0.72 and same_due):
                duplicates.append(candidate)
        return duplicates
