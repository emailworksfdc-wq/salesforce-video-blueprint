# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the major version is `0`, the public API may change in any minor release.

## [Unreleased]

### Added — one-click capture pipeline

Recording a Salesforce process was a manual, multi-step affair: launch the
injector script by hand, remember the timestamped filename it chose, pass that
path to a separate `run` invocation, then find and run `iterate.py` separately.
Lanes B01–B09 replace those three loose ends with one integrated pipeline.

- **`sf-blueprint capture` sub-command (B02).** `capture/inject.py`'s main
  function is now wired as a first-class CLI sub-command:
  `sf-blueprint capture --org-alias <alias> --process-name <slug>`. The capture
  module is lazy-imported, so `cli.py` stays importable on machines that only
  have the base package; a missing `playwright` prints an install hint instead of
  a stack trace.

- **`--process-name` manifest field (B01).** The injector now requires a
  slug-safe process name (`case-creation`, `case-status-update`, etc.) at
  startup, validated before any side-effect runs. The name flows into:
  the output filename (`<process_name>_<timestamp>.dom_capture.jsonl`), a JSONL
  header record so the file is self-describing without the manifest, and the
  manifest JSON's `process_name` key. The manifest is also enriched with
  `sf_cli_version` and `playwright_mcp_version` so a capture can be re-examined
  alongside the tool versions that produced it.

- **Live streaming counter (B05).** During a capture session a background thread
  prints a counter line every five seconds (`[inject] 🔴 Recording — N events
  captured (M network), K errors. Elapsed: Xs`), overwriting the previous line
  in place. A final `Recording ended — N events total` line appears when the
  operator presses Enter. `live_counter_line` and `start_live_counter` are the
  new public helpers.

- **Deny-list integration in the capture layer (B04).** `assert_org_is_safe` in
  `capture/inject.py` now delegates to `org_denylist.is_org_blocked` rather than
  a bare `in BLOCKED_ORG_ALIASES` set test. The previous check missed lowercase
  spellings, username forms, and instance-URL forms of the blocked aliases. The
  function also gains a URL-pattern fallback for the `isSandbox` key missing from
  `sf org display` output since CLI 2.143.6, which was causing the guard to refuse
  legitimate Developer Edition orgs.

- **Redaction audit findings at capture time (B03).** After the operator presses
  Enter, `validate_trace()` now runs automatically on the just-written JSONL.
  `SECURITY CRITICAL` or `DATA LOSS` findings abort with exit code 1 so the
  operator sees failures immediately rather than hours later when the pipeline
  runs. `EVIDENCE INCOMPLETE` findings print as warnings. A clean trace prints a
  success line. Operators no longer need to run validation separately.

- **`selector_confidence` field (B08).** `recorder.js` now scores each captured
  event's selector quality and writes it to the JSONL:
  `1.0` when both `role` and `name` are present (deterministic replay);
  `0.5` for role-only or a stable `data-id`/`testid` attribute;
  `0.1` for `null`/`null` (bare LWC shadow element, CSS-path only).
  A `selector_fallback` field records the best non-null alternative identifier
  (`aria-label > data-id > innerText[:40] > null`). `RawSelectors` in
  `dom_capture.py` gains both as optional fields so pre-B08 captures still parse
  without errors.

- **`sf-blueprint run --last-capture` (B06).** After recording, the operator no
  longer needs to look up the timestamped filename. `--last-capture` scans
  `--capture-dir` (default `./outputs/capture`) for the most recently modified
  `*.dom_capture.jsonl` and prints which file it chose alongside its event count
  from the companion manifest. When both `--last-capture` and `--capture` are
  supplied, `--capture` wins and a warning is printed.

The full one-click flow is now:

```bash
sf-blueprint capture --org-alias <alias> --process-name <slug>
sf-blueprint run --last-capture --org-url "https://your-org.my.salesforce.com"
sf-blueprint iterate --spec outputs/capture/<slug_timestamp>.agent-spec.json
```

### Fixed — the score gate was scoring the wrong things

The gate is the component that decides whether a derived spec is fit to become an
agent, so a defect here is worse than a defect anywhere else: it grants
permission. Eleven of them were found by attacking it rather than testing it; the
ones worth a reader's attention are below.

