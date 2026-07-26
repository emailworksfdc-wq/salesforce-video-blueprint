"""Adversarial tests for dom_capture.py — this is the trust boundary.

The recorder is untrusted. This test suite verifies that the parser:
1. Never aborts on malformed input (truncated/invalid JSON, missing fields)
2. Preserves driver-stamped metadata fields (the Pydantic underscore trap)
3. Orders events correctly (subtle: `seq` restarts per frame, so it's not globally sortable)
4. Detects redaction leaks (security)
5. Handles version mismatches correctly
6. Validates trace integrity without raising

This test file's job is to FALSIFY, not to confirm. The project's history is a
pipeline that reported 100/100 on fabricated data because nothing tested it.
Write tests that would catch that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sf_video_blueprint.dom_capture import (
    CaptureManifest,
    CaptureTrace,
    RawDomEvent,
    load_manifest,
    order_events,
    parse_capture_file,
    redaction_audit,
    synthesize_trace,
    validate_trace,
)


# ============================================================================
# 1. THE PYDANTIC UNDERSCORE-ALIAS TRAP (highest-value test in this file)
# ============================================================================


def test_driver_stamped_fields_survive_parse(tmp_path: Path) -> None:
    """BUG TARGET: Pydantic treats leading-underscore field names as private and
    will SILENTLY DROP them unless explicitly handled via Field(alias=...).

    If the parser drops `_ingest_seq`, `order_events` degrades to page-controlled
    ordering with NO ERROR. This is the highest-value test in the file.
    """
    jsonl_path = tmp_path / "capture.jsonl"
    event_data = {
        "v": 1,
        "seq": 10,
        "t": 1737830000123,
        "type": "click",
        "url": "https://test.my.salesforce.com",
        "frame_path": [],
        "selectors": {
            "test_id": None,
            "aria": None,
            "role_name": None,
            "label_for": None,
            "sf_field": None,
            "css_path": "button.test",
            "text": "Click",
            "xpath": None,
        },
        "element": {
            "tag": "button",
            "type": None,
            "name": None,
            "id": None,
            "classes": [],
            "aria_label": None,
            "text": "Click",
            "is_in_modal": False,
            "modal_label": None,
            "shadow_depth": 0,
        },
        "value": None,
        "value_redacted": False,
        "sf": {
            "object": None,
            "record_id": None,
            "page_type": "unknown",
            "app": None,
        },
        # Driver-stamped fields — MUST survive onto the model
        "_ingest_seq": 42,
        "_ingest_t": 1737830000999,
        "_frame_url": "https://test.my.salesforce.com/lightning/r/Case/500.../view",
        "_page_index": 3,
    }

    jsonl_path.write_text(json.dumps(event_data) + "\n", encoding="utf-8")
    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1
    event = trace.events[0]

    # Verify both alias access and attribute access
    assert event.ingest_seq == 42, "ingest_seq DROPPED — Pydantic underscore trap not fixed"
    assert event.ingest_t == 1737830000999, "ingest_t DROPPED"
    assert event.frame_url == "https://test.my.salesforce.com/lightning/r/Case/500.../view"
    assert event.page_index == 3

    # Also verify the raw dict roundtrip preserves the underscore names
    reserialised = event.model_dump(by_alias=True)
    assert "_ingest_seq" in reserialised
    assert reserialised["_ingest_seq"] == 42


# ============================================================================
# 2. MALFORMED INPUT MUST NEVER ABORT THE PARSE
# ============================================================================


def test_truncated_final_line_does_not_abort_parse(tmp_path: Path) -> None:
    """The normal Ctrl-C case: last line is incomplete JSON."""
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0},
            "value": None,
            "value_redacted": False,
            "sf": {},
        }) + "\n" +
        '{"v": 1, "seq": 2, "t": 2000, "type": "cl',  # truncated
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1, "First event should parse"
    assert len(trace.skipped_lines) == 1, "Truncated line should be skipped"
    assert trace.skipped_lines[0][0] == 2
    assert "JSON decode error" in trace.skipped_lines[0][1]


def test_invalid_json_mid_file_continues_parsing(tmp_path: Path) -> None:
    """Invalid JSON in the middle should not abort — good events still parse."""
    jsonl_path = tmp_path / "capture.jsonl"
    good_event = {
        "v": 1,
        "seq": 1,
        "t": 1000,
        "type": "click",
        "url": "https://test.my.salesforce.com",
        "frame_path": [],
        "selectors": {},
        "element": {"tag": "button", "classes": [], "shadow_depth": 0},
        "value": None,
        "value_redacted": False,
        "sf": {},
    }

    jsonl_path.write_text(
        json.dumps(good_event) + "\n" +
        "NOT JSON AT ALL\n" +
        json.dumps({**good_event, "seq": 3}) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 2, "Two good events should parse"
    assert len(trace.skipped_lines) == 1
    assert trace.skipped_lines[0][0] == 2
    assert "JSON decode error" in trace.skipped_lines[0][1]


def test_empty_lines_are_ignored(tmp_path: Path) -> None:
    """Empty lines are silently skipped — common in hand-edited files."""
    jsonl_path = tmp_path / "capture.jsonl"
    good_event = {
        "v": 1,
        "seq": 1,
        "t": 1000,
        "type": "click",
        "url": "https://test.my.salesforce.com",
        "frame_path": [],
        "selectors": {},
        "element": {"tag": "button", "classes": [], "shadow_depth": 0},
        "value": None,
        "value_redacted": False,
        "sf": {},
    }

    jsonl_path.write_text(
        json.dumps(good_event) + "\n" +
        "\n" +
        "   \n" +
        json.dumps({**good_event, "seq": 2}) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 2
    assert len(trace.skipped_lines) == 0, "Empty lines should not be recorded as skipped"


def test_missing_required_field_lands_in_skipped_lines(tmp_path: Path) -> None:
    """Valid JSON but missing a required Pydantic field."""
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            "v": 1,
            # missing `seq` — required field
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0},
            "sf": {},
        }) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 0
    assert len(trace.skipped_lines) == 1
    assert trace.skipped_lines[0][0] == 1
    assert "Validation error" in trace.skipped_lines[0][1]


def test_valid_json_but_not_an_object_lands_in_skipped_lines(tmp_path: Path) -> None:
    """Valid JSON but not a dict — should skip."""
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        '"just a string"\n' +
        '[1, 2, 3]\n' +
        '42\n',
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 0
    # All three lines should fail validation (not objects)
    assert len(trace.skipped_lines) == 3
    # Verify each skipped line has line number and reason
    for line_no, reason in trace.skipped_lines:
        assert isinstance(line_no, int)
        assert 1 <= line_no <= 3
        assert reason and isinstance(reason, str)


# ============================================================================
# 3. VERSION HANDLING
# ============================================================================


def test_version_1_parses_cleanly(tmp_path: Path) -> None:
    """v=1 is the supported version — should parse with no warnings."""
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0},
            "value": None,
            "value_redacted": False,
            "sf": {},
        }) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1
    # Should not warn about version
    assert not any("version" in w.lower() for w in trace.warnings)


def test_version_2_raises_forward_incompatible(tmp_path: Path) -> None:
    """v=2 (newer/forward-incompatible) RAISES — the parser is out of date."""
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            "v": 2,  # Future version
            "seq": 1,
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0},
            "value": None,
            "value_redacted": False,
            "sf": {},
        }) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        parse_capture_file(jsonl_path)

    assert "version 2 is NEWER" in str(exc_info.value)
    assert "out of date" in str(exc_info.value)


def test_version_0_parses_with_warning(tmp_path: Path) -> None:
    """v=0 (older) parses with a warning but does not raise."""
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            "v": 0,  # Older version
            "seq": 1,
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0},
            "value": None,
            "value_redacted": False,
            "sf": {},
        }) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1, "v=0 must parse despite being older"
    # Should warn about older version — verify warning names the version
    version_warnings = [w for w in trace.warnings if "version" in w.lower() and "0" in w]
    assert version_warnings, f"Must warn about version 0, got warnings: {trace.warnings}"
    assert any("older" in w.lower() or "best-effort" in w.lower() for w in version_warnings)


def test_missing_version_field_assumes_supported_version(tmp_path: Path) -> None:
    """Missing v field should warn and assume supported version."""
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            # No "v" field
            "seq": 1,
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0},
            "value": None,
            "value_redacted": False,
            "sf": {},
        }) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1, "Missing v must parse with default version"
    # Warning must be emitted — the operator must know the recorder misbehaved
    assert any("missing" in w.lower() and "v" in w.lower() for w in trace.warnings), \
        f"Must warn about missing v field, got: {trace.warnings}"
    # Event must have parsed successfully
    assert trace.events[0].v in (0, 1), "Event must have a valid v field after defaulting"


# ============================================================================
# 4. ORDER_EVENTS CORRECTNESS (the subtle one)
# ============================================================================


def test_order_events_uses_ingest_seq_when_present(tmp_path: Path) -> None:
    """SUBTLE: `seq` restarts per document/frame, so naive `seq` sorting gives
    WRONG order. Construct events where naive `seq` sorting is wrong but
    `ingest_seq` gives the right order.
    """
    # Event 1: frame A, seq=1, ingest_seq=10
    # Event 2: frame B, seq=1, ingest_seq=5
    # Naive seq sort would say: [Event 1, Event 2] (both seq=1, stable)
    # But ingest_seq says: [Event 2 (5), Event 1 (10)]

    events = [
        RawDomEvent.model_validate({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": ["iframe#frame-a"],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": "A"},
            "value": None,
            "value_redacted": False,
            "sf": {},
            "_ingest_seq": 10,
        }),
        RawDomEvent.model_validate({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": ["iframe#frame-b"],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": "B"},
            "value": None,
            "value_redacted": False,
            "sf": {},
            "_ingest_seq": 5,
        }),
    ]

    ordered = order_events(events)

    # ingest_seq=5 (Event 2) should come before ingest_seq=10 (Event 1)
    assert ordered[0].element.text == "B"
    assert ordered[1].element.text == "A"


def test_order_events_falls_back_to_t_and_seq_when_no_ingest_seq(tmp_path: Path) -> None:
    """When ingest_seq is absent (older driver), fall back to (t, seq)."""
    events = [
        RawDomEvent.model_validate({
            "v": 1,
            "seq": 2,
            "t": 2000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": "Second"},
            "value": None,
            "value_redacted": False,
            "sf": {},
        }),
        RawDomEvent.model_validate({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": "First"},
            "value": None,
            "value_redacted": False,
            "sf": {},
        }),
    ]

    ordered = order_events(events)

    assert ordered[0].element.text == "First"
    assert ordered[1].element.text == "Second"


def test_order_events_mixed_ingest_seq_and_fallback(tmp_path: Path) -> None:
    """Some events have ingest_seq, some don't — ingest_seq events sort first."""
    events = [
        RawDomEvent.model_validate({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": "NoIngest"},
            "value": None,
            "value_redacted": False,
            "sf": {},
            # No _ingest_seq
        }),
        RawDomEvent.model_validate({
            "v": 1,
            "seq": 1,
            "t": 2000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": "HasIngest"},
            "value": None,
            "value_redacted": False,
            "sf": {},
            "_ingest_seq": 5,
        }),
    ]

    ordered = order_events(events)

    # Events with ingest_seq (key = (0, ...)) sort before events without (key = (1, ...))
    assert ordered[0].element.text == "HasIngest"
    assert ordered[1].element.text == "NoIngest"


