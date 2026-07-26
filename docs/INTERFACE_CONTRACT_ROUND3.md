# Interface contract — convergence round 3 (security + structural defects)

Addendum to `docs/INTERFACE_CONTRACT_ROUND2.md`. Same rules apply: **one owner per file**, 
never edit a file you do not own, never relax a gate, never weaken a test to make it pass. 
This addendum pins the behaviour the round-3 fixes must produce, documenting the defects 
confirmed empirically in this round and the invariants they violate.

## Why this round exists

Round 1 fixed derivation and naming. Round 2 fixed the scoring gate and refinement loop so 
they could actually fail. Round 3 targets **security-critical defects, structural scoring 
gaps, and false-success signals** that survived rounds 1 and 2. Their common shape: the 
pipeline looked clean while either leaking secrets, deleting evidence for points, or 
reporting convergence on a no-op.

## Confirmed defects

### Security & data integrity (most serious)

| ID | File | Measured evidence | Root cause |
|----|------|-------------------|------------|
| **A1** | `dom_capture.py` | An event with `value_redacted=True` carrying the raw value `4111111111111111` parsed cleanly with **0 findings**. | `validate_trace` did not detect a redaction leak. A recorder redaction failure would have leaked PII into specs and HTML reports while the flag asserted it was scrubbed. |
| **A2** | `dom_capture.py` | A capture whose 15 lines all failed to parse produced 0 events, 15 `skipped_lines`, and an **EMPTY** `warnings` list. 100% data loss, silently. | Total data loss produced no warning. The validation logic checked for parse failures but never surfaced them when they represented catastrophic loss. |

### Scoring gate defects (invariant violations)

| ID | File | Measured evidence | Root cause |
|----|------|-------------------|------------|
| **D10** | `spec_score.py` | Deleting genuinely-observed evidence **RAISED** the score. Measured: 3 entities (2 data-delta + 1 ui-action) = 93; delete the ui-action = **95**. | **Invariant G1 violated.** A weighted-AVERAGE grounding formula, `(dd*1.0 + ui*0.85)/total`, and any mean is maximised by deleting its below-average member. This is the most dangerous defect class in the project — the scorer drives a refinement loop, so if deleting evidence pays, the loop learns to delete evidence. |
| **D13** | `spec_score.py` | A spec with **ZERO guardrails** scored 92 and **passed** with no blocking issues. | Absent guardrails were not structural blockers. The completeness dimension subtracted points, but 92 > 75 = pass. A deployable Agentforce agent requires guardrails; their total absence should block regardless of numeric score. |
| **D11** | `spec_score.py` | An **empty spec** scored 10/10 on specificity and 10/10 on placeholder_freedom: **20 vacuous points**. | Both dimensions start at max and only subtract for known-bad patterns. An empty spec has no patterns, so it scores perfectly. Absence of content is not the same as absence of defects. |
| **D12** | `spec_score.py` | Same empty spec scored 10/10 on specificity. | Specificity starts at max, so the zero-entity case never triggered any penalty. Intent="" had no verb/object to check, so "no problem found" = full marks. |
| **D14** | `spec_score.py` | 4 of 5 gaming attacks passed the gate. `PASS_THRESHOLD` is 75/100, leaving **25 points of exploitable slack** (sacrifice testability 10 + provenance 5 + specificity 10 and still pass). Round-3 fixes reduced this to 2 of 12 attacks passing (attacks 5 and 6 still exploit the threshold). | The 75-point threshold was reachable by a deliberately bad spec that maxed out the easiest dimensions while tanking the hardest ones. This is a threshold-surfing attack: structured to hit exactly 75 by gaming dimension weights. |

### Naming & length budget

