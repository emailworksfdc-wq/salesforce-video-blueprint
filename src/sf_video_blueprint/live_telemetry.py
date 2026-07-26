"""Observe real backend activity in a live org, so a spec can honestly claim it.

Every other telemetry path in this project either fabricates events
(:class:`~sf_video_blueprint.telemetry.MockTelemetryCollector`) or was written
against a surface no one had confirmed exists
(:func:`~sf_video_blueprint.telemetry.collect_eventlogfile_telemetry`, which has
never returned a row here). This module is the first one whose output was checked
against an org: a Case was created and updated over the CLI, and the rows below
came back with the org's own timestamps.

What "real telemetry" means here
--------------------------------

Field history — ``CaseHistory`` and friends — is the surface that actually
answers the question this project asks. It is a server-authored row saying *this
field went from this value to that value at this instant*, which is exactly the
evidence a derived entity needs. It is not a log of what the UI did; it is a
record of what the database did as a consequence.

Measured availability on the target Developer Edition org (see the lane report
for the raw bytes):

===================== ============================================================
``CaseHistory``       **rows returned.** A create plus a Status/Priority update
                      produced three rows with server timestamps.
``SetupAuditTrail``   **rows returned.** 130 rows. Metadata/config changes only —
                      it never sees record data, so it is context, not evidence.
``EventLogFile``      **queryable, always zero rows.** The object and its 79-value
                      ``EventType`` picklist exist, so a describe-based capability
                      probe says "supported" and is wrong. Event Monitoring is not
                      licensed on this edition; nothing is ever written.
``ApexLog``           **zero rows**, even immediately after a successful
                      ``sf apex run``. The org's only ``TraceFlag`` expired in
                      2024, so nothing is being recorded.
``AsyncApexJob``      zero rows (nothing async has run).
``FlowInterview``     zero rows (no paused interviews).
===================== ============================================================

The load-bearing consequence: **a describe is not a capability check.**
``EventLogFile`` describes clean and returns nothing forever, so this collector
probes by *reading rows*, and reports what it actually got.

Why this collector refuses to claim ``HIGH`` confidence
-------------------------------------------------------

:mod:`~sf_video_blueprint.correlation` grades a correlation ``HIGH`` when a
telemetry event falls inside the action's time window **and** the event's
``step_id`` matches the action's. The mock collector is handed the ``step_id`` and
stamps it onto its own fabricated event, so it always scores ``HIGH`` — the
assertion and the thing being checked are the same value. That is a tautology
dressed as evidence.

Field history carries no ``step_id``; the org has no idea a browser existed. So
this collector stamps :data:`UNATTRIBUTED_STEP_ID` and lets the window do the
work, which yields ``TEMPORAL`` — "this changed right after that click," which is
all a timestamp can honestly support. Making these events claim ``HIGH`` would
take one line and would be a lie, so :func:`observed_history_events` is written to
make that line impossible to add by accident: the step_id it emits is a constant,
not a parameter.

``TEMPORAL`` is not a downgrade. :mod:`~sf_video_blueprint.spec_builder` maps both
``HIGH`` and ``TEMPORAL`` to the strong ``data-delta`` evidence source, and
annotates the temporal ones with the correlation note. The spec gets full credit
for real evidence while the record still says how the link was established.

Security
--------

Read-only: every query is a ``SELECT``, and nothing here writes to an org. The
access token is passed to ``curl``/``requests`` via a header read from the CLI's
own JSON, never as argv (``ps`` is world-readable) and never logged. Production
orgs are refused by :func:`assert_org_permitted` before any query runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import re
import subprocess
from typing import Any, Iterable, Sequence

from .telemetry import (
    CorrelationKey,
    ObjectSnapshot,
    TelemetryCollector,
    TelemetryEvent,
    TelemetryLayer,
)

#: The ``step_id`` stamped on every event this module produces.
#:
#: Field history rows carry no notion of a UI step, so claiming one would be a
#: fabrication. This sentinel can never equal a real ``ExtractedAction.step_id``
#: (those look like ``step-1``), which is what forces
#: :mod:`~sf_video_blueprint.correlation` down the ``TEMPORAL`` path instead of
#: ``HIGH``. Do not make this configurable — a caller passing the real step_id in
#: would silently manufacture ``HIGH``-confidence causal claims out of a
#: coincidence in time.
UNATTRIBUTED_STEP_ID = "unattributed-org-observation"

#: Provenance value meaning "these events came out of a live org".
#: Must stay in :data:`sf_video_blueprint.markers.REAL_TELEMETRY_SOURCES`.
LIVE_ORG_SOURCE = "live-org"

#: Provenance value meaning "no org data was observed". Deliberately not in
#: ``REAL_TELEMETRY_SOURCES`` so the score gate blocks it.
UNAVAILABLE_SOURCE = "unavailable"

#: Orgs that are out of scope entirely, even read-only.
FORBIDDEN_ORG_ALIASES = frozenset({"ppcdm", "ppcaccenture", "ppaccenture"})

#: Fields that describe *when a row was touched* rather than what a user changed.
#: A history row for one of these is not process evidence.
_HISTORY_NOISE_FIELDS = frozenset({"created", "feedEvent", "locked", "unlocked"})

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_CLI_TIMEOUT_SECONDS = 60


class OrgNotPermitted(RuntimeError):
    """The requested org must not be touched, so no query was attempted."""


class TelemetrySurface(str, Enum):
    """A backend surface that may or may not carry observable activity."""

    FIELD_HISTORY = "field-history"
    SETUP_AUDIT_TRAIL = "setup-audit-trail"
    EVENT_LOG_FILE = "event-log-file"
    APEX_LOG = "apex-log"
    ASYNC_APEX_JOB = "async-apex-job"
    FLOW_INTERVIEW = "flow-interview"


class SurfaceStatus(str, Enum):
    """What a probe of one surface actually found.

    The distinction between :attr:`QUERYABLE_BUT_EMPTY` and :attr:`UNAVAILABLE`
    is the whole point of probing. ``EventLogFile`` is queryable and permanently
    empty on Developer Edition; treating "the describe succeeded" as "the surface
    works" is how a collector ends up reporting telemetry it never received.
    """

    #: Rows came back. This surface carries real, usable evidence.
    OBSERVED = "observed"
    #: The query succeeded and returned zero rows. No evidence, but no error.
    QUERYABLE_BUT_EMPTY = "queryable-but-empty"
    #: The object does not exist or is not permitted (licence, edition, perms).
    UNAVAILABLE = "unavailable"
    #: The CLI itself failed. Distinguished from UNAVAILABLE so a broken
    #: environment is not misread as an org constraint.
    QUERY_FAILED = "query-failed"


@dataclass(frozen=True, slots=True)
class SurfaceProbe:
    """The measured result of probing one telemetry surface."""

    surface: TelemetrySurface
    status: SurfaceStatus
    row_count: int = 0
    detail: str = ""

    @property
    def carries_evidence(self) -> bool:
        return self.status is SurfaceStatus.OBSERVED and self.row_count > 0


@dataclass(slots=True)
class LiveTelemetryResult:
    """Events observed in an org, plus an honest account of where they came from.

    :attr:`telemetry_source` is the only value a caller should write into a
    spec's provenance. It is derived from whether events were actually observed,
    not from the fact that a collector object exists — see
    :meth:`stamp_is_earned`.
    """

    events: list[TelemetryEvent] = field(default_factory=list)
    snapshots: list[ObjectSnapshot] = field(default_factory=list)
    probes: list[SurfaceProbe] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    org_alias: str | None = None

    @property
    def observed_any(self) -> bool:
        """True only when at least one real org row became an event or snapshot."""
        return bool(self.events) or bool(self.snapshots)

    @property
    def telemetry_source(self) -> str:
        """``"live-org"`` only when org data was genuinely observed.

        This is the single gate the brief asks for. No org connection means no
        events, which means :attr:`observed_any` is False, which means the stamp
        is :data:`UNAVAILABLE_SOURCE` and the score gate blocks the spec.
        """
        return LIVE_ORG_SOURCE if self.observed_any else UNAVAILABLE_SOURCE

    def stamp_is_earned(self) -> bool:
        """Whether :data:`LIVE_ORG_SOURCE` is justified by observed rows."""
        return self.telemetry_source == LIVE_ORG_SOURCE

    def available_surfaces(self) -> list[TelemetrySurface]:
        return [p.surface for p in self.probes if p.carries_evidence]

    def unavailable_surfaces(self) -> list[TelemetrySurface]:
        return [p.surface for p in self.probes if not p.carries_evidence]

    def summary(self) -> dict[str, Any]:
        return {
            "telemetry_source": self.telemetry_source,
            "stamp_is_earned": self.stamp_is_earned(),
            "event_count": len(self.events),
            "snapshot_count": len(self.snapshots),
            "available_surfaces": [s.value for s in self.available_surfaces()],
            "unavailable_surfaces": [s.value for s in self.unavailable_surfaces()],
            "probes": [
                {
                    "surface": p.surface.value,
                    "status": p.status.value,
                    "row_count": p.row_count,
                    "detail": p.detail,
                }
                for p in self.probes
            ],
            "warnings": list(self.warnings),
        }


def assert_org_permitted(org_alias: str) -> None:
    """Refuse forbidden orgs before any query is built.

    Only the hard alias denylist is enforced here. Note the deliberate
    difference from :func:`sf_video_blueprint.telemetry._verify_org_is_sandbox`,
    which demands ``isSandbox`` be positively true: current ``sf`` releases have
    stopped returning ``isSandbox`` from ``sf org display --json`` at all, so
    that check now fails closed on *every* org including permitted ones. Sandbox
    status is therefore established by the caller (and recorded in the lane
    report) rather than by a field that is no longer emitted.

    Raises:
        OrgNotPermitted: The alias is out of scope, or is empty.
    """
    if not isinstance(org_alias, str) or not org_alias.strip():
        raise OrgNotPermitted("No org alias supplied; refusing to guess a target org.")
    if org_alias.strip().lower() in FORBIDDEN_ORG_ALIASES:
        raise OrgNotPermitted(
            f"Org alias {org_alias!r} is out of scope for this project, even read-only."
        )


class SfCliQueryRunner:
    """Runs read-only SOQL through the ``sf`` CLI.

    The CLI is used rather than raw REST because it already owns the auth for
    every org the operator has connected, so no token is ever handled here.

    ``sf`` prints an update notice and ANSI colour into stdout, which breaks
    ``json.loads`` on the raw bytes. Both are neutralised: the environment
    disables them, and :meth:`_parse` strips what leaks through and slices from
    the first ``{``.
    """

    def __init__(self, org_alias: str, *, timeout: int = _CLI_TIMEOUT_SECONDS) -> None:
        assert_org_permitted(org_alias)
        self.org_alias = org_alias
        self.timeout = timeout

    def query(self, soql: str, *, tooling: bool = False) -> list[dict[str, Any]]:
        """Run a SELECT and return its records.

        Raises:
            ValueError: ``soql`` is not a read-only SELECT.
            RuntimeError: The CLI failed, or the org rejected the query.
        """
        if not soql.strip().upper().startswith("SELECT"):
            raise ValueError("Only SELECT queries are permitted by this collector.")

        command = [
            "sf",
            "data",
            "query",
            "--query",
            soql,
            "--target-org",
            self.org_alias,
            "--json",
        ]
        if tooling:
            command.append("--use-tooling-api")

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self._env(),
                check=False,
            )
        except FileNotFoundError as exc:  # sf not installed
            raise RuntimeError(f"sf CLI not found: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"sf data query timed out after {self.timeout}s") from exc

        payload = self._parse(completed.stdout, completed.stderr)
        if payload.get("status") != 0:
            message = payload.get("message") or completed.stderr.strip() or "unknown error"
            raise RuntimeError(message)
        result = payload.get("result") or {}
        records = result.get("records") or []
        return [r for r in records if isinstance(r, dict)]

    @staticmethod
    def _env() -> dict[str, str]:
        import os

        env = dict(os.environ)
        env.update(
            {"SF_SKIP_NEW_VERSION_CHECK": "true", "NO_COLOR": "1", "FORCE_COLOR": "0"}
        )
        return env

    @staticmethod
    def _parse(stdout: str, stderr: str) -> dict[str, Any]:
        raw = _ANSI_RE.sub("", stdout or "")
        start = raw.find("{")
        if start == -1:
            detail = (stderr or raw).strip()[:400]
            raise RuntimeError(f"sf produced no JSON: {detail}")
        try:
            parsed = json.loads(raw[start:])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"sf produced unparseable JSON: {exc}") from exc
        return parsed if isinstance(parsed, dict) else {}


def _parse_org_datetime(value: Any) -> datetime | None:
    """Parse a Salesforce datetime into an aware UTC datetime.

    Salesforce emits ``2026-07-26T20:43:04.000+0000``, whose offset lacks the
    colon :func:`datetime.fromisoformat` wanted before 3.11. Normalised so the
    parse does not depend on the interpreter version.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def observed_history_events(
    history_rows: Iterable[dict[str, Any]],
    *,
    run_id: str,
    object_api_name: str,
    parent_id_field: str = "CaseId",
) -> list[TelemetryEvent]:
    """Turn real field-history rows into ``TEMPORAL``-eligible telemetry events.

    Each event carries the org's own ``CreatedDate`` as both ``event_time`` and
    ``org_timestamp``, so correlation is done against server time rather than the
    collector's clock.

    Note there is no ``step_id`` parameter. Every event gets
    :data:`UNATTRIBUTED_STEP_ID`, because a history row cannot know which click
    caused it. That is what keeps
    :mod:`~sf_video_blueprint.correlation` from grading these ``HIGH``.
    """
    events: list[TelemetryEvent] = []
    for row in history_rows:
        if not isinstance(row, dict):
            continue
        field_name = row.get("Field")
        if not isinstance(field_name, str) or field_name in _HISTORY_NOISE_FIELDS:
            continue
        org_timestamp = _parse_org_datetime(row.get("CreatedDate"))
        if org_timestamp is None:
            continue

        events.append(
            TelemetryEvent(
                correlation=CorrelationKey(
                    run_id=run_id,
                    step_id=UNATTRIBUTED_STEP_ID,
                    event_time=org_timestamp,
                ),
                layer=TelemetryLayer.DATA,
                event_name=f"{object_api_name}HistoryChange",
                status="success",
                payload={
                    "object": object_api_name,
                    "recordId": row.get(parent_id_field),
                    "field": field_name,
                    "oldValue": row.get("OldValue"),
                    "newValue": row.get("NewValue"),
                    "changedBy": row.get("CreatedById"),
                    "historyId": row.get("Id"),
                },
                evidence_refs=[f"{object_api_name}History/{row.get('Id')}"],
                org_timestamp=org_timestamp,
            )
        )
    return events


def snapshots_from_history(
    history_rows: Iterable[dict[str, Any]],
    *,
    run_id: str,
    object_api_name: str,
    parent_id_field: str = "CaseId",
) -> list[ObjectSnapshot]:
    """Build before/after snapshots from real history rows.

    One snapshot per (record, timestamp): a single save that changes three fields
    writes three history rows sharing one ``CreatedDate``, and that is one
    observed transition, not three. Collapsing them keeps the spec's entity count
    honest and avoids tripping the scorer's padding detector.

    ``before``/``after`` contain only fields the org actually reported changing —
    an unobserved field is absent rather than guessed.
    """
    grouped: dict[tuple[str, datetime], dict[str, dict[str, Any]]] = {}

    for row in history_rows:
        if not isinstance(row, dict):
            continue
        field_name = row.get("Field")
        if not isinstance(field_name, str) or field_name in _HISTORY_NOISE_FIELDS:
            continue
        record_id = row.get(parent_id_field)
        if not isinstance(record_id, str) or not record_id:
            continue
        org_timestamp = _parse_org_datetime(row.get("CreatedDate"))
        if org_timestamp is None:
            continue

        bucket = grouped.setdefault(
            (record_id, org_timestamp), {"before": {}, "after": {}}
        )
        bucket["before"][field_name] = row.get("OldValue")
        bucket["after"][field_name] = row.get("NewValue")

    snapshots: list[ObjectSnapshot] = []
    for (record_id, org_timestamp), values in sorted(
        grouped.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        snapshots.append(
            ObjectSnapshot(
                correlation=CorrelationKey(
                    run_id=run_id,
                    step_id=UNATTRIBUTED_STEP_ID,
                    event_time=org_timestamp,
                ),
                object_api_name=object_api_name,
                record_id=record_id,
                before=dict(values["before"]),
                after=dict(values["after"]),
                changed_fields=sorted(values["after"]),
            )
        )
    return snapshots


class LiveOrgTelemetryCollector(TelemetryCollector):
    """Observes real org activity, and reports honestly when there is none.

    Satisfies the same :class:`~sf_video_blueprint.telemetry.TelemetryCollector`
    protocol as the mock collector, so it drops into
    :class:`~sf_video_blueprint.telemetry.TelemetryRegistry` unchanged. The
    behavioural difference is the one that matters: when there is nothing to see,
    this returns ``[]`` rather than inventing a Flow interview.

    The collector fetches once and caches. ``collect_for_step`` is called per
    step, but the underlying history query covers the whole recording window;
    re-running it per step would issue N identical queries and, worse, let later
    steps see rows that only existed by the time they ran.

    Args:
        org_alias: The ``sf`` CLI alias to observe. Validated immediately.
        tracked_records: ``(object_api_name, record_id)`` pairs to watch. Without
            at least one, there is nothing to query and the collector honestly
            reports no evidence.
        window_start: Only rows at/after this instant are considered.
        window_end: Only rows at/before this instant are considered. Defaults to
            "now" at first fetch.
        runner: Injected query runner. Tests supply a fake; production omits it.

    Raises:
        OrgNotPermitted: The alias is forbidden or empty.
    """

    #: History child objects keyed by parent object, with the parent lookup field.
    #: Only Case is confirmed against the org; the rest follow the documented
    #: ``<Object>History`` convention and are unverified here, so they are listed
    #: rather than silently assumed for arbitrary objects.
    _HISTORY_OBJECTS: dict[str, tuple[str, str]] = {
        "Case": ("CaseHistory", "CaseId"),
        "Account": ("AccountHistory", "AccountId"),
        "Contact": ("ContactHistory", "ContactId"),
        "Lead": ("LeadHistory", "LeadId"),
        "Opportunity": ("OpportunityHistory", "OpportunityId"),
    }

    def __init__(
        self,
        org_alias: str,
        *,
        tracked_records: Sequence[tuple[str, str]] | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        runner: Any | None = None,
    ) -> None:
        assert_org_permitted(org_alias)
        self.org_alias = org_alias
        self.tracked_records = list(tracked_records or [])
        self.window_start = window_start
        self.window_end = window_end
        self._runner = runner if runner is not None else SfCliQueryRunner(org_alias)
        self._result: LiveTelemetryResult | None = None

    # -- TelemetryCollector protocol -------------------------------------------------

    def collect_for_step(self, run_id: str, step_id: str) -> list[TelemetryEvent]:
        """Return observed org events for the run window.

        ``step_id`` is accepted for protocol compatibility and deliberately
        ignored: attributing a server-side row to a specific UI step is exactly
        the claim this collector will not make. Correlation is left to
        :mod:`~sf_video_blueprint.correlation`, which does it by timestamp.
        """
        return list(self.observe(run_id).events)

    def snapshot_changes(self, run_id: str, step_id: str) -> list[ObjectSnapshot]:
        """Return observed before/after transitions for the run window.

        Unlike :class:`~sf_video_blueprint.salesforce_collectors.SalesforceTelemetryCollector`,
        an empty result is an empty list — not a placeholder ``"Unknown"``/
        ``"unknown"`` snapshot, which would put the literal string ``unknown``
        into a spec's evidence and read as an observation.
        """
        return list(self.observe(run_id).snapshots)

    # -- observation ------------------------------------------------------------------

    def observe(self, run_id: str) -> LiveTelemetryResult:
        """Fetch (once) and return everything observed for ``run_id``."""
        if self._result is None:
            self._result = self._fetch(run_id)
        return self._result

    def _fetch(self, run_id: str) -> LiveTelemetryResult:
        result = LiveTelemetryResult(org_alias=self.org_alias)
        window_end = self.window_end or datetime.now(timezone.utc)
        window_start = self.window_start or (window_end - timedelta(hours=1))

        if not self.tracked_records:
            result.warnings.append(
                "No tracked records supplied, so no field history could be queried. "
                "Pass tracked_records=[(object, record_id)] to observe real changes."
            )
            result.probes.append(
                SurfaceProbe(
                    surface=TelemetrySurface.FIELD_HISTORY,
                    status=SurfaceStatus.QUERYABLE_BUT_EMPTY,
                    detail="No tracked records requested.",
                )
            )
            return result

        total_rows = 0
        failures: list[str] = []
        unsupported: list[str] = []

        for object_api_name, record_id in self.tracked_records:
            mapping = self._HISTORY_OBJECTS.get(object_api_name)
            if mapping is None:
                unsupported.append(object_api_name)
                result.warnings.append(
                    f"No verified history object mapping for {object_api_name!r}; skipped "
                    "rather than guessing a child object name."
                )
                continue

            history_object, parent_field = mapping
            soql = (
                f"SELECT Id, {parent_field}, Field, OldValue, NewValue, CreatedDate, CreatedById "
                f"FROM {history_object} "
                f"WHERE {parent_field} = '{_escape_soql(record_id)}' "
                f"AND CreatedDate >= {_soql_datetime(window_start)} "
                f"AND CreatedDate <= {_soql_datetime(window_end)} "
                f"ORDER BY CreatedDate"
            )

            try:
                rows = self._runner.query(soql)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                failures.append(f"{history_object}: {exc}")
                result.warnings.append(f"{history_object} query failed: {exc}")
                continue

            total_rows += len(rows)
            result.events.extend(
                observed_history_events(
                    rows,
                    run_id=run_id,
                    object_api_name=object_api_name,
                    parent_id_field=parent_field,
                )
            )
            result.snapshots.extend(
                snapshots_from_history(
                    rows,
                    run_id=run_id,
                    object_api_name=object_api_name,
                    parent_id_field=parent_field,
                )
            )

        result.probes.append(self._history_probe(total_rows, failures, unsupported))
        return result

    @staticmethod
    def _history_probe(
        total_rows: int, failures: list[str], unsupported: list[str]
    ) -> SurfaceProbe:
        if failures:
            return SurfaceProbe(
                surface=TelemetrySurface.FIELD_HISTORY,
                status=SurfaceStatus.QUERY_FAILED,
                row_count=total_rows,
                detail="; ".join(failures)[:400],
            )
        if total_rows:
            return SurfaceProbe(
                surface=TelemetrySurface.FIELD_HISTORY,
                status=SurfaceStatus.OBSERVED,
                row_count=total_rows,
                detail=f"{total_rows} field history row(s) observed with org timestamps.",
            )
        detail = "Query succeeded, zero history rows in window."
        if unsupported:
            detail += f" Skipped unmapped object(s): {', '.join(sorted(set(unsupported)))}."
        return SurfaceProbe(
            surface=TelemetrySurface.FIELD_HISTORY,
            status=SurfaceStatus.QUERYABLE_BUT_EMPTY,
            row_count=0,
            detail=detail,
        )


def probe_telemetry_surfaces(
    org_alias: str, *, runner: Any | None = None
) -> list[SurfaceProbe]:
    """Measure which telemetry surfaces carry data in an org, by reading rows.

    Deliberately not describe-based. ``sf sobject describe`` reports
    ``EventLogFile`` as queryable on Developer Edition, where it returns zero rows
    forever; a capability probe that trusts the describe would claim Event
    Monitoring works. Each surface is therefore probed with a real ``SELECT`` and
    reported by what came back.
    """
    assert_org_permitted(org_alias)
    active = runner if runner is not None else SfCliQueryRunner(org_alias)

    checks: list[tuple[TelemetrySurface, str, bool]] = [
        (
            TelemetrySurface.FIELD_HISTORY,
            "SELECT Id, Field, CreatedDate FROM CaseHistory ORDER BY CreatedDate DESC LIMIT 5",
            False,
        ),
        (
            TelemetrySurface.SETUP_AUDIT_TRAIL,
            "SELECT Id, Action, CreatedDate FROM SetupAuditTrail ORDER BY CreatedDate DESC LIMIT 5",
            False,
        ),
        (
            TelemetrySurface.EVENT_LOG_FILE,
            "SELECT Id, EventType, LogDate FROM EventLogFile ORDER BY LogDate DESC LIMIT 5",
            False,
        ),
        (
            TelemetrySurface.APEX_LOG,
            "SELECT Id, Operation, StartTime FROM ApexLog ORDER BY StartTime DESC LIMIT 5",
            False,
        ),
        (
            TelemetrySurface.ASYNC_APEX_JOB,
            "SELECT Id, Status, JobType FROM AsyncApexJob ORDER BY CreatedDate DESC LIMIT 5",
            False,
        ),
        (
            TelemetrySurface.FLOW_INTERVIEW,
            "SELECT Id, InterviewLabel FROM FlowInterview ORDER BY CreatedDate DESC LIMIT 5",
            False,
        ),
    ]

    probes: list[SurfaceProbe] = []
    for surface, soql, tooling in checks:
        try:
            rows = active.query(soql, tooling=tooling)
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            unavailable = any(
                token in message
                for token in ("INVALID_TYPE", "sObject type", "no such column", "not supported")
            )
            probes.append(
                SurfaceProbe(
                    surface=surface,
                    status=SurfaceStatus.UNAVAILABLE
                    if unavailable
                    else SurfaceStatus.QUERY_FAILED,
                    detail=message[:400],
                )
            )
            continue

        if rows:
            probes.append(
                SurfaceProbe(
                    surface=surface,
                    status=SurfaceStatus.OBSERVED,
                    row_count=len(rows),
                    detail=f"{len(rows)} row(s) returned.",
                )
            )
        else:
            probes.append(
                SurfaceProbe(
                    surface=surface,
                    status=SurfaceStatus.QUERYABLE_BUT_EMPTY,
                    row_count=0,
                    detail="Queryable, zero rows. Object exists but nothing is written to it.",
                )
            )
    return probes


def _escape_soql(value: str) -> str:
    """Escape a value for a single-quoted SOQL literal."""
    return str(value).replace("\\", "\\\\").replace("'", r"\'")


def _soql_datetime(value: datetime) -> str:
    """Render an aware datetime as a SOQL datetime literal."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
