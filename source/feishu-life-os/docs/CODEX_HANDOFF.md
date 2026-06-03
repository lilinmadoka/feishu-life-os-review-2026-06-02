# Codex 交接清单

## 0. 当前交付物状态

已经完成并可本地运行：

- FastAPI 服务。
- SQLite 持久化。
- 规则抽取器。
- 每日复盘。
- 飞书适配层。
- dry-run 同步审计。
- 测试用例。

还没有完成真实飞书调用的最后校验，因为需要你的飞书租户、应用凭证、表格 token、权限和回调地址。

## 1. 本地跑通

```bash
cd feishu-life-os
python -m venv .venv
source .venv/bin/activate
make dev
cp .env.example .env
make test
make demo
make run
```

访问：`http://localhost:8000/docs`

## 2. 创建飞书自建应用

在飞书开放平台创建企业自建应用：

- 获取 `App ID` 和 `App Secret`。
- 添加机器人能力。
- 配置事件订阅回调 URL。
- 添加必要权限。
- 发布/启用应用到当前租户。

填写 `.env`：

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

先测试：

```python
from app.config import get_settings
from app.adapters.feishu_client import FeishuClient
import asyncio

async def main():
    client = FeishuClient(get_settings())
    print(await client.tenant_access_token())

asyncio.run(main())
```

## 3. 创建多维表格

按照 `scripts/bitable_schema.json` 创建 3 张表：

- 捕获收件箱
- 行动项
- 每日复盘

把 token/id 填进 `.env`：

```bash
FEISHU_BITABLE_APP_TOKEN=appxxx
FEISHU_BITABLE_CAPTURE_TABLE_ID=tblxxx
FEISHU_BITABLE_ACTION_TABLE_ID=tblxxx
FEISHU_BITABLE_REVIEW_TABLE_ID=tblxxx
FEISHU_SYNC_MODE=bitable
```

然后调用：

```bash
curl -X POST http://localhost:8000/api/captures \
  -H 'Content-Type: application/json' \
  -d '{"raw_text":"周五前提交数据库作业", "source_type":"manual"}'
```

检查飞书表格是否新增记录。

## 4. 校验飞书任务 payload

文件：`app/adapters/feishu_client.py`

函数：`to_task_payload`

要做：

1. 在飞书 API 调试台里用当前 payload 测试 `POST /open-apis/task/v2/tasks`。
2. 如果 due/assignees 字段格式不对，按当前文档修正。
3. 成功后把 `FEISHU_SYNC_MODE=task`，只测试 1 条低风险任务。
4. 将返回的 task_guid/task_id 写回本地 `actions.feishu_task_guid`。当前代码还没自动回填，需要补。

建议补丁：

- `SyncService.sync_action` 在 task success 后解析 response，调用 `repo.update_action(action.id, ActionUpdate(feishu_task_guid=...))`。
- 对重复同步加保护：已有 `feishu_task_guid` 的 action 不再创建新任务，而是 update。

## 5. 校验飞书日历 payload

文件：`app/adapters/feishu_client.py`

函数：`to_calendar_payload`

要做：

1. 调用日历列表或确认 `FEISHU_CALENDAR_ID=primary` 可用。
2. 调试 `POST /open-apis/calendar/v4/calendars/{calendar_id}/events`。
3. 固定时间事件才进日历，普通 deadline 不进日历。
4. 返回 event_id 后写回 metadata 或新增字段。

## 6. 接收飞书消息事件

文件：`app/routers/feishu.py`

当前只做了宽松解析：

- URL verification 返回 challenge。
- 尝试从 `event.message.content` 取文本。

Codex 要做：

1. 根据实际订阅事件打印 payload。
2. 支持 text、post、image、file、audio。
3. image/file/audio 先作为 attachment 保存。
4. 后续下载图片做 OCR，音频做 ASR。
5. 做幂等：同一个 `message_id` 不重复创建 capture。

## 7. 截图/OCR

建议新增服务：

```text
app/services/ocr_service.py
```

接口：

```python
class OCRService:
    async def extract_text(self, attachment: Attachment) -> str: ...
```

处理链路：

```text
Feishu image event -> 下载图片 -> OCR -> CaptureCreate(raw_text=ocr_text, source_type=screenshot, attachment=[...])
```

## 8. 邮件入口

最小实现：

```text
POST /api/inbound/email
```

字段：

- from
- subject
- body_text
- received_at
- message_id

正文拼成 `raw_text` 后调用 CaptureService。

## 9. LLM 抽取器

当前规则抽取已经可用，但复杂聊天可能需要 LLM。

建议保留接口：

```python
class Extractor(Protocol):
    def extract(text: str, capture_id: str | None = None) -> list[ActionCreate]: ...
```

LLM 返回必须符合 `docs/PROMPTS.md` 里的 JSON schema，然后再由后端做日期、优先级、去重和低置信度保护。

## 10. 上线前安全检查

- `.env` 不提交。
- 飞书 app_secret 不打日志。
- 事件回调校验签名/verification token/encrypt key。
- Webhook secret 启用。
- 只把必要聊天转发给机器人，不全量抓取私人聊天。
- 对截图/OCR 标记 privacy_level 或敏感标签。
- 所有自动创建任务/日历前先灰度。

## 11. 推荐 issue 拆分

1. `feat(feishu): validate tenant token and webhook send`
2. `feat(bitable): create records and write back record_id`
3. `feat(task): create/update Feishu tasks idempotently`
4. `feat(calendar): create events for fixed-time commitments`
5. `feat(events): parse Feishu message events with idempotency`
6. `feat(ocr): handle image attachments`
7. `feat(email): inbound email capture endpoint`
8. `feat(llm): optional structured extractor`
9. `feat(reminder): scheduled morning/evening review jobs`
10. `chore(security): callback verification and secret redaction`
