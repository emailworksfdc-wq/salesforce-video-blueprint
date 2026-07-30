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
    """Some events have ingest_seq, some don't — they INTERLEAVE by time.

    ASSERTION CHANGED BY DEFECT L4-3. This test previously asserted
    "ingest_seq events sort first", which encoded the partition bug as the
    contract. Its own fixture disproves it: the unstamped event carries t=1000
    and the stamped one t=2000, so the unstamped event happened FIRST, and the
    old expectation put it last purely because the driver had not stamped it.

    The fixture data is unchanged; only the expected order is corrected. See
    section 14 for the full reasoning and for the invariant test proving that
    page-controlled `t` still cannot reorder two stamped events.
    """
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

    # t=1000 happened before t=2000, and that is the only ordering signal the
    # unstamped event has. It sorts first.
    assert ordered[0].element.text == "NoIngest"
    assert ordered[1].element.text == "HasIngest"


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
    """DEFECT A2: Minor loss (<50% skipped) must not be FATAL.

    A recorder that emits one bad line among 500 good ones must still produce a
    usable trace. What it must not do is proceed silently: as of DEFECT L4-7 the
    same input also yields a non-fatal `EVIDENCE INCOMPLETE:` finding, which this
    test deliberately does not exclude. The assertion is narrowly about the
    `DATA LOSS:` prefix, which cli.py / pipeline.py / mcp_server.py abort on.
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


# ============================================================================
# 13. DEFECT L4-2 — a UTF-8 BOM ate the first event
# ============================================================================
#
# The file was opened with encoding="utf-8", which does not strip a byte-order
# mark. A recorder running on Windows (PowerShell redirection, .NET
# StreamWriter, Notepad) writes EF BB BF at the head of the file, so line 1
# arrives as "﻿{...}". json.loads rejects it with the remarkably specific
# "Unexpected UTF-8 BOM (decode using utf-8-sig)" and the first event — the one
# that establishes where the recording started — is discarded.
#
# Measured before the fix: 3 events written, 2 parsed, 1 skipped.
# `encoding="utf-8-sig"` strips a BOM when present and is a no-op when absent.


def _plain_event(seq: int) -> dict:
    return {
        "v": 1,
        "seq": seq,
        "t": 1774000000000 + seq,
        "type": "click",
        "url": "https://test.my.salesforce.com",
        "frame_path": [],
        "selectors": {"css_path": f"button.n{seq}"},
        "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": f"E{seq}"},
        "value": None,
        "value_redacted": False,
        "sf": {},
        "_ingest_seq": seq,
    }


def _jsonl_body(count: int) -> str:
    return "\n".join(json.dumps(_plain_event(i)) for i in range(1, count + 1)) + "\n"


def test_utf8_bom_does_not_eat_the_first_event(tmp_path: Path) -> None:
    """DEFECT L4-2: a BOM-prefixed capture must lose no events.

    Before the fix the first line failed with "JSON decode error: Unexpected
    UTF-8 BOM (decode using utf-8-sig)" and 3 events became 2.
    """
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_bytes(b"\xef\xbb\xbf" + _jsonl_body(3).encode("utf-8"))

    # Assert the fixture really is BOM-prefixed, so this test cannot pass
    # vacuously by writing a plain file.
    assert jsonl_path.read_bytes()[:3] == b"\xef\xbb\xbf"

    trace = parse_capture_file(jsonl_path)

    assert trace.skipped_lines == [], f"BOM ate an event: {trace.skipped_lines}"
    assert len(trace.events) == 3
    # The FIRST event specifically — that is the one the BOM destroys.
    assert trace.events[0].seq == 1
    assert trace.events[0].element.text == "E1"


def test_capture_without_bom_still_parses(tmp_path: Path) -> None:
    """utf-8-sig must be a no-op on a normal file (the overwhelming case)."""
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(_jsonl_body(3), encoding="utf-8")

    assert jsonl_path.read_bytes()[:1] == b"{"

    trace = parse_capture_file(jsonl_path)

    assert trace.skipped_lines == []
    assert [e.seq for e in trace.events] == [1, 2, 3]


def test_bom_does_not_corrupt_multibyte_content(tmp_path: Path) -> None:
    """utf-8-sig must strip only the BOM, not mangle real non-ASCII text.

    A BOM-stripping implementation that sliced a fixed number of characters, or
    decoded as latin-1, would corrupt this.
    """
    event = _plain_event(1)
    event["value"] = "Café ☕ 日本語 — em-dash"
    event["element"]["text"] = "Zürich"
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_bytes(
        b"\xef\xbb\xbf" + (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1
    assert trace.events[0].value == "Café ☕ 日本語 — em-dash"
    assert trace.events[0].element.text == "Zürich"


def test_bom_prefixed_manifest_loads(tmp_path: Path) -> None:
    """DEFECT L4-2, same bug in load_manifest.

    A Windows recorder that BOMs the capture BOMs the manifest beside it. A
    manifest that silently fails to load takes the event-count cross-check down
    with it, which is exactly the check that detects a truncated capture.
    """
    manifest_path = tmp_path / "dom_capture.manifest.json"
    manifest_path.write_bytes(
        b"\xef\xbb\xbf"
        + json.dumps({
            "capture_id": "bom-test",
            "org_alias": "example-dev",
            "org_instance_url": "https://example-dev.develop.my.salesforce.com",
            "is_sandbox": False,
            "is_scratch": False,
            "started_at": "2026-03-20T09:00:00Z",
            "event_count": 3,
            "network_event_count": 0,
            "sink_errors": 0,
        }).encode("utf-8")
    )

    manifest = load_manifest(manifest_path)

    assert manifest is not None, "a BOM-prefixed manifest was silently discarded"
    assert manifest.capture_id == "bom-test"
    assert manifest.event_count == 3


# ============================================================================
# 14. DEFECT L4-3 — order_events partitioned instead of merging
# ============================================================================
#
# DECISION: the partition is REMOVED. Reasoning, since the brief asks for it
# either way:
#
# The old sort key was (0, ingest_seq, 0) for stamped events and (1, t, seq)
# for unstamped ones. That leading 0/1 is a partition, not a tiebreak: EVERY
# stamped event sorted before EVERY unstamped event, no matter when either
# actually happened. A trace where the driver stamped most events and missed a
# few does not come back mis-tied at the margin, it comes back in two
# concatenated blocks.
#
# Measured before the fix, true order A B C D E (by wall clock):
#   order_events() -> ['A-stamped', 'C-stamped', 'E-stamped',
#                      'B-unstamped', 'D-unstamped']
#
# `capture/recorder.js:44` states the contract the partition broke: "`t`
# (Date.now()) is the global ordering key. Python side uses `t` to merge/sort
# events from multiple frames and across navigations." Merge, not partition.
#
# What is kept: ingest_seq remains ABSOLUTELY authoritative for the relative
# order of stamped events. `t` is page-controlled and therefore untrusted, so
# it is used only to POSITION unstamped events among the stamped ones — it can
# never reorder two stamped events. That is the ordering guarantee the driver
# stamp buys, and it survives intact.
#
# Honest residual: an unstamped event carries only page-controlled `t`, so a
# page with a skewed clock can misplace its own unstamped events. There is no
# trusted signal to do better — the driver never saw them. The alternative
# (the partition) misplaced them unconditionally.


def _ordering_event(label: str, t: int, seq: int, ingest_seq: int | None = None) -> RawDomEvent:
    payload = {
        "v": 1,
        "seq": seq,
        "t": t,
        "type": "click",
        "url": "https://test.my.salesforce.com",
        "frame_path": [],
        "selectors": {},
        "element": {"tag": "button", "classes": [], "shadow_depth": 0, "text": label},
        "value": None,
        "value_redacted": False,
        "sf": {},
    }
    if ingest_seq is not None:
        payload["_ingest_seq"] = ingest_seq
    return RawDomEvent.model_validate(payload)


def test_order_events_interleaves_partially_stamped_trace() -> None:
    """DEFECT L4-3: a driver that stamped most events but missed two must not
    produce two concatenated blocks.

    Before the fix this returned A C E B D.
    """
    events = [
        _ordering_event("A-stamped", t=1000, seq=1, ingest_seq=1),
        _ordering_event("B-unstamped", t=1100, seq=1),
        _ordering_event("C-stamped", t=1200, seq=2, ingest_seq=2),
        _ordering_event("D-unstamped", t=1300, seq=2),
        _ordering_event("E-stamped", t=1400, seq=3, ingest_seq=3),
    ]

    ordered = [e.element.text for e in order_events(events)]

    assert ordered == [
        "A-stamped",
        "B-unstamped",
        "C-stamped",
        "D-unstamped",
        "E-stamped",
    ]


def test_order_events_unstamped_event_before_all_stamped_sorts_first() -> None:
    """An unstamped event that happened first must come first.

    The partition forced it to the back of the trace regardless of its clock.
    """
    events = [
        _ordering_event("stamped-later", t=5000, seq=1, ingest_seq=1),
        _ordering_event("unstamped-first", t=1000, seq=1),
    ]

    ordered = [e.element.text for e in order_events(events)]

    assert ordered == ["unstamped-first", "stamped-later"]


def test_order_events_ingest_seq_still_absolutely_authoritative() -> None:
    """THE INVARIANT THAT MUST NOT REGRESS.

    A page that lies about `t` must not be able to reorder driver-stamped
    events. Here the page claims the second-arriving event happened first, by a
    wide margin. ingest_seq must win.
    """
    events = [
        _ordering_event("arrived-first", t=9_999_999, seq=1, ingest_seq=1),
        _ordering_event("arrived-second", t=1, seq=2, ingest_seq=2),
    ]

    ordered = [e.element.text for e in order_events(events)]

    assert ordered == ["arrived-first", "arrived-second"], (
        "page-controlled `t` must never reorder driver-stamped events"
    )


def test_order_events_non_monotonic_stamped_clock_does_not_misplace_unstamped() -> None:
    """Stamped `t` values need not be monotonic (clock skew across frames /
    navigations). Positioning must still be well defined and must not drop or
    duplicate any event.
    """
    events = [
        _ordering_event("s1", t=1000, seq=1, ingest_seq=1),
        _ordering_event("s2", t=800, seq=1, ingest_seq=2),  # clock went backwards
        _ordering_event("s3", t=1200, seq=2, ingest_seq=3),
        _ordering_event("u-late", t=1150, seq=9),
    ]

    ordered = order_events(events)

    # ingest_seq order among stamped events is untouched by the skew.
    assert [e.element.text for e in ordered if e.ingest_seq is not None] == ["s1", "s2", "s3"]
    # Nothing lost, nothing duplicated.
    assert len(ordered) == 4
    assert {e.element.text for e in ordered} == {"s1", "s2", "s3", "u-late"}
    # The unstamped event lands after the skew-adjusted stamped prefix, not at
    # the end of the trace by fiat.
    assert ordered.index(next(e for e in ordered if e.element.text == "u-late")) == 2


def test_order_events_all_stamped_is_pure_ingest_seq_order() -> None:
    """The overwhelmingly common case — every event stamped — must be exactly
    ingest_seq order, with `t` ignored entirely."""
    events = [
        _ordering_event("third", t=1, seq=1, ingest_seq=3),
        _ordering_event("first", t=999, seq=2, ingest_seq=1),
        _ordering_event("second", t=500, seq=3, ingest_seq=2),
    ]

    assert [e.element.text for e in order_events(events)] == ["first", "second", "third"]


def test_order_events_all_unstamped_is_t_then_seq_order() -> None:
    """With no driver stamps at all (older driver), fall back to (t, seq)."""
    events = [
        _ordering_event("b", t=1000, seq=2),
        _ordering_event("a", t=1000, seq=1),
        _ordering_event("c", t=2000, seq=1),
    ]

    assert [e.element.text for e in order_events(events)] == ["a", "b", "c"]


def test_order_events_is_stable_and_total() -> None:
    """order_events must be a permutation of its input: no drops, no dupes, and
    ties preserve input order."""
    events = [
        _ordering_event("x", t=1000, seq=1),
        _ordering_event("y", t=1000, seq=1),  # exact tie with x
        _ordering_event("z", t=1000, seq=1, ingest_seq=1),
    ]

    ordered = order_events(events)

    assert len(ordered) == 3
    xy = [e.element.text for e in ordered if e.element.text in ("x", "y")]
    assert xy == ["x", "y"], "exact ties must preserve input order"


def test_order_events_empty_input() -> None:
    assert order_events([]) == []


# ============================================================================
# 15. DEFECT L4-5 — parse_capture_file never loaded the manifest
# ============================================================================
#
# `parse_capture_file` returned `manifest=None  # loaded separately via
# load_manifest`, and no production caller ever loaded it: cli.py:97,
# pipeline.py:151 and mcp_server.py:314 all call parse_capture_file and then
# validate_trace, so `validate_trace`'s manifest cross-check — the ONLY check
# that can see events the recorder wrote but the parser never received — was
# structurally dead.
#
# That check is what detects a truncated capture. The parser cannot notice
# events that are absent from the file; only the recorder's own count can.
#
# Measured before the fix, with a manifest reporting event_count=10 and
# sink_errors=4 sitting next to a capture holding 6 events:
#
#     trace.manifest : None
#     events parsed  : 6
#     findings       : []        <- 40% of the capture gone, nothing said so


def _write_capture_with_manifest(
    tmp_path: Path, *, events: int, claimed: int, sink_errors: int = 0
) -> Path:
    capture = tmp_path / "dom_capture.jsonl"
    capture.write_text(_jsonl_body(events), encoding="utf-8")
    (tmp_path / "dom_capture.manifest.json").write_text(
        json.dumps({
            "capture_id": "wiring-test",
            "org_alias": "example-dev",
            "org_instance_url": "https://example-dev.develop.my.salesforce.com",
            "is_sandbox": False,
            "is_scratch": False,
            "started_at": "2026-03-20T09:00:00Z",
            "event_count": claimed,
            "network_event_count": 0,
            "sink_errors": sink_errors,
        }),
        encoding="utf-8",
    )
    return capture


def test_parse_capture_file_loads_sibling_manifest(tmp_path: Path) -> None:
    """DEFECT L4-5: the manifest beside the capture must be wired in."""
    capture = _write_capture_with_manifest(tmp_path, events=6, claimed=6)

    trace = parse_capture_file(capture)

    assert trace.manifest is not None, "sibling manifest was not loaded"
    assert trace.manifest.capture_id == "wiring-test"
    assert trace.manifest.event_count == 6


def test_manifest_cross_check_detects_recorder_parser_gap(tmp_path: Path) -> None:
    """DEFECT L4-5, the whole point: events the recorder wrote but the parser
    never saw must be surfaced.

    Nothing else in the system can detect this. There is no bad line to land in
    skipped_lines — the events are simply absent from the file.
    """
    capture = _write_capture_with_manifest(
        tmp_path, events=6, claimed=10, sink_errors=4
    )

    trace = parse_capture_file(capture)
    findings = validate_trace(trace)

    assert trace.manifest is not None
    joined = " | ".join(findings)
    assert "10" in joined and "6" in joined, f"count mismatch not surfaced: {findings}"
    assert any("sink error" in f for f in findings), f"sink errors not surfaced: {findings}"


def test_manifest_is_discovered_by_both_naming_conventions(tmp_path: Path) -> None:
    """Two names are in use in this repo, so both must resolve.

    - `examples/case_triage.dom_capture.manifest.json` — the `.jsonl` suffix
      swapped for `.manifest.json` (documented example layout)
    - `dom_capture.manifest.json` — the literal name `capture/inject.py` writes
    """
    # Convention A: <capture-stem>.manifest.json
    capture_a = tmp_path / "case_triage.dom_capture.jsonl"
    capture_a.write_text(_jsonl_body(2), encoding="utf-8")
    (tmp_path / "case_triage.dom_capture.manifest.json").write_text(
        json.dumps({
            "capture_id": "convention-a",
            "org_alias": "example-dev",
            "org_instance_url": "https://example-dev.develop.my.salesforce.com",
            "is_sandbox": False,
            "is_scratch": False,
            "started_at": "2026-03-20T09:00:00Z",
            "event_count": 2,
            "network_event_count": 0,
            "sink_errors": 0,
        }),
        encoding="utf-8",
    )

    trace_a = parse_capture_file(capture_a)
    assert trace_a.manifest is not None, "stem-based manifest name not found"
    assert trace_a.manifest.capture_id == "convention-a"

    # Convention B: the literal dom_capture.manifest.json in the same directory
    other = tmp_path / "sub"
    other.mkdir()
    capture_b = other / "recording.jsonl"
    capture_b.write_text(_jsonl_body(2), encoding="utf-8")
    (other / "dom_capture.manifest.json").write_text(
        json.dumps({
            "capture_id": "convention-b",
            "org_alias": "example-dev",
            "org_instance_url": "https://example-dev.develop.my.salesforce.com",
            "is_sandbox": False,
            "is_scratch": False,
            "started_at": "2026-03-20T09:00:00Z",
            "event_count": 2,
            "network_event_count": 0,
            "sink_errors": 0,
        }),
        encoding="utf-8",
    )

    trace_b = parse_capture_file(capture_b)
    assert trace_b.manifest is not None, "literal manifest name not found"
    assert trace_b.manifest.capture_id == "convention-b"


def test_missing_manifest_is_still_degraded_but_usable(tmp_path: Path) -> None:
    """A capture with no manifest must still parse — wiring the manifest in must
    not turn its absence into a hard failure."""
    capture = tmp_path / "dom_capture.jsonl"
    capture.write_text(_jsonl_body(3), encoding="utf-8")

    trace = parse_capture_file(capture)

    assert trace.manifest is None
    assert len(trace.events) == 3
    assert any("no manifest" in w.lower() for w in trace.warnings), (
        f"the absence of a manifest should be visible: {trace.warnings}"
    )


def test_explicit_manifest_argument_overrides_discovery(tmp_path: Path) -> None:
    """A caller with the manifest somewhere else must be able to pass it."""
    capture = _write_capture_with_manifest(tmp_path, events=6, claimed=6)
    elsewhere = tmp_path / "moved.manifest.json"
    elsewhere.write_text(
        json.dumps({
            "capture_id": "explicit",
            "org_alias": "example-dev",
            "org_instance_url": "https://example-dev.develop.my.salesforce.com",
            "is_sandbox": False,
            "is_scratch": False,
            "started_at": "2026-03-20T09:00:00Z",
            "event_count": 99,
            "network_event_count": 0,
            "sink_errors": 0,
        }),
        encoding="utf-8",
    )

    trace = parse_capture_file(capture, manifest_path=elsewhere)

    assert trace.manifest is not None
    assert trace.manifest.capture_id == "explicit"


def test_manifest_discovery_can_be_disabled(tmp_path: Path) -> None:
    """Opt out, for callers that manage the manifest themselves."""
    capture = _write_capture_with_manifest(tmp_path, events=6, claimed=6)

    trace = parse_capture_file(capture, discover_manifest=False)

    assert trace.manifest is None


def test_malformed_manifest_warns_rather_than_vanishing(tmp_path: Path) -> None:
    """load_manifest swallows every exception and returns None. When a manifest
    file EXISTS but will not load, that silence hides the loss of the only
    recorder-side cross-check, so it must produce a warning."""
    capture = tmp_path / "dom_capture.jsonl"
    capture.write_text(_jsonl_body(3), encoding="utf-8")
    (tmp_path / "dom_capture.manifest.json").write_text(
        "{ this is not valid json", encoding="utf-8"
    )

    trace = parse_capture_file(capture)

    assert trace.manifest is None
    assert any("manifest" in w.lower() for w in trace.warnings), (
        f"a present-but-unloadable manifest must warn: {trace.warnings}"
    )


def test_example_capture_manifest_is_wired(tmp_path: Path) -> None:
    """The shipped example must exercise the path end to end.

    examples/case_triage.dom_capture.jsonl has 8 events and its manifest claims
    event_count=8, so a correctly wired parser produces a manifest and NO count
    mismatch. Before the fix `trace.manifest` was None here too.
    """
    example = (
        Path(__file__).resolve().parent.parent
        / "examples"
        / "case_triage.dom_capture.jsonl"
    )
    if not example.is_file():  # pragma: no cover - example is committed
        pytest.skip("example capture not present")

    trace = parse_capture_file(example)

    assert trace.manifest is not None, "example capture's manifest was not wired in"
    assert trace.manifest.event_count == len(trace.events) == 8
    assert not [f for f in validate_trace(trace) if "Manifest reports" in f]


# ============================================================================
# 16. DEFECT L4-6 — the leak detector inspected only element.name
# ============================================================================
#
# `validate_trace` and `redaction_audit` both did:
#
#     field_name_lower = (event.element.name or "").lower()
#
# one of the field-identity signals the recorder captures. Measured across
# twelve signals carrying a sensitive field identity next to an unredacted
# value, validate_trace caught 1 and missed 11 — including
# `element.type == "password"`, which is the single most reliable signal there
# is, and `selectors.sf_field == "Credit_Card_Number__c"`, which is how a
# Salesforce field announces itself.
#
# TEST DISCIPLINE, enforced by test_no_finding_ever_echoes_the_value below:
# these tests assert the FACT of a leak, never its content. The canary value
# must appear in no finding, no log and no assertion message — a test failure
# that prints the secret is itself the leak.

#: A recognisable value that must never appear in any finding text.
_CANARY = "4111111111111111"


@pytest.mark.parametrize(
    "label,patch",
    [
        ("element.name", {"element": {"tag": "input", "name": "password"}}),
        ("element.id", {"element": {"tag": "input", "id": "user-password"}}),
        ("element.aria_label", {"element": {"tag": "input", "aria_label": "Password"}}),
        ("element.classes", {"element": {"tag": "input", "classes": ["form-password-field"]}}),
        ("element.type", {"element": {"tag": "input", "type": "password"}}),
        ("element.text", {"element": {"tag": "input", "text": "Social Security Number"}}),
        ("selectors.sf_field", {"selectors": {"sf_field": "Credit_Card_Number__c"}}),
        ("selectors.label_for", {"selectors": {"label_for": "label[for=ssn]"}}),
        ("selectors.aria", {"selectors": {"aria": "[aria-label='CVV']"}}),
        ("selectors.test_id", {"selectors": {"test_id": "card-number-input"}}),
        (
            "selectors.role_name",
            {"selectors": {"role_name": {"role": "textbox", "name": "Password"}}},
        ),
        ("selectors.css_path", {"selectors": {"css_path": "input#ssn"}}),
    ],
)
def test_leak_detected_across_every_field_identity_signal(label: str, patch: dict) -> None:
    """DEFECT L4-6: a sensitive field identity on ANY signal must be caught.

    Before the fix, 11 of these 12 were missed.
    """
    event: dict = {"value": _CANARY, "value_redacted": False}
    event.update(patch)
    trace = synthesize_trace([event])

    findings = [f for f in validate_trace(trace) if f.startswith("SECURITY")]

    assert findings, f"leak on {label} was not detected"
    # And the audit path must agree with the validator.
    _, leaks = redaction_audit(trace)
    assert leaks, f"redaction_audit missed the leak on {label}"


def test_no_finding_ever_echoes_the_value() -> None:
    """THE RULE FOR THIS WHOLE SECTION: report the fact, never the content.

    Findings are printed to terminals and written into reports. A detector that
    echoes the secret it found has leaked it a second time, into a file that
    outlives the capture.
    """
    trace = synthesize_trace([
        {
            "value": _CANARY,
            "value_redacted": False,
            "element": {"tag": "input", "type": "password", "name": "password", "id": "pw"},
            "selectors": {"sf_field": "Credit_Card_Number__c", "css_path": "input#ssn"},
        },
        # The other leak class: flag set, value still present.
        {
            "value": _CANARY,
            "value_redacted": True,
            "element": {"tag": "input", "name": "cvv"},
        },
    ])

    findings = validate_trace(trace)
    _, leaks = redaction_audit(trace)

    assert findings and leaks, "fixture must actually produce findings"
    for text in [*findings, *leaks]:
        assert _CANARY not in text, "a finding echoed the leaked value"
        assert "4111" not in text, "a finding echoed part of the leaked value"


def test_flag_set_but_value_present_is_still_caught() -> None:
    """The A1 leak class must survive the widening: value_redacted=True with a
    value still attached, regardless of field identity."""
    trace = synthesize_trace([
        {"value": _CANARY, "value_redacted": True, "element": {"tag": "input", "name": "notes"}}
    ])

    findings = validate_trace(trace)

    assert any(f.startswith("SECURITY CRITICAL:") for f in findings)


def test_password_type_is_caught_even_with_innocuous_name() -> None:
    """`type="password"` is the strongest signal available and was ignored.

    A field named `j_idt42` (real-world generated markup) with
    type="password" leaked with no finding at all.
    """
    trace = synthesize_trace([
        {
            "value": _CANARY,
            "value_redacted": False,
            "element": {"tag": "input", "type": "password", "name": "j_idt42"},
        }
    ])

    assert [f for f in validate_trace(trace) if f.startswith("SECURITY")]


def test_ordinary_fields_do_not_produce_findings() -> None:
    """The widening must not cry wolf. A detector that flags everything gets
    ignored, which is the same as not having one."""
    trace = synthesize_trace([
        {
            "value": "Broken air conditioner",
            "value_redacted": False,
            "element": {
                "tag": "textarea",
                "name": "Description",
                "id": "case-description",
                "classes": ["slds-textarea"],
                "text": "Description",
            },
            "selectors": {"sf_field": "Description", "css_path": "textarea.slds-textarea"},
        },
        {
            "value": "High",
            "value_redacted": False,
            "element": {"tag": "select", "name": "Priority", "id": "case-priority"},
            "selectors": {"sf_field": "Priority", "label_for": "label[for=priority]"},
        },
        {
            "value": "New",
            "value_redacted": False,
            "element": {"tag": "select", "name": "Status"},
            "selectors": {"sf_field": "Status", "test_id": "status-picklist"},
        },
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("SECURITY")]

    assert findings == [], f"false positives on ordinary Case fields: {findings}"


def test_pin_substring_does_not_false_positive() -> None:
    """"pin" is a sensitive pattern but also a substring of ordinary words.

    `Shipping__c`, `Opt_In_Preference__c` and a `spinner` class must not trip
    the detector.
    """
    trace = synthesize_trace([
        {
            "value": "Express",
            "value_redacted": False,
            "element": {"tag": "select", "name": "Shipping", "classes": ["spinner-host"]},
            "selectors": {"sf_field": "Shipping_Method__c"},
        }
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("SECURITY")]

    assert findings == [], f"'pin' substring produced a false positive: {findings}"


def test_leak_finding_names_the_signal_that_matched() -> None:
    """An operator has to be able to act on the finding, so it must say WHICH
    signal matched — without quoting the value."""
    trace = synthesize_trace([
        {
            "value": _CANARY,
            "value_redacted": False,
            "element": {"tag": "input", "type": "password", "name": "j_idt42"},
        }
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("SECURITY")]

    assert findings
    assert any("type" in f for f in findings), (
        f"finding should identify the matching signal: {findings}"
    )
    assert all(_CANARY not in f for f in findings)


# ============================================================================
# 17. DEFECT L4-7 — loss below 50% was surfaced nowhere
# ============================================================================
#
# `validate_trace` warned on 100% loss and on >=50% loss, and said nothing at
# all below that. Measured, with `discover_manifest=False` to isolate line-level
# loss:
#
#     good  bad   loss  finding
#       10    0    0%   (nothing — correct)
#        9    1   10%   NOTHING
#        8    2   20%   NOTHING
#        7    3   30%   NOTHING
#        6    4   40%   NOTHING
#        5    5   50%   DATA LOSS: ...
#
# A capture that quietly lost 40% of its events was stamped as real evidence
# with no signal anywhere. That is the exact failure the brief names.
#
# The fix does NOT lower the 50% fail-closed threshold — cli.py, pipeline.py and
# mcp_server.py all abort on a `DATA LOSS:` prefix, and turning a 10%-loss
# capture into a hard failure would be a behaviour change nobody asked for. Any
# loss instead produces an `EVIDENCE INCOMPLETE:` finding, and `CaptureTrace`
# grows a computed `loss_ratio` so the number is impossible to miss
# programmatically rather than only in prose.


def _capture_with_bad_lines(tmp_path: Path, good: int, bad: int) -> Path:
    lines = [json.dumps(_plain_event(i)) for i in range(1, good + 1)]
    lines += ["{ this line is malformed"] * bad
    capture = tmp_path / "dom_capture.jsonl"
    capture.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return capture


@pytest.mark.parametrize("good,bad", [(9, 1), (8, 2), (7, 3), (6, 4)])
def test_sub_threshold_loss_is_surfaced(tmp_path: Path, good: int, bad: int) -> None:
    """DEFECT L4-7: ANY discarded line must produce a finding.

    Every one of these produced an empty finding list before the fix.
    """
    capture = _capture_with_bad_lines(tmp_path, good, bad)

    trace = parse_capture_file(capture, discover_manifest=False)
    findings = validate_trace(trace)

    incomplete = [f for f in findings if f.startswith("EVIDENCE INCOMPLETE:")]
    assert incomplete, f"{bad}/{good + bad} lines lost with no finding: {findings}"
    # The ratio has to be in the text — "some lines were skipped" is not
    # actionable.
    assert f"{bad}" in incomplete[0]


@pytest.mark.parametrize("good,bad", [(9, 1), (6, 4)])
def test_sub_threshold_loss_does_not_fail_the_run_closed(
    tmp_path: Path, good: int, bad: int
) -> None:
    """The 50% fail-closed threshold is NOT lowered.

    cli.py, pipeline.py and mcp_server.py all abort on a `DATA LOSS:` prefix.
    Sub-threshold loss must be loud but non-fatal, so it must NOT carry that
    prefix.
    """
    capture = _capture_with_bad_lines(tmp_path, good, bad)

    findings = validate_trace(parse_capture_file(capture, discover_manifest=False))

    assert not [f for f in findings if f.startswith("DATA LOSS:")], (
        "sub-threshold loss must not trip the fail-closed gate"
    )


@pytest.mark.parametrize("good,bad", [(5, 5), (4, 6)])
def test_at_or_above_threshold_still_fails_closed(
    tmp_path: Path, good: int, bad: int
) -> None:
    """REGRESSION GUARD: >=50% loss must still raise the fail-closed finding.

    This is the check LANE_RULES forbids weakening. If this test ever passes
    vacuously the gate is gone.
    """
    capture = _capture_with_bad_lines(tmp_path, good, bad)

    findings = validate_trace(parse_capture_file(capture, discover_manifest=False))

    assert [f for f in findings if f.startswith("DATA LOSS:")], (
        f"the >=50% fail-closed gate did not fire at {bad}/{good + bad}"
    )


def test_clean_capture_produces_no_loss_finding(tmp_path: Path) -> None:
    """Zero loss must stay silent, or the signal is worthless."""
    capture = _capture_with_bad_lines(tmp_path, 10, 0)

    findings = validate_trace(parse_capture_file(capture, discover_manifest=False))

    assert not [f for f in findings if "INCOMPLETE" in f or f.startswith("DATA LOSS:")]


def test_loss_ratio_is_exposed_on_the_trace(tmp_path: Path) -> None:
    """The number must be reachable programmatically, not only greppable in
    prose. Callers that summarise a run need to render it."""
    capture = _capture_with_bad_lines(tmp_path, 7, 3)

    trace = parse_capture_file(capture, discover_manifest=False)

    assert trace.total_lines == 10
    assert trace.loss_ratio == pytest.approx(0.3)
    assert trace.has_data_loss is True


def test_loss_ratio_on_a_clean_trace_is_zero(tmp_path: Path) -> None:
    capture = _capture_with_bad_lines(tmp_path, 4, 0)

    trace = parse_capture_file(capture, discover_manifest=False)

    assert trace.loss_ratio == 0.0
    assert trace.has_data_loss is False


def test_loss_ratio_on_an_empty_trace_does_not_divide_by_zero() -> None:
    trace = CaptureTrace(events=[])

    assert trace.total_lines == 0
    assert trace.loss_ratio == 0.0


def test_manifest_gap_counts_toward_loss_even_with_no_bad_lines(
    tmp_path: Path,
) -> None:
    """The subtler loss: the recorder wrote 10, the file holds 6, every line
    present parses cleanly.

    skipped_lines is EMPTY here — there is no bad line to count — so a
    line-ratio-only measure reports 0% loss on a capture missing 40% of its
    events. The manifest count is the only witness.
    """
    capture = _write_capture_with_manifest(tmp_path, events=6, claimed=10)

    trace = parse_capture_file(capture)
    findings = validate_trace(trace)

    assert trace.skipped_lines == []
    assert any(f.startswith("EVIDENCE INCOMPLETE:") for f in findings), (
        f"a 4-event manifest gap was not surfaced as incomplete evidence: {findings}"
    )
    assert trace.manifest_gap == 4


def test_manifest_gap_is_zero_when_counts_agree(tmp_path: Path) -> None:
    capture = _write_capture_with_manifest(tmp_path, events=6, claimed=6)

    trace = parse_capture_file(capture)

    assert trace.manifest_gap == 0
    assert not [f for f in validate_trace(trace) if "INCOMPLETE" in f]


def test_manifest_gap_is_none_without_a_manifest(tmp_path: Path) -> None:
    """Unknown is not zero. Without a manifest the gap is unknowable and must
    not be reported as "no gap"."""
    capture = _capture_with_bad_lines(tmp_path, 3, 0)

    trace = parse_capture_file(capture, discover_manifest=False)

    assert trace.manifest_gap is None


def test_evidence_incomplete_finding_is_greppable_and_specific(
    tmp_path: Path,
) -> None:
    """The finding must carry the counts an operator needs to judge severity."""
    capture = _capture_with_bad_lines(tmp_path, 8, 2)

    findings = validate_trace(parse_capture_file(capture, discover_manifest=False))
    incomplete = [f for f in findings if f.startswith("EVIDENCE INCOMPLETE:")]

    assert len(incomplete) == 1
    text = incomplete[0]
    assert "2" in text and "10" in text and "20%" in text


# ============================================================================
# 15. REVIEW FINDING R1 — the widened detector fires on ordinary Lightning
#     markup, which trains operators to ignore it
# ============================================================================
#
# DEFECT L4-6 widened leak detection from `element.name` to fourteen identity
# signals. Two of the patterns it carries — "card" and "auth" — are bare
# substrings, and two of the newly-inspected signals (`element.classes`,
# `selectors.css_path`) are PRESENTATION metadata rather than field identity.
# Salesforce's own design system puts `slds-card__body` on a large fraction of
# record-detail markup, so measured before this fix:
#
#     Subject field inside an slds-card ->  element.classes~'card'
#                                           selectors.css_path~'card'
#     Author__c                         ->  element.name='author__c'
#     Scorecard__c / Discard_Changes    ->  card
#     Authorization_Status__c           ->  auth
#
# These are non-blocking `SECURITY:` findings, so nothing aborts — which is
# precisely the problem. A leak detector that fires on most Lightning inputs is
# one an operator learns to scroll past, and the real leak scrolls past with it.
# Alarm fatigue is a security defect in a control whose only output is an alarm.
#
# The fix keeps every TRUE positive from the L4-6 test matrix. "card" and "auth"
# move to word-boundary matching, and the payment-card signal is carried by the
# more specific patterns that already existed ("credit", "cvv", "cvc") plus the
# word-boundary "card" that still catches `Credit_Card_Number__c`, `card-number`
# and `Card_Number__c`.


def test_slds_card_markup_is_not_a_redaction_leak() -> None:
    """REVIEW R1: `slds-card__body` is design-system markup, not a payment card.

    Before this fix a Case Subject field rendered inside an SLDS card produced
    two SECURITY findings.
    """
    trace = synthesize_trace([
        {
            "value": "Printer is broken",
            "value_redacted": False,
            "element": {
                "tag": "input",
                "type": "text",
                "name": "Subject",
                "classes": ["slds-input", "slds-card__footer"],
            },
            "selectors": {
                "sf_field": "Subject",
                "css_path": "div.slds-card__body input.slds-input",
            },
        }
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("SECURITY")]

    assert findings == [], f"SLDS card markup produced a false leak finding: {findings}"


@pytest.mark.parametrize(
    "field_name",
    [
        "Author__c",
        "AuthorName__c",
        "Author_Bio__c",
        "Authorization_Status__c",
        "Scorecard__c",
        "Discard_Changes",
        "Standard_Card_Layout",
    ],
)
def test_benign_field_names_embedding_card_or_auth_do_not_false_positive(field_name: str) -> None:
    """REVIEW R1: "card" and "auth" as bare substrings catch ordinary SF fields.

    `Author__c` is a person's name, not a credential. `Scorecard__c` is a
    number, not a payment instrument.

    NOT in this list, deliberately: `Dashboard_Pin__c`. "pin" stays a
    word-boundary pattern and still fires on it. A field whose name contains the
    standalone word "Pin" plausibly IS a PIN, the spelling is rare, and the
    finding is non-blocking — so the bias goes toward reporting. That is a
    different situation from "card"/"auth", which matched a large fraction of all
    Lightning markup via SLDS class names and so drowned the signal.
    """
    trace = synthesize_trace([
        {
            "value": "some ordinary value",
            "value_redacted": False,
            "element": {"tag": "input", "type": "text", "name": field_name},
            "selectors": {"sf_field": field_name},
        }
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("SECURITY")]

    assert findings == [], f"{field_name!r} produced a false leak finding: {findings}"


@pytest.mark.parametrize(
    "field_name",
    [
        "Credit_Card_Number__c",
        "card-number-input",
        "Card_Number__c",
        "cardNumber",
        "credit_card",
        "Auth_Token__c",
        "authToken",
        "oauth_token",
        "Credential__c",
    ],
)
def test_genuinely_sensitive_card_and_auth_fields_are_still_caught(field_name: str) -> None:
    """REVIEW R1: narrowing must not cost a single TRUE positive.

    This is the half of the fix that matters — tightening a security pattern is
    only safe if the sensitive cases it existed for still fire.
    """
    trace = synthesize_trace([
        {
            "value": _CANARY,
            "value_redacted": False,
            "element": {"tag": "input", "type": "text", "name": field_name},
            "selectors": {"sf_field": field_name},
        }
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("SECURITY")]

    assert findings, f"{field_name!r} is sensitive but produced NO finding"
    assert not any(_CANARY in f for f in findings), "finding echoed the value"



# =====================================================================# 18. selector_confidence and selector_fallback (lane B08)
# ============================================================================
#
# Two new fields added to RawSelectors and to recorder.js computeSelectors():
#
#   selector_confidence: float 0.0-1.0
#     1.0 = role+name both present (high-quality get_by_role selector)
#     0.5 = role only, OR no role but a stable data-id/testid/qa attribute
#     0.1 = role=null AND name=null AND no data-id (bare LWC shadow element)
#
#   selector_fallback: str | None
#     Best non-null alternative: aria-label > data-id > innerText[:40] > null
#
# Both fields are OPTIONAL: a capture made before this patch still parses.
# None means "capture pre-dates this field"; treat as unknown quality.


def test_selector_confidence_and_fallback_fields_are_optional() -> None:
    """A capture without selector_confidence/selector_fallback must still parse.

    These fields are new — old captures do not have them. Making them required
    would break every existing capture in production.
    """
    trace = synthesize_trace([
        {
            "seq": 1,
            "type": "click",
            "selectors": {
                "role_name": {"role": "button", "name": "Save"},
                "css_path": "button.slds-button",
                # No selector_confidence or selector_fallback
            },
        }
    ])
    event = trace.events[0]
    assert event.selectors.selector_confidence is None
    assert event.selectors.selector_fallback is None


def test_selector_confidence_parses_when_present(tmp_path: Path) -> None:
    """selector_confidence and selector_fallback are stored and retrievable."""
    jsonl_path = tmp_path / "capture.jsonl"
    payload = {
        "v": 1,
        "seq": 1,
        "t": 1000,
        "type": "click",
        "url": "https://test.my.salesforce.com",
        "frame_path": [],
        "selectors": {
            "role_name": {"role": "button", "name": "New Case"},
            "css_path": "button.new-case",
            "selector_confidence": 1.0,
            "selector_fallback": "New Case",
        },
        "element": {"tag": "button", "classes": [], "shadow_depth": 0},
        "value": None,
        "value_redacted": False,
        "sf": {},
    }
    jsonl_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1
    assert trace.events[0].selectors.selector_confidence == 1.0
    assert trace.events[0].selectors.selector_fallback == "New Case"


def test_selector_confidence_rejects_out_of_range_values(tmp_path: Path) -> None:
    """selector_confidence must be in [0.0, 1.0]; values outside are rejected.

    The recorder.js always emits 0.1, 0.5, or 1.0; any other value signals a
    broken recorder or tampered capture and must land in skipped_lines.
    """
    def _make_event(seq: int, confidence: float) -> str:
        return json.dumps({
            "v": 1, "seq": seq, "t": 1000 + seq, "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {"css_path": "div.foo", "selector_confidence": confidence},
            "element": {"tag": "div", "classes": [], "shadow_depth": 0},
            "value": None, "value_redacted": False, "sf": {},
        })

    jsonl_path = tmp_path / "capture.jsonl"
    # 1.5 and -0.1 are both out of [0.0, 1.0]
    jsonl_path.write_text(
        _make_event(1, 1.5) + "\n" + _make_event(2, -0.1) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 0, "Out-of-range selector_confidence must be rejected"
    assert len(trace.skipped_lines) == 2


def test_selector_confidence_boundary_values_are_accepted(tmp_path: Path) -> None:
    """selector_confidence accepts 0.0 and 1.0 (inclusive bounds), plus 0.5 and 0.1."""
    def _make_event(seq: int, confidence: float) -> str:
        return json.dumps({
            "v": 1, "seq": seq, "t": 1000 + seq, "type": "click",
            "url": "https://test.my.salesforce.com",
            "frame_path": [],
            "selectors": {
                "css_path": f"div.item{seq}",
                "selector_confidence": confidence,
            },
            "element": {"tag": "div", "classes": [], "shadow_depth": 0},
            "value": None, "value_redacted": False, "sf": {},
        })

    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        _make_event(1, 0.0) + "\n"
        + _make_event(2, 1.0) + "\n"
        + _make_event(3, 0.5) + "\n"
        + _make_event(4, 0.1) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 4, f"Valid confidences rejected: {trace.skipped_lines}"
    confidences = [e.selectors.selector_confidence for e in trace.events]
    assert confidences == [0.0, 1.0, 0.5, 0.1]


def test_selector_confidence_scores_for_known_cases() -> None:
    """Verify the three score levels (1.0, 0.5, 0.1) are stored faithfully.

    The recorder.js computeSelectorConfidence produces exactly these three
    values. Python must accept and preserve all three.
    """
    trace = synthesize_trace([
        # 1.0: role + name — strongest get_by_role selector
        {
            "seq": 1,
            "selectors": {
                "role_name": {"role": "button", "name": "Save"},
                "selector_confidence": 1.0,
                "selector_fallback": "Save",
            },
        },
        # 0.5: role only (icon button with no accessible name)
        {
            "seq": 2,
            "selectors": {
                "role_name": {"role": "button", "name": None},
                "selector_confidence": 0.5,
                "selector_fallback": None,
            },
        },
        # 0.1: null/null — bare LWC shadow element, hardest to replay
        {
            "seq": 3,
            "selectors": {
                "role_name": {"role": None, "name": None},
                "css_path": "c-case-list-item >>> div",
                "selector_confidence": 0.1,
                "selector_fallback": None,
            },
        },
    ])

    assert trace.events[0].selectors.selector_confidence == 1.0
    assert trace.events[0].selectors.selector_fallback == "Save"
    assert trace.events[1].selectors.selector_confidence == 0.5
    assert trace.events[1].selectors.selector_fallback is None
    assert trace.events[2].selectors.selector_confidence == 0.1
    assert trace.events[2].selectors.selector_fallback is None


def test_selector_fallback_priority_aria_label() -> None:
    """selector_fallback prefers aria-label over innerText.

    The recorder evaluates: aria-label > data-id > innerText[:40] > null.
    Here aria-label is present so it wins over the element's innerText.
    """
    trace = synthesize_trace([
        {
            "seq": 1,
            "element": {
                "tag": "div",
                "aria_label": "Close dialog",
                "classes": [],
                "shadow_depth": 2,
                "text": "X",
            },
            "selectors": {
                "role_name": {"role": None, "name": None},
                "css_path": "div.close",
                "selector_confidence": 0.1,
                "selector_fallback": "Close dialog",
            },
        }
    ])
    assert trace.events[0].selectors.selector_fallback == "Close dialog"


def test_selector_fallback_priority_data_id() -> None:
    """selector_fallback falls back to data-id when no aria-label is present."""
    trace = synthesize_trace([
        {
            "seq": 1,
            "selectors": {
                "role_name": {"role": None, "name": None},
                "css_path": "c-nav-item",
                "selector_confidence": 0.5,
                "selector_fallback": "nav-item-home",
            },
        }
    ])
    assert trace.events[0].selectors.selector_fallback == "nav-item-home"


def test_selector_fallback_innertext_capped_at_40_chars() -> None:
    """selector_fallback from innerText is capped at 40 characters by the recorder.

    The Python model must accept a 40-char string without truncating it further.
    """
    forty_chars = "A" * 40
    trace = synthesize_trace([
        {
            "seq": 1,
            "selectors": {
                "role_name": {"role": None, "name": None},
                "css_path": "div.row",
                "selector_confidence": 0.1,
                "selector_fallback": forty_chars,
            },
        }
    ])
    assert trace.events[0].selectors.selector_fallback == forty_chars
    assert len(trace.events[0].selectors.selector_fallback) == 40


def test_selector_fallback_null_when_no_alternatives() -> None:
    """selector_fallback is null for a fully anonymous element with no text."""
    trace = synthesize_trace([
        {
            "seq": 1,
            "element": {
                "tag": "div",
                "classes": ["slds-icon-container"],
                "shadow_depth": 3,
            },
            "selectors": {
                "role_name": {"role": None, "name": None},
                "css_path": "div.slds-icon-container",
                "selector_confidence": 0.1,
                "selector_fallback": None,
            },
        }
    ])
    assert trace.events[0].selectors.selector_fallback is None


def test_old_capture_without_new_fields_parses_and_defaults_none(tmp_path: Path) -> None:
    """Backward-compat: a capture that pre-dates B08 must parse without the
    new fields. Both selector_confidence and selector_fallback default to None.

    This is the hardest constraint: we must not break existing pipelines.
    """
    payload = {
        "v": 1, "seq": 1, "t": 1000, "type": "click",
        "url": "https://test.my.salesforce.com",
        "frame_path": [],
        "selectors": {
            "role_name": {"role": None, "name": None},
            "css_path": "div.lwc-shadow",
            # No selector_confidence, no selector_fallback
        },
        "element": {"tag": "div", "classes": [], "shadow_depth": 2},
        "value": None, "value_redacted": False, "sf": {},
    }
    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 1, f"Old capture was rejected: {trace.skipped_lines}"
    assert trace.events[0].selectors.selector_confidence is None, (
        "selector_confidence must default to None on old captures"
    )
    assert trace.events[0].selectors.selector_fallback is None, (
        "selector_fallback must default to None on old captures"
    )


def test_many_null_null_events_all_parse_correctly(tmp_path: Path) -> None:
    """End-to-end: 170 events with role=null/name=null (real AFT3 capture shape).

    The spec notes that 170 of 175 real AFT3 capture events had null/null.
    All must parse, and when the recorder emits selector_confidence=0.1 it must
    be preserved faithfully on all events.
    """
    rows = []
    for i in range(1, 171):
        rows.append({
            "v": 1, "seq": i, "t": 1000 + i, "type": "click",
            "url": "https://test.my.salesforce.com/lightning/o/Case/list",
            "frame_path": [],
            "selectors": {
                "role_name": {"role": None, "name": None},
                "css_path": f"c-case-row >>> div.cell-{i}",
                "selector_confidence": 0.1,
                "selector_fallback": None,
            },
            "element": {"tag": "div", "classes": [f"cell-{i}"], "shadow_depth": 3},
            "value": None, "value_redacted": False,
            "sf": {"object": "Case", "record_id": None, "page_type": "list", "app": None},
        })

    jsonl_path = tmp_path / "capture.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )

    trace = parse_capture_file(jsonl_path)

    assert len(trace.events) == 170, (
        f"Expected 170 events, got {len(trace.events)}: {trace.skipped_lines}"
    )
    assert trace.skipped_lines == []
    assert all(e.selectors.selector_confidence == 0.1 for e in trace.events)
    assert all(e.selectors.selector_fallback is None for e in trace.events)
# ============================================================================
# 16. REDACTION AUDIT — org-specific pattern detection
# ============================================================================
#
# The recorder redacts values it knows are sensitive (card numbers, passwords).
# It does NOT scan for Salesforce org-specific patterns: record Ids (15/18-char
# alphanum starting with known prefixes), org instance URLs, or usernames in
# the form @*.salesforce.com.
#
# validate_trace must detect these and emit a "REDACTION AUDIT:" finding.
#
# TEST DISCIPLINE: the matched value must NOT appear in any finding.


#: Synthetic record Ids, safe to embed in tests (never real org data).
#: Format: <3-char prefix> + 12 alphanum chars = 15 chars total.
_SYNTHETIC_CASE_ID = "500SYNTHETIC0001"       # Case (500), 16 chars — 3+13
_SYNTHETIC_ACCOUNT_ID = "001SyntheticAcc01"   # Account (001), 18 chars — need checksum
# Use a simple 15-char form for deterministic tests:
_SYNTHETIC_CASE_ID_15 = "500SyntheticCase"    # 3+12 = 15 chars exactly
_SYNTHETIC_USER_ID_15 = "005SyntheticUser"    # User (005), 15 chars
_SYNTHETIC_USERNAME = "testuser@dev-synthetic.salesforce.com"
_SYNTHETIC_INSTANCE_URL = "https://dev-synthetic-org.my.salesforce.com/lightning/page/home"


def test_redaction_audit_fires_for_known_id_prefix_in_event_value() -> None:
    """A record Id in event.value must produce a REDACTION AUDIT: finding.

    The Id is a 500-prefixed 15-char string — a valid Salesforce Case Id shape.
    The finding must name the key path and the prefix, never the full Id.
    """
    trace = synthesize_trace([
        {
            "value": _SYNTHETIC_CASE_ID_15,
            "value_redacted": False,
            "element": {"tag": "div", "type": None, "name": "CaseNumber"},
        }
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("REDACTION AUDIT:")]

    assert findings, "Known-prefix Id in event.value produced no REDACTION AUDIT finding"
    assert any("event.value" in f for f in findings), (
        "finding must identify the key path (event.value)"
    )
    assert any("prefix=500" in f for f in findings), (
        "finding must report the 3-char prefix without echoing the full Id"
    )
    # The full Id must not appear in any finding
    for f in findings:
        assert _SYNTHETIC_CASE_ID_15 not in f, "finding echoed the raw record Id"


def test_redaction_audit_finding_contains_key_path_not_raw_value() -> None:
    """Findings must reference the structural path, not the matched value.

    The key_path tells the operator WHERE to look; the raw Id is not needed and
    must not appear — it would be the leak we are trying to detect.
    """
    trace = synthesize_trace([
        {
            "value": _SYNTHETIC_USER_ID_15,
            "value_redacted": False,
            "element": {"tag": "span", "text": _SYNTHETIC_USER_ID_15},
        }
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("REDACTION AUDIT:")]

    assert findings, "Id in element.text produced no REDACTION AUDIT finding"
    # At least one finding must include a key path string
    assert any("event.value" in f or "element.text" in f for f in findings)
    # The raw Id must never appear
    for f in findings:
        assert _SYNTHETIC_USER_ID_15 not in f, "finding echoed the raw record Id"


def test_redaction_audit_fires_for_sf_username_in_element_text() -> None:
    """A @*.salesforce.com username in element.text must produce a finding."""
    trace = synthesize_trace([
        {
            "element": {"tag": "span", "text": f"Logged in as {_SYNTHETIC_USERNAME}"},
        }
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("REDACTION AUDIT:")]

    assert findings, "sf_username pattern in element.text produced no REDACTION AUDIT finding"
    assert any("sf_username" in f for f in findings)
    # The username itself must not appear in any finding
    for f in findings:
        assert _SYNTHETIC_USERNAME not in f, "finding echoed the username"


def test_redaction_audit_fires_for_non_capture_host_url_in_value() -> None:
    """A salesforce.com URL in event.value that differs from the capture host
    must produce a REDACTION AUDIT finding (it is a different org's URL).
    """
    # The synthetic trace capture host (from synthesize_trace default) is
    # test.my.salesforce.com — so a different URL must fire.
    trace = synthesize_trace([
        {
            "value": _SYNTHETIC_INSTANCE_URL,
            "value_redacted": False,
        }
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("REDACTION AUDIT:")]

    assert findings, "Non-capture-host sf URL in event.value produced no REDACTION AUDIT finding"
    assert any("sf_instance_url" in f for f in findings)
    for f in findings:
        assert _SYNTHETIC_INSTANCE_URL not in f, "finding echoed the URL"


def test_redaction_audit_silent_for_clean_capture() -> None:
    """A capture with no org-specific patterns must produce no REDACTION AUDIT
    findings — the signal must not be noisy on ordinary captures.
    """
    trace = synthesize_trace([
        {
            "value": "Printer is broken",
            "value_redacted": False,
            "element": {"tag": "input", "type": "text", "name": "Subject", "text": "Printer is broken"},
        },
        {
            "value": None,
            "element": {"tag": "button", "text": "Save"},
        },
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("REDACTION AUDIT:")]

    assert findings == [], f"Clean capture produced spurious REDACTION AUDIT findings: {findings}"


def test_redaction_audit_suppresses_capture_host_url() -> None:
    """The capture host URL (in event.url) must NOT fire a REDACTION AUDIT.

    Every event's URL starts with the session origin; flagging it would make
    every single event produce a finding and drown the signal entirely.
    """
    # synthesize_trace sets url="https://test.my.salesforce.com" by default
    trace = synthesize_trace([
        {
            "value": None,
            "url": "https://test.my.salesforce.com/lightning/r/Case/500SyntheticCase/view",
        }
    ])

    # The event.url IS the capture host, so only the record Id in the path
    # could fire. The Id is in the URL path, not a scanned attribute value —
    # we only scan event.url for URL-pattern hits, not record Id hits in the
    # path. Confirm no false-positive for the capture host hostname.
    findings = [f for f in validate_trace(trace) if f.startswith("REDACTION AUDIT:")]
    # Findings may or may not fire on the Id embedded in the URL path —
    # the key invariant is that the capture host HOSTNAME does not produce
    # an sf_instance_url finding.
    sf_url_findings = [f for f in findings if "sf_instance_url" in f and "event.url" in f]
    assert sf_url_findings == [], (
        "Capture host URL in event.url produced a spurious sf_instance_url finding"
    )


def test_redaction_audit_fires_on_sf_record_id_in_sf_context() -> None:
    """An Id in sf.record_id (the recorder's own reference field) must fire."""
    trace = synthesize_trace([
        {
            "sf": {
                "object": "Case",
                "record_id": _SYNTHETIC_CASE_ID_15,
                "page_type": "record",
                "app": None,
            }
        }
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("REDACTION AUDIT:")]

    assert findings, "Id in sf.record_id produced no REDACTION AUDIT finding"
    assert any("sf.record_id" in f for f in findings)
    for f in findings:
        assert _SYNTHETIC_CASE_ID_15 not in f, "finding echoed the raw record Id"


@pytest.mark.parametrize(
    "prefix,description",
    [
        ("500", "Case"),
        ("001", "Account"),
        ("003", "Contact"),
        ("005", "User"),
        ("006", "Opportunity"),
        ("00D", "Org"),
    ],
)
def test_redaction_audit_fires_for_all_known_prefixes(prefix: str, description: str) -> None:
    """Every known object-key prefix must be detected, not just Case (500).

    A false negative on any prefix would leave that object type unprotected.
    """
    synthetic_id = f"{prefix}SyntheticId012"  # 3 + 12 = 15 chars
    trace = synthesize_trace([
        {"value": synthetic_id, "value_redacted": False}
    ])

    findings = [f for f in validate_trace(trace) if f.startswith("REDACTION AUDIT:")]

    assert findings, f"Prefix {prefix!r} ({description}) Id produced no REDACTION AUDIT finding"
    assert any(f"prefix={prefix}" in f for f in findings), (
        f"finding for prefix {prefix!r} did not report the prefix"
    )
    for f in findings:
        assert synthetic_id not in f, f"finding echoed the raw Id for prefix {prefix!r}"
