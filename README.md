# Salesforce Video Blueprint

Implementation scaffold for the "Salesforce Video-to-HTML Blueprint Plan".

## What this repository contains

- A canonical action and evidence schema for extracted UI steps.
- A deterministic replay engine contract with resilient retries.
- A full telemetry contract that binds backend signals to each UI step.
- Correlation and failure classification logic.
- A single master HTML blueprint generator.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
sf-blueprint run ./inputs/sample_video.mp4 --org-url "https://your-org.my.salesforce.com"
```

The command writes a blueprint HTML file in `./outputs`.

## Modes

- `mock` (default): uses deterministic local stubs for replay and telemetry.
- `live`: enables browser replay adapter and Salesforce REST telemetry.

Live mode example:

```bash
export SF_ACCESS_TOKEN="your_oauth_token"
export SF_BLUEPRINT_PLAYWRIGHT=1
sf-blueprint run ./inputs/sample_video.mp4 \
  --org-url "https://your-org.my.salesforce.com" \
  --mode live \
  --track-record Case:500xx0000012345AAA
```

Optional for credential-based login form flows:

```bash
export SF_USERNAME="user@example.com"
export SF_PASSWORD="password_or_token"
```

Playwright browser install (first run):

```bash
playwright install chromium
```

Replay artifacts:

- Per-step screenshots: `./outputs/replay_artifacts/<step_id>.png`
- Per-step network traces: `./outputs/replay_artifacts/<step_id>.network.json`
- Override artifact location with `SF_BLUEPRINT_ARTIFACTS_DIR`

Hardening and schema standards:

- Replay hardening checklist: `docs/replay-hardening.md`
- Replay manifest schema: `docs/schemas/replay_manifest.schema.json`
- Step ledger schema: `docs/schemas/step_ledger.schema.json`
- Evidence metadata schema: `docs/schemas/evidence_metadata.schema.json`
- Traceability row schema: `docs/schemas/traceability_matrix_row.schema.json`
- Testing rubric example: `docs/schemas/testing_rubric.example.json`
- Governance control catalog template: `docs/schemas/governance_control_catalog.template.json`

Additional standards:

- OmniStudio migration playbook: `docs/omnistudio-to-agentforce-playbook.md`
- OmniStudio worked examples: `docs/omnistudio-mapping-examples.md`
- Governance and compliance: `docs/governance-compliance.md`
- Agent testing framework: `docs/agent-testing-framework.md`
- Agentforce guardrail checklist: `docs/agentforce-guardrail-checklist.md`
- Salesforce execution tracing model: `docs/execution-tracing-model.md`
- Threat model: `docs/threat-model.md`
- MCP product specification: `docs/mcp-product-spec.md`
- Topic/action authoring standard: `docs/topic-action-authoring-standard.md`
- MCP release checklist: `docs/mcp-release-checklist.md`
- Release readiness scorecard: `docs/release-readiness-scorecard.md`
- Executive summary template: `docs/executive-summary-template.md`

Developer org validation loop:

```bash
bash ./scripts/validate_dev_org.sh <org-alias> ./inputs/sample_video.mp4 [ObjectApiName:RecordId]
```
