from __future__ import annotations

from app.workers.codex_review_worker import CodexReviewWorker


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        return None


class FakeApiClient:
    def __init__(self, job):
        self.job = job
        self.posts = []

    def get(self, _url):
        return FakeResponse(self.job)

    def post(self, url, json):
        self.posts.append((url, json))
        return FakeResponse({"ok": True})


class GoodRunner:
    def run(self, prompt: str):
        assert "job_id" in prompt
        return {
            "decision": "ok",
            "summary": "抽取合理",
            "proposed_actions": [],
            "problems_found": [],
            "confidence": 0.9,
            "should_change_existing_actions": False,
        }


class BrokenRunner:
    def run(self, _prompt: str):
        raise ValueError("invalid JSON output")


def fake_job():
    return {
        "id": "job_1",
        "job_type": "extraction_review",
        "capture_id": "cap_1",
        "action_ids": ["act_1"],
        "source_ref": "mid_1",
        "prompt": "请审核",
    }


def test_worker_completes_pending_job():
    api = FakeApiClient(fake_job())
    worker = CodexReviewWorker("https://example.test", "token", GoodRunner(), api_client=api)
    assert worker.run_once() is True
    url, payload = api.posts[0]
    assert url.endswith("/api/codex/jobs/job_1/complete")
    assert payload["result_json"]["decision"] == "ok"


def test_worker_marks_job_failed_when_runner_fails():
    api = FakeApiClient(fake_job())
    worker = CodexReviewWorker("https://example.test", "token", BrokenRunner(), api_client=api)
    assert worker.run_once() is True
    url, payload = api.posts[0]
    assert url.endswith("/api/codex/jobs/job_1/fail")
    assert "invalid JSON output" in payload["error"]


def test_worker_returns_false_when_no_job():
    api = FakeApiClient(None)
    worker = CodexReviewWorker("https://example.test", "token", GoodRunner(), api_client=api)
    assert worker.run_once() is False
    assert api.posts == []
