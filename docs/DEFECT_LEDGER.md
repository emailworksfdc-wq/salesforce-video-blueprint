# Defect Ledger — All Rounds

This ledger tracks all defects across three rounds of adversarial audit. Each round targeted 
a different failure mode:

* **Round 1** (baseline): Derivation and naming correctness. The original 
  `INTERFACE_CONTRACT.md` established the contracts; no pre-existing numbered defects.
* **Round 2** (D1–D6): Scoring gate and refinement loop could not fail. 
  Documented in `INTERFACE_CONTRACT_ROUND2.md`.
* **Round 3** (A1–A2, D10–D14, others): Security, structural defects, false convergence. 
  Documented in `INTERFACE_CONTRACT_ROUND3.md`.

## Ledger

| ID | Round | Severity | Description | Measured Evidence | Root Cause | Owning File(s) | Status | Regression Test |
|----|-------|----------|-------------|-------------------|------------|----------------|--------|-----------------|
| **A1** | 3 | CRITICAL | Redaction leak undetected | Event with `value_redacted=True` carrying `4111111111111111` parsed with 0 findings | `validate_trace` did not check for leaked values when redaction flag was set | `dom_capture.py` | Fixed | `test_validate_trace_detects_redaction_flag_leak` |
| **A2** | 3 | HIGH | Silent total data loss | 15 lines failed to parse → 0 events, 15 `skipped_lines`, EMPTY `warnings` list | Data loss validation checked skipped lines but never surfaced warnings for catastrophic loss | `dom_capture.py` | Fixed | `test_validate_trace_partial_data_loss_warns_above_threshold` |
| **D1** | 2 | HIGH | Untested error paths scored as tested | `_score_testability` matched substring `"observed"` in the builder's negative sentinel `"No failures were observed in this run, so error paths are UNTESTED."` | Substring match instead of stable marker matching; the negative sentinel contains the word "observed" | `spec_score.py` | Fixed | `test_score_testability_negative_sentinel` (round 2) |
| **D2** | 2 | HIGH | Mandated inference penalised | `spec_builder._derive_entities` unconditionally adds `recordId` entity (inference-grounded); scorer subtracted inference penalty, so every real spec lost points for a field the builder mandates | Weighted average formula penalised inference, but builder always emits inference for recordId | `spec_score.py` | Fixed | `test_mandated_recordid_does_not_lower_score` (round 2, now G2) |
| **D3** | 2 | HIGH | Offline improvements lowered score | Deduplicating two identical orchestration steps lowered the score by 3 points | `_score_completeness` rewarded `len(steps) > 1` without checking distinctness; dedup reduced count, thus lowering score | `iterate.py`, `spec_score.py` | Fixed | `test_offline_improvement_changes_spec`, `test_d3_completeness_counts_distinct_steps` (round 2, now F3) |
| **D4** | 2 | HIGH | Provenance integrity bypassed in loop | `refine()` scored with `score_spec` (in-memory), never `score_spec_file`, so provenance_integrity was awarded free 5/5 inside the loop | In-memory scoring had no provenance argument, defaulted to 5/5 | `iterate.py`, `spec_score.py` | Fixed | `test_refine_passes_provenance_to_scorer` (round 2) |
| **D5** | 2 | HIGH | Bad spec scored 100/100 | Consequence of D1+D4: a spec with duplicated steps, duplicated guardrails, and explicitly untested error paths scored 100/100 and `band="high"` | Multiple scoring defects compounded | `spec_score.py` | Fixed | `test_d5_bad_spec_must_fail` (round 2, now F1) |
| **D6** | 2 | MEDIUM | Dead anti-gaming guard | Anti-gaming guard read `current_spec.unknowns` on BOTH sides of the comparison (`iterate.py:178-179`), so condition was `n < n` and could never fire | Copy-paste error: both sides of comparison referenced same variable | `iterate.py` | Fixed | `test_unknowns_deletion_warning_fires` (round 2, now F4) |
| **D10** | 3 | CRITICAL | Deleting evidence raised score | 3 entities (2 data-delta + 1 ui-action) = 93; delete ui-action = 95 (+2 points) | Weighted-AVERAGE grounding formula `(dd*1.0 + ui*0.85)/total`; deleting below-average member raises mean | `spec_score.py` | Fixed | `test_d10_weighted_average_formula_was_non_monotone`, `test_monotone_in_evidence_strengthening_entity_never_lowers_score` |
| **D11** | 3 | HIGH | Empty spec scored 10/10 placeholder_freedom | Empty spec had no content, thus no placeholder markers, thus scored perfectly on placeholder_freedom (10/10 vacuous points) | Dimension starts at max and only subtracts for known-bad patterns; absence of content = absence of patterns = perfect score | `spec_score.py` | Fixed | Empty-spec fixtures in `test_spec_score.py` now score < 50 |
| **D12** | 3 | HIGH | Empty spec scored 10/10 specificity | Empty spec had no intent text to check, thus no generic terms found, thus scored perfectly (10/10 vacuous points) | Specificity starts at max; empty intent has no verb/object, so "no problem found" = full marks | `spec_score.py` | Fixed | Same empty-spec fixtures in `test_spec_score.py` |
| **D13** | 3 | HIGH | Zero guardrails passed gate | Spec with `guardrails=[]` scored 92/100 and `passed=True` with no blocking issues | Absent guardrails subtracted completeness points but did not block; 92 > 75 = pass | `spec_score.py` | Fixed | `test_d13_absent_guardrails_is_blocking` |
| **D14** | 3 | MEDIUM | Gaming attacks exploited threshold | 4 of 5 adversarial specs passed the gate by sacrificing testability/specificity/provenance to hit exactly 75 points | `PASS_THRESHOLD=75` leaves 25 points of slack; weighted dimensions allow threshold surfing | `spec_score.py` | Partially fixed | `test_attack_*` in `test_gaming_resistance.py` — 10 of 12 now fail (attacks 5 & 6 still pass at 77 and 80) |
| **Router overflow** | 3 | MEDIUM | Router action names exceeded 80 chars | `MAX_NAME_LENGTH=80` capped base names, but `router_action_name` returns `f"go_to_{subagent}"` (6 chars longer); 80-char intent → 86-char router action, validated clean | Length cap set to API limit, but derived names add prefixes; validation only checked base names | `naming.py` | Fixed | `test_router_action_name_never_empty`, `test_linkage_router_action_to_subagent` (cap now 74) |
| **B1** | 3 | MEDIUM | Offline improvements no-op on real data | Loop ran 3 rounds scoring 82 → 82 → 82, stopped with "Converged: improvement < 2 for 2 consecutive rounds" | Every branch of `_apply_offline_improvements` matched synthetic placeholders `spec_builder` never emits (e.g., checks for "various", builder emits "UNRESOLVED") | `iterate.py` | Reported fixed, not independently verified | `test_offline_improvement_changes_spec`, `test_offline_improvement_no_change_returns_original` |
| **eval_spec degeneracy** | 3 | MEDIUM | Generated test utterances empty or malformed | Empty intent → `''`; duplicate entities → `'Update Case {status} {status}'`; empty entity name → `'Update Case {}'` | Utterance builder concatenated placeholders without checking for emptiness or duplicates | `eval_spec.py` | Fixed | `test_empty_intent_produces_marker`, `test_empty_entity_name_excluded`, `test_all_empty_entities_produces_base_intent` |
| **dedupe_names non-idempotence** | 3 | LOW | `dedupe_names` output changed on second pass | `dedupe_names(dedupe_names(["A","A"])) != dedupe_names(["A","A"])` | Deduper did not account for pre-existing numeric suffixes, so second pass re-deduped already-deduped names | `naming.py` | Fixed | `test_dedupe_idempotence` |
| **dedupe_names suffix collision** | 3 | LOW | `dedupe_names` produced duplicate output | Input `["A","A","A_2"]` produced output with duplicate `A_2` | Deduper generated `A_2` for second "A", colliding with existing `A_2` in input | `naming.py` | Fixed | `test_dedupe_suffix_collision_protection`, `test_dedupe_pre_existing_suffix` |
| **Needs_Evidence naming bypass** | 4 | CRITICAL | `agentforce_spec.py` never calls `topic_api_name` with the `Needs_Evidence` flag, so invalid names with forbidden patterns passed validation | Topic names containing `"Needs_Evidence"` are invalid per Agentforce grammar but were never filtered | `topic_api_name` has a `needs_evidence` parameter but caller never passes it; the guard exists but is dead code | `agentforce_spec.py` | Fix in flight | Not yet verified |
| **validate_trace never called in production** | 4 | CRITICAL | `dom_capture.validate_trace()` is wired and tested but never invoked in the CLI or any production path; redaction leaks and data loss go undetected | The validator exists but no caller ever runs it, so its findings never surface | Validator written but not integrated into the pipeline | `cli.py`, `dom_capture.py` | Fix in flight | Not yet verified |
| **scripts/score_run.py weaker than in-process scorer** | 4 | HIGH | `scripts/score_run.py` (the gate used for CI/quality checks) is strictly weaker than `spec_score.py:score_spec_file()` AND wrongly blocks declared unknowns (penalizes honesty) | Shell script copy-pasted an older version of the scoring logic and diverged; it does not enforce blocking issues the in-process scorer does | `scripts/score_run.py` maintained separately from `spec_score.py`; no test enforces equivalence | `scripts/score_run.py` | Fix in flight | Not yet verified |
| **evidence_grounding mutable to max with all tests green** | 4 | HIGH | `spec_score.py:_score_evidence_grounding()` formula can be changed to always return max (10/10 regardless of evidence quality) and every test still passes | No test asserts that strengthening evidence from inference→ui-action→data-delta raises the grounding score | Tests check structure but not the grounding formula's sensitivity to evidence quality | `spec_score.py` | Fix in flight | Not yet verified |

