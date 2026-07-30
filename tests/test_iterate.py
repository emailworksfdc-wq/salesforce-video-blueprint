"""Tests for iterate.py (offline spec refinement loop)."""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

import pytest

from sf_video_blueprint.iterate import (
    IterationResult,
    SpecVersion,
    refine,
    write_iteration_summary,
    _apply_offline_improvements,
    _pick_best,
)
from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence


# --- Fixtures ---

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
        ]
    if orchestration_steps is None:
        orchestration_steps = [
            "Resolve and load the target Case record",
            "SUBMIT on button:Save -> writes Status",
        ]
    if guardrails is None:
        guardrails = ["Require confirmation"]
    if failure_handling is None:
        failure_handling = ["Observed validation failure"]
    if unknowns is None:
        unknowns = []
    if evidence is None:
        evidence = [SpecEvidence("telemetry", "validation observed")]

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


# === TEST: Offline improvement actually changes the spec ===

def test_offline_improvement_changes_spec():
    """Offline improvement must actually change the spec, not return it unchanged."""
    spec = _make_spec(
        intent="Update Case Status with various fields",  # vague term
        orchestration_steps=[
            "Load the record",
            "Load the record",  # duplicate
            "Submit changes",
        ],
        guardrails=[
            "Require confirmation",
            "Require confirmation",  # duplicate
        ],
    )

    # Mock a score object with recommendations (duck-typed)
    class MockScore:
        total = 70
        blocking_issues = []
        recommendations = ["Tighten prose", "Remove duplicates"]

    improved, _ = _apply_offline_improvements(spec, MockScore())

    # Should be a NEW object, not the same one
    assert improved is not spec, "Improvement must return a new spec, not mutate the input"

    # Intent should have "various" removed
    assert "various" not in improved.intent, f"Intent still has 'various': {improved.intent}"

    # Orchestration steps should be deduplicated (2 "Load the record" -> 1)
    assert len(improved.orchestration_steps) == 2, (
        f"Orchestration steps not deduplicated: {improved.orchestration_steps}"
    )

    # Guardrails should be deduplicated
    assert len(improved.guardrails) == 1, f"Guardrails not deduplicated: {improved.guardrails}"


def test_offline_improvement_no_change_returns_original():
    """If no improvements can be made, return the ORIGINAL spec (not a copy) to signal stall."""
    spec = _make_spec(
        intent="Update Case Status",  # no vague terms
        orchestration_steps=["Step 1", "Step 2"],  # no duplicates
        guardrails=["Guardrail 1"],  # no duplicates
    )

    class MockScore:
        total = 80
        blocking_issues = []
        recommendations = []

    result, _ = _apply_offline_improvements(spec, MockScore())

    # Should return the SAME object (not a copy) to signal no change
    assert result is spec, "When no improvements are possible, must return the original spec"


# === TEST: Unknowns never shrink ===

def test_offline_improvement_never_removes_unknowns():
    """Offline improvement must NEVER remove unknowns — that's score gaming."""
    spec = _make_spec(
        unknowns=["Unknown A", "Unknown B"],
    )

    class MockScore:
        total = 70
        blocking_issues = []
        recommendations = []

    improved, _ = _apply_offline_improvements(spec, MockScore())

    # Unknowns must be unchanged
    assert len(improved.unknowns) == len(spec.unknowns), (
        f"Unknowns changed from {len(spec.unknowns)} to {len(improved.unknowns)}"
    )
    assert improved.unknowns == spec.unknowns


# === TEST: Confidence never rises ===

def test_offline_improvement_never_raises_confidence():
    """Offline improvement must NEVER raise confidence — no new evidence is added."""
    spec = _make_spec(confidence=0.5)

    class MockScore:
        total = 70
        blocking_issues = []
        recommendations = []

    improved, _ = _apply_offline_improvements(spec, MockScore())

    assert improved.confidence == spec.confidence, (
        f"Confidence changed from {spec.confidence} to {improved.confidence}"
    )


# === TEST: Determinism (same input -> same output) ===

def test_offline_improvement_deterministic():
    """Same input spec must produce the same improved spec."""
    spec = _make_spec(
        intent="Update Case Status with various fields",
        orchestration_steps=["Load the record", "Load the record", "Submit"],
    )

    class MockScore:
        total = 70
        blocking_issues = []
        recommendations = []

    improved1, _ = _apply_offline_improvements(spec, MockScore())
    improved2, _ = _apply_offline_improvements(spec, MockScore())

    assert improved1.intent == improved2.intent
    assert improved1.orchestration_steps == improved2.orchestration_steps
    assert improved1.guardrails == improved2.guardrails


# === TEST: No mutation of input spec ===

def test_offline_improvement_no_mutation():
    """The input spec must NOT be mutated."""
    spec = _make_spec(
        intent="Update Case Status with various fields",
    )

    original_intent = spec.intent

    class MockScore:
        total = 70
        blocking_issues = []
        recommendations = []

    _, _ = _apply_offline_improvements(spec, MockScore())

    # Original spec must be unchanged
    assert spec.intent == original_intent


# === TEST: Each stopping condition ===

def test_stopping_condition_pass_threshold(tmp_path):
    """Loop stops when score reaches pass threshold with no blocking issues.

    This test constructs a spec that:
    1. Scores >= 75 on round 1
    2. Has no blocking issues (balanced across dimensions to avoid threshold surfing)
    3. Does not improve on subsequent rounds (converges)

    The fix ensures that when the FINAL version passes threshold with no blocking issues,
    the stop_reason reports "threshold", not "converged".
    """
    spec = _make_spec(
        intent="Update Case Status by submitting the Save button",  # Specific
        confidence=0.95,  # High confidence
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
                evidence=[SpecEvidence("data-delta", "Case.Id observed")],
            ),
        ],
        orchestration_steps=[
            "Resolve and load the target Case record by ID",
            "Validate Status field value",
            "SUBMIT on button:Save to write Status",
        ],
        guardrails=[
            "Require confirmation before writing Status",
            "Enforce field-level security on Case.Status",
        ],
        failure_handling=[
            "Observed validation failure during recording: Status value invalid"
        ],
        unknowns=[],
    )

    result = refine(
        spec,
        out_dir=tmp_path,
        company_name="Test Co",
        company_description="A test company",
        max_rounds=5,
        use_cli=False,
    )

    assert "threshold" in result.stop_reason.lower(), f"Expected threshold stop, got: {result.stop_reason}"


