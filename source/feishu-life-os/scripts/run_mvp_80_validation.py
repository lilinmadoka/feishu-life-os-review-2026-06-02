from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import get_settings
from app.core.feishu_native import MockFeishuNativeAdapter
from app.core.orchestrator import CoreAgentOrchestrator
from app.core.providers import MockAgentProvider
from app.core.schemas import AgentToolCall, CaptureIn
from app.core.store import StateStore
from app.database import Repository


async def process(orchestrator: CoreAgentOrchestrator, text: str, message_id: str):
    return await orchestrator.process_capture(
        CaptureIn(
            source="mvp_80_validation",
            source_message_id=message_id,
            sender_id="ou_validation",
            chat_id="chat_validation",
            raw_text=text,
        )
    )


def contains_value(obj, key: str, value: str) -> bool:
    if isinstance(obj, dict):
        if obj.get(key) == value:
            return True
        return any(contains_value(item, key, value) for item in obj.values())
    if isinstance(obj, list):
        return any(contains_value(item, key, value) for item in obj)
    return False


async def run() -> dict:
    settings = get_settings()
    with TemporaryDirectory() as tmp:
        repo = Repository(str(Path(tmp) / "lifeos.sqlite3"))
        repo.migrate()
        store = StateStore(repo)
        store.migrate()
        feishu = MockFeishuNativeAdapter()
        orchestrator = CoreAgentOrchestrator(store, MockAgentProvider(settings.tzinfo), feishu, settings.tzinfo)
        results: list[dict] = []

        query = await process(orchestrator, "今天还有什么任务？", "mvp_query_today")
        results.append(
            {
                "scenario": "query_today_no_write",
                "passed": len(store.list_action_items()) == 0
                and len(store.list_calendar_events()) == 0
                and len(store.list_pending_confirmations("ou_validation")) == 0,
                "reply": query.reply_text,
            }
        )

        candidate = await process(orchestrator, "明天下午3点给小王补课，今晚把资料发家长", "mvp_candidates")
        card = feishu.sent_cards[-1]["card"] if feishu.sent_cards else {}
        results.append(
            {
                "scenario": "candidate_card_contains_confirmation_id",
                "passed": bool(candidate.confirmation_id)
                and contains_value(card, "confirmation_id", candidate.confirmation_id)
                and len(store.list_action_items()) == 0
                and len(store.list_calendar_events()) == 0,
                "reply": candidate.reply_text,
                "confirmation_id": candidate.confirmation_id,
            }
        )

        confirmed = await orchestrator.router.resolve_confirmation(
            sender_id="ou_validation", confirmation_id=candidate.confirmation_id
        )
        results.append(
            {
                "scenario": "card_confirm_creates_task_and_calendar",
                "passed": confirmed["status"] == "resolved"
                and len(store.list_action_items()) == 1
                and len(store.list_calendar_events()) == 1
                and len(store.list_tool_runs()) >= 2,
                "reply": confirmed["reply_text"],
                "sync": confirmed.get("synced", []),
            }
        )

        duplicate = await orchestrator.router.resolve_confirmation(
            sender_id="ou_validation", confirmation_id=candidate.confirmation_id
        )
        results.append(
            {
                "scenario": "duplicate_confirm_is_idempotent",
                "passed": duplicate["status"] == "resolved"
                and len(store.list_action_items()) == 1
                and len(store.list_calendar_events()) == 1,
                "reply": duplicate["reply_text"],
            }
        )

        cancel_candidate = await process(orchestrator, "明天下午3点给小王补课，今晚把资料发家长", "mvp_cancel_candidate")
        canceled = await orchestrator.router.resolve_confirmation(
            sender_id="ou_validation",
            confirmation_id=cancel_candidate.confirmation_id,
            action="cancel",
        )
        results.append(
            {
                "scenario": "card_cancel_creates_nothing",
                "passed": canceled["status"] == "canceled"
                and len(store.list_action_items()) == 1
                and len(store.list_calendar_events()) == 1,
                "reply": canceled["reply_text"],
            }
        )

        missing = await orchestrator.router.resolve_confirmation(sender_id="ou_validation", confirmation_id="conf_missing")
        results.append(
            {
                "scenario": "missing_confirmation_safe_failure",
                "passed": missing["status"] == "not_found",
                "reply": missing["reply_text"],
            }
        )

        run = store.create_agent_run(capture_id=None, provider="validation", model=None, input_json={})
        expired = store.create_confirmation(
            agent_run_id=run.id,
            confirmation_type="create_candidates",
            proposed_tool_calls_json=[
                AgentToolCall(
                    tool_name="create_task_candidate",
                    requires_confirmation=True,
                    arguments={"title": "过期任务"},
                ).model_dump(mode="json")
            ],
            sender_id="ou_validation",
            expires_at=datetime.now(settings.tzinfo) - timedelta(minutes=1),
        )
        expired_result = await orchestrator.router.resolve_confirmation(
            sender_id="ou_validation", confirmation_id=expired.id
        )
        results.append(
            {
                "scenario": "expired_confirmation_safe_failure",
                "passed": expired_result["status"] == "expired",
                "reply": expired_result["reply_text"],
            }
        )

        update = await process(orchestrator, "把小王补课改到晚上7点", "mvp_update_calendar")
        before_hour = store.list_calendar_events()[0].start_at.hour
        update_confirmed = await orchestrator.router.resolve_confirmation(
            sender_id="ou_validation", confirmation_id=update.confirmation_id
        )
        after_hour = store.list_calendar_events()[0].start_at.hour
        results.append(
            {
                "scenario": "calendar_update_requires_confirmation",
                "passed": bool(update.confirmation_id)
                and before_hour != 19
                and update_confirmed["status"] == "resolved"
                and after_hour == 19,
                "reply": update.reply_text,
            }
        )

        store.create_action_item({"title": "整理资料"})
        done = await process(orchestrator, "完成整理资料任务", "mvp_complete_unique")
        results.append(
            {
                "scenario": "complete_unique_task",
                "passed": "已完成任务" in done.reply_text
                and store.find_action_items("整理资料", include_done=True)[0].status.value == "done",
                "reply": done.reply_text,
            }
        )

        store.create_action_item({"title": "阅读论文"})
        store.create_action_item({"title": "阅读论文第二篇"})
        ambiguous = await process(orchestrator, "完成阅读论文任务", "mvp_complete_ambiguous")
        results.append(
            {
                "scenario": "complete_ambiguous_task_requires_choice",
                "passed": "找到" in ambiguous.reply_text
                and all(item.status.value != "done" for item in store.find_action_items("阅读论文", include_done=True)),
                "reply": ambiguous.reply_text,
            }
        )

        schedule = await process(orchestrator, "我每周一三五晚上7点到9点固定上课，周二下午2点到5点实验课", "mvp_schedule")
        schedule_confirmed = await orchestrator.router.resolve_confirmation(
            sender_id="ou_validation", confirmation_id=schedule.confirmation_id
        )
        results.append(
            {
                "scenario": "weekly_schedule_blocks",
                "passed": schedule_confirmed["status"] == "resolved" and len(store.list_schedule_blocks()) == 2,
                "reply": schedule.reply_text,
            }
        )

        sleep = await process(orchestrator, "每晚12点到早上8点睡觉", "mvp_sleep")
        await orchestrator.router.resolve_confirmation(sender_id="ou_validation", confirmation_id=sleep.confirmation_id)
        results.append(
            {
                "scenario": "nightly_sleep_block",
                "passed": any(block.title == "睡觉" for block in store.list_schedule_blocks()),
                "reply": sleep.reply_text,
            }
        )

        tomorrow = datetime.now(settings.tzinfo) + timedelta(days=1)
        day_code = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][tomorrow.weekday()]
        store.create_schedule_block(
            {
                "title": "明天下午固定占用",
                "recurrence_rule": f"FREQ=WEEKLY;BYDAY={day_code}",
                "start_time": "15:00",
                "end_time": "16:00",
                "timezone": "Asia/Shanghai",
            }
        )
        conflict = await process(orchestrator, "明天下午3点给小王补课，今晚把资料发家长", "mvp_conflict")
        conflict_card = feishu.sent_cards[-1]["card"] if feishu.sent_cards else {}
        results.append(
            {
                "scenario": "calendar_candidate_conflict_hint",
                "passed": "冲突" in conflict_card.get("elements", [{}])[0].get("text", {}).get("content", ""),
                "reply": conflict.reply_text,
            }
        )

        related = await process(orchestrator, "小王相关的任务有哪些？", "mvp_related")
        results.append(
            {
                "scenario": "related_query_no_write",
                "passed": "小王" in related.reply_text,
                "reply": related.reply_text,
            }
        )

        pending_before = len(store.list_pending_confirmations("ou_validation"))
        pending = await process(orchestrator, "最近待确认项有哪些？", "mvp_pending")
        results.append(
            {
                "scenario": "pending_confirmation_query_no_write",
                "passed": "待确认" in pending.reply_text
                and len(store.list_pending_confirmations("ou_validation")) == pending_before,
                "reply": pending.reply_text,
            }
        )

        return {
            "passed": all(item["passed"] for item in results),
            "results": results,
            "counts": {
                "action_items": len(store.find_action_items("", include_done=True)),
                "calendar_events": len(store.list_calendar_events()),
                "schedule_blocks": len(store.list_schedule_blocks()),
                "agent_runs": len(store.list_agent_runs(limit=100)),
                "tool_runs": len(store.list_tool_runs(limit=100)),
                "sent_texts": len(feishu.sent_texts),
                "sent_cards": len(feishu.sent_cards),
                "synced_tasks": len(feishu.synced_tasks),
                "synced_calendar_events": len(feishu.synced_calendar_events),
                "synced_audits": len(feishu.synced_audits),
            },
            "sync_samples": {
                "tasks": feishu.synced_tasks[-3:],
                "calendar": feishu.synced_calendar_events[-3:],
                "audit": feishu.synced_audits[-3:],
            },
        }


def write_results(summary: dict) -> None:
    out_dir = Path("validation")
    out_dir.mkdir(exist_ok=True)
    (out_dir / "mvp_80_results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 80% MVP Validation Results", ""]
    lines.append(f"Overall: {'PASS' if summary['passed'] else 'FAIL'}")
    lines.append("")
    for item in summary["results"]:
        lines.append(f"## {item['scenario']}")
        lines.append(f"- Status: {'PASS' if item['passed'] else 'FAIL'}")
        if item.get("reply"):
            lines.append(f"- Reply: {item['reply']}")
        lines.append("")
    lines.append("## Counts")
    for key, value in summary["counts"].items():
        lines.append(f"- {key}: {value}")
    (out_dir / "mvp_80_results.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    summary = asyncio.run(run())
    write_results(summary)
    print(json.dumps({"passed": summary["passed"], "count": len(summary["results"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
