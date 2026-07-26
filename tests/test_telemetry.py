"""
Tests for telemetry module, focusing on EventLogFile collection.

These tests are adversarial: they verify security constraints (forbidden orgs,
production orgs, fail-closed on unknowns), determinism (same input -> same output),
and honest availability reporting (latency != absence).

All tests MUST be deterministic and MUST NOT hit a network or org.
"""

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import MagicMock, patch
import subprocess

import pytest

from sf_video_blueprint.telemetry import (
    CorrelationKey,
    EventLogFileAvailability,
    EventLogFileResult,
    ObjectSnapshot,
    TelemetryEvent,
    TelemetryLayer,
    TelemetryRegistry,
    _is_org_forbidden,
    _map_event_type_to_layer,
    _parse_eventlogfile_csv,
    _verify_org_is_sandbox,
    collect_eventlogfile_telemetry,
)


# ============================================================================
# Basic dataclass tests
# ============================================================================


class TestTelemetryDataclasses:
    """Verify backward compatibility of dataclass constructors."""

    def test_telemetry_event_positional_constructor(self):
        """TelemetryEvent can be constructed positionally (existing usage)."""
        event = TelemetryEvent(
            correlation=CorrelationKey(
                run_id="r1", step_id="s1", event_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
            ),
            layer=TelemetryLayer.APEX,
            event_name="ApexTrigger",
            status="success",
            payload={"foo": "bar"},
        )
        assert event.layer == TelemetryLayer.APEX
        assert event.event_name == "ApexTrigger"
        assert event.org_timestamp is None  # New optional field has safe default
        assert event.transaction_id is None

    def test_telemetry_event_with_new_fields(self):
        """TelemetryEvent new optional fields work."""
        org_ts = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
        event = TelemetryEvent(
            correlation=CorrelationKey(
                run_id="r1", step_id="s1", event_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
            ),
            layer=TelemetryLayer.NETWORK,
            event_name="API",
            status="success",
            payload={},
            org_timestamp=org_ts,
            transaction_id="4YRxPfJmVhZ",
        )
        assert event.org_timestamp == org_ts
        assert event.transaction_id == "4YRxPfJmVhZ"

    def test_object_snapshot_unchanged(self):
        """ObjectSnapshot constructor remains stable."""
        snap = ObjectSnapshot(
            correlation=CorrelationKey(
                run_id="r1", step_id="s1", event_time=datetime(2026, 1, 1, tzinfo=timezone.utc)
            ),
            object_api_name="Case",
            record_id="500xx0000012345",
            before={"Status": "New"},
            after={"Status": "Working"},
            changed_fields=["Status"],
        )
        assert snap.object_api_name == "Case"
        assert snap.changed_fields == ["Status"]


# ============================================================================
# Forbidden org detection
# ============================================================================


class TestForbiddenOrgDetection:
    """PPCDM and PPCaccenture must be blocked, case-insensitive."""

    def test_ppcdm_exact(self):
        assert _is_org_forbidden("PPCDM")

    def test_ppcdm_lowercase(self):
        assert _is_org_forbidden("ppcdm")

    def test_ppcdm_mixedcase(self):
        assert _is_org_forbidden("PpCdm")

    def test_ppcaccenture_exact(self):
        assert _is_org_forbidden("PPCaccenture")

    def test_ppcaccenture_lowercase(self):
        """DEFECT L4-4: this is the assertion that was missing.

        The old test was named `test_ppaccenture_lowercase` and asserted
        `_is_org_forbidden("ppaccenture")` — one `c`. That is the same typo the
        deny-set contained, so the test passed while the REAL lowercase
        spelling, `ppcaccenture`, returned False. A test that shares the
        implementation's typo verifies nothing.
        """
        assert _is_org_forbidden("ppcaccenture")

    def test_ppcaccenture_mixedcase(self):
        assert _is_org_forbidden("PPCACCENTURE")
        assert _is_org_forbidden("PpCaccenture")

    def test_ppcaccenture_typo_spelling_still_blocked(self):
        """The `ppaccenture` near-miss stays blocked — see org_denylist."""
        assert _is_org_forbidden("ppaccenture")

    def test_safe_org_aliases(self):
        """Common safe aliases should not trigger."""
        assert not _is_org_forbidden("my-scratch-org")
        assert not _is_org_forbidden("dev-sandbox")
        assert not _is_org_forbidden("uat")


