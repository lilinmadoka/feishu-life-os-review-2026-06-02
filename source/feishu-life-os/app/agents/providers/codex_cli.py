from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from pydantic import ValidationError

from app.agents.models import AgentRequest, AgentResponse
from app.agents.providers.base import AgentProviderError, AgentProviderUnavailable


class CodexCliAgentProvider:
    name = "codex_cli"

    def __init__(self, codex_cli_path: str, timeout_seconds: int = 300):
        self.codex_cli_path = codex_cli_path
        self.timeout_seconds = timeout_seconds
        self.schema_path = Path(__file__).parents[1] / "agent_response_schema.json"

    def run(self, request: AgentRequest) -> AgentResponse:
        path = Path(self.codex_cli_path)
        if not path.exists():
            raise AgentProviderUnavailable(f"Codex CLI not found: {self.codex_cli_path}")
        prompt = self._build_prompt(request)
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False, encoding="utf-8") as output:
            output_path = Path(output.name)
        codex_args = [
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
            str(output_path),
        ]
        command = self._command(path, codex_args)
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                encoding="utf-8",
                env=self._env(),
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                raise AgentProviderError(
                    f"Codex CLI failed with {completed.returncode}: {completed.stderr or completed.stdout}"
                )
            text = output_path.read_text(encoding="utf-8").strip()
            if not text:
                text = completed.stdout.strip()
            try:
                payload = json.loads(text)
                return AgentResponse.model_validate(payload)
            except (json.JSONDecodeError, ValidationError) as exc:
                raise AgentProviderError(f"Codex CLI returned invalid AgentResponse JSON: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AgentProviderUnavailable("Codex CLI timed out") from exc
        finally:
            output_path.unlink(missing_ok=True)

    def _command(self, path: Path, args: list[str]) -> list[str]:
        if path.suffix.lower() == ".ps1":
            return [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(path),
                *args,
            ]
        return [str(path), *args]

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["LC_ALL"] = "C.UTF-8"
        env["LANG"] = "C.UTF-8"
        return env

    def _build_prompt(self, request: AgentRequest) -> str:
        request_json = request.model_dump_json(indent=2)
        return (
            "你是一个 Agent-first 的飞书私人助理 Orchestrator，不是表格搬运工具。\n"
            "你需要根据 AgentRequest 判断用户意图，并只输出一个符合 JSON schema 的 AgentResponse。\n"
            "不要输出 Markdown、解释段落或自由散文。\n\n"
            "可用意图：capture, query, update, clarify, review, ignore, system。\n"
            "可用工具：send_feishu_reply, create_task, query_today, query_tomorrow, query_overdue, query_next_7_days, "
            "update_task_status, update_task_time, ask_confirmation, sync_bitable, sync_feishu_task, "
            "sync_feishu_calendar。\n\n"
            "安全规则：删除、批量修改、低置信度修改、多候选匹配都必须请求确认。"
            "查询类消息不能创建任务。多维表只是后台视图和审计层。\n\n"
            "如果用户只回复 A/B/C、数字、是的、确认，必须先查看 AgentRequest.pending_summary，"
            "判断是否在回答上一轮确认问题。\n\n"
            "create_task 的 arguments 尽量包含 title、description、due_at ISO8601、start_at ISO8601、intent、domain、priority、"
            "evidence_text、confidence。有明确开始/结束时间的课程、家教、出行等日程，先 create_task(intent=event)，"
            "再用 sync_feishu_calendar 同步到飞书日历。普通待办可以用 sync_feishu_task。"
            "update_task_time 尽量包含 action_id 或 query，以及 due_at/start_at/remind_at ISO8601。\n\n"
            f"AgentRequest:\n{request_json}\n"
        )
