#!/usr/bin/env python3
"""Score an agent spec deterministically, without an org.

This module drives the refinement loop: it scores a spec derived from a recording
so the loop can iterate cheaply and reproducibly. The score MUST be falsifiable —
a beautifully structured spec built from fabricated data is worse than an honest
incomplete one, because it invites trust.

Design constraints:

* **Fully deterministic.** No clocks, no randomness, no network, no org, no LLM.
  Same input -> same score, always. The loop's stopping condition depends on this.
* **The scorer must be able to FAIL.** A gate that always returns 100/100 trains
  the loop to fabricate data. Prove yours can fail.
* **Never mutate the input spec.**
* **Provenance integrity is load-bearing.** A spec from stub/mock data is capped
  hard, no matter how well-formed.

Scoring dimensions (weights sum to 100):

1. evidence_grounding (30) — every entity/guardrail traceable to a SpecEvidence
   entry. Entities whose only evidence is "inference" score LOWER than those from
   "data-delta" or "ui-action". This is the MOST IMPORTANT dimension: it measures
   whether the spec describes what was observed or what was assumed.
2. completeness (15) — objects_touched non-empty, entities non-empty,
   orchestration_steps non-trivial, guardrails present, failure_handling present.
3. honesty (20) — unknowns are DECLARED rather than hidden. SUBTLETY: a spec with
   declared unknowns is BETTER than one that silently omits them, so do not simply
   penalise len(unknowns). Penalise heavily when a spec claims high confidence AND
   has structural gaps (that combination is dishonest); reward explicit unknowns at
   low confidence. Getting this backwards trains the loop to hide gaps, which is
   the worst possible outcome.
4. specificity (10) — intent is a concrete verb+object, not generic; no UNRESOLVED;
   topic/role text names real objects and fields.
5. testability (10) — required entities are explicit enough to write test utterances
   against; failure paths observed (not merely asserted).
6. placeholder_freedom (10) — scan for the same markers as scripts/score_run.py.
   Drift risk: this list is duplicated; recommend centralising it.
7. provenance_integrity (5) — if extraction_source is "stub" or telemetry_source
   is "mock", the spec CANNOT reach the top band. Cap it hard and list the reason
   as a blocking issue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from .markers import (
    PLACEHOLDER_MARKERS,
    STUB_FINGERPRINTS,
    REAL_EXTRACTION_SOURCES,
    REAL_TELEMETRY_SOURCES,
    extraction_is_real,
    telemetry_is_real,
    scan_spec,
    scan_text,
)
from .spec_builder import DerivedAgentSpec

# Weights sum to 100. Rationale in module docstring.
DIMENSION_WEIGHTS = {
    "evidence_grounding": 30,
    "completeness": 15,
    "honesty": 20,
    "specificity": 10,
    "testability": 10,
    "placeholder_freedom": 10,
    "provenance_integrity": 5,
}

PASS_THRESHOLD = 75

# Ceiling on the total *displayed* for a spec that has any blocking issue. Set one
# below the moderate band (60) so a blocked spec can never render a number that
# reads as partial success. This caps presentation only — `SpecScore.total` keeps
# the raw sum, because iterate.py needs a real gradient across blocked versions.
MAX_BLOCKED_DISPLAY_TOTAL = 59

# The deriver's own confidence ceiling: `_derive_intent` returns at most 0.7, for a
# single object with observed field changes. A spec claiming more than this asserts
# certainty the builder cannot produce, so honesty treats it as an overclaim rather
# than as information. Kept here rather than imported to avoid a circular import;
# `test_score_calibration.py` pins the two together.
BUILDER_MAX_CONFIDENCE = 0.7

# Dimensions permitted to score 0 on an honest run, and therefore excluded from the
# hollow-dimension blocker:
#   * testability — a recording of a process that simply succeeded has no observed
#     failure path, which is a fact about the recording, not a defect in the spec.
#   * provenance_integrity — scores 0 by design for in-memory scoring
#     (provenance=None), which is the iterate.py path.
# Blocking on either would make the gate unclearable by honest output, which is
# worse than the hole it would close.
ZERO_TOLERATED_DIMENSIONS = frozenset({"testability", "provenance_integrity"})

# Minimum characters an entity evidence detail must carry to count as a real
# observation. The builder's shortest emitted detail on the example capture is 41
# characters, so this is far below honest output. It is a floor against the trivial
# evasion (detail="ab" previously scored full marks), NOT a defence against a
# fabricator willing to write plausible prose — no length test can be that.
MIN_EVIDENCE_DETAIL_CHARS = 12

# A dimension scoring at or below this fraction of its weight is treated as absent
# rather than weak. Set to a tenth: on the 10-point dimensions that is 1 point, which
# is where the subtractive vacuous-content penalties in `_score_specificity` bottom
# out (all-"Step N" steps score 1/10, not 0/10, and would evade a `== 0` check by a
# single point).
HOLLOW_DIMENSION_FRACTION = 0.1

# C8: the minimum an instruction string must carry to count as an instruction at all.
#
# Every specificity check above this line is a blocklist — it looks for known-bad
# phrases ("generic", "placeholder", "Step 1"). A spec whose steps were "aa", "bb",
# "ee" and whose guardrails were "cc", "ff" matched none of them and scored a
# perfect 100/100, outranking the real derived spec at 84. Blocklists cannot catch
# content that says nothing, because saying nothing has no fingerprint.
#
# Both thresholds are set far below honest output, measured rather than guessed. On
# the example capture the builder's shortest orchestration step or guardrail is 52
# characters and 7 words ("submit on button:Save -> writes Status (backend: flow)");
# its shortest is still 4x this floor. The gap exists so the floor never fires on
# real output — a check that penalises the honest path is worse than no check.
#
# This is deliberately a FLOOR, not a defence. Two words of plausible prose defeat
# it, and nothing here can measure whether a sentence is *true*. It closes the
# trivial evasion — one where the attacker does not even try — and it is the
# builder, which cannot write a step it did not observe, that does the real work.
MIN_INSTRUCTION_CHARS = 12
MIN_INSTRUCTION_WORDS = 3

# The fraction of a spec's instruction strings that may be filler before the spec is
# blocked rather than merely docked. Set above zero so a single terse-but-real line
# is a deduction, not a refusal; set below a half so a spec whose narrative is
# mostly empty cannot pass on the strength of its metadata.
MAX_FILLER_INSTRUCTION_FRACTION = 0.4


def score_provenance(provenance: dict[str, str] | None) -> tuple[DimensionScore, list[str]]:
    """Score provenance integrity dimension.

    Returns:
        A tuple of (DimensionScore, blocking_issues list).

    Semantics (fail-closed):
    - provenance supplied and both axes real -> 5/5, no blocker
    - provenance supplied and either axis not real -> 0/5 + blocking issue
    - provenance is None -> 0/5 with explanatory finding, NO blocker
    """
    max_score = DIMENSION_WEIGHTS["provenance_integrity"]
    findings: list[str] = []
    evidence_strs: list[str] = []
    blocking: list[str] = []

    if provenance is None:
        # Caller did not supply provenance. Score 0 but do not block.
        return (
            DimensionScore(
                name="provenance_integrity",
                score=0,
                max_score=max_score,
                weight=max_score,
                findings=["Provenance not supplied (in-memory scoring). "
                         "Call score_spec_file() or pass provenance= for full check."],
                evidence=[],
            ),
            [],
        )

    extraction_source = provenance.get("extraction_source")
    telemetry_source = provenance.get("telemetry_source")

    evidence_strs.append(f"extraction_source={extraction_source}")
    evidence_strs.append(f"telemetry_source={telemetry_source}")

    # Use structural checks from markers.py
    extraction_real = extraction_is_real(extraction_source)
    telemetry_real = telemetry_is_real(telemetry_source)

    if extraction_real and telemetry_real:
        # Both real -> full score
        score = max_score
    else:
        # Either not real -> 0 and blocking
        score = 0
        if not extraction_real:
            findings.append(f"extraction_source '{extraction_source}' is not a known-real source")
            blocking.append(
                "Spec was built from stub/unknown extraction data, not a real recording. "
                "A beautifully-structured spec derived from fabricated data is worse than an ugly honest one."
            )
        if not telemetry_real:
            findings.append(f"telemetry_source '{telemetry_source}' is not a known-real source")
            blocking.append(
                "Spec was built from mock/unknown telemetry, not a live org. "
                "Cannot reach the top band without observed server-side behaviour."
            )

    return (
        DimensionScore(
            name="provenance_integrity",
            score=score,
            max_score=max_score,
            weight=max_score,
            findings=findings,
            evidence=evidence_strs,
        ),
        blocking,
    )


@dataclass(slots=True)
class DimensionScore:
    """Score for one dimension of a spec."""

    name: str
    score: int
    max_score: int
    weight: int
    findings: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SpecScore:
    """The result of scoring a spec.

    Two totals, deliberately:

    ``total`` is the raw dimension sum. ``iterate.py`` compares totals across
    versions to detect improvement and convergence, so this must stay a real
    gradient even for a spec that is blocked — otherwise every blocked version
    collapses to the same number and the refinement loop has nothing to climb.

    ``display_total`` is what a human should be shown. A blocked spec scoring 79
    reads as "nearly there" when the correct reading is "not evidence-backed at
    all", so ``display_total`` is capped below the moderate band whenever a blocker
    is present. The gradient survives; the misread does not.
    """

    total: int
    max_total: int
    band: str  # low | moderate | high
    passed: bool
    dimensions: dict[str, DimensionScore]
    blocking_issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def display_total(self) -> int:
        """The total as it should be reported to a human.

        Capped to ``MAX_BLOCKED_DISPLAY_TOTAL`` when any blocking issue is present.
        Blocking means the spec is not evidence-backed, and a number in the
        moderate or high range contradicts that in the one place a reader looks
        first.
        """
        if self.blocking_issues:
            return min(self.total, MAX_BLOCKED_DISPLAY_TOTAL)
        return self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "display_total": self.display_total,
            "max_total": self.max_total,
            "band": self.band,
            "passed": self.passed,
            "dimensions": {
                name: {
                    "name": dim.name,
                    "score": dim.score,
                    "max_score": dim.max_score,
                    "weight": dim.weight,
                    "findings": dim.findings,
                    "evidence": dim.evidence,
                }
                for name, dim in self.dimensions.items()
            },
            "blocking_issues": self.blocking_issues,
            "recommendations": self.recommendations,
        }

    def summary(self) -> str:
        """Human-readable one-liner.

        Reports ``display_total``, not ``total``, and says so when the two differ.
        A reader who sees only this line must not come away with a better
        impression of the spec than the blocking issues justify.
        """
        passed_str = "PASS" if self.passed else "FAIL"
        head = (
            f"{passed_str}: {self.display_total}/{self.max_total} ({self.band} band), "
            f"{len(self.blocking_issues)} blocking issue(s)"
        )
        if self.display_total != self.total:
            head += f" [blocked: capped from raw {self.total}]"
        return head


@dataclass(slots=True)
class SpecComparison:
    """Comparison of two spec scores (for convergence detection)."""

    delta: int
    improved: bool
    regressions: list[str] = field(default_factory=list)

    def converged(self, epsilon: int = 3) -> bool:
        """Whether the scores are within epsilon points (refinement can stop)."""
        return abs(self.delta) <= epsilon


def score_spec(
    spec: DerivedAgentSpec,
    *,
    yaml_text: str | None = None,
    agent_script_text: str | None = None,
    provenance: dict[str, str] | None = None,
) -> SpecScore:
    """Score an agent spec deterministically, without an org.

    Args:
        spec: The derived spec to score.
        yaml_text: Optional YAML text to scan for placeholders.
        agent_script_text: Optional .agent script text to scan for placeholders.
        provenance: Optional provenance dict with extraction_source and telemetry_source.
                   If None, provenance_integrity scores 0 with an explanatory finding but no blocker.
                   If provided, both sources must be real or score is 0 with blocking issue.

    Returns:
        A SpecScore with total, band, pass/fail, and actionable recommendations.
    """
    dimensions: dict[str, DimensionScore] = {}
    blocking: list[str] = []
    recommendations: list[str] = []

    # 1. Evidence grounding (30 points)
    evidence_dim = _score_evidence_grounding(spec)
    dimensions["evidence_grounding"] = evidence_dim
    # PADDING BLOCKER: If padding is detected in entities, block the spec
    if any("PADDING DETECTED" in f for f in evidence_dim.findings):
        blocking.append("Entity padding detected: multiple entities target the same field, likely a gaming attack.")
    if evidence_dim.score < evidence_dim.max_score * 0.5:
        recommendations.append(
            "Re-record with --track-record ObjectApiName:RecordId to capture field deltas, "
            "which anchor entities to observed data rather than inference."
        )

    # 2. Completeness (15 points)
    completeness_dim = _score_completeness(spec)
    dimensions["completeness"] = completeness_dim
    # PADDING BLOCKER: If padding is detected in steps or guardrails, block the spec
    if any("PADDING DETECTED" in f for f in completeness_dim.findings):
        blocking.append("Padding detected in orchestration steps or guardrails: inflated counts with trivial variants, likely a gaming attack.")
    if not spec.objects_touched:
        blocking.append("No Salesforce object observed (no data delta captured).")
        recommendations.append(
            "Re-record with --track-record ObjectApiName:RecordId so the spec can name concrete targets."
        )
    if not spec.entities:
        blocking.append("Spec derived no input entities.")

    # D13 FIX: Absent guardrails is a structural defect and must block.
    # The builder (_derive_guardrails) ALWAYS emits at least one guardrail for any
    # real recording, so an empty guardrail list means the spec did not come from
    # the real deriver, or someone stripped them. Make this blocking.
    if not spec.guardrails:
        blocking.append(
            "No guardrails present. The spec builder always emits at least one guardrail "
            "(FLS enforcement, write confirmation, scope limits) for any real recording. "
            "An absent guardrail list is a structural defect."
        )

    # 3. Honesty (20 points)
    honesty_dim = _score_honesty(spec)
    dimensions["honesty"] = honesty_dim

    # 4. Specificity (10 points)
    specificity_dim = _score_specificity(spec)
    dimensions["specificity"] = specificity_dim
    # DEFECT 4 FIX: Guard against None/non-string intent
    if isinstance(spec.intent, str) and spec.intent.startswith("UNRESOLVED:"):
        blocking.append("Spec intent is UNRESOLVED.")
        recommendations.append(
            "Recording did not demonstrate a completed business action; record a full end-to-end flow."
        )

    # 5. Testability (10 points)
    testability_dim = _score_testability(spec)
    dimensions["testability"] = testability_dim
    if not _has_observed_failure(spec.failure_handling):
        recommendations.append(
            "Record a failing variant (validation error, auth failure, etc.) to exercise error paths, "
            "which makes the spec testable under failure."
        )

    # 6. Placeholder freedom (10 points)
    placeholder_dim = _score_placeholder_freedom(spec, yaml_text, agent_script_text)
    dimensions["placeholder_freedom"] = placeholder_dim
    if placeholder_dim.findings:
        blocking.append(f"Placeholder/stub content detected: {', '.join(placeholder_dim.findings[:3])}")

    # 7. Provenance integrity (5 points)
    provenance_dim, provenance_blocking = score_provenance(provenance)
    dimensions["provenance_integrity"] = provenance_dim
    blocking.extend(provenance_blocking)

    # C1: the top-level evidence trail must exist.
    #
    # `spec.evidence` is the only field describing the RUN rather than the
    # conclusions drawn from it: which telemetry layers fired, how many actions were
    # extracted, which objects were mutated. Every dimension above scores the spec's
    # prose; this is the one field that scores its provenance, and it was read by
    # nothing. A fabricated spec could therefore ship with no audit trail at all and
    # score 95/100, which inverts the cost of honesty — the trail is the cheapest
    # thing to keep when the recording is real and the hardest to forge when it is not.
    #
    # Safe to make blocking: build_agent_spec unconditionally appends
    # "N action(s) in recording", so no honest spec has an empty trail.
    trail = spec.evidence if isinstance(spec.evidence, list) else []
    if not trail:
        blocking.append(
            "No top-level evidence trail. The spec builder always records what the run "
            "observed (telemetry layers, action count, mutated objects); an empty trail "
            "means the spec does not describe an observed run."
        )

    total = sum(dim.score for dim in dimensions.values())
    max_total = sum(DIMENSION_WEIGHTS.values())

    # THRESHOLD SURFING DEFENSE (Attack 5): Count how many dimensions scored <=50% of their max.
    # If >=2 dimensions are <=50%, this looks like deliberate dimension sacrifice to game the total.
    # Exclude provenance_integrity from this count (it's expected to be 0 in-memory).
    dimensions_below_half = [
        name for name, dim in dimensions.items()
        if name != "provenance_integrity" and dim.score <= dim.max_score * 0.5
    ]
    if len(dimensions_below_half) >= 2:
        blocking.append(
            f"Threshold surfing detected: {len(dimensions_below_half)} dimensions scored <=50% "
            f"({', '.join(dimensions_below_half)}). This pattern suggests gaming the scorer "
            "by maximizing some dimensions while sacrificing others."
        )

    # C7: the CONCENTRATED form of threshold surfing.
    #
    # The check above needs >=2 weak dimensions, so it catches the diffuse attack
    # (shave several dimensions a little) and misses the concentrated one (hollow out
    # a single dimension completely). The concentrated form is strictly cheaper to
    # execute and was measurably survivable:
    #
    #   orchestration_steps=["Step 1","Step 2","Step 3"], guardrails=["Guardrail 1","Rule 2"]
    #       -> specificity 0/10, total 82/100, passed=True
    #   orchestration_steps=["Step 1","Step 2"], guardrails=["Guardrail 1"]
    #       -> specificity 1/10, total exactly 75/100, passed=True
    #
    # The second is why this tests a FRACTION rather than `== 0`: the vacuous-content
    # penalties in `_score_specificity` are subtractive, so a spec whose every step is
    # the literal word "Step" lands on 1/10 rather than 0/10 and would slip a
    # zero-only check by a single point. Anything at or below a tenth of a dimension's
    # weight is an absent signal, not a weak one — the spec has no measurable content
    # on an axis the gate claims to measure, and averaging that into a number labelled
    # "75/100" misrepresents it.
    #
    # ZERO_TOLERATED_DIMENSIONS documents the two dimensions that may honestly be 0
    # and are therefore exempt.
    hollow = [
        name
        for name, dim in dimensions.items()
        if name not in ZERO_TOLERATED_DIMENSIONS
        and dim.max_score > 0
        and dim.score <= dim.max_score * HOLLOW_DIMENSION_FRACTION
    ]
    if hollow:
        detail = ", ".join(f"{name}={dimensions[name].score}/{dimensions[name].max_score}" for name in hollow)
        blocking.append(
            f"Hollow dimension(s): {detail}. A dimension at or near zero means the spec has "
            "no measurable content on an axis the gate claims to measure, so the total does "
            "not describe it. Sacrificing one dimension outright must not be cheaper than "
            "earning it."
        )

    # C8 blocker: a spec whose instructions are mostly filler is not an agent spec.
    #
    # MEASURED before this check: steps ["aa", "bb", "ee"] and guardrails ["cc", "ff"],
    # with concrete well-evidenced entities around them, scored 100/100 passed=True —
    # all seven dimensions at full marks, outranking the real derived spec at 84.
    #
    # The specificity deduction alone is not enough to stop it. specificity is only 10
    # of 100 points, so an attacker who earns the other 90 still clears a threshold of
    # 75 with the dimension zeroed. That gap is the direction a refinement loop
    # optimises: entity metadata is expensive to fabricate, prose is free, so the
    # cheapest route to a high score is real-looking metadata with the narrative
    # hollowed out. The instructions ARE the deliverable — a spec that cannot say what
    # the agent should do fails as a spec no matter how well-evidenced its fields are.
    #
    # Blocked rather than docked for the same reason mock telemetry is blocked: it is a
    # statement about what the artifact is, not a measure of how good it is.
    instructions = _instruction_strings(spec)
    filler = [text for text in instructions if _is_filler_instruction(text)]
    if instructions and len(filler) > len(instructions) * MAX_FILLER_INSTRUCTION_FRACTION:
        shown = ", ".join(repr(text)[:20] for text in filler[:4])
        blocking.append(
            f"{len(filler)} of {len(instructions)} instruction strings carry no instruction "
            f"(under {MIN_INSTRUCTION_CHARS} chars or {MIN_INSTRUCTION_WORDS} words): {shown}. "
            "orchestration_steps and guardrails are the spec's actual deliverable — the text "
            "a human reads to decide whether to trust the agent. Well-evidenced entity "
            "metadata cannot substitute for instructions that say nothing."
        )

    # C10 blocker: the instructions must reference the evidence the spec claims.
    #
    # MEASURED with the C8 floor in place: steps ["do the thing here", "then do it
    # again"] with guardrail ["always be careful now"] scored 92/100 passed=True —
    # HIGHER than the real derived spec's 90. Padding with the object name
    # (["Case aa bb cc dd", ...]) scored the same 92.
    #
    # C8 measures an instruction's SHAPE (12 chars, 3 words), and shape is trivially
    # cheap: an attacker who reads the constant writes four words of nothing. Every
    # length floor has that weakness, which C8's own comment concedes. This asks the
    # question shape cannot: does the narrative describe the same run as the metadata?
    #
    # A spec that declares it observed `Case.Priority` change, and whose every
    # instruction never names `Priority`, is two artifacts describing different things
    # — at most one of which came from a recording. Cheap for the builder to satisfy
    # (it derives steps FROM the deltas, so `_derive_orchestration` writes
    # "-> writes Status" verbatim), expensive for a fabricator, who must now keep two
    # artifacts consistent instead of one.
    #
    # Deliberately conditional on the spec DECLARING a field API name: a UI-only
    # recording resolves no field, has nothing for a step to name, and must stay able
    # to pass. And the obvious evasion — delete the field-bearing entity so the rule
    # never applies — costs evidence_grounding and testability, so it is strictly worse
    # than writing a real step. `test_c10_coupling_rule_cannot_be_evaded_by_deleting_
    # the_entity` pins that ordering.
# The token set is field API names AND entity names, because a UI-only recording
    # resolves no field at all and would otherwise be exempt. MEASURED: requiring only
    # field names left the evasion open — deleting the field-bearing entity and keeping
    # the filler instructions scored 83/100 passed=True. Entity names close it, because
    # the builder derives the entity name from the same observed target it writes into
    # the step ("subject" <- "input on 'input:Subject'").
    #
    # Two exclusions, both to avoid firing on honest output:
    #   * field "Id" and the entity named "recordId" — the builder's mandated
    #     record-identifier, which is an inference about how to act, not an observation
    #     about the process. A spec whose ONLY entity is that one has an empty token set
    #     and is exempt; `_score_evidence_grounding` already scores it a quarter of the
    #     dimension for exactly that reason.
    #   * tokens under 4 characters, which would match ordinary prose by accident and
    #     make the check free to pass.
    observed_tokens: set[str] = set()
    for entity in spec.entities if isinstance(spec.entities, list) else []:
        field_name = getattr(entity, "field_api_name", None)
        if field_name and field_name != "Id":
            observed_tokens.add(field_name)
        entity_name = getattr(entity, "name", None)
        if entity_name and entity_name != "recordId":
            observed_tokens.add(entity_name)
    observed_tokens = {token for token in observed_tokens if len(token) >= 4}

    if observed_tokens:
        narrative = " ".join(text for text in instructions if isinstance(text, str)).lower()
        named = sorted(token for token in observed_tokens if token.lower() in narrative)
        if not named:
            shown = ", ".join(sorted(observed_tokens)[:6])
            blocking.append(
                f"No orchestration step or guardrail names anything the spec claims to have "
                f"observed ({shown}). The entity metadata and the instructions describe "
                "different runs, so at most one of them came from a recording. The builder "
                "writes the observed field or target into the step it derived from it."
            )

    # Band calculation: blocking issues force "low" band regardless of numeric score
    if blocking:
        band = "low"
    elif total >= PASS_THRESHOLD:
        band = "high"
    elif total >= 60:
        band = "moderate"
    else:
        band = "low"

    passed = total >= PASS_THRESHOLD and not blocking

    return SpecScore(
        total=total,
        max_total=max_total,
        band=band,
        passed=passed,
        dimensions=dimensions,
        blocking_issues=blocking,
        recommendations=recommendations,
    )


def score_spec_file(path: Path | str) -> SpecScore:
    """Load and score an agent-spec JSON written by spec_builder.write_spec.

    This reads the on-disk format, which includes the provenance key injected by
    write_spec(). Provenance is checked for stub/mock sources, which are blocking.

    Args:
        path: Path to the agent-spec JSON file. Accepts both Path and str.

    Returns:
        A SpecScore with provenance integrity checked.
    """
    path = Path(path)
    if not path.exists():
        # Return a failing score for missing file.
        return SpecScore(
            total=0,
            max_total=sum(DIMENSION_WEIGHTS.values()),
            band="low",
            passed=False,
            dimensions={},
            blocking_issues=[f"Spec file not found: {path}"],
            recommendations=["Ensure spec_builder.write_spec() ran successfully."],
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return SpecScore(
            total=0,
            max_total=sum(DIMENSION_WEIGHTS.values()),
            band="low",
            passed=False,
            dimensions={},
            blocking_issues=[f"Spec file unreadable: {exc}"],
            recommendations=["Check the JSON is valid and the file is not corrupted."],
        )

    # Reconstruct the DerivedAgentSpec from the on-disk format.
    from .spec_builder import DerivedEntity, SpecEvidence

    entities = [
        DerivedEntity(
            name=ent["name"],
            object_api_name=ent["object_api_name"],
            field_api_name=ent["field_api_name"],
            evidence=[SpecEvidence(source=e["source"], detail=e["detail"]) for e in ent.get("evidence", [])],
        )
        for ent in data.get("entities", [])
    ]
    evidence = [SpecEvidence(source=e["source"], detail=e["detail"]) for e in data.get("evidence", [])]

    spec = DerivedAgentSpec(
        intent=data.get("intent", ""),
        confidence=float(data.get("confidence", 0.0)),
        objects_touched=data.get("objects_touched", []),
        entities=entities,
        orchestration_steps=data.get("orchestration_steps", []),
        guardrails=data.get("guardrails", []),
        failure_handling=data.get("failure_handling", []),
        unknowns=data.get("unknowns", []),
        evidence=evidence,
    )

    # Score the spec, passing provenance through to score_spec.
    provenance = data.get("provenance", {})
    result = score_spec(spec, provenance=provenance)

    return result


def compare(a: SpecScore, b: SpecScore) -> SpecComparison:
    """Compare two spec scores to detect improvement and convergence.

    Args:
        a: The earlier score.
        b: The later score.

    Returns:
        A SpecComparison with delta, improved flag, and any regressions.
    """
    delta = b.total - a.total
    improved = delta > 0
    regressions: list[str] = []

    for name, dim_b in b.dimensions.items():
        dim_a = a.dimensions.get(name)
        if dim_a and dim_b.score < dim_a.score:
            regressions.append(f"{name}: {dim_a.score} -> {dim_b.score}")

    return SpecComparison(delta=delta, improved=improved, regressions=regressions)


def _has_observed_failure(failure_handling: list[str]) -> bool:
    """Check if failure_handling contains genuinely observed failures.

    The builder emits observed failures as:
        "Observed <layer> failure during recording: <reason>"

    The negative sentinel (no failures observed) is:
        "No failures were observed in this run, so error paths are UNTESTED."

    This helper matches the stable emitted fragment, not incidental words.

    DEFECT 4 FIX: Fails closed - if failure_handling is not a list or contains
    non-strings, returns False rather than crashing.
    """
    if not isinstance(failure_handling, list):
        return False
    for item in failure_handling:
        # Match the positive pattern: "Observed <layer> failure during recording:"
        if isinstance(item, str) and "Observed " in item and " failure during recording:" in item:
            return True
    return False


def _score_evidence_grounding(spec: DerivedAgentSpec) -> DimensionScore:
    """Score how well entities and guardrails are grounded in observed evidence.

    Entities from "data-delta" or "ui-action" score higher than "inference".
    This is the single most important dimension.

    CRITICAL FIX (D10/G1/G2): The weighted-average formula was non-monotone —
    deleting a below-average entity raised the score. NEW FORMULA satisfies G1:
    We track the BEST evidence quality seen (strongest signal) + give partial
    credit for coverage. This makes the score monotonically non-decreasing in
    observed evidence: adding a data-delta/ui-action entity never lowers it,
    and removing one never raises it.

    Grounding quality = floor_from_best_evidence + coverage_bonus.
    - floor_from_best_evidence: If ANY data-delta exists -> 50% floor.
                                 If ANY ui-action (no data-delta) -> 35% floor.
                                 All inference -> 0% floor.
    - coverage_bonus: Up to 50% additional for having MANY well-grounded entities.
                      Bonus = (well_grounded / total) * 50%, capped at 50%.

    This satisfies G1 (deletion never pays) and G2 (mandated recordId is excluded
    from scoring, so it doesn't lower the score). A spec with ALL weak evidence
    still scores lower than one with strong evidence.

    PADDING ATTACK FIX: Detect when many entities target the same field_api_name
    (e.g., 10 entities all claiming Case.Status) and penalize as obvious padding.
    """
    max_score = DIMENSION_WEIGHTS["evidence_grounding"]
    findings: list[str] = []
    evidence_strs: list[str] = []

    if not spec.entities:
        return DimensionScore(
            name="evidence_grounding",
            score=0,
            max_score=max_score,
            weight=max_score,
            findings=["No entities derived (no data to ground)."],
            evidence=[],
        )

    # Separate the builder-mandated recordId from genuinely-observed entities.
    # DEFECT 4 FIX: Defensive check - ensure entities is actually a list
    mandated_record_ids = []
    observed_entities = []

    entities = spec.entities if isinstance(spec.entities, list) else []
    for entity in entities:
        # DEFECT 4 FIX: Skip non-entity objects
        if not hasattr(entity, 'evidence') or not hasattr(entity, 'field_api_name'):
            continue
        sources = [e.source for e in entity.evidence] if isinstance(entity.evidence, list) else []
        # Mandated recordId: inference-only, field_api_name == "Id"
        if entity.field_api_name == "Id" and sources == ["inference"]:
            mandated_record_ids.append(entity)
        else:
            observed_entities.append(entity)

    # Score based ONLY on genuinely-observed entities, not the mandated recordId.
    total_entities = len(observed_entities)

    if total_entities == 0:
        # Only mandated recordId exists -> score based on whether we have any real evidence at all
        # This is low but not zero (we do know the object)
        score = max_score // 4
        findings.append("Only the mandated recordId entity exists; no field-level data observed.")
        evidence_strs.append(f"{len(mandated_record_ids)} mandated recordId(s), 0 observed entities")
        return DimensionScore(
            name="evidence_grounding",
            score=score,
            max_score=max_score,
            weight=max_score,
            findings=findings,
            evidence=evidence_strs,
        )

    # PADDING DETECTION: Count entities per field_api_name. If >3 entities target the same field,
    # it's likely padding (e.g., status_1, status_2, ..., status_10 all targeting Case.Status).
    # DEFECT 4 FIX: Defensive checks for missing attributes
    #
    # C11 FIX: skip entities whose object AND field are both unresolved.
    #
    # The key was `f"{object_api_name}.{field_api_name}"`, which for an unresolved
    # entity is the literal string "None.None". Every unresolved entity therefore
    # collided in one bucket and any four of them tripped the detector.
    #
    # MEASURED on lane 02's real AFT3 capture (examples/case_creation_aft3): the
    # builder emits 130 entities, of which 128 are UI inputs that could not be
    # resolved to an object/field. All 128 have DISTINCT names and DISTINCT evidence
    # details — they are 128 separate observed keystrokes — and the gate reported
    # "PADDING DETECTED ... None.None (128x) ... likely an attack to inflate entity
    # counts artificially", cut evidence_grounding to 20% of base (5/30), and raised a
    # blocking issue. On the project's only real capture, the gate accused its own
    # builder of a gaming attack, and the accusation is the reason the run is blocked.
    #
    # The detector's premise is still sound where it applies: N entities all claiming
    # the SAME resolved field is padding, because a resolved field name is a claim
    # about the org. "Unresolved" is not a claim about anything, so unresolved entities
    # cannot be duplicates of each other by that key. They are handled by the
    # `distinct_targets` check below, which compares what they actually observed.
    field_counts: dict[str, int] = {}
    for entity in observed_entities:
        if not hasattr(entity, 'object_api_name') or not hasattr(entity, 'field_api_name'):
            continue
        if not entity.object_api_name and not entity.field_api_name:
            continue
        key = f"{entity.object_api_name}.{entity.field_api_name}"
        field_counts[key] = field_counts.get(key, 0) + 1

    # C11: unresolved entities still have to be checked for real duplication, or the
    # skip above becomes the padding vector itself — an attacker could emit 200
    # identical unresolved entities. Their evidence details are what they claim to have
    # observed, so genuine duplication shows up as repeated details. The real capture
    # has 128 distinct details out of 128, and the honest builder cannot produce two
    # entities with the same detail (the detail embeds the step id).
    unresolved = [
        entity
        for entity in observed_entities
        if hasattr(entity, 'object_api_name')
        and hasattr(entity, 'field_api_name')
        and not entity.object_api_name
        and not entity.field_api_name
    ]
    if len(unresolved) > 3:
        details = [
            ev.detail
            for entity in unresolved
            for ev in (entity.evidence if isinstance(entity.evidence, list) else [])
            if isinstance(ev.detail, str)
        ]
        distinct_details = set(details)
        if details and len(distinct_details) * 3 < len(details):
            field_counts[f"<unresolved> ({len(distinct_details)} distinct of {len(details)})"] = (
                len(details)
            )

    padding_detected = any(count > 3 for count in field_counts.values())
    if padding_detected:
        padded_fields = [f"{field} ({count}x)" for field, count in field_counts.items() if count > 3]
        findings.append(
            f"PADDING DETECTED: Multiple entities target the same field: {', '.join(padded_fields)}. "
            "This is likely an attack to inflate entity counts artificially."
        )

    # Count observed entities by evidence source.
    # Also check for minimal/placeholder evidence details.
    data_delta_count = 0
    ui_action_count = 0
    inference_count = 0
    minimal_evidence_count = 0

    for entity in observed_entities:
        sources = [e.source for e in entity.evidence]
        # C9: an entity whose evidence detail is a stub does not count as grounded.
        #
        # See the block below the loop for the measured defect this feeds. The set of
        # sources is filtered rather than the score being scaled afterwards, so a stub
        # entity contributes nothing instead of taxing the entities around it.
        substantive_sources = [
            e.source
            for e in entity.evidence
            if isinstance(e.detail, str) and len(e.detail.strip()) >= MIN_EVIDENCE_DETAIL_CHARS
        ]
        # C6: raise the minimal-evidence floor from 1 character.
        #
        # The old bound was `len(detail.strip()) <= 1`, so detail="x" was caught and
        # detail="ab" scored full marks. The builder's own shortest emitted detail on
        # the example capture is 41 characters
        # ("input on 'input:Subject' at step step-003"), so the gap between what
        # honest output looks like and what the check tolerated was 39 characters wide.
        #
        # This is a FLOOR, not a defence: an attacker who writes 40 characters of
        # plausible prose defeats any length test, and no length test can distinguish
        # observed prose from invented prose. The real defence is in the builder,
        # which cannot emit a detail for a delta it did not see. MIN_EVIDENCE_DETAIL
        # only removes the trivially-cheap version of the evasion.
        for ev in entity.evidence:
            if not isinstance(ev.detail, str) or len(ev.detail.strip()) < MIN_EVIDENCE_DETAIL_CHARS:
                minimal_evidence_count += 1
                findings.append(f"Entity {entity.name} has minimal evidence detail: {ev.detail!r}")
                break

        # C9: count against substantive_sources, not sources. An entity whose only
        # evidence detail is "ab" is not a well-grounded entity, and it must not be
        # counted as one and then compensated for by a multiplicative penalty.
        if "data-delta" in substantive_sources:
            data_delta_count += 1
        elif "ui-action" in substantive_sources:
            ui_action_count += 1
        elif "inference" in substantive_sources:
            inference_count += 1

    # NEW MONOTONE FORMULA (D10 fix):
    # Score = floor_from_best_evidence + coverage_bonus.
    #
    # Floor is determined by the BEST evidence present (strongest signal):
    #   - ANY data-delta -> 50% floor (15 points)
    #   - ANY ui-action (but no data-delta) -> 35% floor (10.5 points)
    #   - All inference -> 0% floor
    #
    # Coverage bonus rewards having MANY well-grounded entities:
    #   - Bonus = (well_grounded / total) * 50% of max, capped at 50%.
    #   - well_grounded = data_delta_count + ui_action_count
    #
    # Why this is monotone:
    #   - Adding a data-delta/ui-action entity: raises well_grounded, raises coverage_bonus -> never lowers score.
    #   - Removing a data-delta/ui-action entity: lowers well_grounded, lowers coverage_bonus -> never raises score.
    #   - The floor is based on "any present", not a mean, so it doesn't rise when you remove entities.
    #   - A spec with all weak evidence still scores lower (0% floor) than one with strong evidence (50% floor).

    if data_delta_count > 0:
        floor_pct = 0.50  # Best evidence: data-delta
    elif ui_action_count > 0:
        floor_pct = 0.35  # Good evidence: ui-action
    else:
        floor_pct = 0.0  # All inference

    # C5 FIX: coverage bonus counts well-grounded entities, it does not divide by all
    # of them.
    #
    # The old formula was `well_grounded / total_entities`, a RATIO. The docstring
    # above proves monotonicity in one direction — adding a data-delta entity never
    # lowers the score — and that is true as written. But the untested direction is
    # the one that matters: adding an entity whose evidence is honestly labelled
    # `inference` grew the denominator and cost 8 of 30 points. One data-delta entity
    # scored 30/30; that same entity plus a declared inference entity scored 22/30.
    # So CONCEALING the inferred entity paid 8 points.
    #
    # That is the same inversion `_score_honesty` calls "the worst possible outcome"
    # — training the loop to hide gaps — smuggled back in through a different
    # dimension's arithmetic. And inference entities are precisely what an honest
    # deriver emits when it cannot resolve a field, so the incentive landed squarely
    # on honest output. "Declaring beats concealing" has to hold in every dimension,
    # not just the one named honesty.
    #
    # Counting well-grounded entities instead keeps every property the ratio had:
    #   - adding a data-delta/ui-action entity still raises the score (monotone up),
    #   - removing one still lowers it (deletion never pays),
    #   - an all-inference spec still scores 0 (the floor, not the bonus, encodes
    #     the observed-vs-assumed distinction),
    # and it drops the one property nobody wanted: that declaring an assumption is
    # punished. `test_c5_inference_only_spec_still_scores_below_observed_spec` pins
    # the distinction that must survive.
    well_grounded = data_delta_count + ui_action_count
    coverage_bonus_pct = min(well_grounded * 0.25, 0.50)  # 2+ grounded entities -> full bonus

    score_pct = floor_pct + coverage_bonus_pct
    score = int(score_pct * max_score)

    # C9 FIX: minimal evidence is now reported, not multiplied.
    #
    # The old rule was `score = int(score * 0.5)` — halve the WHOLE dimension if ANY
    # entity had a stub detail. That is multiplicative and collective, and it broke the
    # G1 invariant this function's docstring claims to guarantee ("removing one never
    # raises it").
    #
    # MEASURED on a spec with two well-evidenced entities plus one whose detail was
    # "ab": grounding 15/30, total 85. DELETING the stub entity scored 30/30 and total
    # 100 — deleting an observed entity paid +15 points, the largest deletion reward
    # this lane found and the exact gradient the refinement loop must never be taught.
    # A fabricator hits the same cliff in reverse: the cheapest response to the penalty
    # is to delete the weakly-evidenced entity rather than to resolve it.
    #
    # The fix is upstream, in the counting loop: an entity with a stub detail no longer
    # counts toward `well_grounded`, so it earns nothing and costs nothing. Keeping it
    # is then weakly better than deleting it (the floor and the bonus are unchanged),
    # which is the incentive the gate should have — resolve the evidence, don't remove
    # the row. What remains here is a finding so the deficiency is still visible.
    if minimal_evidence_count > 0:
        findings.append(
            f"{minimal_evidence_count} entity/entities have minimal/placeholder evidence "
            "details and are not counted as grounded. Resolve the evidence rather than "
            "removing the entity: deleting an observed entity never improves the score."
        )

    # Penalty for padding (Attack 1 fix)
    if padding_detected:
        score = int(score * 0.2)  # Extreme penalty for padding (80% reduction)
        findings.append("Padding penalty applied: score reduced to 20% of base for duplicate field targeting")

    score = max(0, min(score, max_score))  # clamp to [0, max_score]

    evidence_strs.append(f"{data_delta_count}/{total_entities} observed entities from data-delta")
    evidence_strs.append(f"{ui_action_count}/{total_entities} observed entities from ui-action")
    evidence_strs.append(f"{inference_count}/{total_entities} observed entities from inference")
    if mandated_record_ids:
        evidence_strs.append(f"{len(mandated_record_ids)} mandated recordId(s) (not penalized)")
    evidence_strs.append(
        f"Grounding quality: floor={floor_pct:.0%} (best evidence present) + "
        f"coverage_bonus={coverage_bonus_pct:.0%} ({well_grounded} well-grounded entity/entities)"
    )

    if data_delta_count == 0 and ui_action_count == 0:
        findings.append("No entities grounded in observed data (data-delta or ui-action).")

    if inference_count > total_entities * 0.5:
        findings.append(
            f"Over half of observed entities ({inference_count}/{total_entities}) are inferred, not observed. "
            "This spec describes assumptions more than observed behaviour."
        )

    return DimensionScore(
        name="evidence_grounding",
        score=score,
        max_score=max_score,
        weight=max_score,
        findings=findings,
        evidence=evidence_strs,
    )


def _score_completeness(spec: DerivedAgentSpec) -> DimensionScore:
    """Score whether all required sections are present and non-trivial.

    F3 FIX: Count DISTINCT non-trivial orchestration steps, so two identical steps
    do not read as a two-step process.

    PADDING ATTACK FIX (Attacks 9 & 10): Detect extreme padding where the ratio of
    total items to distinct items is suspiciously high (e.g., 10 steps but only 2-3 distinct).
    """
    import re

    max_score = DIMENSION_WEIGHTS["completeness"]
    findings: list[str] = []
    evidence_strs: list[str] = []

    # Each section contributes equally.
    section_score = max_score // 5

    score = 0
    if spec.objects_touched:
        score += section_score
        evidence_strs.append(f"{len(spec.objects_touched)} object(s) touched")
    else:
        findings.append("No objects_touched.")

    if spec.entities:
        score += section_score
        evidence_strs.append(f"{len(spec.entities)} entities derived")
    else:
        findings.append("No entities.")

    # F3: Count DISTINCT steps (normalize: strip, lowercase, collapse whitespace, strip trailing digits)
    # DEFECT 4 FIX: Defensive check - ensure orchestration_steps is a list of strings
    distinct_steps = set()
    steps = spec.orchestration_steps if isinstance(spec.orchestration_steps, list) else []
    for step in steps:
        if not isinstance(step, str):
            continue
        normalized = " ".join(step.strip().lower().split())
        # Strip trailing digits/numbers to detect padding like "Resolve Case 1", "Resolve Case 2", ...
        # This catches patterns where the only difference is a numeric suffix.
        normalized_without_numbers = re.sub(r'\s+\d+$', '', normalized)  # Strip " 123" at end
        if normalized_without_numbers:  # non-empty after normalization
            distinct_steps.add(normalized_without_numbers)

    # PADDING DETECTION (Attack 9): If we have >5 steps but only 1-3 distinct, it's padding.
    steps_padding_ratio = len(spec.orchestration_steps) / max(len(distinct_steps), 1)
    steps_padding_detected = len(spec.orchestration_steps) > 5 and steps_padding_ratio > 3

    if len(distinct_steps) > 1:  # >1 because a trivial spec has exactly 1
        step_score = section_score
        if steps_padding_detected:
            step_score = section_score // 4  # Heavy penalty for padding
            findings.append(
                f"PADDING DETECTED in orchestration_steps: {len(spec.orchestration_steps)} steps "
                f"but only {len(distinct_steps)} distinct (ratio {steps_padding_ratio:.1f}:1). "
                "This is likely an attack to inflate step counts."
            )
        score += step_score
        evidence_strs.append(f"{len(distinct_steps)} distinct orchestration steps (from {len(spec.orchestration_steps)} total)")
    else:
        # Distinct count <= 1, which is trivial. But if we have >5 raw steps, it's ALSO padding.
        if len(spec.orchestration_steps) > 5:
            findings.append(
                f"PADDING DETECTED in orchestration_steps: {len(spec.orchestration_steps)} steps "
                f"but only {len(distinct_steps)} distinct. This is likely an attack to inflate step counts."
            )
        else:
            findings.append("Orchestration steps trivial or absent (distinct count <= 1).")

    # F3: Also check for distinct guardrails (same logic as steps)
    # DEFECT 4 FIX: Defensive check - ensure guardrails is a list of strings
    distinct_guardrails = set()
    guardrails = spec.guardrails if isinstance(spec.guardrails, list) else []
    for guardrail in guardrails:
        if not isinstance(guardrail, str):
            continue
        normalized = " ".join(guardrail.strip().lower().split())
        # Strip trailing digits to detect padding like "Enforce FLS on Case 1", "Enforce FLS on Case 2", ...
        normalized_without_numbers = re.sub(r'\s+\d+$', '', normalized)
        if normalized_without_numbers:
            distinct_guardrails.add(normalized_without_numbers)

    # PADDING DETECTION (Attack 10): Same logic as steps.
    # DEFECT 4 FIX: Use the defensive local guardrails variable, not spec.guardrails
    guardrails_padding_ratio = len(guardrails) / max(len(distinct_guardrails), 1)
    guardrails_padding_detected = len(guardrails) > 5 and guardrails_padding_ratio > 3

    if guardrails and len(distinct_guardrails) > 0:
        guardrail_score = section_score
        # Award full section score only if guardrails are non-duplicate
        if len(distinct_guardrails) < len(guardrails):
            # Duplicate guardrails detected
            if guardrails_padding_detected:
                guardrail_score = section_score // 4  # Heavy penalty for padding
                findings.append(
                    f"PADDING DETECTED in guardrails: {len(guardrails)} guardrails "
                    f"but only {len(distinct_guardrails)} distinct (ratio {guardrails_padding_ratio:.1f}:1). "
                    "This is likely an attack to inflate guardrail counts."
                )
            else:
                guardrail_score = section_score // 2  # Partial penalty for normal duplicates
                findings.append(
                    f"{len(guardrails) - len(distinct_guardrails)} duplicate guardrail(s) detected "
                    f"({len(distinct_guardrails)} distinct)"
                )
            evidence_strs.append(f"{len(distinct_guardrails)} distinct guardrails (from {len(guardrails)} total)")
        score += guardrail_score
        if not (len(distinct_guardrails) < len(guardrails)):
            evidence_strs.append(f"{len(guardrails)} guardrails")
    else:
        findings.append("No guardrails.")

    if spec.failure_handling:
        score += section_score
        evidence_strs.append(f"{len(spec.failure_handling)} failure handling entries")
    else:
        findings.append("No failure_handling.")

    return DimensionScore(
        name="completeness",
        score=score,
        max_score=max_score,
        weight=max_score,
        findings=findings,
        evidence=evidence_strs,
    )


def _score_honesty(spec: DerivedAgentSpec) -> DimensionScore:
    """Score whether the spec is honest about its unknowns.

    SUBTLETY: a spec with declared unknowns is BETTER than one that silently omits
    them. Penalise heavily when a spec claims high confidence AND has structural
    gaps (dishonest). Reward explicit unknowns at low confidence (honest).

    DEFECT 4 FIX: Fails closed - NaN/inf/negative confidence treated as 0;
    non-list unknowns treated as empty list.
    """
    import math
    max_score = DIMENSION_WEIGHTS["honesty"]
    findings: list[str] = []
    evidence_strs: list[str] = []

    # DEFECT 4 FIX: Clamp confidence to [0, 1] and handle NaN/inf
    confidence = spec.confidence
    if not isinstance(confidence, (int, float)) or math.isnan(confidence) or math.isinf(confidence):
        confidence = 0.0
        findings.append("Confidence is malformed (NaN/inf), treated as 0.")
    else:
        confidence = max(0.0, min(1.0, confidence))
        if spec.confidence < 0 or spec.confidence > 1:
            findings.append(f"Confidence {spec.confidence} out of bounds, clamped to {confidence}.")

    # DEFECT 4 FIX: Handle non-list unknowns
    unknown_count = len(spec.unknowns) if isinstance(spec.unknowns, list) else 0
    if not isinstance(spec.unknowns, list):
        findings.append("unknowns field is malformed (not a list), treated as empty.")

    evidence_strs.append(f"confidence={confidence:.2f}")
    evidence_strs.append(f"{unknown_count} unknown(s) declared")

    # Dishonesty detector: high confidence + structural gaps (no objects/entities).
    has_gaps = not spec.objects_touched or not spec.entities
    is_high_confidence = confidence >= 0.7

    if is_high_confidence and has_gaps:
        # This is DISHONEST: claiming high confidence while missing foundational data.
        score = 0
        findings.append(
            f"DISHONEST: spec claims confidence={confidence:.2f} but has structural gaps "
            f"(objects_touched={bool(spec.objects_touched)}, entities={bool(spec.entities)}). "
            "High confidence on incomplete data trains the loop to fabricate."
        )
    elif not is_high_confidence and unknown_count > 0:
        # HONEST: low confidence and explicit unknowns. Reward this.
        score = max_score
        evidence_strs.append("Honest: low confidence with explicit unknowns (good).")
    elif not is_high_confidence and unknown_count == 0 and has_gaps:
        # Low confidence but gaps not declared. Somewhat honest (low conf) but not explicit.
        score = max_score // 2
        findings.append("Low confidence but gaps not explicitly declared in unknowns.")
    elif not has_gaps:
        # G3 FIX: No structural gaps. Whether unknowns are declared or not, this is honest.
        # A spec can have good structural data (objects, entities) but still declare unknowns
        # about specific fields, error handling, etc. Declaring them should not lower the score.
        score = max_score
        if unknown_count > 0:
            evidence_strs.append("Honest: declares unknowns despite having structural data (transparent).")
        else:
            evidence_strs.append("High confidence with no structural gaps (ideal).")

        # C2 FIX: confidence above the deriver's own ceiling is an overclaim.
        #
        # This branch previously awarded 20/20 for ANY confidence value, so the field
        # was inert whenever objects and entities were present: 0.0 and 1.0 both
        # scored 20/20 and both totalled 95/100. But `_derive_intent` never returns
        # above BUILDER_MAX_CONFIDENCE (0.7) — 0.7 for a single object with observed
        # field changes, 0.5 for several objects, 0.4/0.2/0.05 below that. A spec
        # claiming more than 0.7 therefore asserts certainty the builder is incapable
        # of producing, which is exactly the overclaim this dimension exists to catch.
        #
        # Confidence is also the number a human reads first, so a gate that ignores it
        # teaches the loop that confidence is a free parameter — inflate it, lose
        # nothing, look more authoritative.
        #
        # The penalty is proportional rather than a cliff, and it lands only ABOVE the
        # honest ceiling, so genuine output at 0.7 keeps full marks
        # (`test_c2_builder_confidence_ceiling_is_not_penalised` pins that).
        #
        # Capped at HALF the dimension deliberately. An inflated confidence value is a
        # real dishonesty signal but it is ONE numeric field, and honesty is the
        # second-heaviest dimension at 20 points. A full-dimension penalty would both
        # outweigh the "high confidence + structural gaps" case above — which is
        # substantively worse, and scores 0 — and drive honesty low enough to trip the
        # hollow-dimension blocker, turning a single overstated float into an
        # unconditional block. Overclaiming must cost real points, not everything.
        if confidence > BUILDER_MAX_CONFIDENCE:
            overclaim = confidence - BUILDER_MAX_CONFIDENCE
            max_penalty = max_score // 2
            penalty = min(
                max_penalty,
                round(overclaim / (1.0 - BUILDER_MAX_CONFIDENCE) * max_penalty),
            )
            score = max(0, max_score - penalty)
            findings.append(
                f"Confidence {confidence:.2f} exceeds the deriver's maximum "
                f"({BUILDER_MAX_CONFIDENCE:.2f}); the builder cannot produce this much "
                "certainty, so the value is an overclaim rather than information."
            )
    else:
        # Default: moderate honesty (low confidence, no declared unknowns, but some structure).
        score = max_score * 3 // 4

    return DimensionScore(
        name="honesty",
        score=score,
        max_score=max_score,
        weight=max_score,
        findings=findings,
        evidence=evidence_strs,
    )


def _is_filler_instruction(text: object) -> bool:
    """Is this instruction string too thin to be an instruction at all?

    C8: answers the question no other check in this module asks — not "does this
    contain a known-bad phrase" but "does this carry any content". See
    MIN_INSTRUCTION_CHARS for why both thresholds sit far below honest output, and
    for the limits of what a floor like this can do.

    Deliberately returns True for a non-string: a spec whose steps are integers has
    no instructions either, and the callers treat malformed and empty alike.
    """
    if not isinstance(text, str):
        return True
    stripped = text.strip()
    return len(stripped) < MIN_INSTRUCTION_CHARS or len(stripped.split()) < MIN_INSTRUCTION_WORDS


def _instruction_strings(spec: DerivedAgentSpec) -> list[object]:
    """The strings a human reads to decide whether to trust the agent.

    Steps and guardrails only. `failure_handling` and `unknowns` are excluded on
    purpose: the honest builder writes short declarative entries there ("UNTESTED"),
    and penalising terseness in the fields that exist to declare ignorance would
    reward deleting them — the same declaring-beats-concealing inversion the C5 fix
    removed from evidence_grounding.
    """
    steps = spec.orchestration_steps if isinstance(spec.orchestration_steps, list) else []
    guardrails = spec.guardrails if isinstance(spec.guardrails, list) else []
    return [*steps, *guardrails]


def _score_specificity(spec: DerivedAgentSpec) -> DimensionScore:
    """Score whether the spec is concrete rather than generic.

    D11 FIX: Empty intent/steps/guardrails now score 0, not full marks vacuously.
    An empty spec has no specificity; it should score 0, not be rewarded for
    avoiding known-bad patterns by having no content at all.
    """
    max_score = DIMENSION_WEIGHTS["specificity"]
    findings: list[str] = []
    evidence_strs: list[str] = []

    # D11 FIX + DEFECT 4 FIX: Check for absent/malformed content first. Empty or non-string scores 0.
    # MUST check isinstance BEFORE calling any string methods to avoid AttributeError.
    if not isinstance(spec.intent, str) or not spec.intent.strip():
        findings.append("Intent is absent or malformed (empty spec has no specificity).")
        return DimensionScore(
            name="specificity",
            score=0,
            max_score=max_score,
            weight=max_score,
            findings=findings,
            evidence=["Intent is empty or malformed"],
        )

    score = max_score

    # Check intent.
    if "UNRESOLVED" in spec.intent:
        score -= max_score // 3
        findings.append("Intent is UNRESOLVED.")
    elif spec.intent in ("Complete a data-entry process", "Interact with guided process"):
        score -= max_score // 4
        findings.append("Intent is generic.")
    # ATTACK 5 FIX: Also check for single-word or very short intents with generic terms
    elif len(spec.intent.split()) <= 2 or any(term in spec.intent.lower() for term in ["generic", "action", "something", "vague"]):
        score -= max_score // 2
        findings.append(f"Intent is too generic or vague: {spec.intent[:40]}")
        evidence_strs.append(f"Intent flagged as generic: {spec.intent[:50]}")
    # ATTACK 6 FIX: Check for keyword-stuffed intents (>6 words OR >3 capitalized field-like words)
    elif len(spec.intent.split()) > 6:
        score -= max_score // 2
        findings.append(f"Intent appears keyword-stuffed ({len(spec.intent.split())} words): {spec.intent[:40]}")
        evidence_strs.append(f"Intent flagged as keyword-stuffed: {spec.intent[:50]}")
    else:
        evidence_strs.append(f"Intent is specific: {spec.intent[:50]}")

    # Check for generic placeholder terms in orchestration steps.
    #
    # C4 FIX: dropped "the record", "the field", "the value", "step" and "action" from
    # this list.
    #
    # These were substring-matched against ordinary English, so they fired on the
    # BUILDER'S OWN honest output. The example capture scored specificity 9/10, and the
    # missing point came entirely from the deriver's closing step, "Return a
    # confirmation that names the record and the fields changed." — a sentence the
    # builder emits verbatim and cannot avoid. Deleting that step scored 10/10.
    #
    # A check that only fires on the honest path is worse than no check. It costs the
    # fabricator nothing (write "Return a confirmation naming Case.Priority" and the
    # deduction vanishes) while the builder pays it every run, and it reports a defect
    # that isn't one. The words "step" and "action" were the worst offenders: they
    # appear in almost any correct description of a UI step.
    #
    # What remains are terms that are self-describing placeholders — a step whose text
    # contains "generic" or "placeholder" is telling you it has no content. The
    # regex-anchored vacuous-content checks below ("Step 1", "Guardrail 2") do the
    # structural work, and the new hollow-dimension blocker in score_spec() catches the
    # case where the whole dimension has been emptied.
    # DEFECT 4 FIX: Defensive check - ensure orchestration_steps is a list of strings
    generic_terms = ["generic", "placeholder", "lorem ipsum"]
    steps = spec.orchestration_steps if isinstance(spec.orchestration_steps, list) else []
    for step in steps:
        if not isinstance(step, str):
            continue
        step_lower = step.lower().strip()
        # Heavy penalty for extremely generic steps like "Step 1", "Action 2", etc.
        # Pattern: single word + optional number
        import re
        if re.match(r'^(step|action|task|item)(\s+\d+)?$', step_lower):
            score -= 3  # Heavy penalty for vacuous steps
            findings.append(f"Vacuous orchestration step: {step[:30]}")
        else:
            for term in generic_terms:
                if term in step_lower:
                    score -= 1  # small penalty per instance
                    # C4: emit a finding. This deduction used to be silent, so the
                    # example capture reported specificity 9/10 with an EMPTY findings
                    # list — a point docked with no stated reason, which is unauditable
                    # and was in fact a false positive on honest prose.
                    findings.append(f"Generic term {term!r} in orchestration step: {step[:40]}")
                    break

    # Check for generic guardrails (new for F1)
    # DEFECT 4 FIX: Defensive check - ensure guardrails is a list of strings
    generic_guardrail_terms = [
        "validate input",
        "check input",
        "verify input",
        "validate data",
        "ensure valid",
    ]
    guardrails = spec.guardrails if isinstance(spec.guardrails, list) else []
    for guardrail in guardrails:
        if not isinstance(guardrail, str):
            continue
        guardrail_lower = guardrail.lower().strip()
        # Heavy penalty for vacuous guardrails like "Guardrail 1", "Rule 2", etc.
        import re
        if re.match(r'^(guardrail|rule|check|validation)(\s+\d+)?$', guardrail_lower):
            score -= 3  # Heavy penalty for vacuous guardrails
            findings.append(f"Vacuous guardrail: {guardrail[:30]}")
        else:
            for term in generic_guardrail_terms:
                if term in guardrail_lower:
                    score -= 2  # larger penalty for generic guardrails
                    findings.append(f"Generic guardrail: {guardrail[:40]}")
                    break

    # C8: dock filler instructions — strings too thin to instruct anything.
    #
    # Every check above is a blocklist over known-bad phrases, so steps of "aa",
    # "bb", "ee" with guardrails "cc", "ff" cleared all of them and the spec scored
    # 10/10 specificity on its way to a perfect 100/100. Content that says nothing
    # has no fingerprint to match, so this asks about substance instead.
    #
    # The deduction is per-string and capped at the dimension: one terse line should
    # not zero the dimension, but an all-filler narrative should bottom it out and
    # thereby also trip the hollow-dimension blocker in score_spec().
    instructions = _instruction_strings(spec)
    filler = [text for text in instructions if _is_filler_instruction(text)]
    if filler:
        score -= min(score, 2 * len(filler))
        shown = ", ".join(repr(text)[:20] for text in filler[:4])
        findings.append(
            f"{len(filler)}/{len(instructions)} instruction string(s) carry no instruction "
            f"(under {MIN_INSTRUCTION_CHARS} chars or {MIN_INSTRUCTION_WORDS} words): {shown}. "
            "The builder's shortest real step on the example capture is 52 characters."
        )

    if score < 0:
        score = 0

    evidence_strs.append("Orchestration steps and guardrails checked for generic terms.")

    return DimensionScore(
        name="specificity",
        score=score,
        max_score=max_score,
        weight=max_score,
        findings=findings,
        evidence=evidence_strs,
    )


def _score_testability(spec: DerivedAgentSpec) -> DimensionScore:
    """Score whether the spec is testable: entities explicit, failure paths observed.

    D1/F2 FIX: Use the _has_observed_failure helper to match the stable fragment
    the builder emits, not the incidental word "observed" which also appears in
    the negative sentinel.
    """
    max_score = DIMENSION_WEIGHTS["testability"]
    findings: list[str] = []
    evidence_strs: list[str] = []

    score = 0

    # Entities explicit enough to write test utterances.
    # DEFECT 4 FIX: Defensive check - ensure entities is a list
    entities = spec.entities if isinstance(spec.entities, list) else []

    # C3 FIX: count the entities that ARE explicit; do not let one unresolved entity
    # zero the half.
    #
    # This was `all(...)`, so a single entity with object_api_name=None cost the
    # whole 5 points — and the builder emits exactly those for UI inputs it observed
    # but could not map to a field. On the example capture, DELETING the three
    # unresolved ui-action entities raised the total from 84 to 89. Deleting observed
    # evidence paid 5 points, which is a direct incentive to suppress data and the
    # precise inverse of what evidence_grounding is for.
    #
    # Proportional credit removes the incentive without lowering the bar: a spec whose
    # entities are all explicit still earns the full half, one with none earns nothing,
    # and adding an unresolved entity can only move the score toward, never past, the
    # value it would have without it. The builder should still learn to resolve those
    # fields — that is what the finding below is for — but "resolve it" and "delete it"
    # must not be priced the same.
    explicit = [
        e for e in entities
        if hasattr(e, 'object_api_name') and hasattr(e, 'field_api_name')
        and (e.object_api_name or e.field_api_name)
    ]
    half = max_score // 2
    if explicit:
        # Credit turns on whether the spec is testable AT ALL, which is what the
        # dimension claims to measure: one entity with a resolved object+field is
        # enough to write a test utterance against. Extra unresolved entities do not
        # make the spec less testable — they mean there is more you could test once
        # they are resolved — so they produce a finding, not a deduction.
        #
        # This is strictly more generous than the old `all(...)`, and deliberately so.
        # A fabricator was never affected by the strict rule: they simply never include
        # an unresolved entity, so they always scored the full half either way. The
        # only party the strictness reached was the honest builder, which emits
        # unresolved entities for UI inputs it genuinely observed. The rule therefore
        # taxed honesty and paid for deletion while doing nothing to a fabricator.
        score += half
        evidence_strs.append(
            f"{len(explicit)}/{len(entities)} entities have explicit object/field names."
        )
        if len(explicit) < len(entities):
            findings.append(
                f"{len(entities) - len(explicit)} entity/entities lack explicit object/field "
                "names; resolve them rather than removing them (removing observed entities "
                "never improves the score)."
            )
    else:
        findings.append("Entities lack explicit object/field names.")

    # Failure paths observed (not just asserted).
    # D1/F2: Use the helper that matches the builder's actual output format.
    if _has_observed_failure(spec.failure_handling):
        score += max_score // 2
        evidence_strs.append("Failure paths observed in recording.")
    else:
        findings.append("No observed failure paths (error paths untested).")

    return DimensionScore(
        name="testability",
        score=score,
        max_score=max_score,
        weight=max_score,
        findings=findings,
        evidence=evidence_strs,
    )


def _score_placeholder_freedom(
    spec: DerivedAgentSpec,
    yaml_text: str | None,
    agent_script_text: str | None,
) -> DimensionScore:
    """Scan for placeholder/stub markers that indicate fake content.

    D11 FIX: An empty spec (no entities, no steps, no guardrails, no failures)
    now scores 0 rather than 10/10 vacuously. Absent content is not the same as
    clean content — it's 10 points of noise that inflates every partially-empty spec.
    """
    max_score = DIMENSION_WEIGHTS["placeholder_freedom"]
    findings: list[str] = []
    evidence_strs: list[str] = []

    # D11 FIX: Check for effectively empty spec. If there's no substantive content,
    # score 0 rather than awarding full marks for having no text to scan.
    has_content = (
        spec.entities or
        spec.orchestration_steps or
        spec.guardrails or
        spec.failure_handling or
        (spec.intent and spec.intent.strip())
    )

    if not has_content:
        findings.append("Spec is effectively empty (no content to assess for placeholder freedom).")
        return DimensionScore(
            name="placeholder_freedom",
            score=0,
            max_score=max_score,
            weight=max_score,
            findings=findings,
            evidence=["Spec has no substantive content"],
        )

    # Delegate spec scanning to markers.scan_spec so this gate and
    # scripts/score_run.py cannot disagree about WHERE to look. They previously
    # did: this function scanned entity *names* but not evidence *details*, while
    # score_run.py read the whole JSON as text. A spec with TODO/FIXME/Lorem ipsum
    # sitting in its evidence details — the field a reviewer reads to decide
    # whether to trust the spec at all — scored a clean 10/10 here and was blocked
    # there. Two gates disagreeing means whichever one you happen to run decides
    # the answer, so the scan scope now lives in exactly one place.
    #
    # DEFECT 4 FIX: to_dict() may crash if spec has malformed entities/evidence
    # (non-list or wrong types). Fail closed: if to_dict() crashes, score 0.
    try:
        hits = scan_spec(spec.to_dict())
    except (AttributeError, TypeError) as e:
        # Malformed spec structure crashed to_dict(). This is highly suspicious.
        findings.append(f"Spec structure malformed, failed to serialize: {type(e).__name__}")
        return DimensionScore(
            name="placeholder_freedom",
            score=0,
            max_score=max_score,
            weight=max_score,
            findings=findings,
            evidence=["Spec serialization failed (malformed structure)"],
        )

    # The emitted artifacts are separate documents, not part of the spec dict.
    if yaml_text:
        hits.extend(scan_text(yaml_text))
    if agent_script_text:
        hits.extend(scan_text(agent_script_text))

    if hits:
        score = 0
        findings.extend(hits[:5])  # limit to 5
        evidence_strs.append(f"{len(hits)} placeholder marker(s) detected.")
    else:
        score = max_score
        evidence_strs.append("No placeholder markers detected.")

    return DimensionScore(
        name="placeholder_freedom",
        score=score,
        max_score=max_score,
        weight=max_score,
        findings=findings,
        evidence=evidence_strs,
    )


def _assert_scorer_is_falsifiable() -> None:
    """Self-check: prove the scorer can fail.

    This is a sanity check to ensure the scorer is not a rubber stamp. It creates
    two synthetic specs (one good, one bad) and confirms the bad one scores below
    threshold.

    F1: The bad spec is the exact one from the contract that currently scores 100/100.
    """
    from .spec_builder import DerivedEntity, SpecEvidence

    # Good spec: concrete intent, observed data, explicit unknowns at reasonable confidence.
    good_spec = DerivedAgentSpec(
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
                evidence=[SpecEvidence("inference", "a Case record must be identified to act on it")],
            ),
        ],
        orchestration_steps=[
            "Resolve and load the target Case record; confirm the caller may act on it.",
            "SUBMIT on button:Save -> writes Status (backend: validation, workflow)",
            "Return a confirmation that names the record and the fields changed.",
        ],
        guardrails=[
            "Enforce object- and field-level security on Case for the running user.",
            "Require explicit user confirmation before writing: Status.",
        ],
        failure_handling=["Observed validation failure during recording: Status must be one of approved values"],
        unknowns=[],
        evidence=[
            SpecEvidence("telemetry", "backend layers observed: validation, workflow"),
            SpecEvidence("extraction", "3 action(s) in recording"),
            SpecEvidence("data-delta", "objects mutated: Case"),
        ],
    )

    # F1: The exact bad spec from the contract that must score < 75 and pass=False.
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

    print(f"[spec_score] Falsifiability check PASSED: good={good_score.total}, bad={bad_score.total}")
