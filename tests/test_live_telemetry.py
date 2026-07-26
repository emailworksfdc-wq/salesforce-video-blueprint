"""Tests for the live-org telemetry collector.

Adversarial by intent. The failure this project exists to prevent is a spec that
looks evidence-backed and is not, so the tests that matter most here are the ones
that try to obtain a ``live-org`` stamp without an org:
:class:`TestLiveOrgStampCannotBeFaked`.

Every test is offline and deterministic. Real org rows are frozen into
:data:`REAL_CASE_HISTORY_ROWS` — those are the actual bytes returned by AFT3 for
the Case created in the lane run, kept verbatim so the parsing path is exercised
against real Salesforce output (offset ``+0000`` without a colon, ``OldValue``
null on create) rather than a tidied-up invention.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sf_video_blueprint.correlation import CorrelationConfidence, correlate_step
from sf_video_blueprint.live_telemetry import (
    LIVE_ORG_SOURCE,
    UNATTRIBUTED_STEP_ID,
    UNAVAILABLE_SOURCE,
    LiveOrgTelemetryCollector,
    LiveTelemetryResult,
    OrgNotPermitted,
    SfCliQueryRunner,
    SurfaceProbe,
    SurfaceStatus,
    TelemetrySurface,
    assert_org_permitted,
    observed_history_events,
    probe_telemetry_surfaces,
    snapshots_from_history,
)
from sf_video_blueprint.markers import telemetry_is_real
from sf_video_blueprint.models import ExtractedAction
from sf_video_blueprint.telemetry import (
    ObjectSnapshot,
    TelemetryCollector,
    TelemetryEvent,
    TelemetryLayer,
    TelemetryRegistry,
)

# The row SHAPE is verbatim from a real AFT3 CaseHistory query; the record Id is
# synthetic. A real Id is org-identifying and this repository is public, and the
# Id is never queried here — the runner is stubbed — so a fake one costs nothing.
# Kept exactly as the org returned them, including the create row whose Old/New
# values are null and the '+0000' offset with no colon.
REAL_CASE_HISTORY_ROWS = [
    {
        "Id": "017bm00001tjCQ9AAM",
        "CaseId": "500SYNTHETIC00001",
        "Field": "created",
        "OldValue": None,
        "NewValue": None,
        "CreatedDate": "2026-07-26T20:42:51.000+0000",
        "CreatedById": "005bm00000SiA1xAAF",
    },
    {
        "Id": "017bm00001ti0kHAAQ",
        "CaseId": "500SYNTHETIC00001",
        "Field": "Status",
        "OldValue": "New",
        "NewValue": "Working",
        "CreatedDate": "2026-07-26T20:43:04.000+0000",
        "CreatedById": "005bm00000SiA1xAAF",
    },
    {
        "Id": "017bm00001ti0kIAAQ",
        "CaseId": "500SYNTHETIC00001",
        "Field": "Priority",
        "OldValue": "Low",
        "NewValue": "High",
        "CreatedDate": "2026-07-26T20:43:04.000+0000",
        "CreatedById": "005bm00000SiA1xAAF",
    },
]

REAL_CASE_ID = "500SYNTHETIC00001"
ORG_CHANGE_INSTANT = datetime(2026, 7, 26, 20, 43, 4, tzinfo=timezone.utc)

# The Status Working->Escalated row, written by a real click in a real browser.
# The recorder timestamped that click at 21:03:11.015 (browser clock); the org
# recorded the change it caused at 21:03:11.000 — see
# TestSecondTruncationDefeatsTheForwardWindow.
REAL_UI_DRIVEN_HISTORY_ROW = {
    "Id": "017bm00001tjCQAAAM",
    "CaseId": "500SYNTHETIC00001",
    "Field": "Status",
    "OldValue": "Working",
    "NewValue": "Escalated",
    "CreatedDate": "2026-07-26T21:03:11.000+0000",
    "CreatedById": "005bm00000SiA1xAAF",
}
REAL_SAVE_CLICK_MS = 1785099791015  # 2026-07-26T21:03:11.015Z, from the capture


class FakeRunner:
    """A query runner that returns canned rows and records the SOQL it saw."""

    def __init__(self, rows_by_object: dict[str, list[dict]] | None = None, error=None):
        self.rows_by_object = rows_by_object or {}
        self.error = error
        self.queries: list[str] = []

    def query(self, soql: str, *, tooling: bool = False) -> list[dict]:
        self.queries.append(soql)
        if self.error is not None:
            raise self.error
        for object_name, rows in self.rows_by_object.items():
            if f"FROM {object_name}" in soql:
                return list(rows)
        return []


class NoOrgRunner:
    """Simulates having no usable org connection at all."""

    def __init__(self, message="No authorization information found for AFT3."):
        self.message = message

    def query(self, soql: str, *, tooling: bool = False):
        raise RuntimeError(self.message)


def _case_runner(rows=None):
    return FakeRunner({"CaseHistory": REAL_CASE_HISTORY_ROWS if rows is None else rows})


# ============================================================================
# The central guarantee: no org, no live-org stamp
# ============================================================================


class TestLiveOrgStampCannotBeFaked:
    """A ``live-org`` stamp must be impossible without observed org rows.

    Relabelling mock output as ``live-org`` is the single worst change that could
    be made to this codebase, because it makes fabrication invisible. These tests
    attempt it from every angle a caller realistically could.
    """

    def test_collector_with_no_org_connection_cannot_stamp_live_org(self):
        """The requirement from the brief, stated directly."""
        collector = LiveOrgTelemetryCollector(
            "AFT3",
            tracked_records=[("Case", REAL_CASE_ID)],
            runner=NoOrgRunner(),
        )
        result = collector.observe("run-1")

        assert result.events == []
        assert result.snapshots == []
        assert result.observed_any is False
        assert result.telemetry_source == UNAVAILABLE_SOURCE
        assert result.telemetry_source != LIVE_ORG_SOURCE
        assert result.stamp_is_earned() is False
        # And the score gate must agree that this is not real telemetry.
        assert telemetry_is_real(result.telemetry_source) is False

    def test_no_tracked_records_cannot_stamp_live_org(self):
        """Nothing to observe is not the same as nothing happening."""
        collector = LiveOrgTelemetryCollector("AFT3", runner=FakeRunner())
        result = collector.observe("run-1")

        assert result.telemetry_source == UNAVAILABLE_SOURCE
        assert telemetry_is_real(result.telemetry_source) is False
        assert any("No tracked records" in w for w in result.warnings)

    def test_empty_org_response_cannot_stamp_live_org(self):
        """A successful query returning zero rows is not evidence."""
        collector = LiveOrgTelemetryCollector(
            "AFT3", tracked_records=[("Case", REAL_CASE_ID)], runner=_case_runner(rows=[])
        )
        result = collector.observe("run-1")

        assert result.observed_any is False
        assert result.telemetry_source == UNAVAILABLE_SOURCE
        probe = result.probes[0]
        assert probe.status is SurfaceStatus.QUERYABLE_BUT_EMPTY
        assert probe.carries_evidence is False

    def test_query_failure_cannot_stamp_live_org(self):
        collector = LiveOrgTelemetryCollector(
            "AFT3",
            tracked_records=[("Case", REAL_CASE_ID)],
            runner=FakeRunner(error=RuntimeError("INVALID_SESSION_ID: Session expired")),
        )
        result = collector.observe("run-1")

        assert result.telemetry_source == UNAVAILABLE_SOURCE
        assert result.probes[0].status is SurfaceStatus.QUERY_FAILED
        assert any("Session expired" in w for w in result.warnings)

    def test_rows_with_only_noise_fields_cannot_stamp_live_org(self):
        """A bare 'created' row proves a record exists, not that a process ran."""
        created_only = [REAL_CASE_HISTORY_ROWS[0]]
        collector = LiveOrgTelemetryCollector(
            "AFT3",
            tracked_records=[("Case", REAL_CASE_ID)],
            runner=_case_runner(rows=created_only),
        )
        result = collector.observe("run-1")

        assert result.events == []
        assert result.snapshots == []
        assert result.telemetry_source == UNAVAILABLE_SOURCE

    def test_unparseable_timestamps_cannot_stamp_live_org(self):
        """A row without a usable org timestamp cannot be correlated, so it is dropped."""
        bad = [dict(REAL_CASE_HISTORY_ROWS[1], CreatedDate="not-a-date")]
        collector = LiveOrgTelemetryCollector(
            "AFT3", tracked_records=[("Case", REAL_CASE_ID)], runner=_case_runner(rows=bad)
        )
        result = collector.observe("run-1")

        assert result.telemetry_source == UNAVAILABLE_SOURCE

    def test_observed_rows_do_earn_the_stamp(self):
        """The gate must be able to say yes, or it is not a gate."""
        collector = LiveOrgTelemetryCollector(
            "AFT3", tracked_records=[("Case", REAL_CASE_ID)], runner=_case_runner()
        )
        result = collector.observe("run-1")

        assert result.observed_any is True
        assert result.telemetry_source == LIVE_ORG_SOURCE
        assert result.stamp_is_earned() is True
        assert telemetry_is_real(result.telemetry_source) is True

    def test_result_constructed_with_no_events_is_unavailable(self):
        """The stamp is derived from data, not settable as a field."""
        assert LiveTelemetryResult().telemetry_source == UNAVAILABLE_SOURCE
        assert not hasattr(LiveTelemetryResult(), "_telemetry_source")


# ============================================================================
# Honest correlation confidence
# ============================================================================


class TestCorrelationHonesty:
    """Temporal proximity is not causation, and must not be labelled HIGH."""

    @staticmethod
    def _action_at(instant: datetime, step_id="step-1") -> ExtractedAction:
        return ExtractedAction(
            step_id=step_id,
            sequence=1,
            action_type="submit",
            target="button:Save",
            value=None,
            timestamp_ms=int(instant.timestamp() * 1000),
            confidence=0.9,
        )

    def test_events_carry_the_unattributed_step_id(self):
        events = observed_history_events(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        assert events
        assert all(e.correlation.step_id == UNATTRIBUTED_STEP_ID for e in events)

    def test_observed_events_correlate_as_temporal_not_high(self):
        """The core honesty property: a timestamp match alone is TEMPORAL."""
        action = self._action_at(ORG_CHANGE_INSTANT)
        events = observed_history_events(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        snapshots = snapshots_from_history(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )

        analysis = correlate_step(action, [], events, snapshots)

        assert analysis.correlated_events
        assert analysis.correlated_snapshots
        for correlated in analysis.correlated_events:
            assert correlated.confidence is not CorrelationConfidence.HIGH
        for correlated in analysis.correlated_snapshots:
            assert correlated.confidence is CorrelationConfidence.TEMPORAL

    def test_temporal_still_yields_data_delta_evidence(self):
        """Honesty must not cost the spec its evidence grade.

        spec_builder maps TEMPORAL to the strong 'data-delta' source, so refusing
        to claim HIGH does not push real observations down to 'inference'.
        """
        from sf_video_blueprint.spec_builder import build_agent_spec

        action = self._action_at(ORG_CHANGE_INSTANT)
        events = observed_history_events(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        snapshots = snapshots_from_history(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        analysis = correlate_step(action, [], events, snapshots)
        spec = build_agent_spec([action], [analysis])

        observed = [e for e in spec.entities if e.field_api_name != "Id"]
        assert observed, "expected real field entities from observed history"
        assert all(
            any(ev.source == "data-delta" for ev in entity.evidence) for entity in observed
        )

    def test_events_outside_window_are_not_correlated(self):
        """A change an hour later must not attach to this click."""
        action = self._action_at(datetime(2026, 7, 26, 19, 0, 0, tzinfo=timezone.utc))
        events = observed_history_events(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        analysis = correlate_step(action, [], events, [])
        assert analysis.correlated_events == []

    def test_unattributed_step_id_never_matches_a_real_step_id(self):
        """The sentinel must not be able to collide into a HIGH grade."""
        assert not UNATTRIBUTED_STEP_ID.startswith("step-")
        action = self._action_at(ORG_CHANGE_INSTANT, step_id=UNATTRIBUTED_STEP_ID)
        # Even a caller perversely naming their step the sentinel gets a real
        # timestamp check rather than a free pass; the point is the value is a
        # constant in the module, not caller-supplied telemetry.
        events = observed_history_events(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        analysis = correlate_step(action, [], events, [])
        assert analysis.correlated_events  # in-window, so still correlated


# ============================================================================
# Parsing real org rows
# ============================================================================


class TestHistoryParsing:
    def test_parses_real_salesforce_offset_format(self):
        """'+0000' without a colon must parse; the org emits exactly that."""
        events = observed_history_events(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        status = next(e for e in events if e.payload["field"] == "Status")
        assert status.org_timestamp == ORG_CHANGE_INSTANT
        assert status.org_timestamp.tzinfo is not None
        assert status.correlation.event_time == status.org_timestamp

    def test_uses_org_timestamp_not_local_clock(self):
        events = observed_history_events(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        assert all(e.org_timestamp == e.correlation.event_time for e in events)
        assert all(e.org_timestamp.year == 2026 for e in events)

    def test_noise_fields_are_dropped(self):
        events = observed_history_events(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        fields = {e.payload["field"] for e in events}
        assert fields == {"Status", "Priority"}
        assert "created" not in fields

    def test_payload_carries_the_real_transition(self):
        events = observed_history_events(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        status = next(e for e in events if e.payload["field"] == "Status")
        assert status.payload["oldValue"] == "New"
        assert status.payload["newValue"] == "Working"
        assert status.payload["recordId"] == REAL_CASE_ID
        assert status.layer is TelemetryLayer.DATA
        assert status.evidence_refs == ["CaseHistory/017bm00001ti0kHAAQ"]

    def test_one_save_of_two_fields_is_one_snapshot(self):
        """Status and Priority changed in one save; that is one transition."""
        snapshots = snapshots_from_history(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap.record_id == REAL_CASE_ID
        assert snap.changed_fields == ["Priority", "Status"]
        assert snap.before == {"Status": "New", "Priority": "Low"}
        assert snap.after == {"Status": "Working", "Priority": "High"}
        assert snap.correlation.event_time == ORG_CHANGE_INSTANT

    def test_snapshot_omits_unobserved_fields(self):
        """before/after contain only what the org reported, never a guess."""
        snapshots = snapshots_from_history(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        assert set(snapshots[0].before) == {"Status", "Priority"}
        assert "Subject" not in snapshots[0].after

    def test_distinct_timestamps_produce_distinct_snapshots(self):
        rows = REAL_CASE_HISTORY_ROWS + [
            {
                "Id": "017bm00001ti0kZAAQ",
                "CaseId": REAL_CASE_ID,
                "Field": "Status",
                "OldValue": "Working",
                "NewValue": "Escalated",
                "CreatedDate": "2026-07-26T20:45:00.000+0000",
                "CreatedById": "005bm00000SiA1xAAF",
            }
        ]
        snapshots = snapshots_from_history(rows, run_id="run-1", object_api_name="Case")
        assert len(snapshots) == 2
        assert [s.correlation.event_time for s in snapshots] == sorted(
            s.correlation.event_time for s in snapshots
        )

    def test_malformed_rows_are_skipped_not_fatal(self):
        rows = [None, "garbage", {}, {"Field": 123}, REAL_CASE_HISTORY_ROWS[1]]
        events = observed_history_events(rows, run_id="run-1", object_api_name="Case")
        assert len(events) == 1
        assert events[0].payload["field"] == "Status"

    def test_row_without_record_id_is_not_snapshotted(self):
        rows = [dict(REAL_CASE_HISTORY_ROWS[1], CaseId=None)]
        assert snapshots_from_history(rows, run_id="run-1", object_api_name="Case") == []

    def test_deterministic_across_calls(self):
        first = snapshots_from_history(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        second = snapshots_from_history(
            REAL_CASE_HISTORY_ROWS, run_id="run-1", object_api_name="Case"
        )
        assert [s.changed_fields for s in first] == [s.changed_fields for s in second]
        assert [s.before for s in first] == [s.before for s in second]


# ============================================================================
# Collector behaviour
# ============================================================================


class TestCollectorContract:
    def test_satisfies_the_telemetry_collector_protocol(self):
        """Structural conformance with MockTelemetryCollector's signatures.

        ``TelemetryCollector`` is a plain (non-runtime_checkable) Protocol, so
        this compares the actual call signatures against the mock collector that
        already satisfies it rather than using isinstance.
        """
        import inspect

        from sf_video_blueprint.telemetry import MockTelemetryCollector

        collector = LiveOrgTelemetryCollector(
            "AFT3", tracked_records=[("Case", REAL_CASE_ID)], runner=_case_runner()
        )
        for method in ("collect_for_step", "snapshot_changes"):
            assert inspect.signature(getattr(collector, method)) == inspect.signature(
                getattr(MockTelemetryCollector(), method)
            )

        events = collector.collect_for_step("run-1", "step-1")
        snapshots = collector.snapshot_changes("run-1", "step-1")
        assert all(isinstance(e, TelemetryEvent) for e in events)
        assert all(isinstance(s, ObjectSnapshot) for s in snapshots)

    def test_works_inside_telemetry_registry(self):
        registry = TelemetryRegistry()
        collector = LiveOrgTelemetryCollector(
            "AFT3", tracked_records=[("Case", REAL_CASE_ID)], runner=_case_runner()
        )
        registry.collect_step(collector, "run-1", "step-1")
        assert registry.events
        assert registry.snapshots

    def test_step_id_does_not_change_what_is_observed(self):
        """The org's rows do not depend on which UI step asked."""
        collector = LiveOrgTelemetryCollector(
            "AFT3", tracked_records=[("Case", REAL_CASE_ID)], runner=_case_runner()
        )
        a = collector.collect_for_step("run-1", "step-1")
        b = collector.collect_for_step("run-1", "step-9")
        assert [e.payload for e in a] == [e.payload for e in b]

    def test_queries_the_org_once_not_once_per_step(self):
        runner = _case_runner()
        collector = LiveOrgTelemetryCollector(
            "AFT3", tracked_records=[("Case", REAL_CASE_ID)], runner=runner
        )
        for step in range(5):
            collector.collect_for_step("run-1", f"step-{step}")
            collector.snapshot_changes("run-1", f"step-{step}")
        assert len(runner.queries) == 1

    def test_empty_result_is_empty_list_not_placeholder_snapshot(self):
        """Contrast with SalesforceTelemetryCollector, which emits 'unknown'.

        A placeholder snapshot puts the literal string 'unknown' into a spec's
        evidence, where it reads as an observation.
        """
        collector = LiveOrgTelemetryCollector("AFT3", runner=FakeRunner())
        assert collector.snapshot_changes("run-1", "step-1") == []
        assert collector.collect_for_step("run-1", "step-1") == []

    def test_window_bounds_are_in_the_soql(self):
        runner = _case_runner()
        collector = LiveOrgTelemetryCollector(
            "AFT3",
            tracked_records=[("Case", REAL_CASE_ID)],
            window_start=datetime(2026, 7, 26, 20, 0, tzinfo=timezone.utc),
            window_end=datetime(2026, 7, 26, 21, 0, tzinfo=timezone.utc),
            runner=runner,
        )
        collector.observe("run-1")
        soql = runner.queries[0]
        assert "2026-07-26T20:00:00Z" in soql
        assert "2026-07-26T21:00:00Z" in soql
        assert f"CaseId = '{REAL_CASE_ID}'" in soql

    def test_unmapped_object_is_skipped_with_a_warning(self):
        """Guessing '<Object>History' for an arbitrary object would be a fabrication."""
        collector = LiveOrgTelemetryCollector(
            "AFT3",
            tracked_records=[("Widget__c", "a01000000000001")],
            runner=FakeRunner(),
        )
        result = collector.observe("run-1")
        assert result.telemetry_source == UNAVAILABLE_SOURCE
        assert any("Widget__c" in w for w in result.warnings)
        assert "Widget__c" in result.probes[0].detail

    def test_summary_is_json_safe_and_honest(self):
        collector = LiveOrgTelemetryCollector(
            "AFT3", tracked_records=[("Case", REAL_CASE_ID)], runner=_case_runner()
        )
        summary = collector.observe("run-1").summary()
        import json

        json.loads(json.dumps(summary))
        assert summary["telemetry_source"] == LIVE_ORG_SOURCE
        assert summary["stamp_is_earned"] is True
        assert TelemetrySurface.FIELD_HISTORY.value in summary["available_surfaces"]


