"""Adversarial tests for spec_score.py (offline spec scorer).

These tests verify that the scorer:
10. THE SCORER MUST BE ABLE TO FAIL (can distinguish good from bad)
11. THE HONESTY ASYMMETRY (high confidence + gaps < low confidence + declared unknowns)
12. Provenance hard-cap (stub/mock sources block top-band)
13. Blocking issues override numeric score
14. Determinism and purity (no mutation, same input -> same output)
15. compare/converged works correctly
16. Placeholder scanning catches all markers from score_run.py
17. Weights sum to 100, no dimension can exceed its max
"""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import pytest

from sf_video_blueprint.spec_score import (
    DIMENSION_WEIGHTS,
    PASS_THRESHOLD,
    PLACEHOLDER_MARKERS,
    DimensionScore,
    SpecScore,
    SpecComparison,
    score_spec,
    score_spec_file,
    compare,
)
from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence


# --- Fixture: minimal DerivedAgentSpec ---

def _make_spec(
    intent: str = "Update Case Status",
    confidence: float = 0.75,
    objects_touched: list[str] | None = None,
    entities: list[DerivedEntity] | None = None,
    orchestration_steps: list[str] | None = None,
    guardrails: list[str] | None = None,
    failure_handling: list[str] | None = None,
    unknowns: list[str] | None = None,
    evidence: list[SpecEvidence] | None = None,
) -> DerivedAgentSpec:
    """Build a minimal DerivedAgentSpec for testing."""
    if objects_touched is None:
        objects_touched = ["Case"]
    if entities is None:
        entities = [
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "recordId required")],
            ),
        ]
    if orchestration_steps is None:
        orchestration_steps = [
            "Resolve and load the target Case record",
            "SUBMIT on button:Save -> writes Status",
        ]
    if guardrails is None:
        guardrails = ["Require explicit user confirmation before writing: Status."]
    if failure_handling is None:
        failure_handling = ["Observed validation failure during recording: Status must be one of approved values"]
    if unknowns is None:
        unknowns = []
    if evidence is None:
        evidence = [SpecEvidence("telemetry", "validation observed"), SpecEvidence("data-delta", "Case mutated")]

    return DerivedAgentSpec(
        intent=intent,
        confidence=confidence,
        objects_touched=objects_touched,
        entities=entities,
        orchestration_steps=orchestration_steps,
        guardrails=guardrails,
        failure_handling=failure_handling,
        unknowns=unknowns,
        evidence=evidence,
    )


# === TEST 10: THE SCORER MUST BE ABLE TO FAIL ===

def test_scorer_can_fail():
    """The scorer must distinguish a good spec from a bad one and fail the bad one."""
    # Good spec: concrete intent, observed data, explicit unknowns at reasonable confidence
    good_spec = _make_spec(
        intent="Update Case Status field to 'Working'",
        confidence=0.75,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status changed 'New' -> 'Working' at step 3")],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("data-delta", "Case.Id observed in telemetry: 500xx0000012345")],
            ),
        ],
        orchestration_steps=[
            "Resolve and load the target Case record",
            "SUBMIT on button:Save -> writes Status",
            "Return confirmation",
        ],
        guardrails=["Enforce FLS on Case", "Require confirmation"],
        failure_handling=["Observed validation failure during recording: Status must be approved value"],
        unknowns=[],
    )

    # Bad spec: UNRESOLVED intent, no objects, no entities, high confidence (dishonest), placeholder
    bad_spec = _make_spec(
        intent="UNRESOLVED: recording did not demonstrate a completed business action",
        confidence=0.85,  # Dishonestly high
        objects_touched=[],
        entities=[],
        orchestration_steps=["button:Save"],  # placeholder
        guardrails=[],
        failure_handling=["No failures were observed in this run, so error paths are UNTESTED."],
        unknowns=[],  # Should have declared unknowns but didn't
    )

    good_score = score_spec(good_spec)
    bad_score = score_spec(bad_spec)

    assert good_score.total > bad_score.total, (
        f"Falsifiability check FAILED: good spec scored {good_score.total}, "
        f"bad spec scored {bad_score.total}. The scorer cannot distinguish quality."
    )
    assert bad_score.total < PASS_THRESHOLD, (
        f"Falsifiability check FAILED: bad spec scored {bad_score.total}, "
        f"which is >= PASS_THRESHOLD={PASS_THRESHOLD}. The scorer is too lenient."
    )
    assert not bad_score.passed, "Falsifiability check FAILED: bad spec passed when it should have failed."
    assert good_score.passed, "Falsifiability check FAILED: good spec failed when it should have passed."


# === TEST 11: THE HONESTY ASYMMETRY ===

def test_honesty_asymmetry():
    """CRITICAL: high confidence + structural gaps < low confidence + declared unknowns.

    This is the single most important behavioural test. If this fails, the refinement
    loop will train the spec generator to DELETE honest caveats to raise its score.
    """
    # Dishonest spec: high confidence but structural gaps (no objects, no entities)
    dishonest_spec = _make_spec(
        intent="Update Case Status",
        confidence=0.85,  # HIGH
        objects_touched=[],  # GAP
        entities=[],  # GAP
        orchestration_steps=["Do something"],
        guardrails=[],
        failure_handling=["Unknown"],
        unknowns=[],  # No declared unknowns despite gaps
    )

    # Honest spec: low confidence, declares unknowns, but has some structure
    honest_spec = _make_spec(
        intent="Update Case Status",
        confidence=0.35,  # LOW
        objects_touched=[],  # Gap, but declared below
        entities=[],  # Gap, but declared below
        orchestration_steps=["Do something"],
        guardrails=[],
        failure_handling=["Unknown"],
        unknowns=[
            "No record-level data change was observed, so the target object is unknown.",
            "No input entities could be derived.",
        ],
    )

    dishonest_score = score_spec(dishonest_spec)
    honest_score = score_spec(honest_spec)

    # The honest spec MUST score higher on the honesty dimension
    dishonest_honesty = dishonest_score.dimensions["honesty"].score
    honest_honesty = honest_score.dimensions["honesty"].score

    assert honest_honesty > dishonest_honesty, (
        f"HONESTY ASYMMETRY FAILED: dishonest spec (high conf + gaps) scored {dishonest_honesty} "
        f"on honesty, while honest spec (low conf + declared unknowns) scored {honest_honesty}. "
        "This trains the loop to hide gaps, which is catastrophic."
    )

    # The dishonest spec's honesty score should be very low (0 or near 0)
    assert dishonest_honesty <= DIMENSION_WEIGHTS["honesty"] * 0.3, (
        f"Dishonest spec scored {dishonest_honesty} on honesty, expected near 0"
    )

    # The honest spec's honesty score should be the full weight
    assert honest_honesty == DIMENSION_WEIGHTS["honesty"], (
        f"Honest spec scored {honest_honesty} on honesty, expected {DIMENSION_WEIGHTS['honesty']}"
    )


# === TEST 12: Provenance hard-cap ===

def test_provenance_hard_cap_stub_extraction():
    """A spec with extraction_source: 'stub' must NOT reach top band and must have blocking issue."""
    spec = _make_spec(
        intent="Update Case Status",
        confidence=0.95,  # Perfect confidence
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
        ],
        orchestration_steps=["Perfect orchestration"],
        guardrails=["Perfect guardrails"],
        failure_handling=["Observed failures"],
        unknowns=[],
    )

    # Write to temp file with stub provenance
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = Path(f.name)

    try:
        data = spec.to_dict()
        data["provenance"] = {
            "extraction_source": "stub",
            "telemetry_source": "live-org",
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        result = score_spec_file(path)

        # Must NOT reach top band
        assert result.band == "low", f"Stub-provenance spec reached {result.band} band, expected 'low'"

        # Must NOT pass
        assert not result.passed, "Stub-provenance spec passed when it should have failed"

        # Must have blocking issue mentioning stub
        assert any("stub" in issue.lower() for issue in result.blocking_issues), (
            f"No blocking issue mentions 'stub'. Blocking issues: {result.blocking_issues}"
        )

        # Provenance dimension must score 0
        prov_score = result.dimensions["provenance_integrity"].score
        assert prov_score == 0, f"Provenance score should be 0 for stub source, got {prov_score}"

    finally:
        import os
        os.unlink(path)


def test_provenance_hard_cap_mock_telemetry():
    """A spec with telemetry_source: 'mock' must NOT reach top band and must have blocking issue."""
    spec = _make_spec()

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = Path(f.name)

    try:
        data = spec.to_dict()
        data["provenance"] = {
            "extraction_source": "dom-capture",
            "telemetry_source": "mock",
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        result = score_spec_file(path)

        assert result.band == "low", f"Mock-telemetry spec reached {result.band} band, expected 'low'"
        assert not result.passed, "Mock-telemetry spec passed when it should have failed"
        assert any("mock" in issue.lower() for issue in result.blocking_issues), (
            f"No blocking issue mentions 'mock'. Blocking issues: {result.blocking_issues}"
        )

        prov_score = result.dimensions["provenance_integrity"].score
        assert prov_score == 0, f"Provenance score should be 0 for mock telemetry, got {prov_score}"

    finally:
        import os
        os.unlink(path)


# === TEST 13: Blocking issues override the numeric score ===

def test_blocking_issues_override_score():
    """A spec with a high score but blocking issues must have passed=False."""
    # Good spec but with UNRESOLVED intent (blocking)
    spec = _make_spec(
        intent="UNRESOLVED: something",
        confidence=0.75,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "observed")],
            ),
        ],
    )

    result = score_spec(spec)

    # Should have a blocking issue for UNRESOLVED intent
    assert len(result.blocking_issues) > 0, "Expected blocking issue for UNRESOLVED intent"
    assert any("UNRESOLVED" in issue for issue in result.blocking_issues)

    # Even if score is high, passed must be False
    assert not result.passed, f"Spec with blocking issue passed (score={result.total})"


