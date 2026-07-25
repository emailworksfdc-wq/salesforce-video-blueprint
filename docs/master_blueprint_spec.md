# Master HTML Blueprint Specification

This document defines the output contract for the single-file HTML blueprint.

## Required sections

1. Executive Process Summary
2. Step-by-Step Action Trace
3. UI-to-Backend Mapping
4. Data Impact Ledger
5. Failure and Risk Analysis
6. Conversational AI Agent Blueprint
7. Evidence Appendix

## Readability and Auditability Gates

- Every section begins with a concise executive takeaway.
- Every required section is present and non-empty; if not applicable, include `N/A` with one-sentence justification.
- Every assertion is marked as `Observed`, `Inferred`, or `Assumed`.
- Every `Assumed` assertion includes `assumption_owner`, `validation_plan`, and `due_by`.
- Every `step_id` row must populate all required per-step fields; nullable fields must be explicit `null` with reason.
- `step_id` values are unique per `run_id`, and `sequence` values are strictly increasing with no duplicates.
- Every step assertion links to at least one evidence artifact.
- Every data mutation links back to a source `step_id`.
- Every failure includes impacted layer, root-cause hypothesis, and remediation owner.
- External-agent section includes guardrails and fallback/handoff rules per intent.
- Use controlled labels consistently across sections (`action_type`, `backend_layer`, `result`).
- Every `evidence_ref` resolves to an Evidence Appendix record with `evidence_id`, `artifact_type`, `artifact_path_or_url`, `captured_at`, and `sha256`.

## Per-step minimum fields

Each rendered step row must include these normalized fields (canonical names shown first):

- `step_id`
- `sequence`
- `attempt_no`
- `action_type`
- `target`
- `ui_context`
- `replay_status` (`success` | `failed` | `retried` | `skipped`)
- `triggered_layers`
- `failure_layer` and `failure_reason` when present
- `data_changes` including object, record id, changed fields
- `evidence_refs` (one or more evidence artifact identifiers)

Compatibility note:
- Source replay artifacts may use `status`; blueprint renderers MUST map `status -> replay_status`.
- Source replay artifacts may use `finished_at`; blueprint renderers MUST treat this as equivalent to `ended_at` in timeline views.

## Traceability Matrix Contract

Blueprints must include a matrix with:

- `step_id`
- `business_intent`
- `ui_action`
- `backend_layer`
- `object_api`
- `record_id`
- `evidence_ids`
- `control_ids`
- `result`

## Correlation keys

Each rendered event should include or derive from:

- `run_id`
- `step_id`
- `event_time`

This key enables exact traceability from UI action to backend events and data changes.
- `event_time` and evidence timestamps must be UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SS.sssZ`).

## Failure precedence and rollup

Failure classification MUST follow this precedence when multiple failures are observed in one step:

1. UI
2. Validation
3. Flow
4. Apex
5. Integration
6. Async
7. Data
8. Unknown

Rollup rules:
- If sync fails, async outcome is `not_reached`.
- If sync succeeds and async fails, step outcome is `committed_with_async_failure`.

## AI agent blueprint details

For each business intent:

- Required entities and constraints
- Deterministic orchestration sequence
- Permission and data-visibility guardrails
- Error pathways and recovery strategy
- Observability hooks (run_id and step_id propagation)