# ============================================================================
# 5. REDACTION VERIFICATION (security)
# ============================================================================


def test_redaction_audit_flags_leak_when_value_present_and_field_sensitive() -> None:
    """SECURITY: If value is present but field name is sensitive, this is a
    redaction LEAK — the recorder failed to redact.
    """
    trace = synthesize_trace([
        {
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "input",
            "url": "https://test.my.salesforce.com",
            "element": {
                "tag": "input",
                "type": "password",
                "name": "user_password",  # SENSITIVE
                "classes": [],
                "shadow_depth": 0,
            },
            "value": "hunter2",  # LEAKED — should have been null
            "value_redacted": False,  # And flag is false — recorder failed
        }
    ])

    redacted_count, leak_findings = redaction_audit(trace)

    assert redacted_count == 0
    assert len(leak_findings) == 1
    assert "POTENTIAL REDACTION LEAK" in leak_findings[0]
    assert "user_password" in leak_findings[0]


def test_redaction_audit_passes_when_correctly_redacted() -> None:
    """A correctly redacted event (value=None, value_redacted=True) should NOT
    trip the audit.
    """
    trace = synthesize_trace([
        {
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "input",
            "url": "https://test.my.salesforce.com",
            "element": {
                "tag": "input",
                "type": "password",
                "name": "user_password",
                "classes": [],
                "shadow_depth": 0,
            },
            "value": None,  # CORRECTLY REDACTED
            "value_redacted": True,
        }
    ])

    redacted_count, leak_findings = redaction_audit(trace)

    assert redacted_count == 1
    assert len(leak_findings) == 0


