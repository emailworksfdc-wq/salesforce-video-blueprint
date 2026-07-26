from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import re
from typing import Iterable

from .models import ExtractedAction
from .replay import ReplayEvent, ReplayStatus
from .telemetry import ObjectSnapshot, TelemetryEvent, TelemetryLayer


class FailureLayer(str, Enum):
    UI = "ui"
    VALIDATION = "validation"
    FLOW = "flow"
    APEX = "apex"
    INTEGRATION = "integration"
    DATA = "data"
    UNKNOWN = "unknown"


class CorrelationConfidence(str, Enum):
    """How certain we are that this event belongs to this action."""
    HIGH = "high"          # Within time window AND caller-asserted step_id agrees
    TEMPORAL = "temporal"  # Within time window, no caller assertion or contradicts it
    ASSERTED = "asserted"  # Caller-asserted step_id but outside time window (clock skew suspected)
    AMBIGUOUS = "ambiguous"  # Multiple candidates, chose closest but uncertain


# Time window for correlation: 5 seconds after action timestamp.
# Justification: browser -> org latency for a UI-triggered write is typically <500ms
# in a healthy dev sandbox. 5s allows for slow API calls, busy orgs, and network variance
# without conflating unrelated actions. Clock skew between browser (Date.now()) and org
# (server timestamp) can violate this window, which is why we preserve caller-asserted
# step_id as a fallback signal and mark it ASSERTED when it contradicts the window.
CORRELATION_WINDOW_SECONDS = 5


@dataclass(slots=True)
class CorrelatedTelemetryEvent:
    """A telemetry event matched to a step, with confidence and rationale."""
    event: TelemetryEvent
    confidence: CorrelationConfidence
    note: str  # Why this correlation was made


@dataclass(slots=True)
class CorrelatedSnapshot:
    """A data snapshot matched to a step, with confidence and rationale."""
    snapshot: ObjectSnapshot
    confidence: CorrelationConfidence
    note: str


@dataclass(slots=True)
class StepAnalysis:
    step_id: str
    action_target: str
    replay_status: ReplayStatus | None
    replay_message: str | None
    # Action timestamp for correlation window calculations. If None (tests/legacy callers),
    # correlation operates in backward-compat mode (step_id join only, for tests).
    action_timestamp: datetime | None = None
    triggered_layers: list[TelemetryLayer] = field(default_factory=list)
    data_changes: list[ObjectSnapshot] = field(default_factory=list)
    correlated_events: list[CorrelatedTelemetryEvent] = field(default_factory=list)
    correlated_snapshots: list[CorrelatedSnapshot] = field(default_factory=list)
    failure_layer: FailureLayer | None = None
    failure_reason: str | None = None
    screenshot_path: str | None = None
    network_trace_path: str | None = None


def correlate_step(
    step: ExtractedAction,
    replay_events: Iterable[ReplayEvent],
    telemetry_events: Iterable[TelemetryEvent],
    snapshots: Iterable[ObjectSnapshot],
) -> StepAnalysis:
    """Correlate telemetry to an action via timestamp window + caller assertion.

    Non-tautological correlation: an action at time T is matched to telemetry events
    within [T, T+CORRELATION_WINDOW_SECONDS] based on timestamps. The caller-asserted
    step_id in CorrelationKey is treated as ONE signal among several, not the only one.
    When temporal evidence and caller assertion disagree, we surface the disagreement
    rather than silently preferring one.
    """
    action_time = datetime.fromtimestamp(step.timestamp_ms / 1000.0, tz=timezone.utc)
    window_end = action_time + timedelta(seconds=CORRELATION_WINDOW_SECONDS)

    # Replay events still use step_id because they ARE the action being replayed,
    # not server-side evidence — so this is not tautological for replays.
    replay_for_step = [event for event in replay_events if event.step_id == step.step_id]

    # Timestamp-based correlation for telemetry
    corr_events = _correlate_telemetry(step, action_time, window_end, list(telemetry_events))
    corr_snapshots = _correlate_snapshots(step, action_time, window_end, list(snapshots))

    # Backward compatibility: populate triggered_layers and data_changes from correlated items
    telemetry_for_step = [ce.event for ce in corr_events]
    snapshots_for_step = [cs.snapshot for cs in corr_snapshots]

    latest_replay = replay_for_step[-1] if replay_for_step else None
    layers = sorted({item.layer for item in telemetry_for_step}, key=lambda layer: layer.value)

    failure_layer: FailureLayer | None = None
    failure_reason: str | None = None
    if latest_replay and latest_replay.status in {ReplayStatus.FAILED, ReplayStatus.RETRIED}:
        failure_layer, failure_reason = _classify_failure(latest_replay, telemetry_for_step)
    screenshot_path, network_trace_path = _extract_artifact_paths(latest_replay.message if latest_replay else None)

    return StepAnalysis(
        step_id=step.step_id,
        action_target=step.target,
        action_timestamp=action_time,
        replay_status=latest_replay.status if latest_replay else None,
        replay_message=latest_replay.message if latest_replay else None,
        triggered_layers=layers,
        data_changes=snapshots_for_step,
        correlated_events=corr_events,
        correlated_snapshots=corr_snapshots,
        failure_layer=failure_layer,
        failure_reason=failure_reason,
        screenshot_path=screenshot_path,
        network_trace_path=network_trace_path,
    )