def test_no_objects_touched_is_blocking():
    """A spec with no objects_touched must have a blocking issue and fail."""
    spec = _make_spec(
        objects_touched=[],
        entities=[],  # Consequently no entities either
    )

    result = score_spec(spec)

    assert len(result.blocking_issues) > 0, "Expected blocking issue for no objects_touched"
    assert any("no salesforce object" in issue.lower() for issue in result.blocking_issues), (
        f"Expected 'no salesforce object' in blocking issues, got: {result.blocking_issues}"
    )
    assert not result.passed


# === TEST 14: Determinism and purity ===

def test_determinism():
    """Same input -> identical score."""
    spec = _make_spec()

    score1 = score_spec(spec)
    score2 = score_spec(spec)

    assert score1.total == score2.total, "Same input produced different scores"
    assert score1.band == score2.band
    assert score1.passed == score2.passed
    assert score1.blocking_issues == score2.blocking_issues

    # Deep equality on dimensions
    for name, dim1 in score1.dimensions.items():
        dim2 = score2.dimensions[name]
        assert dim1.score == dim2.score, f"Dimension {name} score differs"
        assert dim1.findings == dim2.findings, f"Dimension {name} findings differ"


def test_purity_no_mutation():
    """The scorer must NOT mutate the input spec."""
    spec = _make_spec()

    # Deep copy before scoring
    spec_copy = copy.deepcopy(spec)

    _ = score_spec(spec)

    # Assert spec is unchanged
    assert spec.intent == spec_copy.intent
    assert spec.confidence == spec_copy.confidence
    assert spec.objects_touched == spec_copy.objects_touched
    assert spec.entities == spec_copy.entities
    assert spec.orchestration_steps == spec_copy.orchestration_steps
    assert spec.guardrails == spec_copy.guardrails
    assert spec.failure_handling == spec_copy.failure_handling
    assert spec.unknowns == spec_copy.unknowns
    assert spec.evidence == spec_copy.evidence


# === TEST 15: compare/converged ===

def test_compare_delta():
    """compare() computes delta correctly."""
    spec1 = _make_spec(confidence=0.5, objects_touched=[])
    spec2 = _make_spec(confidence=0.75, objects_touched=["Case"])

    score1 = score_spec(spec1)
    score2 = score_spec(spec2)

    comparison = compare(score1, score2)

    assert comparison.delta == score2.total - score1.total
    assert comparison.improved == (score2.total > score1.total)


def test_compare_improved():
    """compare() sets improved=True when score increases."""
    spec1 = _make_spec(confidence=0.5, objects_touched=[])
    spec2 = _make_spec(confidence=0.75, objects_touched=["Case"])

    score1 = score_spec(spec1)
    score2 = score_spec(spec2)

    comparison = compare(score1, score2)

    assert comparison.improved, "Score should have improved"
    assert comparison.delta > 0


def test_compare_regressions():
    """compare() detects regressions in individual dimensions."""
    # Artificially create a regression by manipulating a good spec to be worse in one dimension
    spec1 = _make_spec(
        confidence=0.75,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "observed")],
            ),
        ],
    )

    # spec2 has fewer entities (regression in completeness/evidence_grounding)
    spec2 = _make_spec(
        confidence=0.75,
        objects_touched=["Case"],
        entities=[],  # Worse
    )

    score1 = score_spec(spec1)
    score2 = score_spec(spec2)

    comparison = compare(score1, score2)

    # Should detect regressions
    assert len(comparison.regressions) > 0, "No regressions detected when entities were removed"


def test_converged_epsilon():
    """converged() returns True when delta <= epsilon."""
    spec1 = _make_spec(confidence=0.75)
    spec2 = _make_spec(confidence=0.76)  # Tiny change

    score1 = score_spec(spec1)
    score2 = score_spec(spec2)

    comparison = compare(score1, score2)

    # With epsilon=3, a delta of 0-2 should converge
    assert comparison.converged(epsilon=3), f"Should converge with delta={comparison.delta}, epsilon=3"

    # With epsilon=0, should not converge unless delta is exactly 0
    if comparison.delta != 0:
        assert not comparison.converged(epsilon=0), "Should not converge with epsilon=0 and nonzero delta"


def test_not_converged_large_delta():
    """converged() returns False when delta > epsilon."""
    spec1 = _make_spec(confidence=0.5, objects_touched=[])
    spec2 = _make_spec(confidence=0.75, objects_touched=["Case"])

    score1 = score_spec(spec1)
    score2 = score_spec(spec2)

    comparison = compare(score1, score2)

    # Large delta should not converge with small epsilon
    assert not comparison.converged(epsilon=3), f"Should not converge with large delta={comparison.delta}"


# === TEST 16: Placeholder scanning ===

def test_placeholder_scanning_catches_all_markers():
    """The scorer must catch EVERY marker from score_run.py's list."""
    # Both modules should import from markers.py to prevent drift
    from sf_video_blueprint.markers import PLACEHOLDER_MARKERS as MARKERS_PY
    from scripts.score_run import PLACEHOLDER_MARKERS as RUN_MARKERS

    # spec_score.py imports from markers.py (assert identity)
    import sf_video_blueprint.spec_score as spec_score_module
    assert spec_score_module.PLACEHOLDER_MARKERS is MARKERS_PY, (
        "spec_score.PLACEHOLDER_MARKERS should be imported from markers.py, not redefined"
    )

    # score_run.py still defines its own (should import from markers.py instead)
    # markers.py is more comprehensive (includes TODO, FIXME, etc.) and has replaced
    # stub-content proxies (button:Save) with structural checks (STUB_FINGERPRINTS).
    # Verify that markers.py covers at least the core markers from score_run.py,
    # excluding the obsolete button:Save proxy.
    core_run_markers = set(RUN_MARKERS) - {"button:Save", "Heuristic extraction in use"}
    assert core_run_markers.issubset(set(PLACEHOLDER_MARKERS)), (
        f"markers.py missing markers from score_run.py. "
        f"Missing: {core_run_markers - set(PLACEHOLDER_MARKERS)}"
    )

    # Test each marker individually
    for marker in PLACEHOLDER_MARKERS:
        spec = _make_spec(intent=f"Do something {marker}")

        result = score_spec(spec)

        placeholder_dim = result.dimensions["placeholder_freedom"]
        assert placeholder_dim.score == 0, f"Marker {marker!r} not caught by placeholder scanner"
        assert marker in placeholder_dim.findings, f"Marker {marker!r} not in findings"


def test_placeholder_scanning_needs_evidence():
    """The scorer must catch 'NEEDS EVIDENCE' markers from allow_incomplete=True specs."""
    spec = _make_spec(
        orchestration_steps=["[NEEDS EVIDENCE: action API names not observed]"],
    )

    result = score_spec(spec)

    placeholder_dim = result.dimensions["placeholder_freedom"]
    assert placeholder_dim.score == 0, "NEEDS EVIDENCE marker not caught"
    assert "[NEEDS EVIDENCE" in str(placeholder_dim.findings), (
        f"Expected '[NEEDS EVIDENCE' in findings, got: {placeholder_dim.findings}"
    )


def test_button_save_is_not_a_placeholder():
    """Real DOM capture of a Save click produces button:Save legitimately.

    button:Save was removed from PLACEHOLDER_MARKERS because it's real evidence,
    not a stub marker. A spec with button:Save from real extraction should score
    full marks on placeholder_freedom.
    """
    spec = _make_spec(
        orchestration_steps=["SUBMIT on button:Save -> writes Status"],
    )

    result = score_spec(spec)

    placeholder_dim = result.dimensions["placeholder_freedom"]
    assert placeholder_dim.score == placeholder_dim.max_score, (
        f"button:Save wrongly flagged as placeholder. Score: {placeholder_dim.score}/{placeholder_dim.max_score}"
    )


def test_stub_extraction_still_caught():
    """Stub-provenance spec must be caught by structural check, not string matching.

    The stub extractor emits STUB_FINGERPRINTS that identify it, plus
    extraction_source != 'dom-capture'. A stub spec must fail even if it doesn't
    contain button:Save.
    """
    spec = _make_spec(
        orchestration_steps=["Heuristic extraction in use"],  # STUB_FINGERPRINTS
    )

    result = score_spec(spec)

    placeholder_dim = result.dimensions["placeholder_freedom"]
    assert placeholder_dim.score == 0, (
        f"Stub fingerprint not caught. Score: {placeholder_dim.score}"
    )
    assert "Heuristic extraction in use" in placeholder_dim.findings


