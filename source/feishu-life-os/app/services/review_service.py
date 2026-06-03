from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.database import Repository
from app.models import ActionRecord, ActionStatus, ReviewResponse

ACTIVE_STATUSES = [
    ActionStatus.inbox,
    ActionStatus.planned,
    ActionStatus.doing,
    ActionStatus.waiting,
    ActionStatus.snoozed,
]


class ReviewService:
    def __init__(self, repo: Repository, tz: ZoneInfo):
        self.repo = repo
        self.tz = tz

    def daily(self, target_date: datetime | None = None) -> ReviewResponse:
        now = datetime.now(self.tz)
        target = target_date or now
        day_start = target.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        week_end = day_start + timedelta(days=7)

        actions = self.repo.list_actions(statuses=ACTIVE_STATUSES, limit=500)
        sections: dict[str, list[ActionRecord]] = defaultdict(list)
        for action in actions:
            due = action.due_at.astimezone(self.tz) if action.due_at else None
            if action.status == ActionStatus.waiting:
                sections["waiting"].append(action)
            elif due and due < now and action.status != ActionStatus.done:
                sections["overdue"].append(action)
            elif due and day_start <= due < day_end:
                sections["today"].append(action)
            elif due and day_end <= due < week_end:
                sections["next_7_days"].append(action)
            elif action.status == ActionStatus.inbox:
                sections["inbox"].append(action)

        ordered = {
            "overdue": self._sort(sections["overdue"]),
            "today": self._sort(sections["today"]),
            "waiting": self._sort(sections["waiting"]),
            "next_7_days": self._sort(sections["next_7_days"]),
            "inbox": self._sort(sections["inbox"]),
        }
        markdown = self._render_markdown(target, ordered)
        return ReviewResponse(date=target.date().isoformat(), markdown=markdown, sections=ordered)

    def _sort(self, actions: list[ActionRecord]) -> list[ActionRecord]:
        priority_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        return sorted(
            actions,
            key=lambda action: (
                priority_rank.get(action.priority.value if hasattr(action.priority, "value") else action.priority, 9),
                action.due_at or datetime.max.replace(tzinfo=self.tz),
            ),
        )

    def _render_markdown(self, target: datetime, sections: dict[str, list[ActionRecord]]) -> str:
        title = f"# {target.date().isoformat()} 今日行动面板"
        lines = [title, ""]
        names = {
            "overdue": "🔥 已逾期/快炸",
            "today": "✅ 今天要处理",
            "waiting": "⏳ 等别人/需跟进",
            "next_7_days": "📅 未来 7 天",
            "inbox": "📥 未整理收件箱",
        }
        for key, name in names.items():
            items = sections.get(key, [])
            lines.append(f"## {name}（{len(items)}）")
            if not items:
                lines.append("- 无")
            else:
                for action in items[:12]:
                    due = action.due_at.astimezone(self.tz).strftime("%m-%d %H:%M") if action.due_at else "无截止"
                    lines.append(
                        f"- [{action.priority.value}] {action.title} ｜ {due} ｜ {action.domain.value} ｜ {action.id}"
                    )
                if len(items) > 12:
                    lines.append(f"- ……另有 {len(items) - 12} 项")
            lines.append("")
        lines.append("建议：先处理 P0/P1，再清空 3 条收件箱；任何不确定事项只做确认，不强行安排。")
        return "\n".join(lines)
