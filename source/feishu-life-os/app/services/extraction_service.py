from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models import ActionCreate, ActionIntent, Domain, Energy, Priority
from app.services.normalizer import split_candidate_sentences
from app.services.time_parser import parse_datetime

ACTION_VERBS = [
    "要",
    "需要",
    "记得",
    "別忘",
    "别忘",
    "完成",
    "提交",
    "交",
    "写",
    "改",
    "做",
    "复习",
    "预习",
    "开发",
    "部署",
    "发",
    "联系",
    "确认",
    "整理",
    "准备",
    "报名",
    "缴费",
    "打印",
    "上传",
    "下载",
    "回复",
    "催",
]

EVENT_WORDS = [
    "上课",
    "补课",
    "家教",
    "会议",
    "开会",
    "面试",
    "考试",
    "约",
    "见面",
    "讲座",
    "答辩",
    "汇报",
    "签到",
]

WAITING_WORDS = ["等", "等待", "等回复", "等确认", "对方回复", "老师回复", "家长回复"]
URGENT_WORDS = ["急", "紧急", "马上", "立刻", "今天必须", "今晚必须", "截止", "ddl", "deadline", "别忘", "一定", "不要晚交", "不能晚"]

DOMAIN_KEYWORDS: dict[Domain, list[str]] = {
    Domain.school: ["学校", "作业", "老师", "课程", "论文", "考试", "选课", "教务", "校园", "班级", "小组", "学院", "讲座"],
    Domain.tutoring: ["家教", "学生", "补课", "课时", "家长", "小王", "小李", "小张"],
    Domain.study: ["学习", "复习", "预习", "刷题", "阅读", "课程", "网课", "quiz", "assignment", "lecture", "学习平台"],
    Domain.project: ["项目", "开发", "代码", "codex", "bug", "接口", "api", "部署", "上线", "README", "PR", "repo", "后端", "前端"],
    Domain.communication: ["微信", "飞书", "邮件", "邮箱", "回复", "消息", "联系", "发给", "催", "确认", "通知"],
    Domain.personal: ["买", "取", "洗", "健身", "吃药", "家里", "朋友", "身份证", "护照"],
}

ENERGY_HIGH = ["开发", "写论文", "复习", "设计", "整理", "debug", "阅读", "方案", "实现"]
ENERGY_LOW = ["发", "回复", "确认", "买", "取", "转发", "打印", "下载", "上传"]

TITLE_PREFIX_RE = re.compile(
    r"^(帮我)?(记一下|记下|记录一下|提醒我|帮我记|todo|待办|记得|別忘了?|别忘了?|我需要|我要|需要|要)[:：，,\s]*",
    re.IGNORECASE,
)


