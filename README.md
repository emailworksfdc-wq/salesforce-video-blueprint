# salesforce-video-blueprint

[![CI](https://github.com/emailworksfdc-wq/salesforce-video-blueprint/actions/workflows/ci.yml/badge.svg)](https://github.com/emailworksfdc-wq/salesforce-video-blueprint/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](pyproject.toml)

**Record a Salesforce process click-by-click, and get back a conversational agent
spec that only claims what the recording actually proved.**

You record yourself doing a business process in a Salesforce org. This pipeline
turns that recording into a structured Agentforce agent specification — intent,
entities, orchestration steps, guardrails — plus a score that tells you, in
numbers, whether the specification is trustworthy enough to build from.

The design principle is **refusal over invention**. When the recording does not
show something, the spec says so instead of guessing. A run built from
fabricated evidence is *designed to fail* its own quality gate.

---

## Status: v0.1.0 — working pipeline, unvalidated output

Read this before trusting anything this produces.

**Nothing in this project has ever been validated against a real Salesforce
org.** The emitted `.agent` bundle has never been fed to
`sf agent validate authoring-bundle` — the only authority on whether the output
is syntactically valid Agent Script. Local validation passes, but local
validation is this repo's own opinion, not Salesforce's.

Against the stated end goal — *record → derive → run the spec repeatedly →
deploy as an Agentforce agent* — the honest grade is roughly **55%**:

| Stage | Status | Reality |
| --- | --- | --- |
| 1 · Record | 🟡 Partial | Real DOM recorder (`capture/recorder.js`, ~604 lines). Never run against a live org. Invoked by hand, not as a pipeline stage. |
| 2 · Ingest | 🟡 Partial | Parses and validates capture traces. **Silently discards events in three known cases** — see [Known defects](#known-defects). |
| 3 · Derive | 🟢 Works | The strongest part. Correlates, coalesces, derives intent and entities from observed evidence. Refuses to guess; caps confidence at 0.70. |
| 4 · Score | 🟢 Works | Falsifiable 7-dimension gate. Bad specs measurably fail. Cannot be gamed by padding. |
| 5 · Iterate | 🔴 Absent | `sf agent test create/run/results` appear **nowhere** in this repo. The offline loop scores 79/79/79 and reports `converged=true` — a loop that cannot change its input is not a loop. |
| 6 · Deploy | 🟡 Partial | Every emitter is written and unit-tested. **None has a production call site**, and no org has ever validated the output. |

The two stages nearest to zero are the two the end goal names explicitly. This
table is the most important thing in this README.

---

## Quick start

Requires **Python ≥ 3.11** (PEP 604 unions are evaluated at runtime; the macOS
system `python3` is 3.9 and will fail).

```bash
git clone https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git
cd salesforce-video-blueprint

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q          # 763 passed, 1 skipped
```

The one skip is an opt-in check that validates artifacts from a real end-to-end
run; set `SF_BLUEPRINT_E2E_DIR` to enable it. Everything else is hermetic — no
org, no network, no credentials.

Then run the pipeline on the bundled example capture — **no Salesforce org, no
network, no credentials required**:

```bash
.venv/bin/python -m sf_video_blueprint.cli \
  --capture examples/case_triage.dom_capture.jsonl \
  --org-url "https://example-dev.develop.my.salesforce.com" \
  --output-path outputs/case_triage.html
```

Observed output:

```
EXTRACTION: noise reduction: 8 raw events -> 10 actions (coalesced 0 input, ...)
Master blueprint generated: outputs/case_triage.html
Agent spec (machine-readable) generated: outputs/case_triage.agent-spec.json
Derived intent: Update Case (Status) (confidence 0.70)
WARNING: this run contains SIMULATED data and is not audit evidence.
```

That last line is the point. The run used a **real** capture but **mock**
telemetry, so it labels itself as non-evidence. Score it:

```bash
.venv/bin/python -c "
from sf_video_blueprint.spec_score import score_spec_file
r = score_spec_file('outputs/case_triage.agent-spec.json')
print(f'{r.total}/100  band={r.band}  passed={r.passed}')
for k, v in r.dimensions.items(): print(f'  {k:22} {v.score:>3}/{v.max_score}')
for b in r.blocking_issues: print('BLOCKED:', b)
"
```

```
79/100  band=low  passed=False
  evidence_grounding      25/30
  completeness            15/15
  honesty                 20/20
  specificity              9/10
  testability              0/10
  placeholder_freedom     10/10
  provenance_integrity     0/5
BLOCKED: Spec was built from mock/unknown telemetry, not a live org.
          Cannot reach the top band without observed server-side behaviour.
```

**79 points, and it still refuses to pass.** Above the 75-point threshold, but
blocked because the telemetry was mock. `testability` is 0 because the recording
contained no failure path. That is the gate working exactly as intended: the
only way to raise this score is to capture better evidence, never to soften the
gate.

---

## How it works

```
  ┌─────────────┐   dom_capture.jsonl   ┌──────────────┐
  │  Recorder   │ ────────────────────> │   Ingest     │  validate, order,
  │ (browser)   │                       │ dom_capture  │  redaction check
  └─────────────┘                       └──────┬───────┘
                                               │ CaptureTrace
                                        ┌──────▼───────┐
                                        │  Extract     │  coalesce noise,
                                        │ dom_extractor│  rank selectors
                                        └──────┬───────┘
                                               │ ActionExtractionBundle
                   ┌───────────────────────────┼───────────────────────┐
            ┌──────▼──────┐            ┌───────▼──────┐        ┌───────▼──────┐
            │   Replay    │            │  Telemetry   │        │   Record     │
            │ (Playwright │            │  (REST /     │        │   deltas     │
            │  or no-op)  │            │  Tooling API)│        │              │
            └──────┬──────┘            └───────┬──────┘        └───────┬──────┘
                   └───────────────────────────┼───────────────────────┘
                                        ┌──────▼───────┐
                                        │ Correlate    │  join UI step ->
                                        │ correlation  │  backend evidence
                                        └──────┬───────┘
                                        ┌──────▼───────┐
                                        │   Derive     │  intent, entities,
                                        │ spec_builder │  guardrails
                                        └──────┬───────┘
                        ┌──────────────────────┼──────────────────────┐
                 ┌──────▼──────┐        ┌──────▼──────┐       ┌───────▼──────┐
                 │  Score      │        │  Emitters   │       │  HTML        │
                 │ spec_score  │        │ .agent/YAML │       │  blueprint   │
                 │  (gate)     │        │ /eval specs │       │              │
                 └─────────────┘        └─────────────┘       └──────────────┘
```

**Visual walkthrough:** open [`docs/USER_JOURNEY_Story.html`](docs/USER_JOURNEY_Story.html)
in a browser — an animated, six-act story of how an operator actually uses this,
including where the gaps are. It is self-contained; no server needed.

### Module map

| Module | Responsibility |
| --- | --- |
| `capture/recorder.js`, `capture/inject.py` | Browser-side DOM click recorder and its injector |
| `dom_capture.py` | Untrusted-input boundary: parse, validate, order, redaction-leak check |
| `dom_extractor.py` | Raw events → replayable actions; noise coalescing, selector ranking |
| `selectors.py` | Selector strategy tiering (`test_id` > `aria` > `role_name` > … > `xpath`) |
| `replay_browser.py` | Playwright replay with production-org and blocked-alias guards |
| `salesforce_collectors.py`, `telemetry.py` | REST/Tooling telemetry: `ApexLog`, `FlowInterview`, `ValidationRule`, `AsyncApexJob` |
| `correlation.py` | Joins UI steps to backend evidence in a 5-second forward window |
| `spec_builder.py` | Derives the agent spec from correlated evidence. Never invents. |
| `spec_score.py` | The falsifiable quality gate (7 weighted dimensions, threshold 75) |
| `naming.py` | Single source of truth for API names across every artifact |
| `agent_script.py` | `.agent` (Agent Script) + `.bundle-meta.xml` emitters |
| `agentforce_spec.py` | Agentforce agent-spec YAML emitter |
| `eval_spec.py` | `AiEvaluationDefinition` / `AiTestingDefinition` test-spec emitters |
| `iterate.py` | Versioned offline refinement loop |
| `redaction.py` | Secret/PII redaction primitives (**no production callers yet**) |
| `markers.py` | Provenance vocabulary — which sources count as real evidence |

### Why the score gate exists

A pipeline that emits confident-looking specs from thin evidence is worse than
one that emits nothing, because a human will act on it. So provenance is
enforced structurally:

```python
REAL_EXTRACTION_SOURCES = frozenset({"dom-capture", "cv"})
REAL_TELEMETRY_SOURCES  = frozenset({"live-org"})
```

A spec built from stub extraction or mock telemetry is capped and blocked no
matter how complete it looks. The gate also detects **threshold surfing** — a
spec that scrapes past the total while leaving several dimensions near zero is
flagged rather than passed.

> **Contract:** never raise a score by weakening the gate. Making the gate
> weaker is a defect, not a fix. Raise it by capturing better evidence.

---

## Working against a real org

All of Acts 2–5 (ingest → derive → score → iterate) run **fully offline**: no
token, no network, no deploy. Only recording and deployment need an org.

```bash
export SF_ACCESS_TOKEN="..."        # never pass tokens as CLI arguments
export SF_BLUEPRINT_PLAYWRIGHT=1

.venv/bin/python -m sf_video_blueprint.cli \
  --capture ./inputs/dom_capture.jsonl \
  --org-url "https://your-sandbox.sandbox.my.salesforce.com" \
  --mode live \
  --track-record Case:500xx0000012345AAA
```

### Safety rules — enforced in code, not convention

- **Sandbox and scratch orgs only.** Replay *re-executes* recorded actions, so a
  recorded "Create Case" writes a new record on every run. There is no
  idempotency guard and no cleanup.
- **Production guard fails closed.** `replay_browser._is_production_org()` reads
  `sf org display --json` for `isSandbox` / `isScratch` / instance-URL markers.
  If org type cannot be determined, it **refuses to proceed** rather than
  assuming safety. Override with `SF_ALLOW_PRODUCTION_ORG=1` (logged loudly).
- **Two org aliases are hard-blocked with no override.** `BLOCKED_ORG_ALIASES` /
  `_FORBIDDEN_ORG_ALIASES` raise `BlockedOrgError` / `ORG_FORBIDDEN` even when
  `SF_ALLOW_PRODUCTION_ORG=1` is set.
- **Never automate the login form.** Authenticate via signed frontdoor only:
  `sf org open --url-only -o <alias>`, then navigate to that URL. MFA and SSO
  are bypassed by the signed frontdoor.
- **Never pass a token as a command-line argument** — argv is world-readable via
  `ps`. Use `SF_ACCESS_TOKEN`.
- **Every output artifact is sensitive.** `outputs/` and `inputs/` are
  gitignored because HTML blueprints embed real record IDs and field values
  verbatim. Keep them that way.

---

## Known defects

This project keeps an honest ledger rather than a feature list. Full detail in
[`docs/DEFECT_LEDGER.md`](docs/DEFECT_LEDGER.md). The ones that would bite you first:

| Defect | Impact |
| --- | --- |
| `RawRoleName` requires both `role` and `name` (`dom_capture.py:51-55`) | The recorder legitimately emits `role: null` for plain `div`/`span` clicks and `name: null` for bare inputs. Those events are **dropped**. A realistic 10-action session can lose 4 — and the surviving spec is still stamped as real `dom-capture` evidence. |
| Event loss is only reported at **≥ 50%** | At 40% loss: zero warnings, exit 0. A partial recording looks like a clean one. |
| `parse_capture_file` hardcodes `manifest=None` (`:276`) | The manifest `event_count` cross-check — the one test that would catch loss at *any* ratio — never runs. |
| UTF-8 BOM not stripped (`:217`) | A BOM-prefixed capture silently loses its first event. |
| Leak detector inspects only `element.name` (`:414`) | The recorder derives field identity from eight signals. A secret identified via `type=password` or an SF field API name is not caught. |
| `redaction.py` has zero production callers | The redaction primitives exist and are tested, but nothing in the pipeline calls them. |
| `scripts/agentforce_roundtrip.sh` uses three different agent names in one run | A test run would target an agent that does not exist. Fix before spending an org run. |
| Correlation is temporal, not causal | The join proves telemetry was *fetched during* a step, not *caused by* it. |
| No MCP server | `docs/mcp-product-spec.md` describes an unbuilt package. Aspirational. |
| Video extraction is a stub | `HeuristicVideoExtractor` never decodes video; any video yields one placeholder step. **Use `--capture`.** |

`pyproject.toml` declares `pyyaml` and `jsonschema` as dev extras. Until you
install them, the repo uses hand-rolled YAML emitters and a hand-rolled
JSON-Schema-subset validator. Both are tested; neither is a full implementation.

---

## Documentation

**Accurate** — describes code that exists:

- [`docs/USER_JOURNEY_Story.html`](docs/USER_JOURNEY_Story.html) — animated consumer walkthrough
- [`docs/DEFECT_LEDGER.md`](docs/DEFECT_LEDGER.md) — every known defect, with file and line
- [`docs/INTERFACE_CONTRACT.md`](docs/INTERFACE_CONTRACT.md) — the recorder↔parser wire format
- [`docs/step5-dom-capture.md`](docs/step5-dom-capture.md), [`docs/step6-agentforce-bridge.md`](docs/step6-agentforce-bridge.md)
- `docs/master_blueprint_spec.md`, `docs/replay-hardening.md`, `docs/execution-tracing-model.md`
- `docs/topic-action-authoring-standard.md`, `docs/omnistudio-to-agentforce-playbook.md`

**Aspirational** — design intent for capabilities that do **not** exist in code.
Do not read these as status:

- `docs/mcp-product-spec.md`, `docs/mcp-release-checklist.md`
- `docs/governance-compliance.md`, `docs/threat-model.md`
- `docs/agent-testing-framework.md`, `docs/release-readiness-scorecard.md`
- `docs/agentforce-guardrail-checklist.md`

---

## Roadmap

Ordered by what unblocks the most:

1. **Validate against a real dev org.** Run `sf agent validate authoring-bundle`
   on an emitted bundle. This is the only way to learn whether the `.agent`
   grammar is right, whether the API-name cap is really 80 characters, and
   whether the bundle deploys. Everything else is speculation until this runs.
2. **Fix the ingest losses** (the first four rows above) so a capture cannot be
   silently truncated while still being stamped as real evidence.
3. **Close stage 5.** Wire `sf agent test create/run/results` so a spec can
   actually be run, scored against org behaviour, and improved.
4. **Give stage 6 a call site.** The emitters work; nothing calls them.
5. **Call `redaction.py` from the pipeline** before any real recording is made.
6. **Make correlation causal** by threading request/transaction IDs from the UI
   through to backend logs.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short: Python ≥ 3.11, `pytest` must
stay green, and — the rule that matters most here — **never make a gate weaker
to make a number look better.**

## License

[Apache-2.0](LICENSE). Copyright 2026 the salesforce-video-blueprint authors.

This is an independent project. It is not affiliated with, endorsed by, or
supported by Salesforce, Inc. "Salesforce", "Agentforce", and related marks
belong to their respective owners.
