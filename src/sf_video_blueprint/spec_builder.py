from __future__ import annotations

"""Derive a conversational agent spec from observed run data.

This module replaces the hardcoded ``AgentBlueprintSection`` literal that used to
live in ``cli.py``. Everything it emits is a function of the correlated run:
extracted actions, triggered telemetry layers, and observed record deltas.

Design constraints:

* **No invention.** If the run observed no data change, the spec says so rather
  than guessing at an intent. A spec that confidently describes behaviour the
  recording never demonstrated is worse than an empty one, because it looks
  finished.
* **Provenance is explicit.** Every derived field carries the evidence it came
  from, so a reviewer can tell "we saw this" from "we assumed this".
* **Machine-consumable output.** The terminal artifact is JSON/YAML, so it can be
  diffed between runs and fed to ``sf agent generate agent-spec``. HTML cannot be
  iterated on.
"""

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

from .correlation import FailureLayer, StepAnalysis
from .models import ActionType, ExtractedAction
from .telemetry import TelemetryLayer

# Salesforce fields that carry no business meaning for an agent spec.
_SYSTEM_FIELDS = frozenset(
    {
        "Id",
        "CreatedDate",
        "CreatedById",
        "LastModifiedDate",
        "LastModifiedById",
        "SystemModstamp",
        "IsDeleted",
        "LastViewedDate",
        "LastReferencedDate",
    }
)

_WRITE_ACTIONS = frozenset({ActionType.SUBMIT, ActionType.INPUT, ActionType.SELECT})


def _camel(raw: str) -> str:
    """Turn a Salesforce API name or UI label into a lowerCamelCase entity name.

    ``Status`` -> ``status``; ``StageName`` -> ``stageName``;
    ``Account_Name__c`` -> ``accountName``; ``Close Date`` -> ``closeDate``.
    """
    cleaned = raw.strip()
    if cleaned.endswith("__c"):
        cleaned = cleaned[:-3]
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", cleaned) if part]
    if not parts:
        return "value"
    if len(parts) == 1:
        single = parts[0]
        return single[0].lower() + single[1:]
    head, *rest = parts
    return head[0].lower() + head[1:] + "".join(part[:1].upper() + part[1:] for part in rest)


@dataclass(slots=True)
class SpecEvidence:
    """Why a spec element exists. Keeps derivation auditable."""

    source: str
    detail: str


@dataclass(slots=True)
class DerivedEntity:
    name: str
    object_api_name: str | None
    field_api_name: str | None
    evidence: list[SpecEvidence] = field(default_factory=list)


@dataclass(slots=True)
class DerivedAgentSpec:
    intent: str
    confidence: float
    objects_touched: list[str]
    entities: list[DerivedEntity]
    orchestration_steps: list[str]
    guardrails: list[str]
    failure_handling: list[str]
    unknowns: list[str]
    evidence: list[SpecEvidence]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "intent": self.intent,
            "confidence": round(self.confidence, 3),
            "objects_touched": self.objects_touched,
            "entities": [
                {
                    "name": item.name,
                    "object_api_name": item.object_api_name,
                    "field_api_name": item.field_api_name,
                    "evidence": [{"source": e.source, "detail": e.detail} for e in item.evidence],
                }
                for item in self.entities
            ],
            "orchestration_steps": self.orchestration_steps,
            "guardrails": self.guardrails,
            "failure_handling": self.failure_handling,
            "unknowns": self.unknowns,
            "evidence": [{"source": e.source, "detail": e.detail} for e in self.evidence],
        }


