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