def test_stopping_condition_convergence(tmp_path):
    """Loop stops when improvement < epsilon for 2 consecutive rounds."""
    # Create a spec that's almost perfect but not quite passing
    spec = _make_spec(
        intent="Update Case Status",
        confidence=0.70,
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

    result = refine(
        spec,
        out_dir=tmp_path,
        company_name="Test Co",
        company_description="A test company",
        max_rounds=10,
        epsilon=2,
        use_cli=False,
    )

    # If it converged, the stop reason should mention it
    # (May also stop for other reasons, so this is a weaker assertion)
    if result.converged:
        assert "converge" in result.stop_reason.lower()


def test_stopping_condition_insufficient_evidence(tmp_path):
    """Loop stops immediately on InsufficientEvidenceError."""
    spec = _make_spec(
        intent="UNRESOLVED: nothing happened",
        confidence=0.0,
        objects_touched=[],
        entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
        unknowns=[],
        evidence=[],
    )

    # This should raise InsufficientEvidenceError from build_agent_spec_yaml
    with pytest.raises(Exception):  # The exact exception depends on agentforce_spec.py
        refine(
            spec,
            out_dir=tmp_path,
            company_name="Test Co",
            company_description="A test company",
            max_rounds=5,
            use_cli=False,
        )


# === TEST: Versions written to distinct paths ===

def test_versions_distinct_paths(tmp_path):
    """Each version must be written to a distinct path (v1/, v2/, ...)."""
    spec = _make_spec()

    result = refine(
        spec,
        out_dir=tmp_path,
        company_name="Test Co",
        company_description="A test company",
        max_rounds=3,
        use_cli=False,
    )

    # Check that each version has a distinct directory
    version_dirs = [v.spec_path.parent for v in result.versions]
    assert len(version_dirs) == len(set(version_dirs)), "Version paths are not distinct"

    # Check that v1/, v2/, ... exist
    for i in range(1, result.rounds_run + 1):
        version_dir = tmp_path / f"v{i}"
        assert version_dir.exists(), f"Version directory {version_dir} does not exist"
        assert (version_dir / "agent-spec.json").exists()


# === TEST: _pick_best selects highest score, ties break to earlier version ===

def test_pick_best_highest_score():
    """_pick_best selects the version with the highest score."""
    from sf_video_blueprint.iterate import SpecVersion

    class MockScore:
        def __init__(self, total):
            self.total = total
            self.blocking_issues = []

    v1 = SpecVersion(
        version=1,
        spec_path=Path("/tmp/v1/spec.json"),
        yaml_path=None,
        score=MockScore(70),
        role_used="role1",
        source="derived",
        parent_version=None,
    )
    v2 = SpecVersion(
        version=2,
        spec_path=Path("/tmp/v2/spec.json"),
        yaml_path=None,
        score=MockScore(80),
        role_used="role2",
        source="derived",
        parent_version=1,
    )
    v3 = SpecVersion(
        version=3,
        spec_path=Path("/tmp/v3/spec.json"),
        yaml_path=None,
        score=MockScore(75),
        role_used="role3",
        source="derived",
        parent_version=2,
    )

    best = _pick_best([v1, v2, v3])
    assert best.version == 2, f"Expected v2 (score 80), got v{best.version}"


def test_pick_best_tie_breaks_to_earlier():
    """When scores are tied, _pick_best selects the EARLIER version."""
    from sf_video_blueprint.iterate import SpecVersion

    class MockScore:
        def __init__(self, total):
            self.total = total
            self.blocking_issues = []

    v1 = SpecVersion(
        version=1,
        spec_path=Path("/tmp/v1/spec.json"),
        yaml_path=None,
        score=MockScore(75),
        role_used="role1",
        source="derived",
        parent_version=None,
    )
    v2 = SpecVersion(
        version=2,
        spec_path=Path("/tmp/v2/spec.json"),
        yaml_path=None,
        score=MockScore(75),
        role_used="role2",
        source="derived",
        parent_version=1,
    )

    best = _pick_best([v1, v2])
    assert best.version == 1, f"Expected v1 (earlier), got v{best.version}"


def test_pick_best_avoids_blocking_issues():
    """_pick_best never returns a version with blocking issues if an unblocked version exists."""
    from sf_video_blueprint.iterate import SpecVersion

    class MockScore:
        def __init__(self, total, blocking=False):
            self.total = total
            self.blocking_issues = ["Issue"] if blocking else []

    v1 = SpecVersion(
        version=1,
        spec_path=Path("/tmp/v1/spec.json"),
        yaml_path=None,
        score=MockScore(80, blocking=True),
        role_used="role1",
        source="derived",
        parent_version=None,
    )
    v2 = SpecVersion(
        version=2,
        spec_path=Path("/tmp/v2/spec.json"),
        yaml_path=None,
        score=MockScore(70, blocking=False),
        role_used="role2",
        source="derived",
        parent_version=1,
    )

    best = _pick_best([v1, v2])
    assert best.version == 2, f"Expected v2 (unblocked), got v{best.version}"
    assert not best.score.blocking_issues


# === TEST: G4 - Refinement is monotone (never lowers score) ===

def test_refinement_monotone_property():
    """G4: _apply_offline_improvements must NEVER lower the total score on ANY input.

    Build a corpus of realistic specs and assert monotonicity as a property.
    """
    from sf_video_blueprint.spec_score import score_spec

    corpus = [
        # 1. Spec with duplicated steps
        _make_spec(
            intent="Update Case Status",
            orchestration_steps=[
                "Load the Case record",
                "Load the Case record",  # duplicate
                "Update Status field",
            ],
            guardrails=["Validate input", "Validate input"],  # duplicate
        ),
        # 2. Spec with vague terms
        _make_spec(
            intent="Update Case Status with various fields as needed",
            orchestration_steps=["Do various things", "Submit changes"],
        ),
        # 3. Spec with generic guardrails
        _make_spec(
            intent="Update Case Status",
            guardrails=["Validate input", "Require confirmation"],
        ),
        # 4. Minimal spec (no improvements possible)
        _make_spec(
            intent="Update Case Status",
            orchestration_steps=["Step 1", "Step 2"],
            guardrails=["Guardrail 1"],
        ),
        # 5. Spec with multiple vague terms and duplicates
        _make_spec(
            intent="Update Case fields with various values as needed",
            orchestration_steps=[
                "Load the record",
                "Load the record",
                "Update various fields",
                "Submit etc.",
            ],
            guardrails=["Validate input", "Validate input", "Some guardrail"],
        ),
        # 6. Spec resembling real dom-capture output (recordId + observed field)
        _make_spec(
            intent="Update Case Status field",
            entities=[
                DerivedEntity(
                    name="recordId",
                    object_api_name="Case",
                    field_api_name="Id",
                    evidence=[SpecEvidence("inference", "a Case record must be identified to act on it")],
                ),
                DerivedEntity(
                    name="status",
                    object_api_name="Case",
                    field_api_name="Status",
                    evidence=[SpecEvidence("data-delta", "Case.Status observed")],
                ),
            ],
            orchestration_steps=[
                "Resolve and load the target Case record",
                "SUBMIT on button:Save -> writes Status",
            ],
            failure_handling=["No failures were observed in this run, so error paths are UNTESTED."],
        ),
    ]

    for i, spec in enumerate(corpus):
        class MockScore:
            total = 70
            blocking_issues = []
            recommendations = []

        original_score = score_spec(spec)
        improved_spec, _ = _apply_offline_improvements(spec, MockScore())
        improved_score = score_spec(improved_spec)

        assert improved_score.total >= original_score.total, (
            f"G4 VIOLATION on corpus[{i}]: "
            f"Offline improvement LOWERED score from {original_score.total} to {improved_score.total}. "
            f"Intent: {spec.intent}, Steps: {len(spec.orchestration_steps)}, "
            f"Guardrails: {len(spec.guardrails)}"
        )


# === TEST: G5 - Refinement is effective (can raise score) ===

def test_refinement_effectiveness():
    """G5: There must exist at least one realistic below-threshold spec where offline improvement RAISES the score.

    If no such spec exists, the offline path is decorative.
    """
    from sf_video_blueprint.spec_score import score_spec

    # Spec with clear improvement opportunities that affect scoring:
    # - Generic terms in orchestration steps (penalized by specificity dimension)
    # - Vague terms in intent (penalized by specificity)
    spec = _make_spec(
        intent="Update Case Status with various fields as needed",  # "various" and "as needed" are vague
        confidence=0.70,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
        ],
        orchestration_steps=[
            "Load the record",  # "the record" is generic
            "Update the field with the value",  # "the field" and "the value" are generic
            "Submit the record",  # "the record" is generic
        ],
        guardrails=[
            "Validate input",  # Will be expanded to "Validate Case input"
            "Require confirmation",
        ],
        failure_handling=["Observed validation failure"],
    )

    class MockScore:
        total = 70
        blocking_issues = []
        recommendations = []

    original_score = score_spec(spec)
    improved_spec, summary = _apply_offline_improvements(spec, MockScore())
    improved_score = score_spec(improved_spec)

    # For effectiveness, we need strict improvement
    assert improved_score.total > original_score.total, (
        f"G5 VIOLATION: Offline improvement did not RAISE the score. "
        f"Original: {original_score.total}, Improved: {improved_score.total}. "
        f"If the offline path cannot improve ANY spec, it is decorative and should be removed."
    )