| ID | File | Measured evidence | Root cause |
|----|------|-------------------|------------|
| **Router-action overflow** | `naming.py` | ⚠️ **RETRACTED — the org does not enforce this.** Originally recorded as: `MAX_NAME_LENGTH` was 80 and capped topic/subagent names, but `router_action_name` returns `go_to_<subagent>`, 6 chars longer, so an 80-char intent produced an **86-char router action** that `validate_locally` passed and would "reach an org unchecked". Measured against org AFT3 on 2026-07-26 with `sf agent validate authoring-bundle`: an 80-char subagent name with its 86-char `go_to_…` action **compiled successfully** (exit 0), and a deliberately-built **100-char router action** with a short subagent name also compiled (exit 0). The compiler enforces `<=80` on the **subagent name only** — 80 passes, 81 fails with `Too big: expected string to have <=80 characters`. | The original root cause (validation checked base names, not prefixed outputs) was sound reasoning about an API limit that turned out not to apply to the prefixed identifier. The cap remains **74** — strictly inside the measured 80, and `topic_api_name` also feeds the metadata path, whose limit is still unmeasured. The `validate_locally` router-action check is retained but relabelled **advisory**: it flags a runaway derived name, it does not reproduce an org rejection. |

### Refinement loop defects (false convergence)

| ID | File | Measured evidence | Root cause |
|----|------|-------------------|------------|
| **B1** | `iterate.py` | The offline improvement lever is **decorative on real data**. Measured on a genuinely derived spec: the loop ran 3 rounds scoring **82 → 82 → 82** and stopped with "Converged: improvement < 2 for 2 consecutive rounds". | Every branch of `_apply_offline_improvements` only matches synthetic placeholder text `spec_builder` never emits. Examples: "remove vague terms" checks for `"various"` and `"etc."`, but the builder uses the stable marker `"UNRESOLVED"`. "Expand guardrails" checks for `"validate input"`, but the builder emits `"Validate <Object> field changes"`. "Converged" on a no-op lever is a false success signal. |

### Generated test quality

| ID | File | Measured evidence | Root cause |
|----|------|-------------------|------------|
| **eval_spec utterance degeneracy** | `eval_spec.py` | Empty intent → empty utterance `''`; duplicate entities → `'Update Case {status} {status}'`; empty entity name → `'Update Case {}'`. | Generated tests that validate nothing while looking like coverage. The utterance builder concatenated entity placeholders without checking for emptiness or duplicates. Fixed: empty entities excluded, duplicates deduped, empty intent produces a marker. |

### Algorithmic correctness

| ID | File | Measured evidence | Root cause |
|----|------|-------------------|------------|
| **`dedupe_names` non-idempotence** | `naming.py` | `dedupe_names(dedupe_names(["A", "A"]))` ≠ `dedupe_names(["A", "A"])`. Running twice produced different output. | The deduplication logic did not account for pre-existing numeric suffixes, so a second pass would re-dedupe already-deduped names. |
| **`dedupe_names` suffix collision** | `naming.py` | Input `["A", "A", "A_2"]` produced output with a duplicate `A_2`. | The deduper generated `A_2` for the second "A", colliding with the existing `A_2` in the input. Fixed: scan for pre-existing suffixes and skip them. |

## New invariants (continuing from round 2's G1–G5, F1–F4)

### Monotonicity & evidence integrity

* **G6 — Adding evidence never lowers the score.** Strengthening an entity from inference to 
  data-delta or ui-action (adding a SpecEvidence entry) must **never lower** `SpecScore.total`. 
  If it does, the loop learns to delete evidence traces.
* **G7 — Deleting evidence never raises the score.** Removing a genuinely-observed entity 
  (evidence source `data-delta` or `ui-action`) from a spec must **never raise** 
  `SpecScore.total`. This is G1 from round 2, proven violated by D10, now restated with the 
  measured counterexample.

### Structural blockers (absence = fail)

* **S1 — Absent guardrails block deployment.** A spec with `guardrails == []` must have 
  `passed=False` regardless of numeric score. Agentforce agents without guardrails are not 
  deployable; the gate must enforce this structurally.
* **S2 — Empty content is not perfection.** A spec with empty intent, zero entities, or zero 
  orchestration steps must score **< max** on every dimension that measures presence. Starting 
  at max and subtracting only for known-bad patterns is backwards: absence is a defect.
