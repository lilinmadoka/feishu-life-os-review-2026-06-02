# Architecture

The target architecture is Agent-first:

```text
Feishu private/group message, card callback, attachment
-> Feishu Channel Adapter
-> Capture Store / Evidence
-> Agent Orchestrator
-> RiskPolicy + ToolRouter
-> Domain Tools
-> State Store
-> Feishu Native Sync
-> text reply / interactive card / task / calendar / bitable audit
```

## Principles

- Feishu is the interaction channel, not the brain.
- SQLite is the local fact source; Postgres can replace it later.
- Agent providers return structured `AgentResponse` only.
- LLMs never write the database directly.
- ToolRouter executes all writes and records ToolRuns.
- RiskPolicy forces confirmation for create/update/delete/batch/high-risk operations.
- Query intents cannot create or mutate tasks.

## Current Implementation

- Legacy route retained: `/api/feishu/events`.
- v2 route added: `/api/v2/feishu/events`.
- v2 local verification route: `/api/v2/agent/messages`.
- Domain tables live in new SQLite tables prefixed by the v2 domain names, such as `action_items`, `calendar_events`, `schedule_blocks`, `tool_runs`, `confirmations`.
- Mock Feishu adapter is used when Feishu credentials are absent.
- Real Feishu text/card sending is centralized in `FeishuClient`.

## Cutover Plan

Keep legacy route until v2 is proven with real Feishu credentials. Then set Feishu callback to `/api/v2/feishu/events` or switch `/api/feishu/events` internally to v2.
