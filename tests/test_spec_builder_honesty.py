"""Honesty audit for spec_builder.py — 8 properties from INTERFACE_CONTRACT_ROUND2.md.

The spec builder is the honesty chokepoint. Every claim downstream inherits
whatever this module asserts. These tests verify that it never fabricates
evidence and that confidence degrades with weaker inputs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sf_video_blueprint.correlation import FailureLayer, StepAnalysis
from sf_video_blueprint.models import ActionType, ExtractedAction, UIContext
from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence, build_agent_spec
from sf_video_blueprint.telemetry import CorrelationKey, ObjectSnapshot, TelemetryLayer

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _action(step_id: str, sequence: int, action_type: ActionType, target: str, obj: str | None = "Case") -> ExtractedAction:
    return ExtractedAction(
        step_id=step_id,
        sequence=sequence,
        timestamp_ms=sequence * 1000,
        action_type=action_type,
        target=target,
        ui_context=UIContext(object_name=obj),
        confidence=0.9,
    )


def _snapshot(step_id: str, obj: str, before: dict, after: dict, changed: list[str]) -> ObjectSnapshot:
    return ObjectSnapshot(
        correlation=CorrelationKey(run_id="run-1", step_id=step_id, event_time=NOW),
        object_api_name=obj,
        record_id="500xx0000012345AAA",
        before=before,
        after=after,
        changed_fields=changed,
    )


def _analysis(
    step_id: str,
    target: str,
    *,
    layers: list[TelemetryLayer] | None = None,
    changes: list[ObjectSnapshot] | None = None,
    failure: FailureLayer | None = None,
) -> StepAnalysis:
    from sf_video_blueprint.correlation import CorrelatedSnapshot, CorrelationConfidence

    # Build correlated_snapshots from changes (assume HIGH confidence for tests)
    correlated = [
        CorrelatedSnapshot(
            snapshot=snap,
            confidence=CorrelationConfidence.HIGH,
            note="test fixture: assumed HIGH confidence"
        )
        for snap in (changes or [])
    ]

    return StepAnalysis(
        step_id=step_id,
        action_target=target,
        action_timestamp=NOW,
        replay_status=None,
        replay_message="ok",
        triggered_layers=layers or [],
        data_changes=changes or [],
        correlated_snapshots=correlated,
        failure_layer=failure,
        failure_reason="validation: Amount required" if failure else None,
    )


# ==============================================================================
# PROPERTY 1: No fabrication
# ==============================================================================


def test_data_delta_evidence_requires_actual_snapshot() -> None:
    """An entity may only be marked data-delta if a snapshot showed that field changing."""
    actions = [_action("s1", 1, ActionType.INPUT, "input:Status")]
    # Analysis with NO data_changes — UI action only
    analyses = [_analysis("s1", "input:Status")]

    spec = build_agent_spec(actions, analyses)

    # Should have one entity from the UI action
    assert len(spec.entities) == 1
    ent = spec.entities[0]
    assert ent.name == "status"

    # Must NOT have data-delta evidence since no snapshot was observed
    sources = {ev.source for ev in ent.evidence}
    assert "data-delta" not in sources, "data-delta evidence was fabricated without a snapshot"
    assert "ui-action" in sources, "should have ui-action evidence from the input"


def test_ui_action_evidence_only_for_actual_actions() -> None:
    """ui-action evidence can only appear if an INPUT or SELECT action touched that field."""
    # Snapshot shows Status changed, but no UI action captured it
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]  # Only submit, no input
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    status_ent = next((e for e in spec.entities if e.field_api_name == "Status"), None)
    assert status_ent is not None

    # Should have data-delta from snapshot but NOT ui-action
    sources = {ev.source for ev in status_ent.evidence}
    assert "data-delta" in sources
    assert "ui-action" not in sources, "ui-action evidence fabricated without a captured input"


def test_both_ui_and_data_evidence_when_both_observed() -> None:
    """When we see BOTH a UI input AND a data delta, entity gets both evidence types."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [
        _action("s1", 1, ActionType.SELECT, "picklist:Status"),
        _action("s2", 2, ActionType.SUBMIT, "button:Save"),
    ]
    analyses = [
        _analysis("s1", "picklist:Status"),  # UI interaction
        _analysis("s2", "button:Save", changes=[snap]),  # Data persisted
    ]

    spec = build_agent_spec(actions, analyses)

    status_ent = next((e for e in spec.entities if e.field_api_name == "Status"), None)
    assert status_ent is not None

    sources = {ev.source for ev in status_ent.evidence}
    assert "ui-action" in sources, "should have ui-action from the SELECT"
    assert "data-delta" in sources, "should have data-delta from the snapshot"


