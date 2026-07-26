from __future__ import annotations

from datetime import datetime, timezone

from sf_video_blueprint.correlation import FailureLayer, StepAnalysis, correlate_all
from sf_video_blueprint.models import ActionType, ExtractedAction, UIContext
from sf_video_blueprint.replay import ReplayEvent, ReplayStatus
from sf_video_blueprint.spec_builder import build_agent_spec
from sf_video_blueprint.telemetry import CorrelationKey, ObjectSnapshot, TelemetryEvent, TelemetryLayer

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _action(step_id: str, sequence: int, action_type: ActionType, target: str) -> ExtractedAction:
    return ExtractedAction(
        step_id=step_id,
        sequence=sequence,
        timestamp_ms=sequence * 1000,
        action_type=action_type,
        target=target,
        ui_context=UIContext(object_name="Opportunity"),
        confidence=0.9,
    )


def _snapshot(step_id: str, obj: str, before: dict, after: dict, changed: list[str]) -> ObjectSnapshot:
    return ObjectSnapshot(
        correlation=CorrelationKey(run_id="run-1", step_id=step_id, event_time=NOW),
        object_api_name=obj,
        record_id="006000000000001AAA",
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
        replay_status=ReplayStatus.SUCCESS,
        replay_message="ok",
        triggered_layers=layers or [],
        data_changes=changes or [],
        correlated_snapshots=correlated,
        failure_layer=failure,
        failure_reason="validation: Amount required" if failure else None,
    )


def test_spec_is_derived_from_observed_object_and_fields() -> None:
    """The whole point of the builder: different data -> different spec."""
    actions = [_action("s1", 1, ActionType.SELECT, "picklist:StageName")]
    analyses = [
        _analysis(
            "s1",
            "picklist:StageName",
            layers=[TelemetryLayer.FLOW],
            changes=[
                _snapshot(
                    "s1",
                    "Opportunity",
                    {"StageName": "Prospecting"},
                    {"StageName": "Closed Won"},
                    ["StageName"],
                )
            ],
        )
    ]

    spec = build_agent_spec(actions, analyses)

    assert spec.objects_touched == ["Opportunity"]
    assert "Opportunity" in spec.intent
    assert "StageName" in spec.intent
    # Must NOT be the old hardcoded Case literal.
    assert "Update case status from UI workflow" != spec.intent
    entity_fields = {e.field_api_name for e in spec.entities}
    assert "StageName" in entity_fields
    assert "Id" in entity_fields, "record identity is required to act on a record"
    assert spec.confidence >= 0.5


def test_different_recording_yields_different_spec() -> None:
    """Regression guard against reintroducing a hardcoded spec."""
    spec_a = build_agent_spec(
        [_action("s1", 1, ActionType.SUBMIT, "button:Save")],
        [_analysis("s1", "button:Save", changes=[_snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])])],
    )
    spec_b = build_agent_spec(
        [_action("s1", 1, ActionType.SUBMIT, "button:Save")],
        [_analysis("s1", "button:Save", changes=[_snapshot("s1", "Account", {"Rating": "Cold"}, {"Rating": "Hot"}, ["Rating"])])],
    )

    assert spec_a.intent != spec_b.intent
    assert spec_a.objects_touched != spec_b.objects_touched


def test_system_fields_are_not_treated_as_business_entities() -> None:
    analyses = [
        _analysis(
            "s1",
            "button:Save",
            changes=[
                _snapshot(
                    "s1",
                    "Case",
                    {"Status": "New"},
                    {"Status": "Working"},
                    ["Status", "LastModifiedDate", "SystemModstamp"],
                )
            ],
        )
    ]
    spec = build_agent_spec([_action("s1", 1, ActionType.SUBMIT, "button:Save")], analyses)
    fields = {e.field_api_name for e in spec.entities}
    assert "LastModifiedDate" not in fields
    assert "SystemModstamp" not in fields
    assert "Status" in fields


