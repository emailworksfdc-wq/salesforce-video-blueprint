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

from sf_video_blueprint.markers import REAL_EXTRACTION_SOURCES, REAL_TELEMETRY_SOURCES
from sf_video_blueprint.spec_builder import (
    DerivedAgentSpec,
    DerivedEntity,
    SpecEvidence,
)
from sf_video_blueprint.spec_score import (
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