# ==============================================================================
# PROPERTY 2: recordId is builder-mandated inference
# ==============================================================================


def test_recordid_is_always_inference_grounded() -> None:
    """Invariant G2: recordId entity has field_api_name='Id' and evidence source 'inference'."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    record_id = next((e for e in spec.entities if e.field_api_name == "Id"), None)
    assert record_id is not None, "recordId must be emitted when an object was observed"
    assert record_id.name == "recordId"

    sources = [ev.source for ev in record_id.evidence]
    assert sources == ["inference"], f"recordId must be inference-grounded, got {sources}"


def test_no_recordid_when_no_object_observed() -> None:
    """If no data change was observed, no object is known, so no recordId should be emitted."""
    actions = [_action("s1", 1, ActionType.CLICK, "button:Save", obj=None)]
    analyses = [_analysis("s1", "button:Save")]

    spec = build_agent_spec(actions, analyses)

    record_id = next((e for e in spec.entities if e.field_api_name == "Id"), None)
    assert record_id is None, "recordId should not exist when no object was observed"


# ==============================================================================
# PROPERTY 3: Guardrails are always emitted
# ==============================================================================


def test_guardrails_always_present_for_real_recording() -> None:
    """Guardrails must always be emitted for any recording with observed data."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    assert len(spec.guardrails) > 0, "guardrails list must not be empty"


def test_guardrails_name_actual_objects_and_fields() -> None:
    """Guardrails must name the actual objects/fields observed, not generic placeholders."""
    snap = _snapshot("s1", "Opportunity", {"Amount": 100}, {"Amount": 500}, ["Amount"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save", obj="Opportunity")]
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    guardrail_text = " ".join(spec.guardrails).lower()
    assert "opportunity" in guardrail_text, "guardrails should name the observed object"
    assert "amount" in guardrail_text, "guardrails should name the observed field"


def test_validation_layer_adds_validation_guardrail() -> None:
    """When validation layer triggers, a validation-specific guardrail must appear."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    analyses = [_analysis("s1", "button:Save", layers=[TelemetryLayer.VALIDATION], changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    assert any("validation" in g.lower() for g in spec.guardrails)


# ==============================================================================
# PROPERTY 4: Failure handling must be observed, not asserted
# ==============================================================================


def test_observed_failure_uses_correct_format() -> None:
    """Observed failures must use the stable format that scorer detects."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    analyses = [
        _analysis(
            "s1",
            "button:Save",
            layers=[TelemetryLayer.VALIDATION],
            changes=[snap],
            failure=FailureLayer.VALIDATION,
        )
    ]

    spec = build_agent_spec(actions, analyses)

    # Must match the stable fragment scorer expects
    observed = [f for f in spec.failure_handling if f.startswith("Observed ") and "failure during recording:" in f]
    assert len(observed) > 0, "observed failure must use the stable format"


def test_untested_sentinel_appears_when_no_failure_observed() -> None:
    """When no failure was observed, explicit UNTESTED sentinel must appear."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    assert any("UNTESTED" in f for f in spec.failure_handling)


def test_boilerplate_observed_never_emitted_without_real_failure() -> None:
    """Must never emit 'Observed X failure' unless a failure genuinely appeared in telemetry."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    # NO failure_layer set
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    observed = [f for f in spec.failure_handling if f.startswith("Observed ") and "failure during recording:" in f]
    assert len(observed) == 0, "must not emit 'Observed X failure' when no failure was recorded"


# ==============================================================================
# PROPERTY 5: Unknowns must be declared, not hidden
# ==============================================================================


def test_no_telemetry_declares_unknown() -> None:
    """When no backend telemetry was correlated, an explicit unknown must appear."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    # NO triggered_layers
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    assert any("no backend telemetry" in u.lower() for u in spec.unknowns)


def test_no_object_declares_unknown() -> None:
    """When no data change was observed, unknown must say target object is unresolved."""
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save", obj=None)]
    analyses = [_analysis("s1", "button:Save")]

    spec = build_agent_spec(actions, analyses)

    assert any("target object is unknown" in u.lower() for u in spec.unknowns)


def test_no_entities_declares_unknown() -> None:
    """When no input entities could be derived, an explicit unknown must appear."""
    actions = [_action("s1", 1, ActionType.CLICK, "button:View", obj=None)]  # Not a write action
    analyses = [_analysis("s1", "button:View")]

    spec = build_agent_spec(actions, analyses)

    assert any("no input entities" in u.lower() for u in spec.unknowns)


# ==============================================================================
# PROPERTY 6: Confidence must be earned and must degrade
# ==============================================================================


def test_confidence_degrades_with_no_telemetry() -> None:
    """Confidence should be lower when no backend telemetry was observed."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]

    spec_with_telemetry = build_agent_spec(
        actions,
        [_analysis("s1", "button:Save", layers=[TelemetryLayer.FLOW], changes=[snap])],
    )
    spec_without_telemetry = build_agent_spec(
        actions,
        [_analysis("s1", "button:Save", changes=[snap])],
    )

    # Same data observed, but no telemetry should not increase confidence
    assert spec_without_telemetry.confidence <= spec_with_telemetry.confidence


def test_confidence_degrades_with_no_data_changes() -> None:
    """Confidence drops when no data changes were observed."""
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]

    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    spec_with_data = build_agent_spec(actions, [_analysis("s1", "button:Save", changes=[snap])])
    spec_without_data = build_agent_spec(actions, [_analysis("s1", "button:Save")])

    assert spec_without_data.confidence < spec_with_data.confidence