- **Filler instructions scored 100/100.** A spec whose `orchestration_steps` were
  `["aa", "bb", "ee"]` and whose `guardrails` were `["cc", "ff"]` scored a perfect
  100 with all seven dimensions at full marks, outranking the real spec derived
  from the bundled example capture (84/100 at the time). The hollowed-out part was
  the *narrative*, not the metadata: the attack's entities were deliberately
  concrete and well-evidenced, which is exactly why it worked — entity metadata is
  expensive to fabricate, prose is free, and every specificity check was a
  blocklist of known-bad phrases that text saying nothing cannot trip. Instruction
  text now has a length and word floor, a mostly-filler spec is blocked rather
  than docked, and the narrative must name a field or entity the metadata claims
  was observed. A second variant that defeated the floor with fluent-but-empty
  prose (`["do the thing here", "then do it again"]`, 92/100, above real output's
  90) is closed by the same coupling rule.
- **Deleting an observed entity paid +15.** Removing evidence *raised* the score,
  which is the one gradient a refinement loop must never be able to learn — and
  `_score_evidence_grounding`'s own docstring claimed it could not happen.
  Deletion can no longer profit; a guard test asserts it per dimension.
- **The gate accused its own builder of padding on a real capture.** On a real
  recorded Salesforce session, 128 of 130 observed inputs were Lightning UI
  elements whose object/field could not be resolved, so every one collapsed into
  a single `None.None` bucket that looked like the same field repeated. That cut
  `evidence_grounding` to 5/30 and fired a false threshold-surfing block. The
  padding heuristic fired *only* on honest output — synthetic filler sailed past
  it. Unresolved targets are no longer collated as one field.
- **`honesty` was unfalsifiable.** It returned 20/20 on every spec that reached
  it, including the filler ones, so a fifth of the total was a constant —
  `confidence` scored the same at `0.0`, `0.3`, `0.7` and `1.0`. Overclaiming past
  the deriver's own `0.7` ceiling now costs half the dimension. This is a
  deduction, not a blocker: an otherwise well-formed spec claiming `1.0` still
  passes at 82, so treat it as mitigated rather than closed.
- **Concealing a gap paid 8 points.** Declaring an honestly-labelled `inference`
  entity *lowered* `evidence_grounding` from 30/30 to 22/30, because the coverage
  bonus was a ratio and the declaration diluted its denominator. That is the
  inversion `_score_honesty`'s docstring calls the worst possible outcome —
  training the loop to hide what it does not know — reintroduced through a
  different dimension's arithmetic. Declaring an unknown can no longer cost points
  in any dimension.
- **Hollowing out exactly one dimension passed the gate.** The threshold-surfing
  check required two or more dimensions at or below 50%, so zeroing a single one
  was free: `specificity` at 0/10 scored 82 and passed, and at 1/10 landed on
  exactly 75 and passed. A dimension at or below 10% of its weight now blocks,
  excepting the two that can honestly be zero.
- **A blocked run displayed a near-passing number.** `ci_smoke_check.py` printed
  `85/100` three lines above its own `BLOCKED:` line. `SpecScore` now carries
  both `total` (the raw dimension sum, unchanged, so `iterate.py` keeps a usable
  gradient across blocked rounds) and `display_total` (capped below the moderate
  band whenever a blocking issue is present). `summary()` reports the capped
  figure and discloses the raw one — e.g.
  `FAIL: 59/100 (low band), 1 blocking issue(s) [blocked: capped from raw 85]`.
- Four smaller calibration defects are fixed in the same changes: an empty
  evidence trail scored 95/100 (100 while asserting real provenance) and is now
  blocked; the placeholder-detail floor was one character wide against a builder
  whose shortest real detail is 41, and is now 12; `specificity` silently docked
  the deriver's own honest closing step, so every deduction now emits a finding
  explaining itself; and `testability` credited entities only when object *and*
  field resolved, which honest UI-input entities never do — deleting three of them
  from the example used to pay +5 and now costs −7.

