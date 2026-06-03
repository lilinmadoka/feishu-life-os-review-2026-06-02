# API 合同

默认本地服务：`http://localhost:8000`

## 1. Health

```http
GET /health
```

响应：

```json
{
  "ok": true,
  "env": "local",
  "sync_mode": "dry_run"
}
```

## 2. 创建捕获项

```http
POST /api/captures
Content-Type: application/json
```

请求：

```json
{
  "raw_text": "明天下午3点给学生小王补课，记得今晚把资料发给家长",
  "source_type": "manual",
  "source_ref": null,
  "attachments": [],
  "metadata": {}
}
```

响应：

```json
{
  "capture": {
    "id": "cap_xxx",
    "raw_text": "...",
    "normalized_text": "...",
    "source_type": "manual",
    "status": "parsed",
    "confidence": 0.83,
    "created_at": "...",
    "updated_at": "..."
  },
  "actions": [
    {
      "id": "act_xxx",
      "title": "明天下午3点给学生小王补课",
      "intent": "event",
      "domain": "tutoring",
      "priority": "P1",
      "due_at": "...",
      "evidence_text": "..."
    }
  ],
  "duplicate_action_ids": []
}
```

## 3. 列出捕获项

```http
GET /api/captures?status=needs_review&limit=50
```

`status` 可选：`new | parsed | needs_review | archived`

## 4. 获取捕获项

```http
GET /api/captures/{capture_id}
```

## 5. 列出行动项

```http
GET /api/actions?status=inbox&status=planned&limit=100
```

参数：

- `status` 可重复。
- `include_done=true` 时包含 done/canceled。

## 6. 获取行动项

```http
GET /api/actions/{action_id}
```

## 7. 更新行动项

```http
PATCH /api/actions/{action_id}
Content-Type: application/json
```

请求示例：

```json
{
  "status": "planned",
  "priority": "P1",
  "due_at": "2026-05-27T15:00:00+08:00",
  "labels": ["tutoring", "有时间"]
}
```

## 8. 完成行动项

```http
POST /api/actions/{action_id}/complete
```

## 9. 每日复盘

```http
GET /api/reviews/daily?date=2026-05-26
```

响应：

```json
{
  "date": "2026-05-26",
  "markdown": "# 2026-05-26 今日行动面板...",
  "sections": {
    "overdue": [],
    "today": [],
    "waiting": [],
    "next_7_days": [],
    "inbox": []
  }
}
```

## 10. 发送每日复盘到飞书 Webhook

```http
POST /api/reviews/daily/send
```

在 `FEISHU_SYNC_MODE=dry_run` 时只记录 sync event，不真实发送。

## 11. 飞书事件回调

```http
POST /api/feishu/events
```

支持 URL verification：

```json
{
  "type": "url_verification",
  "challenge": "xxx"
}
```

响应：

```json
{"challenge": "xxx"}
```

消息事件会被转换成 `CaptureCreate`，但具体字段需要 Codex 根据实际订阅事件版本调整。