def test_d7_stub_marker_in_entity_evidence_detail_blocks_spec():
    """D7: in-process scanner must catch stub markers anywhere in the spec.

    scripts/score_run.py greps the raw JSON text for PLACEHOLDER_MARKERS +
    STUB_FINGERPRINTS across the WHOLE serialized artifact. scan_spec previously
    walked a whitelist of keys and missed content in fields that weren't on the
    list — evidence[].source, or any new field the builder adds later. That let
    an artifact score 100/100 in-process while CI blocked the same file.

    The regression prevention: put a STUB_FINGERPRINT in a spot the old whitelist
    scan didn't cover — evidence[].source, an entity.evidence.detail with a stub
    fingerprint, and a top-level evidence.detail with a placeholder marker — on
    an otherwise clean spec, and assert the placeholder_freedom dimension flags it
    and a blocking issue fires.
    """
    # Case 1: STUB_FINGERPRINT in entity.evidence[].detail
    # (the scenario explicitly named in the D7 report: entity.evidence.detail).
    spec_a = _make_spec(
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[
                    SpecEvidence(
                        "data-delta",
                        # STUB_FINGERPRINT embedded in an otherwise plausible detail.
                        "Case.Status observed; Heuristic extraction in use during derivation",
                    )
                ],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "recordId required to act on the record")],
            ),
        ],
    )
    result_a = score_spec(spec_a)
    assert result_a.dimensions["placeholder_freedom"].score == 0, (
        f"D7: stub fingerprint in entity.evidence[].detail was NOT caught in-process. "
        f"placeholder_freedom={result_a.dimensions['placeholder_freedom'].score}"
    )
    assert any("Heuristic extraction in use" in issue for issue in result_a.blocking_issues), (
        f"D7: stub in entity.evidence[].detail did not raise a blocking issue. "
        f"blocking={result_a.blocking_issues}"
    )
    assert not result_a.passed, "D7: spec with stub in entity.evidence.detail must not pass"

    # Case 2: PLACEHOLDER_MARKER in evidence[].source (a field the old whitelist
    # scan did NOT read — evidence[].source is not on the whitelist, only
    # evidence[].detail was).
    spec_b = _make_spec(
        evidence=[
            # A source value that also carries a placeholder marker. The old
            # whitelist walked evidence[].detail but never evidence[].source, so
            # this was invisible to scan_spec while score_run.py's raw-text
            # scan caught it.
            SpecEvidence("telemetry TODO: link source not yet wired", "validation observed"),
            SpecEvidence("data-delta", "Case mutated"),
        ],
    )
    result_b = score_spec(spec_b)
    assert result_b.dimensions["placeholder_freedom"].score == 0, (
        f"D7: TODO marker in evidence[].source was NOT caught in-process. "
        f"placeholder_freedom={result_b.dimensions['placeholder_freedom'].score}, "
        f"findings={result_b.dimensions['placeholder_freedom'].findings}"
    )
    assert any(
        "TODO" in issue or "Placeholder" in issue for issue in result_b.blocking_issues
    ), (
        f"D7: TODO in evidence[].source did not raise a blocking issue. "
        f"blocking={result_b.blocking_issues}"
    )


def test_d7_in_process_scan_matches_raw_json_scan():
    """D7: in-process scan_spec must find every marker scripts/score_run.py's raw scan would find.

    The invariant that keeps the two gates from diverging: for any serialized spec
    JSON, `scan_spec(loaded_dict)` and the raw-text scan performed by
    `scripts/score_run.py:_scan_placeholders` must return the SAME set of markers.
    We plant one STUB_FINGERPRINT and one PLACEHOLDER_MARKER in fields the old
    whitelist scan didn't cover, serialize, and compare.
    """
    spec = _make_spec(
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[
                    SpecEvidence(
                        "data-delta",
                        "Case.Status observed; Baseline extraction. Replace with CV pipeline",
                    )
                ],
            ),
        ],
        evidence=[
            SpecEvidence("telemetry", "validation observed"),
            SpecEvidence("extraction", "TODO: capture the 3 action(s) properly"),
        ],
    )

    serialized = json.dumps(spec.to_dict())

    from sf_video_blueprint.markers import (
        PLACEHOLDER_MARKERS as PM,
        STUB_FINGERPRINTS as SF,
        scan_spec,
    )

    raw_hits = {m for m in (*PM, *SF) if m in serialized}
    scan_hits = set(scan_spec(spec.to_dict()))

    # Every marker the raw-JSON scan would find must also be found by scan_spec.
    # (scan_spec may find additional occurrences via deduplication of counts, so
    # we check set containment in one direction rather than equality of counts.)
    assert raw_hits.issubset(scan_hits), (
        f"D7: scan_spec missed markers the raw-JSON scan caught. "
        f"raw_only={raw_hits - scan_hits}, raw={raw_hits}, scan={scan_hits}"
    )
    # And the raw scan must have found something, or this test is vacuous.
    assert raw_hits, "Test setup error: raw scan found no markers to verify against"


# === TEST 17: Weights sum to 100 and no dimension exceeds max ===

def test_weights_sum_to_100():
    """DIMENSION_WEIGHTS must sum to exactly 100."""
    total_weight = sum(DIMENSION_WEIGHTS.values())
    assert total_weight == 100, f"DIMENSION_WEIGHTS sum to {total_weight}, expected 100"


def test_no_dimension_exceeds_max():
    """No dimension score can exceed its max_score."""
    spec = _make_spec(
        intent="Update Case Status",
        confidence=0.95,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "observed")],
            ),
        ],
        orchestration_steps=["Step 1", "Step 2", "Step 3"],
        guardrails=["Guardrail 1", "Guardrail 2"],
        failure_handling=["Observed failure: validation error"],
        unknowns=[],
    )

    result = score_spec(spec)

    for name, dim in result.dimensions.items():
        assert dim.score <= dim.max_score, (
            f"Dimension {name} scored {dim.score}, exceeding max_score {dim.max_score}"
        )


def test_total_cannot_exceed_max_total():
    """The total score cannot exceed max_total (100)."""
    spec = _make_spec(
        intent="Update Case Status",
        confidence=0.95,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "observed")],
            ),
        ],
        orchestration_steps=["Step 1", "Step 2", "Step 3"],
        guardrails=["Guardrail 1", "Guardrail 2"],
        failure_handling=["Observed validation failure during recording: Status must be approved value"],
        unknowns=[],
    )

    result = score_spec(spec)

    assert result.total <= result.max_total, (
        f"Total score {result.total} exceeds max_total {result.max_total}"
    )


# === NEW TESTS FOR ROUND 2 FIXES ===

# === G1: Deletion must not pay ===

def test_g1_removing_observed_entity_never_raises_score():
    """G1: Removing a genuinely-observed entity must never RAISE the total score."""
    # Spec with 2 observed entities (data-delta)
    spec_with_entities = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority observed")],
            ),
        ],
    )

    # Same spec with 1 entity removed
    spec_fewer_entities = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
        ],
    )

    score_with = score_spec(spec_with_entities)
    score_fewer = score_spec(spec_fewer_entities)

    # Removing an observed entity must not raise the score
    assert score_fewer.total <= score_with.total, (
        f"G1 VIOLATED: Removing an observed entity RAISED the score from {score_with.total} to {score_fewer.total}. "
        "This trains the loop to delete evidence."
    )


# === G2: Mandated inference is not the spec's fault ===

def test_g2_mandated_recordid_does_not_lower_score():
    """G2: Adding the mandated recordId entity must never LOWER the total score."""
    # Spec with 1 observed entity (no recordId)
    spec_without_recordid = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
        ],
    )

    # Same spec with the mandated recordId added (as the builder would)
    spec_with_recordid = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "a Case record must be identified to act on it")],
            ),
        ],
    )

    score_without = score_spec(spec_without_recordid)
    score_with = score_spec(spec_with_recordid)

    # Adding the mandated recordId must not lower the score
    assert score_with.total >= score_without.total, (
        f"G2 VIOLATED: Adding the mandated recordId LOWERED the score from {score_without.total} to {score_with.total}. "
        "The builder emits this unconditionally, so the spec must not be penalized."
    )


# === G3: Honesty must pay ===

def test_g3_declaring_unknown_scores_gte_deleting_it():
    """G3: A spec that declares an unknown must score >= the same spec with that unknown deleted."""
    # Spec with declared unknowns (honest)
    spec_with_unknowns = _make_spec(
        confidence=0.5,
        objects_touched=[],
        entities=[],
        unknowns=["No objects_touched observed", "No entities derived"],
    )

    # Same spec with unknowns deleted (dishonest by omission)
    spec_without_unknowns = _make_spec(
        confidence=0.5,
        objects_touched=[],
        entities=[],
        unknowns=[],
    )

    score_with = score_spec(spec_with_unknowns)
    score_without = score_spec(spec_without_unknowns)

    # Declaring unknowns must score >= deleting them
    assert score_with.total >= score_without.total, (
        f"G3 VIOLATED: Declaring unknowns scored {score_with.total}, "
        f"deleting them scored {score_without.total}. "
        "This trains the loop to hide gaps."
    )


# === F1: The bad spec must fail ===

def test_f1_bad_spec_from_contract_must_fail():
    """F1: The exact bad spec from the contract must score < 75 and passed=False."""
    # This is the spec that currently scores 100/100 (the regression to kill)
    bad_spec = DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=0.7,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "x")],
            )
        ],
        orchestration_steps=["Resolve the Case", "Resolve the Case"],  # duplicated
        guardrails=["Validate input", "Validate input"],  # duplicated, generic
        failure_handling=[
            "No failures were observed in this run, so error paths are UNTESTED. "
            "Record a failing variant before relying on this spec."
        ],  # explicitly untested
        unknowns=[],
        evidence=[],
    )

    result = score_spec(bad_spec)

    assert result.total < PASS_THRESHOLD, (
        f"F1 FAILED: The bad spec from the contract scored {result.total}, "
        f"which is >= PASS_THRESHOLD={PASS_THRESHOLD}. It must score < 75."
    )
    assert not result.passed, (
        f"F1 FAILED: The bad spec passed when it should have failed. Score: {result.total}, passed: {result.passed}"
    )


