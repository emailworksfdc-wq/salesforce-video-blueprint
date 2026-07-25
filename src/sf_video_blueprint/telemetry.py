from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol


class TelemetryLayer(str, Enum):
    UI = "ui"
    NETWORK = "network"
    FLOW = "flow"
    APEX = "apex"
    VALIDATION = "validation"
    ASYNC = "async"
    DATA = "data"
    INTEGRATION = "integration"


@dataclass(slots=True)
class CorrelationKey:
    run_id: str
    step_id: str
    event_time: datetime


@dataclass(slots=True)
class TelemetryEvent:
    correlation: CorrelationKey
    layer: TelemetryLayer
    event_name: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ObjectSnapshot:
    correlation: CorrelationKey
    object_api_name: str
    record_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    changed_fields: list[str]


class TelemetryCollector(Protocol):
    def collect_for_step(self, run_id: str, step_id: str) -> list[TelemetryEvent]: ...
    def snapshot_changes(self, run_id: str, step_id: str) -> list[ObjectSnapshot]: ...


class TelemetryRegistry:
    """In-memory event contract store; replace with persistent backend in production."""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []
        self.snapshots: list[ObjectSnapshot] = []

    def collect_step(
        self,
        collector: TelemetryCollector,
        run_id: str,
        step_id: str,
    ) -> None:
        self.events.extend(collector.collect_for_step(run_id, step_id))
        self.snapshots.extend(collector.snapshot_changes(run_id, step_id))

    def append_manual_event(
        self,
        run_id: str,
        step_id: str,
        layer: TelemetryLayer,
        event_name: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            TelemetryEvent(
                correlation=CorrelationKey(
                    run_id=run_id,
                    step_id=step_id,
                    event_time=datetime.now(timezone.utc),
                ),
                layer=layer,
                event_name=event_name,
                status=status,
                payload=payload or {},
            )
        )

