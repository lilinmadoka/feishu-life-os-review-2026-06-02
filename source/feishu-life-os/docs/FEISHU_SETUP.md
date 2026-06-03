# 飞书接入说明

## Agent-first 80% MVP 配置位置

在飞书开放平台的 **事件与回调** 页面：

- **事件配置**：接收机器人普通消息事件。
  - 订阅方式选择：`将事件发送至开发者服务器`。
  - 请求地址填：`https://<trycloudflare-host>/api/v2/feishu/events`。
  - 添加事件：`im.message.receive_v1`。
- **回调配置**：接收交互卡片按钮点击。
  - 回调地址填：`https://<trycloudflare-host>/api/v2/feishu/card`。
  - 这是卡片 **确认 / 取消** 按钮闭环必须配置的地址。
- **加密策略**：MVP 阶段先不要开启。当前只校验 Verification Token。

如果 Cloudflare quick tunnel 重启，`trycloudflare.com` 域名可能变化；变化后需要同时更新事件配置和回调配置里的 URL。

80% MVP 最低权限：

- 机器人接收消息事件：`im.message.receive_v1`。
- 发送应用机器人消息。
- 发送交互卡片。
- 日历事件创建/写入。
- 任务创建/写入。
- 多维表写入可选，仅作为审计/后台视图，不是事实源。

本地 SQLite 仍然是事实源。飞书任务/日历/多维表同步失败时，主流程不能失败，会在 ToolRun 中记录 failed/staged payload。

本文件给 Codex 使用。当前后端已经把飞书适配层留好，但默认 `FEISHU_SYNC_MODE=dry_run`，不会调用真实飞书接口。

## 1. 推荐接入架构

### 1.1 自建应用机器人

用于：

- 接收用户私聊/群聊消息。
- 接收事件订阅。
- 调用飞书开放 API：多维表格、任务、日历、消息。

### 1.2 自定义机器人 Webhook

用于：

- 简单推送每日复盘。
- 推送低频重要提醒。

自定义机器人适合“往群里推消息”，但不能替代自建应用机器人做完整交互。

### 1.3 多维表格

用于长期存储和手动查看：

- 捕获收件箱。
- 行动项。
- 每日复盘。

### 1.4 飞书任务 / 日历

用于真正行动：

- 非固定时间任务进入飞书任务。
- 固定时间事件进入飞书日历。

## 2. 环境变量