* **S3 — Structural defects block regardless of numeric score.** Blocking issues must be 
  independent of `SpecScore.total`. A spec that scores 95/100 but has zero guardrails, or zero 
  entities, or a redaction leak, must have `passed=False`.

### Name derivation & length budgeting

* **N1 — Every name-derivation function is idempotent.** For all `f` in 
  {`topic_name`, `subagent_name`, `router_action_name`, `dedupe_names`}, 
  `f(f(x)) == f(x)` must hold. Running the deriver twice must produce the same output as 
  running it once.
* **N2 — Derived names respect the length budget including all prefixes.** If 
  `MAX_NAME_LENGTH = 74` and `router_action_name(intent)` produces `f"go_to_{subagent_name(intent)}"`, 
  then `len(router_action_name(intent)) <= 80` must hold for all intents. The cap must 
  budget ALL derived forms, not just the base form.
* **N3 — `dedupe_names` output is collision-free.** For all inputs `names: list[str]`, 
  `len(dedupe_names(names)) == len(set(n.lower() for n in dedupe_names(names)))` must hold 
  (case-insensitive uniqueness).

### Redaction & secret handling

* **R1 — A redaction leak is always a finding and is never echoed.** If 
  `event.value_redacted=True` and `event.value is not None`, `validate_trace` must return a 
  finding flagged `"SECURITY CRITICAL"`. The finding text must **never** echo the leaked value. 
  Log the event seq/index only.
* **R2 — Total or substantial data loss is always surfaced.** If 
  `len(skipped_lines) / (len(events) + len(skipped_lines)) > 0.2` (20% loss), 
  `validate_trace().warnings` must contain a data-loss warning. Silent loss is a defect.

### Refinement convergence

* **C1 — A no-op refinement is never reported as convergence.** If 
  `_apply_offline_improvements(spec, score) == (spec, summary)` (no changes), 
  `IterationResult.stop_reason` must **not** claim convergence. It must say 
  `"No improvement applied; stopping"` or equivalent. Reporting "converged" when the lever 
  never fired is a lie.
* **C2 — Generated test utterances are never empty or degenerate.** For all 
  `build_test_utterance(intent, entities)`, the output must be non-empty, must not be 
  `"{}"` (empty placeholder), and must not contain duplicate placeholders (`"{status} {status}"`).

## Hard prohibitions (carrying forward from rounds 1 and 2, unchanged)

* **Never relax `scripts/score_run.py` thresholds.** `PASS_THRESHOLD` stays 75. Making the 
  gate weaker is a defect, not a fix. Every change in this round must make the gate strictly 
  *more* able to fail.
* **Never weaken a gate or a test to make it pass.** If a test fails, fix the code or the 
  fixture, then add a test proving the fix fails in the right cases — in *both* directions.
* **Never invent evidence.** `_apply_offline_improvements` must never add entities, objects, 
  topics, or failure scenarios that were not in the input spec. It may only tighten prose, 
  dedupe, normalise names, reorder, and make existing guardrails name objects/fields already 
  present.
* **Never echo a leaked secret into a log, finding, exception, or report.** If a redaction 
  leak is detected, log the event sequence number and field name only. The leaked value must 
  not appear in any output the user or an LLM will read.
* **Never stamp a real provenance source on stub/mock data.** If `extraction_source="stub"` 
  or `telemetry_source="mock"`, the provenance integrity dimension must score 0 and block. 
  This is the hard cap that makes the whole scoring regime falsifiable.
* **Scorer/builder/extractor stay deterministic and pure.** No clocks, no randomness, no 
  network, no org, no LLM in the scorer, the builder, or the offline loop. The stopping 
  condition depends on determinism.

## Residual risk (what is still unproven)

This section is the most important and must not be softened. Passing three rounds of 
adversarial audit earns confidence in the pipeline's **internal consistency** — derivation, 
scoring, and refinement are now provably non-degenerate and evidence-respecting. It does 
**not** mean the output is deployable.

### Nothing in this project has ever touched a real Salesforce org

* `sf agent validate authoring-bundle` — the only authority on whether the emitted `.agent` 
  file's grammar is correct — **has never been run** on output from this pipeline.