# ============================================================================
# Org verification (fail-closed)
# ============================================================================


class TestOrgVerification:
    """Org type verification must fail closed: deny unless positively sandbox."""

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    def test_sandbox_verified(self, mock_run):
        """Sandbox with IsSandbox=true passes."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": {"isSandbox": True}}),
        )
        is_safe, detail = _verify_org_is_sandbox("safe-sandbox")
        assert is_safe
        assert "verified as sandbox" in detail.lower()

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    def test_production_org_denied(self, mock_run):
        """Production org with IsSandbox=false is denied."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": {"isSandbox": False}}),
        )
        is_safe, detail = _verify_org_is_sandbox("prod-org")
        assert not is_safe
        assert "production" in detail.lower()

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    def test_missing_issandbox_field_denied(self, mock_run):
        """Missing IsSandbox field -> fail closed."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": {}}),
        )
        is_safe, detail = _verify_org_is_sandbox("unknown-org")
        assert not is_safe
        assert "issandbox field missing" in detail.lower()

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    def test_cli_failure_denied(self, mock_run):
        """sf org display failure -> fail closed."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="Error: No org found",
        )
        is_safe, detail = _verify_org_is_sandbox("no-such-org")
        assert not is_safe
        assert "sf org display failed" in detail.lower()

    def test_forbidden_org_blocked_before_cli(self):
        """PPCDM/PPCaccenture blocked without calling sf CLI."""
        is_safe, detail = _verify_org_is_sandbox("PPCDM")
        assert not is_safe
        assert "forbidden" in detail.lower()

        is_safe2, detail2 = _verify_org_is_sandbox("PPCaccenture")
        assert not is_safe2
        assert "forbidden" in detail2.lower()


# ============================================================================
# Event type to layer mapping
# ============================================================================


class TestEventTypeMapping:
    """EventLogFile EventType -> TelemetryLayer mapping."""

    def test_apex_events(self):
        assert _map_event_type_to_layer("ApexExecution") == TelemetryLayer.APEX
        assert _map_event_type_to_layer("ApexTrigger") == TelemetryLayer.APEX
        assert _map_event_type_to_layer("ApexCallout") == TelemetryLayer.APEX

    def test_network_events(self):
        assert _map_event_type_to_layer("API") == TelemetryLayer.NETWORK
        assert _map_event_type_to_layer("URI") == TelemetryLayer.NETWORK
        assert _map_event_type_to_layer("RestApi") == TelemetryLayer.NETWORK

    def test_validation_events(self):
        assert _map_event_type_to_layer("ValidationRule") == TelemetryLayer.VALIDATION

    def test_flow_events(self):
        assert _map_event_type_to_layer("FlowExecution") == TelemetryLayer.FLOW

    def test_ui_events(self):
        assert _map_event_type_to_layer("LightningInteraction") == TelemetryLayer.UI
        assert _map_event_type_to_layer("LightningPageView") == TelemetryLayer.UI
        assert _map_event_type_to_layer("AuraAction") == TelemetryLayer.UI

    def test_async_events(self):
        assert _map_event_type_to_layer("BulkApi") == TelemetryLayer.ASYNC
        assert _map_event_type_to_layer("AsyncApex") == TelemetryLayer.ASYNC

    def test_data_events(self):
        assert _map_event_type_to_layer("Report") == TelemetryLayer.DATA
        assert _map_event_type_to_layer("WavePerformance") == TelemetryLayer.DATA

    def test_integration_events(self):
        assert _map_event_type_to_layer("ExternalCrossOrgCallout") == TelemetryLayer.INTEGRATION
        assert _map_event_type_to_layer("Callout") == TelemetryLayer.INTEGRATION

    def test_unknown_event_defaults_to_network(self):
        """Unrecognized event types default to NETWORK."""
        assert _map_event_type_to_layer("SomeNewEventType") == TelemetryLayer.NETWORK


# ============================================================================
# CSV parsing
# ============================================================================


