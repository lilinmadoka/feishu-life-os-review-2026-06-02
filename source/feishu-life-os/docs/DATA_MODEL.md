# 数据模型

## 1. Capture / 捕获项

捕获项保存“原始输入”，不要求它已经变成任务。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | string | `cap_xxx` |
| raw_text | text | 原始文本、OCR 文本、转写文本 |
| normalized_text | text | 清理空白后的文本 |
| source_type | enum | manual, feishu_bot, feishu_event, email, chat, screenshot, learning_platform, voice, notification, api |
| source_ref | string | 外部 message_id/email_id/file_token 等 |
| attachments | json | 图片、文件、音频、链接 |
| metadata | json | 原始事件、平台信息、调试信息 |
| status | enum | new, parsed, needs_review, archived |
| confidence | float | 抽取结果总体置信度 |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

设计原因：

- 原始信息可能来自多个地方，不能只保存任务标题。
- 低置信度内容也有价值，应该进入 `needs_review`。
- 后续 OCR/ASR/LLM 能基于原始证据重新抽取。

## 2. Action / 行动项

行动项是从捕获项里抽取出的结构化结果。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | string | `act_xxx` |
| capture_id | string | 来源捕获项 |
| title | string | 可执行标题 |
| description | text | 补充说明 |
| intent | enum | task, event, followup, waiting, note, habit, deadline |
| domain | enum | school, tutoring, study, project, communication, personal, other |
| status | enum | inbox, planned, doing, waiting, done, canceled, snoozed |
| priority | enum | P0, P1, P2, P3 |
| energy | enum | low, medium, high |
| due_at | datetime | 截止时间或发生时间 |
| start_at | datetime | 固定事件开始时间，可选 |
| remind_at | datetime | 自定义提醒时间，可选 |
| estimated_minutes | int | 估计耗时 |
| people | json/list | 相关人物 |
| projects | json/list | 相关项目 |
| labels | json/list | 标签 |
| evidence_text | text | 原始证据片段 |
| confidence | float | 抽取置信度 |
| metadata | json | 例如 time_match, time_confidence, possible_duplicate_ids |
| feishu_task_guid | string | 飞书任务 ID/GUID |
| feishu_record_id | string | 多维表格记录 ID |
| created_at | datetime | 创建时间 |
| updated_at | datetime | 更新时间 |

## 3. SyncEvent / 同步事件

用于审计飞书同步过程，尤其是在 dry-run 和调试阶段。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | string | `sync_xxx` |
| target | enum | bitable, task, calendar, webhook |
| entity_type | string | capture/action/review |
| entity_id | string | 本地实体 ID |
| status | string | dry_run, success, skipped, error |
| request_payload | json | 即将发给飞书的 payload |
| response_payload | json | 飞书响应 |
| error | text | 错误信息 |
| created_at | datetime | 创建时间 |

## 4. 状态流转

### Capture

```text
new -> parsed -> archived
new -> needs_review -> parsed
```

### Action

```text
inbox -> planned -> doing -> done
inbox -> waiting -> planned/done
inbox -> snoozed -> inbox/planned
inbox/planned -> canceled
```

## 5. 去重策略

当前实现：

- 对标题做紧凑 fingerprint。
- 与未完成/未取消行动项做相似度比较。
- 标题相似度 >= 0.88 视为重复。
- 标题相似度 >= 0.72 且截止日期相同，也视为可能重复。

后续可增强：

- 使用 embedding。
- 用 `source_ref` 防止同一消息重复处理。
- 对同一来源多次转发做幂等键。
- 允许用户在飞书多维表格里手动合并。

## 6. 为什么不直接以飞书多维表格为唯一数据库

可以，但不建议第一版这样做。

原因：

1. 核心抽取与调试需要本地事务和审计。
2. 飞书 API 有频率、并发和字段类型限制。
3. 离线开发、测试、迁移更方便。
4. 后续接入邮件/OCR/LLM 时，本地数据库更适合作为缓冲层。

最终可以保持“双写”：本地数据库作为系统真相源，多维表格作为可视化和人工修正层。