`PASS_THRESHOLD` is unchanged at 75 and `markers.py` is untouched — no fix here
works by lowering a bar, and `test_gate_constants_are_unchanged` asserts both
marker sets at runtime so a weakening edit fails a test before it can weaken a
run. `tests/test_gaming_resistance.py` and the new
`tests/test_score_calibration.py` (35 tests) fail if any of these regress.

### Added — `telemetry_source: live-org` is earnable for the first time

Until now the score gate required `telemetry_source` to be `live-org` and the
pipeline could only ever produce `mock`, so no spec this project derived could
pass its own gate. That is no longer structurally true.

- **`live_telemetry.LiveOrgTelemetryCollector`** reads real Salesforce field
  history over `sf data query` and satisfies the same `TelemetryCollector`
  interface as the mock, plus a run-scoped `observe(run_id)`.
- **The stamp is derived from observed rows, never from a caller's assertion.** A
  live collector pointed at an org that returned nothing is stamped
  `"unavailable"`, not `"live-org"`, because evidence-wise it is indistinguishable
  from having no org at all. `unavailable` is absent from
  `markers.REAL_TELEMETRY_SOURCES`, so it blocks at the gate. Verified
  end-to-end: an empty live collector yields `unavailable` and `passed=False`.
- **Correlation on real telemetry is `TEMPORAL`, never `HIGH`.** The mock earns
  `HIGH` only by re-reading the `step_id` the caller handed it — a tautology, the
  join re-confirms an assertion. Real history rows carry no `step_id`, so events
  are stamped with the module constant `UNATTRIBUTED_STEP_ID`, deliberately not a
  parameter, so no caller can manufacture a causal claim. This costs nothing:
  `TEMPORAL` still maps to the strong `data-delta` evidence source.

Two defects that only a real run could expose:

- **Temporal correlation had never once matched a real observation.**
  `dom_extractor` writes `timestamp_ms` relative to the first event; `correlation`
  reads it as absolute epoch ms. Under the relative reading every capture's first
  action sits at 1970-01-01, so no real org timestamp can fall inside any
  `[T, T+5s]` window and real telemetry was silently dropped. It stayed hidden
  because the mock always matched on the caller-asserted `step_id` instead,
  reporting 1,785,100,742 seconds of "clock skew". Corrected at the pipeline's own
  join site using the absolute instant already carried by
  `EvidenceArtifact.captured_at`, so nothing is inferred.
- **One real org change was multiplied into nine, then dismissed as ambiguous.**
  A per-step collector interface suits something that fabricates an event on
  demand; an org is not like that — what happened happened once. Driving the
  collector per action turned a single `Case.Status` change into 9 identical
  snapshots, and correlation demoted all of them to `AMBIGUOUS`, grading the field
  `inference` instead of `data-delta`. Run-scoped collectors are now called
  exactly once.

Measured against AFT3 (a Developer Edition org, not production): a real Case was
created, its Status changed through the Lightning UI, and the resulting history row
correlated back to the click that caused it. Combined effect of the two fixes on
the real spec: 56/100 → 65/100, `objects_touched` `[]` → `['Case']`.

**The real spec still does not pass the gate: 65/100, band `low`.** One small edit
is thin evidence and the scorer says so — `evidence_grounding` 6/30,
`testability` 0/10. The route to 75 is a richer capture, not a scoring change.
`PASS_THRESHOLD` is still 75 and `markers.py` is untouched.

Also measured and worth knowing before relying on it: `EventLogFile`, `ApexLog`,
`AsyncApexJob` and `FlowInterview` are all queryable but return **zero rows** on
Developer Edition, so a `describe`-based capability probe reports "supported" for
surfaces that can never yield evidence. Field history and `SetupAuditTrail` are the
only two that returned rows. Only `Case` history was verified; the other objects
are mapped by naming convention and marked as such in the code.

### Added — stage 5: a refinement loop that learns from a real agent

`iterate.refine` re-scores the same spec offline and calls it converged after three
identical scores, which says only that the offline scorer stopped changing its mind.
`stage5` plus `iterate.refine_with_org_feedback` close that loop against a live
Agentforce agent: emit a test spec, run it in the org, parse the real per-case
verdicts, fold them back in as *added* observations, re-score with the existing gate
unchanged.