## Still open (requiring upstream or org-dependent validation)

| Item | Why Open | Mitigation | Tracked In |
|------|----------|------------|------------|
| **Live org validation** | `sf agent validate authoring-bundle` and `sf agent generate agent-spec` have never been run on pipeline output | Only way to prove deployability is to run against a real org; local validation proves internal consistency only | `INTERFACE_CONTRACT_ROUND3.md` § Residual risk |
| **Event Monitoring collection** | No Event Monitoring logs have ever been collected; correlation tests use hand-written mocks | The `telemetry_source="event-monitoring"` marker is wired into the scoring gate but aspirational | `INTERFACE_CONTRACT_ROUND3.md` § Residual risk |
| **Non-tautological correlation** | Correlation tests construct UI events and backend logs from the same spec, guaranteed to match | Whether genuinely-observed UI events correlate to genuinely-observed backend queries (independent data sources) is unproven | `INTERFACE_CONTRACT_ROUND3.md` § Residual risk |
| **Orphaned schemas** | 5 JSON schemas have no emitting module: `step_ledger_schema.json`, `evidence_metadata_schema.json`, `traceability_matrix_row_schema.json`, `failure_summary_schema.json`, `replay_manifest_schema.json` | Documented as "forward-looking" in `test_schemas.py`; should be deleted or moved to `docs/schemas/` as non-normative examples if never emitted | `test_schemas.py`, `INTERFACE_CONTRACT_ROUND3.md` § Residual risk |
| **Dependency pinning** | Salesforce CLI and plugins are not pinned; tested version is `@salesforce/plugin-agent` 2.143.6 but not enforced | Pin in lockfile or document tested version explicitly; uncontrolled CLI upgrade could break emitters silently | `INTERFACE_CONTRACT_ROUND3.md` § Residual risk |
| **Prod-vs-sandbox guard** | ~~No runtime enforcement~~ **Now enforced in code** — `replay_browser.BLOCKED_ORG_ALIASES` (`replay_browser.py:17`, hard-deny checked at `:126`), `_is_production_org()` (`:187`), and `telemetry._verify_org_is_sandbox()` / `_is_org_forbidden()` (`telemetry.py:152-163`, fail-closed). `SF_ALLOW_PRODUCTION_ORG=1` is an escape hatch for production but **cannot** override the PPCDM/PPCaccenture hard-deny. | Residual: the guards are unit-tested but have never run against a real org, so the `sf org display --json` parsing path is unexercised. Bypass surface (alias case/whitespace/substring variants, reaching a blocked org via username or instance URL without naming its alias) is under audit. | Verified at source by the orchestrator, round 5 |