def test_validate_trace_includes_redaction_leaks_as_security_findings() -> None:
    """validate_trace should word redaction leaks as SECURITY findings."""
    trace = synthesize_trace([
        {
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "input",
            "url": "https://test.my.salesforce.com",
            "element": {
                "tag": "input",
                "type": "text",
                "name": "credit_card_number",  # SENSITIVE
                "classes": [],
                "shadow_depth": 0,
            },
            "value": "4111111111111111",  # LEAKED
            "value_redacted": False,
        }
    ])

    findings = validate_trace(trace)

    security_findings = [f for f in findings if "SECURITY" in f]
    assert len(security_findings) == 1
    assert "credit_card_number" in security_findings[0]
    assert "Redaction may have FAILED" in security_findings[0]


def test_validate_trace_detects_redaction_flag_leak() -> None:
    """DEFECT A1: Event claims value_redacted=True but value is still present.

    This is the most serious redaction leak: the recorder identified a value as
    sensitive (set the flag) but failed to actually redact it. Must be caught.
    """
    trace = synthesize_trace([
        {
            "seq": 1,
            "type": "input",
            "element": {
                "tag": "input",
                "type": "text",
                "name": "CardNumber",
                "classes": [],
                "shadow_depth": 0,
            },
            "value": "4111111111111111",  # LEAKED — should have been None
            "value_redacted": True,        # Claims redacted but isn't
        }
    ])

    findings = validate_trace(trace)

    # Must have exactly one SECURITY CRITICAL finding
    critical_findings = [f for f in findings if "SECURITY CRITICAL" in f]
    assert len(critical_findings) == 1, f"Expected 1 critical finding, got {len(critical_findings)}: {critical_findings}"

    finding = critical_findings[0]
    assert "value_redacted=True" in finding
    assert "value is still present" in finding
    assert "redaction leak" in finding.lower()

    # MUST NOT include the leaked value itself in the finding text
    assert "4111111111111111" not in finding, \
        "Finding must NOT echo the leaked value — that defeats the purpose of redaction"


