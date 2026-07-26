# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the major version is `0`, the public API may change in any minor release.

## [Unreleased]

### Planned

- Validate an emitted bundle with `sf agent validate authoring-bundle` against a
  dev org. Until this runs, `.agent` grammar correctness is unverified.
- Fix the four ingest defects that allow a capture to be silently truncated
  while still being stamped as real evidence (see `docs/DEFECT_LEDGER.md`).
- Wire `sf agent test create/run/results` so stage 5 (iterate) exists.
- Give the stage 6 emitters a production call site.
- Call `redaction.py` from the pipeline instead of leaving it unreferenced.

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

- **No real-org validation has ever occurred.** `sf agent validate
  authoring-bundle` has never been run. The emitted bundle may be invalid.
- Stage 5 (run the spec repeatedly to improve it) does not exist. The offline
  loop reports `converged=true` after three identical scores.
- Stage 6 emitters have no production call site.
- Capture ingest can silently discard events: strict `role`/`name` requirements
  drop legitimate events, loss below 50% is never reported, the manifest
  cross-check is disabled, and a UTF-8 BOM eats the first event.
- The leak detector inspects only one of the eight field-identity signals the
  recorder captures.
- `scripts/agentforce_roundtrip.sh` refers to three different agent names in a
  single run.
- Video extraction is a stub: `HeuristicVideoExtractor` never decodes video and
  returns one placeholder step for any input. Use `--capture`.
- No MCP server exists, despite `docs/mcp-product-spec.md`.
- HTML output embeds record IDs and field values verbatim. Treat every artifact
  in `outputs/` as sensitive.

[Unreleased]: https://github.com/emailworksfdc-wq/salesforce-video-blueprint/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/emailworksfdc-wq/salesforce-video-blueprint/releases/tag/v0.1.0
