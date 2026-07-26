# Interface Contract — Step 5 (DOM capture) + Step 6 (Agentforce bridge)

**This file is the single source of truth for parallel work.** Every agent working
on Step 5 or Step 6 writes ONLY the files it owns and consumes ONLY the
interfaces below. If you need a change to a shared interface, you do not make it
— you report it, and the orchestrator changes it once.

Non-negotiable rules:

1. **Never edit a file you do not own.** Collisions silently destroy other
   agents' work.
2. **Never relax `scripts/score_run.py` thresholds** to make a run pass. The gate
   exists to catch fake evidence. Making the gate weaker is a defect, not a fix.
3. **No invention.** If a value was not observed, emit an explicit unknown. A
   confident wrong spec is worse than an honest incomplete one.
4. **Every new module ships with tests in its own test file.** No test file
   sharing (that is a merge collision).
5. **Python >= 3.11.** Use `./.venv/bin/python`. The system `python3` is 3.9.
6. **Never target production.** Dev/scratch orgs only. `PPCDM` and
   `PPCaccenture` are permanently out of scope.

---

## 1. The pipeline, end to end

```
                    ┌──────────── STEP 5 ────────────┐
recording (browser) │                                 │
   │                │  capture/*.js  (in-page probe)  │
   ├─ user clicks ──┼─> dom_capture.jsonl (raw trace) │
   │                │            │                    │
   │                │            v                    │
   │                │  dom_extractor.py  ─────────────┼─> ActionExtractionBundle
   └────────────────┘  (normalise + selector rank)    │   (existing contract,
                       └─────────────────────────────┘    models.py, UNCHANGED)
                                     │
                    ┌────────────────┼──── EXISTING (do not rewrite) ─────────┐
                    │  replay.py -> replay_browser.py -> telemetry ->         │
                    │  correlation.py -> spec_builder.py (DerivedAgentSpec)   │
                    └────────────────┬───────────────────────────────────────┘
                                     │
                    ┌──────────── STEP 6 ────────────┐
                    │  agentforce_spec.py            │  -> specs/<name>.yaml
                    │  agent_script.py               │  -> *.agent
                    │  eval_spec.py                  │  -> testSpec.yaml
                    │  iterate.py (refinement loop)  │  -> scored, versioned specs
                    └────────────────────────────────┘
```

**The seam is `ActionExtractionBundle`** (`src/sf_video_blueprint/models.py`).
Step 5 produces one. Step 6 consumes `DerivedAgentSpec` from `spec_builder.py`.
Neither side may change `models.py` or `spec_builder.py` without orchestrator
approval, because both are load-bearing for the other half of the fleet.

---

## 2. STEP 5 CONTRACT — DOM capture

### 2.1 Raw capture format: `dom_capture.jsonl`

One JSON object per line, append-only, written by the in-page recorder. This is
the wire format between the browser and Python. **Frozen — do not extend without
orchestrator approval.**

```jsonc
{
  "v": 1,                          // schema version, integer
  "seq": 12,                       // monotonic, 1-based
  "t": 1737830000123,              // epoch ms, Date.now() at capture
  "type": "click",                 // click|input|change|submit|navigate|keydown|scroll
  "url": "https://x.my.salesforce.com/lightning/r/Case/500.../view",
  "frame_path": [],                // ordered iframe chain, outermost first; [] = top document
  "selectors": {                   // ALL that could be computed; nulls allowed
    "test_id": "[data-testid='save']",
    "aria": "button[aria-label='Save']",
    "role_name": {"role": "button", "name": "Save"},
    "label_for": null,             // for inputs resolved via <label for>
    "sf_field": "Status",          // Salesforce field API name if derivable
    "css_path": "div.slds-form > button.slds-button_brand",
    "text": "Save",
    "xpath": "/html/body/div[1]/..."
  },
  "element": {
    "tag": "button",
    "type": null,                  // input[type] when present
    "name": null,
    "id": null,
    "classes": ["slds-button", "slds-button_brand"],
    "aria_label": "Save",
    "text": "Save",
    "is_in_modal": false,
    "modal_label": null,
    "shadow_depth": 2              // 0 = light DOM
  },
  "value": null,                   // post-change value for input/change/select; REDACTED if sensitive
  "value_redacted": false,         // true when `value` was scrubbed
  "sf": {                          // Salesforce-specific context, best effort
    "object": "Case",
    "record_id": "500xx0000012345AAA",
    "page_type": "record_home",    // record_home|list|app_page|modal|setup|unknown
    "app": "Service Console"
  }
}
```

