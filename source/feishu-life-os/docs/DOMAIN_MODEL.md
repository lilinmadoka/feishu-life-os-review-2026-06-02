# Domain Model

## Capture

Raw input from Feishu or local API. Stored in `core_captures`.

Fields: `id, source, source_message_id, source_event_id, sender_id, chat_id, content_type, raw_text, attachment_refs, received_at, processed_status, created_at`.

## Evidence

Evidence chain for a capture. Stored in `evidences`.

Fields: `id, capture_id, evidence_type, content_ref, original_filename, source_url_or_message_id, created_at`.

## ActionItem

Ordinary task. Stored in `action_items`.

Fields: `id, title, description, status, priority, due_at, estimated_minutes, project_id, person_id, source_capture_id, confidence, created_at, updated_at`.

## CalendarEvent

Explicit time range. Stored in `calendar_events`.

Fields: `id, title, description, start_at, end_at, location, status, source_capture_id, feishu_event_id, confidence, created_at, updated_at`.

## ScheduleBlock

Fixed unavailable time, such as school timetable or fixed work. Stored in `schedule_blocks`.

Fields: `id, title, recurrence_rule, start_time, end_time, timezone, status, source_capture_id, created_at, updated_at`.

## Other Objects

Implemented tables also include `reminders`, `commitments`, `waiting_for`, `projects`, `persons`, `core_agent_runs`, `tool_runs`, and `confirmations`.

## Candidate vs Confirmed

Candidates are not persisted as final tasks/events directly. They are stored inside `confirmations.proposed_tool_calls_json`. `resolve_confirmation` creates final `action_items`, `calendar_events`, or `schedule_blocks`.