def test_validate_trace_legitimate_redacted_event_no_finding() -> None:
    """A correctly redacted event (value_redacted=True, value=None) should NOT trigger."""
    trace = synthesize_trace([
        {
            "seq": 1,
            "type": "input",
            "element": {
                "tag": "input",
                "type": "password",
                "name": "password",
                "classes": [],
                "shadow_depth": 0,
            },
            "value": None,         # Correctly redacted
            "value_redacted": True,
        }
    ])

    findings = validate_trace(trace)

    # Should have no redaction-related findings
    redaction_findings = [f for f in findings if "redact" in f.lower() or "leak" in f.lower()]
    assert len(redaction_findings) == 0, \
        f"Legitimate redacted event should not trigger findings, got: {redaction_findings}"


def test_validate_trace_non_redacted_normal_value_no_finding() -> None:
    """A non-sensitive value with value_redacted=False should NOT trigger."""
    trace = synthesize_trace([
        {
            "seq": 1,
            "type": "input",
            "element": {
                "tag": "input",
                "type": "email",
                "name": "email",
                "classes": [],
                "shadow_depth": 0,
            },
            "value": "alice@example.com",  # Not sensitive
            "value_redacted": False,
        }
    ])

    findings = validate_trace(trace)

    # Should have no redaction-related findings
    redaction_findings = [f for f in findings if "redact" in f.lower() or "leak" in f.lower()]
    assert len(redaction_findings) == 0, \
        f"Non-sensitive value should not trigger findings, got: {redaction_findings}"


# ============================================================================
# 6. VALIDATE_TRACE FINDINGS (integrity checks)
# ============================================================================


def test_validate_trace_manifest_event_count_mismatch() -> None:
    """Manifest reports N events, but parsed M events."""
    trace = synthesize_trace(
        events_data=[{"seq": 1}, {"seq": 2}],
        manifest_data={
            "capture_id": "test-id",
            "org_alias": "test-org",
            "org_instance_url": "https://test.my.salesforce.com",
            "is_sandbox": True,
            "is_scratch": False,
            "started_at": "2026-07-25T00:00:00Z",
            "ended_at": "2026-07-25T00:10:00Z",
            "event_count": 10,  # MISMATCH — we only have 2
            "network_event_count": 0,
            "sink_errors": 0,
        },
    )

    findings = validate_trace(trace)

    assert any("Manifest reports 10 events, but parsed 2 events" in f for f in findings)


def test_validate_trace_sink_errors_nonzero() -> None:
    """Manifest.sink_errors > 0 means recorder failed to write some events."""
    trace = synthesize_trace(
        manifest_data={
            "capture_id": "test-id",
            "org_alias": "test-org",
            "org_instance_url": "https://test.my.salesforce.com",
            "is_sandbox": True,
            "is_scratch": False,
            "started_at": "2026-07-25T00:00:00Z",
            "ended_at": "2026-07-25T00:10:00Z",
            "event_count": 1,
            "network_event_count": 0,
            "sink_errors": 3,  # ERRORS
        },
    )

    findings = validate_trace(trace)

    assert any("Manifest reports 3 sink errors" in f for f in findings)


def test_validate_trace_all_events_identical_timestamp() -> None:
    """All events have identical t — broken clock or synthetic data."""
    trace = synthesize_trace([
        {"seq": 1, "t": 1000},
        {"seq": 2, "t": 1000},
        {"seq": 3, "t": 1000},
    ])

    findings = validate_trace(trace)

    assert any("identical timestamp" in f for f in findings)
    assert any("broken clock or synthetic data" in f for f in findings)


def test_validate_trace_ingest_seq_monotonicity_violation() -> None:
    """ingest_seq must be strictly increasing — if not, report."""
    trace = synthesize_trace([
        {"seq": 1, "_ingest_seq": 1},
        {"seq": 2, "_ingest_seq": 3},
        {"seq": 3, "_ingest_seq": 2},  # VIOLATION — decreased
    ])

    findings = validate_trace(trace)

    assert any("Monotonicity violation" in f for f in findings)
    assert any("ingest_seq=2" in f and "not greater than previous 3" in f for f in findings)


def test_validate_trace_zero_events() -> None:
    """A trace with zero events is CRITICAL."""
    trace = CaptureTrace(events=[], warnings=[], skipped_lines=[])

    findings = validate_trace(trace)

    assert len(findings) == 1
    assert "CRITICAL: Trace contains zero events" in findings[0]


def test_validate_trace_total_parse_failure_warns() -> None:
    """DEFECT A2: Zero events parsed while lines were skipped = 100% data loss.

    This must produce a summary warning — an operator whose recorder drifted
    needs to know that ALL evidence was discarded.
    """
    trace = CaptureTrace(
        events=[],
        warnings=[],
        skipped_lines=[(i, "malformed JSON") for i in range(1, 16)],
    )

    findings = validate_trace(trace)

    # Must have both CRITICAL (zero events) and DATA LOSS findings
    critical_findings = [f for f in findings if "CRITICAL" in f]
    data_loss_findings = [f for f in findings if "DATA LOSS" in f]

    assert len(critical_findings) == 1
    assert len(data_loss_findings) == 1

    finding = data_loss_findings[0]
    assert "Zero events parsed" in finding
    assert "15 lines" in finding
    assert "skipped" in finding
    # Must mention the cause
    assert "drift" in finding.lower() or "mismatch" in finding.lower()