## Test coverage summary

Any count here is a snapshot and goes stale fast. Run `pytest -q` for the current number.

As of 2026-07-25, measured by the orchestrator at the end of round 5:

* **Total tests:** 764 passing, 0 failing (`./.venv/bin/python -m pytest tests/ -q`)
* Earlier counts recorded during round 4/5 (483, 588, 689/51, 702/1, 752/3) were taken *while*
  concurrent agents were mid-edit and were never end-state measurements. Do not cite them.
* **Round 3 defects verified fixed:** A1, A2, D10, D11, D12, D13, router overflow, eval_spec 
  degeneracy, dedupe_names idempotence & collision
* **Round 4 criticals — all four fixed and independently verified at source by the orchestrator**
  (not merely reported by the fixing agent):
  * `Needs_Evidence` bypass — topic name now derives from `naming.topic_api_name` for unresolved
    intents; the hardcoded literal is gone from `src/`.
  * `validate_trace` never called — now wired at `cli.py:127`, before extraction, failing closed
    on `SECURITY CRITICAL:` and `DATA LOSS:` findings. The A1 leak probe reports the leak and the
    canary value appears in no output file.
  * `score_run.py` weaker than the in-process scorer — now delegates to
    `spec_score.score_spec_file()`. Re-measured with a rebuilt F1 "bad spec": previously
    100/100 pass, now **exit code 1** with three blocking issues. The honesty inversion
    (blocking on declared `unknowns` / confidence < 0.5) is removed. No threshold was relaxed.
  * `_score_evidence_grounding` mutable to max with all tests green — absolute floor/ceiling
    assertions added, plus blocker-presence tests. An ungrounded spec now measures 2/30.
