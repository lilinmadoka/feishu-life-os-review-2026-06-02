from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import get_settings
from app.core.feishu_native import MockFeishuNativeAdapter
from app.core.orchestrator import CoreAgentOrchestrator
from app.core.providers import MockAgentProvider
from app.core.schemas import CaptureIn
from app.core.store import StateStore
from app.database import Repository


async def process(orchestrator: CoreAgentOrchestrator, text: str, message_id: str):
    return await orchestrator.process_capture(
        CaptureIn(
            source="validation",
            source_message_id=message_id,
            sender_id="ou_validation",
            chat_id="chat_validation",
            raw_text=text,
        )
    )


async def run() -> dict:
    settings = get_settings()
    with TemporaryDirectory() as tmp:
        repo = Repository(str(Path(tmp) / "lifeos.sqlite3"))
        repo.migrate()
        store = StateStore(repo)
        store.migrate()
        feishu = MockFeishuNativeAdapter()
        orchestrator = CoreAgentOrchestrator(store, MockAgentProvider(settings.tzinfo), feishu, settings.tzinfo)

        results = []

        first = await process(orchestrator, "今天还有什么任务？", "e2e_1")
        results.append(
            {
                "scenario": "query_today",
                "passed": store.list_action_items() == [] and store.list_calendar_events() == [],
                "reply": first.reply_text,
                "created_tasks": len(store.list_action_items()),
                "created_events": len(store.list_calendar_events()),
            }
        )

        second = await process(orchestrator, "明天下午3点给小王补课，今晚把资料发家长", "e2e_2")
        results.append(
            {
                "scenario": "create_candidates",
                "passed": bool(second.confirmation_id)
                and len(store.list_action_items()) == 0
                and len(store.list_calendar_events()) == 0,
                "reply": second.reply_text,
                "confirmation_id": second.confirmation_id,
            }
        )

        third = await process(orchestrator, "确认", "e2e_3")
        results.append(
            {
                "scenario": "confirm_candidates",
                "passed": len(store.list_action_items()) == 1 and len(store.list_calendar_events()) == 1,
                "reply": third.reply_text,
                "tool_runs": len(store.list_tool_runs()),
            }
        )

        fourth = await process(orchestrator, "把小王补课改到晚上7点", "e2e_4")
        results.append(
            {
                "scenario": "update_calendar_event_requires_confirmation",
                "passed": bool(fourth.confirmation_id),
                "reply": fourth.reply_text,
                "confirmation_id": fourth.confirmation_id,
            }
        )

        fifth = await process(orchestrator, "我每周一三五晚上7点到9点固定上课，周二下午2点到5点实验课", "e2e_5")
        results.append(
            {
                "scenario": "schedule_block_candidates",
                "passed": bool(fifth.confirmation_id) and len(store.list_schedule_blocks()) == 0,
                "reply": fifth.reply_text,
                "confirmation_id": fifth.confirmation_id,
            }
        )

        summary = {
            "passed": all(item["passed"] for item in results),
            "results": results,
            "mock": {
                "sent_texts": feishu.sent_texts,
                "sent_cards": feishu.sent_cards,
                "synced_tasks": feishu.synced_tasks,
                "synced_calendar_events": feishu.synced_calendar_events,
                "synced_audits": feishu.synced_audits,
            },
        }
        return summary


def write_results(summary: dict) -> None:
    out_dir = Path("validation")
    out_dir.mkdir(exist_ok=True)
    (out_dir / "e2e_results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# E2E Results", ""]
    lines.append(f"Overall: {'PASS' if summary['passed'] else 'FAIL'}")
    lines.append("")
    for item in summary["results"]:
        lines.append(f"## {item['scenario']}")
        lines.append(f"- Status: {'PASS' if item['passed'] else 'FAIL'}")
        lines.append(f"- Reply: {item.get('reply', '')}")
        if item.get("confirmation_id"):
            lines.append(f"- Confirmation: {item['confirmation_id']}")
        lines.append("")
    (out_dir / "e2e_results.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    summary = asyncio.run(run())
    write_results(summary)
    print(json.dumps({"passed": summary["passed"], "count": len(summary["results"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