Run for real against `Coral_Cloud_Booking_Agent` in AFT3 with no injected runner. The
agent routed every case to `Booked_Activity_Management` rather than the derived
`Update_Case_Status` — real evidence that the derived spec does not match that agent,
which is exactly what an offline loop can never learn.

- **`sf agent test run-eval` accepts the legacy `AiEvaluationDefinition` dialect
  only.** Measured, and root-caused in the CLI's own `yamlSpecTranslator.js`: it reads
  only top-level legacy keys (`utterance`, `expectedTopic`, `expectedOutcome`). An NGT
  file puts its utterance at `inputs[].utterance`, so the translator never sees it and
  the server rejects the payload. `select_dialect_for_run_eval()` refuses the wrong
  dialect locally instead of paying for a round trip to a 422.
- **Feeding an NGT spec to the default `testing-center` runner silently emits a hollow
  test definition** — five empty `<inputs></inputs>`, zero utterances, 5 expected
  values against 14 for the legacy spec. The reverse direction fails loudly. A test
  definition that looks like it worked and verifies nothing is worse than a rejection.

Two defects in this feature were found by review before it landed, both mutation-tested:

- **An injected test runner produced feedback stamped `run-eval` — i.e. real.** Anyone
  with a fixture and a one-line lambda could have manufactured a `round.json` claiming
  `trustworthy: true` with no org involved, byte-indistinguishable from a genuine
  round. Provenance now follows *who produced the bytes*: an injected runner is stamped
  `injected-runner`, which is not in `REAL_FEEDBACK_SOURCES` and fails closed.
  Verified — `injected-runner`, `mock` and an arbitrary source are all blocked, and
  `run-eval` with zero cases is blocked too, so an empty result cannot pass as
  validation.
- **The no-overwrite guard ran after the damage.** It fired inside `write_round`, i.e.
  after the round's `testSpec.yaml` had been overwritten and after the org had been
  billed for real LLM calls, leaving `round-N/` holding a spec that no longer matched
  its `round.json`. A half-replaced audit trail is worse than a refused one;
  `assert_round_unwritten()` now runs before any write and before the subprocess.

Also fixed: `is_pass=bool(...)` read the string `"false"` as a **pass** — the single
field that decides pass/fail was the only one coerced fail-open. It is now an `is True`
identity check; verified that `False`, `"false"`, `"FALSE"`, `"FAILED"`, `"true"`, `1`
and `None` all read as failures and only literal `True` passes. A bare `assert`
guarding the confidence invariant was replaced (`python -O` strips asserts), and a
`JSONDecodeError` no longer escapes while discarding the output that failed to parse.

**Disclosed rather than "fixed": a failing round can make the score go up.** The rubric
awards honesty points for declaring unknowns, and a failed case adds one — so the spec
really did get more honest, but a reader diffing `score_before` against `score_after`
would read the rise as the agent improving. A round with failures whose score rose now
carries an explicit note. The rubric is untouched.
### Added — the first real Salesforce capture in the repository

Every fixture in `examples/` until now was written by hand to exercise a code
path. `examples/case_creation_aft3.dom_capture.jsonl` is the opposite: 175 events
recorded off a live Developer Edition org while a Case was created and its Status
changed, committed with a manifest (`.dom_capture.manifest.json`) that records the
recorder SHA, the browser version, and exactly what was redacted. Org host, org
id, username and record ids are synthetic; DOM structure, event order, shadow
depths and — the part that matters — the null selectors are unmodified.

Hand-written fixtures cannot fail in the ways real Lightning DOM does, which is
why two defects fixed earlier in this release had never been confirmed against
anything real. Measured on this artifact:

- **Ingest accepts all of it: 175/175 events parsed, 0 lines skipped,
  `validate_trace()` returns `[]`.** Before the nullable `role`/`name` fix it
  parsed 4 of 175 and the pipeline rejected the run outright as 98% data loss.
  The capture still carries 170+ null role/name pairs, so it keeps proving what it
  was recorded to prove — the fix did not come from sanding the input down.
- **`evidence_grounding` scores 25/30, not the 5/30 the false padding detector
  produced, and no `PADDING` finding fires.** That is the `None.None` collapse
  confirmed dead on the data that exposed it rather than on a reconstruction.

