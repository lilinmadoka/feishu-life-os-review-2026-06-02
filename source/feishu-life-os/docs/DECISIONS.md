# Decisions

## 2026-05-28: Parallel v2 Core

Decision: add `app/core` v2 instead of rewriting legacy route immediately.

Reason: the legacy Feishu path already works enough for real bot testing. v2 needs stable e2e validation before cutover.

## 2026-05-28: Confirmation First For Writes

Decision: task/calendar/schedule-block creation goes into `confirmations` first.

Reason: prevents accidental task/date pollution and follows the product principle that writes are tool-routed and auditable.

## 2026-05-28: Mock First For Feishu Native Sync

Decision: v2 Feishu Task/Calendar/Bitable sync uses mock adapter when credentials or schema are absent.

Reason: external API gaps should not block local architecture, tests, and e2e validation.