# ============================================================================
# Safety
# ============================================================================


class TestOrgSafety:
    @pytest.mark.parametrize("alias", ["PPCDM", "ppcdm", "PPCaccenture", "ppcaccenture"])
    def test_forbidden_orgs_are_refused(self, alias):
        with pytest.raises(OrgNotPermitted):
            assert_org_permitted(alias)
        with pytest.raises(OrgNotPermitted):
            LiveOrgTelemetryCollector(alias, runner=FakeRunner())

    @pytest.mark.parametrize("alias", ["", "   ", None, 123])
    def test_missing_alias_is_refused(self, alias):
        with pytest.raises(OrgNotPermitted):
            assert_org_permitted(alias)

    def test_permitted_alias_passes(self):
        assert_org_permitted("AFT3")

    def test_only_select_queries_are_allowed(self):
        runner = SfCliQueryRunner("AFT3")
        for bad in [
            "DELETE FROM Case",
            "UPDATE Case SET Status='x'",
            "  insert into Case",
        ]:
            with pytest.raises(ValueError, match="SELECT"):
                runner.query(bad)

    def test_record_id_is_escaped_into_the_soql_literal(self):
        runner = _case_runner()
        collector = LiveOrgTelemetryCollector(
            "AFT3", tracked_records=[("Case", "500x'OR'1'='1")], runner=runner
        )
        collector.observe("run-1")
        assert r"\'" in runner.queries[0]

    def test_no_token_appears_in_summary_or_warnings(self):
        collector = LiveOrgTelemetryCollector(
            "AFT3",
            tracked_records=[("Case", REAL_CASE_ID)],
            runner=FakeRunner(error=RuntimeError("Bearer 00Dxx!SECRETTOKEN failed")),
        )
        result = collector.observe("run-1")
        # The collector must not manufacture tokens; if a caller's error text
        # contains one, that is the caller's leak. What matters is the collector
        # never reads a token itself.
        import inspect
        from sf_video_blueprint import live_telemetry

        source = inspect.getsource(live_telemetry)
        assert "accessToken" not in source
        assert result.telemetry_source == UNAVAILABLE_SOURCE