`test_c11_on_lane_02_real_capture_when_available` consequently runs instead of
skipping, taking the suite from two expected skips to one. A skip retired by
supplying the evidence it was waiting for, not by relaxing what it asserts.

**The real spec still does not pass: 59/100 displayed (85 raw), band `low`.** The
sole blocking issue is `telemetry_source` — this capture carries no live-org
telemetry, so the gate refuses it, exactly as designed. Nothing in `markers.py`
or `PASS_THRESHOLD` was touched to accommodate the artifact.

A wrongly-typed selector is still malformed: `role=None` is accepted, `role=123`,
`["button"]`, `{"role": "button"}`, `1.5` and `True` all still raise. Accepting
absent data is not the same as accepting garbage, and a test pins the difference.

### Known gap

`telemetry._verify_org_is_sandbox` cannot succeed as written: `sf org display
--json` no longer returns an `isSandbox` key (CLI 2.143.6), so the check fails
closed on every org including permitted dev ones. Anything treating it as a
production guard is relying on nothing. The alias deny-list in `org_denylist` is
the control that actually holds.

Salesforce truncates field-history timestamps to whole seconds, so a genuine
cause/effect pair can have the org's timestamp up to 999 ms *before* the click
that caused it — outside `correlation.py`'s forward-only `[T, T+5s]` window.
Whether real telemetry correlates therefore depends on where in the second the
user clicked. Asserted by tests rather than fixed: widening a *causal* window
backwards is a semantic decision about what may be claimed as caused.

*(Resolved in this release — see "the first real Salesforce capture" above.
`test_c11_on_lane_02_real_capture_when_available` now runs against the committed
artifact instead of skipping.)*

## [0.1.1] — 2026-07-26

A release whose main job is to be installable. `v0.1.0` cannot be installed on
Python 3.12 or 3.13 at all — see **Fixed** below — so every consumer of the
latest tag was blocked. Also ships the MCP server and the first output of this
project that Salesforce has ever accepted.

### Added

- **MCP server** (`src/sf_video_blueprint/mcp_server.py`), installed as
  `sf-blueprint-mcp` via a new `[mcp]` optional extra. Speaks stdio, so it works
  with Claude Code, Claude Desktop, Cursor, Windsurf, Continue, or any other
  MCP-capable harness. Seven tools: `health`, `validate_capture`, `derive_spec`,
  `score_spec`, `emit_agent_bundle`, `emit_test_spec`, `preview_api_names`.
  Every tool is offline and read-only — none contacts a Salesforce org or launches
  a browser, so an agent driving this server cannot mutate an org through it.
- `pipeline.py` — the shared in-process API (`run_pipeline`, `PipelineResult`,
  `CaptureRejected`) that the CLI, library, and MCP server all call. Previously
  assembling the pipeline took seven imports and a dozen lines of glue, which
  coupled every consumer to internal module layout.
- A real public API in `__init__.py`. `from sf_video_blueprint import run_pipeline`
  now works; before, the package exported nothing and `dir()` returned `[]`.
- `scripts/mcp_stdio_check.py` — CI gate that launches the installed executable
  and drives it over real stdio JSON-RPC, asserting over the wire that a spec
  built from mock telemetry is still refused by the score gate.
- `docs/mcp-install.md` — install and per-harness configuration.
- `scripts/roundtrip_lib.py` — single source of every name the round trip uses,
  derived from `naming.py`. Also the single `DerivedAgentSpec` JSON parser, which
  `agentforce_roundtrip.sh` previously carried three copies of in shell heredocs.
- `tests/test_roundtrip_lib.py` — 25 tests, including regression guards that
  reject the exact three-name triple the round-trip script used to carry and an
  end-to-end check that an offline run refuses to claim org validation.
- `scripts/roundtrip_check.py` — CI gate that runs the round trip with no org and
  then reads the *summary* rather than the exit code, asserting that the skipped
  org stage is reported as skipped and that every artifact still names one agent.
- A "Use it in your project" README section covering all three consumption modes.
- CI: a `mcp-server` job, plus a second `pytest` run **without** the `mcp` extra to
  prove the new tests skip rather than fail when the optional dependency is absent.
