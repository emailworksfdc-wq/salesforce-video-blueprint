# Unified Release Readiness Scorecard

Release Readiness Score (RRS):

`RRS = 0.30*Implementation + 0.35*Testing + 0.20*Governance + 0.15*MCPPackaging`

## Pillars

- Implementation (30)
  - scope completion
  - defect escape rate
  - performance/SLO conformance
  - runbook completeness
- Testing (35)
  - test pass with flake penalty
  - risk-based coverage
  - non-functional checks
  - rollback/restore evidence
- Governance (20)
  - required approvals
  - change artifacts
  - policy checks
  - exception quality
- MCP Packaging (15)
  - schema/package integrity
  - compatibility matrix
  - install/upgrade validation
  - observability/support metadata

## Hard-Stop Controls

Any one failure => Blocker:

- critical/high unresolved security issue
- rollback path not proven
- required compliance approval missing
- package install/upgrade fails on target environment
- Sev1/Sev2 open defect tied to release scope

## Gate Bands

- 0-59: Blocker (No-Go)
- 60-74: At Risk (No-Go unless approved exception)
- 75-89: Conditional Go
- 90-100: Go

Conditional Go requires:

- no hard-stop control failures
- every pillar >= 70

## Anti-Gaming Floor

`EffectiveGateScore = min(RRS, lowest_pillar + 20)`