def test_f1_near_duplicate_content_bypass_is_blocked():
    """F1 REGRESSION (anti-gaming): a spec that keeps the three named F1 defects
    (dup-in-spirit steps, generic-tone guardrails, UNTESTED failure) but paraphrases
    around the fixture's incidental blockers must not pass.

    The F1 fix, as originally written, was a check on the exact-string shape of the
    F1 fixture: it relied on empty evidence trail, 1-char evidence detail 'x',
    literal "Validate input" guardrails, and steps that never name Status to trip
    other blockers (hollow evidence_grounding, C8 filler, C10 coupling). A
    refinement loop that reads the scorer converges on a spec that keeps the three
    named semantic defects while patching those four incidentals — and it scored
    82/100 passed=True with no blocking issue, because none of the surviving
    checks look at NEAR-duplication of instructions.

    This test pins the semantic property the F1 fix advertises: near-duplicate
    orchestration steps and near-duplicate guardrails must block the spec, even
    when the exact strings differ by trailing filler, punctuation, or word order.
    """
    counter_example = DerivedAgentSpec(
        intent="Update Case Status",
        confidence=0.7,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[
                    SpecEvidence(
                        "data-delta",
                        "Case.Status changed New -> Working at step-004",
                    )
                ],
            )
        ],
        # Near-duplicates: identical content tokens after stripping stopwords
        # ("now"/"here"). The exact-string set counts these as 2 distinct steps,
        # so the completeness section awards full credit and the earlier F1 fix
        # sees nothing to fail on.
        orchestration_steps=[
            "Resolve the Case Status now",
            "Resolve the Case Status here",
        ],
        # Near-duplicates: differ only by a trailing period, and the generic-guardrail
        # blocklist does not contain "handle input safely".
        guardrails=[
            "Please handle input safely",
            "Please handle input safely.",
        ],
        failure_handling=[
            "No failures were observed in this run, so error paths are UNTESTED. "
            "Record a failing variant before relying on this spec."
        ],
        unknowns=[],
        # Non-empty evidence trail so the "no top-level evidence trail" blocker
        # does not fire.
        evidence=[SpecEvidence("extraction", "4 actions in recording")],
    )

    result = score_spec(counter_example)

    assert not result.passed, (
        f"F1 ANTI-GAMING REGRESSION: near-duplicate paraphrase spec passed "
        f"(score={result.total}/100). The F1 fix must reject two steps or two "
        "guardrails that carry the same content tokens after stopword/punctuation "
        "stripping, not merely two textually-identical strings."
    )

    # The blocker must explicitly cite near-duplication so the failure is
    # auditable — a spec author who fixes an unrelated defect must not
    # accidentally make this one silently pass.
    near_dup_blockers = [
        issue for issue in result.blocking_issues
        if "near-duplicate" in issue.lower()
    ]
    assert near_dup_blockers, (
        f"F1 ANTI-GAMING REGRESSION: spec blocked but not with a near-duplicate "
        f"reason. Blocking issues: {result.blocking_issues}"
    )

    # Both surfaces (steps AND guardrails) must be called out; otherwise a spec
    # can shed one and slip through by paraphrasing on the other side.
    step_blocker = [i for i in near_dup_blockers if "orchestration step" in i.lower()]
    guardrail_blocker = [i for i in near_dup_blockers if "guardrail" in i.lower()]
    assert step_blocker, (
        f"F1 ANTI-GAMING REGRESSION: near-duplicate steps not flagged. "
        f"Blocking issues: {result.blocking_issues}"
    )
    assert guardrail_blocker, (
        f"F1 ANTI-GAMING REGRESSION: near-duplicate guardrails not flagged. "
        f"Blocking issues: {result.blocking_issues}"
    )


def test_f1_near_duplicate_check_does_not_fire_on_legitimate_enumeration():
    """F1 REGRESSION guard: the near-duplicate check must not fire on
    legitimately-distinct enumerated steps like D3's "Escalate to tier N", where
    the numeric suffix is the semantic differentiator.

    Digits are content tokens, not stopwords, so a signature over the token set
    preserves the distinction. This test pins that: if the check ever starts
    stripping digits (the same mistake the D3 fix corrected in the distinct-set
    logic), it will fail here.
    """
    spec = DerivedAgentSpec(
        intent="Escalate Case through tiers",
        confidence=0.5,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="CaseAgent",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("data-delta", "Case observed at step-001")],
            ),
        ],
        orchestration_steps=[
            "Escalate to tier 1",
            "Escalate to tier 2",
            "Escalate to tier 3",
        ],
        guardrails=[
            "Enforce FLS on Case 1",
            "Enforce FLS on Case 2",
        ],
        failure_handling=[
            "Observed validation failure during recording: tier locked at step-003"
        ],
        unknowns=[],
        evidence=[SpecEvidence("data-delta", "Case observed")],
    )

    result = score_spec(spec)
    near_dup_blockers = [
        issue for issue in result.blocking_issues
        if "near-duplicate" in issue.lower()
    ]
    assert not near_dup_blockers, (
        f"F1 ANTI-GAMING REGRESSION guard: near-duplicate check falsely fired on "
        f"legitimately-distinct enumerated steps/guardrails. Blocking issues: "
        f"{result.blocking_issues}"
    )


# === F2: Untested != observed ===

def test_f2_untested_failure_handling_scores_zero():
    """F2: A spec with only the negative sentinel (untested) must score 0 on testability."""
    spec = _make_spec(
        failure_handling=[
            "No failures were observed in this run, so error paths are UNTESTED. "
            "Record a failing variant before relying on this spec."
        ],
    )

    result = score_spec(spec)
    testability_score = result.dimensions["testability"].score

    # The testability dimension awards half for observed failures
    # With only the negative sentinel, that half should be 0
    # (the other half is for explicit entities, which the fixture provides)
    max_testability = DIMENSION_WEIGHTS["testability"]
    expected_max = max_testability // 2  # Only the entities half

    assert testability_score <= expected_max, (
        f"F2 FAILED: Untested failure handling scored {testability_score} on testability, "
        f"expected <= {expected_max} (half of {max_testability})."
    )


def test_f2_observed_failure_handling_scores_full():
    """F2: A spec with observed failures (builder's positive pattern) must score full marks."""
    spec = _make_spec(
        failure_handling=[
            "Observed validation failure during recording: Status must be one of approved values"
        ],
    )

    result = score_spec(spec)
    testability_score = result.dimensions["testability"].score

    # With observed failures AND explicit entities (from fixture), should score full
    max_testability = DIMENSION_WEIGHTS["testability"]

    assert testability_score == max_testability, (
        f"F2 FAILED: Observed failure handling scored {testability_score} on testability, "
        f"expected {max_testability}."
    )


# === F3: Duplicates are not completeness ===

def test_f3_duplicate_steps_count_as_one():
    """F3: Two identical orchestration steps must not count as a two-step process."""
    spec = _make_spec(
        orchestration_steps=["Resolve the Case", "Resolve the Case"],  # identical
    )

    result = score_spec(spec)
    completeness_dim = result.dimensions["completeness"]

    # The completeness dimension awards 1/5 for orchestration_steps > 1 distinct
    # With only 1 distinct step, this section should score 0
    section_score = DIMENSION_WEIGHTS["completeness"] // 5

    # We have objects, entities, guardrails, failure_handling from fixture (4/5 sections)
    # Only orchestration should fail
    expected_max = section_score * 4

    assert completeness_dim.score <= expected_max, (
        f"F3 FAILED: Duplicate steps scored {completeness_dim.score} on completeness, "
        f"expected <= {expected_max}. The distinct step count should be 1, not 2."
    )


def test_f3_distinct_steps_score_correctly():
    """F3: Two distinct steps should score full marks for the orchestration section."""
    spec = _make_spec(
        orchestration_steps=["Resolve the Case", "Submit the form"],  # distinct
    )

    result = score_spec(spec)
    completeness_dim = result.dimensions["completeness"]

    # All 5 sections present -> full score
    assert completeness_dim.score == completeness_dim.max_score, (
        f"F3 FAILED: Distinct steps scored {completeness_dim.score} on completeness, "
        f"expected {completeness_dim.max_score}."
    )


# === D3 COMPATIBILITY: numbered-suffix enumerations are legitimately distinct ===

def test_d3_enumerated_orchestration_steps_are_not_padding():
    """D3: Six semantically-distinct enumerated escalation steps must score full marks.

    The prior fix stripped a trailing `\\s+\\d+$` before hashing into
    `distinct_steps`. That collapsed legitimately-distinct enumerated steps like
    ["Escalate to tier 1", ..., "Escalate to tier 6"] into a single bucket
    ("escalate to tier"), tripping PADDING DETECTED and knocking the section
    score down to section_score // 4.

    The counter-example from the D3 report: six semantically-distinct escalation
    paths. After the fix, each retains its digit suffix, all six are distinct,
    the padding ratio is 1.0, and the orchestration section awards full credit.
    """
    from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence

    spec = DerivedAgentSpec(
        intent="Escalate Case through tiers",
        confidence=0.5,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="CaseAgent",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("data-delta", "Case observed")],
            ),
        ],
        orchestration_steps=[
            "Escalate to tier 1",
            "Escalate to tier 2",
            "Escalate to tier 3",
            "Escalate to tier 4",
            "Escalate to tier 5",
            "Escalate to tier 6",
        ],
        guardrails=["Do not leak PII"],
        failure_handling=["Retry once"],
        unknowns=[],
        evidence=[SpecEvidence("data-delta", "Case observed")],
    )

    result = score_spec(spec)
    completeness_dim = result.dimensions["completeness"]
    section_score = DIMENSION_WEIGHTS["completeness"] // 5

    # PADDING DETECTED must NOT appear for legitimately-distinct enumerated steps.
    padding_findings = [f for f in completeness_dim.findings if "PADDING DETECTED" in f and "orchestration_steps" in f]
    assert not padding_findings, (
        f"D3 FAILED: PADDING DETECTED falsely fired for enumerated escalation steps. "
        f"findings={completeness_dim.findings}"
    )

    # All 5 sections present -> full score; orchestration must get section_score, not section_score // 4.
    assert completeness_dim.score == completeness_dim.max_score, (
        f"D3 FAILED: Six distinct enumerated steps scored {completeness_dim.score} on completeness, "
        f"expected {completeness_dim.max_score}. The orchestration section was likely knocked to "
        f"section_score // 4 = {section_score // 4} instead of section_score = {section_score}."
    )


