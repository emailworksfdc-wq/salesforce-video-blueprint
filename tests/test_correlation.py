"""Tests for timestamp-based correlation, replacing the tautological step_id join.

The defect: the original correlation joined on CorrelationKey.step_id, which the caller
assigned, so the join proved nothing about causality — it just re-read an assertion the
caller made. This was tautological.

The fix: temporal correlation with an explicit window (5s), treating caller-asserted
step_id as one signal among several, and surfacing ambiguity rather than silently picking.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sf_video_blueprint.correlation import (
    CORRELATION_WINDOW_SECONDS,
    CorrelationConfidence,
    correlate_all,
    correlate_step,
)
from sf_video_blueprint.models import ActionType, ExtractedAction, UIContext
from sf_video_blueprint.replay import ReplayEvent, ReplayStatus
from sf_video_blueprint.telemetry import (
    CorrelationKey,
    ObjectSnapshot,
    TelemetryEvent,
    TelemetryLayer,
)


@pytest.fixture
def base_time() -> datetime:
    """Fixed base timestamp for deterministic tests."""
    return datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def action(base_time: datetime) -> ExtractedAction:
    """A single action at T=0."""
    return ExtractedAction(
        step_id="step_1",
        sequence=1,
        timestamp_ms=int(base_time.timestamp() * 1000),
        action_type=ActionType.SUBMIT,
        target="button:Save",
        confidence=0.9,
    )


def test_clean_1to1_correlation_within_window(base_time: datetime, action: ExtractedAction) -> None:
    """One action, one telemetry event within the window, caller-asserted step_id matches."""
    tel_time = base_time + timedelta(seconds=1)
    tel = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="ValidationRule",
        status="success",
    )
    snap_time = base_time + timedelta(seconds=1.5)
    snap = ObjectSnapshot(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=snap_time),
        object_api_name="Case",
        record_id="500XX0001",
        before={"Status": "New"},
        after={"Status": "Working"},
        changed_fields=["Status"],
    )

    analysis = correlate_step(action, [], [tel], [snap])

    assert analysis.step_id == "step_1"
    assert len(analysis.correlated_events) == 1
    assert analysis.correlated_events[0].confidence == CorrelationConfidence.HIGH
    assert "within 5s window AND caller-asserted step_id matches" in analysis.correlated_events[0].note

    assert len(analysis.correlated_snapshots) == 1
    assert analysis.correlated_snapshots[0].confidence == CorrelationConfidence.HIGH

    # Backward compat
    assert analysis.triggered_layers == [TelemetryLayer.VALIDATION]
    assert analysis.data_changes == [snap]


def test_event_outside_window_not_correlated(base_time: datetime, action: ExtractedAction) -> None:
    """Event occurs 10 seconds after the action, outside the 5s window, not caller-asserted."""
    tel_time = base_time + timedelta(seconds=10)
    tel = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="other_step", event_time=tel_time),
        layer=TelemetryLayer.DATA,
        event_name="Insert",
        status="success",
    )

    analysis = correlate_step(action, [], [tel], [])

    assert len(analysis.correlated_events) == 0
    assert analysis.triggered_layers == []


def test_two_candidate_actions_for_one_event_ambiguous(base_time: datetime) -> None:
    """Two actions within a short interval, one event in the window.

    Event at T+3s is within the window of action_1 (T+0, window ends T+5),
    but is also within the window of action_2 (T+2, window ends T+7).
    Since correlation is per-step, action_1 correlates it (TEMPORAL because
    step_id doesn't match), and action_2 correlates it (HIGH because step_id matches).
    """
    action_1 = ExtractedAction(
        step_id="step_1",
        sequence=1,
        timestamp_ms=int(base_time.timestamp() * 1000),
        action_type=ActionType.SUBMIT,
        target="button:Save",
        confidence=0.9,
    )
    action_2 = ExtractedAction(
        step_id="step_2",
        sequence=2,
        timestamp_ms=int((base_time + timedelta(seconds=2)).timestamp() * 1000),
        action_type=ActionType.SUBMIT,
        target="button:Submit",
        confidence=0.9,
    )
    # Event at T+3s — within window of BOTH actions
    tel_time = base_time + timedelta(seconds=3)
    tel = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_2", event_time=tel_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="ValidationRule",
        status="success",
    )

    a1 = correlate_step(action_1, [], [tel], [])
    a2 = correlate_step(action_2, [], [tel], [])

    # action_1: event is within its window but step_id doesn't match
    assert len(a1.correlated_events) == 1
    assert a1.correlated_events[0].confidence == CorrelationConfidence.TEMPORAL

    # action_2: event is within its window and caller-asserted
    assert len(a2.correlated_events) == 1
    assert a2.correlated_events[0].confidence == CorrelationConfidence.HIGH


def test_one_action_two_candidate_events_ambiguous(base_time: datetime, action: ExtractedAction) -> None:
    """One action, two validation events within the window — mark as ambiguous and choose closest."""
    tel1_time = base_time + timedelta(seconds=0.5)
    tel1 = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel1_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="ValidationRuleA",
        status="success",
    )
    tel2_time = base_time + timedelta(seconds=2)
    tel2 = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel2_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="ValidationRuleB",
        status="success",
    )

    analysis = correlate_step(action, [], [tel1, tel2], [])

    # Should correlate the closest one and mark it ambiguous
    assert len(analysis.correlated_events) == 1
    assert analysis.correlated_events[0].confidence == CorrelationConfidence.AMBIGUOUS
    assert "multiple validation events in window" in analysis.correlated_events[0].note
    assert analysis.correlated_events[0].event == tel1  # closest


def test_caller_asserted_key_contradicts_timestamps(base_time: datetime, action: ExtractedAction) -> None:
    """Caller says step_id matches but event is outside the window — clock skew suspected."""
    tel_time = base_time + timedelta(seconds=10)  # outside 5s window
    tel = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="ValidationRule",
        status="success",
    )

    analysis = correlate_step(action, [], [tel], [])

    assert len(analysis.correlated_events) == 1
    assert analysis.correlated_events[0].confidence == CorrelationConfidence.ASSERTED
    assert "outside window (clock skew suspected)" in analysis.correlated_events[0].note


def test_temporal_match_overrides_wrong_caller_assertion(base_time: datetime, action: ExtractedAction) -> None:
    """Event is within window but caller said step_id=other — timestamp wins."""
    tel_time = base_time + timedelta(seconds=1)
    tel = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="other_step", event_time=tel_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="ValidationRule",
        status="success",
    )

    analysis = correlate_step(action, [], [tel], [])

    assert len(analysis.correlated_events) == 1
    assert analysis.correlated_events[0].confidence == CorrelationConfidence.TEMPORAL
    assert "within 5s window" in analysis.correlated_events[0].note
    assert "caller asserted step_id='other_step', ignored" in analysis.correlated_events[0].note


def test_clock_skew_larger_than_window(base_time: datetime, action: ExtractedAction) -> None:
    """Event timestamp is 20s in the past (org clock ahead of browser) — should degrade."""
    tel_time = base_time - timedelta(seconds=20)
    tel = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="ValidationRule",
        status="success",
    )

    analysis = correlate_step(action, [], [tel], [])

    # Not in window, but caller-asserted — ASSERTED
    assert len(analysis.correlated_events) == 1
    assert analysis.correlated_events[0].confidence == CorrelationConfidence.ASSERTED


def test_empty_telemetry_no_crash(base_time: datetime, action: ExtractedAction) -> None:
    """No telemetry or snapshots — should return empty correlated lists."""
    analysis = correlate_step(action, [], [], [])

    assert analysis.step_id == "step_1"
    assert analysis.correlated_events == []
    assert analysis.correlated_snapshots == []
    assert analysis.triggered_layers == []
    assert analysis.data_changes == []


def test_determinism_same_input_identical_output(base_time: datetime, action: ExtractedAction) -> None:
    """Run correlation twice with identical inputs — output must match exactly."""
    tel_time = base_time + timedelta(seconds=1)
    tel = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="ValidationRule",
        status="success",
    )
    snap_time = base_time + timedelta(seconds=1.5)
    snap = ObjectSnapshot(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=snap_time),
        object_api_name="Case",
        record_id="500XX0001",
        before={"Status": "New"},
        after={"Status": "Working"},
        changed_fields=["Status"],
    )

    a1 = correlate_step(action, [], [tel], [snap])
    a2 = correlate_step(action, [], [tel], [snap])

    assert a1.step_id == a2.step_id
    assert len(a1.correlated_events) == len(a2.correlated_events)
    assert a1.correlated_events[0].confidence == a2.correlated_events[0].confidence
    assert a1.correlated_events[0].note == a2.correlated_events[0].note
    assert len(a1.correlated_snapshots) == len(a2.correlated_snapshots)
    assert a1.correlated_snapshots[0].confidence == a2.correlated_snapshots[0].confidence


def test_correlate_all_preserves_order(base_time: datetime) -> None:
    """correlate_all must process actions in sequence order, deterministically."""
    actions = [
        ExtractedAction(
            step_id=f"step_{i}",
            sequence=i,
            timestamp_ms=int((base_time + timedelta(seconds=i)).timestamp() * 1000),
            action_type=ActionType.CLICK,
            target=f"button:Step{i}",
            confidence=0.9,
        )
        for i in [3, 1, 2]  # out of order
    ]
    tel = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_2", event_time=base_time + timedelta(seconds=2.5)),
        layer=TelemetryLayer.VALIDATION,
        event_name="V",
        status="success",
    )

    analyses = correlate_all(actions, [], [tel], [])

    # Must be sorted by sequence
    assert [a.step_id for a in analyses] == ["step_1", "step_2", "step_3"]
    # step_2 should have the telemetry event
    assert len(analyses[1].correlated_events) == 1


def test_multiple_snapshots_same_object_ambiguous(base_time: datetime, action: ExtractedAction) -> None:
    """Two snapshots of the same object+record in the window — choose closest, mark ambiguous."""
    snap1_time = base_time + timedelta(seconds=0.5)
    snap1 = ObjectSnapshot(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=snap1_time),
        object_api_name="Case",
        record_id="500XX0001",
        before={"Status": "New"},
        after={"Status": "In Progress"},
        changed_fields=["Status"],
    )
    snap2_time = base_time + timedelta(seconds=2)
    snap2 = ObjectSnapshot(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=snap2_time),
        object_api_name="Case",
        record_id="500XX0001",
        before={"Status": "In Progress"},
        after={"Status": "Working"},
        changed_fields=["Status"],
    )

    analysis = correlate_step(action, [], [], [snap1, snap2])

    assert len(analysis.correlated_snapshots) == 1
    assert analysis.correlated_snapshots[0].confidence == CorrelationConfidence.AMBIGUOUS
    assert "multiple snapshots" in analysis.correlated_snapshots[0].note
    assert analysis.correlated_snapshots[0].snapshot == snap1  # closest


def test_confidence_ordering_in_output(base_time: datetime, action: ExtractedAction) -> None:
    """Events should be ordered by confidence: HIGH > TEMPORAL > ASSERTED > AMBIGUOUS."""
    # Create events with different confidences
    # HIGH: in window, caller match
    tel_high_time = base_time + timedelta(seconds=1)
    tel_high = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel_high_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="High",
        status="success",
    )
    # ASSERTED: out of window, caller match
    tel_asserted_time = base_time + timedelta(seconds=10)
    tel_asserted = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel_asserted_time),
        layer=TelemetryLayer.DATA,
        event_name="Asserted",
        status="success",
    )
    # TEMPORAL: in window, caller mismatch
    tel_temporal_time = base_time + timedelta(seconds=2)
    tel_temporal = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="other", event_time=tel_temporal_time),
        layer=TelemetryLayer.APEX,
        event_name="Temporal",
        status="success",
    )

    # Pass in scrambled order
    analysis = correlate_step(action, [], [tel_asserted, tel_temporal, tel_high], [])

    # Should be sorted HIGH, TEMPORAL, ASSERTED
    assert len(analysis.correlated_events) == 3
    assert analysis.correlated_events[0].confidence == CorrelationConfidence.HIGH
    assert analysis.correlated_events[1].confidence == CorrelationConfidence.TEMPORAL
    assert analysis.correlated_events[2].confidence == CorrelationConfidence.ASSERTED


def test_replay_events_use_step_id_not_tautological(base_time: datetime, action: ExtractedAction) -> None:
    """Replay events are NOT server-side evidence — they ARE the action, so step_id join is correct."""
    replay = ReplayEvent(
        run_id="r1",
        step_id="step_1",
        attempted_at=base_time,
        status=ReplayStatus.SUCCESS,
        attempt_no=1,
        duration_ms=250,
        message="replayed successfully",
    )

    analysis = correlate_step(action, [replay], [], [])

    assert analysis.replay_status == ReplayStatus.SUCCESS
    assert analysis.replay_message == "replayed successfully"


def test_telemetry_at_exactly_window_boundary(base_time: datetime, action: ExtractedAction) -> None:
    """Event at T+5s (exactly at window_end) should be included (<=, not <)."""
    tel_time = base_time + timedelta(seconds=CORRELATION_WINDOW_SECONDS)
    tel = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="EdgeCase",
        status="success",
    )

    analysis = correlate_step(action, [], [tel], [])

    assert len(analysis.correlated_events) == 1
    assert analysis.correlated_events[0].confidence == CorrelationConfidence.HIGH


def test_telemetry_before_action_not_correlated(base_time: datetime, action: ExtractedAction) -> None:
    """Event 1 second BEFORE the action should not correlate."""
    tel_time = base_time - timedelta(seconds=1)
    tel = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="BeforeAction",
        status="success",
    )

    analysis = correlate_step(action, [], [tel], [])

    # Not in window [action_time, action_time + 5s], but caller-asserted
    assert len(analysis.correlated_events) == 1
    assert analysis.correlated_events[0].confidence == CorrelationConfidence.ASSERTED


def test_different_layers_in_window_all_correlated(base_time: datetime, action: ExtractedAction) -> None:
    """Multiple events of different layers within the window — all should correlate, no ambiguity."""
    tel1_time = base_time + timedelta(seconds=1)
    tel1 = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel1_time),
        layer=TelemetryLayer.VALIDATION,
        event_name="V",
        status="success",
    )
    tel2_time = base_time + timedelta(seconds=2)
    tel2 = TelemetryEvent(
        correlation=CorrelationKey(run_id="r1", step_id="step_1", event_time=tel2_time),
        layer=TelemetryLayer.FLOW,
        event_name="F",
        status="success",
    )

    analysis = correlate_step(action, [], [tel1, tel2], [])

    assert len(analysis.correlated_events) == 2
    assert all(ce.confidence == CorrelationConfidence.HIGH for ce in analysis.correlated_events)
    assert {ce.event.layer for ce in analysis.correlated_events} == {TelemetryLayer.VALIDATION, TelemetryLayer.FLOW}
