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
| **Router overflow** | 3 | ~~MEDIUM~~ NOT A DEFECT | ~~Router action names exceeded 80 chars~~ | **Retracted on org evidence.** Claimed: 80-char intent → 86-char router action, validated clean. Measured on AFT3 2026-07-26 via `sf agent validate authoring-bundle`: that 86-char action **compiles** (exit 0), and a 100-char router action also compiles (exit 0). The compiler applies `<=80` to the **subagent name** only — 80 passes, 81 fails with `Too big: expected string to have <=80 characters` | The 80-char API-name limit is real but applies to the subagent name, not to the `go_to_` action referencing it. The original finding assumed the cap covered derived identifiers | `naming.py` | Retracted; cap kept at 74 as deliberate headroom (see `COMPILER_VERIFIED_NAME_LIMIT`) | `test_compiler_verified_limit_is_80`, `test_derived_names_stay_inside_the_compiler_verified_limit` |
| **Block scalar missing owning key** | Lane 01 | **CRITICAL** | Every emitted bundle failed to compile | `sf agent validate authoring-bundle -o AFT3 --json` → exit 1, **24 `CompilationError`s**: ``Syntax error: unexpected `->` [Ln 108, Col 8]``, then one ``Syntax error: unexpected `\| …` `` per instruction line. `validate_locally` reported **0 findings** on the rejected file | `_block_scalar` emitted a bare `->` opener with `\|` lines at the same indent. The grammar requires `instructions: ->` with `\|` lines nested one level deeper. The three standard subagents are copy-pasted from the first-party template and always had the key, so the emitter's own grammar model was never exercised until an org saw it | `agent_script.py` | Fixed — same bundle now compiles, exit 0 `{"success": true}` | `test_derived_subagent_block_scalar_has_instructions_key`, `test_block_scalar_pipes_are_indented_deeper_than_their_opener` |
| **B1** | 3 | MEDIUM | Offline improvements no-op on real data | Loop ran 3 rounds scoring 82 → 82 → 82, stopped with "Converged: improvement < 2 for 2 consecutive rounds" | Every branch of `_apply_offline_improvements` matched synthetic placeholders `spec_builder` never emits (e.g., checks for "various", builder emits "UNRESOLVED") | `iterate.py` | Reported fixed, not independently verified | `test_offline_improvement_changes_spec`, `test_offline_improvement_no_change_returns_original` |
| **eval_spec degeneracy** | 3 | MEDIUM | Generated test utterances empty or malformed | Empty intent → `''`; duplicate entities → `'Update Case {status} {status}'`; empty entity name → `'Update Case {}'` | Utterance builder concatenated placeholders without checking for emptiness or duplicates | `eval_spec.py` | Fixed | `test_empty_intent_produces_marker`, `test_empty_entity_name_excluded`, `test_all_empty_entities_produces_base_intent` |
| **dedupe_names non-idempotence** | 3 | LOW | `dedupe_names` output changed on second pass | `dedupe_names(dedupe_names(["A","A"])) != dedupe_names(["A","A"])` | Deduper did not account for pre-existing numeric suffixes, so second pass re-deduped already-deduped names | `naming.py` | Fixed | `test_dedupe_idempotence` |
| **dedupe_names suffix collision** | 3 | LOW | `dedupe_names` produced duplicate output | Input `["A","A","A_2"]` produced output with duplicate `A_2` | Deduper generated `A_2` for second "A", colliding with existing `A_2` in input | `naming.py` | Fixed | `test_dedupe_suffix_collision_protection`, `test_dedupe_pre_existing_suffix` |
| **Needs_Evidence naming bypass** | 4 | CRITICAL | `agentforce_spec.py` never calls `topic_api_name` with the `Needs_Evidence` flag, so invalid names with forbidden patterns passed validation | Topic names containing `"Needs_Evidence"` are invalid per Agentforce grammar but were never filtered | `topic_api_name` has a `needs_evidence` parameter but caller never passes it; the guard exists but is dead code | `agentforce_spec.py` | Fix in flight | Not yet verified |
| **validate_trace never called in production** | 4 | CRITICAL | `dom_capture.validate_trace()` is wired and tested but never invoked in the CLI or any production path; redaction leaks and data loss go undetected | The validator exists but no caller ever runs it, so its findings never surface | Validator written but not integrated into the pipeline | `cli.py`, `dom_capture.py` | Fix in flight | Not yet verified |
| **scripts/score_run.py weaker than in-process scorer** | 4 | HIGH | `scripts/score_run.py` (the gate used for CI/quality checks) is strictly weaker than `spec_score.py:score_spec_file()` AND wrongly blocks declared unknowns (penalizes honesty) | Shell script copy-pasted an older version of the scoring logic and diverged; it does not enforce blocking issues the in-process scorer does | `scripts/score_run.py` maintained separately from `spec_score.py`; no test enforces equivalence | `scripts/score_run.py` | Fix in flight | Not yet verified |
| **evidence_grounding mutable to max with all tests green** | 4 | HIGH | `spec_score.py:_score_evidence_grounding()` formula can be changed to always return max (10/10 regardless of evidence quality) and every test still passes | No test asserts that strengthening evidence from inference→ui-action→data-delta raises the grounding score | Tests check structure but not the grounding formula's sensitivity to evidence quality | `spec_score.py` | Fix in flight | Not yet verified |
| **L4-1** | 6 | HIGH | Ingest rejected legitimate events: `role`/`name` required on `RawRoleName`, but the recorder returns `{role, name: null}` for any element with no accessible name and `{role: null, ...}` for any tag outside its ~17-entry implicit-role map (`capture/recorder.js:161`, `:136`) — so every `div`/`span` click was discarded | 4 legitimate lines → 1 event, 3 `skipped_lines`, `DATA LOSS: 3 of 4 lines were skipped (75%)`. After: 4/4 parsed, no finding | Parser modelled the schema it wished the recorder had, not the one it emits. Two required fields on an untrusted-input boundary where the producer legitimately emits null | `dom_capture.py` | Fixed | `test_role_name_both_null_is_accepted`, `test_role_name_still_rejects_wrong_types`, `test_role_name_optional_fields_produce_no_role_selector` (+3) |
| **L4-2** | 6 | HIGH | UTF-8 BOM ate the first event: capture and manifest were opened with `encoding="utf-8"`, so a BOM-prefixed file lost line 1 | 3 events written → 2 parsed, skipped line 1 `"Unexpected UTF-8 BOM (decode using utf-8-sig)"`. After: 3/3. Same bug found and fixed in `load_manifest` | Decoding a file written by an unknown producer without tolerating a BOM | `dom_capture.py` | Fixed | `test_bom_prefixed_capture_parses_every_event`, `test_bom_prefixed_manifest_parses` (+2) |
| **L4-3** | 6 | HIGH | `order_events` partitioned the stream: driver-stamped events were sorted ahead of ALL unstamped ones instead of interleaved, so a merged trace came back in an order that never happened | True order A B C D E returned as `A C E B D`. After: A B C D E | Two ordering keys of different trust levels (`_ingest_seq`, stamped by the driver; `t`, chosen by the page) were resolved by partitioning rather than by merging unstamped events into stamped anchor positions | `dom_capture.py` | Fixed — partition removed, replaced with a `bisect_right` merge over a monotonic running-max anchor list | `test_order_events_interleaves_unstamped_events_into_position`, `test_order_events_ingest_seq_still_absolutely_authoritative` (+6) |
| **L4-4** | 6 | **CRITICAL (safety)** | Org deny-list was matched by exact string on a raw alias, and one of the two entries was misspelled `ppaccenture` (missing `c`) in both `telemetry.py:152` and `replay_browser.py:17`. `PPCaccenture` was therefore never blocked by the typo'd entry, and `ppcdm`/`ppcaccenture` in any other casing bypassed both guards | `ppcaccenture`, `PPCACCENTURE`, `PpCaccenture`, `" PPCaccenture "`, `PPC-accenture` all returned `False` from `_is_org_forbidden`. `replay_browser` did not block `ppcdm`/`ppcaccenture` at all. `open_org_with_frontdoor` contacted the org via `subprocess` BEFORE refusing. After: all 15 spellings blocked, `subprocess.run` call count 0 | Exact-match comparison on unnormalised operator input, a typo shared between the implementation and its own test (so the test verified nothing), and a guard placed after the side effect it was meant to prevent | `org_denylist.py` (new), `telemetry.py`, `replay_browser.py` | Fixed | `tests/test_org_denylist.py` (52 tests), `test_ppcaccenture_typo_spelling_still_blocked`, `test_blocked_alias_never_reaches_subprocess` |
| **L4-5** | 6 | HIGH | `parse_capture_file` hardcoded `manifest=None`; `load_manifest` was written, tested, and never called from any production path — so no capture ever had a manifest to validate against and the count-mismatch and sink-error checks in `validate_trace` were unreachable | Manifest claiming 10 events beside a 6-event capture: `trace.manifest is None`, `findings == []`. After: count-mismatch and sink-error findings both raised | Loader implemented but never wired in — the same defect class as round 4's "validate_trace never called" | `dom_capture.py` | Fixed | `test_manifest_is_discovered_beside_the_capture`, `test_missing_manifest_warns_rather_than_failing` (+6) |
| **L4-6** | 6 | HIGH | Redaction-leak detector checked only `element.name` for sensitive-field patterns, so a leaked value in a field identified by `id`, `type="password"`, `aria_label`, `sf_field`, `test_id`, `label_for`, `css_path`, `xpath` or visible `text` was not flagged | 12 field-identity signals carrying the same leak: 11 missed (including `type="password"`). After: 0 missed. A pre-existing `pin` substring false positive ("Shipping", "spinner") also fixed | Leak detection keyed on ONE of the many places a field's identity appears in the schema | `dom_capture.py` | Fixed | 12-way parametrized `test_leak_detected_via_signal`, `test_no_finding_ever_echoes_the_value`, plus false-positive guards |
| **R1** | 6 | MEDIUM | Leak detector cried wolf on ordinary SLDS markup. L4-6's widening carried bare substrings `card` and `auth`, and two of the newly inspected signals (`element.classes`, `selectors.css_path`) are presentation metadata | A Case Subject field inside an `slds-card` produced `SECURITY: … [element.classes~'card', selectors.css_path~'card']`; `Author__c`, `AuthorName__c`, `Authorization_Status__c`, `Scorecard__c`, `Discard_Changes`, `Standard_Card_Layout` all fired. After: 0 false positives across those 9 cases, 0 true positives lost across 9 sensitive cases | Bare-substring patterns applied to presentation metadata. Findings are non-blocking, so the cost was alarm fatigue rather than a broken run — for a control whose entire output is an alarm, crying wolf IS the failure mode | `dom_capture.py` | Fixed (review finding on L4-6) | `test_benign_field_names_embedding_card_or_auth_do_not_false_positive`, `test_genuinely_sensitive_card_and_auth_fields_are_still_caught` |
| **R2** | 6 | **HIGH** | `dom_extractor.py:788` kept a SECOND copy of the sensitive-field pattern list. L4-6 hardened the copy in `dom_capture` and the two silently diverged by eleven patterns | For an unredacted `IBAN__c` / `Passport_No__c` / `tax_id` / `National_ID__c` / `Credential__c`, `validate_trace` correctly emitted a SECURITY finding **and then the extractor wrote the value verbatim** into `inferred_intent` (`Set IBAN__c to <value>`). After: `Set IBAN__c (redacted)`. Verified end to end that the canary reaches neither stdout, spec JSON, nor HTML | Duplicated pattern list with no test pinning the two together. Detecting a leak in one module and laundering it in the next is worse than not detecting it, because the finding asserts the control held | `dom_extractor.py` | Fixed (review finding on L4-6) — now delegates to the single shared detector | `test_extractor_uses_the_hardened_pattern_list_not_a_stale_copy` (asserts against the shared `SENSITIVE_PATTERNS`, so a private copy fails the suite) |
| **L4-7** | 6 | HIGH | Loss below the 50% fail-closed threshold reached no consumer at all; and loss via a truncated file was invisible by construction, since every line present parses cleanly and `skipped_lines` is empty | 10%/20%/30%/40% line loss each produced an EMPTY finding list and no CLI output. After: each emits `EVIDENCE INCOMPLETE:` in its own CLI block with the ratio; 50%/60% still emit fatal `DATA LOSS:`; 0% still silent | Only one loss channel was measured, only as a raw count, and only above a threshold — so "3 skipped lines" reached the operator without its denominator, or did not reach them at all | `dom_capture.py`, `pipeline.py`, `cli.py` | Fixed. Threshold NOT lowered — `_FAIL_CLOSED_LOSS_RATIO` stays 0.5 per LANE_RULES | `test_at_or_above_threshold_still_fails_closed`, `test_loss_at_the_threshold_still_aborts`, `test_truncated_capture_surfaces_a_manifest_gap` (+30) |
| **R3** | 6 | **CRITICAL (safety)** | `_verify_org_is_sandbox` read `isSandbox` from `sf org display --json`. **That key is not in that payload.** Four tests mocked `{"result": {"isSandbox": true}}` — a shape the real CLI never emits — so all four passed while the guard returned "IsSandbox field missing" for every org that has ever existed | Probed the real CLI against AFT3: the payload has no `isSandbox` at any nesting level. The guard could only ever refuse. Worse, reading it correctly would STILL have refused AFT3, which is Developer Edition with `IsSandbox=false` — so the guard needed a new predicate, not a corrected key | A mock written from the code's belief about its input rather than from the input. Identical root cause to L4-4. A safety guard that refuses everything looks safe and is untestable-by-observation: nobody notices a control that never says yes until they need it to | `telemetry.py` | Fixed — the guard now distinguishes non-production (sandbox, scratch, Developer Edition) from production, and can return yes for a real org. **It had never once returned yes before this commit** | `test_developer_edition_is_allowed_and_named_as_such`, plus the four mocks rewritten against real `sf org display --json` output |
| **R4** | 6 | **HIGH (security)** | `TelemetryRegistry` ingested whole Salesforce records and left scrubbing to whoever remembered to call `scrub_collected_telemetry`. Only `cli.py` remembered, so `pipeline.py` and any direct registry caller read unredacted org records, and `append_manual_event` bypassed the scrub entirely. Two tests asserted this gap as a known limit because `telemetry.py` belonged to another lane | Before: a key-shaped canary in a Case Description survived `collect_step` and `append_manual_event` intact when the CLI's pass was not called. After: both ingest doors scrub `before`, `after` and `payload`; 5 tests fail on the old source and pass on the new. **Regression found inside this fix:** moving the scrub to ingest made `cli.py`'s `REDACTION:` audit line go silent, because it reported from the second pass's return value and that pass correctly finds nothing on already-clean data | The boundary lived at a call site instead of on the container, so the guarantee held only for callers who knew the rule. The secondary lesson is sharper: when a control's own reporting is derived from finding something, making the control work upstream makes it look like the control stopped running. Clean data and a broken reporter are indistinguishable from the outside | `telemetry.py`, `redaction.py`, `cli.py` | Fixed (lane 06 handoff, unblocked once `pipeline_policy` reached `main`). Reported categories are now the UNION of ingest's and the second pass's; a non-empty second pass is itself the signal that ingest was bypassed | `test_registry_ingest_is_itself_a_boundary`, `test_ingest_scrub_covers_before_images_and_event_payloads`, `test_manual_event_payload_is_scrubbed_on_append`, `test_the_cli_scrub_is_still_called_and_still_reports` |

