from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.database import Repository
from app.main import create_app
from app.models import CaptureCreate, SourceType
from app.services.capture_service import CaptureService
from app.services.extraction_service import RuleBasedExtractor

TZ = ZoneInfo("Asia/Singapore")
BASE = datetime(2026, 5, 26, 10, 0, tzinfo=TZ)


def test_repository_capture_roundtrip(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    service = CaptureService(repo, RuleBasedExtractor(TZ, now_provider=lambda: BASE))
    result = service.capture(CaptureCreate(raw_text="明天下午3点补课", source_type=SourceType.manual))
    assert result.capture.status == "parsed"
    assert len(result.actions) == 1
    assert repo.get_action(result.actions[0].id).title


def test_api_health():
    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
