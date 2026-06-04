# Model-First Runtime Redesign Plan

Date: 2026-06-05

## 1. Why this redesign is needed

The latest implementation now has two strong foundations:

1. `ContextCompiler` can provide compact, provider-readable context capsules.
2. `Visual Observability` can show trace spans, context artifacts, provider output summaries, planner outcomes, tool results, and state diffs.

However, the review memo in `docs/12_MODEL_FIRST_ARCHITECTURE_GAP_ANALYSIS.md` identifies a deeper architecture problem: semantic authority is still split across several layers.

Current effective shape:

```text
User message
  -> ContextCompiler
  -> Provider / model
  -> provider post-processing rules
  -> PlannerService raw_text interpretation
  -> ToolRouter legacy planning support
  -> SQLite / Feishu
```

This means natural language can still be reinterpreted after the model has produced a decision. The observed failure:

```text
User: 你这个候选计划我看不懂
```

was treated as a plan refinement instead of feedback about the card/proposal. This is not a single bad keyword rule; it is a boundary problem.

## 2. New architecture principle

Adopt this principle as a hard runtime rule:

```text
The model is the only component that interprets user natural language.
Backend code validates model decisions, stores state, computes deterministic facts, enforces risk boundaries, renders UI, and executes confirmed operations.
```

This does **not** mean the model can write data directly. It means:

- semantic classification is model-owned;
- proposal creation/refinement intent is model-owned;
- proposal patch content is model-owned and schema-validated;
- backend deterministic logic expands and validates explicit model decisions;
- writes remain behind RiskPolicy and confirmation cards.

## 3. Target pipeline

```text
CaptureIn
  -> ContextCompiler
       facts / summaries / evidence refs / forbidden actions
  -> ModelPlanner
       AssistantDecision schema
  -> DecisionPolicy
       validates decision type, allowed transitions, risk boundary
  -> PlannerRuntime
       persists proposals, applies explicit proposal_patch, expands deterministic schedules
  -> ConfirmationBoundary
       creates user-facing confirmation cards for writes
  -> ToolExecutor
       executes only concrete read-only or confirmed operations
  -> SQLite / Feishu / ReminderWorker
```

Important naming intent:

- `ModelPlanner` replaces the current provider-as-postprocessor role.
- `PlannerRuntime` replaces PlannerService's raw-text interpretation role.
- `ToolExecutor` is the future slim version of ToolRouter.
- `ConfirmationBoundary` becomes explicit instead of being embedded in ToolRouter.

## 4. New contracts

### 4.1 AssistantDecision

The provider should return one `AssistantDecision`, not direct execution-oriented tool calls.

Suggested schema:

```python
class AssistantDecision(BaseModel):
    decision_schema_version: int = 1
    action: Literal[
        "reply",
        "ask_clarification",
        "create_proposal",
        "refine_proposal",
        "explain_proposal",
        "regenerate_proposal_card",
        "prepare_tool_confirmation",
        "resolve_confirmation",
        "query"
    ]
    confidence: float = Field(ge=0, le=1)
    reasoning_summary: str = ""
    reply_to_user: str = ""
    referenced_context: list[str] = Field(default_factory=list)  # capsule ids / confirmation ids / plan ids
    proposal: AssistantProposal | None = None
    proposal_patch: ProposalPatch | None = None
    query: QueryRequest | None = None
    confirmation_action: ConfirmationAction | None = None
    candidate_operations: list[ConcreteOperation] = Field(default_factory=list)
    ui_action: UIAction | None = None
    uncertainty: list[str] = Field(default_factory=list)
```

`referenced_context` is important for observability: it explains which context capsules or active state the model used.

### 4.2 ProposalPatch

`ProposalPatch` must be explicit and structured. It replaces backend raw-text parsing in `PlannerService`.

```python
class ProposalPatch(BaseModel):
    plan_draft_id: str
    patch_type: Literal["merge_fields", "replace", "cancel", "pause", "explain_only"]
    fields: dict[str, Any] = Field(default_factory=dict)
    user_visible_summary: str = ""
    missing_info: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
```