def test_no_observed_data_yields_unresolved_intent_not_invention() -> None:
    """An empty run must admit ignorance rather than fabricate a plausible spec."""
    spec = build_agent_spec([_action("s1", 1, ActionType.CLICK, "button:Save")], [_analysis("s1", "button:Save")])

    assert spec.intent.startswith("UNRESOLVED")
    assert spec.confidence < 0.5
    assert spec.objects_touched == []
    assert any("no record-level data change" in u.lower() for u in spec.unknowns)


def test_untested_error_paths_are_flagged_when_no_failure_observed() -> None:
    spec = build_agent_spec(
        [_action("s1", 1, ActionType.SUBMIT, "button:Save")],
        [_analysis("s1", "button:Save", changes=[_snapshot("s1", "Case", {}, {"Status": "New"}, ["Status"])])],
    )
    assert any("UNTESTED" in item for item in spec.failure_handling)


def test_observed_validation_failure_drives_guardrails_and_handling() -> None:
    analyses = [
        _analysis(
            "s1",
            "button:Save",
            layers=[TelemetryLayer.VALIDATION],
            changes=[_snapshot("s1", "Opportunity", {"Amount": None}, {"Amount": 100}, ["Amount"])],
            failure=FailureLayer.VALIDATION,
        )
    ]
    spec = build_agent_spec([_action("s1", 1, ActionType.SUBMIT, "button:Save")], analyses)

    assert any("validation" in g.lower() for g in spec.guardrails)
    assert any("validation" in f.lower() for f in spec.failure_handling)


def test_create_is_distinguished_from_update_by_empty_before_state() -> None:
    spec = build_agent_spec(
        [_action("s1", 1, ActionType.SUBMIT, "button:Save")],
        [_analysis("s1", "button:Save", changes=[_snapshot("s1", "Case", {}, {"Subject": "New case"}, ["Subject"])])],
    )
    assert spec.intent.startswith("Create")


def test_async_layer_adds_completion_guardrail() -> None:
    spec = build_agent_spec(
        [_action("s1", 1, ActionType.SUBMIT, "button:Save")],
        [
            _analysis(
                "s1",
                "button:Save",
                layers=[TelemetryLayer.ASYNC],
                changes=[_snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])],
            )
        ],
    )
    assert any("async" in g.lower() for g in spec.guardrails)


def test_spec_serializes_to_stable_dict() -> None:
    spec = build_agent_spec(
        [_action("s1", 1, ActionType.SUBMIT, "button:Save")],
        [_analysis("s1", "button:Save", changes=[_snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])])],
    )
    payload = spec.to_dict()
    assert payload["schema_version"] == "1.0.0"
    for key in ("intent", "confidence", "objects_touched", "entities", "orchestration_steps", "guardrails"):
        assert key in payload
    assert isinstance(payload["entities"], list)
    assert payload["entities"][0]["evidence"], "each entity must carry its evidence"


def test_correlation_pipeline_feeds_spec_builder() -> None:
    """End-to-end wiring: correlate_all output is consumable by build_agent_spec."""
    actions = [_action("s1", 1, ActionType.SUBMIT, "button:Save")]
    replay_events = [
        ReplayEvent(
            run_id="run-1",
            step_id="s1",
            attempted_at=NOW,
            status=ReplayStatus.SUCCESS,
            attempt_no=1,
            duration_ms=10,
            message="ok",
        )
    ]
    telemetry = [
        TelemetryEvent(
            correlation=CorrelationKey(run_id="run-1", step_id="s1", event_time=NOW),
            layer=TelemetryLayer.FLOW,
            event_name="FlowInterviewExecuted",
            status="success",
        )
    ]
    snapshots = [_snapshot("s1", "Case", {"Status": "New"}, {"Status": "Working"}, ["Status"])]

    analyses = correlate_all(actions, replay_events, telemetry, snapshots)
    spec = build_agent_spec(actions, analyses)

    assert spec.objects_touched == ["Case"]
    assert any("Flow" in step or "flow" in step for step in spec.orchestration_steps)
