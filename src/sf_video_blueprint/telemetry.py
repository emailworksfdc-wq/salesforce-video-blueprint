from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from .org_denylist import BLOCKED_ORG_ALIASES as _BLOCKED_ORG_ALIASES
from .org_denylist import is_org_blocked

# Safe to import at module scope: `redaction` imports only stdlib (hashlib, hmac,
# re), nothing from this package, so there is no cycle. Verified, not assumed.
from .redaction import pipeline_policy, redact_mapping


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


class MockTelemetryCollector(TelemetryCollector):
    """Returns the same fabricated Flow event and Case diff for every step.

    Nothing here is observed. The payload is fixed, the record id is fake, and the
    Case Status transition is invented — a run through this collector cannot tell
    you what the org did.

    That is precisely why a run using it is stamped `telemetry_source: "mock"`,
    which `markers.telemetry_is_real` rejects and the score gate blocks on. Real
    telemetry requires a live org (see `SalesforceTelemetryCollector`). Do not add
    "mock" to `REAL_TELEMETRY_SOURCES` to make a run pass; that would make the
    fabrication invisible, which is the one failure this project exists to prevent.

    Lives here rather than in `cli.py` so library and MCP consumers can assemble a
    mock run without importing the CLI (and therefore `typer`).
    """

    def collect_for_step(self, run_id: str, step_id: str) -> list[TelemetryEvent]:
        return [
            TelemetryEvent(
                correlation=CorrelationKey(
                    run_id=run_id, step_id=step_id, event_time=datetime.now(timezone.utc)
                ),
                layer=TelemetryLayer.FLOW,
                event_name="FlowInterviewExecuted",
                status="success",
                payload={"flowApiName": "Sample_Flow"},
            )
        ]

    def snapshot_changes(self, run_id: str, step_id: str) -> list[ObjectSnapshot]:
        return [
            ObjectSnapshot(
                correlation=CorrelationKey(
                    run_id=run_id, step_id=step_id, event_time=datetime.now(timezone.utc)
                ),
                object_api_name="Case",
                record_id="500xx0000012345AAA",
                before={"Status": "New"},
                after={"Status": "Working"},
                changed_fields=["Status"],
            )
        ]


