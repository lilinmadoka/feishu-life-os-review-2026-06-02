# Model-First Architecture Gap Analysis

Date: 2026-06-05

This note records a newly observed architecture gap for expert review. It is intentionally a review memo, not an implementation patch. No business code changes are proposed as accepted here.

## Executive Summary

The intended architecture is model-first:

```text
User input -> Context Compiler -> Model planning/decision -> Policy -> Planner runtime -> Confirmation boundary -> Tool execution
```

The current implementation still contains multiple post-model semantic interpreters. As a result, user meaning can be reclassified by Python code after the model has already produced a response. This makes the system feel rigid and rule-driven, and it can override the user's actual conversational intent.

The core gap is not one bad keyword rule. The gap is that the runtime does not yet enforce a single semantic authority.

## Observed Failure

Real user interaction:

```text
User: 今天下午的课调到了下下周周日早上
Assistant card: 计划草案
  目标: 今天下午的课调到了下下周周日早上
  状态: 完善中
  还缺: 执行方式、每次时长、偏好时间、持续周期
  候选计划: 课程表导入 (...)
```

Then:

```text
User: 你这个候选计划我看不懂
Assistant card: 计划草案
  目标: 今天下午的课调到了下下周周日早上
  候选计划: 课程表导入 (latest_user_reply=你这个候选计划我看不懂, byday=SU, frequency=weekly)
```

The second assistant response is especially important: the user was giving feedback about the card readability, but the system treated that sentence as a new structured refinement of the same plan draft.

## Expected Behavior

For a message like "你这个候选计划我看不懂", the system should not update a plan draft unless the model explicitly decides that the user is modifying the draft.

Reasonable model-first outcomes could include:

- explain the current card in natural language
- regenerate the card in a clearer user-facing format
- ask which part is unclear
- acknowledge the proposal is likely wrong and ask whether to reinterpret the original request
- cancel or pause the current draft only if the user requests that

The backend should not infer any of these from rules. It should execute the model's explicit decision after schema validation and risk policy.

## Current Runtime Shape

The documented target pipeline is close to:

```text
CaptureIn
  -> ContextCompiler
  -> Provider
  -> RiskPolicy
  -> PlannerService
  -> ToolRouter
  -> SQLite / Feishu
```

The effective runtime still behaves more like:

```text
CaptureIn
  -> ContextCompiler
  -> Provider / model
  -> Provider post-processing rules
  -> PlannerService raw_text interpretation
  -> ToolRouter legacy planning support
  -> SQLite / Feishu
```

The second pipeline has at least three semantic decision points after the model.

## Suspected Boundary Violations

### 1. Provider does more than provider work

File:

```text
source/feishu-life-os/app/core/providers.py
```

Examples for review:

- `_intent_to_agent_response(...)`
- `_looks_like_*` helpers
- `_should_*` helpers
- deterministic mapping from model intent into business planning calls

Risk:

The Provider is not only calling the model and parsing model schema. It also rewrites or upgrades semantic intent based on backend heuristics.

Target boundary:

Provider should call the model, validate the model output schema, and return a typed model decision. It should not decide that a user utterance "looks like" a plan refinement, a course timetable, a habit, or a follow-up.

### 2. PlannerService interprets user language after the model

File:

```text
source/feishu-life-os/app/core/planner.py
```

Examples for review:

- `_should_refine_active_proposal(...)`
- `refine_active_proposal(...)`
- `_merge_proposal_from_text(...)`
- `_infer_kind(...)`
- `_method_from_text(...)`
- `_byday_from_text(...)`
- `_duration_days_from_text(...)`

Risk:

PlannerService can treat arbitrary raw user text as a proposal patch when an active proposal exists. This is how "你这个候选计划我看不懂" can be persisted as `latest_user_reply` instead of being routed through the model as feedback or clarification.

Target boundary:

PlannerService may persist proposals, apply explicit model-provided patches, perform deterministic schedule expansion, render cards, and prepare confirmation candidates. It should not infer natural-language semantics from `raw_text`.

### 3. ToolRouter still contains legacy planning behavior

File:

```text
source/feishu-life-os/app/core/tools.py
```

Examples for review:

- `schedule_time_budget_plan`
- `start_plan_refinement`
- `refine_plan_draft`
- `generate_plan_schedule_confirmation`
- habit and course timetable planning helpers

