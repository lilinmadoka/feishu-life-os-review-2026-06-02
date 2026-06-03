# Agent Protocol

Agent providers must return a single JSON object matching `app/core/agent_response_schema.json`.

```json
{
  "intent": "query_today | create_candidates | update_existing | complete_item | schedule_blocks | smalltalk | unknown",
  "confidence": 0.0,
  "reasoning_summary": "short summary",
  "reply_to_user": "short user-facing reply",
  "tool_calls": [
    {
      "tool_name": "create_task_candidate",
      "risk_level": "low | medium | high",
      "requires_confirmation": true,
      "arguments": {}
    }
  ]
}
```

## Hard Rules

- Query intents cannot call write tools.
- `unknown` can only reply or ask for clarification.
- `create_task_candidate`, `create_calendar_event_candidate`, `create_schedule_block_candidates`, and update/delete/batch tools require confirmation.
- `confirm_*` and `resolve_confirmation` are driven by user confirmation text or card callbacks.
- `cancel_task` always requires confirmation.
- `complete_task` may execute directly only when exactly one task matches; ambiguous matches must ask the user to choose.
- `query_pending_confirmations` is a read-only tool and must not create new confirmations.
- Every AgentRun is stored in `core_agent_runs`.
- Every executed tool is stored in `tool_runs`.

## Providers

- `mock_provider`: deterministic offline provider for tests and local validation.
- `codex_cli_provider`: structured-output wrapper around `codex exec`.
- `lm_studio_provider`: local OpenAI-compatible chat provider for LM Studio.
- `openai_api_provider`: stub.
- `local_multimodal_provider`: stub.