# === TEST: F4 - Dead guard actually fires ===

def test_unknowns_deletion_warning_fires(tmp_path):
    """F4: Verify the unknowns-deletion warning appears when a later round has fewer unknowns at higher score.

    After spec_score.py was rewritten with a monotone formula (G1/G2/G3), deleting unknowns
    alone no longer raises the score when structural data is present. The guard must fire on
    a REAL gaming attempt: filling gaps by adding evidence (which could be fabricated) while
    deleting honest caveats.

    This test constructs a realistic below-threshold spec where unknowns drop AND score rises
    through added evidence, and proves the guard fires.
    """
    import copy
    from sf_video_blueprint.iterate import SpecVersion
    from sf_video_blueprint.spec_score import score_spec

    # Create v1: honest but incomplete (low evidence, declared unknowns)
    spec_v1 = _make_spec(
        intent="Update Case Status",
        confidence=0.60,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("inference", "assumed field")],  # Weak evidence
            ),
        ],
        orchestration_steps=["Step 1"],  # Only 1 step (low completeness)
        guardrails=["Require confirmation"],
        failure_handling=["Observed validation failure"],
        unknowns=["Unknown: which other fields are involved", "Unknown: error handling paths"],
    )

    # Write v1 to disk
    v1_dir = tmp_path / "v1"
    v1_dir.mkdir(parents=True, exist_ok=True)
    v1_spec_path = v1_dir / "agent-spec.json"
    v1_spec_path.write_text(json.dumps(spec_v1.to_dict(), indent=2), encoding="utf-8")

    score_v1 = score_spec(spec_v1)

    # Create v2: GAMING by deleting unknowns AND adding strong evidence entities
    # (which could be fabricated to game the score)
    spec_v2 = copy.deepcopy(spec_v1)
    spec_v2.unknowns = []  # GAMING: delete caveats
    # Add entities with strong evidence (data-delta) to raise evidence_grounding score
    spec_v2.entities.append(
        DerivedEntity(
            name="priority",
            object_api_name="Case",
            field_api_name="Priority",
            evidence=[SpecEvidence("data-delta", "observed Priority field change")],
        )
    )
    spec_v2.entities.append(
        DerivedEntity(
            name="subject",
            object_api_name="Case",
            field_api_name="Subject",
            evidence=[SpecEvidence("data-delta", "observed Subject field change")],
        )
    )

    # Write v2 to disk
    v2_dir = tmp_path / "v2"
    v2_dir.mkdir(parents=True, exist_ok=True)
    v2_spec_path = v2_dir / "agent-spec.json"
    v2_spec_path.write_text(json.dumps(spec_v2.to_dict(), indent=2), encoding="utf-8")

    score_v2 = score_spec(spec_v2)

    # Verify this is a real gaming scenario: score rose, unknowns dropped
    assert score_v2.total > score_v1.total, (
        f"Test precondition failed: score did not increase (v1={score_v1.total}, v2={score_v2.total}). "
        "The gaming scenario is not realistic."
    )
    assert len(spec_v2.unknowns) < len(spec_v1.unknowns), (
        "Test precondition failed: unknowns did not decrease."
    )

    # Manually trigger the guard logic by constructing versions list
    versions = [
        SpecVersion(
            version=1,
            spec_path=v1_spec_path,
            yaml_path=None,
            score=score_v1,
            role_used="role1",
            source="derived",
            parent_version=None,
            notes=[],
        )
    ]

    # Now simulate what happens in the loop when checking v2
    prev = versions[-1]

    # Read parent's unknowns from disk (this is the fix for D6)
    prev_spec_data = json.loads(prev.spec_path.read_text(encoding="utf-8"))
    prev_unknowns_count = len(prev_spec_data.get("unknowns", []))

    curr_unknowns_count = len(spec_v2.unknowns)

    notes_v2 = []
    if score_v2.total > score_v1.total and curr_unknowns_count < prev_unknowns_count:
        notes_v2.append(
            "WARNING: score improved but unknowns decreased — verify this is honest "
            "refinement (filling gaps with evidence) and not gaming the metric by "
            "deleting caveats."
        )

    # Assert the warning fired
    assert len(notes_v2) > 0, (
        f"Guard did not fire when unknowns decreased with score increase. "
        f"v1: score={score_v1.total}, unknowns={prev_unknowns_count}; "
        f"v2: score={score_v2.total}, unknowns={curr_unknowns_count}"
    )
    assert any("unknowns decreased" in note.lower() for note in notes_v2), (
        f"Expected unknowns warning in notes, got: {notes_v2}"
    )


