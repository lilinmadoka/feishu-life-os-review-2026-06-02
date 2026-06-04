# Accelerated Visual Observability 90% Sprint

## 1. Current state

The repository already has Visual Observability Phase 1:

- `app/core/observability/` with trace schemas, SQLite trace store, redaction, and emitter.
- `OBSERVABILITY_ENABLED` configuration.
- Best-effort/no-op behavior when disabled.
- Minimal `CoreAgentOrchestrator` spans.
- Read-only API routes:
  - `GET /api/v2/observability/traces`
  - `GET /api/v2/observability/traces/{trace_id}`
- Admin-token guarded access.
- Tests for disabled no-op behavior, enabled trace creation, route protection, write-failure tolerance, and redaction.

The next target is to reach **90% practical completion** of the visual observability feature in one accelerated implementation pass.

## 2. Definition of 90% complete

For this sprint, 90% complete means:

1. A local dashboard exists and can be opened from the FastAPI app.
2. The dashboard shows a dense, serious, progress-bar-style view of recent traces.
3. A single trace can be inspected through:
   - KPI strip
   - multi-lane dynamic timeline
   - Context Lens
   - Provider/Policy/Planner/Tool sections
   - confirmation/state diff sections
   - artifacts/events panel
4. Context Compiler internals are visible enough to debug model-context problems:
   - capsules generated
   - capsules rendered
   - facts kept/dropped
   - render policy
   - request size
5. Planner, ToolRouter, confirmation, and state mutations emit useful trace events/artifacts/diffs.
6. Feishu adapter and ReminderWorker have at least coarse-grained observability.
7. The UI supports manual refresh and lightweight live polling.
8. Replay is available as a client-side animation over spans.
9. Security/privacy defaults remain conservative.
10. Existing tests pass, and new observability tests cover the dashboard/API shape.

Not required for this sprint:

- OpenTelemetry exporter.
- Langfuse/LangSmith integration.
- Full metrics dashboard.
- Perfect UI polish.
- Full distributed tracing across separate processes.
- Rich chart library or npm build.
- 3D/neural-network-style animation.

## 3. Architectural rule

Visual Observability remains旁路式:

```text
business path executes normally
  -> best-effort emit trace/span/event/artifact/diff
  -> observability failure is swallowed and logged at debug level
  -> dashboard is read-only
```

No observability code may change PlannerService decisions, RiskPolicy decisions, ToolRouter execution semantics, or Feishu sync behavior.

## 4. One-pass Codex task

Copy the following task to Codex.

```text
Read these documents first:
- docs/10_VISUAL_OBSERVABILITY_ARCHITECTURE.md
- docs/11_ACCELERATED_VISUAL_OBSERVABILITY_90_SPRINT.md

Implement the accelerated Visual Observability 90% sprint.

Hard constraints:
1. Do not change business semantics.
2. Observability must remain best-effort and must never fail the main user request.
3. Do not expose raw secrets, open_id/user_id/union_id, absolute attachment paths, full prompt, or full Feishu payload by default.
4. No npm build and no external CDN. Use static HTML/CSS/JS/SVG only.
5. Keep routes admin-token protected.
6. Keep OBSERVABILITY_ENABLED=false as the default no-op path.
7. Run pytest and ruff.
```

## 5. Backend implementation scope

### 5.1 Add UI/API router features

Extend `app/routers/observability.py` with:

```text
GET /api/v2/observability/ui
GET /api/v2/observability/traces/{trace_id}/timeline
GET /api/v2/observability/traces/{trace_id}/graph
GET /api/v2/observability/traces/{trace_id}/artifacts
GET /api/v2/observability/summary
```

Return JSON shapes optimized for the UI:

```python
TraceTimelineResponse:
  trace
  lanes: list[{lane, spans}]
  critical_path_ms
  status_counts
  kpis

TraceGraphResponse:
  nodes: list[{id, label, kind, status, lane, attrs}]
  edges: list[{source, target, label}]

ObservabilitySummary:
  recent_trace_count
  failed_trace_count
  avg_duration_ms
  provider_latency_avg_ms
  policy_block_count
  confirmation_created_count
  feishu_failure_count
```

Implementation can derive timeline/graph from existing `TraceDetail` without adding new tables.

### 5.2 Static dashboard

Add:

```text
app/static/observability/index.html
app/static/observability/observability.css
app/static/observability/observability.js
```

Serve via the observability router. Keep it local/admin-token protected.

UI layout:

```text
┌────────────────────────────────────────────────────────────┐
│ Header: LifeOS Observatory / status / refresh / replay      │
├───────────────┬────────────────────────────────────────────┤
│ Trace List    │ KPI Strip                                   │
│ Filters       ├────────────────────────────────────────────┤
│               │ Multi-lane Dynamic Timeline                 │
├───────────────┼────────────────────────────────────────────┤
│ Context Lens  │ Span Detail / Events / Artifacts / Diffs     │
└───────────────┴────────────────────────────────────────────┘
```

UI behavior:

- Fetch trace list every 2 seconds when live mode is on.
- Selecting a trace loads detail/timeline/graph/artifacts.
- Timeline spans render as progress bars grouped by lane.
- Replay button animates spans in timestamp order.
- Critical path or longest span should be visually emphasized.
- Status colors:
  - ok = steady green/blue
  - warn = amber
  - failed = red
  - blocked = purple/red
  - skipped = muted gray
- Information density should be high; avoid oversized cards.

### 5.3 Trace model helpers

Add helper functions under:

```text
app/core/observability/ui_models.py
```

Responsibilities:

- group spans by lane
- compute relative start/end offsets
- compute status counts
- compute KPI strip
- create graph nodes/edges
- summarize artifacts and state diffs

Do not put this logic in the frontend if it can be derived cleanly in Python.

### 5.4 Context Lens instrumentation

Add trace artifacts/events around Context Compiler and provider request rendering.

Minimum artifact:

```text
kind=context_lens
label=context_v2_summary
redaction=summary_only
payload:
  legacy_size_bytes
  context_v2_size_bytes
  provider_request_size_bytes
  render_policy
  generated_capsules: [{domain, capsule_id, facts_count, evidence_count, confidence}]
  rendered_capsules: [{domain, capsule_id, facts_count, evidence_count}]
  skipped_capsules: [{domain, reason}]
```

Because the current render policy already decides which capsule facts are exposed, the trace should capture that decision explicitly.

Preferred implementation:

- Add a render diagnostics helper in `app/core/context/render.py`, or return a small diagnostics object alongside rendered capsules.
- Keep `CompiledContext.provider_request()` backward-compatible.
- If changing return types is risky, add an optional function such as `render_provider_capsules_with_diagnostics(...)`.

### 5.5 Provider instrumentation

Provider span should record:

```text
provider_name
model
response_format
intent
confidence
tool_names
has_assistant_proposal
reply_length
```

Do not store full prompt or full model output by default. If an artifact is stored, use summary only.

### 5.6 RiskPolicy instrumentation

Add events:

```text
policy.response_validated
policy.violation
policy.call_normalized
policy.confirmation_required
```

Useful attrs:

```text
intent
tool_name
risk_level
requires_confirmation
reason
```

If direct instrumentation inside `RiskPolicy` creates too much coupling, emit these events from Orchestrator/ToolRouter around policy calls.

### 5.7 PlannerService instrumentation

Add spans/events for:

```text
planner.plan_response
planner.save_or_refine_proposal
planner.refine_active_proposal
planner.generate_schedule_preview
planner.cancel_stale_confirmation
```

At minimum, trace these attrs when available:

```text
proposal_id
plan_draft_id
kind
old_status
new_status
missing_fields_count
planned_event_count
confirmation_id
card_sent
```

If adding emitter to PlannerService constructor is too invasive, start with Orchestrator-level planner span plus artifacts derived from `planning` outcome. But to reach 90%, at least PlanDraft status changes should be visible in trace events or state diffs.

### 5.8 ToolRouter / Confirmation instrumentation

Add visibility for:

```text
tool_router.execute_calls
confirmation.create
confirmation.resolve
confirmation.apply_tool_call
```

Minimum state diffs:

- confirmation created
- confirmation resolved/canceled/expired
- action_item created/updated/canceled/done
- calendar_event created/updated/canceled
- schedule_block created/updated/canceled/reminder disabled
- plan_draft status update when confirmation affects it

Only store summaries:

```text
id
title
status
start/end/due time
kind/type
```

Do not store full proposed tool call payload by default.

### 5.9 Feishu adapter coarse spans

Add best-effort span wrapping around adapter methods:

```text
feishu.send_text
feishu.send_card
feishu.sync_task
feishu.sync_calendar_event
feishu.sync_schedule_block
feishu.update_calendar_event
feishu.delete_calendar_event
```

Minimum attrs:

```text
operation
status
target
entity_id
external_id if available
error_class if failed
```

If broad adapter instrumentation is too risky, implement wrapper methods in `CoreAgentOrchestrator` and `ToolRouter` call sites first.

### 5.10 ReminderWorker coarse traces

Add workflow trace around `ReminderWorker.run_once()`:

```text
workflow_type=reminder_worker_run_once
```

Minimum spans:

```text
worker.daily_review
worker.legacy_action_reminders
worker.core_action_item_reminders
worker.core_schedule_reminders
```