- CI: a `roundtrip` job that runs `scripts/agentforce_roundtrip.sh` end to end with
  no org. It can only be a CI job now that the script's org calls are opt-in.

### Changed

- `NoopUIAdapter` moved from `cli.py` to `replay.py`, and `MockTelemetryCollector`
  from `cli.py` to `telemetry.py`. Importing them from `cli.py` dragged in `typer`,
  which made the pipeline unimportable in a minimal environment. Both now carry
  docstrings explaining that they fabricate their output and why the score gate
  therefore refuses runs that use them.
- `eval_spec.render_test_spec()` added so YAML can be produced in-process;
  `write_test_spec()` now delegates to it. Previously the only way to see the YAML
  was to write a file.
- `docs/mcp-product-spec.md` and `docs/mcp-release-checklist.md` no longer claim
  "NOT IMPLEMENTED". Each section now carries its own status, and the reasons the
  shipped server deviates from the `workflow.*` design are recorded in both.

### Fixed

- **`v0.1.0` could not be installed on Python 3.12 or 3.13.** `playwright~=1.46.0`
  resolves to `playwright 1.46.0`, which pins `greenlet==3.0.3`; that greenlet has
  no cp313 wheel, so pip builds it from source and the C extension fails against
  the 3.12+ C API (`_PyCFrame` and `PyThreadState.trash` were removed):

  ```
  src/greenlet/greenlet_greenlet.hpp:104:9: error: unknown type name '_PyCFrame'
  error: command '/usr/bin/clang++' failed with exit code 1
  ERROR: Failed building wheel for greenlet
  ```

  Fixed in `ca6b0b8` by replacing compatible-release pins with lower bounds plus a
  major-version ceiling, floored at versions that build on 3.13. That fix landed
  on `main` after `v0.1.0` was tagged, which is why this release exists.
- `SyntaxWarning: invalid escape sequence '\d'` from two docstrings in
  `selectors.py`, which will become a `SyntaxError` in a future Python.
- **`scripts/agentforce_roundtrip.sh` referred to three different agent names in a
  single run** (`test_agent` in the `.agent` config, `RoundtripTestAgent` in the
  CLI flags, `TestAgent` in both test specs), so the emitted test suite targeted an
  agent no stage had produced and the round trip could never have completed. Every
  name is now derived from `naming.py`; the script spells none itself. The round
  trip does now complete: `--org <dev-org>` reaches
  `sf agent validate authoring-bundle` and exits 0 with `{"success": true}`.
- The same script printed `All executed stages PASSED` and wrote
  `{"pass": true}` while both org-dependent stages were skipped. Skipped stages are
  now reported as `SKIPPED`, the summary records `salesforce_validated`, and the
  single ambiguous `pass` boolean is gone.
- The script's org stages were controlled by `DRY_RUN=1`, i.e. org calls were the
  default and required a positional org alias to run at all. It now runs offline by
  default and org work is opt-in behind `--org <alias>`.
- `roundtrip_lib.derive_identity` folded its `SFVB TEST` org-artifact prefix into
  the intent before truncation, so the prefix competed with the intent for one
  length budget and won: both `"A" * 90` and `"!!!"` derived the bare name
  `SFVB_TEST`, and two unrelated recordings collided on one agent.
  `naming.prefixed_api_name` now holds the prefix out of the budget. Note that
  `assert_coherent` *passed* on the colliding name — every dialect agreed, on a
  name that identified no recording.

### Validated against a real Salesforce org — a first for this project

On 2026-07-26, `sf agent validate authoring-bundle` was run against output of
this pipeline for the first time. Read the scope carefully; it is narrow.

- **What passed.** One bundle, API name `SFVB_TEST_Case_Triage`, derived from
  `examples/case_triage.dom_capture.jsonl` and validated against a Developer
  Edition org: `{"status": 0, "result": {"success": true}}`.
- **What failed first.** The initial emitted bundle was **rejected with 24
  `CompilationError`s**, all in the derived subagent's `reasoning:` block. A bare
  `->` on its own line is not legal Agent Script — `->` is only valid as the value
  of a key, so `instructions: ->` compiles and a standalone `->` does not.