复制 `.env.example` 到 `.env` 后填写：

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_OPEN_API_BASE=https://open.feishu.cn/open-apis
FEISHU_BOT_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
FEISHU_BOT_SECRET=xxx
FEISHU_BITABLE_APP_TOKEN=appxxx
FEISHU_BITABLE_CAPTURE_TABLE_ID=tblxxx
FEISHU_BITABLE_ACTION_TABLE_ID=tblxxx
FEISHU_BITABLE_REVIEW_TABLE_ID=tblxxx
FEISHU_DEFAULT_ASSIGNEE_OPEN_ID=ou_xxx
FEISHU_CALENDAR_ID=primary
```

## 3. 飞书能力核对

Codex 接入时按以下顺序调通。

### Step 1：获取 tenant_access_token

代码位置：`app/adapters/feishu_client.py::tenant_access_token`

当前 endpoint：

```text
POST /open-apis/auth/v3/tenant_access_token/internal
```

请求体：

```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxx"
}
```

期望返回里包含 `tenant_access_token` 和 `expire`。

### Step 2：接多维表格

代码位置：

- `FeishuClient.bitable_batch_create_records`
- `FeishuClient.to_capture_record`
- `FeishuClient.to_action_record`

当前 endpoint：

```text
POST /open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create
```

注意：

- 单次新增最多 500 条记录。
- 写入同一个 table 不要并发过高，否则可能出现 write conflict。
- 字段名必须与多维表格中的字段完全一致。
- 日期字段使用毫秒时间戳。

建议先设置：

```bash
FEISHU_SYNC_MODE=bitable
```

只同步多维表格，避免误创建任务/日历。

### Step 3：接飞书任务

代码位置：

- `FeishuClient.create_task`
- `FeishuClient.to_task_payload`

当前 endpoint：

```text
POST /open-apis/task/v2/tasks
```

当前 payload 是保守版本：

```json
{
  "summary": "任务标题",
  "description": "任务描述与原始证据",
  "due": {
    "time": "2026-05-27T15:00:00+08:00",
    "timezone": "Asia/Singapore",
    "is_all_day": false
  },
  "assignees": [{"id": "ou_xxx"}]
}
```

Codex 必须用飞书调试台校验当前 Task v2 的字段格式；如果 due 字段格式变化，以开放平台为准。

### Step 4：接飞书日历

代码位置：

- `FeishuClient.create_calendar_event`
- `FeishuClient.to_calendar_payload`

当前 endpoint：

```text
POST /open-apis/calendar/v4/calendars/{calendar_id}/events
```

当前 payload：

```json
{
  "summary": "事件标题",
  "description": "描述与证据",
  "start_time": {"timestamp": "1780000000", "timezone": "Asia/Singapore"},
  "end_time": {"timestamp": "1780003600", "timezone": "Asia/Singapore"}
}
```

Codex 需要确认 `calendar_id=primary` 是否适用于当前租户；必要时先调用日历列表接口。

### Step 5：接事件订阅

回调地址：

```text
POST /api/feishu/events
```

当前已支持 URL verification 的常见 challenge 返回。消息事件结构需要按你订阅的具体事件版本调整。

## 4. 多维表格字段

参考：`scripts/bitable_schema.json`

建议建 3 张表：

1. 捕获收件箱
2. 行动项
3. 每日复盘

如果字段类型不完全匹配，优先保证字段名匹配；类型可以先用文本兜底，等流程稳定后再改成单选、多选、日期。

## 5. 权限建议

在飞书开发者后台至少检查：

- 机器人能力。
- 事件订阅能力。
- 多维表格读写相关权限。
- 任务创建/读取/更新相关权限。
- 日历创建/读取/更新相关权限。
- 消息发送权限。

权限名称可能随飞书后台调整，最终以开发者后台的权限列表和 API 调试台报错为准。

## 6. 推荐灰度开关

不要一开始开 `all`。

推荐顺序：

```bash
FEISHU_SYNC_MODE=dry_run   # 默认，只记录 payload
FEISHU_SYNC_MODE=bitable   # 确认字段和数据结构
FEISHU_SYNC_MODE=webhook   # 只推送复盘
FEISHU_SYNC_MODE=task      # 创建任务
FEISHU_SYNC_MODE=calendar  # 创建日历
FEISHU_SYNC_MODE=all       # 全量
```

## 7. 常见问题

### 7.1 为什么不直接把所有事情都建成飞书任务？

因为用户输入里有很多低置信度事项、碎片想法和待确认信息。全部建任务会制造噪音，最后用户反而不看提醒。

### 7.2 截图怎么办？

当前后端支持 attachment 字段，但未内置 OCR。Codex 后续可以接：

- 飞书图片消息下载。
- OCR 服务。
- 将 OCR 文本作为 `CaptureCreate.raw_text` 再走同一条抽取链路。

### 7.3 语音怎么办？

同理，先转写为文本，再走 `POST /api/captures`。

### 7.4 邮件和学习平台怎么办？

最小方案：邮件转发到一个中转服务，中转服务调用 `POST /api/captures`。学习平台通知也先转成文本捕获。
# Agent-first v2 Notes

For the v2 Agent-first route, set the event callback URL to:

```text
https://<public-host>/api/v2/feishu/events
```

Keep the legacy route available until v2 is verified:

```text
https://<public-host>/api/feishu/events
```

Required capabilities:

- Bot receives `im.message.receive_v1`.
- App can send messages to users.
- Interactive cards and card callback are needed for confirmation UX.
- Calendar write permission is needed for real `sync_feishu_calendar`.
- Task write permission is needed for real `sync_feishu_task`.
- Bitable write permission is needed for audit sync.

If any permission is missing, use `CORE_AGENT_PROVIDER=mock_provider`; v2 tests and local validation still run without real Feishu credentials.