class RuleBasedExtractor:
    """Deterministic extractor used before an LLM is connected.

    It is deliberately conservative: uncertain content becomes an inbox item with
    evidence and confidence rather than being silently discarded.
    """

    def __init__(self, tz: ZoneInfo, now_provider: Callable[[], datetime] | None = None):
        self.tz = tz
        self.now_provider = now_provider or (lambda: datetime.now(tz))

    def extract(self, text: str, capture_id: str | None = None) -> list[ActionCreate]:
        candidates = self._split(text)
        actions: list[ActionCreate] = []
        for candidate in candidates:
            if not candidate.strip():
                continue
            action = self._extract_one(candidate.strip(), capture_id=capture_id, full_text=text)
            if action:
                actions.append(action)
        if not actions and text.strip():
            actions.append(self._fallback_note(text, capture_id=capture_id))
        return actions

    def _split(self, text: str) -> list[str]:
        rough = []
        for chunk in split_candidate_sentences(text):
            chunk = re.sub(r"[，,]\s*(?=(记得|别忘|別忘|还要|另外|还有|并且|同时|然后))", "；", chunk)
            rough.extend(part.strip() for part in re.split(r"[；;]\s*", chunk) if part.strip())
        return rough

    def _extract_one(self, text: str, capture_id: str | None, full_text: str) -> ActionCreate | None:
        base = self.now_provider()
        parsed_dt = parse_datetime(text, tz=self.tz, base=base)
        intent = self._infer_intent(text, parsed_dt is not None)
        has_action_signal = any(word.lower() in text.lower() for word in ACTION_VERBS + EVENT_WORDS + WAITING_WORDS)
        if not has_action_signal and parsed_dt is None:
            return None

        title = self._clean_title(text)
        domain = self._infer_domain(text)
        priority = self._infer_priority(text, parsed_dt.value if parsed_dt else None, base)
        energy = self._infer_energy(text)
        people = self._extract_people(text)
        projects = self._extract_projects(text)
        labels = self._build_labels(text, parsed_dt is not None, intent, domain)
        estimated_minutes = self._estimate_minutes(text, intent)
        confidence = 0.4
        if has_action_signal:
            confidence += 0.2
        if parsed_dt:
            confidence += min(parsed_dt.confidence, 0.95) * 0.25
        if domain != Domain.other:
            confidence += 0.1
        confidence = round(min(confidence, 0.95), 2)

        metadata = {}
        if parsed_dt:
            metadata["time_match"] = parsed_dt.matched_text
            metadata["time_confidence"] = parsed_dt.confidence
            metadata["time_all_day"] = parsed_dt.all_day

        return ActionCreate(
            capture_id=capture_id,
            title=title,
            description=None if title == text else text,
            intent=intent,
            domain=domain,
            priority=priority,
            energy=energy,
            due_at=parsed_dt.value if parsed_dt else None,
            estimated_minutes=estimated_minutes,
            people=people,
            projects=projects,
            labels=labels,
            evidence_text=text if len(text) < 500 else text[:497] + "...",
            confidence=confidence,
            metadata=metadata,
        )

    def _fallback_note(self, text: str, capture_id: str | None) -> ActionCreate:
        return ActionCreate(
            capture_id=capture_id,
            title=self._clean_title(text)[:120],
            description=text,
            intent=ActionIntent.note,
            domain=self._infer_domain(text),
            priority=Priority.p3,
            energy=Energy.low,
            labels=["待澄清"],
            evidence_text=text[:500],
            confidence=0.25,
        )

    def _infer_intent(self, text: str, has_time: bool) -> ActionIntent:
        lowered = text.lower()
        if any(word in text for word in WAITING_WORDS):
            return ActionIntent.waiting
        if any(word in text for word in EVENT_WORDS) and has_time:
            return ActionIntent.event
        if "提醒" in text or "催" in text:
            return ActionIntent.followup
        if "截止" in text or "ddl" in lowered or "deadline" in lowered or "前" in text and has_time:
            return ActionIntent.deadline
        return ActionIntent.task

    def _infer_domain(self, text: str) -> Domain:
        lowered = text.lower()
        scores: dict[Domain, int] = {}
        for domain, keywords in DOMAIN_KEYWORDS.items():
            scores[domain] = sum(1 for keyword in keywords if keyword.lower() in lowered)
        best = max(scores, key=scores.get)
        return best if scores[best] > 0 else Domain.other

    def _infer_priority(self, text: str, due_at: datetime | None, base: datetime) -> Priority:
        urgent = any(word.lower() in text.lower() for word in URGENT_WORDS)
        if due_at:
            delta = due_at - base
            if delta <= timedelta(hours=6):
                return Priority.p0
            if delta <= timedelta(hours=24):
                return Priority.p1
            if delta <= timedelta(days=3):
                return Priority.p2 if not urgent else Priority.p1
            return Priority.p2 if (urgent or "前" in text) else Priority.p3
        return Priority.p1 if urgent else Priority.p3

    def _infer_energy(self, text: str) -> Energy:
        lowered = text.lower()
        if any(word.lower() in lowered for word in ENERGY_HIGH):
            return Energy.high
        if any(word.lower() in lowered for word in ENERGY_LOW):
            return Energy.low
        return Energy.medium

    def _estimate_minutes(self, text: str, intent: ActionIntent) -> int | None:
        duration = re.search(r"(\d+(?:\.\d+)?)\s*(小时|h|分钟|min)", text, re.IGNORECASE)
        if duration:
            value = float(duration.group(1))
            unit = duration.group(2).lower()
            return int(value * 60 if unit in {"小时", "h"} else value)
        if intent == ActionIntent.event:
            return 60
        if any(word in text for word in ["回复", "确认", "发给", "打印", "下载"]):
            return 10
        if any(word in text for word in ["开发", "论文", "复习", "整理"]):
            return 90
        return 30

    def _extract_people(self, text: str) -> list[str]:
        people = set(re.findall(r"@([\w\u4e00-\u9fff\-]{1,20})", text))
        for pattern in [r"学生([\u4e00-\u9fffA-Za-z0-9]{1,8})", r"老师([\u4e00-\u9fffA-Za-z0-9]{0,8})", r"家长"]:
            for match in re.findall(pattern, text):
                if isinstance(match, tuple):
                    match = "".join(match)
                label = match if pattern == r"家长" else match.strip()
                if label:
                    people.add(label if label in {"家长"} else label)
        return sorted(people)

    def _extract_projects(self, text: str) -> list[str]:
        projects = set(re.findall(r"#([^#]{1,30})#", text))
        project_match = re.findall(r"([\u4e00-\u9fffA-Za-z0-9_\-]{2,30})项目", text)
        projects.update(project_match)
        return sorted(projects)

    def _build_labels(
        self, text: str, has_time: bool, intent: ActionIntent, domain: Domain
    ) -> list[str]:
        labels = {domain.value, intent.value}
        if has_time:
            labels.add("有时间")
        if any(word.lower() in text.lower() for word in URGENT_WORDS):
            labels.add("紧急")
        return sorted(label for label in labels if label != Domain.other.value)

    def _clean_title(self, text: str) -> str:
        title = TITLE_PREFIX_RE.sub("", text.strip())
        title = re.sub(r"^(另外|还有|还要|并且|然后)[:：，,\s]*", "", title)
        title = re.sub(r"\s+", " ", title)
        return title[:120]
