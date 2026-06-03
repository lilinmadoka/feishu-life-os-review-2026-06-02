# LLM 抽取提示词设计

当前代码使用规则抽取器，不依赖 LLM。后续可以把 LLM 作为增强层，但必须遵守“保守、可追溯、低置信度不强提醒”的原则。

## 1. System Prompt

```text
你是一个个人事务抽取器。你的任务是从用户输入中抽取行动项、事件、等待事项、截止日期、提醒和笔记。

要求：
1. 不要编造输入中没有的信息。
2. 每个结果必须保留 evidence_text，直接引用或概括原始证据。
3. 时间不明确时不要强行补全；用 null，并降低 confidence。
4. 固定时间发生的事情标为 event；有截止但不固定发生时间的事情标为 deadline/task。
5. 等别人回复或等待外部结果标为 waiting。
6. 无法判断但可能有用的信息标为 note。
7. 输出必须是合法 JSON，不要输出 Markdown。
```

## 2. User Prompt Template

```text
当前日期时间：{now_iso}
时区：{timezone}

请从以下输入抽取结构化事项：

---
{raw_text}
---

输出 JSON，格式如下：
{
  "items": [
    {
      "title": "string",
      "description": "string|null",
      "intent": "task|event|followup|waiting|note|habit|deadline",
      "domain": "school|tutoring|study|project|communication|personal|other",
      "due_at": "ISO8601|null",
      "start_at": "ISO8601|null",
      "remind_at": "ISO8601|null",
      "estimated_minutes": 30,
      "people": ["string"],
      "projects": ["string"],
      "labels": ["string"],
      "evidence_text": "string",
      "confidence": 0.0
    }
  ],
  "needs_review_reason": "string|null"
}
```

## 3. Few-shot 示例

### 示例 1

输入：

```text
明天下午3点给学生小王补课，记得今晚把资料发给家长
```

输出：

```json
{
  "items": [
    {
      "title": "给学生小王补课",
      "description": null,
      "intent": "event",
      "domain": "tutoring",
      "due_at": null,
      "start_at": "2026-05-27T15:00:00+08:00",
      "remind_at": null,
      "estimated_minutes": 60,
      "people": ["小王", "家长"],
      "projects": [],
      "labels": ["家教", "固定时间"],
      "evidence_text": "明天下午3点给学生小王补课",
      "confidence": 0.9
    },
    {
      "title": "把资料发给家长",
      "description": null,
      "intent": "task",
      "domain": "tutoring",
      "due_at": "2026-05-26T21:00:00+08:00",
      "start_at": null,
      "remind_at": null,
      "estimated_minutes": 10,
      "people": ["家长"],
      "projects": [],
      "labels": ["沟通", "家教"],
      "evidence_text": "记得今晚把资料发给家长",
      "confidence": 0.82
    }
  ],
  "needs_review_reason": null
}
```

### 示例 2

输入：

```text
老师说周五前提交数据库作业，不要晚交
```

输出：

```json
{
  "items": [
    {
      "title": "提交数据库作业",
      "description": "老师说不要晚交",
      "intent": "deadline",
      "domain": "school",
      "due_at": "2026-05-29T23:59:00+08:00",
      "start_at": null,
      "remind_at": null,
      "estimated_minutes": 90,
      "people": ["老师"],
      "projects": [],
      "labels": ["学校", "作业", "截止"],
      "evidence_text": "老师说周五前提交数据库作业，不要晚交",
      "confidence": 0.86
    }
  ],
  "needs_review_reason": null
}
```

### 示例 3

输入：

```text
这个选题可能可以做成一个飞书插件
```

输出：

```json
{
  "items": [
    {
      "title": "飞书插件选题想法",
      "description": "这个选题可能可以做成一个飞书插件",
      "intent": "note",
      "domain": "project",
      "due_at": null,
      "start_at": null,
      "remind_at": null,
      "estimated_minutes": null,
      "people": [],
      "projects": ["飞书插件"],
      "labels": ["想法", "待澄清"],
      "evidence_text": "这个选题可能可以做成一个飞书插件",
      "confidence": 0.45
    }
  ],
  "needs_review_reason": "只有想法，没有明确行动或时间"
}
```

## 4. 后处理规则

LLM 输出后仍然必须经过后端后处理：

1. 校验 JSON schema。
2. 所有时间转为配置时区。
3. 重新计算 priority。
4. 去重。
5. confidence < 0.55 的事项不强同步飞书任务/日历，只进收件箱。
6. evidence_text 为空的结果丢弃或降置信度。