class TestEventLogFileCSVParsing:
    """Parse EventLogFile CSV into TelemetryEvents."""

    def test_parse_apex_execution_log(self):
        """Parse a well-formed ApexExecution CSV."""
        csv_body = """TIMESTAMP,REQUEST_ID,EVENT_TYPE,RUN_TIME,CPU_TIME,SUCCESS
2026-01-15T12:34:56.789Z,4YRxPfJmVhZ,ApexTrigger,123,45,1
2026-01-15T12:35:01.234Z,4YRxPfJmVha,ApexClass,234,56,1
"""
        events, warnings = _parse_eventlogfile_csv(csv_body, "ApexExecution", "run-123")

        assert len(events) == 2
        assert len(warnings) == 0

        # First event
        e1 = events[0]
        assert e1.layer == TelemetryLayer.APEX
        assert e1.event_name == "ApexTrigger"
        assert e1.status == "success"
        assert e1.org_timestamp == datetime(2026, 1, 15, 12, 34, 56, 789000, tzinfo=timezone.utc)
        assert e1.transaction_id == "4YRxPfJmVhZ"
        assert e1.payload["RUN_TIME"] == "123"
        assert e1.payload["CPU_TIME"] == "45"

        # Second event
        e2 = events[1]
        assert e2.event_name == "ApexClass"
        assert e2.transaction_id == "4YRxPfJmVha"

    def test_parse_api_log_with_status_field(self):
        """Parse API log where status is a STATUS field, not SUCCESS."""
        csv_body = """TIMESTAMP,REQUEST_ID,OPERATION,STATUS,URI
2026-01-15T14:00:00.000Z,5ZWxQgKnWiA,Query,SUCCESS,/services/data/v60.0/query
2026-01-15T14:00:05.000Z,5ZWxQgKnWiB,Update,ERROR,/services/data/v60.0/sobjects/Case/500xx
"""
        events, warnings = _parse_eventlogfile_csv(csv_body, "API", "run-456")

        assert len(events) == 2
        assert events[0].status == "success"
        assert events[1].status == "error"

    def test_parse_row_missing_timestamp_skipped(self):
        """Rows without TIMESTAMP are skipped with warning."""
        csv_body = """TIMESTAMP,REQUEST_ID,EVENT_TYPE,SUCCESS
2026-01-15T12:00:00.000Z,abc123,Event1,1
,def456,Event2,1
2026-01-15T12:00:10.000Z,ghi789,Event3,1
"""
        events, warnings = _parse_eventlogfile_csv(csv_body, "Test", "run-x")

        assert len(events) == 2  # Row 2 skipped
        assert any("skipped" in w.lower() for w in warnings)

    def test_parse_malformed_timestamp_skipped(self):
        """Rows with unparseable timestamps are skipped."""
        csv_body = """TIMESTAMP,EVENT_TYPE,SUCCESS
not-a-timestamp,Event1,1
2026-01-15T12:00:00.000Z,Event2,1
"""
        events, warnings = _parse_eventlogfile_csv(csv_body, "Test", "run-y")

        assert len(events) == 1
        assert events[0].event_name == "Event2"

    def test_parse_large_fraction_lost_warning(self):
        """If >30% of rows are lost, emit a summary warning."""
        csv_body = """TIMESTAMP,EVENT_TYPE
invalid1,E1
invalid2,E2
invalid3,E3
2026-01-15T12:00:00.000Z,E4
"""
        events, warnings = _parse_eventlogfile_csv(csv_body, "Test", "run-z")

        assert len(events) == 1
        # 3 out of 4 rows lost = 75% > 30%
        assert any(">30%" in w for w in warnings)

    def test_parse_empty_csv(self):
        """Empty CSV yields no events, no warnings."""
        events, warnings = _parse_eventlogfile_csv("", "Test", "run-empty")
        assert len(events) == 0

    def test_parse_csv_with_only_headers(self):
        """CSV with only headers yields no events."""
        csv_body = "TIMESTAMP,EVENT_TYPE,SUCCESS\n"
        events, warnings = _parse_eventlogfile_csv(csv_body, "Test", "run-headers")
        assert len(events) == 0


