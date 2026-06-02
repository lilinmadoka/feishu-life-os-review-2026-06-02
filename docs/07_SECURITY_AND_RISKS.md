# 安全、隐私与主要风险

## 当前安全边界

已有措施：

- 飞书事件 token 校验。
- 飞书发送者 open_id/user_id/union_id 白名单。
- 群聊消息必须 mention 才处理。
- 公开 tunnel 下保护 `/docs` 等本地管理界面。
- 附件只下载到本地 storage，不上传到本审查包。
- 写操作经 RiskPolicy 和确认卡。
- 模型低置信度时进入澄清，不写库。

本审查包已排除：

- `.env`
- `.data`
- SQLite DB
- 附件和图片
- log
- zip handoff package
- 真实 token/open_id/webhook

## 高优先级风险

### 1. 手写迁移不可审计

当前 schema 使用 `CREATE TABLE IF NOT EXISTS` 和 suppress exception 的 `ALTER TABLE`。多人开发后容易出现：

- 字段缺失但启动不失败。
- 本地和部署 schema 不一致。
- 回滚困难。

建议：

- 引入 migration version table。
- 使用 Alembic 或明确 migration scripts。
- 每个迁移有测试。

### 2. Worker 幂等和并发

提醒 worker 是轮询模型。若多个 worker 同时运行，可能重复发卡或重复强提醒。

建议：

- 对 `reminders(target_type,target_id,channel)` 建唯一索引。
- 写提醒状态用事务和 upsert。
- 对 send 操作引入 outbox pattern。

### 3. Prompt/规则/业务逻辑混杂

`app/core/providers.py` 同时承担 prompt 构造、规则兜底、业务防错和 intent 映射，后续变大后难维护。

建议拆分：

- `IntentClassifier`
- `EntityExtractor`
- `SemanticGuard`
- `ProviderTransport`
- `ToolCallMapper`

### 4. 多模态课程表可靠性

当前策略依赖本地多模态模型抽结构。图片表格、低清晰度、错行错列会影响结果。

建议：

- 独立 OCR/table parser 插件接口。
- 保存 evidence text 和置信度。
- 对低置信度强制澄清。
- 建课程表 fixture suite。

### 5. 飞书 API 状态同步

本地先写、飞书同步失败时会降级或记录，但需要完整的重试/补偿策略。

建议：

- outbox/sync queue。
- 明确同步状态。
- 手动重放工具。
- Feishu event id 反查和去重。

## 中优先级风险

- legacy 和 v2 双栈共存，开发者容易改错路径。
- `PlanDraft.payload_json` 缺 per-kind schema。
- `schedule_blocks.recurrence_rule` 只解析部分 RRULE。
- 附件 storage 生命周期未定义。
- 本地 LM Studio 的模型能力、上下文长度、response_format 不稳定。
- 当前运行状态依赖 Windows 本地进程，不是可复现部署。

## 数据隐私建议

- 对 `core_agent_runs.input_json/output_json` 做敏感字段裁剪。
- 附件本地路径不要进入外部日志。
- 飞书 open_id 只在白名单和最小必要记录中保存。
- prompt 样本集脱敏后再用于测试。
- 审查仓库保持 private。

## 审查前可接受状态

作为原型，目前的安全状态可以接受：

- 单用户/少用户。
- 本地运行。
- 飞书白名单。
- 强确认策略。
- 数据不外发到公开仓库。

若进入多人协作或生产化，需要先处理：

1. 正式 git repo 和 CI。
2. migration system。
3. worker 幂等。
4. secrets management。
5. structured observability。

