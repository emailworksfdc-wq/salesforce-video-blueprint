from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(slots=True)
class StepAnalysis:
    step_id: str
    action_target: str
    replay_status: ReplayStatus | None
    replay_message: str | None
    triggered_layers: list[TelemetryLayer] = field(default_factory=list)
    data_changes: list[ObjectSnapshot] = field(default_factory=list)
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
    replay_for_step = [event for event in replay_events if event.step_id == step.step_id]
    telemetry_for_step = [event for event in telemetry_events if event.correlation.step_id == step.step_id]
    snapshots_for_step = [snap for snap in snapshots if snap.correlation.step_id == step.step_id]

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
        replay_status=latest_replay.status if latest_replay else None,
        replay_message=latest_replay.message if latest_replay else None,
        triggered_layers=layers,
        data_changes=snapshots_for_step,
        failure_layer=failure_layer,
        failure_reason=failure_reason,
        screenshot_path=screenshot_path,
        network_trace_path=network_trace_path,
    )


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

