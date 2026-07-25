# MCP Product Specification

> **STATUS: ASPIRATIONAL — NOT IMPLEMENTED.**
> This document describes intended design, not current behaviour. As of this
> commit there is no code in `src/` that implements or enforces anything below.
> Do not cite it as evidence of a control, capability, or release gate.
> See the README status table for what actually works.

Package target: `@salesforce-video-blueprint/mcp-workflow`

## Minimum Tool Set

- `workflow.plan`
- `workflow.execute`
- `workflow.status`
- `workflow.result`
- `workflow.cancel`
- `workflow.health`

## Contract Principles

- JSON Schema 2020-12 for all tool input/output.
- Shared response envelope with:
  - `ok`
  - `requestId`
  - `serverVersion`
  - `timestamp`
  - `error` (when not ok)
- Semantic versioning with explicit deprecation windows.

## Idempotency

Required for mutating tools:

- `idempotencyKey` required for execute/cancel
- identical key + identical request fingerprint => replay original response
- identical key + different fingerprint => conflict error

## Error Taxonomy

Categories:

- VALIDATION
- AUTH
- PERMISSION
- NOT_FOUND
- CONFLICT
- RATE_LIMIT
- DEPENDENCY
- INTERNAL
- TIMEOUT
- UNAVAILABLE

## Observability Contract

Structured logs must include:

- `requestId`, `tool`, `runId`, `durationMs`, `outcome`, `error.code`

Metrics:

- request totals/latency by tool
- workflow run counts by state
- step duration
- idempotency replay count
- upstream dependency failures

Tracing:

- root span per tool call
- child spans per workflow step and dependency

## CI Quality Gates

- schema validation
- contract tests for success + each error class
- backward compatibility check
- idempotency replay and conflict tests
- security scan and secret scan
- observability emission checks
- load smoke for execute/status
