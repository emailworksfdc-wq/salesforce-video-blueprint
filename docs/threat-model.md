# Threat Model: Process-to-Agent Conversion Pipeline

> **STATUS: ASPIRATIONAL — NOT IMPLEMENTED.**
> This document describes intended design, not current behaviour. As of this
> commit there is no code in `src/` that implements or enforces anything below.
> Do not cite it as evidence of a control, capability, or release gate.
> See the README status table for what actually works.

## Scope

Assets:
- enterprise data and PII
- action permissions and credentials
- workflow integrity
- audit artifacts

Trust boundaries:
- untrusted ingestion content
- transformation/generation services
- runtime execution environment
- deployment/configuration and observability surfaces

## Primary Threats and Controls

| Threat | Mitigation | Detection |
| --- | --- | --- |
| Prompt injection in source artifacts | Treat retrieved content as untrusted; isolate policies; allowlist tools/intents | Injection-pattern alerts and blocked-tool metrics |
| Data exfiltration via actions | Least privilege and per-action authz; egress allowlists | Egress anomaly and DLP alerts |
| Cross-tenant/context leakage | Tenant segmentation and retrieval filters | Cross-tenant query mismatch alerts |
| Unsafe generated workflows | Schema and policy-as-code validation; human approval for privileged paths | Validation and approval-bypass alerts |
| Secret leakage in artifacts/logs | Secret scanning and redaction pipeline | Secret-scan CI failures and redaction-failure alerts |
| Connector abuse/SSRF | URL/domain allowlists and private-range blocking | Denied destination telemetry |
| Runaway cost/loops | Token/time/rate budgets and depth caps | Budget breach and retry-storm alerts |
| Config/release tampering | Signed configs/artifacts and RBAC approvals | Signature and drift-failure alerts |

## Minimum Detection KPIs

- blocked high-risk tool calls per 1k runs
- cross-tenant retrieval violation count
- secret leakage findings
- approval-gate bypass attempts
- denied egress destination count
- token/time budget breaches