def build_agent_spec(
    actions: list[ExtractedAction],
    analyses: list[StepAnalysis],
    extraction_warnings: list[str] | None = None,
) -> DerivedAgentSpec:
    """Derive a spec from what the run actually observed.

    Args:
        actions: List of extracted actions from the recording.
        analyses: List of step analyses with correlated telemetry and snapshots.
        extraction_warnings: Optional list of warnings from the extraction phase
                           (e.g., redaction warnings) that represent missing information.
    """
    evidence: list[SpecEvidence] = []
    unknowns: list[str] = []

    objects: list[str] = []
    for analysis in analyses:
        for snapshot in analysis.data_changes:
            if snapshot.object_api_name not in objects:
                objects.append(snapshot.object_api_name)

    # DEFECT 2 FIX: Thread extraction warnings into unknowns
    # Filter for warnings that represent MISSING INFORMATION (redaction, suppression)
    if extraction_warnings:
        for warning in extraction_warnings:
            # Redaction warnings indicate missing field values
            if "redacted" in warning.lower():
                # Extract field reference from warning without echoing the value
                # Expected format: "Step N: value redacted, requires manual input at replay"
                unknowns.append(
                    f"Value redacted during recording: manual input required at replay. "
                    f"The field selector is present but the actual value was redacted for security."
                )

    entities, ambiguous_fields = _derive_entities(actions, analyses, objects)
    intent, confidence = _derive_intent(actions, analyses, objects)

    # DEFECT 1 FIX: Record ambiguous correlations in unknowns
    if ambiguous_fields:
        unknowns.append(
            f"Ambiguous field correlation: {len(ambiguous_fields)} field(s) had multiple data snapshots "
            f"in the correlation window. The closest snapshot was chosen, but the provenance is uncertain. "
            f"Fields: {', '.join(ambiguous_fields[:5])}"
            + (f" (and {len(ambiguous_fields) - 5} more)" if len(ambiguous_fields) > 5 else "")
        )

    if not objects:
        unknowns.append(
            "No record-level data change was observed, so the target object is unknown. "
            "Re-run with --track-record ObjectApiName:RecordId to capture field deltas."
        )
    if not entities:
        unknowns.append(
            "No input entities could be derived: the recording contained no input/select "
            "actions and no observed field changes."
        )

    steps = _derive_orchestration(actions, analyses, objects)
    guardrails = _derive_guardrails(analyses, objects)
    failure_handling = _derive_failure_handling(analyses)

    layers = sorted({layer.value for a in analyses for layer in a.triggered_layers})
    if layers:
        evidence.append(SpecEvidence("telemetry", f"backend layers observed: {', '.join(layers)}"))
    evidence.append(SpecEvidence("extraction", f"{len(actions)} action(s) in recording"))
    if objects:
        evidence.append(SpecEvidence("data-delta", f"objects mutated: {', '.join(objects)}"))

    if not any(a.triggered_layers for a in analyses):
        unknowns.append(
            "No backend telemetry was correlated to any step, so the orchestration below "
            "reflects UI sequence only, not verified server-side behaviour."
        )

    return DerivedAgentSpec(
        intent=intent,
        confidence=confidence,
        objects_touched=objects,
        entities=entities,
        orchestration_steps=steps,
        guardrails=guardrails,
        failure_handling=failure_handling,
        unknowns=unknowns,
        evidence=evidence,
    )


def _derive_intent(
    actions: list[ExtractedAction],
    analyses: list[StepAnalysis],
    objects: list[str],
) -> tuple[str, float]:
    """Infer a verb+object intent. Confidence reflects evidence, not optimism."""
    changed_fields = [
        field_name
        for analysis in analyses
        for snapshot in analysis.data_changes
        for field_name in snapshot.changed_fields
        if field_name not in _SYSTEM_FIELDS
    ]
    has_writes = any(a.action_type in _WRITE_ACTIONS for a in actions)
    obj = objects[0] if objects else None

    if obj and changed_fields:
        verb = "Create" if _looks_like_create(analyses) else "Update"
        fields = ", ".join(dict.fromkeys(changed_fields[:3]))
        return f"{verb} {obj} ({fields})", 0.7 if len(objects) == 1 else 0.5
    if obj:
        return f"Interact with {obj} via guided process", 0.4
    if has_writes:
        return "Complete a data-entry process (target object unresolved)", 0.2
    return "UNRESOLVED: recording did not demonstrate a completed business action", 0.05


def _looks_like_create(analyses: list[StepAnalysis]) -> bool:
    for analysis in analyses:
        for snapshot in analysis.data_changes:
            if not snapshot.before:
                return True
    return False