* `sf agent generate agent-spec` — the real LLM-driven refinement path — **has never been 
  run** in the loop. The `use_cli=True` branch in `iterate.py` is a placeholder; it shells 
  out but does not parse the result.
* The 80-char API-name cap is an **assumption**. It is documented in Salesforce conventions 
  and enforced by the round-3 naming fix (cap at 74 to budget prefixes), but it has never 
  been validated against a real org's metadata limits.
* A clean local validation (0 findings from `validate_trace`, `score >= 75`, 
  `passed=True`) does **not** mean the artifact deploys. It means the artifact is internally 
  consistent and evidence-grounded. Deployability requires an org.

### Event Monitoring collection is still aspirational

* The telemetry layer (`correlation.py`) assumes Event Monitoring logs will provide SOQL 
  query results, Apex execution traces, and REST call payloads to correlate with UI actions.
* **No Event Monitoring data has ever been collected** in this project. The correlation tests 
  use hand-written mock payloads that match the expected shape. Whether real Event Monitoring 
  logs are parseable, complete, or arrive in the right time window is unproven.
* The `telemetry_source="event-monitoring"` provenance marker is **aspirational**. It is 
  wired into the scoring gate (via `markers.REAL_TELEMETRY_SOURCES`) but has never been 
  stamped on real data.

### Non-tautological correlation is unproven

* `correlation.py` matches UI events to backend queries/Apex calls by comparing object names 
  and timestamp windows. The tests construct UI events and backend logs **from the same 
  source** (a spec), so they are guaranteed to match. This is a tautology.
* Whether a genuinely-observed UI event (clicking "Save" on a Case record) will successfully 
  correlate to a genuinely-observed backend query (a SOQL `UPDATE` on `Case`) when both come 
  from independent data sources (DOM capture + Event Monitoring) is **unproven**.

### Orphaned schemas are documented but not validated

* Five JSON schemas in `schemas/` have no emitting module: `step_ledger_schema.json`, 
  `evidence_metadata_schema.json`, `traceability_matrix_row_schema.json`, 
  `failure_summary_schema.json`, `replay_manifest_schema.json`.
* These are documented as "forward-looking" or "template" artifacts in `test_schemas.py`, 
  but no code validates that they are needed, sufficient, or correctly shaped for the 
  downstream tools they imply (a traceability matrix viewer, a failure summarizer, etc.).
* They remain in the repo as design debt. If they are never emitted, they should be deleted 
  or moved to a `docs/schemas/` directory as non-normative examples.

### Dependency pinning is incomplete

* `pyproject.toml` pins Pydantic, Playwright, and a few others, but **not** the Salesforce CLI 
  or its plugins. `sf --version` in CI is whatever the installer fetches.
* The CLI's YAML and `.agent` grammars are load-bearing interfaces. An uncontrolled CLI 
  upgrade could break the emitters (`agentforce_spec.py`, `agent_script.py`) silently if the 
  grammar changes.
* Mitigation: pin `@salesforce/plugin-agent` and `@salesforce/agents` in a lockfile or 
  document the tested version explicitly. Current tested version: `@salesforce/plugin-agent` 
  2.143.6 (documented in `INTERFACE_CONTRACT.md`, not enforced).

### Prod-vs-sandbox guard is NOW ENFORCED (updated 2026-07-25)

* **UPDATE:** Round 4 confirmed that production-org guards ARE enforced in code. Both 
  `replay_browser.py` and `telemetry.py` implement fail-closed guards:
  - `replay_browser.py:_is_production_org()` queries `sf org display --json` for `isSandbox`, 
    `isScratch`, and instance URL markers. If org type cannot be determined, the guard fails 
    closed and refuses to proceed. Production orgs are refused by default; override via 
    `SF_ALLOW_PRODUCTION_ORG=1` (logged as a warning).
  - `telemetry.py:_verify_org_is_sandbox()` and `_is_org_forbidden()` enforce the same rules 
    for telemetry collection.
  - **PPCDM and PPCaccenture are hard-blocked** via `BLOCKED_ORG_ALIASES` / 
    `_FORBIDDEN_ORG_ALIASES`. Even `SF_ALLOW_PRODUCTION_ORG=1` cannot bypass this. Both raise 
    `BlockedOrgError` / return `ORG_FORBIDDEN` with no override available.