- **The emitter fix is in this version.** `_block_scalar` now emits
  `instructions: ->` with the `|` lines indented one level deeper, and the bundle
  built from this release's emitter is the one the compiler accepted. It also
  **deployed** to the org as `AiAuthoringBundle` metadata and round-tripped
  byte-identically through deploy → retrieve.
- **What this still does not license.** One bundle, one intent shape
  (single-topic router), one org, one CLI version (`@salesforce/cli 2.143.6`,
  `@salesforce/agents 1.6.6`). Compilation is **syntax, not semantics**: no agent
  has been published and nothing has checked how a compiled agent behaves. Bundles
  carrying `@apex.*`/`@flow.*` actions are never emitted, so they are unvalidated.
  `[NEEDS EVIDENCE: …]` markers compile successfully — the compiler is not a
  safety net for evidence quality.
- **`validate_locally()` is blind to this error class.** It reported zero findings
  on the exact file the compiler rejected, both before and after the fix. A clean
  local validation is not evidence of deployability.
- **The subagent name cap is now measured, not assumed.** 80 characters passes and
  81 fails with `Too big: expected string to have <=80 characters`. Router action
  names are not length-checked at all (a 100-char action compiled), which retracts
  a previously recorded "router-action overflow" defect. `MAX_NAME_LENGTH` stays at
  74 as deliberate headroom; the *metadata* channel's limit remains unmeasured.
- **The docs premise about needing a deploy first was wrong.** The command
  resolves the bundle from the *local* SFDX project and POSTs the file content to
  the authoring compile endpoint; the org connection is used for auth only. A
  purely local `.agent` file can be validated with no deploy at all.

### Planned

- Teach `validate_locally()` the two grammar rules the compiler taught us, so a
  bare `->` opener and same-column `|` lines fail locally instead of only in an
  org. It reported zero findings on the file Salesforce rejected with 24 errors.
- Broaden org validation beyond the single case: another intent shape, multi-topic
  specs, bundles carrying `@apex.*`/`@flow.*` actions, and the name limit on the
  *metadata* path. One passing bundle confirms one grammar path, not the emitter.
- Make validation a step this repo runs itself rather than a manual CLI call
  alongside it — it needs no deploy, so it is cheap enough for CI.
- Publish an agent and run `sf agent test` against it, so something checks
  behaviour rather than only syntax.
- Fix the four ingest defects that allow a capture to be silently truncated
  while still being stamped as real evidence (see `docs/DEFECT_LEDGER.md`).
- Wire `sf agent test create/run/results` so stage 5 (iterate) exists.
- Call `redaction.py` from the pipeline instead of leaving it unreferenced.
- Publish to PyPI. Until then the package installs from the git URL only, and the
  `sf-video-blueprint` name is unclaimed.

## [0.1.0] — 2026-07-26

First tagged release. A working offline pipeline with unvalidated output; see
the status table in the README for an honest per-stage grade (~55% of the stated
end goal).

### Added

**Capture and ingest**

- DOM click recorder (`capture/recorder.js`) and Playwright injector
  (`capture/inject.py`) that record clicks, inputs, and Salesforce page context
  to JSONL.
- `dom_capture.py` — the untrusted-input boundary. Parses and validates capture
  traces, preserves driver-stamped ordering metadata, detects redaction leaks,
  and never aborts on a single malformed line.
- `dom_extractor.py` — raw events to replayable actions, with noise coalescing
  (consecutive inputs, event bubbling, scroll/keydown) and synthetic navigate
  insertion.
- `selectors.py` — selector strategy tiering, preferring stable identifiers
  (`test_id`, `aria`, `role_name`) over brittle ones (`css_path`, `xpath`).
- JSON schemas for the capture wire format and the emitted agent spec
  (`schemas/`).

**Derivation**

- `correlation.py` — joins UI steps to backend telemetry within a 5-second
  forward window, with an explicit confidence enum
  (`HIGH` / `TEMPORAL` / `ASSERTED` / `AMBIGUOUS`) rather than an implied causal
  claim.
- `spec_builder.py` — derives intent, entities, orchestration steps, guardrails,
  and failure handling from correlated evidence. Carries per-field provenance and
  declares gaps instead of filling them.