## Still open (requiring upstream or org-dependent validation)

| Item | Why Open | Mitigation | Tracked In |
|------|----------|------------|------------|
| **Live org validation** | **Partially closed 2026-07-26, not closed.** `sf agent validate authoring-bundle` HAS now run on pipeline output: it rejected the first bundle with 24 `CompilationError`s, accepted it after the emitter fix (exit 0, `{"success": true}`), and the bundle deployed to a DE org and round-tripped byte-identically. Still open: compilation is **syntax, not semantics** — no agent was published, no behaviour was checked; only one intent shape (single-topic router) on one org/CLI version was tried; `@apex.*`/`@flow.*` bundles are unemitted and unvalidated; and `sf agent generate agent-spec` has still never been run in the loop | Validation needs **no deploy** — the CLI reads the local `.agent` file and POSTs it to the compiler, using the org for auth only, so the loop is cheap and mutates nothing. Run it on every emitted bundle. Behaviour still requires publishing plus `sf agent test` | `INTERFACE_CONTRACT_ROUND3.md` § Residual risk; block-scalar row above |
| **Event Monitoring collection** | No Event Monitoring logs have ever been collected; correlation tests use hand-written mocks | The `telemetry_source="event-monitoring"` marker is wired into the scoring gate but aspirational | `INTERFACE_CONTRACT_ROUND3.md` § Residual risk |
| **Non-tautological correlation** | Correlation tests construct UI events and backend logs from the same spec, guaranteed to match | Whether genuinely-observed UI events correlate to genuinely-observed backend queries (independent data sources) is unproven | `INTERFACE_CONTRACT_ROUND3.md` § Residual risk |
| **Orphaned schemas** | 5 JSON schemas have no emitting module: `step_ledger_schema.json`, `evidence_metadata_schema.json`, `traceability_matrix_row_schema.json`, `failure_summary_schema.json`, `replay_manifest_schema.json` | Documented as "forward-looking" in `test_schemas.py`; should be deleted or moved to `docs/schemas/` as non-normative examples if never emitted | `test_schemas.py`, `INTERFACE_CONTRACT_ROUND3.md` § Residual risk |
| **Dependency pinning** | Salesforce CLI and plugins are not pinned; tested version is `@salesforce/plugin-agent` 2.143.6 but not enforced | Pin in lockfile or document tested version explicitly; uncontrolled CLI upgrade could break emitters silently | `INTERFACE_CONTRACT_ROUND3.md` § Residual risk |
| **Prod-vs-sandbox guard** | ~~No runtime enforcement~~ **Now enforced in code** — `replay_browser.BLOCKED_ORG_ALIASES` (`replay_browser.py:17`, hard-deny checked at `:126`), `_is_production_org()` (`:187`), and `telemetry._verify_org_is_sandbox()` / `_is_org_forbidden()` (`telemetry.py:152-163`, fail-closed). `SF_ALLOW_PRODUCTION_ORG=1` is an escape hatch for production but **cannot** override the PPCDM/PPCaccenture hard-deny. | Residual: the guards are unit-tested but have never run against a real org, so the `sf org display --json` parsing path is unexercised. **The bypass surface named here as "under audit" was audited in round 6 and was real — see L4-4. Case, whitespace and punctuation variants all bypassed both guards, `PPCaccenture` was misspelled in the deny-list itself, and a blocked org could be reached via username or instance URL. Now normalised in `org_denylist.py` and matched across alias/username/instanceUrl at both call sites.** | L4-4; audited by lane 04, round 6 |