def test_confidence_degrades_with_ambiguous_intent() -> None:
    """Confidence drops when intent cannot be resolved (no writes, no data)."""
    actions = [_action("s1", 1, ActionType.CLICK, "button:View", obj=None)]
    analyses = [_analysis("s1", "button:View")]

    spec = build_agent_spec(actions, analyses)

    assert spec.confidence < 0.5, "ambiguous intent should have low confidence"
    assert "UNRESOLVED" in spec.intent


def test_confidence_earned_with_full_evidence() -> None:
    """Confidence should be reasonably high when full evidence is present."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    analyses = [_analysis("s1", "button:Save", layers=[TelemetryLayer.FLOW], changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    assert spec.confidence >= 0.5, "full evidence should yield reasonable confidence"


# ==============================================================================
# PROPERTY 7: Degenerate inputs
# ==============================================================================


def test_zero_actions_does_not_crash() -> None:
    """Builder must handle zero actions gracefully."""
    spec = build_agent_spec([], [])

    assert spec.confidence < 0.5
    assert "UNRESOLVED" in spec.intent
    assert len(spec.unknowns) > 0


def test_one_action_with_no_data_declares_unknowns() -> None:
    """One action with no data should be honest about what's missing."""
    actions = [_action("s1", 1, ActionType.CLICK, "button:Save", obj=None)]
    analyses = [_analysis("s1", "button:Save")]

    spec = build_agent_spec(actions, analyses)

    assert len(spec.unknowns) > 0
    assert spec.confidence < 0.5


def test_duplicate_field_writes_produce_single_entity() -> None:
    """Same field written twice in different steps should produce one entity, not two."""
    snap1 = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    snap2 = _snapshot("s2", "Case", {"Status": "Working"}, {"Status": "Escalated"}, ["Status"])

    actions = [
        _action("s1", 1, ActionType.SELECT, "picklist:Status"),
        _action("s2", 2, ActionType.SELECT, "picklist:Status"),
    ]
    analyses = [
        _analysis("s1", "picklist:Status", changes=[snap1]),
        _analysis("s2", "picklist:Status", changes=[snap2]),
    ]

    spec = build_agent_spec(actions, analyses)

    entity_names = [e.name for e in spec.entities]
    status_count = entity_names.count("status")
    assert status_count == 1, f"duplicate 'status' entities found: {entity_names}"