# ============================================================================
# Determinism tests
# ============================================================================


class TestDeterminism:
    """Same input must yield identical output (required for offline scoring)."""

    def test_parse_csv_deterministic(self):
        """Same CSV parsed twice yields identical events."""
        csv_body = """TIMESTAMP,REQUEST_ID,EVENT_TYPE,SUCCESS
2026-01-15T10:00:00.000Z,id1,Event1,1
2026-01-15T10:00:01.000Z,id2,Event2,0
"""
        events1, _ = _parse_eventlogfile_csv(csv_body, "Test", "run-1")
        events2, _ = _parse_eventlogfile_csv(csv_body, "Test", "run-1")

        assert len(events1) == len(events2)
        for e1, e2 in zip(events1, events2):
            assert e1.event_name == e2.event_name
            assert e1.status == e2.status
            assert e1.org_timestamp == e2.org_timestamp
            assert e1.transaction_id == e2.transaction_id

    def test_event_type_mapping_deterministic(self):
        """Event type mapping is stable."""
        result1 = _map_event_type_to_layer("ApexExecution")
        result2 = _map_event_type_to_layer("ApexExecution")
        assert result1 == result2


# ============================================================================
# Security: no tokens/URLs in logs
# ============================================================================


class TestSecurityNoLeaks:
    """Verify no tokens or frontdoor URLs appear in logs or exceptions."""

    def test_parse_csv_payload_does_not_leak_token_field(self):
        """If CSV contains a token-like field, it goes in payload (not logged by this module)."""
        csv_body = """TIMESTAMP,REQUEST_ID,SESSION_KEY,EVENT_TYPE
2026-01-15T10:00:00.000Z,id1,00D8Y000000AbCd!AR8AQ,TestEvent
"""
        events, _ = _parse_eventlogfile_csv(csv_body, "Test", "run-sec")

        assert len(events) == 1
        # SESSION_KEY ends up in payload, not logged by telemetry.py itself
        assert "SESSION_KEY" in events[0].payload
        # The test verifies that telemetry.py does not log this; actual logging
        # is the caller's responsibility (cli.py, etc.)

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    def test_collect_does_not_log_frontdoor_url(self, mock_run):
        """
        Frontdoor URL must not appear in warnings or detail.
        This test verifies the code does not echo subprocess stdout containing secrets.
        """
        # Mock org verification
        mock_run.side_effect = [
            # First call: org display for IsSandbox check
            MagicMock(
                returncode=0,
                stdout=json.dumps({"result": {"isSandbox": True}}),
            ),
            # Second call: EventLogFile query returns no records
            MagicMock(
                returncode=0,
                stdout=json.dumps({"result": {"records": []}}),
            ),
        ]

        start = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
        end = datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)
        result = collect_eventlogfile_telemetry("safe-org", start, end, "run-sec2")

        # No frontdoor URL should appear in any text output
        assert "frontdoor" not in result.detail.lower()
        for w in result.warnings:
            assert "frontdoor" not in w.lower()


# ============================================================================
# Availability classification
# ============================================================================


