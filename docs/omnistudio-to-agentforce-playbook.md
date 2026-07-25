# OmniStudio to Agentforce Migration Playbook

## Translation Model

Treat every legacy OmniStudio process as a conversation capability:

- Intent: user job-to-be-done
- Entities: required/optional structured slots
- Actions: deterministic read/write/orchestration operations
- Guardrails: policy and safety boundaries
- Conversation policy: clarifications, confirmations, fallback, handoff

## Component Mapping

| OmniStudio Asset | Current Role | Agentforce Equivalent | Notes |
| --- | --- | --- | --- |
| FlexCard | Context and quick actions | Intent launcher and response card | Keep as UI shell early in migration |
| OmniScript | Guided step flow | Multi-turn dialog state machine | Convert steps to slot states and transitions |
| DataRaptor | Data access/transform/load | Typed actions and schema adapters | Enforce contract tests per action |
| Integration Procedure | Backend orchestration | Action workflows/toolchain | Preserve sequence and error policies |
| Apex | Complex business logic | Trusted domain actions | Wrap with guard and audit contracts |
| Decision Matrix | Eligibility/routing rules | Policy/guardrail layer | Make routing explicit and testable |

## Migration Phases

### 1) Shadow Mode

- Agent predicts intents/entities/actions while legacy flow remains source of truth.
- Compare outputs and policy decisions.

Exit criteria:
- high intent-match on top journeys
- no critical compliance divergence

### 2) Hybrid Mode

- Agent handles conversation and simple actions.
- Complex orchestration remains in legacy backend.

Exit criteria:
- stable completion rate
- acceptable fallback volume
- production SLOs met

### 3) Full Replacement

- Agent owns routing, slot filling, policy checks, and orchestration.
- Legacy UX retired, selected backend services reused.

Exit criteria:
- KPI parity or improvement
- compliance sign-off
- validated rollback path

## Mapping Worksheet Schema

Use one row per migration unit:

- `journey_id`
- `business_outcome`
- `legacy_asset_type`
- `legacy_asset_name`
- `legacy_step_or_node`
- `agent_intent`
- `entities_required`
- `entities_optional`
- `action_name`
- `action_type`
- `action_contract_ref`
- `policy_guardrails`
- `fallback_strategy`
- `human_handoff_condition`
- `observability_events`
- `risk_rating`
- `migration_phase`
- `readiness_status`

## Readiness Gate

A journey is migration-ready only when:

- functional parity is proven on critical paths
- action idempotency is demonstrated for writes
- guardrails are complete (authz, PII, refusal, escalation)
- observability is end-to-end with trace IDs
- fallback and rollback procedures are tested

Idempotency implementation note:
- Journey-level write idempotency for migrated actions should align with MCP mutating-tool idempotency semantics (`idempotencyKey` replay/conflict behavior) where MCP orchestration is used.