Constraints:

- **Never write a raw secret.** Any field matching password / token / ssn / card
  patterns, or any `input[type=password]`, MUST emit `value: null` and
  `value_redacted: true`. This is a hard requirement, not best-effort.
- `frame_path` entries are selector strings resolving each iframe from its
  parent document, outermost first.
- Shadow DOM: `css_path` uses ` >>> ` between shadow boundaries.

### 2.2 Selector ranking (canonical order)

Highest to lowest priority. `dom_extractor.py` emits this order into
`ExtractedAction.ui_context.selector_hint` (best) with the rest as fallbacks:

1. `data-testid` / `data-qa` (stable contract)
2. `role` + accessible name (`get_by_role`)
3. `aria-label` exact
4. `<label for>` association (form fields)
5. Salesforce field API name → `[data-field-api-name='X']` / `lightning-input[data-name]`
6. Visible text scoped to a stable container
7. CSS path (last resort, brittle — must be marked low confidence)
8. XPath (diagnostic only, never primary)

Confidence mapping for `ExtractedAction.confidence`: tier 1–2 → 0.95, tier 3–4 →
0.85, tier 5 → 0.8, tier 6 → 0.6, tier 7–8 → 0.35.

### 2.3 Mapping raw capture → `ExtractedAction`

`ActionType` is fixed (`models.py`): `click, input, navigate, select, submit,
wait, scroll, hotkey, assert`.

| raw `type` | `ActionType` |
| --- | --- |
| `click` | `CLICK`, or `SUBMIT` when the element is a submit control or its label matches `Save/Submit/Next/Finish` |
| `input` / `change` on text/textarea | `INPUT` |
| `change` on select / combobox / lightning-combobox | `SELECT` |
| `navigate` | `NAVIGATE` |
| `keydown` with modifier | `HOTKEY` |
| `scroll` | `SCROLL` |

`target` MUST keep the existing prefix grammar consumed by
`replay_browser.build_selector_candidates`: `button:<label>`, `input:<label>`,
`link:<label>`, `text:<label>`. New prefixes require a matching change in
`build_selector_candidates` — coordinate through the orchestrator.

`ui_context` fields map: `object_name` ← `sf.object`, `modal_name` ←
`element.modal_label`, `url` ← `url`, `selector_hint` ← rank-1 selector,
`page_title`/`app_name` ← `sf.app` / captured title.

### 2.4 Noise reduction (required)

A raw trace of a real process contains 3–10x more events than meaningful steps.
`dom_extractor.py` MUST:

- Collapse consecutive `input` events on the same element into one final value.
- Drop `click` events on non-interactive containers that are followed within
  150ms by a `click` on a descendant/ancestor interactive element (event
  bubbling duplicates).
- Drop `scroll` unless it precedes an interaction with a previously off-screen
  element.
- Emit a `NAVIGATE` when `url` changes between consecutive events.
- Record every dropped event count in `ActionExtractionBundle.warnings`, so
  reduction is auditable and never silent.

### 2.5 Provenance

`DataProvenance.extraction_source` gains the value `"dom-capture"`. When DOM
capture is the source, `is_simulated` MUST NOT be triggered by extraction.
Owner of `html_report.py` change: **A5 only**.

---

## 3. STEP 6 CONTRACT — Agentforce bridge