def test_validate_trace_partial_data_loss_warns_above_threshold() -> None:
    """DEFECT A2: Substantial data loss (>=50% skipped) must warn."""
    # 5 events, 5 skipped = 50% loss (at threshold)
    trace = synthesize_trace(
        events_data=[{"seq": i} for i in range(1, 6)]
    )
    trace.skipped_lines = [(i, "bad") for i in range(6, 11)]

    findings = validate_trace(trace)

    data_loss_findings = [f for f in findings if "DATA LOSS" in f]
    assert len(data_loss_findings) == 1

    finding = data_loss_findings[0]
    assert "5 of 10 lines" in finding
    assert "50%" in finding
    assert "More than half" in finding


def test_validate_trace_minor_data_loss_no_warning() -> None:
    """DEFECT A2: Minor loss (<50% skipped) should NOT warn.

    A recorder that emits one bad line among 500 good ones must still produce
    a usable trace without alarming the operator.
    """
    # 10 events, 2 skipped = 17% loss (below threshold)
    trace = synthesize_trace(
        events_data=[{"seq": i} for i in range(1, 11)]
    )
    trace.skipped_lines = [(11, "bad"), (12, "bad")]

    findings = validate_trace(trace)

    data_loss_findings = [f for f in findings if "DATA LOSS" in f]
    assert len(data_loss_findings) == 0, \
        f"Minor loss (<50%) should not warn, but got: {data_loss_findings}"


def test_validate_trace_clean_trace_no_data_loss_warning() -> None:
    """A clean trace with no skipped lines should not produce data-loss warnings."""
    trace = synthesize_trace(
        events_data=[{"seq": i} for i in range(1, 11)]
    )

    findings = validate_trace(trace)

    data_loss_findings = [f for f in findings if "DATA LOSS" in f]
    assert len(data_loss_findings) == 0


def test_validate_trace_frame_path_without_frame_url() -> None:
    """Non-empty frame_path but missing _frame_url (driver metadata missing)."""
    trace = synthesize_trace([
        {
            "seq": 1,
            "frame_path": ["iframe#frame-a"],
            # No _frame_url
        }
    ])

    findings = validate_trace(trace)

    assert any("non-empty frame_path but missing _frame_url" in f for f in findings)


def test_validate_trace_never_raises() -> None:
    """validate_trace NEVER raises — it returns findings."""
    # Even with catastrophically bad data, it should return findings, not raise
    trace = synthesize_trace([
        {
            "seq": 1,
            "t": 1000,
            "value": "secret",
            "value_redacted": False,
            "element": {"name": "password", "tag": "input", "classes": [], "shadow_depth": 0},
        }
    ])

    # Should not raise
    findings = validate_trace(trace)

    assert isinstance(findings, list)
    assert len(findings) > 0  # Should have at least one finding


# ============================================================================
# 7. UNKNOWN EVENT TYPES MUST NOT REJECT THE TRACE
# ============================================================================


def test_unknown_event_type_parses_with_warning(tmp_path: Path) -> None:
    """A type: "dblclick" (unknown) must parse (with warning), because throwing
    away a 10-minute recording over one unknown event type is unacceptable.
    """
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "dblclick",  # UNKNOWN
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0},
            "value": None,
            "value_redacted": False,
            "sf": {},
        }) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1, "Unknown type should still parse"
    assert trace.events[0].type == "dblclick"
    # Should warn about unknown type
    assert any("unknown event type 'dblclick'" in w for w in trace.warnings)
    assert any("not an error" in w for w in trace.warnings)


def test_is_known_type_method() -> None:
    """RawDomEvent.is_known_type() returns False for unknown types."""
    event = RawDomEvent.model_validate({
        "v": 1,
        "seq": 1,
        "t": 1000,
        "type": "dblclick",  # UNKNOWN
        "url": "https://test.my.salesforce.com",
        "frame_path": [],
        "selectors": {},
        "element": {"tag": "button", "classes": [], "shadow_depth": 0},
        "value": None,
        "value_redacted": False,
        "sf": {},
    })

    assert not event.is_known_type()

    # Known type should return True
    known_event = RawDomEvent.model_validate({
        "v": 1,
        "seq": 1,
        "t": 1000,
        "type": "click",
        "url": "https://test.my.salesforce.com",
        "frame_path": [],
        "selectors": {},
        "element": {"tag": "button", "classes": [], "shadow_depth": 0},
        "value": None,
        "value_redacted": False,
        "sf": {},
    })

    assert known_event.is_known_type()


# ============================================================================
# 8. LOAD_MANIFEST
# ============================================================================


def test_load_manifest_missing_file_returns_none(tmp_path: Path) -> None:
    """load_manifest with a missing file returns None (degraded but usable)."""
    manifest_path = tmp_path / "nonexistent.json"

    manifest = load_manifest(manifest_path)

    assert manifest is None


def test_load_manifest_malformed_json_returns_none(tmp_path: Path) -> None:
    """load_manifest with malformed JSON returns None, not raise."""
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("NOT JSON", encoding="utf-8")

    manifest = load_manifest(manifest_path)

    assert manifest is None