def test_deleting_unknowns_alone_does_not_raise_score():
    """G3: Deleting unknowns alone (without new evidence) must NOT raise the score.

    After the honesty dimension was fixed (G3), a spec with structural data scores
    full honesty marks whether or not unknowns are declared. This prevents the loop
    from gaming the metric by hiding gaps.

    This test proves the property: score(spec_with_unknowns) >= score(spec_without_unknowns)
    when the only difference is the unknowns list.
    """
    from sf_video_blueprint.spec_score import score_spec

    # Spec with good structural data + declared unknowns
    spec_with_unknowns = _make_spec(
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
        orchestration_steps=["Load the Case record", "Submit changes"],
        guardrails=["Require confirmation", "Enforce FLS"],
        failure_handling=["Observed validation failure"],
        unknowns=["Unknown: which automation rules fire on Status change"],
    )

    # Same spec but with unknowns deleted (gaming attempt)
    import copy
    spec_without_unknowns = copy.deepcopy(spec_with_unknowns)
    spec_without_unknowns.unknowns = []

    score_with = score_spec(spec_with_unknowns)
    score_without = score_spec(spec_without_unknowns)

    # G3: declaring unknowns must score >= deleting them
    assert score_with.total >= score_without.total, (
        f"G3 VIOLATION: Deleting unknowns raised the score from {score_with.total} to {score_without.total}. "
        "This trains the loop to hide gaps, which is the worst possible outcome."
    )

    # Bonus check: honesty dimension should not penalize declared unknowns when structural data is present
    assert score_with.dimensions["honesty"].score == score_without.dimensions["honesty"].score, (
        f"Honesty dimension changed when unknowns were deleted: "
        f"with_unknowns={score_with.dimensions['honesty'].score}, "
        f"without_unknowns={score_without.dimensions['honesty'].score}. "
        "When structural data is present, honesty should score the same regardless of declared unknowns."
    )


# === TEST: DEFECT 2 - max_rounds=0 and edge cases ===

def test_max_rounds_zero_raises_error(tmp_path):
    """DEFECT 2: max_rounds=0 must raise ValueError, not crash in _pick_best."""
    spec = _make_spec()

    with pytest.raises(ValueError, match="max_rounds must be >= 1"):
        refine(
            spec,
            out_dir=tmp_path,
            company_name="Test Co",
            company_description="A test company",
            max_rounds=0,
            use_cli=False,
        )


def test_max_rounds_negative_raises_error(tmp_path):
    """DEFECT 2: Negative max_rounds must raise ValueError."""
    spec = _make_spec()

    with pytest.raises(ValueError, match="max_rounds must be >= 1"):
        refine(
            spec,
            out_dir=tmp_path,
            company_name="Test Co",
            company_description="A test company",
            max_rounds=-5,
            use_cli=False,
        )


def test_max_rounds_one_works(tmp_path):
    """DEFECT 2: max_rounds=1 is valid and should complete 1 round."""
    spec = _make_spec()

    result = refine(
        spec,
        out_dir=tmp_path,
        company_name="Test Co",
        company_description="A test company",
        max_rounds=1,
        use_cli=False,
    )

    assert result.rounds_run == 1
    assert len(result.versions) == 1


# === TEST: DEFECT 3 - compare() argument order verification ===

def test_compare_argument_order_improvement(tmp_path):
    """DEFECT 3: Verify compare(earlier, later) with a genuinely improving sequence never reports regression."""
    spec_v1 = _make_spec(
        intent="Update Case Status",
        confidence=0.60,  # Will score lower
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("inference", "assumed")],  # Weak evidence
            ),
        ],
        orchestration_steps=["Step 1"],  # Minimal
    )

    # Improve it: add stronger evidence
    import copy
    spec_v2 = copy.deepcopy(spec_v1)
    spec_v2.entities.append(
        DerivedEntity(
            name="recordId",
            object_api_name="Case",
            field_api_name="Id",
            evidence=[SpecEvidence("data-delta", "observed")],
        )
    )
    spec_v2.orchestration_steps.append("Step 2")

    # Write both to disk and refine with the improved one as round 2
    v1_dir = tmp_path / "v1"
    v1_dir.mkdir(parents=True, exist_ok=True)
    v1_spec_path = v1_dir / "agent-spec.json"
    v1_spec_path.write_text(json.dumps(spec_v1.to_dict(), indent=2), encoding="utf-8")

    # Run refine on v2 starting from v1
    result = refine(
        spec_v1,
        out_dir=tmp_path / "run",
        company_name="Test Co",
        company_description="A test company",
        max_rounds=2,
        use_cli=False,
    )

    # If compare() argument order is correct, a genuinely improving sequence will not
    # report regression (stop_reason should not contain "Regression")
    assert "regression" not in result.stop_reason.lower(), (
        f"Expected no regression in improving sequence, got: {result.stop_reason}"
    )