- `naming.py` — single source of truth for API names, keeping the topic API name,
  test `expectedTopic`, subagent reference, and router action mutually
  consistent across every emitted artifact.

**Scoring**

- `spec_score.py` — falsifiable quality gate across seven weighted dimensions
  (evidence grounding 30, honesty 20, completeness 15, specificity 10,
  testability 10, placeholder freedom 10, provenance integrity 5), threshold 75.
- Threshold-surfing detection: a spec that scrapes past the total while leaving
  dimensions near zero is flagged rather than passed.
- `markers.py` — the provenance vocabulary. Only `dom-capture` and `cv` count as
  real extraction; only `live-org` counts as real telemetry. Everything else is
  capped and blocked.
- `tests/test_gaming_resistance.py` — tests that specifically fail if the gate is
  weakened.

**Agentforce bridge**

- `agent_script.py` — `.agent` (Agent Script) and `.bundle-meta.xml` emitters,
  with local validation.
- `agentforce_spec.py` — Agentforce agent-spec YAML emitter.
- `eval_spec.py` — test-spec emitters for both the legacy
  `AiEvaluationDefinition` and the newer `AiTestingDefinition` dialects.
- `iterate.py` — versioned offline refinement loop. Every round is written to its
  own directory; overwriting a prior version is forbidden because the audit trail
  is the product.

**Safety**

- Fail-closed production-org guard in `replay_browser.py`: when org type cannot
  be determined, replay refuses rather than assuming safety.
- Two org aliases hard-blocked with no override path, in both the replay and
  telemetry layers.
- URL redaction for `sid`, `access_token`, and session parameters before anything
  is written to an audit artifact.
- `redaction.py` — secret and PII redaction primitives (implemented and tested;
  **not yet called from the pipeline**).

**Reporting and docs**

- `html_report.py` — HTML blueprint with explicit provenance labelling and a
  simulated-data warning that cannot be suppressed.
- `docs/USER_JOURNEY_Story.html` — self-contained animated six-act walkthrough of
  how an operator uses the framework, including where it falls short.
- `docs/DEFECT_LEDGER.md` — every known defect with file and line.
- `docs/INTERFACE_CONTRACT.md` — the recorder-to-parser wire format.
- `examples/case_triage.dom_capture.jsonl` — synthetic example capture so the
  pipeline can be run end to end with no org.

**Project infrastructure**

- Apache-2.0 license, contributing guide, PR and issue templates, `CODEOWNERS`.
- GitHub Actions CI running the suite on Python 3.11, 3.12, and 3.13.
- Dependabot for `pip` and `github-actions`.

### Known limitations

Carried forward deliberately, not overlooked:

- **No real-org validation had occurred as of this release.** `sf agent validate
  authoring-bundle` had not been run against any output. The emitted bundle may be
  invalid. *(First validated on 2026-07-26 — see `[0.1.1]`, and note that the
  bundle only compiled after an emitter fix.)*
- Stage 5 (run the spec repeatedly to improve it) does not exist. The offline
  loop reports `converged=true` after three identical scores.
- Stage 6 emitters have no production call site.
- Capture ingest can silently discard events: strict `role`/`name` requirements
  drop legitimate events, loss below 50% is never reported, the manifest
  cross-check is disabled, and a UTF-8 BOM eats the first event.
- The leak detector inspects only one of the eight field-identity signals the
  recorder captures.
- `scripts/agentforce_roundtrip.sh` refers to three different agent names in a
  single run. *(Fixed in Unreleased.)*
- Video extraction is a stub: `HeuristicVideoExtractor` never decodes video and
  returns one placeholder step for any input. Use `--capture`.
- No MCP server exists, despite `docs/mcp-product-spec.md`. *(Added in
  Unreleased.)*
- HTML output embeds record IDs and field values verbatim. Treat every artifact
  in `outputs/` as sensitive.

[Unreleased]: https://github.com/emailworksfdc-wq/salesforce-video-blueprint/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/emailworksfdc-wq/salesforce-video-blueprint/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/emailworksfdc-wq/salesforce-video-blueprint/releases/tag/v0.1.0
