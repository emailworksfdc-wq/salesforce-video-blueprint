# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the major version is `0`, the public API may change in any minor release.

## [Unreleased]

Nothing yet.

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