## Round 6 — lane 04 (ingest hardening)

Scope: the capture-ingest path only (`dom_capture.py`, plus the org deny-list shared by
`telemetry.py` and `replay_browser.py`). Seven defects briefed, **seven fixed**, one commit each,
each with a test that fails before the fix and passes after. Measured, not asserted: every defect
was reproduced with a script before being touched and re-measured after.

* **Suite: 829 passed / 1 skipped → 979 passed / 1 skipped** (`[dev,mcp]`), +150 tests, 0 failures.
  Also 792/2 → **942/2** with `[dev]` only. The seven briefed defects accounted for 957/1; the two
  review findings below (R1, R2) added the rest.
* **Two review findings were raised against L4-6 itself and are fixed: R1 and R2 above.** L4-6's
  widening was right in direction and wrong in two details — it carried bare substrings that fire
  on ordinary Lightning markup, and it hardened only ONE of the two copies of the pattern list that
  existed in the codebase, leaving `dom_extractor.py` to launder values the validator had just
  flagged. The lesson worth carrying: widening a detector is not finished until you have measured
  its false-positive rate on ordinary input AND grepped for a second copy of whatever list you
  changed.
* **Latent, not fixed, no test:** `ExtractedAction.value` retains the raw string for sensitive
  fields even though its sibling `inferred_intent` is now redacted. Measured on this branch it
  reaches no artifact — a canary in an `IBAN__c` field appears in neither stdout, the spec JSON,
  nor the HTML, and there is no render path for the field — so there is nothing to fix today.
  Nothing structurally prevents a future consumer from rendering it. Left for whoever owns the
  redaction choke points.