def test_d3_step_1_step_2_step_3_convention_is_not_padding():
    """D3: The common ["Step 1","Step 2","Step 3"] naming convention must not zero the section."""
    spec = _make_spec(
        orchestration_steps=["Step 1", "Step 2", "Step 3"],
    )
    result = score_spec(spec)
    completeness_dim = result.dimensions["completeness"]

    # "Step 1"/"Step 2"/"Step 3" previously collapsed to {"step"} -> distinct_count == 1
    # -> else branch -> orchestration section = 0 points. After fix, all three are
    # distinct -> full section credit.
    assert completeness_dim.score == completeness_dim.max_score, (
        f"D3 FAILED: ['Step 1','Step 2','Step 3'] scored {completeness_dim.score} on completeness, "
        f"expected {completeness_dim.max_score}. Numbered-label convention was wrongly collapsed."
    )


def test_d3_enumerated_guardrails_are_not_padding():
    """D3: Enumerated guardrails like ['Enforce FLS on Case 1'..'..N'] must not trigger PADDING DETECTED.

    The identical regex was reused for guardrails; the same defect applied.
    """
    spec = _make_spec(
        guardrails=[
            "Enforce FLS on Case 1",
            "Enforce FLS on Case 2",
            "Enforce FLS on Case 3",
            "Enforce FLS on Case 4",
            "Enforce FLS on Case 5",
            "Enforce FLS on Case 6",
        ],
    )
    result = score_spec(spec)
    completeness_dim = result.dimensions["completeness"]

    padding_findings = [f for f in completeness_dim.findings if "PADDING DETECTED" in f and "guardrails" in f]
    assert not padding_findings, (
        f"D3 FAILED: PADDING DETECTED falsely fired for enumerated guardrails. "
        f"findings={completeness_dim.findings}"
    )

    # Six textually-distinct guardrails => full guardrail-section credit, no duplicate penalty.
    assert completeness_dim.score == completeness_dim.max_score, (
        f"D3 FAILED: Six distinct enumerated guardrails scored {completeness_dim.score} on completeness, "
        f"expected {completeness_dim.max_score}."
    )


def test_d3_actual_duplicate_steps_still_penalized():
    """D3 GUARDRAIL: textually-identical duplicates must still collapse and trigger padding penalty.

    The fix removes trailing-digit stripping but must NOT weaken detection of
    true duplicates like ["Save Case","Save Case",...] (which the exact-match
    set still collapses).
    """
    spec = _make_spec(
        orchestration_steps=["Save Case"] * 6,
    )
    result = score_spec(spec)
    completeness_dim = result.dimensions["completeness"]

    padding_findings = [f for f in completeness_dim.findings if "PADDING DETECTED" in f and "orchestration_steps" in f]
    assert padding_findings, (
        f"D3 REGRESSION: True duplicates no longer trigger PADDING DETECTED. "
        f"findings={completeness_dim.findings}"
    )


# === ROUND 3 PROPERTY TESTS: D10, D11, D13 fixes ===

def test_d10_weighted_average_formula_was_non_monotone():
    """D10: The old weighted-average formula made deletion pay.

    Reproduce the exact scenario: 3 entities (2 data-delta + 1 ui-action) scoring 93,
    then removing the ui-action entity raises the score to 95. This was the regression.
    The new formula must make this scenario monotone (removal never raises).
    """
    # 3 entities: 2 data-delta + 1 ui-action
    spec_3_entities = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority observed")],
            ),
            DerivedEntity(
                name="owner",
                object_api_name="Case",
                field_api_name="Owner",
                evidence=[SpecEvidence("ui-action", "Owner field clicked")],
            ),
        ],
    )

    # Same spec with the ui-action entity removed (2 entities: 2 data-delta)
    spec_2_entities = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority observed")],
            ),
        ],
    )

    score_3 = score_spec(spec_3_entities)
    score_2 = score_spec(spec_2_entities)

    # D10: Removing an observed entity must NEVER raise the total score
    assert score_2.total <= score_3.total, (
        f"D10 REGRESSION: Deleting the ui-action entity RAISED the score from {score_3.total} to {score_2.total}. "
        f"Grounding dimension: {score_3.dimensions['evidence_grounding'].score} -> {score_2.dimensions['evidence_grounding'].score}. "
        "The new formula is still non-monotone."
    )


def test_d11_empty_spec_scores_zero_on_specificity_and_placeholder_freedom():
    """D11: An empty spec must score 0 on specificity and placeholder_freedom, not 10/10 vacuously."""
    empty_spec = DerivedAgentSpec(
        intent="",
        confidence=0.0,
        objects_touched=[],
        entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
        unknowns=[],
        evidence=[],
    )

    result = score_spec(empty_spec)

    specificity_score = result.dimensions["specificity"].score
    placeholder_freedom_score = result.dimensions["placeholder_freedom"].score

    assert specificity_score == 0, (
        f"D11 FAILED (specificity): Empty spec scored {specificity_score}/10 on specificity, expected 0. "
        "Vacuous full marks are noise that inflate every partially-empty spec."
    )

    assert placeholder_freedom_score == 0, (
        f"D11 FAILED (placeholder_freedom): Empty spec scored {placeholder_freedom_score}/10 on placeholder_freedom, expected 0. "
        "Absent content is not the same as clean content."
    )


def test_d13_absent_guardrails_is_blocking():
    """D13: A spec with NO guardrails must have a blocking issue and fail, even if other dimensions score well."""
    spec_no_guardrails = _make_spec(
        intent="Update Case Status",
        confidence=0.75,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
        ],
        orchestration_steps=["Resolve the Case", "Submit the form"],
        guardrails=[],  # ABSENT
        failure_handling=["Observed validation failure during recording: Status must be approved value"],
        unknowns=[],
    )

    result = score_spec(spec_no_guardrails)

    assert not result.passed, (
        f"D13 FAILED: Spec with no guardrails passed (score={result.total}/100). "
        "Absent guardrails is a structural defect and must block."
    )

    assert len(result.blocking_issues) > 0, "D13 FAILED: Expected blocking issue for absent guardrails"

    guardrail_blocker = [issue for issue in result.blocking_issues if "guardrail" in issue.lower()]
    assert len(guardrail_blocker) > 0, (
        f"D13 FAILED: No blocking issue mentions 'guardrail'. Blocking issues: {result.blocking_issues}"
    )


# === PROPERTY TESTS: Systematic corpus over G1/G2/G3 ===

def _build_corpus():
    """Generate a systematic corpus of specs varying evidence sources, counts, and structure.

    Returns a list of (label, spec) tuples for property testing.
    """
    from itertools import product

    corpus = []

    # Vary entity counts and evidence sources
    evidence_configs = [
        ("1-data-delta", [("status", "data-delta")]),
        ("2-data-delta", [("status", "data-delta"), ("priority", "data-delta")]),
        ("1-ui-action", [("status", "ui-action")]),
        ("2-ui-action", [("status", "ui-action"), ("priority", "ui-action")]),
        ("1-inference", [("status", "inference")]),
        ("2-inference", [("status", "inference"), ("priority", "inference")]),
        ("mixed-dd+ui", [("status", "data-delta"), ("priority", "ui-action")]),
        ("mixed-dd+inf", [("status", "data-delta"), ("priority", "inference")]),
        ("mixed-ui+inf", [("status", "ui-action"), ("priority", "inference")]),
        ("3-all-types", [("status", "data-delta"), ("priority", "ui-action"), ("owner", "inference")]),
    ]

    for label, entity_config in evidence_configs:
        entities = [
            DerivedEntity(
                name=name,
                object_api_name="Case",
                field_api_name=name.capitalize(),
                evidence=[SpecEvidence(source, f"{name} from {source}")],
            )
            for name, source in entity_config
        ]

        spec = _make_spec(
            intent=f"Update Case ({label})",
            objects_touched=["Case"],
            entities=entities,
            orchestration_steps=["Resolve the Case", "Submit the form"],
            guardrails=["Enforce FLS"],
            failure_handling=["Observed validation failure during recording: x"],
            unknowns=[],
        )

        corpus.append((label, spec))

    # Add the builder-mandated recordId case (G2)
    for label, entity_config in evidence_configs[:3]:  # Just a few base cases
        entities = [
            DerivedEntity(
                name=name,
                object_api_name="Case",
                field_api_name=name.capitalize(),
                evidence=[SpecEvidence(source, f"{name} from {source}")],
            )
            for name, source in entity_config
        ]
        # Add the mandated recordId
        entities.append(
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "a Case record must be identified")],
            )
        )

        spec = _make_spec(
            intent=f"Update Case ({label}+recordId)",
            objects_touched=["Case"],
            entities=entities,
            orchestration_steps=["Resolve the Case", "Submit the form"],
            guardrails=["Enforce FLS"],
            failure_handling=["Observed validation failure during recording: x"],
            unknowns=[],
        )

        corpus.append((f"{label}+recordId", spec))

    return corpus