def _correlate_telemetry(
    step: ExtractedAction,
    action_time: datetime,
    window_end: datetime,
    all_events: list[TelemetryEvent],
) -> list[CorrelatedTelemetryEvent]:
    """Match telemetry events to this action via timestamp window.

    Returns events sorted by confidence (HIGH > TEMPORAL > ASSERTED > AMBIGUOUS),
    then by event_time within each confidence tier.
    """
    candidates: list[tuple[TelemetryEvent, CorrelationConfidence, str]] = []

    for event in all_events:
        caller_match = event.correlation.step_id == step.step_id
        in_window = action_time <= event.correlation.event_time <= window_end

        if in_window and caller_match:
            conf = CorrelationConfidence.HIGH
            note = f"within {CORRELATION_WINDOW_SECONDS}s window AND caller-asserted step_id matches"
        elif in_window and not caller_match:
            conf = CorrelationConfidence.TEMPORAL
            note = f"within {CORRELATION_WINDOW_SECONDS}s window (caller asserted step_id={event.correlation.step_id!r}, ignored)"
        elif not in_window and caller_match:
            delta_s = (event.correlation.event_time - action_time).total_seconds()
            conf = CorrelationConfidence.ASSERTED
            note = f"caller-asserted match but {delta_s:.1f}s outside window (clock skew suspected)"
        else:
            continue  # Neither in window nor caller-asserted — not a candidate

        candidates.append((event, conf, note))

    # Detect ambiguity: multiple events of the same layer within the window
    if candidates:
        by_layer: dict[TelemetryLayer, list[tuple[TelemetryEvent, CorrelationConfidence, str]]] = {}
        for event, conf, note in candidates:
            by_layer.setdefault(event.layer, []).append((event, conf, note))

        for layer, items in by_layer.items():
            in_window_count = sum(1 for _, conf, _ in items if conf in {CorrelationConfidence.HIGH, CorrelationConfidence.TEMPORAL})
            if in_window_count > 1:
                # Ambiguous: mark the closest one as chosen, flag the rest
                sorted_items = sorted(items, key=lambda x: abs((x[0].correlation.event_time - action_time).total_seconds()))
                chosen_event, chosen_conf, _ = sorted_items[0]
                delta_ms = int(abs((chosen_event.correlation.event_time - action_time).total_seconds() * 1000))
                for i, (event, conf, _) in enumerate(sorted_items):
                    if i == 0:
                        items[i] = (event, CorrelationConfidence.AMBIGUOUS, f"multiple {layer.value} events in window; chose closest ({delta_ms}ms)")
                    else:
                        # Remove the non-chosen candidates
                        by_layer[layer].remove((event, conf, _))

        # Flatten back
        candidates = [item for items in by_layer.values() for item in items]

    # Confidence ordering for stable output
    conf_order = {
        CorrelationConfidence.HIGH: 0,
        CorrelationConfidence.TEMPORAL: 1,
        CorrelationConfidence.ASSERTED: 2,
        CorrelationConfidence.AMBIGUOUS: 3,
    }
    candidates.sort(key=lambda x: (conf_order[x[1]], x[0].correlation.event_time))

    return [CorrelatedTelemetryEvent(event, conf, note) for event, conf, note in candidates]