def _derive_entities(
    actions: list[ExtractedAction],
    analyses: list[StepAnalysis],
    objects: list[str],
) -> tuple[list[DerivedEntity], list[str]]:
    """Entities come from observed field deltas and from user input actions.

    Returns:
        A tuple of (entities, ambiguous_fields) where ambiguous_fields lists
        field keys that had AMBIGUOUS correlation confidence.
    """
    entities: dict[str, DerivedEntity] = {}
    ambiguous_fields: list[str] = []

    for analysis in analyses:
        # DEFECT 1 FIX: Use correlated_snapshots with confidence, not raw data_changes
        for corr_snap in analysis.correlated_snapshots:
            snapshot = corr_snap.snapshot
            confidence = corr_snap.confidence

            for field_name in snapshot.changed_fields:
                if field_name in _SYSTEM_FIELDS:
                    continue
                key = f"{snapshot.object_api_name}.{field_name}"

                # DEFECT 1 FIX: Map correlation confidence to evidence source
                # The scorer recognizes: "data-delta" (strong), "ui-action" (medium), "inference" (weak)
                # HIGH → "data-delta" (within time window AND step_id agrees - strongest evidence)
                # TEMPORAL → "data-delta" (within time window but no step_id match - still good)
                # ASSERTED → "inference" (step_id only, outside window - clock skew, basically a guess)
                # AMBIGUOUS → "inference" (multiple candidates, chose closest - a guess)
                from .correlation import CorrelationConfidence

                if confidence == CorrelationConfidence.HIGH:
                    source = "data-delta"
                    detail = (
                        f"{key} changed "
                        f"{snapshot.before.get(field_name, '<unset>')!r} -> "
                        f"{snapshot.after.get(field_name, '<unset>')!r} at {analysis.step_id}"
                    )
                elif confidence == CorrelationConfidence.TEMPORAL:
                    source = "data-delta"
                    detail = (
                        f"{key} changed "
                        f"{snapshot.before.get(field_name, '<unset>')!r} -> "
                        f"{snapshot.after.get(field_name, '<unset>')!r} at {analysis.step_id} "
                        f"(temporal correlation: {corr_snap.note})"
                    )
                elif confidence == CorrelationConfidence.ASSERTED:
                    source = "inference"
                    detail = (
                        f"{key} inferred from caller assertion (clock skew suspected): "
                        f"changed {snapshot.before.get(field_name, '<unset>')!r} -> "
                        f"{snapshot.after.get(field_name, '<unset>')!r} at {analysis.step_id}. "
                        f"Reason: {corr_snap.note}"
                    )
                else:  # AMBIGUOUS
                    source = "inference"
                    detail = (
                        f"{key} ambiguously correlated (multiple candidates, chose closest): "
                        f"changed {snapshot.before.get(field_name, '<unset>')!r} -> "
                        f"{snapshot.after.get(field_name, '<unset>')!r} at {analysis.step_id}. "
                        f"Reason: {corr_snap.note}"
                    )
                    # Track ambiguous fields for unknowns
                    if key not in ambiguous_fields:
                        ambiguous_fields.append(key)

                entities.setdefault(
                    key,
                    DerivedEntity(
                        name=_camel(field_name),
                        object_api_name=snapshot.object_api_name,
                        field_api_name=field_name,
                        evidence=[SpecEvidence(source, detail)],
                    ),
                )

    # A record must be identified before it can be updated.
    if objects:
        entities.setdefault(
            f"{objects[0]}.Id",
            DerivedEntity(
                name="recordId",
                object_api_name=objects[0],
                field_api_name="Id",
                evidence=[SpecEvidence("inference", f"a {objects[0]} record must be identified to act on it")],
            ),
        )

    # UI inputs are keyed by target, while data deltas are keyed by
    # Object.Field — so typing into the Status field and observing Case.Status
    # change used to yield TWO entities both named "status". That produced
    # nonsense derived utterances ("Update Case {status} {status}") and told the
    # generated agent to collect the same value twice.
    #
    # Match on the derived entity NAME, which is what downstream artifacts
    # actually key on, and merge the UI evidence into the existing entity rather
    # than dropping it: a field confirmed by both a data delta and an observed
    # keystroke is better evidenced than one confirmed by either alone.
    by_name = {entity.name: entity for entity in entities.values()}

    for action in actions:
        if action.action_type not in {ActionType.INPUT, ActionType.SELECT}:
            continue
        name = _camel(action.target.split(":")[-1])
        ui_evidence = SpecEvidence(
            "ui-action",
            f"{action.action_type.value} on {action.target!r} at step {action.step_id}",
        )
        existing = by_name.get(name)
        if existing is not None:
            if ui_evidence not in existing.evidence:
                existing.evidence.append(ui_evidence)
            continue

        entity = DerivedEntity(
            name=name,
            object_api_name=None,
            field_api_name=None,
            evidence=[ui_evidence],
        )
        entities[f"input:{action.target}"] = entity
        by_name[name] = entity

    return list(entities.values()), ambiguous_fields