def test_non_ascii_field_labels() -> None:
    """Non-ASCII field labels should be handled gracefully."""
    snap = _snapshot("s1", "Case", {"Descripción": "test"}, {"Descripción": "prueba"}, ["Descripción"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    # Should not crash and should derive an entity
    assert len(spec.entities) >= 1  # At least the non-ASCII field entity (plus recordId)


def test_very_long_field_name() -> None:
    """Very long field names should not break entity derivation."""
    long_name = "A" * 200 + "__c"
    snap = _snapshot("s1", "Case", {long_name: "old"}, {long_name: "new"}, [long_name])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    # Should derive an entity without crashing
    field_names = {e.field_api_name for e in spec.entities}
    assert long_name in field_names


# ==============================================================================
# PROPERTY 8: No duplicate entities
# ==============================================================================


def test_no_duplicate_entities_regression() -> None:
    """Regression: entity list must not contain duplicates.

    Previously verified working: ['status','priority','recordId','comments'] with no duplicates.
    This test ensures it stays that way.
    """
    snap = _snapshot(
        "s1",
        "Case",
        {"Status": "New", "Priority": "Low"},
        {"Status": "Working", "Priority": "High"},
        ["Status", "Priority"],
    )
    actions = [
        _action("s1", 1, ActionType.SELECT, "picklist:Status"),
        _action("s2", 2, ActionType.SELECT, "picklist:Priority"),
        _action("s3", 3, ActionType.SUBMIT, "button:Save"),
    ]
    analyses = [
        _analysis("s1", "picklist:Status"),
        _analysis("s2", "picklist:Priority"),
        _analysis("s3", "button:Save", changes=[snap]),
    ]

    spec = build_agent_spec(actions, analyses)

    entity_names = [e.name for e in spec.entities]
    unique_names = set(entity_names)

    assert len(entity_names) == len(unique_names), f"duplicate entities found: {entity_names}"


# ==============================================================================
# ROUND 2 FIXES: Correlation confidence propagation (DEFECT 1, 2, 3)
# ==============================================================================


def test_high_confidence_correlation_produces_data_delta_evidence() -> None:
    """DEFECT 1: HIGH correlation confidence should produce strong data-delta evidence."""
    from sf_video_blueprint.correlation import CorrelatedSnapshot, CorrelationConfidence

    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]

    # Create analysis with HIGH confidence correlation
    corr_snap = CorrelatedSnapshot(
        snapshot=snap,
        confidence=CorrelationConfidence.HIGH,
        note="within 5s window AND caller-asserted step_id matches"
    )
    analysis = StepAnalysis(
        step_id="s1",
        action_target="button:Save",
        action_timestamp=NOW,
        replay_status=None,
        replay_message="ok",
        triggered_layers=[],
        data_changes=[snap],
        correlated_snapshots=[corr_snap],
        failure_layer=None,
        failure_reason=None,
    )

    spec = build_agent_spec(actions, [analysis])

    status_ent = next((e for e in spec.entities if e.field_api_name == "Status"), None)
    assert status_ent is not None
    sources = {ev.source for ev in status_ent.evidence}
    assert "data-delta" in sources, "HIGH confidence should produce data-delta evidence"
    assert "inference" not in sources, "HIGH confidence should not produce inference evidence"


def test_temporal_confidence_correlation_produces_data_delta_with_note() -> None:
    """DEFECT 1: TEMPORAL correlation confidence should produce data-delta with temporal note."""
    from sf_video_blueprint.correlation import CorrelatedSnapshot, CorrelationConfidence

    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]

    # Create analysis with TEMPORAL confidence correlation
    corr_snap = CorrelatedSnapshot(
        snapshot=snap,
        confidence=CorrelationConfidence.TEMPORAL,
        note="within 5s window (caller asserted step_id='s2', ignored)"
    )
    analysis = StepAnalysis(
        step_id="s1",
        action_target="button:Save",
        action_timestamp=NOW,
        replay_status=None,
        replay_message="ok",
        triggered_layers=[],
        data_changes=[snap],
        correlated_snapshots=[corr_snap],
        failure_layer=None,
        failure_reason=None,
    )

    spec = build_agent_spec(actions, [analysis])

    status_ent = next((e for e in spec.entities if e.field_api_name == "Status"), None)
    assert status_ent is not None
    sources = [ev.source for ev in status_ent.evidence]
    assert "data-delta" in sources, "TEMPORAL confidence should still produce data-delta"
    # Check that the detail contains temporal correlation info
    details = [ev.detail for ev in status_ent.evidence if ev.source == "data-delta"]
    assert any("temporal correlation" in d.lower() for d in details)