Rules:

- Backend may apply `fields` only after schema validation.
- Backend may not infer additional fields from raw user text.
- If action is `explain_proposal`, backend renders explanation and does not mutate draft.
- If action is `regenerate_proposal_card`, backend renders a clearer card and does not mutate the domain payload unless an explicit patch is present.

### 4.3 ConcreteOperation

Concrete operations are backend-executable but still must be gated by policy and confirmation.

```python
class ConcreteOperation(BaseModel):
    operation: Literal[
        "create_task",
        "update_task",
        "complete_task",
        "cancel_task",
        "create_calendar_event",
        "update_calendar_event",
        "cancel_calendar_event",
        "create_schedule_block",
        "update_schedule_block",
        "disable_schedule_block_reminders",
        "cancel_schedule_block",
        "sync_feishu_task",
        "sync_feishu_calendar"
    ]
    risk_level: RiskLevel
    requires_confirmation: bool = True
    arguments: dict[str, Any]
```

Initially this can wrap existing `AgentToolCall`, but the target is to separate model semantic decisions from execution tool names.

## 5. Component boundaries

### 5.1 ContextCompiler

Allowed:

- Summarize factual state.
- Include compact evidence refs.
- Include missing fields.
- Include forbidden actions.
- Include mechanical hints only, such as: bare “确认” can resolve the latest pending confirmation.

Not allowed:

- Strongly route ordinary follow-up text to active plan refinement.
- Tell the model that a message “should refine” unless the condition is mechanical and unambiguous.
- Include internal plan dictionaries in user-visible summaries.

Change request:

- Review `decision_hints` in all capsules.
- Replace imperative hints with neutral facts where possible.
- Keep confirmation-specific hints because “确认/取消” resolution is mechanical.

### 5.2 ModelPlanner / Provider

Allowed:

- Call the model.
- Validate model response schema.
- Repair invalid JSON only when semantics are preserved.
- Return `AssistantDecision`.

Not allowed:

- `_looks_like_*` semantic routing after model output.
- `_should_*` semantic overrides after model output.
- Directly mapping raw text into business planning calls without an explicit model decision.

Migration strategy:

- Keep old provider code behind `legacy_model_adapter` temporarily.
- Add `ModelDecisionProvider` as a new path.
- Route only selected test cases to `ModelDecisionProvider` first.

### 5.3 DecisionPolicy

This extends the existing `RiskPolicy` idea.

Responsibilities:

- Validate allowed action for current context.
- Validate that query decisions do not contain write operations.
- Validate that create/refine proposal actions do not directly execute writes.
- Validate that `resolve_confirmation` references an existing pending confirmation or relies on a clear latest pending confirmation.
- Validate risk level and confirmation requirement for `candidate_operations`.

### 5.4 PlannerRuntime

Allowed:

- Persist a new `AssistantProposal`.
- Apply explicit `ProposalPatch`.
- Expand deterministic schedule previews from explicit fields.
- Generate user-facing proposal view models.
- Generate concrete operations from explicit proposal state.

Not allowed:

- `_infer_kind(raw_text)`.
- `_method_from_text(raw_text)`.
- `_byday_from_text(raw_text)`.
- `_duration_days_from_text(raw_text)`.
- `_should_refine_active_proposal(raw_text, response, sender_id)`.

Temporary exception:

- Existing legacy parsing can remain inside `LegacyPlannerAdapter`, but it must not be invoked after a model decision unless explicitly marked as legacy fallback.

### 5.5 CardRenderer / ViewModel layer

Problem:

Proposal cards currently can expose internal fields like `latest_user_reply`, `byday`, and `frequency`.

Target:

```text
PlanDraft payload -> ProposalViewModel -> Feishu card
```

Rules:

- User-facing cards never render raw candidate plan dicts.
- Internal fields remain in metadata only.
- Card content uses natural language sections:
  - What I understood
  - What is still missing
  - Candidate schedule preview
  - Why confirmation is needed
  - Actions: explain / modify / confirm / cancel

### 5.6 ConfirmationBoundary

Move confirmation creation semantics out of the generic ToolRouter concept.

Responsibilities:

- Convert concrete operations into confirmation rows.
- Render confirmation cards from view models.
- Resolve confirmation after user action.
- Produce state diffs for observability.

### 5.7 ToolExecutor

Future slimmed ToolRouter.

Allowed:

- Read-only queries.
- Execute confirmed concrete operations.
- Sync Feishu best-effort.
- Record tool runs and state diffs.

Not allowed:

- Planning-only natural-language interpretation.
- Habit/course timetable parsing.
- PlanDraft semantic refinement.

## 6. Migration plan

### Phase A: Lock current failure with tests

Add regression tests before refactor:

1. Active PlanDraft + user says “你这个候选计划我看不懂”:
   - should not mutate `PlanDraft.payload.latest_user_reply`;
   - should not infer `byday`, `frequency`, `duration`, or method;
   - should produce an explanation/clarification response or a clearer card action.

2. Active PlanDraft + user says “改成每周日早上 8 点，持续 4 周”:
   - should mutate only if model explicitly returns `refine_proposal` with `proposal_patch`.

3. Provider returns `reply` / `explain_proposal`:
   - PlannerRuntime must not mutate plan state.

4. Query intent with candidate write operations:
   - DecisionPolicy rejects.

### Phase B: Introduce AssistantDecision without removing old path

- Add schemas:
  - `AssistantDecision`
  - `ProposalPatch`
  - `ConcreteOperation`
  - `UIAction`
  - `ConfirmationAction`
- Add `ModelDecisionProvider` adapter that wraps old provider output where needed.
- Add tests for schema validation and policy validation.

### Phase C: Add DecisionPolicy

- Keep current `RiskPolicy` for tool calls.
- Add `DecisionPolicy` for `AssistantDecision`.
- Orchestrator flow becomes:

```text
provider.run_decision(request)
  -> DecisionPolicy.validate(decision)
  -> PlannerRuntime.apply(decision)
  -> ConfirmationBoundary / ToolExecutor
```

### Phase D: Refactor PlannerService into PlannerRuntime + LegacyPlannerAdapter

- Move raw-text parsing helpers into `LegacyPlannerAdapter`.
- New `PlannerRuntime` accepts only structured `AssistantDecision` / `ProposalPatch`.
- Old tests continue through legacy path during migration.
- New model-first tests use PlannerRuntime directly.

### Phase E: Refactor ToolRouter into ConfirmationBoundary + ToolExecutor

- Keep old `ToolRouter` class initially.
- Add internal classes:
  - `ConfirmationBoundary`
  - `ToolExecutor`
- Move confirmation creation/resolution into boundary.
- Move concrete operation execution into executor.
- Mark planning-only methods as deprecated.

### Phase F: CardRenderer ViewModels

- Add `PlanDraftViewModel` and `ConfirmationViewModel`.
- Ensure cards no longer show internal candidate dict details.
- Add snapshot tests for cards.

### Phase G: Tighten ContextCompiler hints

- Remove or soften strong plan-refinement decision hints.
- Add tests ensuring active plan draft capsule does not force feedback into refinement.

## 7. Orchestrator target shape

Current orchestrator can keep observability, but semantic flow should become:

```python
compiled = context_compiler.compile(capture)
request = compiled.provider_request(...)

with trace.span("model_planner.run"):
    decision = model_planner.run(request)

with trace.span("decision_policy.validate"):
    decision_policy.validate(decision, request)

with trace.span("planner_runtime.apply"):
    outcome = planner_runtime.apply(decision, request, capture_id, sender_id)

with trace.span("confirmation_boundary.or_execute"):
    final = confirmation_boundary.or_execute(outcome)
```