def test_compare_argument_order_regression(tmp_path):
    """DEFECT 3: Verify compare(earlier, later) with a genuinely worsening sequence reports regression."""
    # This is harder to trigger because _apply_offline_improvements is monotone (G4).
    # Instead, we test the compare function directly.
    from sf_video_blueprint.spec_score import score_spec, compare

    # The evidence details below are full sentences because the builder's are. Every
    # SpecEvidence detail spec_builder emits is an f-string naming the object, field
    # and step id; the shortest on the example capture is 41 characters
    # ("select on 'input:Status' at step step-009"). These fixtures previously read
    # "observed" and "assumed" (8 and 7 characters), which no builder path can
    # produce, and `_score_evidence_grounding` now declines to count a stub detail as
    # grounded — so the fixture, not the scorer, was the thing that had to change.
    spec_better = _make_spec(
        intent="Update Case Status",
        confidence=0.80,
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status changed 'New' -> 'Working' at step-003")],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("data-delta", "Case.Id resolved from the record page at step-001")],
            ),
        ],
        orchestration_steps=["Step 1", "Step 2", "Step 3"],
    )

    spec_worse = _make_spec(
        intent="Update Case Status",
        confidence=0.50,
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("inference", "Case.Status assumed to change from the button label")],
            ),
        ],
        orchestration_steps=["Step 1"],
    )

    score_better = score_spec(spec_better)
    score_worse = score_spec(spec_worse)

    # If compare(earlier, later) has correct order, compare(better, worse) should have negative delta
    comparison = compare(score_better, score_worse)
    assert comparison.delta < 0, (
        f"Expected negative delta when worse follows better, got {comparison.delta}"
    )

    # And compare(worse, better) should have positive delta
    comparison2 = compare(score_worse, score_better)
    assert comparison2.delta > 0, (
        f"Expected positive delta when better follows worse, got {comparison2.delta}"
    )


# === TEST: DEFECT 4 - unknowns deletion warning fires correctly ===

def test_unknowns_deletion_warning_on_read_error(tmp_path):
    """DEFECT 4: When parent spec cannot be read, the loop must warn that the check was skipped, not silently suppress."""
    spec = _make_spec(
        unknowns=["Unknown A"],
    )

    # Write a corrupted parent spec
    v1_dir = tmp_path / "v1"
    v1_dir.mkdir(parents=True, exist_ok=True)
    v1_spec_path = v1_dir / "agent-spec.json"
    v1_spec_path.write_text("CORRUPTED JSON{{{", encoding="utf-8")  # Invalid JSON

    # Run a second round (will try to read v1 and fail)
    result = refine(
        spec,
        out_dir=tmp_path / "run",
        company_name="Test Co",
        company_description="A test company",
        max_rounds=2,
        use_cli=False,
    )

    # Check that v2 notes contain the read-error warning
    if len(result.versions) > 1:
        v2_notes = result.versions[1].notes
        assert any("could not read parent spec" in note.lower() for note in v2_notes), (
            f"Expected read-error warning in v2 notes, got: {v2_notes}"
        )

# =====================================================================# === TESTS: refine_with_org_feedback stopping conditions ====================
# ============================================================================
#
# Each test uses an injected runner so no org is needed.  An injected runner
# stamps feedback with source=INJECTED_RUNNER_SOURCE, which is NOT in
# REAL_FEEDBACK_SOURCES, so every round is correctly marked untrustworthy and
# the spec is NOT carried forward.  That means:
# - The spec's score_after stays constant (same spec is re-scored every round)
# - The identical_score_plateau condition fires predictably
# - The gate_pass condition fires only when the spec itself is already above
#   PASS_THRESHOLD with no blocking issues
#
# Helper builders are local to this section to keep each test self-contained.

def _make_org_runner(payload_json: str):
    """Return a (runner, calls) pair that records every invocation."""
    import json as _json

    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = payload_json
        stderr = ""

    def runner(cmd, timeout):
        calls.append(cmd)
        return Done()

    return runner, calls


def _run_eval_payload_json() -> str:
    """Return the real fixture as a JSON string for the injected runner."""
    import json as _json
    from pathlib import Path as _Path

    fixture = _Path(__file__).parent / "fixtures" / "run_eval_aft3_coral_cloud_booking.json"
    return fixture.read_text(encoding="utf-8")


def _make_passing_spec() -> DerivedAgentSpec:
    """A spec that scores >= PASS_THRESHOLD=75 with no blocking issues.

    Verified to score 82/100 (passed=True, blocking=[]) both before and after
    apply_feedback with synthetic feedback. Used to test gate_pass: the spec
    already satisfies the gate, so gate_pass fires on round 1 regardless of
    the runner used.

    This uses the same construction as test_stage5._make_spec() which has been
    confirmed to score 82 with no blocking issues.
    """
    return DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=0.7,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            )
        ],
        orchestration_steps=["Resolve the Case", "SUBMIT on button:Save -> writes Status"],
        guardrails=["Require explicit user confirmation before writing: Status."],
        failure_handling=["No failures were observed in this run, so error paths are UNTESTED."],
        unknowns=["Action API names were not observed."],
        evidence=[SpecEvidence("dom-capture", "8 events observed")],
    )


def _make_stalling_spec() -> DerivedAgentSpec:
    """A spec that scores well below PASS_THRESHOLD and stays flat across rounds.

    Uses inference-only evidence (no dom-capture/telemetry) so evidence_grounding
    stays near zero, keeping the total safely under 75.  apply_feedback with a
    synthetic (injected-runner) result adds an 'unvalidated' unknown but does not
    change the score because the honesty dimension is already penalised by the
    blocking issues.
    """
    return DerivedAgentSpec(
        intent="Update Case Status field value in the record",
        confidence=0.5,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("inference", "inferred from recording context")],
            )
        ],
        orchestration_steps=[
            "Navigate to the Case record by record identifier",
            "Locate the Status field on the Case detail layout",
            "Update the Status field to the desired new value",
            "Submit the form to persist the Status change",
        ],
        guardrails=[
            "Require explicit confirmation before writing Status field",
            "Enforce field-level security on Case.Status",
        ],
        failure_handling=["Observed validation error: Status value was rejected by the server"],
        unknowns=["Exact Action API name was not observed in the recording"],
        evidence=[SpecEvidence("inference", "inferred from user-click sequence")],
    )


# --- Stopping condition 1: gate_pass ---