def _correlate_snapshots(
    step: ExtractedAction,
    action_time: datetime,
    window_end: datetime,
    all_snapshots: list[ObjectSnapshot],
) -> list[CorrelatedSnapshot]:
    """Match data snapshots to this action via timestamp window.

    Same logic as telemetry correlation: timestamp window is the primary signal,
    caller-asserted step_id is preserved as a secondary/fallback signal.
    """
    candidates: list[tuple[ObjectSnapshot, CorrelationConfidence, str]] = []

    for snap in all_snapshots:
        caller_match = snap.correlation.step_id == step.step_id
        in_window = action_time <= snap.correlation.event_time <= window_end

        if in_window and caller_match:
            conf = CorrelationConfidence.HIGH
            note = f"within {CORRELATION_WINDOW_SECONDS}s window AND caller-asserted step_id matches"
        elif in_window and not caller_match:
            conf = CorrelationConfidence.TEMPORAL
            note = f"within {CORRELATION_WINDOW_SECONDS}s window (caller asserted step_id={snap.correlation.step_id!r}, ignored)"
        elif not in_window and caller_match:
            delta_s = (snap.correlation.event_time - action_time).total_seconds()
            conf = CorrelationConfidence.ASSERTED
            note = f"caller-asserted match but {delta_s:.1f}s outside window (clock skew suspected)"
        else:
            continue

        candidates.append((snap, conf, note))

    # Ambiguity detection: multiple snapshots of the same object+record in window
    if candidates:
        by_key: dict[tuple[str, str], list[tuple[ObjectSnapshot, CorrelationConfidence, str]]] = {}
        for snap, conf, note in candidates:
            key = (snap.object_api_name, snap.record_id)
            by_key.setdefault(key, []).append((snap, conf, note))

        for key, items in by_key.items():
            in_window_count = sum(1 for _, conf, _ in items if conf in {CorrelationConfidence.HIGH, CorrelationConfidence.TEMPORAL})
            if in_window_count > 1:
                sorted_items = sorted(items, key=lambda x: abs((x[0].correlation.event_time - action_time).total_seconds()))
                chosen_snap, chosen_conf, _ = sorted_items[0]
                delta_ms = int(abs((chosen_snap.correlation.event_time - action_time).total_seconds() * 1000))
                for i, (snap, conf, _) in enumerate(sorted_items):
                    if i == 0:
                        items[i] = (snap, CorrelationConfidence.AMBIGUOUS, f"multiple snapshots of {key[0]}:{key[1]} in window; chose closest ({delta_ms}ms)")
                    else:
                        by_key[key].remove((snap, conf, _))

        candidates = [item for items in by_key.values() for item in items]

    conf_order = {
        CorrelationConfidence.HIGH: 0,
        CorrelationConfidence.TEMPORAL: 1,
        CorrelationConfidence.ASSERTED: 2,
        CorrelationConfidence.AMBIGUOUS: 3,
    }
    candidates.sort(key=lambda x: (conf_order[x[1]], x[0].correlation.event_time))

    return [CorrelatedSnapshot(snap, conf, note) for snap, conf, note in candidates]


def correlate_all(
    steps: list[ExtractedAction],
    replay_events: list[ReplayEvent],
    telemetry_events: list[TelemetryEvent],
    snapshots: list[ObjectSnapshot],
) -> list[StepAnalysis]:
    return [
        correlate_step(step, replay_events, telemetry_events, snapshots)
        for step in sorted(steps, key=lambda item: item.sequence)
    ]


def _classify_failure(
    replay_event: ReplayEvent,
    telemetry: list[TelemetryEvent],
) -> tuple[FailureLayer, str]:
    error_code = replay_event.error_code or ""
    if error_code.startswith("UI_") or error_code in {"ELEMENT_NOT_FOUND", "TIMEOUT"}:
        return FailureLayer.UI, replay_event.message

    for event in telemetry:
        if event.layer == TelemetryLayer.VALIDATION and event.status.lower() == "error":
            return FailureLayer.VALIDATION, event.payload.get("message", replay_event.message)
        if event.layer == TelemetryLayer.FLOW and event.status.lower() == "error":
            return FailureLayer.FLOW, event.payload.get("fault", replay_event.message)
        if event.layer == TelemetryLayer.APEX and event.status.lower() == "error":
            return FailureLayer.APEX, event.payload.get("exception", replay_event.message)
        if event.layer in {TelemetryLayer.NETWORK, TelemetryLayer.INTEGRATION} and event.status.lower() == "error":
            return FailureLayer.INTEGRATION, event.payload.get("error", replay_event.message)

    return FailureLayer.UNKNOWN, replay_event.message


def _extract_artifact_paths(message: str | None) -> tuple[str | None, str | None]:
    if not message:
        return None, None
    screenshot_match = re.search(r"screenshot=([^;]+)", message)
    network_match = re.search(r"network_trace=([^;]+)", message)
    screenshot_path = screenshot_match.group(1).strip() if screenshot_match else None
    network_trace_path = network_match.group(1).strip() if network_match else None
    return screenshot_path, network_trace_path

