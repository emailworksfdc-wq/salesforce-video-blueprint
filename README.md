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

## Status: v0.1.1 — working pipeline, output now compiles in one measured case

Read this before trusting anything this produces.

**The emitted `.agent` bundle now compiles against a real Salesforce org — and
it did not the first time.** On 2026-07-26 the bundle derived from
`examples/case_triage.dom_capture.jsonl` was submitted to
`sf agent validate authoring-bundle` against a Developer Edition org. Salesforce
rejected it with **24 `CompilationError`s**, beginning:

```
CompilationError: Syntax error: unexpected `->` [Ln 108, Col 8]
CompilationError: Syntax error: unexpected `| Follow these steps:` [Ln 109, Col 8]
```

Every derived subagent was malformed: in each one's `reasoning:` block the emitter
wrote a bare `->` opener where the grammar requires `instructions: ->`, with the
`|` lines under-indented. The three standard subagents compiled fine — they are
copy-pasted from the first-party template, so they were never testing this
project's grammar model at all. After fixing the emitter, the same bundle
compiles: exit 0, `{"success": true}`, and it then **deployed** to the org as
`AiAuthoringBundle` metadata and round-tripped byte-identically.

**What that does and does not license.** Validated: one bundle, one intent
(`Update Case (Status)`), one org, one CLI version (`@salesforce/cli 2.143.6`,
`@salesforce/agents 1.6.6`). The `.agent` **grammar** for a topic-router agent is
confirmed, as is the 80-character subagent-name limit (measured: 80 passes, 81
fails with `Too big: expected string to have <=80 characters`). Not validated:
any spec shape other than the single-topic router, any bundle carrying
`@apex.*`/`@flow.*` actions (the emitter never emits them), and anything about
whether the compiled agent *behaves* correctly — compilation is syntax, not
semantics. No agent has been published.

The critical lesson stands regardless: **`validate_locally()` reported zero
findings on the file Salesforce rejected.** Local validation is this repo's own
opinion, and it was measurably blind to the entire error class. Run the CLI.

Against the stated end goal — *record → derive → run the spec repeatedly →
deploy as an Agentforce agent* — the honest grade is roughly **58%**:

| Stage | Status | Reality |
| --- | --- | --- |
| 1 · Record | 🟡 Partial | Real DOM recorder (`capture/recorder.js`, ~604 lines). Never run against a live org. Invoked by hand, not as a pipeline stage. |
| 2 · Ingest | 🟡 Partial | Parses and validates capture traces. **Silently discards events in three known cases** — see [Known defects](#known-defects). |
| 3 · Derive | 🟢 Works | The strongest part. Correlates, coalesces, derives intent and entities from observed evidence. Refuses to guess; caps confidence at 0.70. |
| 4 · Score | 🟢 Works | Falsifiable 7-dimension gate. Bad specs measurably fail. Cannot be gamed by padding. |
| 5 · Iterate | 🔴 Absent | `sf agent test create/run/results` appear **nowhere** in this repo. The offline loop scores 79/79/79 and reports `converged=true` — a loop that cannot change its input is not a loop. |
| 6 · Deploy | 🟡 Partial | Every emitter is written and unit-tested, and reachable from the MCP server. One emitted bundle (`SFVB_TEST_Case_Triage`, 2026-07-26) now **compiles** (`sf agent validate authoring-bundle` → exit 0) and **deploys** as `AiAuthoringBundle` metadata to a DE org, round-tripping byte-identically. Still partial: one intent shape only, no agent has been published, and nothing checks the compiled agent's behaviour — compilation is syntax, not semantics. |

The two stages nearest to zero are the two the end goal names explicitly. This
table is the most important thing in this README.

The pipeline is reachable three ways — an [MCP server](#1--mcp-server--use-it-from-any-ai-tool)
for any AI harness, a [Python API](#2--python-library), and a [CLI](#3--command-line).
That is packaging, not progress against the table above: all three run the same
offline pipeline and none of them contacts an org.

The one code path that does is `scripts/agentforce_roundtrip.sh`, which drives the
whole chain — derive, score, emit the bundle into a throwaway SFDX project, then
`sf agent validate authoring-bundle`. It runs **offline by default**; the org step
is opt-in behind `--org <alias>`, and when you omit it the run says so rather than
implying a verdict it never got. That script is how the validation above was
performed and how you can reproduce it.

### How to validate an emitted bundle yourself

`sf agent validate authoring-bundle` does **not** require the bundle to be
deployed first, contrary to a natural reading of its docs. The command
(`plugin-agent` 1.40.5) is `requiresProject = true`: it locates the bundle in the
**local** SFDX project, reads the `.agent` file off disk, and POSTs its contents
to the compile endpoint, using the org connection for authentication only. So the
loop is cheap and mutates nothing:

```bash
# 1. Emit the bundle (see the Python API section) into a throwaway SFDX project:
#      <proj>/force-app/main/default/aiAuthoringBundles/<ApiName>/<ApiName>.agent
#                                                               /<ApiName>.bundle-meta.xml
#    <proj>/sfdx-project.json needs only: packageDirectories [{path: force-app, default: true}]
# 2. Compile it. No deploy required.
cd <proj> && sf agent validate authoring-bundle -o <your-org> -n <ApiName> --json
```

Exit 0 with `{"success": true}` means the Agent Script compiled. Exit 1 returns a
`data.errors[]` array with `errorType`, `description`, and line/column for each
error. Deploying (`sf project deploy start -d force-app`) is a separate,
optional step and is only needed to publish.

Or let the round-trip script do all of it, including laying out the throwaway
project:

```bash
bash scripts/agentforce_roundtrip.sh --org <your-org>   # derive → score → emit → validate
bash scripts/agentforce_roundtrip.sh                    # same, minus the org step
```

Every API name in the emitted artifacts is derived from `naming.py`, so the
bundle, the `.agent` config, and both test-spec dialects cannot drift apart. Drop
`--org` and the run reports the compile step as `SKIPPED` and ends with
`NOTHING WAS VALIDATED BY SALESFORCE` — it will not imply a verdict it did not
get. See [`docs/step6-agentforce-bridge.md`](docs/step6-agentforce-bridge.md) §6.

---

## Quick start

Requires **Python ≥ 3.11** (PEP 604 unions are evaluated at runtime; the macOS
system `python3` is 3.9 and will fail).

```bash
git clone https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git
cd salesforce-video-blueprint

python3 -m venv .venv
.venv/bin/pip install -e ".[dev,mcp]"
.venv/bin/python -m pytest -q          # 1054 passed, 2 skipped
```

Without the `mcp` extra you get `1016 passed, 3 skipped` — the MCP server tests
skip rather than fail when the optional dependency is absent.

Two skips are expected. One is an opt-in check that validates artifacts from a
real end-to-end run; set `SF_BLUEPRINT_E2E_DIR` to enable it. The other is a
score-gate assertion against a real recorded capture, which stays skipped until
that capture is committed *and* survives ingest — see `docs/DEFECT_LEDGER.md`.
Everything else is hermetic — no org, no network, no credentials.

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

## Use it in your project

Three ways, all installable from the repo. Install from `@main` rather than
`@v0.1.0` — that tag predates a dependency fix and does not install on Python
3.12+ ([known defect](#known-defects)). The fix is on `main` and is what v0.1.1
carries; no `v0.1.1` tag has been pushed and nothing has been published to PyPI,
so `@main` is the only install source.

### 1 · MCP server — use it from any AI tool

Exposes the pipeline to Claude Code, Claude Desktop, Cursor, Windsurf, Continue,
or any other MCP-capable harness. Every tool is offline and read-only: none
contacts a Salesforce org or launches a browser.

```bash
pipx install "sf-video-blueprint[mcp] @ git+https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git@main"
```

```json
{
  "mcpServers": {
    "sf-blueprint": {
      "command": "sf-blueprint-mcp"
    }
  }
}
```

Seven tools: `health`, `validate_capture`, `derive_spec`, `score_spec`,
`emit_agent_bundle`, `emit_test_spec`, `preview_api_names`. Then just ask:

> Derive an agent spec from `~/recordings/case_triage.dom_capture.jsonl` and tell
> me what is blocking it from passing the quality gate.

Full setup for each harness, the response envelope, and troubleshooting:
**[docs/mcp-install.md](docs/mcp-install.md)**.

### 2 · Python library

```bash
pip install "sf-video-blueprint @ git+https://github.com/emailworksfdc-wq/salesforce-video-blueprint.git@main"
```

```python
from sf_video_blueprint import run_pipeline

result = run_pipeline(
    "dom_capture.jsonl",
    org_url="https://your-sandbox.sandbox.my.salesforce.com",
)

print(result.spec.intent)        # Update Case (Status)
print(result.score.total)        # 79
print(result.score.passed)       # False — mock telemetry
print(result.score.blocking_issues)
```

Check `result.score.passed` before trusting anything downstream, and
`result.evidence_is_real` to know whether the run observed a real org at all. An
in-process run uses mock telemetry, so it will not pass — by design.

`run_pipeline` is offline and writes nothing to disk. It raises `CaptureRejected`
when a capture leaks a secret or loses ≥50% of its events, rather than deriving a
spec from a damaged recording.

### 3 · Command line

```bash
sf-blueprint --capture dom_capture.jsonl \
  --org-url "https://your-sandbox.sandbox.my.salesforce.com" \
  --output-path outputs/blueprint.html
```

Writes an HTML blueprint plus `<output>.agent-spec.json`. Works from any
directory once installed.

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
| `pipeline.py` | Shared in-process API (`run_pipeline`) the CLI, library, and MCP server all call |
| `mcp_server.py` | MCP server (`sf-blueprint-mcp`) — 7 offline, read-only tools over stdio |
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
| Redaction does not cover record IDs, names, or free-text field values | Secrets, emails, and credential URL parameters are stripped at three choke points. Record IDs are retained deliberately (audit trail); customer **names** and ordinary field text are **not** detected at all. `outputs/` is still org-confidential. |
| Correlation is temporal, not causal | The join proves telemetry was *fetched during* a step, not *caused by* it. |
| The `v0.1.0` tag does not install | Measured on Python 3.13.14: `playwright~=1.46.0` pins `greenlet==3.0.3`, which has no cp313 wheel and whose C++ source fails to build (`error: unknown type name '_PyCFrame'`) → `ERROR: Failed building wheel for greenlet`. Fixed on `main` (relaxed to `playwright>=1.55,<2`, resolving `greenlet 3.5.4` from a wheel) and carried by v0.1.1. The broken tag still exists, so install from `@main`. |
| Video extraction is a stub | `HeuristicVideoExtractor` never decodes video; any video yields one placeholder step. **Use `--capture`.** |

`pyproject.toml` declares `pyyaml` and `jsonschema` as dev extras. Until you
install them, the repo uses hand-rolled YAML emitters and a hand-rolled
JSON-Schema-subset validator. Both are tested; neither is a full implementation.

---

## Documentation

**Accurate** — describes code that exists:

- [`docs/mcp-install.md`](docs/mcp-install.md) — MCP server install and per-harness config
- [`docs/USER_JOURNEY_Story.html`](docs/USER_JOURNEY_Story.html) — animated consumer walkthrough
- [`docs/DEFECT_LEDGER.md`](docs/DEFECT_LEDGER.md) — every known defect, with file and line
- [`docs/INTERFACE_CONTRACT.md`](docs/INTERFACE_CONTRACT.md) — the recorder↔parser wire format
- [`docs/step5-dom-capture.md`](docs/step5-dom-capture.md), [`docs/step6-agentforce-bridge.md`](docs/step6-agentforce-bridge.md)
- `docs/master_blueprint_spec.md`, `docs/replay-hardening.md`, `docs/execution-tracing-model.md`
- `docs/topic-action-authoring-standard.md`, `docs/omnistudio-to-agentforce-playbook.md`

**Partly implemented** — read the status banner at the top of each:

- `docs/mcp-product-spec.md` — the original `workflow.*` design. The server that
  shipped exposes the pipeline's real capabilities instead; the envelope and error
  taxonomy were adopted. Deviations are recorded in both documents.
- `docs/mcp-release-checklist.md` — written for a public npm/PyPI release that has
  not happened.

**Aspirational** — design intent for capabilities that do **not** exist in code.
Do not read these as status:

- `docs/governance-compliance.md`, `docs/threat-model.md`
- `docs/agent-testing-framework.md`, `docs/release-readiness-scorecard.md`
- `docs/agentforce-guardrail-checklist.md`

---

## Roadmap

Ordered by what unblocks the most:

1. **Broaden org validation.** ~~Run `sf agent validate authoring-bundle` on an
   emitted bundle.~~ Done for one case, and it found a real emitter bug on the
   first attempt (see [Status](#status-v010--working-pipeline-output-now-compiles-in-one-measured-case)).
   The grammar for a single-topic router agent is confirmed and the subagent-name
   cap is measured at 80. What remains speculative: multi-topic specs, bundles
   with `@apex.*`/`@flow.*` actions, the name limit on the *metadata* path
   (spec YAML `topics[].name` and `expectedTopic`, which the compiler never sees),
   and whether a published agent behaves as the spec describes. Validation should
   also become a step this repo can run itself, rather than a manual CLI
   invocation alongside it.
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