def test_load_manifest_valid_file() -> None:
    """load_manifest with valid data returns a CaptureManifest."""
    from pathlib import Path
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump({
            "capture_id": "test-id",
            "org_alias": "test-org",
            "org_instance_url": "https://test.my.salesforce.com",
            "is_sandbox": True,
            "is_scratch": False,
            "started_at": "2026-07-25T00:00:00Z",
            "ended_at": "2026-07-25T00:10:00Z",
            "event_count": 10,
            "network_event_count": 5,
            "sink_errors": 0,
            "recorder_sha256": "abc123",
            "playwright_version": "1.50.0",
            "operator_note": "Test run",
        }, f)
        f.flush()
        manifest_path = Path(f.name)

    try:
        manifest = load_manifest(manifest_path)

        assert manifest is not None
        assert manifest.capture_id == "test-id"
        assert manifest.event_count == 10
    finally:
        manifest_path.unlink()


# ============================================================================
# 9. SYNTHESIZE_TRACE (test helper)
# ============================================================================


def test_synthesize_trace_marked_synthetic() -> None:
    """synthesize_trace must be unmistakably marked synthetic — this guards
    against synthetic data ever passing as real evidence.
    """
    trace = synthesize_trace()

    # Must have a warning that says SYNTHETIC
    assert any("SYNTHETIC" in w for w in trace.warnings)
    assert any("not from a real recording" in w for w in trace.warnings)


def test_synthesize_trace_provides_minimal_defaults() -> None:
    """synthesize_trace with no args produces a valid trace."""
    trace = synthesize_trace()

    assert len(trace.events) == 1
    assert trace.events[0].v == 1
    assert trace.events[0].type == "click"


def test_synthesize_trace_custom_events() -> None:
    """synthesize_trace accepts custom event data."""
    trace = synthesize_trace([
        {"seq": 1, "type": "input", "value": "test"},
        {"seq": 2, "type": "click"},
    ])

    assert len(trace.events) == 2
    assert trace.events[0].type == "input"
    assert trace.events[0].value == "test"
    assert trace.events[1].type == "click"


def test_manifest_none_works_everywhere() -> None:
    """trace.manifest=None must work throughout extraction pipeline.
    A4 says _derive_recording_id was the only unguarded access. Verify
    independently by building a trace with manifest=None and driving
    the FULL extract path.
    """
    from sf_video_blueprint.dom_extractor import DomCaptureExtractor

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"text": "Save"},
                "element": {"tag": "button", "text": "Save"},
            }
        ],
        manifest_data=None,
    )

    assert trace.manifest is None, "Test precondition: manifest must be None"

    # Drive the full extraction path
    extractor = DomCaptureExtractor()
    bundle = extractor.extract_from_trace(trace)

    # Must produce a valid bundle
    assert bundle.recording_id, "Must derive recording_id even without manifest"
    assert len(bundle.actions) > 0, "Must produce actions"
    assert bundle.evidence, "Must produce evidence"


def test_manifest_none_recording_id_discriminates() -> None:
    """When manifest=None, recording_id must differ for different traces.
    The content-hash fallback must actually discriminate, or every
    manifest-less recording collides.
    """
    from sf_video_blueprint.dom_extractor import DomCaptureExtractor

    trace_a = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com/page-a",
                "selectors": {"text": "Button A"},
                "element": {"tag": "button", "text": "Button A"},
            }
        ],
        manifest_data=None,
    )

    trace_b = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 2000000,
                "type": "click",
                "url": "https://test.salesforce.com/page-b",
                "selectors": {"text": "Button B"},
                "element": {"tag": "button", "text": "Button B"},
            }
        ],
        manifest_data=None,
    )

    extractor = DomCaptureExtractor()
    bundle_a = extractor.extract_from_trace(trace_a)
    bundle_b = extractor.extract_from_trace(trace_b)

    # Recording IDs must differ
    assert bundle_a.recording_id != bundle_b.recording_id, \
        f"Different traces must yield different recording_id, got: {bundle_a.recording_id} == {bundle_b.recording_id}"


# ============================================================================
# 11. "ALSO VERIFY" REQUIREMENTS — confirming existing behavior still works
# ============================================================================


def test_ingest_seq_authoritative_for_ordering() -> None:
    """Round 3 requirement: driver-stamped _ingest_seq is authoritative.

    After A1/A2 fixes, confirm this still works: events with ingest_seq must
    be ordered by ingest_seq, not by seq (which restarts per frame).
    """
    # Already tested above in test_order_events_uses_ingest_seq_when_present
    # but re-verify after changes:
    events = [
        RawDomEvent.model_validate({
            "v": 1, "seq": 1, "t": 1000, "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [], "selectors": {}, "value": None, "value_redacted": False,
            "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": "Second"},
            "sf": {}, "_ingest_seq": 10,
        }),
        RawDomEvent.model_validate({
            "v": 1, "seq": 1, "t": 1000, "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [], "selectors": {}, "value": None, "value_redacted": False,
            "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": "First"},
            "sf": {}, "_ingest_seq": 5,
        }),
    ]

    ordered = order_events(events)
    assert ordered[0].element.text == "First", "ingest_seq=5 must come first"
    assert ordered[1].element.text == "Second", "ingest_seq=10 must come second"


