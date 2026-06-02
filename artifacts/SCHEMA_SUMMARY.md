# SQLite Schema Summary

Generated from local migrated SQLite schema on 2026-06-02. This file contains table/column metadata only, no row values.

## v2 core tables

### `core_captures`

`id`, `source`, `source_message_id`, `source_event_id`, `sender_id`, `chat_id`, `content_type`, `raw_text`, `attachment_refs`, `received_at`, `processed_status`, `created_at`

### `evidences`

`id`, `capture_id`, `evidence_type`, `content_ref`, `original_filename`, `source_url_or_message_id`, `created_at`

### `action_items`

`id`, `title`, `description`, `status`, `priority`, `due_at`, `estimated_minutes`, `project_id`, `person_id`, `source_capture_id`, `confidence`, `created_at`, `updated_at`

### `calendar_events`

`id`, `title`, `description`, `start_at`, `end_at`, `location`, `status`, `source_capture_id`, `feishu_event_id`, `confidence`, `created_at`, `updated_at`, `plan_draft_id`, `plan_item_id`

### `schedule_blocks`

`id`, `title`, `recurrence_rule`, `start_time`, `end_time`, `timezone`, `status`, `source_capture_id`, `created_at`, `updated_at`, `feishu_event_id`, `reminder_enabled`

### `plan_drafts`

`id`, `kind`, `status`, `title`, `payload_json`, `missing_fields_json`, `source_capture_id`, `sender_id`, `confidence`, `created_at`, `updated_at`

### `confirmations`

`id`, `agent_run_id`, `confirmation_type`, `proposed_tool_calls_json`, `status`, `expires_at`, `feishu_card_id`, `created_at`, `resolved_at`, `sender_id`

### `reminders`

`id`, `target_type`, `target_id`, `remind_at`, `channel`, `status`, `created_at`

### `core_agent_runs`

`id`, `capture_id`, `provider`, `model`, `input_json`, `output_json`, `tool_calls_json`, `latency_ms`, `status`, `error`, `created_at`

### `tool_runs`

`id`, `agent_run_id`, `tool_name`, `input_json`, `output_json`, `status`, `error`, `created_at`

## legacy tables

### `captures`

`id`, `raw_text`, `normalized_text`, `source_type`, `source_ref`, `attachments`, `metadata`, `status`, `confidence`, `created_at`, `updated_at`

### `actions`

`id`, `capture_id`, `title`, `description`, `intent`, `domain`, `status`, `priority`, `energy`, `due_at`, `start_at`, `remind_at`, `estimated_minutes`, `people`, `projects`, `labels`, `evidence_text`, `confidence`, `metadata`, `feishu_task_guid`, `feishu_record_id`, `created_at`, `updated_at`

### `agent_runs`

`id`, `capture_id`, `source_ref`, `provider`, `status`, `request_json`, `response_json`, `tool_results_json`, `error`, `created_at`, `updated_at`

### `review_jobs`

`id`, `job_type`, `capture_id`, `action_ids`, `source_ref`, `status`, `prompt`, `result_json`, `error`, `created_at`, `updated_at`

### `sync_events`

`id`, `target`, `entity_type`, `entity_id`, `status`, `request_payload`, `response_payload`, `error`, `created_at`

## common supporting tables

### `projects`

`id`, `name`, `description`, `status`, `created_at`, `updated_at`

### `persons`

`id`, `name`, `aliases`, `role`, `notes`, `created_at`, `updated_at`

### `commitments`

`id`, `title`, `promised_to`, `due_at`, `linked_action_item_id`, `source_capture_id`, `status`, `created_at`

### `waiting_for`

`id`, `title`, `waiting_for_person`, `expected_at`, `linked_project_id`, `source_capture_id`, `status`, `created_at`

