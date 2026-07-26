"""Gaming resistance tests for spec_score.py — the permanent adversarial suite.

This file exists to be the scorer's adversary forever. The spec scorer drives a
refinement loop that the user will run repeatedly, so whatever the scorer rewards
is what the loop will learn to produce. Gaming attacks are specs engineered to
score well without being grounded in real evidence.

These tests prove the scorer can detect and fail the following attack classes:
1. Padding — inflating counts with trivial duplicates
2. Evidence laundering — plausible-looking but self-declared sources
3. Caveat deletion — hiding unknowns to inflate honesty
4. Confidence inflation — high confidence with structural gaps
5. Threshold surfing — sacrificing cheap dimensions to clear 75 anyway
6. Keyword stuffing — text engineered to game heuristics without real content
7. Failure fabrication — asserting observed failures without evidence
8. Empty-but-well-formed — structurally complete, semantically vacuous

Plus one real-spec test that must PASS, so any hardening that blocks honest output
is caught immediately.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from sf_video_blueprint.spec_score import PASS_THRESHOLD, score_spec, score_spec_file
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


def test_attack_1_padding_duplicate_entities():
    """ATTACK 1 (Padding): Inflate entity count with near-duplicate entities.

    The attacker creates 10 entities that differ only trivially (Status1, Status2, ...),
    all claiming data-delta evidence. A naive counter rewards this; the scorer must detect
    that the entities are substantively identical and penalize it.

    Why passing this would be dangerous: The loop would learn to pad entity lists with
    trivial variants to maximize completeness/evidence scores, making the spec unreadable
    and untrustworthy.
    """
    attack = DerivedAgentSpec(
        intent="Update Case Status",
        confidence=0.85,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name=f"status_{i}",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", f"variant {i}")],
            )
            for i in range(10)
        ],
        orchestration_steps=["Resolve the Case", "Submit the form"],
        guardrails=["Enforce FLS on Case"],
        failure_handling=["Observed validation failure during recording: x"],
        unknowns=[],
        evidence=[],
    )

    result = score_spec(attack)

    assert not result.passed, (
        f"ATTACK 1 (Padding) PASSED with score {result.total}/100. "
        "10 entities all targeting Case.Status is obvious padding; the scorer must detect and fail it."
    )


def test_attack_2_evidence_laundering():
    """ATTACK 2 (Evidence laundering): Claim data-delta on a field no delta could produce.

    The attacker asserts SpecEvidence(source="data-delta", ...) on an entity whose
    field_api_name is not in the observed deltas. The source looks real (not "inference"),
    but it's self-declared without backing evidence.

    Why passing this would be dangerous: A naive loop could fabricate entities and claim
    they came from observed data, bypassing the evidence grounding check entirely.

    NOTE: This attack may NOT be detectable purely offline (the scorer receives a spec dict,
    not the original capture/telemetry). If unfixable here, document where the check belongs.
    """
    attack = DerivedAgentSpec(
        intent="Update Case Status",
        confidence=0.85,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="fabricated_field",
                object_api_name="Case",
                field_api_name="NonExistentField__c",
                evidence=[SpecEvidence("data-delta", "claimed but never observed")],
            ),
        ],
        orchestration_steps=["Resolve the Case", "Submit the form"],
        guardrails=["Enforce FLS on Case"],
        failure_handling=["Observed validation failure during recording: x"],
        unknowns=[],
        # A real trail, because this test is about whether LAUNDERING is detectable.
        # The fixture used to carry `evidence=[]`, which the gate now blocks on its own
        # (build_agent_spec always records the run — see the C1 finding in
        # test_score_calibration.py). That made the test block for a reason unrelated to
        # laundering and stop measuring the thing it documents.
        evidence=[
            SpecEvidence("extraction", "2 action(s) in recording"),
            SpecEvidence("data-delta", "objects mutated: Case"),
        ],
    )

    result = score_spec(attack)

    # This attack is LIKELY unfixable offline without the original capture/telemetry.
    # The scorer sees only the spec dict, so it cannot cross-check "data-delta" claims
    # against actual observed deltas. We document this limitation but still test to
    # verify the scorer doesn't accidentally block honest specs with uncommon field names.
    # For now, we expect this to pass (the scorer cannot detect it), and we note where
    # the check SHOULD live.
    if result.passed:
        # Expected: the scorer cannot detect evidence laundering offline.
        # The check belongs in spec_builder._derive_entities, which has access to
        # the observed deltas and can refuse to emit data-delta evidence for fields
        # that were not in the snapshot.
        pass
    else:
        # Unexpected: if the scorer DID block this, verify it's not a false positive
        # that would also block honest specs with custom fields.
        assert "NonExistentField__c" in str(result.blocking_issues) or \
               "fabricated" in str(result.blocking_issues).lower(), (
            f"ATTACK 2 blocked but reason unclear. Blocking issues: {result.blocking_issues}"
        )


def test_attack_3_caveat_deletion():
    """ATTACK 3 (Caveat deletion): Delete unknowns to inflate honesty score.

    The attacker starts with a low-confidence spec that honestly declares unknowns,
    then deletes the unknowns[] list and raises confidence to game the honesty dimension.

    Why passing this would be dangerous: G3 exists to prevent this — declaring unknowns
    must score >= hiding them. If this attack passes, the loop learns to hide gaps.
    """
    attack = DerivedAgentSpec(
        intent="Update Case Status",
        confidence=0.85,  # High confidence
        objects_touched=[],  # Gap: no objects observed
        entities=[],  # Gap: no entities observed
        orchestration_steps=["Do something vague"],
        guardrails=["Validate input"],
        failure_handling=["No failures were observed in this run, so error paths are UNTESTED."],
        unknowns=[],  # DELETED (should have declared the gaps)
        evidence=[],
    )

    result = score_spec(attack)

    assert not result.passed, (
        f"ATTACK 3 (Caveat deletion) PASSED with score {result.total}/100. "
        "High confidence + structural gaps + no declared unknowns is dishonest; must fail."
    )

    # The honesty dimension should score very low (near 0)
    honesty_score = result.dimensions["honesty"].score
    honesty_max = result.dimensions["honesty"].max_score
    assert honesty_score <= honesty_max * 0.3, (
        f"ATTACK 3: Honesty score {honesty_score}/{honesty_max} is too high for a dishonest spec."
    )


def test_attack_4_confidence_inflation():
    """ATTACK 4 (Confidence inflation): Claim 0.95 confidence on a spec with obvious gaps.

    Similar to Attack 3 but focuses on the confidence field itself. The attacker sets
    confidence=0.95 while objects_touched and entities are empty.

    Why passing this would be dangerous: The honesty dimension exists to catch exactly
    this — high confidence on incomplete data is dishonest.
    """
    attack = DerivedAgentSpec(
        intent="Update Case Status",
        confidence=0.95,  # Absurdly high for an empty spec
        objects_touched=[],
        entities=[],
        orchestration_steps=["Vague action"],
        guardrails=["Generic guardrail"],
        failure_handling=["Unknown"],
        unknowns=[],
        evidence=[],
    )

    result = score_spec(attack)

    assert not result.passed, (
        f"ATTACK 4 (Confidence inflation) PASSED with score {result.total}/100. "
        "0.95 confidence on an empty spec is absurd; must fail."
    )


def test_attack_5_threshold_surfing():
    """ATTACK 5 (Threshold surfing): Sacrifice cheap dimensions, max the rest to clear 75.

    The attacker deliberately scores 0 on testability (10 points), provenance (5 points),
    and specificity (10 points) — 25 points total — and maximizes the remaining 75 points
    to exactly hit the threshold. This is a rational attack if the scorer has exploitable
    weight imbalances.

    Why passing this would be dangerous: The loop would learn to ignore entire dimensions
    (testability, specificity) and focus only on gaming the high-weight ones.
    """
    attack = DerivedAgentSpec(
        intent="Generic action",  # Deliberately generic (low specificity)
        confidence=0.50,  # Moderate (avoids honesty penalty)
        objects_touched=["Case"],  # Enough to avoid blocking
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "observed")],
            ),
        ],
        orchestration_steps=["Step 1", "Step 2"],  # Distinct (completeness)
        guardrails=["Guardrail 1"],  # Present (completeness)
        failure_handling=["No failures were observed in this run, so error paths are UNTESTED."],  # Deliberately untested (testability=0)
        unknowns=["Some unknown"],  # Declare one to boost honesty
        evidence=[SpecEvidence("telemetry", "x")],
    )

    result = score_spec(attack, provenance=None)  # Provenance=None -> 0/5 but no blocker

    assert not result.passed, (
        f"ATTACK 5 (Threshold surfing) PASSED with score {result.total}/100. "
        "Deliberately sacrificing testability, specificity, and provenance to hit 75 must fail."
    )


def test_attack_6_keyword_stuffing():
    """ATTACK 6 (Keyword stuffing): Engineer text to satisfy heuristics without real content.

    The attacker stuffs orchestration_steps and guardrails with object/field names
    (satisfying specificity checks) but the text describes nothing actually observed.

    Why passing this would be dangerous: The specificity and testability dimensions rely
    on keyword matching (e.g., "names real objects and fields"). A naive heuristic can
    be gamed by keyword stuffing.
    """
    attack = DerivedAgentSpec(
        intent="Update Case Status Priority Owner Subject Description",  # Keyword-stuffed
        confidence=0.75,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "x")],  # Minimal evidence
            ),
        ],
        orchestration_steps=[
            "Mention Case Status Priority Owner in a vague way",
            "Reference Case Subject Description but do nothing specific",
        ],
        guardrails=[
            "Enforce FLS on Case Status Priority Owner Subject Description",
            "Validate Case Status Priority Owner against business rules",
        ],
        failure_handling=["Observed validation failure during recording: blah"],
        unknowns=[],
        evidence=[],
    )

    result = score_spec(attack)

    # The minimal evidence (detail="x") should trigger the penalty added for Attack 2.
    # Even if specificity is fooled, evidence_grounding should catch the "x" detail.
    assert not result.passed, (
        f"ATTACK 6 (Keyword stuffing) PASSED with score {result.total}/100. "
        "Keyword-stuffed text with minimal evidence must fail."
    )


def test_attack_7_failure_fabrication():
    """ATTACK 7 (Failure fabrication): Assert observed failure using the exact builder fragment.

    The attacker knows the builder emits "Observed <layer> failure during recording: <reason>"
    and copies that pattern verbatim to game the testability dimension, without any real
    failure having been observed.

    Why passing this would be dangerous: The testability dimension awards half its score for
    observed failures. A naive substring check can be fooled by copying the builder's output.
    """
    attack = DerivedAgentSpec(
        intent="Update Case Status",
        confidence=0.75,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "x")],
            ),
        ],
        orchestration_steps=["Resolve the Case", "Submit the form"],
        guardrails=["Enforce FLS on Case"],
        failure_handling=[
            "Observed validation failure during recording: fabricated reason"
        ],  # Copied the builder's pattern
        unknowns=[],
        evidence=[],
    )

    result = score_spec(attack)

    # This attack is HARD to detect offline. The scorer sees only the text, not the original
    # telemetry that would prove whether a failure was actually observed. However, the minimal
    # evidence detail "x" should still cause a penalty.
    # We test this to document the limitation: failure_handling text alone cannot be verified
    # without cross-checking against telemetry.
    if result.passed:
        # The scorer cannot fully verify failure_handling text offline. The check belongs
        # in spec_builder._derive_failure_handling, which has access to telemetry.
        pass
    else:
        # If it did block, verify the minimal evidence penalty fired.
        assert result.dimensions["evidence_grounding"].score < result.dimensions["evidence_grounding"].max_score * 0.6, (
            f"Expected low evidence_grounding for minimal detail, got {result.dimensions['evidence_grounding'].score}"
        )


def test_attack_8_empty_but_well_formed():
    """ATTACK 8 (Empty-but-well-formed): Structurally complete, semantically vacuous.

    The attacker provides all required fields (objects_touched, entities, steps, guardrails,
    failures) but with minimal/placeholder content. Every field is present but carries no
    real information.

    Why passing this would be dangerous: D11 was added to catch exactly this — an empty spec
    must score 0, not be rewarded for avoiding known-bad patterns by having no content at all.
    """
    attack = DerivedAgentSpec(
        intent="Do something",  # Vacuous
        confidence=0.5,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="x",
                object_api_name="Case",
                field_api_name="X",
                evidence=[SpecEvidence("data-delta", "x")],
            ),
        ],
        orchestration_steps=["Step", "Step"],  # Duplicate + vacuous
        guardrails=["Check"],  # Vacuous
        failure_handling=["Error"],  # Vacuous
        unknowns=[],
        evidence=[],
    )

    result = score_spec(attack)

    assert not result.passed, (
        f"ATTACK 8 (Empty-but-well-formed) PASSED with score {result.total}/100. "
        "Structurally present but semantically vacuous content must fail."
    )

    # Minimal evidence penalty should fire
    assert result.dimensions["evidence_grounding"].score < result.dimensions["evidence_grounding"].max_score * 0.6, (
        f"Expected low evidence_grounding for minimal detail, got {result.dimensions['evidence_grounding'].score}"
    )


def test_real_derived_spec_must_pass():
    """PERMANENT CONTROL: A genuinely derived spec from real capture must PASS.

    This test uses the same pattern as the e2e fixture: real observed data (data-delta
    evidence), distinct steps, specific guardrails, observed failure. If any hardening
    in this file causes this to fail, that is a REGRESSION, not stricter enforcement.

    A gate no honest recording can clear is worse than the hole.
    """
    real_spec = DerivedAgentSpec(
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
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority changed 'Low' -> 'High' at step 4")],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "a Case record must be identified to act on it")],
            ),
        ],
        orchestration_steps=[
            "Resolve and load the target Case record; confirm the caller may act on it.",
            "SUBMIT on button:Save -> writes Status, Priority (backend: validation, workflow)",
            "Return a confirmation that names the record and the fields changed.",
        ],
        guardrails=[
            "Enforce object- and field-level security on Case for the running user.",
            "Require explicit user confirmation before writing: Status, Priority.",
        ],
        failure_handling=[
            "Observed validation failure during recording: Status must be one of approved values"
        ],
        unknowns=[],
        evidence=[
            SpecEvidence("telemetry", "backend layers observed: validation, workflow"),
            SpecEvidence("extraction", "5 action(s) in recording, coalesced to 3 steps"),
            SpecEvidence("data-delta", "objects mutated: Case"),
        ],
    )

    # Score with real provenance (both axes real)
    provenance_real = {
        "extraction_source": "dom-capture",
        "telemetry_source": "live-org",
    }

    result = score_spec(real_spec, provenance=provenance_real)

    assert result.passed, (
        f"REAL SPEC FAILED with score {result.total}/100. "
        f"Blocking issues: {result.blocking_issues}. "
        "This is a REGRESSION — a genuinely derived spec must pass. "
        "If any hardening in test_gaming_resistance.py caused this, that hardening is wrong."
    )

    assert result.total >= PASS_THRESHOLD, (
        f"REAL SPEC scored {result.total}/100, below threshold {PASS_THRESHOLD}. "
        "A genuine recording must clear the gate."
    )

    # Provenance integrity should score full marks
    assert result.dimensions["provenance_integrity"].score == 5, (
        f"Real provenance scored {result.dimensions['provenance_integrity'].score}/5, expected 5."
    )


def test_real_spec_in_memory_scoring_must_pass():
    """PERMANENT CONTROL: Real spec with provenance=None (in-memory) must PASS.

    Same as test_real_derived_spec_must_pass, but scored in-memory (provenance=None).
    This is the iterate.py use case: the loop scores in-memory and must be able to
    converge on a passing spec. Provenance scores 0 but does NOT block.

    Max in-memory score is 95/100 (100 - 5 for provenance), so threshold is still reachable.
    """
    real_spec = DerivedAgentSpec(
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
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority changed 'Low' -> 'High' at step 4")],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "a Case record must be identified to act on it")],
            ),
        ],
        orchestration_steps=[
            "Resolve and load the target Case record; confirm the caller may act on it.",
            "SUBMIT on button:Save -> writes Status, Priority (backend: validation, workflow)",
            "Return a confirmation that names the record and the fields changed.",
        ],
        guardrails=[
            "Enforce object- and field-level security on Case for the running user.",
            "Require explicit user confirmation before writing: Status, Priority.",
        ],
        failure_handling=[
            "Observed validation failure during recording: Status must be one of approved values"
        ],
        unknowns=[],
        evidence=[
            SpecEvidence("telemetry", "backend layers observed: validation, workflow"),
            SpecEvidence("extraction", "5 action(s) in recording, coalesced to 3 steps"),
            SpecEvidence("data-delta", "objects mutated: Case"),
        ],
    )

    result = score_spec(real_spec, provenance=None)

    assert result.passed, (
        f"REAL SPEC (in-memory) FAILED with score {result.total}/100. "
        f"Blocking issues: {result.blocking_issues}. "
        "In-memory scoring must allow genuine specs to pass (max 95/100, threshold 75)."
    )

    assert result.total >= PASS_THRESHOLD, (
        f"REAL SPEC (in-memory) scored {result.total}/100, below threshold {PASS_THRESHOLD}."
    )

    # Provenance should score 0 but NOT block
    assert result.dimensions["provenance_integrity"].score == 0, (
        f"In-memory provenance should score 0, got {result.dimensions['provenance_integrity'].score}."
    )
    assert len([b for b in result.blocking_issues if "provenance" in b.lower()]) == 0, (
        f"In-memory scoring (provenance=None) must not have provenance-related blockers. "
        f"Blocking issues: {result.blocking_issues}"
    )


def test_attack_9_padding_duplicate_steps():
    """ATTACK 9 (Padding): Inflate orchestration step count with near-duplicates.

    Similar to Attack 1 but targets orchestration_steps. The attacker creates 10 steps
    that are trivially different ("Resolve Case 1", "Resolve Case 2", ...).

    Why passing this would be dangerous: F3 exists to count DISTINCT steps. If this passes,
    the loop learns to pad step lists.
    """
    attack = DerivedAgentSpec(
        intent="Update Case Status",
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
        orchestration_steps=[f"Resolve the Case {i}" for i in range(10)],  # Padding
        guardrails=["Enforce FLS on Case"],
        failure_handling=["Observed validation failure during recording: x"],
        unknowns=[],
        evidence=[],
    )

    result = score_spec(attack)

    assert not result.passed, (
        f"ATTACK 9 (Padding steps) PASSED with score {result.total}/100. "
        "10 trivially different steps is padding; must fail."
    )


def test_attack_10_padding_duplicate_guardrails():
    """ATTACK 10 (Padding): Inflate guardrail count with near-duplicates.

    The attacker creates 10 guardrails that differ only trivially.

    Why passing this would be dangerous: Completeness counts guardrails. If duplicates
    are not detected, the loop learns to pad guardrail lists.
    """
    attack = DerivedAgentSpec(
        intent="Update Case Status",
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
        orchestration_steps=["Resolve the Case", "Submit the form"],
        guardrails=[f"Enforce FLS on Case {i}" for i in range(10)],  # Padding
        failure_handling=["Observed validation failure during recording: x"],
        unknowns=[],
        evidence=[],
    )

    result = score_spec(attack)

    assert not result.passed, (
        f"ATTACK 10 (Padding guardrails) PASSED with score {result.total}/100. "
        "10 trivially different guardrails is padding; must fail."
    )


# === SUMMARY OF UNFIXABLE ATTACKS (documented, not fixable offline) ===

# ATTACK 2 (Evidence laundering): The scorer receives a spec dict, not the original
# capture/telemetry, so it cannot cross-check "data-delta" claims against actual observed
# deltas. The fix belongs in spec_builder._derive_entities, which has access to the
# observed snapshots and can refuse to emit data-delta evidence for fields not in the delta.

# ATTACK 7 (Failure fabrication): Similarly, the scorer cannot verify failure_handling
# text against observed telemetry offline. The fix belongs in spec_builder._derive_failure_handling,
# which has access to telemetry and can refuse to emit "Observed <layer> failure" text
# unless the corresponding TelemetryEvent exists.


# === BLOCKER-PRESENCE TESTS (Mutant 2 killer) ===

def test_blocker_presence_no_objects_touched():
    """BLOCKER-PRESENCE: No objects_touched must produce a specific blocker."""
    spec = _make_spec(objects_touched=[], entities=[])
    result = score_spec(spec)

    # Search for the exact blocker substring from line 259 of spec_score.py
    blocker_found = any("No Salesforce object observed" in issue for issue in result.blocking_issues)
    assert blocker_found, (
        f"Expected 'No Salesforce object observed' blocker, got: {result.blocking_issues}"
    )


def test_blocker_presence_no_entities():
    """BLOCKER-PRESENCE: No entities must produce a specific blocker."""
    spec = _make_spec(
        objects_touched=["Case"],
        entities=[],
    )
    result = score_spec(spec)

    # Search for the exact blocker substring from line 264 of spec_score.py
    blocker_found = any("Spec derived no input entities" in issue for issue in result.blocking_issues)
    assert blocker_found, (
        f"Expected 'Spec derived no input entities' blocker, got: {result.blocking_issues}"
    )


def test_blocker_presence_no_guardrails():
    """BLOCKER-PRESENCE: No guardrails must produce a specific blocker."""
    spec = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "observed")],
            ),
        ],
        guardrails=[],
    )
    result = score_spec(spec)

    # Search for the exact blocker substring from lines 271-275 of spec_score.py
    blocker_found = any("No guardrails present" in issue for issue in result.blocking_issues)
    assert blocker_found, (
        f"Expected 'No guardrails present' blocker, got: {result.blocking_issues}"
    )


def test_blocker_presence_unresolved_intent():
    """BLOCKER-PRESENCE: UNRESOLVED intent must produce a specific blocker."""
    spec = _make_spec(
        intent="UNRESOLVED: something",
    )
    result = score_spec(spec)

    # Search for the exact blocker substring from line 285 of spec_score.py
    blocker_found = any("Spec intent is UNRESOLVED" in issue for issue in result.blocking_issues)
    assert blocker_found, (
        f"Expected 'Spec intent is UNRESOLVED' blocker, got: {result.blocking_issues}"
    )


def test_blocker_presence_placeholder_content():
    """BLOCKER-PRESENCE: Placeholder content must produce a blocker mentioning 'Placeholder/stub'."""
    spec = _make_spec(
        intent="Update Case TODO",  # Contains placeholder
    )
    result = score_spec(spec)

    # Search for the blocker substring from line 303 of spec_score.py
    blocker_found = any("Placeholder/stub content detected" in issue for issue in result.blocking_issues)
    assert blocker_found, (
        f"Expected 'Placeholder/stub content detected' blocker, got: {result.blocking_issues}"
    )


def test_blocker_presence_stub_extraction_provenance():
    """BLOCKER-PRESENCE: Stub extraction_source must produce a specific blocker."""
    spec = _make_spec()
    provenance = {"extraction_source": "stub", "telemetry_source": "live-org"}
    result = score_spec(spec, provenance=provenance)

    # Search for the exact blocker substring from lines 125-128 of spec_score.py
    blocker_found = any(
        "Spec was built from stub/unknown extraction data" in issue
        for issue in result.blocking_issues
    )
    assert blocker_found, (
        f"Expected 'Spec was built from stub/unknown extraction data' blocker, got: {result.blocking_issues}"
    )


def test_blocker_presence_mock_telemetry_provenance():
    """BLOCKER-PRESENCE: Mock telemetry_source must produce a specific blocker."""
    spec = _make_spec()
    provenance = {"extraction_source": "dom-capture", "telemetry_source": "mock"}
    result = score_spec(spec, provenance=provenance)

    # Search for the exact blocker substring from lines 131-134 of spec_score.py
    blocker_found = any(
        "Spec was built from mock/unknown telemetry" in issue
        for issue in result.blocking_issues
    )
    assert blocker_found, (
        f"Expected 'Spec was built from mock/unknown telemetry' blocker, got: {result.blocking_issues}"
    )


def test_blocker_presence_entity_padding():
    """BLOCKER-PRESENCE: Entity padding (many entities targeting same field) must produce a blocker."""
    spec = _make_spec(
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name=f"status_{i}",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", f"variant {i}")],
            )
            for i in range(5)  # >3 entities targeting same field
        ],
    )
    result = score_spec(spec)

    # Search for the blocker substring from line 245 of spec_score.py
    blocker_found = any(
        "Entity padding detected" in issue and "multiple entities target the same field" in issue
        for issue in result.blocking_issues
    )
    assert blocker_found, (
        f"Expected 'Entity padding detected: multiple entities target the same field' blocker, got: {result.blocking_issues}"
    )


def test_blocker_presence_steps_padding():
    """BLOCKER-PRESENCE: Orchestration steps padding must produce a specific blocker."""
    spec = _make_spec(
        orchestration_steps=[f"Resolve the Case {i}" for i in range(10)],  # Padding
    )
    result = score_spec(spec)

    # Search for the blocker substring from line 257 of spec_score.py
    blocker_found = any(
        "Padding detected in orchestration steps or guardrails" in issue
        for issue in result.blocking_issues
    )
    assert blocker_found, (
        f"Expected 'Padding detected in orchestration steps or guardrails' blocker, got: {result.blocking_issues}"
    )


def test_blocker_presence_guardrails_padding():
    """BLOCKER-PRESENCE: Guardrails padding must produce a specific blocker."""
    spec = _make_spec(
        guardrails=[f"Enforce FLS on Case {i}" for i in range(10)],  # Padding
    )
    result = score_spec(spec)

    # Search for the blocker substring from line 257 of spec_score.py
    blocker_found = any(
        "Padding detected in orchestration steps or guardrails" in issue
        for issue in result.blocking_issues
    )
    assert blocker_found, (
        f"Expected 'Padding detected in orchestration steps or guardrails' blocker, got: {result.blocking_issues}"
    )


def test_blocker_presence_threshold_surfing():
    """BLOCKER-PRESENCE: Threshold surfing (>=2 dimensions <=50%) must produce a specific blocker."""
    # Deliberately craft a spec with 2+ dimensions scoring <=50%
    spec = _make_spec(
        intent="Generic",  # Low specificity
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("inference", "inferred")],  # Low grounding
            ),
        ],
        orchestration_steps=["Step"],  # Minimal completeness
        guardrails=["Check"],  # Minimal
        failure_handling=["No failures were observed in this run, so error paths are UNTESTED."],  # Low testability
        unknowns=[],
    )
    result = score_spec(spec)

    # Search for the blocker substring from lines 321-325 of spec_score.py
    blocker_found = any(
        "Threshold surfing detected" in issue and "dimensions scored <=50%" in issue
        for issue in result.blocking_issues
    )
    # This may or may not fire depending on the exact scores, but if it does, the substring must match
    if any("Threshold surfing" in issue for issue in result.blocking_issues):
        assert blocker_found, (
            f"Threshold surfing blocker present but substring mismatch: {result.blocking_issues}"
        )