def test_g1_property_removing_observed_entity_never_raises_score():
    """G1 PROPERTY TEST: For every spec in the corpus, removing any genuinely-observed entity
    (data-delta or ui-action) must never RAISE the total score.
    """
    corpus = _build_corpus()
    violations = []

    for label, spec in corpus:
        # Score the full spec
        full_score = score_spec(spec)

        # For each entity, remove it and re-score
        for i, entity in enumerate(spec.entities):
            sources = [e.source for e in entity.evidence]
            # Only check genuinely-observed entities (data-delta or ui-action)
            if "data-delta" in sources or "ui-action" in sources:
                # Build a new spec with this entity removed
                reduced_entities = spec.entities[:i] + spec.entities[i+1:]
                reduced_spec = _make_spec(
                    intent=spec.intent,
                    confidence=spec.confidence,
                    objects_touched=spec.objects_touched,
                    entities=reduced_entities,
                    orchestration_steps=spec.orchestration_steps,
                    guardrails=spec.guardrails,
                    failure_handling=spec.failure_handling,
                    unknowns=spec.unknowns,
                    evidence=spec.evidence,
                )

                reduced_score = score_spec(reduced_spec)

                # G1: Removing an observed entity must never raise the score
                if reduced_score.total > full_score.total:
                    violations.append(
                        f"[{label}] Removing entity '{entity.name}' (source={sources}) "
                        f"RAISED score from {full_score.total} to {reduced_score.total}. "
                        f"Grounding: {full_score.dimensions['evidence_grounding'].score} -> {reduced_score.dimensions['evidence_grounding'].score}"
                    )

    if violations:
        pytest.fail(f"G1 PROPERTY VIOLATED in {len(violations)} cases:\n" + "\n".join(violations[:5]))


def test_g2_property_mandated_recordid_never_lowers_score():
    """G2 PROPERTY TEST: For every spec in the corpus, adding the builder-mandated recordId
    (inference-grounded, field_api_name == "Id") must never LOWER the total score.
    """
    corpus = _build_corpus()
    violations = []

    for label, spec in corpus:
        # Skip specs that already have the mandated recordId
        if any(e.field_api_name == "Id" and [ev.source for ev in e.evidence] == ["inference"] for e in spec.entities):
            continue

        # Score without recordId
        without_score = score_spec(spec)

        # Add the mandated recordId
        entities_with_recordid = list(spec.entities) + [
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "a Case record must be identified to act on it")],
            )
        ]
        spec_with_recordid = _make_spec(
            intent=spec.intent,
            confidence=spec.confidence,
            objects_touched=spec.objects_touched,
            entities=entities_with_recordid,
            orchestration_steps=spec.orchestration_steps,
            guardrails=spec.guardrails,
            failure_handling=spec.failure_handling,
            unknowns=spec.unknowns,
            evidence=spec.evidence,
        )

        with_score = score_spec(spec_with_recordid)

        # G2: Adding the mandated recordId must never lower the score
        if with_score.total < without_score.total:
            violations.append(
                f"[{label}] Adding mandated recordId LOWERED score from {without_score.total} to {with_score.total}. "
                f"Grounding: {without_score.dimensions['evidence_grounding'].score} -> {with_score.dimensions['evidence_grounding'].score}"
            )

    if violations:
        pytest.fail(f"G2 PROPERTY VIOLATED in {len(violations)} cases:\n" + "\n".join(violations[:5]))


def test_g3_property_declaring_unknown_never_lowers_score():
    """G3 PROPERTY TEST: For every spec, declaring an unknown must score >= deleting that unknown."""
    corpus = _build_corpus()
    violations = []

    for label, spec in corpus:
        # Build two variants: one with declared unknowns, one without
        with_unknowns = _make_spec(
            intent=spec.intent,
            confidence=spec.confidence,
            objects_touched=spec.objects_touched,
            entities=spec.entities,
            orchestration_steps=spec.orchestration_steps,
            guardrails=spec.guardrails,
            failure_handling=spec.failure_handling,
            unknowns=["Some field could not be derived"],  # Declare unknown
            evidence=spec.evidence,
        )

        without_unknowns = _make_spec(
            intent=spec.intent,
            confidence=spec.confidence,
            objects_touched=spec.objects_touched,
            entities=spec.entities,
            orchestration_steps=spec.orchestration_steps,
            guardrails=spec.guardrails,
            failure_handling=spec.failure_handling,
            unknowns=[],  # Hide the unknown
            evidence=spec.evidence,
        )

        with_score = score_spec(with_unknowns)
        without_score = score_spec(without_unknowns)

        # G3: Declaring unknowns must score >= deleting them
        if with_score.total < without_score.total:
            violations.append(
                f"[{label}] Declaring unknowns scored {with_score.total}, "
                f"deleting them scored {without_score.total}. This trains the loop to hide gaps."
            )

    if violations:
        pytest.fail(f"G3 PROPERTY VIOLATED in {len(violations)} cases:\n" + "\n".join(violations[:5]))


def test_monotone_in_evidence_strengthening_entity_never_lowers_score():
    """MONOTONICITY PROPERTY: Strengthening an entity's evidence source must never lower the score.

    inference -> ui-action -> data-delta is the quality hierarchy.
    Upgrading an entity along this chain must never lower the total.
    """
    violations = []

    # Base spec with one entity at each evidence level
    base_entities = [
        ("status_inference", "inference"),
        ("priority_ui", "ui-action"),
        ("owner_dd", "data-delta"),
    ]

    for name, initial_source in base_entities:
        # Build a spec with this entity at initial_source
        initial_spec = _make_spec(
            intent=f"Update Case ({name})",
            objects_touched=["Case"],
            entities=[
                DerivedEntity(
                    name=name.split("_")[0],
                    object_api_name="Case",
                    field_api_name=name.split("_")[0].capitalize(),
                    evidence=[SpecEvidence(initial_source, f"{name} from {initial_source}")],
                )
            ],
            orchestration_steps=["Resolve the Case", "Submit the form"],
            guardrails=["Enforce FLS"],
            failure_handling=["Observed validation failure during recording: x"],
            unknowns=[],
        )

        initial_score = score_spec(initial_spec)

        # Upgrade the evidence source
        upgrades = {
            "inference": ["ui-action", "data-delta"],
            "ui-action": ["data-delta"],
            "data-delta": [],
        }

        for upgraded_source in upgrades[initial_source]:
            upgraded_spec = _make_spec(
                intent=f"Update Case ({name} -> {upgraded_source})",
                objects_touched=["Case"],
                entities=[
                    DerivedEntity(
                        name=name.split("_")[0],
                        object_api_name="Case",
                        field_api_name=name.split("_")[0].capitalize(),
                        evidence=[SpecEvidence(upgraded_source, f"{name} upgraded to {upgraded_source}")],
                    )
                ],
                orchestration_steps=["Resolve the Case", "Submit the form"],
                guardrails=["Enforce FLS"],
                failure_handling=["Observed validation failure during recording: x"],
                unknowns=[],
            )

            upgraded_score = score_spec(upgraded_spec)

            # Upgrading evidence must never lower the score
            if upgraded_score.total < initial_score.total:
                violations.append(
                    f"[{name}] Upgrading evidence from {initial_source} to {upgraded_source} "
                    f"LOWERED score from {initial_score.total} to {upgraded_score.total}. "
                    f"Grounding: {initial_score.dimensions['evidence_grounding'].score} -> {upgraded_score.dimensions['evidence_grounding'].score}"
                )

    if violations:
        pytest.fail(f"MONOTONICITY VIOLATED in {len(violations)} cases:\n" + "\n".join(violations[:5]))


# === ABSOLUTE VALUE TESTS FOR EVIDENCE_GROUNDING (Mutant 1 killer) ===

def test_evidence_grounding_no_evidence_at_all_scores_zero():
    """ABSOLUTE: A spec with NO evidence at all must score 0 on evidence_grounding.

    This is the decisive test that kills Mutant 1 (unconditional max return).
    """
    spec = DerivedAgentSpec(
        intent="Update Case Status",
        confidence=0.5,
        objects_touched=["Case"],
        entities=[],  # No entities at all
        orchestration_steps=["Do something"],
        guardrails=["Enforce FLS"],
        failure_handling=["Unknown"],
        unknowns=[],
        evidence=[],
    )

    result = score_spec(spec)
    grounding = result.dimensions["evidence_grounding"]

    assert grounding.score == 0, (
        f"Spec with NO entities must score exactly 0/30 on evidence_grounding, got {grounding.score}"
    )


def test_evidence_grounding_only_inference_entities_scores_near_zero():
    """ABSOLUTE: A spec with only inference-based entities must score very low (<= 3/30)."""
    spec = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("inference", "inferred from context")],
            ),
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("inference", "inferred from context")],
            ),
        ],
    )

    result = score_spec(spec)
    grounding = result.dimensions["evidence_grounding"]

    # Floor is 0% for all-inference, coverage bonus is 0% (no well-grounded entities)
    # Expected: 0/30
    assert grounding.score <= 3, (
        f"Spec with only inference entities must score <= 3/30 on evidence_grounding, got {grounding.score}"
    )