* Two pre-existing tests had their **assertions changed** because they encoded a defect as the
  contract. Both are integration risks worth knowing about:
  * `test_order_events_mixed_ingest_seq_and_fallback` asserted that `ingest_seq` events sort
    first — the L4-3 partition — while its own fixture (unstamped `t=1000` before stamped
    `t=2000`) disproved it. Expectation corrected; the fixture data was not touched.
  * `test_ppaccenture_lowercase` asserted the deny-list's own typo (`ppaccenture`, missing the
    `c`). A test that shares the implementation's misspelling verifies nothing. Renamed to
    `test_ppcaccenture_lowercase` and now asserts the real spelling; the typo is kept as a
    blocked token so both spellings are refused.
* **Nothing was weakened to make anything pass.** `spec_score.PASS_THRESHOLD` is still 75,
  `markers.REAL_EXTRACTION_SOURCES` is still `{"dom-capture","cv"}`, `REAL_TELEMETRY_SOURCES` is
  still `{"live-org"}`, and the 50% data-loss abort is unchanged — now named
  `_FAIL_CLOSED_LOSS_RATIO` so any future change to it is visible in a diff.
* **Known accepted tradeoff (L4-4):** deny-list matching is containment-based on the normalised
  identifier, so a hypothetical org named `ppcdmx` or `myppcdm-sandbox` is also refused. Chosen
  deliberately — a false refusal costs an operator one message, a false permit touches an org
  that is out of scope.