def test_asserted_confidence_correlation_produces_inference_evidence() -> None:
    """DEFECT 1: ASSERTED correlation confidence (clock skew) should produce inference evidence."""
    from sf_video_blueprint.correlation import CorrelatedSnapshot, CorrelationConfidence

    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]

    # Create analysis with ASSERTED confidence correlation (clock skew)
    corr_snap = CorrelatedSnapshot(
        snapshot=snap,
        confidence=CorrelationConfidence.ASSERTED,
        note="caller-asserted match but 10.0s outside window (clock skew suspected)"
    )
    analysis = StepAnalysis(
        step_id="s1",
        action_target="button:Save",
        action_timestamp=NOW,
        replay_status=None,
        replay_message="ok",
        triggered_layers=[],
        data_changes=[snap],
        correlated_snapshots=[corr_snap],
        failure_layer=None,
        failure_reason=None,
    )

    spec = build_agent_spec(actions, [analysis])

    status_ent = next((e for e in spec.entities if e.field_api_name == "Status"), None)
    assert status_ent is not None
    sources = {ev.source for ev in status_ent.evidence}
    assert "inference" in sources, "ASSERTED confidence should produce inference evidence (clock skew)"
    assert "data-delta" not in sources, "ASSERTED confidence should NOT produce data-delta evidence"
    # Check detail mentions clock skew
    details = [ev.detail for ev in status_ent.evidence if ev.source == "inference"]
    assert any("clock skew" in d.lower() for d in details)


def test_ambiguous_confidence_correlation_produces_inference_and_unknown() -> None:
    """DEFECT 1: AMBIGUOUS correlation should produce inference evidence AND record in unknowns."""
    from sf_video_blueprint.correlation import CorrelatedSnapshot, CorrelationConfidence

    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]

    # Create analysis with AMBIGUOUS confidence correlation
    corr_snap = CorrelatedSnapshot(
        snapshot=snap,
        confidence=CorrelationConfidence.AMBIGUOUS,
        note="multiple validation events in window; chose closest (150ms)"
    )
    analysis = StepAnalysis(
        step_id="s1",
        action_target="button:Save",
        action_timestamp=NOW,
        replay_status=None,
        replay_message="ok",
        triggered_layers=[],
        data_changes=[snap],
        correlated_snapshots=[corr_snap],
        failure_layer=None,
        failure_reason=None,
    )

    spec = build_agent_spec(actions, [analysis])

    # Check evidence is inference
    status_ent = next((e for e in spec.entities if e.field_api_name == "Status"), None)
    assert status_ent is not None
    sources = {ev.source for ev in status_ent.evidence}
    assert "inference" in sources, "AMBIGUOUS confidence should produce inference evidence"
    assert "data-delta" not in sources, "AMBIGUOUS confidence should NOT produce data-delta evidence"

    # Check detail mentions ambiguous
    details = [ev.detail for ev in status_ent.evidence if ev.source == "inference"]
    assert any("ambiguous" in d.lower() for d in details)

    # CRITICAL: Check that ambiguous correlation is recorded in unknowns
    assert any("ambiguous" in u.lower() for u in spec.unknowns), (
        "AMBIGUOUS correlation must be recorded in spec.unknowns so the operator knows provenance is uncertain"
    )
    assert any("Case.Status" in u for u in spec.unknowns), (
        "Unknown should name the ambiguous field"
    )


def test_extraction_warnings_thread_into_unknowns() -> None:
    """DEFECT 2: Extraction warnings (redaction) should propagate to spec.unknowns."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    # Simulate redaction warning from extractor
    warnings = [
        "Step 3: value redacted, requires manual input at replay",
        "Some other warning that doesn't indicate missing data"
    ]

    spec = build_agent_spec(actions, analyses, extraction_warnings=warnings)

    # The redaction warning should appear in unknowns
    assert any("redacted" in u.lower() for u in spec.unknowns), (
        "Redaction warnings must propagate to unknowns"
    )
    # SECURITY: The unknown must NOT echo the redacted value
    # (The warning itself doesn't contain the value, so this is safe, but verify the pattern)
    assert all("value redacted" in u.lower() or "value" not in u.lower() for u in spec.unknowns if "redacted" in u.lower())


def test_no_fabricated_failure_handling_without_observed_failure() -> None:
    """DEFECT 3b: Must never emit 'Observed X failure' without an actual failure in analyses."""
    snap = _snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    # NO failure_layer set
    analyses = [_analysis("s1", "button:Save", changes=[snap])]

    spec = build_agent_spec(actions, analyses)

    # Should only have the untested sentinel, not any "Observed X failure" statements
    observed_failures = [f for f in spec.failure_handling if f.startswith("Observed ") and "failure during recording:" in f]
    assert len(observed_failures) == 0, (
        f"Must not fabricate failure statements without observed failures. Found: {observed_failures}"
    )
    # Should have the untested sentinel
    assert any("UNTESTED" in f for f in spec.failure_handling)
