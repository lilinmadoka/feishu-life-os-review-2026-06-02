# Context Compiler 架构设计草案

## 1. 背景与目标

当前 v2 agent runtime 已经形成了比较清晰的安全执行链路：

```text
CaptureIn -> AgentContextPack -> Provider -> RiskPolicy -> PlannerService -> ToolRouter -> SQLite / Feishu
```

其中 `context_builder.py` 已经在做轻量上下文筛选和压缩：它从 SQLite 事实源中抽取近期消息、待确认项、活动中的 PlanDraft、今天/明天/未来 7 天事项、长期任务和固定安排，再组成 `AgentContextPack` 交给 provider。

但随着本地模型、本地多模态、课程表、长期计划、习惯、提醒和飞书同步继续增加，简单“拼上下文”的方式会越来越脆弱：

- Codex 或强长上下文模型可以靠读取大量材料保持稳定，本地模型通常不行。
- 本地模型更依赖明确任务边界、压缩后的事实、严格 schema 和更短上下文。
- 如果上下文压缩不可追溯，模型错误时很难判断是模型错、压缩错、检索漏、还是 planner 解释错。
- 如果长期计划、课程表、提醒状态都靠 prompt 里临时说明，项目很难继续通用化。

本设计建议把现有 `context_builder.py` 演进为 **Context Compiler（上下文编译器）**：

```text
SQLite 事实源 / 附件解析 / 最近对话 / 待确认 / PlanDraft / 日程占用
  -> Domain Compressors
  -> Context Capsules
  -> AgentContextPackV2
  -> Provider / 总参
  -> PlannerService / RiskPolicy / ToolRouter
```

核心原则：

> 不把模型当记忆体，而把模型当判断器；事实、记忆、状态机、压缩、执行都留在模型外部。

## 2. 与现有架构的关系

本提案不是替换现有 v2 runtime，而是收敛现有职责边界：

| 当前模块 | 建议演进 |
| --- | --- |
| `app/core/context_builder.py` | 演进为 `app/core/context/compiler.py` 和多个 compressor |
| `AgentContextPack` | 保留兼容，新增 `AgentContextPackV2` / `ContextCapsule` |
| `providers.py` | 逐步减少业务规则和上下文拼装逻辑，只消费编译后的上下文 |
| `PlannerService` | 继续负责 AssistantProposal、PlanDraft、计划状态推进和候选 tool call 生成 |
| `RiskPolicy` | 继续作为模型输出和工具调用的安全门 |
| `ToolRouter` | 逐步收敛为具体执行层，不再承载复杂规划逻辑 |
| `StateStore` / SQLite | 继续作为唯一事实源 |

目标链路：

```text
FastAPI Router
  -> CoreAgentOrchestrator
  -> ContextCompiler.compile(capture, purpose)
  -> Provider.run(compiled_context)
  -> RiskPolicy.validate_response(response)
  -> PlannerService.plan_response(response, compiled_context)
  -> ToolRouter.execute_calls(...)
  -> Confirmation / SQLite / Feishu
```

## 3. 关键抽象

### 3.1 ContextCapsule

`ContextCapsule` 是给模型看的最小上下文单元。它必须可追溯、可失效、可测试。

建议 schema：

```python
class ContextCapsule(BaseModel):
    capsule_id: str
    domain: str
    purpose: str
    summary: str
    facts: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    decision_hints: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    relevance_score: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.5, ge=0, le=1)
    freshness: str = "live"
    expires_at: datetime | None = None
    token_estimate: int | None = None
```

字段含义：

- `domain`: `confirmation`、`plan_draft`、`schedule`、`task`、`recent_turn`、`preference`、`attachment` 等。
- `summary`: 给模型看的自然语言简报。
- `facts`: 可被后端验证的结构化事实。
- `assumptions`: 压缩器或模型推断，不可当成事实直接写库。
- `missing_info`: 当前任务仍缺的信息。
- `decision_hints`: 对总参的路由建议，例如“这像是在补充 active habit plan”。
- `forbidden_actions`: 明确禁止总参直接建议的动作，例如“不要直接创建日历”。
- `evidence_refs`: 指向 `capture_id`、`plan_draft_id`、`confirmation_id`、`calendar_event_id` 等。
- `freshness` / `expires_at`: 用于处理日程、提醒、待确认等易过期上下文。

### 3.2 AgentContextPackV2

建议在不破坏现有 provider 的情况下新增 v2 包装：

```python
class AgentContextPackV2(BaseModel):
    context_schema_version: int = 2
    current_message: dict[str, Any]
    system_brief: str
    safety_rules: list[str]
    available_intents: list[str]
    capsules: list[ContextCapsule]
    context_trace: dict[str, Any]
    budgets: dict[str, Any]
```