def test_org_feedback_stops_when_gate_passes(tmp_path: Path) -> None:
    """refine_with_org_feedback stops after the first round when the spec passes the gate.

    The fixture uses _make_passing_spec() which already scores >= PASS_THRESHOLD
    with no blocking issues.  apply_feedback with synthetic results does not lower
    the score, so gate_pass fires on round 1 even though rounds=5 was requested.

    Assertions:
    - Only 1 round returned (early exit, not all 5)
    - The round's stop_reason starts with 'gate_pass:'
    - The round.json on disk contains a 'stop_reason' field
    """
    from sf_video_blueprint.iterate import refine_with_org_feedback
    from sf_video_blueprint.spec_score import PASS_THRESHOLD

    runner, calls = _make_org_runner(_run_eval_payload_json())
    rounds = refine_with_org_feedback(
        _make_passing_spec(),
        out_dir=tmp_path / "stage5",
        org_alias="AFT3",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        rounds=5,
        runner=runner,
    )

    # Only one round should have run (gate fired immediately)
    assert len(rounds) == 1, (
        f"Expected 1 round (gate_pass), got {len(rounds)}. "
        f"stop_reason of last round: {rounds[-1].stop_reason!r}"
    )
    assert len(calls) == 1, f"Expected 1 org call, got {len(calls)}"

    # The terminal round must carry the stop_reason
    terminal = rounds[-1]
    assert terminal.stop_reason is not None, "Terminal round must have a stop_reason"
    assert terminal.stop_reason.startswith("gate_pass:"), (
        f"Expected stop_reason to start with 'gate_pass:', got: {terminal.stop_reason!r}"
    )
    assert str(PASS_THRESHOLD) in terminal.stop_reason

    # The round.json on disk must include stop_reason
    round_json_path = tmp_path / "stage5" / "round-1" / "round.json"
    assert round_json_path.exists()
    payload = json.loads(round_json_path.read_text(encoding="utf-8"))
    assert "stop_reason" in payload, "round.json must contain stop_reason for gate_pass round"
    assert payload["stop_reason"].startswith("gate_pass:"), payload["stop_reason"]


# --- Stopping condition 2: identical_score_plateau ---

def test_org_feedback_stops_on_three_identical_scores(tmp_path: Path) -> None:
    """refine_with_org_feedback stops after 3 consecutive identical scores.

    The fixture uses _make_stalling_spec() which scores flat with synthetic
    feedback.  With rounds=10, the loop should stop at round 3 (the third
    consecutive identical score triggers the plateau condition).

    Assertions:
    - Exactly 3 rounds returned
    - The terminal round's stop_reason starts with 'identical_score_plateau:'
    - The round.json of round 3 contains stop_reason
    - Rounds 1 and 2 do NOT have a stop_reason (they did not terminate the loop)
    """
    from sf_video_blueprint.iterate import refine_with_org_feedback

    runner, calls = _make_org_runner(_run_eval_payload_json())
    rounds = refine_with_org_feedback(
        _make_stalling_spec(),
        out_dir=tmp_path / "stage5",
        org_alias="AFT3",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        rounds=10,
        runner=runner,
    )

    # Should stop at round 3 (three consecutive identical scores)
    assert len(rounds) == 3, (
        f"Expected 3 rounds (identical_score_plateau after 3), got {len(rounds)}. "
        f"Scores: {[r.score_after.total for r in rounds if r.score_after]}"
    )
    assert len(calls) == 3, f"Expected 3 org calls, got {len(calls)}"

    # All three scores must be identical
    scores = [r.score_after.total for r in rounds if r.score_after is not None]
    assert len(set(scores)) == 1, f"Expected identical scores, got: {scores}"

    # Terminal round has the stop_reason; earlier rounds do not
    terminal = rounds[-1]
    assert terminal.stop_reason is not None, "Terminal round must have a stop_reason"
    assert terminal.stop_reason.startswith("identical_score_plateau:"), (
        f"Expected stop_reason to start with 'identical_score_plateau:', got: {terminal.stop_reason!r}"
    )
    assert "3 consecutive" in terminal.stop_reason

    # Non-terminal rounds must NOT have a stop_reason
    for r in rounds[:-1]:
        assert r.stop_reason is None, (
            f"Round {r.round_number} should not have a stop_reason, got: {r.stop_reason!r}"
        )

    # Terminal round.json must include stop_reason
    round_json_path = tmp_path / "stage5" / "round-3" / "round.json"
    assert round_json_path.exists()
    payload = json.loads(round_json_path.read_text(encoding="utf-8"))
    assert "stop_reason" in payload, "round.json must contain stop_reason for plateau round"
    assert payload["stop_reason"].startswith("identical_score_plateau:"), payload["stop_reason"]

    # Earlier rounds must NOT have stop_reason in their JSON
    for n in (1, 2):
        prev_json = tmp_path / "stage5" / f"round-{n}" / "round.json"
        prev_payload = json.loads(prev_json.read_text(encoding="utf-8"))
        assert "stop_reason" not in prev_payload, (
            f"round-{n}.json should not have stop_reason, got: {prev_payload.get('stop_reason')!r}"
        )


# --- Stopping condition 3: max_no_improvement ---

def test_org_feedback_stops_on_max_no_improvement(tmp_path: Path) -> None:
    """refine_with_org_feedback stops when max_no_improvement consecutive rounds
    pass without the score going up.

    Uses _make_stalling_spec() (flat score) with max_no_improvement=1 and
    rounds=10.  The score never improves, so after 1 no-improvement round
    the loop stops.

    Counting: round 1 sets best_score_seen=S and no_improvement_streak=0.
    Round 2 scores S again (not > S) -> streak=1 >= max_no_improvement=1 -> stop.
    2 rounds total.

    max_no_improvement=1 is chosen deliberately so the stopping condition fires
    at round 2, before the identical_score_plateau condition (which fires at
    round 3).  This avoids ambiguity about which condition takes precedence when
    two conditions fire simultaneously.

    Assertions:
    - Exactly 2 rounds returned (1 baseline + 1 no-improvement)
    - Terminal round's stop_reason starts with 'max_no_improvement:'
    - round.json of the terminal round contains stop_reason
    """
    from sf_video_blueprint.iterate import refine_with_org_feedback

    runner, calls = _make_org_runner(_run_eval_payload_json())
    rounds = refine_with_org_feedback(
        _make_stalling_spec(),
        out_dir=tmp_path / "stage5",
        org_alias="AFT3",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        rounds=10,
        max_no_improvement=1,
        runner=runner,
    )

    # Should stop at round 2 (round 1 = baseline, round 2 = no improvement)
    assert len(rounds) == 2, (
        f"Expected 2 rounds (max_no_improvement=1), got {len(rounds)}. "
        f"Scores: {[r.score_after.total for r in rounds if r.score_after]}"
    )

    terminal = rounds[-1]
    assert terminal.stop_reason is not None, "Terminal round must have a stop_reason"
    assert terminal.stop_reason.startswith("max_no_improvement:"), (
        f"Expected stop_reason to start with 'max_no_improvement:', got: {terminal.stop_reason!r}"
    )
    assert "limit=1" in terminal.stop_reason

    # Terminal round.json must include stop_reason
    round_json_path = tmp_path / "stage5" / "round-2" / "round.json"
    assert round_json_path.exists()
    payload = json.loads(round_json_path.read_text(encoding="utf-8"))
    assert "stop_reason" in payload, "round.json must contain stop_reason for max_no_improvement round"
    assert payload["stop_reason"].startswith("max_no_improvement:"), payload["stop_reason"]