* **Not fixed / carried forward:** L4-1 was fixed against the recorder source
  (`capture/recorder.js:161`, `:136`) rather than against a real captured trace, because
  `_shared/findings/lane-02.md` did not exist when this lane ran. If lane 02's real-DOM evidence
  shows the recorder emitting a shape not covered here, L4-1 needs re-checking against it.

## Test coverage summary

Any count here is a snapshot and goes stale fast. Run `pytest -q` for the current number.

As of 2026-07-26, measured by lane 04 at the end of round 6:

* **Total tests:** 979 passing, 1 skipped (`./.lanevenv/bin/python -m pytest -q`, `[dev,mcp]`);
  942 passing, 2 skipped with `[dev]` only. The extra skip in the `[dev]` run is the MCP-extra
  guard, which skips by design when the extra is absent.

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
* **Round 6 was NOT clean either.** Seven defects were found in the ingest path alone, one of
  them a safety-critical deny-list bypass (L4-4) in a guard the round-5 notes had already recorded
  as "now enforced in code". Two of the seven (L4-4's typo'd deny-list entry, L4-3's partition)
  were being actively protected by passing tests that asserted the buggy behaviour. The bar
  remains unmet, and the round-6 defects were concentrated in code that earlier rounds had
  reviewed — which says the review method, not just the code, missed them.

## Verification notes

The following fixes were independently verified by running code and observing behaviour, not 
just reading test names:

1. **A1 (redaction leak):** Ran `test_validate_trace_detects_redaction_flag_leak` and 
   confirmed it detects `value_redacted=True` with `value != None` as a finding flagged 
   "SECURITY CRITICAL". Read `dom_capture.py:403-407` to confirm the validation logic.
2. **A2 (data loss):** Ran `test_validate_trace_partial_data_loss_warns_above_threshold` and 
   confirmed it surfaces a warning when `skipped_lines / total > 0.2`. **Correction (round 6):
   the threshold in code was 0.5, not 0.2 — and A2's fix stopped at the fatal case. Loss below
   0.5 produced no finding of any kind, which L4-7 measured at 10%/20%/30%/40% and fixed. A2's
   companion test `test_validate_trace_minor_data_loss_no_warning` had encoded that silence as
   the intended contract.**
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
