# Safety Policy

## Allowed Without Confirmation

- `query_today`
- `query_tomorrow`
- `query_week`
- `query_tasks`
- `query_schedule_blocks`
- `check_conflicts`
- text replies

## Requires Confirmation

- task candidate creation
- calendar event candidate creation
- schedule block candidate creation
- task/calendar updates
- completion when target is ambiguous
- deletes/cancels
- batch operations
- repeated calendar creation
- low-confidence modifications

## Enforcement

`RiskPolicy` validates the full AgentResponse and normalizes tool calls before ToolRouter execution. LLM output cannot bypass this because all writes go through ToolRouter.

## Failure Behavior

If the provider is unavailable, returns invalid JSON, or violates policy, the message is recorded and the user gets a safe failure reply. No database mutation is performed beyond audit records.