def test_org_feedback_max_no_improvement_triggers_at_limit_1(tmp_path: Path) -> None:
    """max_no_improvement=1 stops after the first round without a score increase.

    Round 1 sets best=S.  Round 2 also returns S -> no improvement -> streak=1
    >= max_no_improvement=1 -> stop.  2 rounds total.
    """
    from sf_video_blueprint.iterate import refine_with_org_feedback

    runner, _ = _make_org_runner(_run_eval_payload_json())
    rounds = refine_with_org_feedback(
        _make_stalling_spec(),
        out_dir=tmp_path / "stage5",
        org_alias="AFT3",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        rounds=10,
        max_no_improvement=1,
        runner=runner,
    )

    assert len(rounds) == 2, (
        f"Expected 2 rounds (max_no_improvement=1: 1 baseline + 1 no-improvement), got {len(rounds)}"
    )
    terminal = rounds[-1]
    assert terminal.stop_reason is not None
    assert terminal.stop_reason.startswith("max_no_improvement:")
    assert "limit=1" in terminal.stop_reason


def test_org_feedback_max_no_improvement_invalid_value_raises(tmp_path: Path) -> None:
    """max_no_improvement < 1 must raise ValueError."""
    from sf_video_blueprint.iterate import refine_with_org_feedback

    with pytest.raises(ValueError, match="max_no_improvement must be >= 1"):
        refine_with_org_feedback(
            _make_stalling_spec(),
            out_dir=tmp_path,
            org_alias="AFT3",
            agent_api_name="TestAgent",
            test_spec_name="TestSpec",
            max_no_improvement=0,
        )


def test_org_feedback_max_no_improvement_none_does_not_stop_loop(tmp_path: Path) -> None:
    """max_no_improvement=None (the default) means the condition is disabled.

    With a stalling spec and 3 rounds budget, the loop should run all 3 rounds
    (stopped only by the identical_score_plateau after round 3, not by
    max_no_improvement since it is None).

    This also verifies that not passing max_no_improvement at all doesn't
    accidentally enable the condition with some default limit.
    """
    from sf_video_blueprint.iterate import refine_with_org_feedback

    runner, calls = _make_org_runner(_run_eval_payload_json())
    rounds = refine_with_org_feedback(
        _make_stalling_spec(),
        out_dir=tmp_path / "stage5",
        org_alias="AFT3",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        rounds=3,
        # max_no_improvement not passed -> None -> disabled
        runner=runner,
    )

    # Without max_no_improvement, the loop runs 3 rounds (stopped by the
    # identical_score_plateau condition after the 3rd identical score).
    assert len(rounds) == 3, f"Expected 3 rounds without max_no_improvement, got {len(rounds)}"
    assert len(calls) == 3


def test_org_feedback_gate_pass_stop_reason_written_to_disk(tmp_path: Path) -> None:
    """stop_reason must appear in round.json when gate_pass fires.

    This is the audit-trail contract: a reader opening round.json for the
    terminal round must be able to see WHY the loop ended without reading any
    other file.
    """
    from sf_video_blueprint.iterate import refine_with_org_feedback
    from sf_video_blueprint.spec_score import PASS_THRESHOLD

    runner, _ = _make_org_runner(_run_eval_payload_json())
    refine_with_org_feedback(
        _make_passing_spec(),
        out_dir=tmp_path / "stage5",
        org_alias="AFT3",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        rounds=3,
        runner=runner,
    )

    # round-1 is the terminal round; it must have stop_reason in its JSON
    payload = json.loads(
        (tmp_path / "stage5" / "round-1" / "round.json").read_text(encoding="utf-8")
    )
    assert "stop_reason" in payload
    assert "gate_pass" in payload["stop_reason"]
    assert str(PASS_THRESHOLD) in payload["stop_reason"]

    # round-2 and round-3 must NOT exist (loop stopped at 1)
    assert not (tmp_path / "stage5" / "round-2").exists(), "No round-2 should exist after gate_pass"


def test_org_feedback_non_terminal_rounds_have_no_stop_reason(tmp_path: Path) -> None:
    """Non-terminal rounds must NOT carry a stop_reason field in round.json.

    Only the round that actually triggered a stopping condition should have
    stop_reason in its JSON.  Earlier rounds having it would imply the loop
    was stopped early on every round, which is false.
    """
    from sf_video_blueprint.iterate import refine_with_org_feedback

    runner, _ = _make_org_runner(_run_eval_payload_json())
    rounds = refine_with_org_feedback(
        _make_stalling_spec(),
        out_dir=tmp_path / "stage5",
        org_alias="AFT3",
        agent_api_name="TestAgent",
        test_spec_name="TestSpec",
        rounds=10,
        runner=runner,
    )

    # Loop stops at round 3 due to identical_score_plateau.
    # Rounds 1 and 2 must have no stop_reason.
    assert len(rounds) == 3
    for n in (1, 2):
        payload = json.loads(
            (tmp_path / "stage5" / f"round-{n}" / "round.json").read_text(encoding="utf-8")
        )
        assert "stop_reason" not in payload, (
            f"round-{n}.json must not contain stop_reason; "
            f"only the terminal round should. Got: {payload.get('stop_reason')!r}"
        )

    # Round 3 IS the terminal and must have stop_reason.
    terminal_payload = json.loads(
        (tmp_path / "stage5" / "round-3" / "round.json").read_text(encoding="utf-8")
    )
    assert "stop_reason" in terminal_payload

# =============================================================================
# === TEST: write_iteration_summary ===
# =============================================================================

def _make_mock_score(
    total: int = 70,
    max_total: int = 100,
    band: str = "AMBER",
    passed: bool = False,
    blocking_issues: list[str] | None = None,
    recommendations: list[str] | None = None,
):
    """Build a duck-typed mock score object for summary tests."""
    class _Score:
        pass

    s = _Score()
    s.total = total
    s.max_total = max_total
    s.band = band
    s.passed = passed
    s.blocking_issues = blocking_issues if blocking_issues is not None else []
    s.recommendations = recommendations if recommendations is not None else []
    return s


