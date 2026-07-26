"""Calibration attacks on spec_score.py — does the gate measure what it claims?

`test_gaming_resistance.py` asks "can a *badly-formed* spec score well?". This file
asks the harder question: **can a spec that was never derived from a recording at
all score well by imitating the shape of one?** Every attack here is well-formed,
distinct, non-padded, and specific. None of them would trip a single existing
check. They are what a competent fabricator produces, and each one below was
measured passing the gate before the fixes in this commit.

The distinction matters because the two files defend different things. Gaming
resistance defends against *sloppy* inflation — duplicates, keyword salad, absent
sections. Calibration defends against *fluent* inflation, where the attacker has
read the scorer and writes exactly what it rewards. Fluent inflation is the
realistic threat, because the refinement loop is itself a fluent writer: whatever
the scorer rewards, the loop converges on.

Attack classes proven here:

* C1 — Evidence-trail deletion: `spec.evidence` (the top-level provenance trail)
  was never read by any dimension, so a spec could carry none at all.
* C2 — Confidence overclaim: `confidence=1.0` scored the same 20/20 honesty as
  `0.0`, though the deriver's own ceiling is 0.7.
* C3/C4 — Deletion pays: removing observed entities and removing the builder's own
  honest boilerplate each *raised* the total.
* C5 — Inference concealment: declaring an inferred entity cost 8 points, so
  hiding it paid — the exact inversion the honesty dimension exists to prevent.
* C6 — Minimal-evidence evasion: the "placeholder detail" floor was 1 character.

Each test states the measured pre-fix number in its assertion message, so a
regression tells you what it regressed *to*.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from sf_video_blueprint.markers import REAL_EXTRACTION_SOURCES, REAL_TELEMETRY_SOURCES
from sf_video_blueprint.pipeline import run_pipeline
from sf_video_blueprint.spec_builder import (
    DerivedAgentSpec,
    DerivedEntity,
    SpecEvidence,
)
from sf_video_blueprint.spec_score import (
    MIN_EVIDENCE_DETAIL_CHARS,
    PASS_THRESHOLD,
    _score_evidence_grounding,
    _score_testability,
    score_spec,
)

# The builder's real evidence trail: build_agent_spec always appends at least the
# "N action(s) in recording" entry, so requiring a non-empty trail cannot block
# honest output. See spec_builder.build_agent_spec.
_REAL_TRAIL = [
    SpecEvidence("telemetry", "backend layers observed: validation, workflow"),
    SpecEvidence("extraction", "10 action(s) in recording"),
    SpecEvidence("data-delta", "objects mutated: Case"),
]

_REAL_PROVENANCE = {"extraction_source": "dom-capture", "telemetry_source": "live-org"}

# The shipped example capture. Used as the control for "would this fix break the
# only real derived output in the repo?" run_pipeline is offline and side-effect
# free, so this stays a pure unit test.
EXAMPLE_CAPTURE = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"
ORG_URL = "https://example.my.salesforce.com"


def _fluent_spec(**overrides) -> DerivedAgentSpec:
    """A spec that is well-formed on every axis the older tests check.

    No duplicates, no padding, no placeholder markers, concrete API names, an
    observed-failure line, a specific guardrail. This is the shape a fabricator
    imitates, and the baseline every attack below mutates.
    """
    base = {
        "intent": "Escalate Case Priority",
        "confidence": 0.7,
        "objects_touched": ["Case"],
        "entities": [
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority changed 'Low' -> 'High' at step-004")],
            ),
        ],
        "orchestration_steps": [
            "Resolve and load the target Case record; confirm the caller may act on it.",
            "SUBMIT on button:Escalate -> writes Priority (backend: validation)",
        ],
        "guardrails": ["Enforce object- and field-level security on Case for the running user."],
        "failure_handling": [
            "Observed validation failure during recording: Priority must be one of approved values"
        ],
        "unknowns": [],
        "evidence": list(_REAL_TRAIL),
    }
    base.update(overrides)
    return DerivedAgentSpec(**base)


# --- C1: the top-level evidence trail was never scored -----------------------


def test_c1_spec_with_no_evidence_trail_must_not_pass():
    """C1: A spec carrying NO top-level evidence trail must not clear the gate.

    MEASURED PRE-FIX: total=95/100, passed=True, with `evidence=[]`.

    `spec.evidence` is the spec's provenance trail — which telemetry layers were
    seen, how many actions were extracted, which objects were mutated. It is the
    only field that describes the *run* rather than the *conclusions*, and it was
    read by exactly zero dimensions. A fabricator could therefore delete the
    entire audit trail and lose nothing, which is backwards: the audit trail is
    the cheapest thing to keep when a recording is real and the hardest thing to
    forge convincingly when it is not.

    Why passing this is dangerous: it makes the gate indifferent to whether a run
    happened. Every other dimension scores the fabricator's *prose*; this is the
    one field that scores their *evidence*.
    """
    attack = _fluent_spec(evidence=[])

    result = score_spec(attack)

    assert not result.passed, (
        f"C1 (evidence-trail deletion) PASSED with {result.total}/100. "
        "A spec with an empty top-level evidence trail describes no observed run. "
        "Pre-fix this scored 95/100 and passed."
    )
    assert any("evidence trail" in issue.lower() for issue in result.blocking_issues), (
        f"C1 blocked, but not for the right reason. Blocking issues: {result.blocking_issues}"
    )


def test_c1_evidence_trail_requirement_does_not_block_honest_output():
    """C1 CONTROL: the builder's real trail must satisfy the new requirement.

    build_agent_spec unconditionally appends "N action(s) in recording", so the
    honest path always has a trail. If this test fails, the C1 fix is too strict
    and blocks genuine recordings — which is worse than the hole it closed.
    """
    honest = _fluent_spec()

    result = score_spec(honest, provenance=_REAL_PROVENANCE)

    assert result.passed, (
        f"C1 CONTROL FAILED: honest spec scored {result.total}/100, "
        f"blocking={result.blocking_issues}. The evidence-trail check must not block real output."
    )


# --- C2: confidence was unscored whenever structure was complete -------------


def test_c2_confidence_overclaim_must_cost_honesty():
    """C2: confidence=1.0 must score lower on honesty than confidence=0.7.

    MEASURED PRE-FIX: honesty=20/20 at every confidence from 0.0 to 1.0, and
    total=95/100 passed=True at confidence=1.0.

    The honesty dimension's `not has_gaps` branch awarded full marks regardless of
    the confidence value, so the field was inert whenever objects and entities
    were present. But `_derive_intent` never emits above **0.7** — 0.7 for a
    single object with observed field changes, less for anything weaker. A spec
    claiming 1.0 therefore asserts more certainty than the deriver is capable of
    producing, which is the textbook definition of the overclaim this dimension
    exists to catch.

    Why passing this is dangerous: confidence is the number a human reads first.
    A gate that ignores it teaches the loop that confidence is a free parameter.
    """
    honest = score_spec(_fluent_spec(confidence=0.7), provenance=_REAL_PROVENANCE)
    overclaim = score_spec(_fluent_spec(confidence=1.0), provenance=_REAL_PROVENANCE)

    assert overclaim.dimensions["honesty"].score < honest.dimensions["honesty"].score, (
        f"C2 (confidence overclaim) UNDETECTED: honesty scored "
        f"{overclaim.dimensions['honesty'].score}/20 at confidence=1.0 and "
        f"{honest.dimensions['honesty'].score}/20 at confidence=0.7. "
        "Pre-fix both were 20/20, making the confidence field inert."
    )
    assert any("confidence" in f.lower() for f in overclaim.dimensions["honesty"].findings), (
        f"C2: overclaim penalised but not explained. Findings: {overclaim.dimensions['honesty'].findings}"
    )


def test_c2_builder_confidence_ceiling_is_not_penalised():
    """C2 CONTROL: 0.7 is the deriver's maximum and must score full honesty.

    If this fails, the C2 fix penalises the honest ceiling, which would make every
    genuine single-object recording look dishonest.
    """
    result = score_spec(_fluent_spec(confidence=0.7), provenance=_REAL_PROVENANCE)

    assert result.dimensions["honesty"].score == 20, (
        f"C2 CONTROL FAILED: the deriver's own ceiling (0.7) scored "
        f"{result.dimensions['honesty'].score}/20 honesty. Honest output must not be penalised."
    )


# --- C3: deleting observed entities must never raise the score --------------


def test_c3_deleting_observed_entities_must_not_raise_total():
    """C3: Removing genuinely-observed entities must not increase the score.

    MEASURED PRE-FIX: deleting the 3 ui-action entities that the builder emitted
    without a resolved field (`object_api_name=None`) raised the example capture's
    total from 84 to 89 — **+5 points for deleting observed evidence**.

    Root cause: `_score_testability` used `all(...)` over entities, so a single
    unresolved entity zeroed half the dimension. The cheapest way to earn those 5
    points was to delete the entities rather than resolve them.

    Why passing this is dangerous: it is a direct incentive to suppress observed
    data. The loop would learn that the highest-scoring spec is the one that
    mentions the least. That inverts the entire premise of evidence grounding.
    """
    full = _fluent_spec(
        entities=[
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority changed 'Low' -> 'High' at step-004")],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "a Case record must be identified to act on it")],
            ),
            # Observed in the UI but the builder could not resolve it to a field.
            DerivedEntity(
                name="subject",
                object_api_name=None,
                field_api_name=None,
                evidence=[SpecEvidence("ui-action", "input on 'input:Subject' at step step-003")],
            ),
        ]
    )
    pruned = dataclasses.replace(
        full, entities=[e for e in full.entities if e.object_api_name and e.field_api_name]
    )

    before = score_spec(full, provenance=_REAL_PROVENANCE)
    after = score_spec(pruned, provenance=_REAL_PROVENANCE)

    assert after.total <= before.total, (
        f"C3 (deletion pays) CONFIRMED: pruning observed entities moved the total "
        f"{before.total} -> {after.total} (+{after.total - before.total}). "
        "Deleting observed evidence must never pay."
    )
    assert after.dimensions["testability"].score <= before.dimensions["testability"].score, (
        f"C3: testability rose {before.dimensions['testability'].score} -> "
        f"{after.dimensions['testability'].score} when observed entities were deleted."
    )


def test_c3_testability_is_reachable_not_dead_weight():
    """C3/brief-Q2: prove testability is EARNABLE, so 0/10 is a real deficiency.

    The brief asks whether testability scores 0 on the example because the spec is
    genuinely deficient or because the dimension cannot be earned at all. A
    dimension that is unreachable in practice is dead weight and misstates the
    total, so this distinction has to be settled by construction rather than
    argument.

    This test constructs the spec that *should* earn it and asserts 10/10:
    entities with explicit object+field names, plus a failure the recording
    actually observed. Both halves are reachable. Therefore the example's 0/10 is
    a statement about the example (no failing variant was recorded), not a defect
    in the dimension.
    """
    earning = _fluent_spec()

    assert _score_testability(earning).score == 10, (
        f"testability scored {_score_testability(earning).score}/10 on a spec built "
        "specifically to earn it. If this is not 10, the dimension is unreachable "
        "and should be removed from the weights rather than silently dragging every total down."
    )

    # And the half that the example genuinely fails: no observed failure path.
    untested = _fluent_spec(
        failure_handling=[
            (
                "No failures were observed in this run, so error paths are UNTESTED. "
                "Record a failing variant before relying on this spec."
            )
        ]
    )
    partial = _score_testability(untested)
    assert partial.score == 5, (
        f"Expected exactly half credit (5/10) when entities are explicit but no failure "
        f"was observed, got {partial.score}/10."
    )


# --- C4: deleting the builder's own honest prose must not raise the score ----


def test_c4_deleting_builder_boilerplate_must_not_raise_specificity():
    """C4: Removing an honest builder-emitted step must not raise specificity.

    MEASURED PRE-FIX: the example capture scored specificity 9/10, and the missing
    point came from the builder's OWN closing step, "Return a confirmation that
    names the record and the fields changed." — because the generic-term list
    substring-matched "the record" and "the field" inside it. Deleting that step
    scored 10/10.

    So the deduction was a tax on honest phrasing that an attacker pays nothing to
    avoid (just don't use the article "the") while the builder cannot avoid it at
    all. A check that only fires on the honest path is worse than no check: it
    creates a deletion incentive and reports a defect that isn't one.

    Why passing this is dangerous: it prices honest output below terse output for
    reasons unrelated to evidence.
    """
    honest_closing = "Return a confirmation that names the record and the fields changed."
    with_step = _fluent_spec(
        orchestration_steps=[
            "Resolve and load the target Case record; confirm the caller may act on it.",
            "SUBMIT on button:Escalate -> writes Priority (backend: validation)",
            honest_closing,
        ]
    )
    without_step = dataclasses.replace(
        with_step, orchestration_steps=with_step.orchestration_steps[:-1]
    )

    kept = score_spec(with_step, provenance=_REAL_PROVENANCE)
    dropped = score_spec(without_step, provenance=_REAL_PROVENANCE)

    assert kept.dimensions["specificity"].score >= dropped.dimensions["specificity"].score, (
        f"C4 (honesty tax) CONFIRMED: specificity rose "
        f"{kept.dimensions['specificity'].score} -> {dropped.dimensions['specificity'].score} "
        "when the builder's own honest closing step was deleted."
    )


def test_c4_vacuous_steps_and_guardrails_are_still_penalised():
    """C4 CONTROL: the vacuous-content checks must survive the C4 fix.

    The C4 fix relaxes substring matching on ordinary English articles. It must NOT
    relax the checks that catch actually-vacuous content, or it becomes a
    weakening. "Step 1" / "Guardrail 2" must still cost points.
    """
    vacuous = _fluent_spec(
        orchestration_steps=["Step 1", "Step 2"],
        guardrails=["Guardrail 1"],
    )
    concrete = _fluent_spec()

    v = score_spec(vacuous)
    c = score_spec(concrete)

    assert v.dimensions["specificity"].score < c.dimensions["specificity"].score, (
        f"C4 CONTROL FAILED: vacuous steps/guardrails scored "
        f"{v.dimensions['specificity'].score}/10, concrete scored "
        f"{c.dimensions['specificity'].score}/10. The vacuous-content check was lost."
    )
    assert not v.passed, "A spec whose steps are 'Step 1'/'Step 2' must not pass."


# --- C7: one dimension at zero was survivable ------------------------------


def test_c7_a_single_hollowed_out_dimension_must_block():
    """C7: A spec scoring literally 0 on specificity must not pass at all.

    MEASURED PRE-FIX: steps `["Step 1","Step 2","Step 3"]` and guardrails
    `["Guardrail 1","Rule 2"]` scored **specificity 0/10 and total 82/100,
    passed=True**. The threshold-surfing detector did not fire because it requires
    **>=2** dimensions at or below 50%, and here exactly one dimension was
    sacrificed — completely.

    So the detector caught the *diffuse* version of the attack (shave several
    dimensions) and missed the *concentrated* version (delete one entirely), which
    is strictly easier to execute and cheaper in points: 10 points buys you a spec
    whose every orchestration step is the word "Step".

    A dimension at literal zero is not a weak signal, it is an absent one. It means
    the spec has no measurable content on an axis the gate claims to measure, and
    averaging that into a total labelled "82/100" misrepresents it.

    Note the deliberate exclusions in the fix: `testability` and
    `provenance_integrity` may legitimately be 0 on an honest run (no failing
    variant was recorded; in-memory scoring supplies no provenance). Blocking on
    those would make the gate unclearable by honest output, which the brief and
    LANE_RULES both forbid as worse than the hole.
    """
    hollow = _fluent_spec(
        orchestration_steps=["Step 1", "Step 2", "Step 3"],
        guardrails=["Guardrail 1", "Rule 2"],
    )

    result = score_spec(hollow)

    assert result.dimensions["specificity"].score == 0, (
        f"This test needs specificity to be exactly 0; got "
        f"{result.dimensions['specificity'].score}/10."
    )
    assert not result.passed, (
        f"C7 (single hollowed dimension) PASSED with {result.total}/100 while "
        "specificity scored 0/10. Pre-fix this scored 82/100 and passed."
    )
    assert any("scored 0" in issue or "no measurable" in issue.lower() for issue in result.blocking_issues), (
        f"C7 blocked, but not for the hollow-dimension reason: {result.blocking_issues}"
    )


def test_c7_honest_zero_testability_must_still_be_able_to_pass():
    """C7 CONTROL: testability=0 must NOT block, or no happy-path recording passes.

    A recording of a process that simply succeeded has no observed failure, so
    testability legitimately loses its failure half. If the C7 hollow-dimension
    blocker included testability, every honest happy-path capture would be blocked
    and the gate would be unclearable. This test pins that exclusion.
    """
    happy_path = _fluent_spec(
        entities=[
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority changed 'Low' -> 'High' at step-004")],
            ),
        ],
        failure_handling=[
            (
                "No failures were observed in this run, so error paths are UNTESTED. "
                "Record a failing variant before relying on this spec."
            )
        ],
    )

    result = score_spec(happy_path, provenance=_REAL_PROVENANCE)

    assert result.dimensions["testability"].score < 10, "Expected reduced testability."
    assert result.passed, (
        f"C7 CONTROL FAILED: an honest happy-path recording scored {result.total}/100 "
        f"and was blocked by {result.blocking_issues}. A recording with no failure "
        "variant must still be able to clear the gate."
    )


def test_c4_explicitly_generic_language_is_still_penalised():
    """C4 CONTROL: self-describing placeholder language must still be caught."""
    generic = _fluent_spec(
        orchestration_steps=[
            "Perform the generic operation on the placeholder target",
            "Apply a generic transformation",
        ],
    )
    result = score_spec(generic)

    assert result.dimensions["specificity"].score < 10, (
        f"C4 CONTROL FAILED: steps containing 'generic'/'placeholder' scored full "
        f"specificity ({result.dimensions['specificity'].score}/10)."
    )


# --- C5: concealing an inferred entity must not pay -------------------------


def test_c5_declaring_an_inferred_entity_must_not_lower_the_score():
    """C5: Adding a declared-inference entity must not lower evidence_grounding.

    MEASURED PRE-FIX: one data-delta entity scored 30/30. Adding a second entity
    whose evidence is honestly labelled `inference` dropped it to 22/30 — so
    **hiding the inferred entity paid 8 points**.

    Cause: `coverage_bonus` was `well_grounded / total_entities`, a ratio. Any
    honestly-labelled inference entity dilutes the denominator. The docstring
    claimed monotonicity, and it is true as written ("adding a data-delta entity
    never lowers it") — but the untested direction is the one that matters, because
    inference entities are exactly what an honest deriver emits when it cannot
    resolve a field.

    Why passing this is dangerous: this is the same inversion `_score_honesty`
    documents as "the worst possible outcome" — training the loop to hide gaps —
    reintroduced through the arithmetic of a different dimension. Declaring beats
    concealing must hold in *every* dimension, not just the one named honesty.
    """
    grounded_only = _fluent_spec()
    plus_inference = _fluent_spec(
        entities=[
            *grounded_only.entities,
            DerivedEntity(
                name="reason",
                object_api_name="Case",
                field_api_name="Reason",
                evidence=[SpecEvidence("inference", "Reason inferred from the picklist label observed at step-006")],
            ),
        ]
    )

    concealed = _score_evidence_grounding(grounded_only)
    declared = _score_evidence_grounding(plus_inference)

    assert declared.score >= concealed.score, (
        f"C5 (inference concealment) CONFIRMED: evidence_grounding scored "
        f"{concealed.score}/30 when the inferred entity was hidden and {declared.score}/30 "
        f"when it was declared — concealing pays {concealed.score - declared.score} points."
    )


def test_c5_inference_only_spec_still_scores_below_observed_spec():
    """C5 CONTROL: the C5 fix must not make inference as good as observation.

    Removing the ratio must not remove the *floor* distinction. A spec grounded
    only in inference must still score materially below one grounded in observed
    deltas, or evidence_grounding stops measuring evidence.
    """
    observed = _score_evidence_grounding(_fluent_spec())
    inferred = _score_evidence_grounding(
        _fluent_spec(
            entities=[
                DerivedEntity(
                    name="priority",
                    object_api_name="Case",
                    field_api_name="Priority",
                    evidence=[SpecEvidence("inference", "Priority assumed to change based on the button label")],
                ),
            ]
        )
    )

    assert inferred.score < observed.score, (
        f"C5 CONTROL FAILED: an inference-only spec scored {inferred.score}/30 and an "
        f"observed spec scored {observed.score}/30. Observation must outrank assumption."
    )
    assert inferred.score == 0, (
        f"An all-inference spec should earn no grounding floor, got {inferred.score}/30."
    )


def test_c5_fix_does_not_let_stub_entities_farm_the_coverage_bonus():
    """C5 CONTROL: counting well-grounded entities must not become a farmable meter.

    The C5 fix replaced a ratio (`well_grounded / total_entities`) with a count
    (`min(well_grounded * 0.25, 0.50)`). A count is the gaming-resistant direction
    for the honest deriver — declaring an inference entity no longer dilutes the
    denominator — but it raises the mirror question: can a fabricator mint the bonus
    by listing many entities whose evidence is nominally `data-delta` but
    substanceless?

    MEASURED POST-FIX: 8 entities with `detail="ab"` cap at 15/30, while 2 entities
    with real 40+ character details reach 30/30. The minimal-detail check (C6) fires
    per entity and disqualifies each stub from `well_grounded`, so the count cannot
    be farmed with empty rows: adding stubs 3..8 buys exactly 0 additional points.

    This is a control, not a fix — it exists so a future change to either the
    coverage bonus or the detail floor cannot silently open the vector.
    """

    def stub_entity(index: int) -> DerivedEntity:
        return DerivedEntity(
            name=f"f{index}",
            object_api_name="Case",
            field_api_name=f"F{index}__c",
            evidence=[SpecEvidence("data-delta", "ab")],
        )

    few_stubs = _score_evidence_grounding(_fluent_spec(entities=[stub_entity(0), stub_entity(1)]))
    many_stubs = _score_evidence_grounding(
        _fluent_spec(entities=[stub_entity(i) for i in range(8)])
    )
    two_real = _score_evidence_grounding(
        _fluent_spec(
            entities=[
                DerivedEntity(
                    name="priority",
                    object_api_name="Case",
                    field_api_name="Priority",
                    evidence=[SpecEvidence("data-delta", "Case.Priority changed 'Low' -> 'High' at step-004")],
                ),
                DerivedEntity(
                    name="reason",
                    object_api_name="Case",
                    field_api_name="Reason",
                    evidence=[SpecEvidence("data-delta", "Case.Reason changed '' -> 'Escalated' at step-005")],
                ),
            ]
        )
    )

    assert many_stubs.score == few_stubs.score, (
        f"Padding 2 stub entities out to 8 moved evidence_grounding "
        f"{few_stubs.score}/30 -> {many_stubs.score}/30. The coverage bonus counts "
        "well-grounded entities, so stub rows must buy nothing."
    )
    assert many_stubs.score < two_real.score, (
        f"8 stub entities scored {many_stubs.score}/30 while 2 real ones scored "
        f"{two_real.score}/30. Quantity of stubs must never beat quality of evidence."
    )


# --- C6: the placeholder-detail floor was one character ----------------------


def test_c6_two_character_evidence_detail_must_not_earn_full_grounding():
    """C6: A 2-character evidence detail must not earn full evidence_grounding.

    MEASURED PRE-FIX: `detail="x"` scored 15/30 (the minimal-evidence penalty
    fired), but `detail="ab"` scored 30/30 — full marks. The check was
    `len(detail.strip()) <= 1`, so one extra character evaded it completely.

    The builder's own shortest entity evidence detail on the example capture is 41
    characters ("input on 'input:Subject' at step step-003"), so the gap between
    "what honest output looks like" and "what the check tolerates" was 39
    characters wide.

    This check is inherently weak — an attacker who pads to 40 characters of
    plausible prose defeats any length floor. It is reported as a *floor*, not a
    defence; the real defence lives in the builder, which cannot fabricate a
    detail it did not observe.
    """
    trivial = _score_evidence_grounding(
        _fluent_spec(
            entities=[
                DerivedEntity(
                    name="priority",
                    object_api_name="Case",
                    field_api_name="Priority",
                    evidence=[SpecEvidence("data-delta", "ab")],
                ),
            ]
        )
    )
    real = _score_evidence_grounding(_fluent_spec())

    assert trivial.score < real.score, (
        f"C6 (minimal-evidence evasion) CONFIRMED: a 2-character evidence detail scored "
        f"{trivial.score}/30, same tier as a real 55-character one at {real.score}/30. "
        "Pre-fix 'ab' scored a full 30/30."
    )


# --- Brief Q1: a blocked total must not read as a good score -----------------


def test_blocked_spec_display_total_is_capped_into_the_low_band():
    """BRIEF Q1: a blocked spec must not *display* a high number.

    The example capture scores 79/100 with `band=low, passed=False`, blocked on
    mock telemetry. But "79/100" is the number a human's eye lands on, and 79 out
    of 100 reads as "nearly there" when the correct reading is "this spec is not
    evidence-backed at all". The pass/fail boolean is correct; the *presentation*
    invites the misread.

    The fix keeps `total` as the raw dimension sum — `iterate.py` compares totals
    across versions to detect improvement and convergence, so capping `total`
    itself would flatten every blocked version to the same number and destroy the
    loop's gradient. Instead `display_total` is capped below the moderate band
    whenever a blocker is present, and `summary()` reports that. The gradient
    survives; the misread does not.
    """
    blocked = score_spec(
        _fluent_spec(), provenance={"extraction_source": "dom-capture", "telemetry_source": "mock"}
    )

    assert blocked.blocking_issues, "Expected a mock-telemetry blocker."
    assert not blocked.passed
    assert blocked.total >= 60, (
        f"This test needs a spec whose RAW total is high while blocked; got {blocked.total}."
    )
    assert blocked.display_total < 60, (
        f"A blocked spec displayed {blocked.display_total}/100, which reads as a "
        "moderate score. A blocked spec must display inside the low band."
    )
    # The raw total is preserved for the refinement loop's gradient.
    assert blocked.total != blocked.display_total, (
        "display_total must differ from total when blocked, or the gradient is lost."
    )
    assert str(blocked.display_total) in blocked.summary(), (
        f"summary() must report the capped total. Got: {blocked.summary()!r}"
    )
    assert "blocked" in blocked.summary().lower(), (
        f"summary() must say the score is blocked. Got: {blocked.summary()!r}"
    )


def test_unblocked_spec_display_total_equals_total():
    """BRIEF Q1 CONTROL: with no blockers, display_total must not distort anything."""
    clean = score_spec(_fluent_spec(), provenance=_REAL_PROVENANCE)

    assert not clean.blocking_issues, f"Expected no blockers, got {clean.blocking_issues}"
    assert clean.display_total == clean.total, (
        f"Unblocked spec: display_total {clean.display_total} != total {clean.total}."
    )


def test_display_total_is_serialised_for_downstream_consumers():
    """BRIEF Q1: the capped total must survive to_dict(), or reports miss it."""
    blocked = score_spec(
        _fluent_spec(), provenance={"extraction_source": "stub", "telemetry_source": "mock"}
    )
    payload = blocked.to_dict()

    assert "display_total" in payload, "to_dict() must expose display_total."
    assert payload["display_total"] == blocked.display_total
    assert payload["total"] == blocked.total, "Raw total must remain in to_dict() for the loop."


# --- The hard constraint: the gate itself must not have been weakened -------


def test_gate_constants_are_unchanged():
    """HARD CONSTRAINT: threshold and marker sets are exactly as specified.

    LANE_RULES rule 5 and the brief both fix these values. This test exists so any
    future edit that loosens them fails here rather than silently making
    fabrication invisible.
    """
    assert PASS_THRESHOLD == 75, f"PASS_THRESHOLD is {PASS_THRESHOLD}, must be 75."
    assert REAL_EXTRACTION_SOURCES == frozenset({"dom-capture", "cv"}), (
        f"REAL_EXTRACTION_SOURCES is {set(REAL_EXTRACTION_SOURCES)}, must be "
        "{'dom-capture', 'cv'}."
    )
    assert REAL_TELEMETRY_SOURCES == frozenset({"live-org"}), (
        f"REAL_TELEMETRY_SOURCES is {set(REAL_TELEMETRY_SOURCES)}, must be {{'live-org'}}."
    )
    assert "mock" not in REAL_TELEMETRY_SOURCES, "'mock' must never be a real telemetry source."
    assert "stub" not in REAL_EXTRACTION_SOURCES, "'stub' must never be a real extraction source."


def test_no_fix_in_this_file_made_the_gate_reachable_without_evidence():
    """HARD CONSTRAINT: the fully-fabricated spec must still fail after the fixes.

    This is the composite of every attack above: fluent prose, concrete API names,
    an observed-failure line, no padding, maximal confidence, and no evidence
    trail. MEASURED PRE-FIX: 95/100 passed=True (and 100/100 with claimed real
    provenance). It must fail on the evidence trail alone, and it must still fail
    even when the attacker asserts real provenance strings, because provenance is
    self-declared text and cannot be the only thing standing between fabrication
    and a pass.
    """
    fabricated = _fluent_spec(confidence=1.0, evidence=[])

    in_memory = score_spec(fabricated)
    assert not in_memory.passed, (
        f"Fully-fabricated spec PASSED in-memory with {in_memory.total}/100."
    )

    claimed_real = score_spec(fabricated, provenance=_REAL_PROVENANCE)
    assert not claimed_real.passed, (
        f"Fully-fabricated spec PASSED with self-claimed real provenance at "
        f"{claimed_real.total}/100. Provenance strings are attacker-controlled text."
    )


# --- C8: filler instructions survived every fix above -----------------------


def test_c8_filler_instruction_text_must_not_pass():
    """C8: A spec whose entire instruction set is meaningless tokens must not pass.

    This attack survived every fix above. MEASURED against the fixes in this file:
    100/100, passed=True, all seven dimensions at full marks.

    The point of an agent spec is the instructions it gives: `orchestration_steps`
    and `guardrails` are the text a human reads to decide whether to trust the
    agent, and the text downstream builders turn into topic and action bodies.
    Here they are `"aa"`, `"bb"`, `"ee"` and `"cc"`, `"ff"`. The spec is worthless,
    and it earned a perfect score.

    It survives because every check upstream of the words is satisfied: the
    entities are concrete and well-evidenced (so evidence_grounding and
    testability are genuinely earned), the evidence trail is present, confidence
    is at the builder's ceiling, and an unknown is declared. Then completeness
    counts the steps (3 distinct, no padding), specificity finds no known-bad
    phrase in them, and placeholder_freedom finds no marker. Nothing anywhere
    asks whether the instruction strings say anything.

    That gap is exactly the direction a refinement loop optimises: the entity
    metadata is expensive to fabricate, but the prose is free, so the cheapest way
    to a perfect score is real-looking metadata with the narrative hollowed out.
    """
    attack = _fluent_spec(
        orchestration_steps=["aa", "bb", "ee"],
        guardrails=["cc", "ff"],
        unknowns=["gg"],
        entities=[
            DerivedEntity(
                name="q",
                object_api_name="Case",
                field_api_name="Q",
                evidence=[SpecEvidence("data-delta", "Case.Q changed '1' -> '2' at step-003")],
            ),
            DerivedEntity(
                name="r",
                object_api_name="Case",
                field_api_name="R",
                evidence=[SpecEvidence("data-delta", "Case.R changed '3' -> '4' at step-004")],
            ),
        ],
    )

    result = score_spec(attack, provenance=_REAL_PROVENANCE)

    assert not result.passed, (
        f"C8 FILLER INSTRUCTIONS PASSED with {result.total}/100. The whole instruction "
        f"set is {attack.orchestration_steps + attack.guardrails}. Dimensions: "
        f"{ {n: d.score for n, d in result.dimensions.items()} }"
    )


def test_c8_filler_must_not_outrank_real_derived_output():
    """C8: the ordering property — filler must never beat the real pipeline's output.

    More important than any threshold: if hollow text outranks genuinely derived
    output, the gate is actively pointing the loop away from real evidence.
    """
    real = run_pipeline(EXAMPLE_CAPTURE, org_url=ORG_URL).spec
    real_score = score_spec(real, provenance=_REAL_PROVENANCE)

    attack = _fluent_spec(orchestration_steps=["aa", "bb", "ee"], guardrails=["cc", "ff"])
    attack_score = score_spec(attack, provenance=_REAL_PROVENANCE)

    assert attack_score.total < real_score.total, (
        f"C8 ORDERING VIOLATED: filler scored {attack_score.total}, real derived output "
        f"scored {real_score.total}."
    )


def test_c8_real_and_control_instruction_text_is_not_flagged_as_filler():
    """C8 CONTROL: the filler rule must not fire on genuine instruction text.

    A rule that rejects honest output is worse than the hole it closes. This pins
    both the real builder's own steps and the fluent control spec as acceptable.
    """
    real = run_pipeline(EXAMPLE_CAPTURE, org_url=ORG_URL).spec
    real_result = score_spec(real, provenance=_REAL_PROVENANCE)
    flagged = [b for b in real_result.blocking_issues if "filler" in b.lower()]
    assert not flagged, f"C8 FALSE POSITIVE on genuine builder output: {flagged}"
    assert real_result.passed, (
        f"C8 REGRESSION: real derived output failed at {real_result.total}/100: "
        f"{real_result.blocking_issues}"
    )

    control = score_spec(_fluent_spec(), provenance=_REAL_PROVENANCE)
    assert control.passed, (
        f"C8 FALSE POSITIVE: the fluent control spec failed at {control.total}/100: "
        f"{control.blocking_issues}"
    )


# --- C9: the minimal-evidence penalty made deleting an entity pay +15 --------


def test_c9_deleting_a_weakly_evidenced_entity_must_not_raise_the_score():
    """C9: G1 violation — removing an observed entity paid the biggest reward found.

    MEASURED PRE-FIX, with two well-evidenced entities plus one whose evidence
    detail was "ab": evidence_grounding 15/30, total 85. Deleting the weak entity
    scored 30/30 and total 100 — **deleting an observed entity paid +15 points**.

    Cause: the minimal-evidence rule was `score = int(score * 0.5)` — halve the
    WHOLE dimension if ANY entity carries a stub detail. Multiplicative and
    collective, so the weak entity did not merely fail to earn credit, it
    confiscated credit the other entities had earned. The cheapest response is
    always to delete the row rather than resolve it.

    This is the same class of defect as C3 (`all()` over entity bindings) and the
    D10 weighted-average it replaced, and it is the one `_score_evidence_grounding`'s
    own docstring claims cannot happen: "removing one never raises it". The claim
    was true of the formula and false of the function, because the penalty was
    applied after the formula.

    The fix moves the check upstream into the counting loop: an entity whose detail
    is a stub does not count toward `well_grounded`, so it earns nothing and costs
    nothing. Keeping it is then weakly better than deleting it, which is the
    gradient the gate should expose — resolve the evidence, don't remove the row.
    """
    strong = [
        DerivedEntity(
            name="priority",
            object_api_name="Case",
            field_api_name="Priority",
            evidence=[SpecEvidence("data-delta", "Case.Priority changed 'Low' -> 'High' at step-004")],
        ),
        DerivedEntity(
            name="status",
            object_api_name="Case",
            field_api_name="Status",
            evidence=[SpecEvidence("data-delta", "Case.Status changed 'New' -> 'Working' at step-005")],
        ),
    ]
    weak = DerivedEntity(
        name="reason",
        object_api_name="Case",
        field_api_name="Reason",
        evidence=[SpecEvidence("data-delta", "ab")],
    )

    kept = score_spec(_fluent_spec(entities=[*strong, weak]), provenance=_REAL_PROVENANCE)
    deleted = score_spec(_fluent_spec(entities=list(strong)), provenance=_REAL_PROVENANCE)

    assert deleted.total <= kept.total, (
        f"C9 (deletion reward) CONFIRMED: keeping a weakly-evidenced observed entity "
        f"scored {kept.total}/100 and deleting it scored {deleted.total}/100 — deletion "
        f"pays {deleted.total - kept.total} points. Pre-fix this delta was +15."
    )
    assert deleted.dimensions["evidence_grounding"].score <= kept.dimensions["evidence_grounding"].score, (
        f"evidence_grounding rose from {kept.dimensions['evidence_grounding'].score}/30 to "
        f"{deleted.dimensions['evidence_grounding'].score}/30 on deletion."
    )


def test_c9_stub_evidence_still_earns_nothing():
    """C9 CONTROL: removing the multiplier must not make stub evidence acceptable.

    The C9 fix stops a stub entity from confiscating other entities' credit. It must
    not go further and let stub evidence *earn* credit — otherwise the fix trades a
    deletion reward for a fabrication reward, which is a worse bargain.

    MEASURED POST-FIX: a spec whose every entity detail is "ab" scores
    evidence_grounding 0/30 and total 70, below the threshold of 75, where pre-fix
    it scored 15/30 and total 85 with `passed=True`.
    """
    all_stubs = _fluent_spec(
        entities=[
            DerivedEntity(
                name=f"f{index}",
                object_api_name="Case",
                field_api_name=f"F{index}__c",
                evidence=[SpecEvidence("data-delta", "ab")],
            )
            for index in range(3)
        ]
    )

    result = score_spec(all_stubs, provenance=_REAL_PROVENANCE)

    assert result.dimensions["evidence_grounding"].score == 0, (
        f"Stub-only evidence earned {result.dimensions['evidence_grounding'].score}/30. "
        "An entity whose detail carries no observation must ground nothing."
    )
    assert not result.passed, (
        f"A spec whose every evidence detail is 'ab' PASSED at {result.total}/100."
    )
    assert result.dimensions["evidence_grounding"].findings, (
        "The deficiency must still be reported, not silently scored zero."
    )


def test_c9_real_builder_output_is_unaffected_by_the_evidence_floor():
    """C9 CONTROL: the floor must not fire on anything the builder emits.

    The builder's shortest evidence detail on the example capture is 41 characters
    ("select on 'input:Status' at step step-009") against a floor of 12, so this
    pins the margin. A regression that raised the floor above honest output would
    penalise every real run.
    """
    real = run_pipeline(EXAMPLE_CAPTURE, org_url=ORG_URL).spec

    shortest = min(
        len(ev.detail)
        for entity in real.entities
        for ev in entity.evidence
        if isinstance(ev.detail, str)
    )
    assert shortest >= MIN_EVIDENCE_DETAIL_CHARS, (
        f"The builder's shortest evidence detail is {shortest} chars, below the "
        f"{MIN_EVIDENCE_DETAIL_CHARS}-char floor. The floor now penalises honest output."
    )

    result = score_spec(real, provenance=_REAL_PROVENANCE)
    flagged = [f for f in result.dimensions["evidence_grounding"].findings if "minimal" in f.lower()]
    assert not flagged, f"C9 FALSE POSITIVE on genuine builder output: {flagged}"


# --- C10: fluent filler defeats the C8 length floor -------------------------


def test_c10_three_word_filler_must_not_pass():
    """C10: filler padded past the C8 floor must still not pass.

    MEASURED with the C8 fix in place: steps ``["do the thing here", "then do it
    again"]`` and guardrail ``["always be careful now"]`` score **92/100,
    passed=True** — a higher total than the real derived spec's 90.

    C8 measures the *shape* of an instruction (12 chars, 3 words). Shape is
    trivially cheap: any attacker who reads the constant writes four words of
    nothing and the deduction vanishes. That is the generic weakness of every
    length floor, and C8's own docstring concedes it ("Two words of plausible prose
    defeat it").

    So this asks a question shape cannot answer: does the instruction text
    *connect to the evidence the spec claims*? The spec asserts it observed
    ``Case.Priority`` change. If not one orchestration step so much as names
    ``Priority``, then the metadata and the narrative describe different things,
    and at most one of them came from a recording.

    That coupling is what makes the check expensive to evade honestly and cheap to
    satisfy honestly: the builder writes the field name into the step because it
    derived the step FROM the field delta (`_derive_orchestration` emits
    "-> writes Status"). A fabricator must keep two artifacts consistent instead
    of one.
    """
    attack = _fluent_spec(
        orchestration_steps=["do the thing here", "then do it again"],
        guardrails=["always be careful now"],
    )

    result = score_spec(attack, provenance=_REAL_PROVENANCE)

    assert not result.passed, (
        f"C10 FLUENT FILLER PASSED with {result.total}/100. Steps "
        f"{attack.orchestration_steps} never name the field the spec claims to have "
        f"observed (Case.Priority). Dimensions: "
        f"{ {n: d.score for n, d in result.dimensions.items()} }"
    )


def test_c10_padding_with_the_object_name_is_not_enough():
    """C10: naming the *object* must not satisfy the *field* coupling requirement.

    MEASURED with the C8 fix in place: 92/100, passed=True.

    The cheapest evasion of a coupling check is to sprinkle in the one name the
    attacker already wrote at the top of the spec. `objects_touched` is a single
    token they had to supply anyway, so accepting it would make the check free to
    pass. The field API names are the part that must have come from an observed
    data delta, so those are what the steps must reference.
    """
    attack = _fluent_spec(
        orchestration_steps=["Case aa bb cc dd", "Case ee ff gg hh"],
        guardrails=["Case ii jj kk ll"],
    )

    result = score_spec(attack, provenance=_REAL_PROVENANCE)

    assert not result.passed, (
        f"C10 OBJECT-NAME PADDING PASSED with {result.total}/100: naming 'Case' was "
        "accepted in place of naming the observed field."
    )


def test_c10_real_builder_output_satisfies_the_coupling_requirement():
    """C10 CONTROL: the builder cannot help but satisfy this, so it must not fire.

    `_derive_orchestration` builds each step from the observed delta and writes the
    field API names into the text ("-> writes Status"). This test pins that fact:
    if the builder's output is ever flagged as uncoupled, the check is wrong, not
    the builder.
    """
    real = run_pipeline(EXAMPLE_CAPTURE, org_url=ORG_URL).spec
    result = score_spec(real, provenance=_REAL_PROVENANCE)

    uncoupled = [issue for issue in result.blocking_issues if "name" in issue.lower() and "field" in issue.lower()]
    assert not uncoupled, f"C10 FALSE POSITIVE on real builder output: {uncoupled}"
    assert result.passed, (
        f"C10 REGRESSION: real derived output failed at {result.total}/100: {result.blocking_issues}"
    )


def test_c10_ui_only_capture_is_exempt_because_it_has_no_field_names():
    """C10 CONTROL: a UI-only recording must not be blocked by a field-name rule.

    When the DOM capture never correlates a field API name, `_derive_entities`
    emits entities with ``field_api_name=None``. There is no field for a step to
    name, so the requirement cannot apply — and must not, or the gate becomes
    unclearable for exactly the honest-but-weak recordings the brief warns about.
    """
    ui_only = _fluent_spec(
        confidence=0.4,
        entities=[
            DerivedEntity(
                name="subject",
                object_api_name=None,
                field_api_name=None,
                evidence=[SpecEvidence("ui-action", "input on 'input:Subject' at step-002")],
            ),
        ],
        orchestration_steps=[
            "Resolve and load the target Case record; confirm the caller may act on it.",
            "input on input:Subject then submit the form and wait for the write",
        ],
        failure_handling=["No failures were observed in this run, so error paths are UNTESTED."],
        unknowns=["No backend telemetry was correlated to any step."],
    )

    result = score_spec(ui_only, provenance=_REAL_PROVENANCE)

    uncoupled = [issue for issue in result.blocking_issues if "name" in issue.lower() and "field" in issue.lower()]
    assert not uncoupled, (
        f"C10 FALSE POSITIVE: a UI-only capture with no field API names was blocked "
        f"by the field-coupling rule: {uncoupled}"
    )


def test_c10_coupling_rule_cannot_be_evaded_by_deleting_the_entity():
    """C10 GAMING PROOF: dropping the field-bearing entity must not pay.

    The rule is conditional — it only applies when the spec declares a field API
    name — so the obvious evasion is to delete the entity that triggers it. That
    would recreate exactly the inversion C2/C3 fixed ("deletion pays").

    MEASURED: deleting the Case.Priority entity costs points overall (evidence
    grounding and testability both fall), so the evasion is strictly worse than
    writing a real step. This test pins the ordering, which is the property that
    matters — not the specific numbers.
    """
    uncoupled = _fluent_spec(
        orchestration_steps=["do the thing here", "then do it again"],
        guardrails=["always be careful now"],
    )
    evaded = dataclasses.replace(
        uncoupled,
        entities=[
            DerivedEntity(
                name="subject",
                object_api_name=None,
                field_api_name=None,
                evidence=[SpecEvidence("ui-action", "input on 'input:Subject' at step-002")],
            ),
        ],
    )

    evaded_score = score_spec(evaded, provenance=_REAL_PROVENANCE)

    assert not evaded_score.passed, (
        f"C10 EVASION SUCCEEDED: deleting the field-bearing entity to escape the "
        f"coupling rule scored {evaded_score.total}/100 and PASSED."
    )


# --- C11: the padding detector fired on the only real capture ----------------


def _unresolved_ui_entity(index: int, detail: str | None = None) -> DerivedEntity:
    """A UI-input entity the builder could not resolve to an object/field.

    This is the shape `spec_builder` emits for a keystroke into a Lightning custom
    element: a name derived from the tag, `object_api_name=None`,
    `field_api_name=None`, and one `ui-action` evidence entry naming the target and
    step id. 128 of the 130 entities on lane 02's real AFT3 capture look like this.
    """
    return DerivedEntity(
        name=f"lightningInput{index}",
        object_api_name=None,
        field_api_name=None,
        evidence=[
            SpecEvidence(
                "ui-action",
                detail or f"input on 'text:lightning-input#{index}' at step step-{index:03d}",
            )
        ],
    )


def test_c11_distinct_unresolved_entities_must_not_be_called_padding():
    """C11: the gate accused its own builder of a gaming attack on real output.

    MEASURED on lane 02's real AFT3 capture (`examples/case_creation_aft3`, 175
    events from an actual Case creation in a Developer Edition org): the builder
    emits 130 entities, 128 of them UI inputs into Lightning custom elements that
    could not be resolved to an object or field. All 128 have distinct names and
    distinct evidence details — 128 separately observed keystrokes.

    The gate reported: "PADDING DETECTED: Multiple entities target the same field:
    None.None (128x). This is likely an attack to inflate entity counts
    artificially." It cut evidence_grounding to 5/30, and the resulting two-dimension
    shortfall also tripped the threshold-surfing blocker. Raw total 70, and *three*
    blocking issues where only one (mock telemetry) was real.

    Cause: the detector keyed on `f"{object_api_name}.{field_api_name}"`, which is
    the literal string `"None.None"` when both are unresolved. Every unresolved
    entity collided in one bucket, so any four of them tripped a `> 3` threshold.

    This is the worst class of calibration defect in the lane, for two reasons.
    First, it fires ONLY on honest output — a fabricator writes concrete field names
    (that is what makes the spec look good) and never lands in the None bucket, so
    the check taxed exclusively the path it was meant to protect. Second, it does not
    merely dock points, it *names the builder an attacker*, which is the same
    accusation a real gaming attempt draws. A gate whose loudest signal on real data
    is a false accusation teaches its reader to discount the signal.

    Post-fix, the same real capture scores evidence_grounding 25/30 with the mock
    telemetry blocker as its only blocking issue — which is the honest verdict.
    """
    spec = _fluent_spec(
        entities=[*_fluent_spec().entities, *[_unresolved_ui_entity(i) for i in range(128)]]
    )

    result = score_spec(spec, provenance=_REAL_PROVENANCE)
    grounding = result.dimensions["evidence_grounding"]
    accusations = [f for f in grounding.findings if "PADDING" in f]

    assert not accusations, (
        f"C11 (false padding accusation) CONFIRMED: {len(spec.entities)} entities with "
        f"distinct names and distinct evidence details were reported as padding: "
        f"{accusations}. Pre-fix this scored evidence_grounding 5/30 on lane 02's real "
        "capture and raised a blocking issue."
    )
    assert result.passed, (
        f"Real-shaped output with 128 distinct unresolved entities failed at "
        f"{result.total}/100: {result.blocking_issues}"
    )


def test_c11_fix_does_not_open_an_unresolved_padding_vector():
    """C11 CONTROL: skipping unresolved entities must not make them a free vector.

    If unresolved entities were simply exempt from padding detection, an attacker
    could emit 200 identical `object=None, field=None` entities and collect the
    coverage bonus. The fix therefore still checks them — by evidence detail, which
    is what they claim to have observed, rather than by a field name they do not have.

    MEASURED POST-FIX: 10 unresolved entities sharing ONE identical detail are
    flagged and score 5/30; 10 unresolved entities with distinct details score 25/30.
    """
    identical = _fluent_spec(
        entities=[
            _unresolved_ui_entity(i, detail="input on 'text:x' at step step-001")
            for i in range(10)
        ]
    )
    distinct = _fluent_spec(entities=[_unresolved_ui_entity(i) for i in range(10)])

    identical_result = _score_evidence_grounding(identical)
    distinct_result = _score_evidence_grounding(distinct)

    assert any("PADDING" in f for f in identical_result.findings), (
        "10 unresolved entities with one identical evidence detail were not flagged as "
        "padding. The C11 fix must check unresolved entities by detail, not exempt them."
    )
    assert identical_result.score < distinct_result.score, (
        f"Duplicated unresolved entities scored {identical_result.score}/30 and distinct "
        f"ones scored {distinct_result.score}/30. Duplication must cost."
    )


def test_c11_classic_same_field_padding_is_still_detected():
    """C11 CONTROL: the attack the detector was written for must still be caught.

    `status_1 .. status_10`, all claiming `Case.Status`. A resolved field name is a
    claim about the org, so N entities making the same claim is padding regardless of
    how their details are worded.
    """
    padded = _fluent_spec(
        entities=[
            DerivedEntity(
                name=f"status_{index}",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[
                    SpecEvidence("data-delta", f"Case.Status changed 'New' -> 'Working' at step-{index:03d}")
                ],
            )
            for index in range(10)
        ]
    )

    result = score_spec(padded, provenance=_REAL_PROVENANCE)

    assert any("PADDING" in f for f in result.dimensions["evidence_grounding"].findings), (
        "10 entities all targeting Case.Status were not detected as padding."
    )
    assert not result.passed, (
        f"Classic same-field padding PASSED at {result.total}/100."
    )


def test_c11_on_lane_02_real_capture_when_available():
    """C11 against the real artifact, once lane 02's capture merges.

    Lane 02 owns `examples/case_creation_aft3.dom_capture.jsonl`, so this lane does
    not copy it in (LANE_RULES 6). The test skips until that fixture lands on the
    integration branch and then asserts the property on real data rather than on the
    synthetic reconstruction above.

    Note the capture is currently REJECTED by `run_pipeline` — 171 of 175 events fail
    `RawDomEvent` validation because `selectors.role_name.role` is null for LWC
    elements with no implicit ARIA role (lane 02's finding, lane 04 owns the fix). A
    rejected capture yields no spec, which is the correct fail-closed behaviour and is
    asserted here too: the gate must never score the 4 surviving events.
    """
    capture = EXAMPLE_CAPTURE.parent / "case_creation_aft3.dom_capture.jsonl"
    if not capture.is_file():
        pytest.skip("lane 02's real capture has not merged yet")

    # CaptureRejected is raised by (and defined in) pipeline, not dom_capture.
    from sf_video_blueprint.pipeline import CaptureRejected

    try:
        spec = run_pipeline(capture, org_url=ORG_URL).spec
    except CaptureRejected:
        pytest.skip("capture is still rejected by ingest; lane 04 owns that fix")

    result = score_spec(spec, provenance=_REAL_PROVENANCE)
    accusations = [f for f in result.dimensions["evidence_grounding"].findings if "PADDING" in f]
    assert not accusations, (
        f"The gate accused the builder of padding on a REAL capture: {accusations}"
    )