All shapes below were read out of the installed Salesforce CLI
(`@salesforce/plugin-agent` 2.143.6, `@salesforce/agents`). Do not invent keys.

### 3.1 Agent spec YAML (input to `sf agent generate authoring-bundle --spec`)

Key order is significant — the CLI writes it in exactly this order:

```yaml
agentType: internal          # internal | customer
companyName: "..."
companyDescription: "..."
companyWebsite: "..."        # optional
role: "..."                  # the agent's job, prose
maxNumOfTopics: 5
agentUser: "..."             # optional org username
enrichLogs: false            # optional
tone: formal                 # formal | casual | neutral
promptTemplateName: "..."    # optional
groundingContext: "..."      # optional
topics:
  - name: Topic_Name
    description: "..."
```

`topics[]` entries carry `{name, description}` (the CLI sorts these keys
reverse-alphabetically on write: `name` then `description`).

### 3.2 Agent Script (`.agent`) grammar

Real grammar, from `@salesforce/agents/lib/templates/agentScriptTemplate.js`.
Blocks, in order: `system:` (`instructions`, `messages.welcome`,
`messages.error`), `config:` (`developer_name`, `default_agent_user`,
`agent_label`, `description`), `variables:` (`linked string` with
`source: @Object.Field`, or `mutable string`), `language:` (`default_locale`,
`additional_locales`, `all_additional_locales`), then
`start_agent <name>:` and one or more `subagent <name>:`.

Each agent/subagent block:

```
subagent update_case_status:
    label: "Update Case Status"
    description: "..."

    reasoning:
        instructions: ->
            | Multi-line instruction text, pipe-prefixed, indented.
        actions:
            action_name: @utils.transition to @subagent.other
```

Indentation is 4 spaces, nested consistently. `->` opens a block scalar and `|`
prefixes each line. Names in `@subagent.X` are `snake_case`. The router pattern
is `start_agent agent_router` with one `go_to_<topic>` action per subagent, plus
the three standard subagents `escalation`, `off_topic`, `ambiguous_question`.

**Generated `.agent` files must be validated with
`sf agent validate authoring-bundle`** — that is the only authority on whether
the grammar is right. A `.agent` file that has not been through the validator is
unverified output and must be labelled as such.

### 3.3 Test spec YAML — TWO runners, do not conflate

**Legacy / testing-center** (`AiEvaluationDefinition`):

```yaml
name: My_Tests
subjectType: AGENT
subjectName: My_Agent
testCases:
  - utterance: "Set case 500... to Working"
    expectedTopic: Update_Case_Status
    expectedActions: ["UpdateCaseStatus"]
    expectedOutcome: "Confirms the case status is now Working"
    customEvaluations: []
    metrics: [completeness, coherence, conciseness, output_latency_milliseconds]
```

Legacy expectation names map to: `topic_sequence_match`/`topic_assertion`,
`action_sequence_match`/`actions_assertion`,
`bot_response_rating`/`output_validation`.

**NGT / agentforce-studio** (`AiTestingDefinition`) — different shape, detected
by `inputs` + `scorers` on each test case:

```yaml
name: My_Tests
subjectName: My_Agent
testCases:
  - inputs:
      - utterance: "..."
        conversationHistory: []      # required by the task_resolution scorer
    scorers:
      - name: topic_match
        expected: Update_Case_Status
```

Rules enforced by the CLI: every NGT case needs >=1 `inputs` and >=1 `scorers`;
most scorers require `expected:`; multi-agent subjects require an
`agent_handoff_match` scorer.

**Default to legacy/testing-center** unless the target org only has
`AiTestingDefinition`. Emitting both is acceptable; guessing which one the org
uses is not — `sf agent generate test-spec` auto-detects, so prefer letting the
CLI decide and generate the shape that matches.

### 3.4 The iteration loop (the user's actual goal)

"Run that spec over and over to improve the spec" is implemented as:

```
derived spec (JSON)  ->  agentforce_spec.py  ->  specs/v1.yaml
                                                    │
   sf agent generate agent-spec --spec specs/v1.yaml --role "<refined>"
                                                    │  (LLM re-generates topics)
                                                 specs/v2.yaml
                                                    │
                              score each version offline (spec_score.py)
                                                    │
                    keep the higher score; stop when delta < epsilon
```

Offline scoring must be deterministic and must not require an org, so the loop
can run without burning org calls: coverage of observed objects/fields, topic
count sanity, absence of placeholder text, guardrail presence, entity coverage.
Org-dependent scoring (`sf agent test run`) is a separate, opt-in stage.

Every iteration MUST be written to disk as a new versioned file. Overwriting the
previous version destroys the audit trail that makes the loop meaningful.

---

## 4. File ownership map (STRICT — one owner per file)

### Step 5

| Agent | Owns (create/edit) |
| --- | --- |
| A1 | `capture/recorder.js` (in-page probe: listeners, selector computation, redaction) |
| A2 | `capture/inject.py` (Playwright driver: launch, frontdoor auth, inject probe, collect JSONL) |
| A3 | `src/sf_video_blueprint/dom_capture.py` (JSONL parse + Pydantic `RawDomEvent` model + validation) |
| A4 | `src/sf_video_blueprint/dom_extractor.py` (`DomCaptureExtractor`: noise reduction + `ActionExtractionBundle`) |
| A5 | `src/sf_video_blueprint/selectors.py` (ranking + `to_playwright_selectors`), plus the `"dom-capture"` provenance value in `html_report.py` |
| A6 | `tests/test_dom_capture.py` |
| A7 | `tests/test_dom_extractor.py` |
| A8 | `tests/test_selectors.py` |
| A9 | `src/sf_video_blueprint/redaction.py` (`redact_value`, `is_sensitive_field`) + `tests/test_redaction.py` |
| A10 | `docs/step5-dom-capture.md` (operator runbook: how to record a process) |

### Step 6

| Agent | Owns (create/edit) |
| --- | --- |
| B1 | `src/sf_video_blueprint/agentforce_spec.py` (`DerivedAgentSpec` -> spec YAML) |
| B2 | `src/sf_video_blueprint/agent_script.py` (`DerivedAgentSpec` -> `.agent`) |
| B3 | `src/sf_video_blueprint/eval_spec.py` (both test-spec dialects) |
| B4 | `src/sf_video_blueprint/spec_score.py` (deterministic offline spec scoring) |
| B5 | `src/sf_video_blueprint/iterate.py` (versioned refinement loop) |
| B6 | `tests/test_agentforce_spec.py` |
| B7 | `tests/test_agent_script.py` |
| B8 | `tests/test_eval_spec.py` + `tests/test_spec_score.py` |
| B9 | `scripts/agentforce_roundtrip.sh` (CLI round-trip: spec -> bundle -> validate) |
| B10 | `docs/step6-agentforce-bridge.md` (runbook + the iteration loop) |

### Orchestrator-only (no agent touches these)

`src/sf_video_blueprint/models.py`, `src/sf_video_blueprint/spec_builder.py`,
`src/sf_video_blueprint/cli.py`, `src/sf_video_blueprint/correlation.py`,
`src/sf_video_blueprint/replay_browser.py`, `scripts/score_run.py`,
`pyproject.toml`, `README.md`, this file.

Report needed changes to these; do not make them.

---

## 5. Definition of done (per agent)

1. The file(s) you own exist and import cleanly:
   `./.venv/bin/python -c "import sf_video_blueprint.<mod>"`
2. `./.venv/bin/python -m pytest tests/<your_test>.py` passes.
3. No placeholder markers in output: `Sample_Flow`, `button:Save` as a
   hardcoded default, `500xx0000012345AAA`, `TODO`, `FIXME`, `pass  # stub`.
4. Every function that can fail to derive something emits an explicit unknown
   rather than a plausible default.
5. You state, in your report, **what you did not verify**. Unverified work
   presented as done is the failure mode this whole project is correcting.
