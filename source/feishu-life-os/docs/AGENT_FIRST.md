# Agent-first 私人助理说明

当前主链路已经从“规则抽取 + 写多维表”改为 Agent-first：

1. 飞书机器人私聊消息进入 Agent-first v2 回调 `/api/v2/feishu/events`。
2. 系统校验飞书 token、过滤非私聊、按 `message_id` 幂等。
3. 原始消息先保存到本地 SQLite。
4. `AgentOrchestrator` 构造 `AgentRequest`，默认调用本机 `codex_cli` provider。
5. Codex CLI 只能返回结构化 `AgentResponse`，不能直接改数据库。
6. 本地工具层执行 `create_task`、`query_today`、`update_task_time` 等工具。
7. 最后通过飞书机器人回复用户。

## 配置

`.env` 默认配置：

```env
AGENT_PROVIDER=codex_cli
CODEX_CLI_PATH=C:\Users\Administrator\AppData\Roaming\npm\codex.ps1
AGENT_CODEX_TIMEOUT_SECONDS=300
```

如果 `codex_cli` 不可用，系统会保存原始消息，并回复：

```text
智能处理器未启动/不可用，已记录消息但不会自动处理。
```

不会静默退化为规则抽取或写多维表。

## 多维表定位

多维表现在只是后台展示层和审计层。查询类消息，例如“今天还有什么任务？”，不会被创建成新任务，也不会写入任务输入表。

## 本地网关

继续使用：

```powershell
.\scripts\start_local_gateway.ps1
.\scripts\stop_local_gateway.ps1
.\scripts\status_local_gateway.ps1
```

Agent-first 模式不再需要常驻 Codex review worker。Codex CLI 会在收到飞书私聊消息时由 FastAPI 按需调用。

## 第一轮验收

在飞书私聊机器人里测试：

- `今天还有什么任务？`：机器人回复今天任务列表，不创建新任务。
- `明天下午3点给小王补课，今晚把资料发家长`：机器人回复识别到 2 个事项，并创建结构化任务。
- `把小王补课改到晚上7点`：单一匹配时修改时间，多候选时要求确认。
- 发送截图：先保存附件信息，回复多模态处理状态，不整条塞进多维表。
