# Salesforce Video Blueprint

Pipeline scaffold that correlates a recorded Salesforce UI process with backend
telemetry and derives a conversational agent spec from what it observed.

## Status: skeleton, not a finished product

Read this section before trusting any output.

**What genuinely works today**

- Correlation of UI steps to telemetry layers and record deltas (`correlation.py`).
- Live Salesforce telemetry collection via valid REST/Tooling queries —
  `ApexLog`, `AsyncApexJob`, `FlowInterview`, `ValidationRule`
  (`salesforce_collectors.py`).
- Record field-diff tracking via `--track-record`.
- Agent spec derivation from observed data, with per-field evidence
  (`spec_builder.py`), emitted as JSON.
- HTML blueprint rendering, with explicit provenance labelling.
- Browser replay via Playwright (`replay_browser.py`) — implemented, but see
  limitations.

**What does NOT work yet — do not assume otherwise**

| Capability | Reality |
| --- | --- |
| Video → click extraction | **Not implemented.** `HeuristicVideoExtractor` never decodes the video. It checks that the file exists and returns one placeholder step (`button:Save`). Any video yields the same step. |
| MCP server | **Does not exist.** No tool registration, no stdio handler. `docs/mcp-product-spec.md` and `docs/mcp-release-checklist.md` describe an unbuilt package. |
| Governance / threat-model controls | **Not enforced anywhere in code.** No redaction, classification, retention, or consent gating. The docs are aspirational. |
| `replay_manifest.json`, `step_ledger.json`, `failure_summary.json` | **No emitter.** The schemas in `docs/schemas/` describe artifacts nothing writes, so `STRICT_ARTIFACTS=1` always fails. |
| PII redaction in output | **None.** The HTML embeds real record ids and field values verbatim. Treat every output file as sensitive. |
| Causal step↔telemetry correlation | Correlation joins on a locally generated `step_id` that the collector itself stamped. It proves telemetry was *fetched during* a step, not *caused by* it. |
| Modal scoping in replay | `_resolve_locator` passes `[role='dialog']` and `.slds-modal__container` to `frame_locator()`, which only accepts iframes. Those scopes silently never resolve. |

Because extraction is a stub, a default run produces a blueprint whose steps and
telemetry are placeholders. The report labels this with a red banner and the
quality gate refuses to pass it. That is deliberate — see "Honest scoring".

## Requirements

- **Python >= 3.11** (the code uses PEP 604 unions evaluated at runtime).
  The macOS system `python3` is 3.9 and will fail.
- Salesforce CLI (`sf`) for the validation loop.
- Playwright browsers for live replay: `playwright install chromium`.

## Quick start

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest            # 31 tests
```

Mock run (produces clearly-labelled simulated output):

```bash
.venv/bin/python -m sf_video_blueprint.cli ./inputs/sample_video.mp4 \
  --org-url "https://your-org.my.salesforce.com"
```

Note there is no `run` subcommand — the CLI is a single command, so the video
path is the first argument.

Outputs:

- `./outputs/master_blueprint.html` — human-readable report.
- `./outputs/master_blueprint.agent-spec.json` — machine-readable derived spec
  (this is the artifact to iterate on; override with `--spec-output`).

## Modes

- `mock` (default): local stubs for replay and telemetry. Output is **simulated**
  and is not audit evidence.
- `live`: browser replay plus Salesforce REST telemetry.

```bash
export SF_ACCESS_TOKEN="your_oauth_token"   # never pass tokens as CLI args
export SF_BLUEPRINT_PLAYWRIGHT=1
.venv/bin/python -m sf_video_blueprint.cli ./inputs/sample_video.mp4 \
  --org-url "https://your-org.my.salesforce.com" \
  --mode live \
  --track-record Case:500xx0000012345AAA
```

### Live-mode safety

- **Sandbox and scratch orgs only.** Replay re-executes recorded actions, so a
  recorded "Create Case" writes a new record on every run. There is no
  idempotency guard, no cleanup, and **no code-level check that the target is not
  production** — that is operator discipline.
- Output artifacts contain real org data and are gitignored. Keep them that way.

## Honest scoring

`scripts/score_run.py` is designed so that a run emitting placeholder content
**cannot pass**, regardless of liveness:

- Weighted gates, including `evidence_is_real_ok` (scans output for placeholder
  markers) and `spec_derived_ok` (validates the emitted spec).
- Exits non-zero on failure so `set -e` in the caller trips.

A default mock run currently scores 65/100, `pass: false` — correctly, because
its evidence is fabricated. Do not "fix" this by relaxing the gate; fix it by
implementing real extraction.

```bash
bash ./scripts/validate_dev_org.sh <org-alias> ./inputs/sample_video.mp4 [Object:RecordId]
```

Set `PY_BIN` to choose the interpreter if `./.venv` is absent.

## Documentation

**Implemented / accurate** — describes code that exists:

- `docs/master_blueprint_spec.md` — HTML contract, matches `html_report.py`.
- `docs/replay-hardening.md` — Salesforce replay patterns (retry/backoff are
  implemented; the selector contract and artifact bundle are not).
- `docs/execution-tracing-model.md` — Salesforce save-path domain reference.
- `docs/omnistudio-to-agentforce-playbook.md`, `docs/omnistudio-mapping-examples.md`
- `docs/topic-action-authoring-standard.md`

**Aspirational** — describes capabilities that do **not** exist in code. Useful
as design intent; must not be read as implemented status:

- `docs/mcp-product-spec.md`, `docs/mcp-release-checklist.md`
- `docs/governance-compliance.md`, `docs/threat-model.md`
- `docs/agent-testing-framework.md`, `docs/release-readiness-scorecard.md`
- `docs/agentforce-guardrail-checklist.md`

## Next steps

1. Replace video extraction with DOM-level capture (Playwright codegen, a click
   observer, or UI Automation Recorder). CV-on-video yields pixel coordinates;
   replay needs selectors, and nothing bridges the two.
2. Bridge the derived spec to `sf agent generate agent-spec` / Agent Script
   (`.agent`) and use `AiEvaluationDefinition` for the iteration loop.
3. Generate `docs/schemas/*.json` from the Pydantic models to stop the two
   sources of truth drifting, and add emitters for the orphaned schemas.
4. Add a redaction layer before anything real is recorded.
