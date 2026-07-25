# Replay Hardening Standard (Salesforce UI)

This standard defines reliability and auditability rules for browser replay.
Use it for all process-to-agent conversion runs.

## 1) Locator Resilience

- Prefer selectors in this order:
  1. `data-testid` or `data-qa`
  2. Stable `aria-label` / role
  3. Visible text scoped to stable container
  4. Fallback CSS selectors
- Avoid brittle selectors:
  - Deep nth-child chains
  - Utility-class-only selectors
- Require per-step selector contract:
  - `primary_selector`
  - `fallback_selectors[]`
  - `validation_predicate`

## 2) Frame/Modal State Discipline

- Treat context as state machine:
  - `root -> iframe -> nested_iframe -> modal`
- Before every action:
  - assert frame identity
  - assert modal visibility when applicable
- After modal close:
  - assert teardown complete before proceeding

## 3) Deterministic Wait Gates

- No arbitrary sleep-based synchronization.
- Require all applicable gates before action:
  - DOM gate: attached + visible + enabled + stable
  - App gate: no loading spinner/skeleton overlay
  - Data gate: required request/response complete
- Re-check actionability immediately before click/type.

## 4) Retry Rules

- Retry only idempotent operations by default.
- Capture artifacts on first failure before retry.
- Use bounded retries with jittered backoff.
- Emit soft-flaky signal when retry succeeds after a failure.

## 5) Dynamic Rendering Safeguards

- Re-resolve locators after:
  - navigation
  - tab switch
  - modal transitions
  - tracked state mutation
- For virtualized tables/lists:
  - scroll row into view
  - assert row identity key before interaction

## 6) Network and Screenshot Evidence

- Capture network metadata per step:
  - URL, method, status, resource_type, timestamp
  - correlation identifiers when available
- Screenshot checkpoints:
  - `before_action`
  - `after_action`
  - `on_retry`
  - `on_failure`
  - `final_state`

## 7) Reproducibility Bundle

Each run must emit:

- `replay_manifest.json`
- `step_ledger.json`
- step screenshots
- step network traces
- failure summary (if any)

Schema contract:
- `replay_manifest.json` MUST validate against `docs/schemas/replay_manifest.schema.json`
- `step_ledger.json` MUST validate against `docs/schemas/step_ledger.schema.json`
- `failure_summary.json` MUST validate against `docs/schemas/failure_summary.schema.json` when any step fails

## 8) Flake Governance

- Track:
  - retry-pass rate
  - top failing selectors
  - frame/modal mismatch frequency
  - timeout debt
- Quarantine flaky scenarios and require root-cause tag to re-enable.

## 9) CI Gates

- Fail if:
  - selector contract missing for any step
  - artifact bundle missing required files
  - retry budget exceeded
  - critical step lacks network trace or screenshot

## 10) Canonical Field Mapping

To prevent drift across docs and artifacts, use this mapping:

- `status` (artifact field) -> `replay_status` (blueprint presentation field)
- `ended_at` (artifact field) -> timeline end timestamp
- `evidence_refs[]` is mandatory for every step, including successful steps