def test_evidence_grounding_data_delta_reaches_top_band():
    """ABSOLUTE: A maximally-grounded spec with data-delta entities must reach top band (>= 27/30)."""
    spec = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status changed 'New' -> 'Working' at step 3")],
            ),
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority changed 'Low' -> 'High' at step 4")],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "a Case record must be identified")],
            ),
        ],
    )

    result = score_spec(spec)
    grounding = result.dimensions["evidence_grounding"]

    # Floor 50% (15) + coverage bonus 50% (15) = 30, but mandated recordId doesn't lower it
    # Expected: 30/30
    assert grounding.score >= 27, (
        f"Spec with all data-delta entities (except mandated recordId) must score >= 27/30, got {grounding.score}"
    )


def test_evidence_grounding_dimension_max_is_exactly_30():
    """ABSOLUTE: The evidence_grounding dimension max must be exactly 30."""
    spec = _make_spec()
    result = score_spec(spec)
    grounding = result.dimensions["evidence_grounding"]

    assert grounding.max_score == 30, f"evidence_grounding max_score must be 30, got {grounding.max_score}"


def test_evidence_grounding_never_exceeds_30():
    """ABSOLUTE: No spec can score > 30 on evidence_grounding, no matter how it's constructed."""
    # Fuzz with various configurations
    configs = [
        # Many data-delta entities
        _make_spec(
            objects_touched=["Case"],
            entities=[
                DerivedEntity(
                    name=f"field_{i}",
                    object_api_name="Case",
                    field_api_name=f"Field{i}__c",
                    evidence=[SpecEvidence("data-delta", f"Case.Field{i}__c observed")],
                )
                for i in range(20)
            ],
        ),
        # Mixed evidence sources
        _make_spec(
            objects_touched=["Case"],
            entities=[
                DerivedEntity(
                    name=f"dd_{i}",
                    object_api_name="Case",
                    field_api_name=f"DD{i}__c",
                    evidence=[SpecEvidence("data-delta", f"observed {i}")],
                )
                for i in range(10)
            ] + [
                DerivedEntity(
                    name=f"ui_{i}",
                    object_api_name="Case",
                    field_api_name=f"UI{i}__c",
                    evidence=[SpecEvidence("ui-action", f"clicked {i}")],
                )
                for i in range(10)
            ],
        ),
    ]

    for spec in configs:
        result = score_spec(spec)
        grounding = result.dimensions["evidence_grounding"]

        assert 0 <= grounding.score <= 30, (
            f"evidence_grounding score {grounding.score} outside valid range [0, 30]"
        )


# === Provenance kwarg tests ===

def test_provenance_kwarg_both_real():
    """Provenance supplied with both axes real -> 5/5, no blocker."""
    spec = _make_spec()
    provenance = {"extraction_source": "dom-capture", "telemetry_source": "live-org"}

    result = score_spec(spec, provenance=provenance)

    prov_dim = result.dimensions["provenance_integrity"]
    assert prov_dim.score == 5, f"Expected 5/5 for real provenance, got {prov_dim.score}"
    assert len(result.blocking_issues) == 0, (
        f"Expected no blocking issues for real provenance, got: {result.blocking_issues}"
    )


def test_provenance_kwarg_extraction_not_real():
    """Provenance with extraction_source not real -> 0/5 + blocker + passed=False."""
    spec = _make_spec()
    provenance = {"extraction_source": "stub", "telemetry_source": "live-org"}

    result = score_spec(spec, provenance=provenance)

    prov_dim = result.dimensions["provenance_integrity"]
    assert prov_dim.score == 0, f"Expected 0/5 for stub extraction, got {prov_dim.score}"
    assert len(result.blocking_issues) > 0, "Expected blocking issue for stub extraction"
    assert any("stub" in issue.lower() for issue in result.blocking_issues)
    assert not result.passed, "Spec with stub extraction must not pass"


def test_provenance_kwarg_telemetry_not_real():
    """Provenance with telemetry_source not real -> 0/5 + blocker + passed=False."""
    spec = _make_spec()
    provenance = {"extraction_source": "dom-capture", "telemetry_source": "mock"}

    result = score_spec(spec, provenance=provenance)

    prov_dim = result.dimensions["provenance_integrity"]
    assert prov_dim.score == 0, f"Expected 0/5 for mock telemetry, got {prov_dim.score}"
    assert len(result.blocking_issues) > 0, "Expected blocking issue for mock telemetry"
    assert any("mock" in issue.lower() for issue in result.blocking_issues)
    assert not result.passed, "Spec with mock telemetry must not pass"


def test_provenance_kwarg_none():
    """Provenance not supplied (None) -> 0/5 with explanatory finding, NO blocker."""
    spec = _make_spec()

    result = score_spec(spec, provenance=None)

    prov_dim = result.dimensions["provenance_integrity"]
    assert prov_dim.score == 0, f"Expected 0/5 when provenance=None, got {prov_dim.score}"
    assert len(prov_dim.findings) > 0, "Expected finding explaining provenance not supplied"
    # No blocking issue should mention provenance when it's simply not supplied
    provenance_blockers = [issue for issue in result.blocking_issues if "provenance" in issue.lower()]
    assert len(provenance_blockers) == 0, (
        f"Expected no provenance-related blocker when provenance=None, got: {provenance_blockers}"
    )


# === C12: Mandated-only branch must not reward deleting an inference entity ===

def test_c12_deleting_inference_entity_down_to_mandated_recordid_never_raises_score():
    """C12 REGRESSION: G1 violation via the mandated-only branch.

    Pre-fix: a spec with one honestly-labelled inference-only observed entity
    (customer_name on Contact.Name) plus the mandated recordId scored
    evidence_grounding=0 (all-inference path, floor 0% + coverage 0%). Deleting
    the inference entity left ONLY the mandated recordId, sending the scorer
    into the `if total_entities == 0` branch, which returned max_score // 4 = 7.

    That is a G1 violation: deletion of the honestly-labelled inference entity
    raised evidence_grounding by 7. It also crossed
    HOLLOW_DIMENSION_FRACTION * 30 = 3, so the hollow-dimension blocker that
    fired at 0/30 stopped firing at 7/30 — deletion additionally REMOVED a
    blocking issue.

    This is the exact "concealing beats declaring" inversion the C5 fix
    already removed from the ratio arithmetic, smuggled back in through the
    mandated-only fallback.

    Fix: the mandated-only branch must score no higher than an all-inference
    observed spec (which scores 0). This test pins both invariants:
      1. evidence_grounding does not RISE on deletion,
      2. the hollow-dimension blocker does not DISAPPEAR on deletion.
    """
    spec_before = DerivedAgentSpec(
        intent="Look up customer for case",
        confidence=0.5,
        objects_touched=["Case", "Contact"],
        entities=[
            DerivedEntity(
                name="customer_name",
                object_api_name="Contact",
                field_api_name="Name",
                evidence=[SpecEvidence(
                    "inference",
                    "customer name is likely needed for the process",
                )],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence(
                    "inference",
                    "a Case record must be identified",
                )],
            ),
        ],
        orchestration_steps=[
            "Resolve and load the target Case record",
            "SUBMIT on button:Save -> writes Status",
        ],
        guardrails=["Require explicit user confirmation before writing: Status."],
        failure_handling=[
            "Observed validation failure during recording: Status must be one of approved values",
        ],
        unknowns=[],
        evidence=[SpecEvidence("telemetry", "validation observed")],
    )

    # Delete the inference entity, leaving only the mandated recordId.
    spec_after = DerivedAgentSpec(
        intent=spec_before.intent,
        confidence=spec_before.confidence,
        objects_touched=spec_before.objects_touched,
        entities=[
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence(
                    "inference",
                    "a Case record must be identified",
                )],
            ),
        ],
        orchestration_steps=list(spec_before.orchestration_steps),
        guardrails=list(spec_before.guardrails),
        failure_handling=list(spec_before.failure_handling),
        unknowns=list(spec_before.unknowns),
        evidence=list(spec_before.evidence),
    )

    score_before = score_spec(spec_before)
    score_after = score_spec(spec_after)

    grounding_before = score_before.dimensions["evidence_grounding"].score
    grounding_after = score_after.dimensions["evidence_grounding"].score

    # (1) Deletion must not RAISE evidence_grounding.
    assert grounding_after <= grounding_before, (
        f"C12 VIOLATED: Deleting the honestly-labelled inference entity raised "
        f"evidence_grounding from {grounding_before}/30 to {grounding_after}/30. "
        "Deletion of an honestly-declared inference row must never pay."
    )

    # (2) Deletion must not remove the hollow-dimension blocker on
    #     evidence_grounding. If the pre-deletion spec was blocked as hollow on
    #     evidence_grounding (score <= 10% of 30 = 3), the post-deletion spec
    #     must be at least as hollow.
    def _hollow_on_grounding(result):
        return any(
            "Hollow dimension" in issue and "evidence_grounding" in issue
            for issue in result.blocking_issues
        )

    if _hollow_on_grounding(score_before):
        assert _hollow_on_grounding(score_after), (
            "C12 VIOLATED: Pre-deletion spec was blocked as hollow on "
            "evidence_grounding; post-deletion spec no longer trips the "
            "hollow-dimension blocker. Deletion must not clear a blocking issue."
        )

    # (3) Belt-and-braces: total score must also not rise.
    assert score_after.total <= score_before.total, (
        f"C12 VIOLATED: Total rose on deletion, "
        f"{score_before.total} -> {score_after.total}."
    )


# === D2 COMPLETENESS: mandated-only branch must not reward deletion ===