def test_unknown_event_type_preserved_with_warning() -> None:
    """Round 3 requirement: unknown event type values preserved with warning.

    After A1/A2 fixes, confirm unknown types still parse and warn.
    """
    # Already tested in test_unknown_event_type_parses_with_warning
    # Re-verify after changes:
    trace = synthesize_trace([{"type": "future_type"}])
    assert len(trace.events) == 1
    assert trace.events[0].type == "future_type"
    # synthesize_trace adds a SYNTHETIC warning, but the parse path adds the unknown-type warning


def test_forward_version_raises() -> None:
    """Round 3 requirement: v > SUPPORTED_VERSION raises.

    After A1/A2 fixes, confirm forward-incompatible versions still raise.
    """
    # Already tested in test_version_2_raises_forward_incompatible
    # This is a critical safety check — re-verify after changes
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({
            "v": 99,  # Future version
            "seq": 1, "t": 1000, "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [], "selectors": {}, "value": None, "value_redacted": False,
            "element": {"tag": "button", "classes": [], "shadow_depth": 0}, "sf": {},
        }) + "\n")
        f.flush()
        path = Path(f.name)

    try:
        with pytest.raises(ValueError) as exc_info:
            parse_capture_file(path)
        assert "version 99 is NEWER" in str(exc_info.value)
    finally:
        path.unlink()


def test_malformed_line_does_not_abort_parse() -> None:
    """Round 3 requirement: malformed lines land in skipped_lines without aborting.

    After A1/A2 fixes, confirm parse continues on bad lines.
    """
    # Already tested in multiple places above, but re-verify the key invariant:
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        # Good event
        f.write(json.dumps({
            "v": 1, "seq": 1, "t": 1000, "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [], "selectors": {}, "value": None, "value_redacted": False,
            "element": {"tag": "button", "classes": [], "shadow_depth": 0}, "sf": {},
        }) + "\n")
        # Bad line
        f.write("NOT JSON\n")
        # Another good event
        f.write(json.dumps({
            "v": 1, "seq": 2, "t": 2000, "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [], "selectors": {}, "value": None, "value_redacted": False,
            "element": {"tag": "button", "classes": [], "shadow_depth": 0}, "sf": {},
        }) + "\n")
        f.flush()
        path = Path(f.name)

    try:
        trace = parse_capture_file(path)
        assert len(trace.events) == 2, "Both good events should parse"
        assert len(trace.skipped_lines) == 1, "Bad line should be skipped"
    finally:
        path.unlink()


# ============================================================================
# 10. UNICODE, EMPTY STRINGS, VERY LONG VALUES, DEEPLY NESTED frame_path
# ============================================================================


def test_unicode_values_do_not_crash(tmp_path: Path) -> None:
    """Unicode in values, text, labels should not crash."""
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "input",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {"text": "保存 🚀"},
            "element": {
                "tag": "input",
                "classes": [],
                "shadow_depth": 0,
                "text": "日本語テキスト",
                "aria_label": "Emoji 🎉",
            },
            "value": "Käse 🧀",
            "value_redacted": False,
            "sf": {},
        }) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1
    assert trace.events[0].element.text == "日本語テキスト"
    assert trace.events[0].value == "Käse 🧀"


def test_empty_strings_do_not_crash(tmp_path: Path) -> None:
    """Empty strings in various fields should not crash."""
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "input",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {"text": "", "css_path": ""},
            "element": {
                "tag": "input",
                "name": "",
                "id": "",
                "classes": [],
                "shadow_depth": 0,
                "text": "",
            },
            "value": "",
            "value_redacted": False,
            "sf": {"object": "", "record_id": ""},
        }) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1


def test_very_long_values_do_not_crash(tmp_path: Path) -> None:
    """Very long strings (10k+ chars) should not crash."""
    long_value = "A" * 50000
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "input",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {},
            "element": {"tag": "textarea", "classes": [], "shadow_depth": 0},
            "value": long_value,
            "value_redacted": False,
            "sf": {},
        }) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1
    assert len(trace.events[0].value) == 50000


def test_deeply_nested_frame_path_does_not_crash(tmp_path: Path) -> None:
    """Deeply nested frame_path (10+ levels) should not crash."""
    deep_path = [f"iframe#frame-{i}" for i in range(20)]
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps({
            "v": 1,
            "seq": 1,
            "t": 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": deep_path,
            "selectors": {},
            "element": {"tag": "button", "classes": [], "shadow_depth": 0},
            "value": None,
            "value_redacted": False,
            "sf": {},
        }) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1
    assert len(trace.events[0].frame_path) == 20


# ============================================================================
# 12. DEFECT L4-1 — role/name were REQUIRED, so real DOM events were dropped
# ============================================================================
#
# `RawRoleName` declared `role: str` and `name: str`, both mandatory. The
# recorder this project ships does not honour that contract:
#
#   capture/recorder.js:161   return { role, name: null };
#
# is the terminal branch of `getRoleAndName` — an element with no aria-label,
# no aria-labelledby, no text, no title and no alt gets `name: null`. And
# `role` is `explicitRole || implicitRole` (recorder.js:136), which is `null`
# for any tag outside the implicit-role map — `div` and `span`, i.e. most of
# Lightning. So the parser rejected its own recorder's documented output and
# the event vanished into `skipped_lines`.