Minimum attrs:

```text
sent_count
pre_strong_sent
strong_sent
skipped_disabled_schedule_blocks
error_count
```

If this risks destabilizing worker tests, implement only a top-level trace and one span per existing helper.

## 6. Frontend details

### 6.1 Data loading

Frontend should call:

```text
GET /api/v2/observability/traces?limit=50
GET /api/v2/observability/traces/{trace_id}
GET /api/v2/observability/traces/{trace_id}/timeline
GET /api/v2/observability/traces/{trace_id}/graph
GET /api/v2/observability/summary
```

Admin token handling options:

- Prompt once for admin token and store in `sessionStorage`.
- Send it as `x-admin-token`.
- Do not put token in query string.

### 6.2 Timeline rendering

Represent each span as:

```text
left = span.relative_start_ms / trace.duration_ms
width = max(span.duration_ms / trace.duration_ms, minimum_width)
```

Display compact labels:

```text
provider.run 1210ms
context.compile 35ms
policy.validate ok
```

Clicking a span opens detail panel:

```text
name
component/lane/status
duration
attrs JSON
related events
related artifacts
related state diffs
```

### 6.3 Replay

Client-side replay is enough:

- sort spans by relative_start_ms
- clear active state
- reveal spans over time
- support speed: 0.5x, 1x, 2x, 5x
- pause/resume

### 6.4 Context Lens panel

Show generated/rendered/skipped capsule rows.

Columns:

```text
domain | capsule_id | generated | rendered | facts kept | facts dropped | confidence | evidence | reason
```

If `context_lens` artifact is absent, fallback to `context_v2` artifact or current trace attrs.

### 6.5 Graph panel

MVP graph can be simple SVG:

```text
Capture -> Context -> Provider -> Policy -> Planner -> ToolRouter -> State/Feishu
```

Graph nodes should be derived from spans and artifacts. Do not hand-code a fake flow if the span is absent; show missing nodes as gray only when useful.

## 7. Testing scope

Add/extend tests:

```text
tests/test_observability.py
```

Required tests:

1. UI route requires admin token.
2. UI route returns HTML when admin token is valid.
3. Timeline endpoint returns lanes and span offsets.
4. Graph endpoint returns nodes and edges for an agent message trace.
5. Context Lens artifact exists after an agent message with context_v2.
6. Provider span records intent/confidence/tool names but not raw full prompt.
7. State diff records confirmation creation for a create-candidates message.
8. Trace detail remains redacted: no raw open_id, no absolute local_path, no long raw_text.
9. Observability disabled still creates no traces.
10. Full pytest and ruff pass.

Optional but preferred:

- ReminderWorker run_once creates a worker trace when enabled.
- Feishu send_card creates an external span when a confirmation card is sent.

## 8. Priority order for Codex

Implement in this order to maximize chance of a working next version:

1. Timeline/graph/summary API derived from current trace store.
2. Static dashboard with trace list + KPI + timeline.
3. Context Lens artifact and panel.
4. ToolRouter/confirmation state diffs.
5. Provider/policy/planner events.
6. Feishu coarse spans.
7. ReminderWorker coarse trace.
8. Replay animation.
9. Tests and docs update.

Do not start with a fancy UI before the timeline endpoint and trace data are reliable.

## 9. Acceptance checklist

The sprint is accepted when all are true:

- `OBSERVABILITY_ENABLED=false`: behavior unchanged, no traces.
- `OBSERVABILITY_ENABLED=true`: one local API message creates a trace with spans.
- Dashboard is accessible with admin token.
- Dashboard shows recent traces and a multi-lane progress-bar timeline.
- A trace shows Context Lens information.
- A create-candidates message shows confirmation-related state diff.
- A provider call shows intent/confidence/tool names.
- Sensitive identifiers are not visible in trace detail or dashboard JSON.
- No external CDN/npm build.
- `pytest -q` passes.
- `ruff check app tests` passes.

## 10. Explicit deferrals

Leave these for the final 10%:

- OpenTelemetry exporter.
- Langfuse/LangSmith exporter.
- Multi-process distributed trace propagation.
- Complex charts and historical metrics.
- Full visual polish.
- Long-term trace retention cleanup job.
- Screenshot/image artifact viewer.
- Advanced anomaly detection.

## 11. Notes for future exporter compatibility

Keep names close to common tracing concepts:

- trace
- span
- event
- artifact
- state_diff
- component
- lane
- status
- duration_ms

OpenTelemetry GenAI conventions are still evolving, but they already define GenAI signals for events, exceptions, metrics, model spans, and agent spans. Future exporter work should map LifeOS spans into those concepts without forcing the local schema to become OpenTelemetry-specific.