No step after `model_planner.run` may infer natural-language meaning from `raw_text`.

## 8. Observability changes needed

Visual Observability should help prove the new invariant.

Add trace fields/events:

- `semantic_authority=model`
- `decision.action`
- `decision.referenced_context`
- `decision_policy.result`
- `backend_semantic_fallback_used=false`
- `legacy_planner_adapter_used=true/false`

Add dashboard section:

```text
Semantic Authority
  model decision: explain_proposal
  backend fallback: no
  legacy adapter: no
  plan mutation: no
```

For migration, warn when:

- backend semantic fallback is used;
- PlannerRuntime touches raw_text;
- ToolRouter legacy planning support is invoked;
- Context capsule imperative decision hints are rendered.

## 9. Codex implementation guidance

Do not attempt the full refactor in one patch. Start with tests and schemas.

### Codex Task 1: lock failure and add schemas

```text
Read docs/12_MODEL_FIRST_ARCHITECTURE_GAP_ANALYSIS.md and docs/13_MODEL_FIRST_RUNTIME_REDESIGN.md.

Implement Task 1 only:
1. Add tests that reproduce the active PlanDraft feedback failure:
   - user says “你这个候选计划我看不懂”
   - the system must not mutate the active PlanDraft as a refinement.
2. Add new schemas in app/core/decision_schemas.py:
   - AssistantDecision
   - ProposalPatch
   - ConcreteOperation
   - ConfirmationAction
   - UIAction
3. Add DecisionPolicy skeleton with validation tests.
4. Do not change runtime behavior except where required to pass the new failure-locking test.
5. Do not remove existing tools yet.
6. Run pytest and ruff.
```

### Codex Task 2: introduce model decision path behind feature flag

```text
Implement a feature-flagged model-first path:
1. Add CORE_AGENT_RUNTIME_MODE=legacy|model_first, default legacy.
2. Add ModelDecisionProvider adapter.
3. Add orchestrator branch for model_first mode.
4. In model_first mode, PlannerRuntime accepts only AssistantDecision / ProposalPatch.
5. Keep legacy path untouched.
6. Add tests for reply, explain_proposal, refine_proposal, resolve_confirmation.
```

### Codex Task 3: split PlannerRuntime from legacy parsing

```text
1. Create PlannerRuntime with no raw_text interpretation.
2. Move current raw-text parsing helpers behind LegacyPlannerAdapter.
3. Ensure model_first mode does not call LegacyPlannerAdapter.
4. Add observability event when legacy adapter is used.
5. Add tests proving backend_semantic_fallback_used=false in model_first traces.
```

## 10. Acceptance criteria

The redesign is accepted when:

- The observed failure cannot recur in model-first mode.
- Backend code does not infer natural-language semantic patches after model decision.
- Active PlanDraft follow-up behavior is determined by explicit `AssistantDecision.action`.
- Query/read decisions cannot carry write operations.
- Proposal card rendering does not expose raw internal candidate plan dictionaries.
- Tool execution remains behind confirmation boundary.
- Existing legacy tests still pass while model-first tests prove the new invariant.
- Visual Observability can show whether a trace used model-first semantics or legacy fallback.

## 11. What not to do

- Do not add more keyword rules to fix the observed failure.
- Do not make PlannerService smarter at guessing user intent.
- Do not let ContextCompiler decide user intent.
- Do not let ToolRouter continue to grow as a planning layer.
- Do not remove confirmation cards or RiskPolicy.
- Do not rewrite the whole project before locking regression tests.

## 12. Long-term target

Final target:

```text
ContextCompiler = factual compression
ModelPlanner = sole semantic interpreter
DecisionPolicy = schema/risk validation
PlannerRuntime = deterministic state transition and schedule expansion
ConfirmationBoundary = human approval
ToolExecutor = concrete side effects
Visual Observability = proof and diagnosis layer
```

This keeps the project flexible for local models while reducing accidental rigidity from backend semantic rules.