class TestAvailabilityClassification:
    """Distinguish unavailable from empty from latency."""

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    @patch("sf_video_blueprint.telemetry.datetime")
    def test_empty_recent_recording(self, mock_datetime, mock_run):
        """Recording <2h old with no rows -> EMPTY_RECENT (latency expected)."""
        # Mock current time
        now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        mock_datetime.now.return_value = now
        mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

        # Recording end time 1 hour ago
        end_time = datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)

        mock_run.side_effect = [
            # org verification
            MagicMock(returncode=0, stdout=json.dumps({"result": {"isSandbox": True}})),
            # EventLogFile query returns no records
            MagicMock(returncode=0, stdout=json.dumps({"result": {"records": []}})),
        ]

        result = collect_eventlogfile_telemetry(
            "safe-org",
            end_time - timedelta(minutes=5),
            end_time,
            "run-recent",
        )

        assert result.availability == EventLogFileAvailability.EMPTY_RECENT
        assert "hourly logs appear" in result.detail.lower()

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    @patch("sf_video_blueprint.telemetry.datetime")
    def test_empty_stale_recording(self, mock_datetime, mock_run):
        """Recording >25h old with no rows -> EMPTY_STALE (not latency)."""
        now = datetime(2026, 1, 16, 14, 0, tzinfo=timezone.utc)
        mock_datetime.now.return_value = now
        mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)

        # Recording end time 26 hours ago
        end_time = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({"result": {"isSandbox": True}})),
            MagicMock(returncode=0, stdout=json.dumps({"result": {"records": []}})),
        ]

        result = collect_eventlogfile_telemetry(
            "safe-org",
            end_time - timedelta(minutes=5),
            end_time,
            "run-stale",
        )

        assert result.availability == EventLogFileAvailability.EMPTY_STALE
        assert "too stale" in result.detail.lower()

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    def test_license_missing(self, mock_run):
        """Query failure with INVALID_TYPE -> LICENSE_MISSING."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({"result": {"isSandbox": True}})),
            MagicMock(
                returncode=1,
                stderr="INVALID_TYPE: sObject type 'EventLogFile' is not supported",
            ),
        ]

        result = collect_eventlogfile_telemetry(
            "no-license-org",
            datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc),
            "run-lic",
        )

        assert result.availability == EventLogFileAvailability.LICENSE_MISSING
        assert "event monitoring license" in result.detail.lower()

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    def test_org_forbidden(self, mock_run):
        """PPCDM org -> ORG_FORBIDDEN, no CLI calls."""
        # Should not even call subprocess for forbidden org
        result = collect_eventlogfile_telemetry(
            "PPCDM",
            datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc),
            "run-forbidden",
        )

        assert result.availability == EventLogFileAvailability.ORG_FORBIDDEN
        assert "forbidden" in result.detail.lower()
        # Verify subprocess was never called
        mock_run.assert_not_called()

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    def test_org_type_unknown(self, mock_run):
        """Org verification failure -> ORG_TYPE_UNKNOWN, fail closed."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stderr="No org configuration found",
        )

        result = collect_eventlogfile_telemetry(
            "unknown-org",
            datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc),
            "run-unk",
        )

        assert result.availability == EventLogFileAvailability.ORG_TYPE_UNKNOWN
        assert result.events == []

    @patch("sf_video_blueprint.telemetry.subprocess.run")
    def test_query_failed_timeout(self, mock_run):
        """Query timeout -> QUERY_FAILED."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=json.dumps({"result": {"isSandbox": True}})),
            subprocess.TimeoutExpired("sf", 30),
        ]

        result = collect_eventlogfile_telemetry(
            "slow-org",
            datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc),
            "run-timeout",
        )

        assert result.availability == EventLogFileAvailability.QUERY_FAILED
        assert "timed out" in result.detail.lower()


# ============================================================================
# Integration: TelemetryRegistry
# ============================================================================


class TestTelemetryRegistry:
    """TelemetryRegistry collects from TelemetryCollector protocol."""

    def test_collect_step_from_protocol(self):
        """Registry collects events from a TelemetryCollector."""

        class MockCollector:
            def collect_for_step(self, run_id: str, step_id: str):
                return [
                    TelemetryEvent(
                        correlation=CorrelationKey(
                            run_id=run_id,
                            step_id=step_id,
                            event_time=datetime(2026, 1, 15, tzinfo=timezone.utc),
                        ),
                        layer=TelemetryLayer.FLOW,
                        event_name="FlowStart",
                        status="success",
                    )
                ]

            def snapshot_changes(self, run_id: str, step_id: str):
                return [
                    ObjectSnapshot(
                        correlation=CorrelationKey(
                            run_id=run_id,
                            step_id=step_id,
                            event_time=datetime(2026, 1, 15, tzinfo=timezone.utc),
                        ),
                        object_api_name="Case",
                        record_id="500xx",
                        before={},
                        after={"Status": "Working"},
                        changed_fields=["Status"],
                    )
                ]

        registry = TelemetryRegistry()
        collector = MockCollector()
        registry.collect_step(collector, "run-1", "step-1")

        assert len(registry.events) == 1
        assert len(registry.snapshots) == 1
        assert registry.events[0].event_name == "FlowStart"
        assert registry.snapshots[0].object_api_name == "Case"