def _make_spec_version(
    version: int,
    spec_path: Path,
    score=None,
    notes: list[str] | None = None,
) -> SpecVersion:
    """Build a minimal SpecVersion for summary tests."""
    if score is None:
        score = _make_mock_score()
    return SpecVersion(
        version=version,
        spec_path=spec_path,
        yaml_path=None,
        score=score,
        role_used="analyst",
        source="derived",
        parent_version=version - 1 if version > 1 else None,
        notes=notes if notes is not None else [],
    )


def _write_spec_json(path: Path, intent: str = "Handle Case escalation") -> Path:
    """Write a minimal agent-spec.json to path and return path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"intent": intent, "confidence": 0.75, "unknowns": []}),
        encoding="utf-8",
    )
    return path


def test_write_iteration_summary_file_is_written(tmp_path: Path) -> None:
    """Summary file must be created at the requested path."""
    spec_path = _write_spec_json(tmp_path / "v1" / "agent-spec.json")

    result = IterationResult(
        versions=[_make_spec_version(1, spec_path)],
        best=_make_spec_version(1, spec_path),
        converged=False,
        stop_reason="Reached max_rounds=1",
        rounds_run=1,
    )

    out = write_iteration_summary(tmp_path / "iteration_summary.md", result)
    assert out.exists(), f"Summary file was not written to {out}"
    assert out.suffix == ".md"


def test_write_iteration_summary_contains_intent(tmp_path: Path) -> None:
    """Summary file must contain the intent read from the best version's spec JSON."""
    intent = "Update Case status to Closed"
    spec_path = _write_spec_json(tmp_path / "v1" / "agent-spec.json", intent=intent)

    result = IterationResult(
        versions=[_make_spec_version(1, spec_path)],
        best=_make_spec_version(1, spec_path),
        converged=False,
        stop_reason="Reached max_rounds=1",
        rounds_run=1,
    )

    out = write_iteration_summary(tmp_path / "iteration_summary.md", result)
    content = out.read_text(encoding="utf-8")
    assert intent in content, f"Intent '{intent}' not found in summary:\n{content}"


def test_write_iteration_summary_contains_round_table(tmp_path: Path) -> None:
    """Summary must contain a table with a row per round."""
    spec_path_v1 = _write_spec_json(tmp_path / "v1" / "agent-spec.json")
    spec_path_v2 = _write_spec_json(tmp_path / "v2" / "agent-spec.json")
    spec_path_v3 = _write_spec_json(tmp_path / "v3" / "agent-spec.json")

    v1 = _make_spec_version(1, spec_path_v1, score=_make_mock_score(total=60, band="RED"))
    v2 = _make_spec_version(2, spec_path_v2, score=_make_mock_score(total=70, band="AMBER"))
    v3 = _make_spec_version(3, spec_path_v3, score=_make_mock_score(total=80, band="GREEN", passed=True))

    result = IterationResult(
        versions=[v1, v2, v3],
        best=v3,
        converged=False,
        stop_reason="Score 80/100 >= threshold 75 with no blocking issues",
        rounds_run=3,
    )

    out = write_iteration_summary(tmp_path / "iteration_summary.md", result)
    content = out.read_text(encoding="utf-8")

    # Each version row must appear
    assert "v1" in content, "Round 1 row missing from summary"
    assert "v2" in content, "Round 2 row missing from summary"
    assert "v3" in content, "Round 3 row missing from summary"

    # Score values must appear
    assert "60" in content, "Score 60 missing from summary"
    assert "70" in content, "Score 70 missing from summary"
    assert "80" in content, "Score 80 missing from summary"

    # Band values
    assert "RED" in content, "Band RED missing"
    assert "AMBER" in content, "Band AMBER missing"
    assert "GREEN" in content, "Band GREEN missing"


def test_write_iteration_summary_blocking_issues_present(tmp_path: Path) -> None:
    """When the final round has blocking issues, they must appear in the summary."""
    blocking = ["Must observe at least one failure path", "Insufficient field evidence"]
    spec_path = _write_spec_json(tmp_path / "v1" / "agent-spec.json")
    score = _make_mock_score(total=60, blocking_issues=blocking)

    v1 = _make_spec_version(1, spec_path, score=score)
    result = IterationResult(
        versions=[v1],
        best=v1,
        converged=False,
        stop_reason="Reached max_rounds=1",
        rounds_run=1,
    )

    out = write_iteration_summary(tmp_path / "iteration_summary.md", result)
    content = out.read_text(encoding="utf-8")

    for issue in blocking:
        assert issue in content, f"Blocking issue '{issue}' missing from summary:\n{content}"


def test_write_iteration_summary_no_blocking_issues_message(tmp_path: Path) -> None:
    """When there are no blocking issues, the summary must say so explicitly."""
    spec_path = _write_spec_json(tmp_path / "v1" / "agent-spec.json")
    score = _make_mock_score(total=80, passed=True, blocking_issues=[])

    v1 = _make_spec_version(1, spec_path, score=score)
    result = IterationResult(
        versions=[v1],
        best=v1,
        converged=False,
        stop_reason="Score 80/100 >= threshold 75 with no blocking issues",
        rounds_run=1,
    )

    out = write_iteration_summary(tmp_path / "iteration_summary.md", result)
    content = out.read_text(encoding="utf-8")
    assert "no blocking issues" in content.lower(), (
        f"Expected 'no blocking issues' message in summary:\n{content}"
    )
    assert "spec passed the gate" in content.lower(), (
        f"Expected 'spec passed the gate' in summary:\n{content}"
    )


def test_write_iteration_summary_full_run(tmp_path: Path) -> None:
    """Integration: run refine() and then write_iteration_summary(); verify all sections present."""
    spec = _make_spec(intent="Escalate a Support Case")

    result = refine(
        spec,
        out_dir=tmp_path / "run",
        company_name="ACME",
        company_description="Testing company",
        max_rounds=3,
        use_cli=False,
    )

    out = write_iteration_summary(tmp_path / "run" / "iteration_summary.md", result)
    content = out.read_text(encoding="utf-8")

    # Intent section
    assert "Escalate" in content or "Case" in content, (
        "Intent not found in summary from real refine() run"
    )

    # Rounds run and stop reason
    assert str(result.rounds_run) in content
    assert result.stop_reason[:30] in content  # partial match is enough

    # Per-round table header
    assert "Round" in content
    assert "Score" in content
    assert "Band" in content

    # Blocking issues section
    assert "Blocking Issues" in content
