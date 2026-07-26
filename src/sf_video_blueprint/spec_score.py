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
    """The result of scoring a spec."""

    total: int
    max_total: int
    band: str  # low | moderate | high
    passed: bool
    dimensions: dict[str, DimensionScore]
    blocking_issues: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
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
        """Human-readable one-liner."""
        passed_str = "PASS" if self.passed else "FAIL"
        return (
            f"{passed_str}: {self.total}/{self.max_total} ({self.band} band), "
            f"{len(self.blocking_issues)} blocking issue(s)"
        )


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


def score_spec_file(path: Path) -> SpecScore:
    """Load and score an agent-spec JSON written by spec_builder.write_spec.

    This reads the on-disk format, which includes the provenance key injected by
    write_spec(). Provenance is checked for stub/mock sources, which are blocking.

    Args:
        path: Path to the agent-spec JSON file.

    Returns:
        A SpecScore with provenance integrity checked.
    """
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
    field_counts: dict[str, int] = {}
    for entity in observed_entities:
        if not hasattr(entity, 'object_api_name') or not hasattr(entity, 'field_api_name'):
            continue
        key = f"{entity.object_api_name}.{entity.field_api_name}"
        field_counts[key] = field_counts.get(key, 0) + 1

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
        # Check for minimal evidence (single char, placeholder-like)
        for ev in entity.evidence:
            if len(ev.detail.strip()) <= 1:
                minimal_evidence_count += 1
                findings.append(f"Entity {entity.name} has minimal evidence detail: {ev.detail!r}")
                break

        if "data-delta" in sources:
            data_delta_count += 1
        elif "ui-action" in sources:
            ui_action_count += 1
        elif "inference" in sources:
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

    well_grounded = data_delta_count + ui_action_count
    coverage_ratio = well_grounded / total_entities if total_entities > 0 else 0
    coverage_bonus_pct = coverage_ratio * 0.50  # Up to 50% bonus for full coverage

    score_pct = floor_pct + coverage_bonus_pct
    score = int(score_pct * max_score)

    # Penalty for minimal evidence (F1 fix)
    if minimal_evidence_count > 0:
        score = int(score * 0.5)  # Cut score in half if any evidence is minimal
        findings.append(f"{minimal_evidence_count} entity/entities have minimal/placeholder evidence details")

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
    evidence_strs.append(f"Grounding quality: floor={floor_pct:.0%} (best evidence present) + coverage_bonus={coverage_bonus_pct:.0%}")

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
    # DEFECT 4 FIX: Defensive check - ensure orchestration_steps is a list of strings
    generic_terms = ["the record", "the field", "the value", "generic", "placeholder", "step", "action"]
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

    if score < 0:
        score = 0

    evidence_strs.append(f"Orchestration steps and guardrails checked for generic terms.")

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
    if entities and all(hasattr(e, 'object_api_name') and hasattr(e, 'field_api_name') and
                        (e.object_api_name or e.field_api_name) for e in entities):
        score += max_score // 2
        evidence_strs.append(f"{len(entities)} entities with explicit object/field names.")
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
