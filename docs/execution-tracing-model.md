# Salesforce Execution Tracing Model

## Canonical Timeline

For record-save actions, model these phases:

1. Request accepted.
2. Server validations and before-save automation.
3. Save path and after-save automation.
4. Workflow field-update re-entry (optional second trigger cycle).
5. Commit boundary.
6. Post-commit async effects.

Important platform behavior:

- Trigger order is not guaranteed when multiple triggers exist for same object/event.
- Async failures after commit are downstream failures, not sync transaction failures.

## Correlation Model

Primary keys:

- `run_id` (journey)
- `step_id` (UI action)
- `txn_id` (transaction)

Propagation and causality:

- `event_id`
- `parent_event_id`
- `origin_txn_id` (for async descendants)
- `execution_phase` (`sync_pre_commit`, `sync_reentry`, `post_commit_async`)
- `attempt_no`

## Determinism Rules

- Correlate by IDs first, timestamps second.
- Track observed trigger order without assuming semantic order.
- Encode workflow re-entry as explicit second attempt.
- Use payload fingerprint dedupe for repeated event emissions.

## Failure Classification Precedence

Use first decisive failure in this order:

1. UI
2. Validation
3. Flow
4. Apex
5. Integration
6. Async
7. Data
8. Unknown

Step-level rollup:

- if sync fails: async is `not_reached`
- if sync succeeds and async fails: outcome is `committed_with_async_failure`

## Recommended Telemetry Fields

- `run_id`, `step_id`, `txn_id`, `origin_txn_id`
- `event_id`, `parent_event_id`
- `execution_phase`, `attempt_no`
- `layer`, `event_name`, `event_status`
- `object_api`, `record_id`, `changed_fields`
- `flow_api_name`, `flow_interview_id`, `flow_element`
- `apex_class`, `apex_method`, `trigger_object`, `trigger_operation`
- `validation_rule`, `duplicate_rule`
- `async_type`, `async_job_id`
- `integration_endpoint`, `http_status`
- `error_code`, `error_message`, `is_retriable`
- `started_at`, `ended_at`, `duration_ms`
- `evidence_ref`