class TelemetryRegistry:
    """In-memory event contract store; replace with persistent backend in production.

    **Ingest is a redaction boundary.** Everything that enters this registry is
    scrubbed on the way in, not on the way out. Snapshots and payloads are whole
    Salesforce records — `SalesforceRestClient.get_record` results and raw SOQL rows
    — fetched *after* extraction has finished, so the extraction choke point in
    `dom_extractor` structurally cannot see them. `spec_builder._derive_entities`
    interpolates their field values straight into entity evidence, so an unscrubbed
    token in a Case Description reached `agent-spec.json` verbatim.

    Scrubbing here rather than at the call site is deliberate: a boundary that lives
    on the container covers every caller, including ones not written yet. The
    alternative — each caller remembering to scrub — is the shape of defect that
    ships. `cli.py` still calls `redaction.scrub_collected_telemetry` afterwards as
    defence in depth; that pass is idempotent, so the two do not fight.

    Scrubbing uses `redaction.pipeline_policy()`, NOT `RedactionPolicy.strict()`.
    Strict redacts record ids (destroying the audit trail that ties a spec back to
    the record it came from) and any 10 consecutive digits (rewriting Luhn-passing
    epoch-millisecond timestamps as `[REDACTED:phone]`, corrupting the correlation
    timeline). Both were measured, not assumed — see `tests/test_redaction_wiring_telemetry.py`.
    """

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []
        self.snapshots: list[ObjectSnapshot] = []
        # What the ingest scrub actually found, so the run can report that the
        # control fired. A silent control cannot be audited. Names the KIND of
        # value found ("aws_key", "email") and never the value itself.
        self.redaction_categories: list[str] = []
        self._redaction_policy = pipeline_policy()

    def _record_categories(self, found: list[str]) -> None:
        """Accumulate scrub categories, de-duplicated, preserving first-seen order."""
        for category in found:
            if category not in self.redaction_categories:
                self.redaction_categories.append(category)

    def _scrub_event(self, event: TelemetryEvent) -> TelemetryEvent:
        if event.payload:
            event.payload, found = redact_mapping(event.payload, self._redaction_policy)
            self._record_categories(found)
        return event

    def _scrub_snapshot(self, snapshot: ObjectSnapshot) -> ObjectSnapshot:
        # `record_id`, `object_api_name` and `changed_fields` are NOT scrubbed:
        # they are the audit trail and the field API names that drive entity
        # derivation. Masking a field name there would silently change what the
        # derived agent spec asks for.
        for attr in ("before", "after"):
            record = getattr(snapshot, attr, None)
            if record:
                scrubbed, found = redact_mapping(record, self._redaction_policy)
                setattr(snapshot, attr, scrubbed)
                self._record_categories(found)
        return snapshot

    def collect_step(
        self,
        collector: TelemetryCollector,
        run_id: str,
        step_id: str,
    ) -> None:
        """Ingest one step's telemetry, scrubbing every value on the way in."""
        self.events.extend(
            self._scrub_event(event) for event in collector.collect_for_step(run_id, step_id)
        )
        self.snapshots.extend(
            self._scrub_snapshot(snapshot)
            for snapshot in collector.snapshot_changes(run_id, step_id)
        )

    def append_manual_event(
        self,
        run_id: str,
        step_id: str,
        layer: TelemetryLayer,
        event_name: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append a hand-built event. Scrubbed like any other ingest path.

        No in-tree caller today, which is exactly why it is covered: the first
        caller to appear would otherwise reintroduce the leak silently.
        """
        self.events.append(
            self._scrub_event(
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


#: Canonical names, re-exported for callers and error messages. The matching
#: logic lives in `org_denylist`; this is not a match set.
#:
#: DEFECT L4-4: this used to be a hand-maintained case-variant set,
#: {"PPCDM", "PPCaccenture", "ppcdm", "ppaccenture"}, whose lowercase entry read
#: `ppaccenture` — one `c` — where it meant `ppcaccenture`. So
#: `_is_org_forbidden("ppcaccenture")`, the spelling a shell user is most likely
#: to type, returned False and reached a hard-blocked org.
_FORBIDDEN_ORG_ALIASES = _BLOCKED_ORG_ALIASES


def _is_org_forbidden(org_alias: str) -> bool:
    """Check if an org identifier names a permanently out-of-scope org.

    Delegates to `org_denylist.is_org_blocked`, which normalizes case,
    whitespace and punctuation and matches derived / username / instance-URL
    forms as well as bare aliases.
    """
    return is_org_blocked(org_alias)


#: Org types that are safe to collect telemetry from even though `IsSandbox` is
#: false. A Developer Edition org is not a sandbox *and* not production: it holds
#: no customer data and exists to be experimented on.
#:
#: REVIEW FINDING R3 — this set is why the fix is not simply "read IsSandbox".
#: Measured on AFT3, the org LANE_RULES assigns this lane:
#:     SELECT IsSandbox, OrganizationType FROM Organization
#:     -> IsSandbox=False, OrganizationType='Developer Edition'
#: Keying safety on `IsSandbox` alone would refuse the only org this project is
#: permitted to touch.
_NON_PRODUCTION_ORG_TYPES: frozenset[str] = frozenset(
    {
        "developer edition",
        "team edition",  # legacy name for DE-class orgs
    }
)


def _verify_org_is_sandbox(org_alias: str) -> tuple[bool, str]:
    """
    Verify that the target org is safe to collect from — sandbox, scratch or DE.

    Fails closed: returns is_safe=True only on a *positive* identification. An
    org that cannot be classified is refused.

    REVIEW FINDING R3 — this function could previously never return True for any
    org. It read `isSandbox` from `sf org display --json`, which does not contain
    that key; measured against AFT3, the payload carries only::

        accessToken alias apiVersion clientId connectedStatus id instanceUrl username

    so the `is_sandbox is None` branch fired every time and the answer was always
    "IsSandbox field missing". It failed closed, so it was never a safety hole —
    but a guard that always refuses is indistinguishable from one that works,
    because the refusal looks like the fail-closed path working as designed. The
    org check was decorative. Its four unit tests all passed, because each mocked
    a `{"result": {"isSandbox": ...}}` payload the real CLI never emits.

    The org type lives on the `Organization` sobject, so that is what we query.

    Returns:
        (is_safe, detail): is_safe=True only if positively identified as a
                          sandbox, scratch org, or Developer Edition org.
                          is_safe=False with detail if prod, forbidden, or unknown.
    """
    # HARD BLOCK on forbidden org aliases. Before any subprocess: a blocked org
    # must not be contacted even to classify it.
    if _is_org_forbidden(org_alias):
        return False, f"Org alias '{org_alias}' is strictly forbidden (PPCDM/PPCaccenture out of scope)"

    try:
        # Step 1: confirm the alias resolves to an authenticated org at all.
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

        # Step 2: ask the org what it is. `sf org display` cannot answer this —
        # see R3 above — so read it off the Organization object.
        org_type_result = subprocess.run(
            [
                "sf",
                "data",
                "query",
                "--query",
                "SELECT IsSandbox, OrganizationType FROM Organization",
                "--target-org",
                org_alias,
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if org_type_result.returncode != 0:
            return (
                False,
                (
                    f"Could not determine org type for '{org_alias}': "
                    "querying the Organization object failed"
                ),
            )

        records = (
            _json.loads(org_type_result.stdout).get("result", {}).get("records") or []
        )
        if not records:
            return (
                False,
                (
                    f"Could not determine org type for '{org_alias}': "
                    "the Organization query returned no rows"
                ),
            )

        record = records[0]
        is_sandbox = record.get("IsSandbox")
        org_type = (record.get("OrganizationType") or "").strip()

        if is_sandbox is None:
            return (
                False,
                (
                    f"Could not determine org type for '{org_alias}': "
                    "Organization.IsSandbox was absent from the query result"
                ),
            )

        if is_sandbox:
            return True, f"Org '{org_alias}' verified as sandbox ({org_type or 'unknown edition'})"

        # Not a sandbox. It may still be a non-production org type.
        if org_type.lower() in _NON_PRODUCTION_ORG_TYPES:
            return (
                True,
                (
                    f"Org '{org_alias}' verified as {org_type} — not a sandbox, "
                    "but not production either (no customer data)"
                ),
            )

        if not org_type:
            return (
                False,
                (
                    f"Could not determine org type for '{org_alias}': "
                    "not a sandbox and OrganizationType was empty"
                ),
            )

        return (
            False,
            (
                f"Org '{org_alias}' is a production org ({org_type}); "
                "production orgs are off-limits"
            ),
        )

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