* **Round 5 was NOT clean.** A TODO false-positive fix narrowed `markers.scan_spec` so it stopped
  scanning evidence details, silently reverting round 3's D7 fix and failing the regression class
  named `TestD7Regression`. Caught by an orchestrator suite run, not by the agent's own report,
  which had claimed success. Reverted; D7 again catches all three stub fingerprints in evidence
  details with `scan_spec ⊆ scan_text(json.dumps(spec))` containment intact.
* **Consequence for the two-consecutive-clean-rounds bar: not met.** Round 5 introduced a real
  regression, so at least one further clean round is required before that claim can be made.

## Verification notes

The following fixes were independently verified by running code and observing behaviour, not 
just reading test names:

1. **A1 (redaction leak):** Ran `test_validate_trace_detects_redaction_flag_leak` and 
   confirmed it detects `value_redacted=True` with `value != None` as a finding flagged 
   "SECURITY CRITICAL". Read `dom_capture.py:403-407` to confirm the validation logic.
2. **A2 (data loss):** Ran `test_validate_trace_partial_data_loss_warns_above_threshold` and 
   confirmed it surfaces a warning when `skipped_lines / total > 0.2`.
3. **D10 (monotonicity):** Ran `test_d10_weighted_average_formula_was_non_monotone` and 
   confirmed deleting a ui-action entity no longer raises the score. Read 
   `spec_score.py:_score_evidence_grounding` to confirm the formula changed from weighted 
   average to a floor+bonus model.
4. **D13 (guardrails):** Ran `test_d13_absent_guardrails_is_blocking` and confirmed 
   `guardrails=[]` produces a blocking issue and `passed=False`.

The following fixes were taken on report from test names and commit history, not 
independently verified by running the defective code:

* **B1 (offline improvements no-op):** Tests pass (`test_offline_improvement_changes_spec`), 
  but I did not reproduce the original 82 → 82 → 82 convergence on a real spec. The fix 
  expanded the improvement patterns to match what `spec_builder` actually emits, which is 
  plausible, but unverified on real data.
* **Router overflow, eval_spec degeneracy, dedupe_names fixes:** Tests pass and code 
  inspection confirms the fixes (cap reduced to 74, empty entities excluded, idempotence 
  checks added), but I did not reproduce the original defects by generating an 80-char intent 
  or running `dedupe_names` twice on the same input.

## Claims in round 1/round 2 contracts that are no longer true

None. The round 1 and round 2 contracts established interfaces and invariants; all remain 
valid. The round-3 fixes extend those invariants (adding G6, G7, S1–S3, N1–N3, R1–R2, C1–C2) 
but do not invalidate prior contracts.

The partial fix for D14 (2 of 12 attacks still pass) does not invalidate the round-2 claim 
that "the gate is falsifiable" — it is, on 10 of 12 vectors. The threshold-surfing attacks 
(5 & 6) exploit a known weakness (25 points of slack in the 75/100 threshold) that is 
documented as residual risk, not claimed as solved.

## Round 4 status (as of 2026-07-25)

Round 4 was **NOT clean**. Four critical defects were confirmed and are being fixed by other 
agents right now:

1. **Needs_Evidence naming bypass** — `agentforce_spec.py` never passes the `needs_evidence` 
   flag to `topic_api_name`, so invalid names slip through.
2. **validate_trace never called** — the validator exists but no production path invokes it.
3. **scripts/score_run.py weaker than in-process scorer** — the gate script diverged from the 
   real scorer and wrongly penalizes declared unknowns (honesty penalty).
4. **evidence_grounding formula mutability** — the grounding score can be changed to always 
   return max with all tests green; no test asserts sensitivity to evidence quality.

The "two consecutive clean rounds" bar for shipping is **not met**. Round 5 will be required 
to verify these fixes and check for new defects they may have introduced.