* The round-3 claim that this was "a convention, not enforcement" is **no longer accurate**. 
  The guards exist and are wired. This section is preserved for history but marked as outdated.

## What changed in round 3 (summary for agents)

Round-3 fixes must produce these behaviours, provable by the tests listed:

1. **A1 fix:** `validate_trace` detects redaction leaks (test: 
   `test_validate_trace_detects_redaction_flag_leak`).
2. **A2 fix:** Total data loss produces a warning (test: 
   `test_validate_trace_partial_data_loss_warns_above_threshold`).
3. **D10 fix:** Deleting observed evidence never raises the score (test: 
   `test_d10_weighted_average_formula_was_non_monotone`).
4. **D13 fix:** Absent guardrails block deployment (test: 
   `test_d13_absent_guardrails_is_blocking`).
5. **D11/D12 fix:** Empty content scores low, not perfect (tests: empty-spec fixtures in 
   `test_spec_score.py` now score < 50).
6. **Router overflow fix:** `MAX_NAME_LENGTH` reduced to 74, all derived names fit within 80 
   chars (test: `test_router_action_name_never_empty` plus manual measurement).
7. **B1 fix:** Offline improvements now fire on real data; convergence only reported when 
   changes stop (test: `test_offline_improvement_changes_spec`).
8. **eval_spec fix:** Utterances never empty or degenerate (tests: 
   `test_empty_intent_produces_marker`, `test_empty_entity_name_excluded`).
9. **dedupe_names fix:** Idempotent and collision-free (tests: `test_dedupe_idempotence`, 
   `test_dedupe_suffix_collision_protection`).

**Still open (D14 partial):** 2 of 12 gaming attacks still pass (attacks 5 and 6). These 
exploit threshold surfing: deliberately sacrificing testability, specificity, and provenance 
to hit exactly 75/100. They score 77 and 80 respectively. Tightening the threshold is 
prohibited (hard prohibition: never relax the gate), so fixing this requires either 
(a) making those dimensions harder to exploit, or (b) adding cross-dimension sanity checks 
(e.g., if `testability < 5/10` AND `specificity < 5/10`, block regardless of total).

Round 3 reduced exploitable attacks from 4 of 5 (80% pass rate on adversarial inputs) to 
2 of 12 (17% pass rate). The gate is now falsifiable on 10 of 12 attack vectors.

## Round 4 status (as of 2026-07-25)

Round 4 was **NOT clean**. Four critical/high defects were confirmed:

1. **Needs_Evidence naming bypass (CRITICAL)** — `agentforce_spec.py` never calls 
   `topic_api_name` with the `needs_evidence` parameter, so invalid names containing 
   `"Needs_Evidence"` pass validation. The guard exists but is dead code.
2. **validate_trace never called in production (CRITICAL)** — `dom_capture.validate_trace()` is 
   wired and tested but never invoked in the CLI or any production path. Redaction leaks and 
   data loss go undetected.
3. **scripts/score_run.py weaker than in-process scorer (HIGH)** — The gate script 
   (`scripts/score_run.py`) diverged from `spec_score.py:score_spec_file()` and wrongly 
   blocks declared unknowns (penalizes honesty). It is strictly weaker than the real scorer 
   on blocking issues.
4. **evidence_grounding mutable to max with all tests green (HIGH)** — The grounding formula 
   can be changed to always return 10/10 regardless of evidence quality, and every test still 
   passes. No test asserts that strengthening evidence from inference→ui-action→data-delta 
   raises the score.

**Fixes are in flight by other agents** (see `DEFECT_LEDGER.md`). These defects are NOT 
independently verified as fixed yet.

**The "two consecutive clean rounds" bar is NOT met.** Round 5 will be required to verify 
these fixes and check for regressions they may have introduced.