为了兼容现有 `AgentContextPack`，可以先让 `ContextCompiler` 同时输出：

```python
class CompiledContext(BaseModel):
    legacy_pack: AgentContextPack
    v2_pack: AgentContextPackV2
```

短期 provider 仍吃 legacy pack；新增 provider 或本地模型路径优先吃 v2 pack。

## 4. 首批 compressor

### 4.1 PendingConfirmationCompressor

目标：让模型稳定理解“确认/取消”到底对应哪张卡。

输入：

- `store.list_pending_confirmations(sender_id=...)`
- proposed tool calls
- confirmation status / expires_at

输出示例：

```json
{
  "domain": "confirmation",
  "summary": "当前有 1 条待确认操作：habit_schedule，候选为每天 20:00 跑步 30 分钟，持续 30 天。用户说确认/取消时应解析为 resolve_confirmation。",
  "facts": [
    {"confirmation_id": "conf_xxx", "type": "habit_schedule", "status": "pending", "candidate_count": 31}
  ],
  "decision_hints": ["plain confirmation text should resolve the latest pending confirmation"],
  "forbidden_actions": ["do not create new items when the user only says confirm/cancel"],
  "evidence_refs": [{"kind": "confirmation", "id": "conf_xxx"}]
}
```

### 4.2 ActivePlanDraftCompressor

目标：压缩正在完善的长期计划、习惯和课程表。

输入：

- active `plan_drafts`
- `payload["assistant_proposal"]`
- missing fields
- planned events preview

输出重点：

- 当前计划类型和目标。
- 已知字段。
- 缺失字段。
- 下一步应该 refine、generate schedule confirmation，还是等待用户确认。
- 明确禁止直接写日历。

### 4.3 ScheduleAvailabilityCompressor

目标：把日历事件和固定安排压缩成和当前问题相关的 busy/free 摘要。

输入：

- `calendar_events`
- `schedule_blocks`
- 当前问题中的 day/window/focus

输出：

- 忙碌区间。
- 可用区间。
- 是否有足够连续时间。
- 冲突候选。

这部分最好尽量确定性计算，不交给模型自行推理。

### 4.4 RecentTurnCompressor

目标：压缩最近对话，使短句 follow-up 可被本地模型理解。

示例：

```text
上一轮助手生成了锻炼习惯草案，缺少每次时长和持续周期。
用户本轮“每次 30 分钟，先坚持一个月”应视为补充 active habit plan，不是新任务。
```

### 4.5 UserPreferenceCompressor

目标：把长期偏好从 prompt 中抽出，作为事实源或配置源的一部分。

初期可以从配置和硬编码规则开始，后续再建 `user_preferences` 表。

示例偏好：

- 固定安排保留在查询和日历同步中，但可关闭提醒。
- 长期计划、习惯、课程表必须先进入草案或确认卡。
- 晨间汇总默认时间。
- 强提醒策略。

### 4.6 Attachment/OCR Compressor

目标：图片、课程表、截图先解析成结构化事实和置信度，再交给总参。

建议输出：

- OCR 原文摘要。
- 表格结构。
- 字段置信度。
- 低置信字段。
- evidence_refs。
- 禁止直接写日历的提示。

## 5. Provider 侧策略

不同 provider 应使用不同上下文预算，但都应该走同一个 compiler：

| Provider | 建议上下文策略 |
| --- | --- |
| `mock_provider` | 保持轻量，用于测试 |
| `lm_studio_provider` | 优先吃 capsule，严格限制 token，减少原始历史 |
| `openai_api_provider` | 可给更多 capsule 和少量 evidence 摘要 |
| `codex_cli_provider` | 可以给更长上下文，但仍必须消费 compiler 产物，避免和本地模型路径割裂 |

本地模型路径建议：

- 只给当前任务相关 capsule。
- 尽量少给原始历史。
- 强 schema 输出。
- 分阶段：intent -> entity/proposal -> planner。
- 不让模型直接输出高风险写工具。

## 6. 实施路线

### Phase 1：低风险重构

目标：不改变行为，只重组 context 代码。

- 新建 `app/core/context/` 包。
- 把现有 `context_builder.py` 的 schema 和函数迁移或包裹起来。
- 新增 `ContextCapsule`、`AgentContextPackV2`、`CompiledContext` schema。
- `build_agent_context()` 保持兼容，对外仍返回旧 `AgentContextPack`。
- 新增 `ContextCompiler.compile()`，内部先调用旧逻辑，同时生成 v2 pack。

验收：现有测试不变，全部通过。

### Phase 2：首批 compressor

目标：让待确认和 active PlanDraft 的上下文更稳定。