def test_d2_deleting_declared_inference_into_mandated_only_branch_never_raises_score():
    """D2 COMPLETENESS: Deleting an honestly-declared inference entity, so that only
    the mandated recordId remains, must not raise evidence_grounding.

    The counter-example: a spec with entities=[recordId(inference), priority(inference)]
    is scored via the all-inference observed-entity path -> 0/30. Deleting `priority`
    used to fall into the `if total_entities == 0:` shortcut and jump to
    max_score // 4 = 7/30. That paid +7 for concealment and symmetrically cost 7
    for honestly declaring an inference — the "concealing beats declaring" inversion
    the C5 comment in spec_score.py names as the worst possible outcome.
    """
    spec_declared = DerivedAgentSpec(
        intent="Update Case",
        confidence=0.5,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "a Case record must be identified to act on it")],
            ),
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("inference", "priority may be adjusted during triage based on customer type")],
            ),
        ],
        orchestration_steps=[
            "Resolve and load the target Case record",
            "SUBMIT on button:Save -> writes Priority",
        ],
        guardrails=["Enforce object- and field-level security on Case for the running user."],
        failure_handling=["No failures were observed in this run, so error paths are UNTESTED."],
        unknowns=[],
        evidence=[SpecEvidence("extraction", "2 action(s) in recording")],
    )

    spec_deleted = DerivedAgentSpec(
        intent="Update Case",
        confidence=0.5,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "a Case record must be identified to act on it")],
            ),
        ],
        orchestration_steps=[
            "Resolve and load the target Case record",
            "SUBMIT on button:Save -> writes Priority",
        ],
        guardrails=["Enforce object- and field-level security on Case for the running user."],
        failure_handling=["No failures were observed in this run, so error paths are UNTESTED."],
        unknowns=[],
        evidence=[SpecEvidence("extraction", "2 action(s) in recording")],
    )

    score_declared = score_spec(spec_declared)
    score_deleted = score_spec(spec_deleted)

    declared_grounding = score_declared.dimensions["evidence_grounding"].score
    deleted_grounding = score_deleted.dimensions["evidence_grounding"].score

    # (1) The dimension score cannot rise on deletion.
    assert deleted_grounding <= declared_grounding, (
        f"D2 COMPLETENESS VIOLATED: Deleting the honestly-declared inference entity "
        f"raised evidence_grounding from {declared_grounding} to {deleted_grounding}. "
        "The `if total_entities == 0:` mandated-only shortcut still pays for concealment."
    )

    # (2) The total also cannot rise on deletion.
    assert score_deleted.total <= score_declared.total, (
        f"D2 COMPLETENESS VIOLATED: Deletion raised total {score_declared.total} -> {score_deleted.total}."
    )

    # (3) Concrete pin: the pre-fix behaviour was exactly declared=0, deleted=7.
    # After the fix both must be 0, so declaring an inference costs nothing on grounding.
    assert declared_grounding == 0, (
        f"D2 setup precondition: declared-inference spec must score 0 on grounding, "
        f"got {declared_grounding}."
    )
    assert deleted_grounding == 0, (
        f"D2 COMPLETENESS VIOLATED: Mandated-only branch returned {deleted_grounding}, "
        "should be 0. Otherwise deletion is rewarded on this branch."
    )


def test_d2_mandated_filter_uses_semantic_all_inference_not_list_identity():
    """D2 SECONDARY: The mandated-recordId filter must treat a recordId whose EVERY
    evidence entry is inference as mandated, regardless of how many inference entries
    it carries. The previous `sources == ['inference']` list-identity check let a
    recordId with two inference evidence entries escape the filter and be scored as
    an observed entity — so "mandated" was defined by list length, not semantics.

    Behavioural pin: a spec whose ONLY entity is a recordId with two inference
    entries must be indistinguishable, on evidence_grounding, from a spec whose
    ONLY entity is a recordId with one inference entry. Both are the mandated-only
    case and must score identically (0/30 after the D2 fix).
    """
    spec_single = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "a Case record must be identified to act on it")],
            ),
        ],
    )
    spec_multi = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[
                    SpecEvidence("inference", "a Case record must be identified to act on it"),
                    SpecEvidence("inference", "the record id anchors the write target for downstream steps"),
                ],
            ),
        ],
    )

    g_single = score_spec(spec_single).dimensions["evidence_grounding"].score
    g_multi = score_spec(spec_multi).dimensions["evidence_grounding"].score

    assert g_single == g_multi, (
        f"D2 SECONDARY VIOLATED: recordId with a single inference entry scored "
        f"{g_single}, with two inference entries scored {g_multi}. The mandated "
        "filter must be defined by evidence semantics ('all sources are inference'), "
        "not by list length."
    )


def test_d2_anti_gaming_subject_inference_deletion_never_pays_seam_check():
    """D2 ANTI-GAMING: pin the exact counter-example from the D2-followup review.

    Claim to disprove: 'When only the mandated recordId remains (total_entities==0),
    score is max_score//4 = 7. When one all-inference observed entity is added,
    floor_pct=0 and coverage_bonus=0, so score is 0. Deleting the honest inference
    entity therefore raises the score from 0 to 7.'

    Since `spec_builder` emits `source='inference'` for asserted/ambiguous inputs
    (lines ~292/300 at the time of writing), teaching the refinement loop that
    deleting those inference rows PAYS +7 is the same 'concealing beats declaring'
    inversion the C5 comment in spec_score.py names as the worst possible outcome.

    This test pins:
      1. deleting the honest inference `subject` entity does NOT raise
         evidence_grounding;
      2. it does NOT raise the total either;
      3. it does NOT remove a hollow-dimension blocker on evidence_grounding.

    Uses the review's exact entity shapes (recordId + subject inference detail)
    verbatim, so the two branches at the seam (`total_entities==0` and
    `total_entities>0, all-inference`) are compared on the exact input the review
    used to construct the inversion.
    """
    with_subject = DerivedAgentSpec(
        intent="Update Case subject",
        confidence=0.5,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence(
                    "inference",
                    "a Case record must be identified to act on it",
                )],
            ),
            DerivedEntity(
                name="subject",
                object_api_name="Case",
                field_api_name="Subject",
                evidence=[SpecEvidence(
                    "inference",
                    "input observed at step-005; no data delta could be resolved within window",
                )],
            ),
        ],
        orchestration_steps=[
            "Resolve and load the target Case record",
            "SUBMIT on button:Save -> writes Subject",
        ],
        guardrails=["Require explicit user confirmation before writing: Subject."],
        failure_handling=["Observed validation failure during recording: Subject required"],
        unknowns=[],
        evidence=[SpecEvidence("extraction", "2 action(s) in recording")],
    )

    # Delete the honestly-declared subject inference entity, leaving only the
    # mandated recordId. This is the exact deletion the review claimed pays +7.
    only_mandated = DerivedAgentSpec(
        intent=with_subject.intent,
        confidence=with_subject.confidence,
        objects_touched=list(with_subject.objects_touched),
        entities=[
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence(
                    "inference",
                    "a Case record must be identified to act on it",
                )],
            ),
        ],
        orchestration_steps=list(with_subject.orchestration_steps),
        guardrails=list(with_subject.guardrails),
        failure_handling=list(with_subject.failure_handling),
        unknowns=list(with_subject.unknowns),
        evidence=list(with_subject.evidence),
    )

    score_with = score_spec(with_subject)
    score_deleted = score_spec(only_mandated)

    grounding_with = score_with.dimensions["evidence_grounding"].score
    grounding_deleted = score_deleted.dimensions["evidence_grounding"].score

    # (1) The seam invariant: crossing between the `total_entities==0` shortcut and
    # the all-inference general path must not reward deletion.
    assert grounding_deleted <= grounding_with, (
        f"D2 ANTI-GAMING VIOLATED: deleting the honest inference `subject` entity "
        f"raised evidence_grounding {grounding_with}/30 -> {grounding_deleted}/30. "
        "The mandated-only shortcut is again paying for concealing an "
        "honestly-declared inference row — the exact 'concealing beats declaring' "
        "inversion the C5 comment forbids."
    )

    # (2) Total must not rise either — a rise on the seam can leak into the total
    # even if the dimension is separately capped.
    assert score_deleted.total <= score_with.total, (
        f"D2 ANTI-GAMING VIOLATED: deleting the honest inference `subject` entity "
        f"raised total {score_with.total} -> {score_deleted.total}."
    )

    # (3) Hollow-dimension blocker preservation. If the pre-deletion spec was
    # blocked as hollow on evidence_grounding, deletion must NOT clear that
    # blocker — otherwise the fabricator gains a strictly-better outcome than the
    # score alone reveals (fewer blocking issues => higher band, potentially
    # passed=True even if the raw number is unchanged).
    def _hollow_on_grounding(result):
        return any(
            "Hollow dimension" in issue and "evidence_grounding" in issue
            for issue in result.blocking_issues
        )

    if _hollow_on_grounding(score_with):
        assert _hollow_on_grounding(score_deleted), (
            "D2 ANTI-GAMING VIOLATED: pre-deletion spec was blocked as hollow on "
            "evidence_grounding; post-deletion spec no longer trips the "
            "hollow-dimension blocker. Deletion silently cleared a blocker."
        )

    # (4) Concrete pins on the review's numeric claim. Both branches at the seam
    # must be exactly 0/30 — not 7 in one and 0 in the other. If a future edit
    # restores `max_score // 4` on the mandated-only branch, this line fires.
    assert grounding_with == 0, (
        f"Setup precondition: all-inference (recordId + subject inference) must "
        f"score 0/30 on evidence_grounding, got {grounding_with}."
    )
    assert grounding_deleted == 0, (
        f"D2 ANTI-GAMING VIOLATED: mandated-only branch scored {grounding_deleted}/30, "
        "must be 0 to match the all-inference general path. Any positive value "
        "on this branch re-establishes the +7 inversion the review described."
    )
