from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from .telemetry import CorrelationKey, ObjectSnapshot, TelemetryCollector, TelemetryEvent, TelemetryLayer


class SalesforceRestClient:
    def __init__(self, base_url: str, access_token: str, api_version: str = "v61.0") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    def query(self, soql: str) -> dict[str, Any]:
        url = f"{self.base_url}/services/data/{self.api_version}/query"
        response = self.session.get(url, params={"q": soql}, timeout=30)
        response.raise_for_status()
        return response.json()

    def tooling_query(self, soql: str) -> dict[str, Any]:
        url = f"{self.base_url}/services/data/{self.api_version}/tooling/query"
        response = self.session.get(url, params={"q": soql}, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_record(self, object_api_name: str, record_id: str) -> dict[str, Any]:
        url = f"{self.base_url}/services/data/{self.api_version}/sobjects/{object_api_name}/{record_id}"
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return response.json()


class SalesforceTelemetryCollector(TelemetryCollector):
    """
    Collector that pulls platform telemetry using REST/Tooling APIs.
    """

    def __init__(
        self,
        client: SalesforceRestClient,
        tracked_records: list[tuple[str, str]] | None = None,
    ) -> None:
        self.client = client
        self.tracked_records = tracked_records or []
        self._last_seen_records: dict[str, dict[str, Any]] = {}

    def collect_for_step(self, run_id: str, step_id: str) -> list[TelemetryEvent]:
        at = datetime.now(timezone.utc)
        key = CorrelationKey(run_id=run_id, step_id=step_id, event_time=at)
        events: list[TelemetryEvent] = []

        events.append(
            self._safe_query_event(
                key,
                layer=TelemetryLayer.APEX,
                event_name="ApexLogFetch",
                query_type="tooling",
                soql=(
                    "SELECT Id, Status, Operation, LogLength, StartTime "
                    "FROM ApexLog ORDER BY StartTime DESC LIMIT 5"
                ),
            )
        )

        events.append(
            self._safe_query_event(
                key,
                layer=TelemetryLayer.ASYNC,
                event_name="AsyncApexJobFetch",
                query_type="data",
                soql=(
                    "SELECT Id, Status, JobType, MethodName, CreatedDate "
                    "FROM AsyncApexJob ORDER BY CreatedDate DESC LIMIT 10"
                ),
            )
        )

        events.append(
            self._safe_query_event(
                key,
                layer=TelemetryLayer.FLOW,
                event_name="FlowInterviewFetch",
                query_type="data",
                soql=(
                    "SELECT Id, InterviewLabel, CurrentElement, PauseLabel, CreatedDate "
                    "FROM FlowInterview ORDER BY CreatedDate DESC LIMIT 10"
                ),
            )
        )

        events.append(
            self._safe_query_event(
                key,
                layer=TelemetryLayer.VALIDATION,
                event_name="ValidationRuleFetch",
                query_type="tooling",
                soql=(
                    "SELECT Id, ValidationName, Active, ErrorDisplayField, ErrorMessage "
                    "FROM ValidationRule WHERE Active = true LIMIT 100"
                ),
            )
        )

        events.extend(self._derive_failure_events_from_logs(key))
        return events

    def snapshot_changes(self, run_id: str, step_id: str) -> list[ObjectSnapshot]:
        at = datetime.now(timezone.utc)
        key = CorrelationKey(run_id=run_id, step_id=step_id, event_time=at)
        if not self.tracked_records:
            return [
                ObjectSnapshot(
                    correlation=key,
                    object_api_name="Unknown",
                    record_id="unknown",
                    before={},
                    after={},
                    changed_fields=[],
                )
            ]

        snapshots: list[ObjectSnapshot] = []
        for object_api_name, record_id in self.tracked_records:
            cache_key = f"{object_api_name}:{record_id}"
            before = self._last_seen_records.get(cache_key, {})
            try:
                after = self.client.get_record(object_api_name, record_id)
            except Exception:  # noqa: BLE001
                after = {}
            changed_fields = sorted(
                field_name
                for field_name, after_value in after.items()
                if before.get(field_name) != after_value
            )
            snapshots.append(
                ObjectSnapshot(
                    correlation=key,
                    object_api_name=object_api_name,
                    record_id=record_id,
                    before=before,
                    after=after,
                    changed_fields=changed_fields,
                )
            )
            if after:
                self._last_seen_records[cache_key] = after
        return snapshots

    def _safe_query_event(
        self,
        key: CorrelationKey,
        layer: TelemetryLayer,
        event_name: str,
        query_type: str,
        soql: str,
    ) -> TelemetryEvent:
        try:
            if query_type == "tooling":
                records = self.client.tooling_query(soql).get("records", [])
            else:
                records = self.client.query(soql).get("records", [])
            return TelemetryEvent(
                correlation=key,
                layer=layer,
                event_name=event_name,
                status="success",
                payload={"records": records, "soql": soql},
            )
        except Exception as exc:  # noqa: BLE001
            return TelemetryEvent(
                correlation=key,
                layer=layer,
                event_name=event_name,
                status="error",
                payload={"error": str(exc), "soql": soql},
            )

    def _derive_failure_events_from_logs(self, key: CorrelationKey) -> list[TelemetryEvent]:
        events: list[TelemetryEvent] = []
        try:
            logs = self.client.tooling_query(
                "SELECT Id, Status, Operation FROM ApexLog ORDER BY StartTime DESC LIMIT 10"
            ).get("records", [])
        except Exception as exc:  # noqa: BLE001
            events.append(
                TelemetryEvent(
                    correlation=key,
                    layer=TelemetryLayer.APEX,
                    event_name="ApexFailureSignal",
                    status="error",
                    payload={"error": str(exc)},
                )
            )
            return events

        failed = [item for item in logs if str(item.get("Status", "")).lower() not in {"success", "completed"}]
        if failed:
            events.append(
                TelemetryEvent(
                    correlation=key,
                    layer=TelemetryLayer.APEX,
                    event_name="ApexFailureSignal",
                    status="error",
                    payload={"failedLogs": failed},
                )
            )
        return events