def _derive_orchestration(
    actions: list[ExtractedAction],
    analyses: list[StepAnalysis],
    objects: list[str],
) -> list[str]:
    steps: list[str] = []
    if objects:
        steps.append(f"Resolve and load the target {objects[0]} record; confirm the caller may act on it.")

    by_id = {a.step_id: a for a in actions}
    for analysis in analyses:
        action = by_id.get(analysis.step_id)
        if action is None:
            continue
        layers = ", ".join(sorted(layer.value for layer in analysis.triggered_layers))
        changed = [
            f_name
            for snap in analysis.data_changes
            for f_name in snap.changed_fields
            if f_name not in _SYSTEM_FIELDS
        ]
        detail = f"{action.action_type.value} on {action.target}"
        if changed:
            detail += f" -> writes {', '.join(dict.fromkeys(changed))}"
        if layers:
            detail += f" (backend: {layers})"
        steps.append(detail)

    if any(TelemetryLayer.FLOW in a.triggered_layers for a in analyses):
        steps.append("Invoke the equivalent Flow rather than reproducing its logic in the agent.")
    steps.append("Return a confirmation that names the record and the fields changed.")
    return steps


def _derive_guardrails(analyses: list[StepAnalysis], objects: list[str]) -> list[str]:
    guardrails: list[str] = []
    if objects:
        guardrails.append(
            f"Enforce object- and field-level security on {objects[0]} for the running user; "
            "never widen access beyond the recorded profile."
        )
    if any(TelemetryLayer.VALIDATION in a.triggered_layers for a in analyses):
        guardrails.append(
            "Validation rules fired during this process: surface field-level errors verbatim "
            "instead of retrying blindly."
        )
    writes = [
        f_name
        for a in analyses
        for snap in a.data_changes
        for f_name in snap.changed_fields
        if f_name not in _SYSTEM_FIELDS
    ]
    if writes:
        guardrails.append(
            f"Require explicit user confirmation before writing: {', '.join(dict.fromkeys(writes))}."
        )
    if any(TelemetryLayer.ASYNC in a.triggered_layers for a in analyses):
        guardrails.append(
            "Async work was triggered; do not report success until the async job completes."
        )
    guardrails.append("Scope the agent to the objects listed above; refuse unrelated requests.")
    return guardrails


def _derive_failure_handling(analyses: list[StepAnalysis]) -> list[str]:
    handling: list[str] = []
    observed = {a.failure_layer for a in analyses if a.failure_layer}
    for layer in sorted(observed, key=lambda item: item.value):
        reason = next(
            (a.failure_reason for a in analyses if a.failure_layer == layer and a.failure_reason),
            "no detail captured",
        )
        handling.append(f"Observed {layer.value} failure during recording: {reason}")
    if FailureLayer.VALIDATION in observed:
        handling.append("On validation error, return the offending field and message; do not auto-correct.")
    if not handling:
        handling.append(
            "No failures were observed in this run, so error paths are UNTESTED. "
            "Record a failing variant before relying on this spec."
        )
    return handling


def write_spec(path: Path, spec: DerivedAgentSpec, provenance: dict[str, str]) -> Path:
    """Write the machine-consumable spec (JSON) next to the HTML report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = spec.to_dict()
    payload["provenance"] = provenance
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path
