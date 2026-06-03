from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.config import get_settings

DEPRECATED_AGENT_FIRST_NOTE = (
    "Deprecated: Agent-first mode calls Codex CLI on demand from AgentOrchestrator. "
    "This review worker is retained only for legacy review_jobs tooling."
)

REQUIRED_KEYS = {
    "decision",
    "summary",
    "proposed_actions",
    "problems_found",
    "confidence",
    "should_change_existing_actions",
}


class CodexRunner(Protocol):
    def run(self, prompt: str) -> dict[str, Any]:
        ...


class ReviewApiClient(Protocol):
    def get(self, url: str):
        ...

    def post(self, url: str, json: dict[str, Any]):
        ...


class CodexCliRunner:
    def __init__(self, codex_cli_path: str):
        self.codex_cli_path = codex_cli_path
        self.schema_path = Path(__file__).with_name("codex_review_schema.json")

    def run(self, prompt: str) -> dict[str, Any]:
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False, encoding="utf-8") as output:
            output_path = output.name
        base_args = [
            "-a",
            "never",
            "-c",
            'model_reasoning_effort="low"',
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            output_path,
        ]
        path = Path(self.codex_cli_path)
        if path.suffix.lower() == ".ps1":
            command = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                self.codex_cli_path,
                *base_args,
            ]
        else:
            command = [self.codex_cli_path, *base_args]
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                encoding="utf-8",
                env=_utf8_env(),
                capture_output=True,
                timeout=300,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Codex CLI failed with {completed.returncode}: {completed.stderr or completed.stdout}"
                )
            text = Path(output_path).read_text(encoding="utf-8").strip()
            if not text:
                text = completed.stdout.strip()
            result = json.loads(text)
            validate_codex_result(result)
            return result
        finally:
            Path(output_path).unlink(missing_ok=True)


class CodexReviewWorker:
    def __init__(
        self,
        public_api_base: str,
        admin_api_token: str,
        runner: CodexRunner,
        poll_seconds: float = 10,
        api_client: ReviewApiClient | None = None,
    ):
        self.public_api_base = public_api_base.rstrip("/")
        self.admin_api_token = admin_api_token
        self.runner = runner
        self.poll_seconds = poll_seconds
        self.api_client = api_client

    def run_once(self) -> bool:
        headers = {"X-Admin-Token": self.admin_api_token}
        if self.api_client is not None:
            return self._run_once_with_client(self.api_client)
        with httpx.Client(timeout=30, headers=headers) as client:
            return self._run_once_with_client(client)

    def _run_once_with_client(self, client: ReviewApiClient) -> bool:
        response = client.get(f"{self.public_api_base}/api/codex/jobs/next")
        response.raise_for_status()
        job = response.json()
        if not job:
            return False
        job_id = job["id"]
        try:
            result = self.runner.run(build_prompt(job))
            client.post(
                f"{self.public_api_base}/api/codex/jobs/{job_id}/complete",
                json={"result_json": result},
            ).raise_for_status()
        except Exception as exc:  # noqa: BLE001 - worker must persist failures
            client.post(
                f"{self.public_api_base}/api/codex/jobs/{job_id}/fail",
                json={"error": str(exc), "result_json": {}},
            ).raise_for_status()
        return True

    def run_forever(self) -> None:
        while True:
            did_work = self.run_once()
            if not did_work:
                time.sleep(self.poll_seconds)


def validate_codex_result(result: dict[str, Any]) -> None:
    missing = REQUIRED_KEYS - set(result)
    if missing:
        raise ValueError(f"Codex result missing keys: {sorted(missing)}")
    if result["decision"] not in {"ok", "needs_user_review", "system_issue"}:
        raise ValueError("Codex result has invalid decision")
    if not isinstance(result["proposed_actions"], list):
        raise ValueError("Codex result proposed_actions must be a list")
    if not isinstance(result["problems_found"], list):
        raise ValueError("Codex result problems_found must be a list")
    confidence = result["confidence"]
    if not isinstance(confidence, int | float) or not 0 <= confidence <= 1:
        raise ValueError("Codex result confidence must be between 0 and 1")
    if not isinstance(result["should_change_existing_actions"], bool):
        raise ValueError("Codex result should_change_existing_actions must be boolean")


def _utf8_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    return env


def build_prompt(job: dict[str, Any]) -> str:
    return (
        "你是飞书个人任务管理系统的 Codex 审核 worker。"
        "请审核下面这个 job，发现抽取错误、同步错误或系统问题。"
        "v1 只记录建议，不要要求直接修改系统状态。"
        "必须输出符合 JSON schema 的单个 JSON 对象。\n\n"
        f"job_type: {job.get('job_type')}\n"
        f"job_id: {job.get('id')}\n"
        f"capture_id: {job.get('capture_id')}\n"
        f"action_ids: {job.get('action_ids')}\n"
        f"source_ref: {job.get('source_ref')}\n\n"
        f"{job.get('prompt')}\n"
    )


def main() -> None:
    settings = get_settings()
    if not settings.public_api_base:
        raise RuntimeError("PUBLIC_API_BASE is required for Codex review worker")
    if not settings.admin_api_token:
        raise RuntimeError("ADMIN_API_TOKEN is required for Codex review worker")
    worker = CodexReviewWorker(
        settings.public_api_base,
        settings.admin_api_token,
        CodexCliRunner(settings.codex_cli_path),
        poll_seconds=settings.codex_worker_poll_seconds,
    )
    worker.run_forever()


if __name__ == "__main__":
    main()