- 实现 `PendingConfirmationCompressor`。
- 实现 `ActivePlanDraftCompressor`。
- 在 `AgentContextPackV2.capsules` 中加入它们。
- 给 provider 的 prompt 增加 capsules 摘要，但保留旧字段兜底。

验收：

- “确认/取消”不误创建新项。
- 用户补充 habit/long-term plan 时能命中 active draft。
- 低置信或缺字段时只澄清，不写库。

### Phase 3：ScheduleAvailabilityCompressor

目标：把时间计算尽量从模型中移出。

- 实现 busy/free window 计算。
- 查询 availability 时优先使用压缩后的 deterministic summary。
- Planner 生成日程候选时引用 availability capsule。

验收：

- 查询空闲时间不创建任务/日程。
- 重排/排程候选可解释冲突来源。

### Phase 4：Context replay 测试

目标：可定位“模型错、压缩错、检索漏、planner 错”。

- 为关键场景存储 compiled context snapshot。
- 增加 `tests/test_context_compiler.py`。
- 增加 fixture：待确认、active habit、active course timetable、固定安排关闭提醒、明天下午可用性。

验收：

- 每个 compressor 有单测。
- 每个 capsule 至少包含 `domain`、`summary`、`evidence_refs`、`confidence`。
- 上下文大小预算可测试。

## 7. 建议目录结构

```text
app/core/context/
  __init__.py
  schemas.py
  compiler.py
  budget.py
  render.py
  compressors/
    __init__.py
    base.py
    confirmations.py
    plan_drafts.py
    schedule.py
    recent_turns.py
    preferences.py
    attachments.py
```

其中：

- `schemas.py`: `ContextCapsule`, `AgentContextPackV2`, `CompiledContext`。
- `compiler.py`: 编排所有 compressors。
- `budget.py`: token/bytes 预算、provider-specific budget。
- `render.py`: 把 capsules 渲染为 provider 可消费的文本或 JSON。
- `compressors/base.py`: compressor protocol。

## 8. 与 PlannerService 的边界

Context Compiler 不应该生成 tool calls，也不应该推进状态机。

它只负责：

- 选取上下文。
- 压缩事实。
- 标注缺失信息。
- 给出非强制 decision hints。
- 提供 evidence_refs。

PlannerService 负责：

- AssistantProposal 保存/合并。
- PlanDraft 状态推进。
- 生成候选日程/任务。
- 将计划转成需要确认的 tool calls。

RiskPolicy 负责：

- 模型响应级安全校验。
- 工具级确认归一。
- 查询/写入隔离。

ToolRouter 负责：

- 执行已确认或低风险工具。
- 生成确认卡。
- resolve confirmation 后写库和同步飞书。

## 9. 需要避免的设计

- 不要把摘要写回事实源覆盖原始数据。
- 不要让总参直接读全库。
- 不要让 compressor 直接调用飞书或写业务表。
- 不要因为 Codex 能读长上下文就绕过 compiler。
- 不要让本地模型直接输出复杂写工具参数。
- 不要把 assumption 当作 fact 执行。
- 不要先上向量库替代结构化查询；当前数据规模下 SQLite + 明确 domain 查询更可控。

## 10. Codex 实施提示

建议 Codex 优先按以下顺序改：

1. 新增 schemas，不改现有 behavior。
2. 写 `ContextCompiler` 包裹现有 `build_agent_context()`。
3. 加 `PendingConfirmationCompressor` 和 `ActivePlanDraftCompressor`。
4. 加单测，不动飞书真实路径。
5. 再让 provider 读取 `capsules`。
6. 最后逐步迁移 `providers.py` 中的业务 guard 到 SemanticGuard / compiler hints / PlannerService。

首个 PR 的范围建议控制在 docs + schemas + compiler skeleton + tests，不直接改 ToolRouter/PlannerService 行为。

## 11. 成功标准

短期成功标准：

- 同样输入下，compiled context 可回放。
- 本地模型看到的上下文更短、更结构化。
- active PlanDraft follow-up 更稳定。
- 待确认确认/取消更稳定。
- 现有测试全部通过。

中期成功标准：

- `providers.py` 里的上下文和业务 guard 明显减少。
- `ToolRouter` 只保留执行逻辑，计划逻辑继续迁出。
- 长期计划、习惯、课程表的上下文都由 capsule 驱动。
- 可以根据 provider 能力自动选择 context budget。

长期成功标准：

- Context Compiler 成为所有模型路径的统一入口。
- Codex、OpenAI、本地 LM Studio 使用同一上下文语义，只是预算不同。
- 每次 agent run 都能回答：模型看到了哪些事实、哪些摘要、哪些假设、哪些 evidence refs。
