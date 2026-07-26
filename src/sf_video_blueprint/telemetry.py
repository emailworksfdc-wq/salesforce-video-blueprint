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
    # Optional fields for EventLogFile correlation; must have safe defaults for backward compat
    org_timestamp: datetime | None = None  # Server-side timestamp from the org, not locally generated
    transaction_id: str | None = None  # REQUEST_ID from EventLogFile when available


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


# ============================================================================
# EventLogFile Collection — Salesforce Event Monitoring
# ============================================================================
#
# INTEGRATION POINT FOR telemetry_source CONTROL:
#
# The caller (cli.py or equivalent) MUST check EventLogFileResult.availability
# before setting provenance.telemetry_source = "live-org". Only AVAILABLE status
# means real org data was collected. All other statuses (EMPTY_RECENT, EMPTY_STALE,
# LICENSE_MISSING, ORG_FORBIDDEN, ORG_TYPE_UNKNOWN, QUERY_FAILED) mean the
# telemetry is unavailable, and telemetry_source must remain "mock" or similar.
#
# Example integration in cli.py:
#
#     result = collect_eventlogfile_telemetry(org_alias, start, end, run_id)
#     if result.availability == EventLogFileAvailability.AVAILABLE:
#         telemetry_source = "live-org"
#     else:
#         telemetry_source = "mock"  # or "unavailable"
#         # Log result.detail to explain why
#
# This prevents the pipeline from marking a spec as "observed" when EventLogFile
# data was unavailable due to licensing, latency, or org constraints.
#
# ============================================================================


from enum import Enum as _Enum
import csv
import io
import json as _json
import subprocess
import warnings


class EventLogFileAvailability(_Enum):
    """Why EventLogFile data is or is not available."""

    AVAILABLE = "available"  # Rows returned and within reasonable latency window
    EMPTY_RECENT = "empty_recent"  # No rows yet, but could appear soon (< 2 hours old request)
    EMPTY_STALE = "empty_stale"  # No rows and too old to be latency (> 25 hours)
    LICENSE_MISSING = "license_missing"  # Org does not have Event Monitoring enabled
    ORG_TYPE_UNKNOWN = "org_type_unknown"  # Cannot determine if target is sandbox; fail closed
    ORG_FORBIDDEN = "org_forbidden"  # PPCDM or PPCaccenture, strictly out of scope
    QUERY_FAILED = "query_failed"  # sf CLI or REST call failed


@dataclass(slots=True)
class EventLogFileResult:
    """Result of an EventLogFile collection attempt."""

    availability: EventLogFileAvailability
    events: list[TelemetryEvent]
    warnings: list[str] = field(default_factory=list)
    detail: str = ""  # Human-readable explanation of availability status


_FORBIDDEN_ORG_ALIASES = frozenset({"PPCDM", "PPCaccenture", "ppcdm", "ppaccenture"})


def _is_org_forbidden(org_alias: str) -> bool:
    """Check if org alias is in the forbidden set (case-insensitive)."""
    return org_alias in _FORBIDDEN_ORG_ALIASES or org_alias.lower() in {
        "ppcdm",
        "ppaccenture",
    }