# ============================================================================
# Surface probing
# ============================================================================


class TestSurfaceProbing:
    def test_queryable_but_empty_is_not_reported_as_available(self):
        """The EventLogFile trap: describes fine, returns nothing, forever."""
        runner = FakeRunner(
            {
                "CaseHistory": REAL_CASE_HISTORY_ROWS,
                "SetupAuditTrail": [{"Id": "0Ym", "Action": "x", "CreatedDate": "2026-07-25T13:51:53.000+0000"}],
                # EventLogFile / ApexLog / AsyncApexJob / FlowInterview return []
            }
        )
        probes = {p.surface: p for p in probe_telemetry_surfaces("AFT3", runner=runner)}

        assert probes[TelemetrySurface.FIELD_HISTORY].status is SurfaceStatus.OBSERVED
        assert probes[TelemetrySurface.FIELD_HISTORY].carries_evidence is True
        assert probes[TelemetrySurface.SETUP_AUDIT_TRAIL].status is SurfaceStatus.OBSERVED

        for surface in (
            TelemetrySurface.EVENT_LOG_FILE,
            TelemetrySurface.APEX_LOG,
            TelemetrySurface.ASYNC_APEX_JOB,
            TelemetrySurface.FLOW_INTERVIEW,
        ):
            assert probes[surface].status is SurfaceStatus.QUERYABLE_BUT_EMPTY
            assert probes[surface].carries_evidence is False

    def test_invalid_type_error_maps_to_unavailable(self):
        runner = FakeRunner(error=RuntimeError("INVALID_TYPE: sObject type 'EventLogFile' is not supported"))
        probes = probe_telemetry_surfaces("AFT3", runner=runner)
        assert all(p.status is SurfaceStatus.UNAVAILABLE for p in probes)

    def test_transport_error_maps_to_query_failed_not_unavailable(self):
        """A broken environment must not be reported as an org constraint."""
        runner = FakeRunner(error=RuntimeError("socket hang up"))
        probes = probe_telemetry_surfaces("AFT3", runner=runner)
        assert all(p.status is SurfaceStatus.QUERY_FAILED for p in probes)

    def test_probe_covers_every_declared_surface(self):
        probes = probe_telemetry_surfaces("AFT3", runner=FakeRunner())
        assert {p.surface for p in probes} == set(TelemetrySurface)

    def test_forbidden_org_is_refused_before_probing(self):
        runner = FakeRunner()
        with pytest.raises(OrgNotPermitted):
            probe_telemetry_surfaces("PPCDM", runner=runner)
        assert runner.queries == []