Risk:

ToolRouter nominally rejects planning-only direct calls, but the file still contains legacy planning handlers used through PlannerService support paths. This weakens the execution boundary and makes it harder to reason about where planning state changes happen.

Target boundary:

ToolRouter should execute concrete confirmed operations and low-risk read-only queries. Planning-only behavior should live behind a model-driven Planner runtime, not in a legacy execution router.

### 4. Context capsules can become strong routing hints

Files:

```text
source/feishu-life-os/app/core/context/
```

Risk:

Context Compiler is supposed to compress facts and evidence. If capsules include strong decision hints such as "this should refine the active draft", the model can be biased into continuing an old state even when the user's latest message is feedback, correction, or smalltalk.

Target boundary:

Capsules should expose compact facts, missing information, forbidden actions, and evidence refs. They should avoid imperative routing language unless the decision is purely mechanical, such as resolving a bare "确认" against a pending confirmation.

### 5. Card rendering leaks internal plan details

File:

```text
source/feishu-life-os/app/core/planner.py
```

Observed issue:

The proposal card showed internal fields such as:

```text
latest_user_reply=...
byday=SU
frequency=weekly
```

Risk:

Even when the internal representation is valid, exposing implementation fields makes the assistant feel mechanical and hard to understand. It also makes debugging harder because users react to backend implementation details rather than the intended proposal.

Target boundary:

Card rendering should use a separate user-facing view model. Internal candidate plan payloads should remain in metadata or persisted state, not visible card markdown.

## Architectural Principle Under Review

Recommended principle:

```text
The model is the only component that interprets user natural language.
Backend code validates, stores, computes deterministic facts, enforces risk boundaries, renders UI, and executes confirmed tools.
```

This does not mean the model can write data directly. It means semantic classification and proposal updates should be model decisions, while the backend remains the safety and execution boundary.

## Possible Target Shape

One possible direction for expert evaluation:

```text
User message
  -> ContextCompiler
       facts, summaries, evidence refs, forbidden actions
  -> ModelPlanner
       AssistantDecision schema
  -> RiskPolicy
       validates allowed decision and write boundary
  -> PlannerRuntime
       persists proposal, applies explicit proposal_patch, expands deterministic schedules
  -> ConfirmationBoundary
       creates confirmation card for writes
  -> ToolRouter
       executes only concrete confirmed tool calls or read-only queries
```

Potential `AssistantDecision` actions:

- `reply`
- `ask_clarification`
- `create_proposal`
- `refine_proposal`
- `explain_proposal`
- `regenerate_proposal_card`
- `prepare_tool_confirmation`
- `resolve_confirmation`

Open question for reviewers:

Should `AssistantProposal` remain separate from `AssistantDecision`, or should it become one payload variant inside a larger decision schema?

## Evaluation Questions For Experts

1. Where should the exact boundary be between model semantic planning and backend deterministic planning?
2. Should `Provider` return only `AssistantDecision`, with no direct tool calls?
3. Should legacy planning-only tools be removed from `ToolRouter` entirely, or kept as internal PlannerRuntime adapters during migration?
4. How should active PlanDraft context be exposed without biasing every follow-up into refinement?
5. How should the model express "explain this proposal/card" versus "modify this proposal"?
6. What minimum schema is needed so model output remains flexible but auditable?
7. Should user-facing cards be rendered only from view models, never raw candidate plan dictionaries?
8. How should existing tests be reorganized to prove that no backend code interprets natural language after the model?

## Non-Goals For This Review Memo

- No new keyword rules.
- No immediate code patch.
- No change to real `.env`.
- No real Feishu synchronization changes.
- No database migration proposal yet.
- No frontend observability redesign beyond recording the problem.

## Suggested Review Focus

Please review the following source areas first:

```text
source/feishu-life-os/app/core/providers.py
source/feishu-life-os/app/core/planner.py
source/feishu-life-os/app/core/tools.py
source/feishu-life-os/app/core/context/
source/feishu-life-os/tests/test_core_agent_v2.py
source/feishu-life-os/tests/test_context_compiler.py
```

The key question is not "which rule should be changed". The key question is how to enforce a single semantic authority while preserving confirmation safety and deterministic execution.