def _verify_org_is_sandbox(org_alias: str) -> tuple[bool, str]:
    """
    Verify that the target org is a sandbox, not production.

    Returns:
        (is_safe, detail): is_safe=True only if positively identified as sandbox.
                          is_safe=False with detail if prod, forbidden, or unknown.
    """
    # HARD BLOCK on forbidden org aliases
    if _is_org_forbidden(org_alias):
        return False, f"Org alias '{org_alias}' is strictly forbidden (PPCDM/PPCaccenture out of scope)"

    try:
        # sf org display --json gives org info including IsSandbox
        result = subprocess.run(
            ["sf", "org", "display", "--target-org", org_alias, "--json"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return (
                False,
                f"Could not determine org type for '{org_alias}': sf org display failed",
            )

        org_info = _json.loads(result.stdout)
        is_sandbox = org_info.get("result", {}).get("isSandbox")

        if is_sandbox is None:
            return (
                False,
                f"Could not determine org type for '{org_alias}': IsSandbox field missing",
            )

        if not is_sandbox:
            return False, f"Org '{org_alias}' is a production org; production orgs are off-limits"

        return True, f"Org '{org_alias}' verified as sandbox"

    except subprocess.TimeoutExpired:
        return False, f"Org verification timed out for '{org_alias}'"
    except Exception as e:
        return False, f"Org verification failed for '{org_alias}': {e}"


def _map_event_type_to_layer(event_type: str) -> TelemetryLayer:
    """Map EventLogFile EventType to TelemetryLayer."""
    event_upper = event_type.upper()

    # Check ASYNC before APEX to avoid matching "AsyncApex" as APEX
    if "BULK" in event_upper or "ASYNC" in event_upper:
        return TelemetryLayer.ASYNC
    if "APEX" in event_upper:
        return TelemetryLayer.APEX
    if event_upper in {"API", "URI", "RESTAPI"}:
        return TelemetryLayer.NETWORK
    if "VALIDATION" in event_upper or "VALIDATIONRULE" in event_upper:
        return TelemetryLayer.VALIDATION
    if "FLOW" in event_upper:
        return TelemetryLayer.FLOW
    if "LIGHTNING" in event_upper or "AURAACTION" in event_upper:
        return TelemetryLayer.UI
    if "WAVE" in event_upper or "REPORT" in event_upper:
        return TelemetryLayer.DATA
    if "CALLOUT" in event_upper or "EXTERNALCROSSORG" in event_upper:
        return TelemetryLayer.INTEGRATION

    # Default for unrecognized types
    return TelemetryLayer.NETWORK


def _parse_eventlogfile_csv(
    log_body: str, event_type: str, run_id: str
) -> tuple[list[TelemetryEvent], list[str]]:
    """
    Parse EventLogFile CSV body into TelemetryEvents.

    Returns:
        (events, warnings): events parsed from CSV, warnings for malformed rows.
    """
    events: list[TelemetryEvent] = []
    warnings_list: list[str] = []
    skipped_count = 0

    try:
        reader = csv.DictReader(io.StringIO(log_body))
        for idx, row in enumerate(reader, start=1):
            try:
                # Extract timestamp (TIMESTAMP field in most logs, sometimes EVENT_DATE)
                timestamp_str = row.get("TIMESTAMP") or row.get("EVENT_DATE")
                if not timestamp_str:
                    skipped_count += 1
                    continue

                # Parse org timestamp
                try:
                    # EventLogFile timestamps are ISO 8601 UTC
                    org_timestamp = datetime.fromisoformat(
                        timestamp_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    skipped_count += 1
                    continue

                # Extract transaction/request ID if present
                transaction_id = row.get("REQUEST_ID") or row.get("TRANSACTION_ID")

                # Determine status from row (e.g., SUCCESS field or STATUS)
                status_raw = row.get("SUCCESS") or row.get("STATUS") or "unknown"
                if status_raw in {"1", "true", "True", "TRUE"}:
                    status = "success"
                elif status_raw in {"0", "false", "False", "FALSE"}:
                    status = "error"
                else:
                    status = str(status_raw).lower()

                # Event name from row type or operation
                event_name = (
                    row.get("EVENT_TYPE")
                    or row.get("OPERATION")
                    or event_type
                    or "UnknownEvent"
                )

                # Build payload from remaining fields (strip out correlation fields)
                payload = {
                    k: v
                    for k, v in row.items()
                    if k
                    not in {
                        "TIMESTAMP",
                        "EVENT_DATE",
                        "REQUEST_ID",
                        "TRANSACTION_ID",
                        "SUCCESS",
                        "STATUS",
                        "EVENT_TYPE",
                        "OPERATION",
                    }
                    and v
                }

                layer = _map_event_type_to_layer(event_type)

                # Step ID unknown at parse time; caller must correlate by timestamp window
                event = TelemetryEvent(
                    correlation=CorrelationKey(
                        run_id=run_id,
                        step_id="unknown",  # Correlation.py will assign via timestamp
                        event_time=org_timestamp,
                    ),
                    layer=layer,
                    event_name=event_name,
                    status=status,
                    payload=payload,
                    org_timestamp=org_timestamp,
                    transaction_id=transaction_id,
                )
                events.append(event)

            except Exception as e:
                skipped_count += 1
                if skipped_count <= 5:  # Only log first few to avoid spam
                    warnings_list.append(f"Row {idx}: parse failed ({e})")

        if skipped_count > 0:
            warnings_list.append(f"Skipped {skipped_count} malformed rows")

        # Warn if large fraction lost
        total = len(events) + skipped_count
        if total > 0 and skipped_count / total > 0.3:
            warnings_list.append(
                f"WARNING: {skipped_count}/{total} rows lost to parse errors (>30%)"
            )

    except Exception as e:
        warnings_list.append(f"CSV parse failed entirely: {e}")

    return events, warnings_list


def collect_eventlogfile_telemetry(
    org_alias: str,
    start_time: datetime,
    end_time: datetime,
    run_id: str,
    event_types: list[str] | None = None,
) -> EventLogFileResult:
    """
    Collect telemetry from Salesforce EventLogFile for a time window.

    This function queries EventLogFile records via `sf data query` and fetches log
    bodies via the LogFile URL. It respects the CLI-first rule: all Salesforce
    interaction goes through `sf` CLI, never browser automation.

    Event Monitoring licensing and latency constraints:
    - EventLogFile availability depends on Event Monitoring / Shield license
    - Hourly logs: available ~1-2 hours after the hour ends (requires Event Monitoring + hourly config)
    - Daily logs: available ~24 hours after the day ends
    - A recording made minutes ago will have NO EventLogFile rows yet

    Security constraints:
    - Production orgs are off-limits
    - PPCDM and PPCaccenture sandboxes are strictly out of scope
    - Org type is verified before any query; fail closed if unknown

    Args:
        org_alias: sf CLI org alias (e.g., "my-scratch-org")
        start_time: Start of time window (browser event time - epsilon)
        end_time: End of time window (browser event time + correlation window)
        run_id: Run identifier for correlation
        event_types: EventLogFile event types to query; defaults to relevant types

    Returns:
        EventLogFileResult with availability status, events (empty if unavailable), and warnings.
    """
    if event_types is None:
        # Default to event types relevant to this pipeline
        event_types = [
            "ApexExecution",
            "API",
            "URI",
            "ValidationRule",
            "LightningInteraction",
            "LightningPageView",
            "FlowExecution",
        ]

    # STEP 1: Verify org is sandbox and not forbidden
    is_safe, org_detail = _verify_org_is_sandbox(org_alias)
    if not is_safe:
        if "forbidden" in org_detail.lower():
            return EventLogFileResult(
                availability=EventLogFileAvailability.ORG_FORBIDDEN,
                events=[],
                detail=org_detail,
            )
        return EventLogFileResult(
            availability=EventLogFileAvailability.ORG_TYPE_UNKNOWN,
            events=[],
            detail=org_detail,
        )

    # STEP 2: Query EventLogFile metadata
    # LogDate is date of the log, not the events inside it; use it to filter candidates
    start_date = start_time.date()
    end_date = end_time.date()

    event_type_filter = ", ".join(f"'{et}'" for et in event_types)
    query = f"""
        SELECT Id, EventType, LogDate, LogFile, LogFileLength, Interval
        FROM EventLogFile
        WHERE LogDate >= {start_date.isoformat()}
          AND LogDate <= {end_date.isoformat()}
          AND EventType IN ({event_type_filter})
        ORDER BY LogDate, EventType
    """

    try:
        result = subprocess.run(
            ["sf", "data", "query", "--query", query, "--target-org", org_alias, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            stderr = result.stderr or ""
            # Check if error indicates Event Monitoring not enabled
            if "INVALID_TYPE" in stderr or "sObject type 'EventLogFile'" in stderr:
                return EventLogFileResult(
                    availability=EventLogFileAvailability.LICENSE_MISSING,
                    events=[],
                    detail=f"EventLogFile unavailable in org '{org_alias}': Event Monitoring license missing or disabled",
                )
            return EventLogFileResult(
                availability=EventLogFileAvailability.QUERY_FAILED,
                events=[],
                detail=f"EventLogFile query failed for '{org_alias}': {stderr.strip()}",
            )

        query_result = _json.loads(result.stdout)
        records = query_result.get("result", {}).get("records", [])

    except subprocess.TimeoutExpired:
        return EventLogFileResult(
            availability=EventLogFileAvailability.QUERY_FAILED,
            events=[],
            detail=f"EventLogFile query timed out for '{org_alias}'",
        )
    except Exception as e:
        return EventLogFileResult(
            availability=EventLogFileAvailability.QUERY_FAILED,
            events=[],
            detail=f"EventLogFile query failed for '{org_alias}': {e}",
        )

    # STEP 3: Determine availability based on results and latency
    now = datetime.now(timezone.utc)
    recording_age = now - end_time

    if not records:
        # No records: distinguish latency from absence
        if recording_age.total_seconds() < 2 * 3600:  # < 2 hours old
            return EventLogFileResult(
                availability=EventLogFileAvailability.EMPTY_RECENT,
                events=[],
                detail=f"No EventLogFile rows yet; recording is {recording_age.total_seconds()/3600:.1f}h old (hourly logs appear ~1-2h after the hour)",
            )
        elif recording_age.total_seconds() < 25 * 3600:  # < 25 hours old
            return EventLogFileResult(
                availability=EventLogFileAvailability.EMPTY_RECENT,
                events=[],
                detail=f"No EventLogFile rows yet; recording is {recording_age.total_seconds()/3600:.1f}h old (daily logs appear ~24h after the day ends)",
            )
        else:
            return EventLogFileResult(
                availability=EventLogFileAvailability.EMPTY_STALE,
                events=[],
                detail=f"No EventLogFile rows for time window; recording is {recording_age.total_seconds()/3600:.1f}h old (too stale to be latency)",
            )

    # STEP 4: Fetch and parse log files
    all_events: list[TelemetryEvent] = []
    all_warnings: list[str] = []

    for record in records:
        event_type = record.get("EventType", "Unknown")
        log_file_url = record.get("LogFile")

        if not log_file_url:
            all_warnings.append(f"EventLogFile {record.get('Id')} has no LogFile URL")
            continue

        # Fetch log body via sf org open --url-only + curl
        # SECURITY: Never log the frontdoor URL or pass token as argv
        try:
            # Get signed frontdoor URL (bypasses SSO/MFA)
            fd_result = subprocess.run(
                ["sf", "org", "open", "--url-only", "-o", org_alias],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if fd_result.returncode != 0:
                all_warnings.append(
                    f"Could not get frontdoor URL for log {record.get('Id')}"
                )
                continue

            # LogFile is a relative path; need instance URL
            # Extract instance URL from org display
            org_display = subprocess.run(
                ["sf", "org", "display", "--target-org", org_alias, "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if org_display.returncode != 0:
                all_warnings.append(f"Could not get instance URL for org '{org_alias}'")
                continue

            org_info = _json.loads(org_display.stdout)
            instance_url = org_info.get("result", {}).get("instanceUrl")
            if not instance_url:
                all_warnings.append(f"Instance URL missing for org '{org_alias}'")
                continue

            # Fetch log body (CSV) via REST
            full_log_url = f"{instance_url}{log_file_url}"
            # Use session ID from sf org display; NEVER log this value
            access_token = org_info.get("result", {}).get("accessToken")
            if not access_token:
                all_warnings.append(f"Access token unavailable for org '{org_alias}'")
                continue

            # Fetch via curl with Authorization header
            curl_result = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-H",
                    f"Authorization: Bearer {access_token}",
                    "-H",
                    "X-PrettyPrint: 1",
                    full_log_url,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if curl_result.returncode != 0:
                all_warnings.append(
                    f"Failed to fetch log file for {record.get('Id')}: curl failed"
                )
                continue

            log_body = curl_result.stdout

            # Parse CSV into events
            events, warnings = _parse_eventlogfile_csv(log_body, event_type, run_id)
            all_events.extend(events)
            all_warnings.extend(warnings)

        except Exception as e:
            all_warnings.append(f"Failed to process log {record.get('Id')}: {e}")
            continue

    return EventLogFileResult(
        availability=EventLogFileAvailability.AVAILABLE,
        events=all_events,
        warnings=all_warnings,
        detail=f"Collected {len(all_events)} events from {len(records)} log file(s)",
    )