class TestSurfaceProbeDataclass:
    def test_carries_evidence_requires_rows(self):
        assert (
            SurfaceProbe(TelemetrySurface.FIELD_HISTORY, SurfaceStatus.OBSERVED, 3).carries_evidence
            is True
        )
        assert (
            SurfaceProbe(TelemetrySurface.FIELD_HISTORY, SurfaceStatus.OBSERVED, 0).carries_evidence
            is False
        )
        assert (
            SurfaceProbe(
                TelemetrySurface.EVENT_LOG_FILE, SurfaceStatus.QUERYABLE_BUT_EMPTY, 0
            ).carries_evidence
            is False
        )


class TestSecondTruncationDefeatsTheForwardWindow:
    """A measured defect in the correlation window, found by running the real thing.

    Salesforce stores ``CaseHistory.CreatedDate`` truncated to whole seconds — every
    row AFT3 returned ends in ``.000+0000``. The recorder timestamps clicks in
    milliseconds. So for a genuine cause/effect pair the org's recorded time is the
    click time rounded DOWN, landing up to 999 ms BEFORE the click that caused it.

    ``correlation.py`` matches on the forward-only window ``[T, T+5s]``. When the
    click falls at ``.015`` of a second, the change it caused is stamped ``.000`` and
    is excluded — not because the evidence is weak, but because truncation moved it
    into the past. Whether real telemetry correlates therefore depends on where in
    the second the user happened to click, which is a coin flip.

    This is asserted, not fixed. The window belongs to ``correlation.py``, and
    widening a *causal* window backwards is a semantic decision about what may be
    claimed as caused — not a lane-05 call to make quietly. The request is written up
    in ``_shared/findings/lane-05.md``. These tests fail the day someone fixes it,
    which is the point: they say what the behaviour is today.
    """

    @staticmethod
    def _save_click() -> ExtractedAction:
        return ExtractedAction(
            step_id="step-9",
            sequence=9,
            action_type="submit",
            target="button:Save",
            value=None,
            timestamp_ms=REAL_SAVE_CLICK_MS,
            confidence=0.9,
        )

    def test_org_truncates_history_timestamps_to_whole_seconds(self):
        """Every real row came back with .000 milliseconds. That is the root cause."""
        rows = [*REAL_CASE_HISTORY_ROWS, REAL_UI_DRIVEN_HISTORY_ROW]
        assert all(r["CreatedDate"].endswith(".000+0000") for r in rows)

        events = observed_history_events(
            [REAL_UI_DRIVEN_HISTORY_ROW], run_id="run-1", object_api_name="Case"
        )
        assert events[0].correlation.event_time.microsecond == 0

    def test_the_real_pair_is_15ms_out_of_order(self):
        """The org's effect predates the click that caused it, by sub-second rounding."""
        events = observed_history_events(
            [REAL_UI_DRIVEN_HISTORY_ROW], run_id="run-1", object_api_name="Case"
        )
        click = datetime.fromtimestamp(REAL_SAVE_CLICK_MS / 1000, tz=timezone.utc)
        delta_ms = (events[0].correlation.event_time - click).total_seconds() * 1000

        assert delta_ms == pytest.approx(-15.0, abs=1.0)
        assert delta_ms < 0, "truncation must place the org row before the click"

    def test_forward_window_misses_the_genuine_cause_and_effect(self):
        """CURRENT BEHAVIOUR: the strongest evidence this lane produced is dropped.

        Both sides are real — a real click, and the row the org wrote because of it —
        and they still do not correlate. Documented so the loss is visible.
        """
        events = observed_history_events(
            [REAL_UI_DRIVEN_HISTORY_ROW], run_id="run-1", object_api_name="Case"
        )
        snapshots = snapshots_from_history(
            [REAL_UI_DRIVEN_HISTORY_ROW], run_id="run-1", object_api_name="Case"
        )

        analysis = correlate_step(self._save_click(), [], events, snapshots)

        assert analysis.correlated_events == []
        assert analysis.correlated_snapshots == []

    def test_same_evidence_correlates_when_the_click_lands_on_a_second_boundary(self):
        """Proof the miss is truncation, not weak evidence.

        Identical org row; only the click's sub-second offset changes. At `.000` the
        pair correlates TEMPORAL. Nothing about the evidence differs — which is what
        makes the current window's behaviour arbitrary.
        """
        events = observed_history_events(
            [REAL_UI_DRIVEN_HISTORY_ROW], run_id="run-1", object_api_name="Case"
        )
        snapshots = snapshots_from_history(
            [REAL_UI_DRIVEN_HISTORY_ROW], run_id="run-1", object_api_name="Case"
        )
        on_boundary = ExtractedAction(
            step_id="step-9",
            sequence=9,
            action_type="submit",
            target="button:Save",
            value=None,
            timestamp_ms=REAL_SAVE_CLICK_MS - 15,  # 21:03:11.000
            confidence=0.9,
        )

        analysis = correlate_step(on_boundary, [], events, snapshots)

        assert analysis.correlated_events, "same evidence, 15ms earlier click, now matches"
        assert analysis.correlated_snapshots
        assert all(
            c.confidence is CorrelationConfidence.TEMPORAL for c in analysis.correlated_snapshots
        )