def _role_name_event(seq: int, role_name: object) -> dict:
    """A minimal, otherwise-valid click event carrying the given role_name."""
    return {
        "v": 1,
        "seq": seq,
        "t": 1774000000000 + seq,
        "type": "click",
        "url": "https://test.my.salesforce.com/lightning/o/Case/list",
        "frame_path": [],
        "selectors": {
            "test_id": None,
            "aria": None,
            "role_name": role_name,
            "label_for": None,
            "sf_field": None,
            "css_path": "button.slds-x",
            "text": "New",
            "xpath": None,
        },
        "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": "New"},
        "value": None,
        "value_redacted": False,
        "sf": {},
        "_ingest_seq": seq,
    }


def test_role_without_accessible_name_is_not_dropped(tmp_path: Path) -> None:
    """DEFECT L4-1: an icon-only button yields {role: "button", name: null}.

    That is `capture/recorder.js:161` verbatim. Before the fix this event was
    rejected with "selectors.role_name.name / Input should be a valid string"
    and silently discarded.
    """
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps(_role_name_event(1, {"role": "button", "name": None})) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert trace.skipped_lines == [], f"legitimate event was dropped: {trace.skipped_lines}"
    assert len(trace.events) == 1
    assert trace.events[0].selectors.role_name is not None
    assert trace.events[0].selectors.role_name.role == "button"
    assert trace.events[0].selectors.role_name.name is None


def test_accessible_name_without_role_is_not_dropped(tmp_path: Path) -> None:
    """DEFECT L4-1: a `div` with text yields {role: null, name: "Save"}.

    `role` is `explicitRole || implicitRole` and `div` is not in the recorder's
    implicit-role map, so `role` is null for most Lightning markup.
    """
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps(_role_name_event(1, {"role": None, "name": "Save"})) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert trace.skipped_lines == [], f"legitimate event was dropped: {trace.skipped_lines}"
    assert len(trace.events) == 1
    assert trace.events[0].selectors.role_name.role is None
    assert trace.events[0].selectors.role_name.name == "Save"


def test_role_name_both_absent_is_not_dropped(tmp_path: Path) -> None:
    """DEFECT L4-1: a bare `<span>` click yields {role: null, name: null}.

    The event still carries a css_path and a text selector, so it is useful
    evidence. Dropping the whole event because one of eight selector
    strategies is empty is the data loss this lane exists to stop.
    """
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        json.dumps(_role_name_event(1, {"role": None, "name": None})) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert trace.skipped_lines == [], f"legitimate event was dropped: {trace.skipped_lines}"
    assert len(trace.events) == 1
    assert trace.events[0].selectors.css_path == "button.slds-x"


def test_real_dom_shaped_capture_loses_no_events(tmp_path: Path) -> None:
    """DEFECT L4-1, end to end: four events in the recorder's real output
    shapes must produce four events, not one.

    Measured before the fix: 4 lines in, 1 event out, 3 skipped, and
    `validate_trace` reported 75% DATA LOSS on a capture in which every line
    was legitimate.
    """
    rows = [
        _role_name_event(1, {"role": "button", "name": "New"}),
        _role_name_event(2, {"role": "button", "name": None}),
        _role_name_event(3, {"role": None, "name": "Save"}),
        _role_name_event(4, {"role": None, "name": None}),
    ]
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 4, f"skipped: {trace.skipped_lines}"
    assert trace.skipped_lines == []
    assert not [f for f in validate_trace(trace) if f.startswith("DATA LOSS:")]


def test_role_name_still_rejects_wrong_types(tmp_path: Path) -> None:
    """The fix must loosen NULLABILITY, not TYPE.

    Making the fields optional must not turn the model into a dict sponge.
    A role that is a number, a list, or a nested object is malformed recorder
    output and must still land in skipped_lines — the point of defect L4-1 is
    to stop dropping *legitimate* events, not to start accepting garbage.
    """
    garbage = [
        {"role": 123, "name": "Save"},
        {"role": ["button"], "name": "Save"},
        {"role": {"nested": "object"}, "name": "Save"},
        {"role": "button", "name": 4.5},
    ]
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(_role_name_event(i + 1, g)) for i, g in enumerate(garbage))
        + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert trace.events == [], "malformed role_name types must not be accepted"
    assert len(trace.skipped_lines) == 4


def test_role_name_optional_fields_produce_no_role_selector() -> None:
    """Downstream contract: a role_name with no role must yield no role
    selector, rather than an unusable `role=None[...]` string.

    This is the validation the required-fields constraint was really buying.
    It belongs in the selector builder, which already handles it — so the
    parser does not need to drop the event to protect this invariant.
    """
    from sf_video_blueprint.selectors import rank_selectors

    trace = synthesize_trace([
        {
            "selectors": {
                "role_name": {"role": None, "name": "Save"},
                "css_path": "button.slds-x",
            }
        }
    ])
    ranked = rank_selectors(trace.events[0].selectors)

    assert all(r.kind != "role_name" for r in ranked), (
        f"a role_name without a role must not produce a role selector: {ranked}"
    )
    assert any(r.kind == "css" for r in ranked), "the css fallback must survive"
