#!/usr/bin/env python3
"""Tests for markers.py — the single source of truth for placeholder detection.

Tests MUST verify both directions:
1. Markers ARE caught, including in evidence.detail (the D7 defect)
2. Legitimate real-capture content is NOT falsely flagged
"""
from __future__ import annotations

import pytest

from sf_video_blueprint.markers import (
    PLACEHOLDER_MARKERS,
    STUB_FINGERPRINTS,
    scan_text,
    scan_spec,
    extraction_is_real,
    telemetry_is_real,
)


class TestScanText:
    """Test the basic text scanner."""

    def test_detects_placeholder_markers(self):
        """All PLACEHOLDER_MARKERS are detected."""
        for marker in PLACEHOLDER_MARKERS:
            hits = scan_text(f"prefix {marker} suffix")
            assert marker in hits, f"Failed to detect {marker!r}"

    def test_detects_stub_fingerprints(self):
        """All STUB_FINGERPRINTS are detected."""
        for marker in STUB_FINGERPRINTS:
            hits = scan_text(f"prefix {marker} suffix")
            assert marker in hits, f"Failed to detect {marker!r}"

    def test_clean_text_passes(self):
        """Text without markers passes."""
        clean = "Update Case Status field to Working via Lightning UI"
        assert scan_text(clean) == []

    def test_case_sensitive(self):
        """Markers are case-sensitive (lowercase todo/fixme should not trigger)."""
        assert scan_text("This has TODO") != []
        assert scan_text("This has todo") == []
        assert scan_text("This has FIXME") != []
        assert scan_text("This has fixme") == []

    def test_button_save_not_blocked(self):
        """button:Save was deliberately REMOVED in round 1 (false positive)."""
        # Real DOM capture produces button:Save as legitimate observed evidence
        assert scan_text("SUBMIT on button:Save -> writes Status") == []


class TestScanSpec:
    """Test the full-spec scanner that closes the D7 scope gap."""

    def test_scans_top_level_fields(self):
        """Markers in top-level text fields are caught."""
        spec_dict = {
            "intent": "TODO: derive real intent",
            "objects_touched": ["Case"],
            "orchestration_steps": ["Step with FIXME"],
            "guardrails": ["Lorem ipsum"],
            "failure_handling": ["Handle errors"],
            "unknowns": ["Unknown aspect"],
            "entities": [],
            "evidence": [],
        }
        hits = scan_spec(spec_dict)
        assert "TODO" in hits
        assert "FIXME" in hits
        assert "Lorem ipsum" in hits

    def test_scans_entity_evidence_details(self):
        """Markers in entity.evidence[].detail are caught (D7 defect fix)."""
        spec_dict = {
            "intent": "Update Case Status",
            "confidence": 0.8,
            "objects_touched": ["Case"],
            "entities": [
                {
                    "name": "status",
                    "object_api_name": "Case",
                    "field_api_name": "Status",
                    "evidence": [
                        {"source": "data-delta", "detail": "TODO: capture actual change"},
                        {"source": "ui-action", "detail": "FIXME: better selector"},
                    ],
                }
            ],
            "orchestration_steps": ["Navigate to Case"],
            "guardrails": ["Validate permissions"],
            "failure_handling": ["Handle errors"],
            "unknowns": [],
            "evidence": [],
        }
        hits = scan_spec(spec_dict)
        assert "TODO" in hits, "Failed to scan entity evidence details"
        assert "FIXME" in hits, "Failed to scan entity evidence details"

    def test_scans_spec_evidence_details(self):
        """Markers in spec.evidence[].detail are caught (also missing in D7)."""
        spec_dict = {
            "intent": "Update Case Status",
            "confidence": 0.8,
            "objects_touched": ["Case"],
            "entities": [],
            "orchestration_steps": ["Navigate"],
            "guardrails": [],
            "failure_handling": [],
            "unknowns": [],
            "evidence": [
                {"source": "extraction", "detail": "Lorem ipsum dolor sit amet"},
                {"source": "telemetry", "detail": "backend observed"},
            ],
        }
        hits = scan_spec(spec_dict)
        assert "Lorem ipsum" in hits, "Failed to scan spec evidence details"

    def test_clean_spec_passes(self):
        """A well-formed spec with real content passes."""
        spec_dict = {
            "intent": "Update Case Status field to Working",
            "confidence": 0.85,
            "objects_touched": ["Case"],
            "entities": [
                {
                    "name": "status",
                    "object_api_name": "Case",
                    "field_api_name": "Status",
                    "evidence": [
                        {"source": "data-delta", "detail": "Case.Status changed 'New' -> 'Working'"},
                        {"source": "ui-action", "detail": "INPUT on input:Status at step-002"},
                    ],
                },
                {
                    "name": "recordId",
                    "object_api_name": "Case",
                    "field_api_name": "Id",
                    "evidence": [
                        {"source": "inference", "detail": "a Case record must be identified to act on it"}
                    ],
                },
            ],
            "orchestration_steps": [
                "Resolve and load the target Case record",
                "SUBMIT on button:Save -> writes Status",
                "Return confirmation",
            ],
            "guardrails": [
                "Enforce object- and field-level security on Case",
                "Require explicit user confirmation before writing: Status",
            ],
            "failure_handling": [
                "Observed validation failure during recording: Status must be one of approved values"
            ],
            "unknowns": [],
            "evidence": [
                {"source": "telemetry", "detail": "backend layers observed: validation, workflow"},
                {"source": "extraction", "detail": "3 action(s) in recording"},
            ],
        }
        hits = scan_spec(spec_dict)
        assert hits == [], f"False positives detected: {hits}"

    def test_real_salesforce_names_not_blocked(self):
        """Real Salesforce API names and common patterns are not blocked."""
        spec_dict = {
            "intent": "Update Opportunity Stage",
            "confidence": 0.9,
            "objects_touched": ["Opportunity"],
            "entities": [
                {
                    "name": "stageName",
                    "object_api_name": "Opportunity",
                    "field_api_name": "StageName",
                    "evidence": [
                        {
                            "source": "data-delta",
                            "detail": "Opportunity.StageName changed 'Prospecting' -> 'Closed Won'",
                        }
                    ],
                }
            ],
            "orchestration_steps": ["SUBMIT on button:Save -> writes StageName"],
            "guardrails": ["Enforce FLS"],
            "failure_handling": [],
            "unknowns": [],
            "evidence": [],
        }
        hits = scan_spec(spec_dict)
        assert hits == [], f"Legitimate content blocked: {hits}"


class TestSubstringCollisions:
    """Audit for false positive risk from substring matching."""

    def test_todoist_would_be_blocked(self):
        """TODOIST contains TODO and would be blocked (acceptable risk)."""
        # If a user has an object/Flow literally named "TODOIST", it would be blocked.
        # BUT: Real Salesforce names go through _camel() -> "todoist" (lowercase).
        # Only risk: if the ORIGINAL name appears in evidence.detail.
        hits = scan_text("TODOIST")
        assert "TODO" in hits

    def test_fixme_asap_would_be_blocked(self):
        """FIXME_ASAP would be blocked (acceptable - unlikely real field name)."""
        hits = scan_text("FIXME_ASAP")
        assert "FIXME" in hits

    def test_sample_flow_v2_blocked(self):
        """Sample_Flow_v2 contains Sample_Flow marker and is blocked."""
        hits = scan_text("Sample_Flow_v2")
        assert "Sample_Flow" in hits

    def test_camelcase_names_pass(self):
        """Real entity names (camelCased by spec_builder) do not trigger."""
        # spec_builder._camel() turns "TODOIST" -> "todoist", "TODO_Field__c" -> "todoField"
        # These camelCase forms should NOT trigger the all-caps markers.
        assert scan_text("todoist") == []
        assert scan_text("todoField") == []
        assert scan_text("todoProcessor") == []


class TestProvenanceChecks:
    """Test structural provenance checks (extraction_is_real, telemetry_is_real)."""

    def test_extraction_is_real(self):
        """Only known-real extraction sources pass."""
        assert extraction_is_real("dom-capture") is True
        assert extraction_is_real("cv") is True
        assert extraction_is_real("stub") is False
        assert extraction_is_real("heuristic") is False
        assert extraction_is_real(None) is False
        assert extraction_is_real("") is False
        assert extraction_is_real("unknown-new-source") is False  # fail closed

    def test_telemetry_is_real(self):
        """Only known-real telemetry sources pass."""
        assert telemetry_is_real("live-org") is True
        assert telemetry_is_real("mock") is False
        assert telemetry_is_real(None) is False
        assert telemetry_is_real("") is False
        assert telemetry_is_real("unknown-new-source") is False  # fail closed


class TestD7Regression:
    """Regression test for defect D7 — gate-divergence on evidence details."""

    def test_d7_evidence_detail_markers_now_caught(self):
        """The exact case from D7: markers in evidence details must be caught."""
        spec_dict = {
            "intent": "Update Case Status",
            "confidence": 0.8,
            "objects_touched": ["Case"],
            "entities": [
                {
                    "name": "status",
                    "object_api_name": "Case",
                    "field_api_name": "Status",
                    "evidence": [
                        # These were INVISIBLE to spec_score._score_placeholder_freedom
                        {"source": "data-delta", "detail": "TODO: capture actual field change"},
                        {"source": "ui-action", "detail": "FIXME: need better selector"},
                    ],
                }
            ],
            "orchestration_steps": ["Navigate to Case", "Update Status field"],
            "guardrails": ["Validate permissions"],
            "failure_handling": ["Handle validation errors"],
            "unknowns": [],
            "evidence": [
                # These were ALSO invisible
                {"source": "extraction", "detail": "Lorem ipsum dolor sit amet"},
                {"source": "telemetry", "detail": "backend observed"},
            ],
        }

        hits = scan_spec(spec_dict)
        assert "TODO" in hits, "D7 regression: TODO in entity evidence not caught"
        assert "FIXME" in hits, "D7 regression: FIXME in entity evidence not caught"
        assert "Lorem ipsum" in hits, "D7 regression: Lorem ipsum in spec evidence not caught"

        # Verify the old _score_placeholder_freedom WOULD have missed these
        # (before the fix, it only scanned top-level fields + entity NAMES)
        top_level_only = " ".join(
            [
                spec_dict["intent"],
                *spec_dict["objects_touched"],
                *[e["name"] for e in spec_dict["entities"]],
                *spec_dict["orchestration_steps"],
                *spec_dict["guardrails"],
                *spec_dict["failure_handling"],
                *spec_dict["unknowns"],
            ]
        )
        from sf_video_blueprint.markers import scan_text

        old_scan_hits = scan_text(top_level_only)
        assert old_scan_hits == [], "Old scan should have missed evidence.detail markers"


class TestStubIdentity:
    """Verify stub extractor is caught by IDENTITY, not shared strings."""

    def test_stub_fingerprints_unique_to_stub(self):
        """STUB_FINGERPRINTS appear only in HeuristicVideoExtractor."""
        # These should NEVER appear in real dom_extractor output
        for marker in STUB_FINGERPRINTS:
            # Just verify they're in the list and would be caught
            assert scan_text(marker) == [marker]

    def test_button_save_appears_in_real_capture(self):
        """button:Save appears in BOTH stub AND real capture (why it was removed)."""
        # From extractor.py HeuristicVideoExtractor: target="button:Save"
        # From dom_extractor.py real capture: also produces button:Save
        # This is why button:Save is NOT in the marker list anymore.
        assert "button:Save" not in PLACEHOLDER_MARKERS
        assert "button:Save" not in STUB_FINGERPRINTS

    def test_stub_caught_by_provenance_not_string(self):
        """Stub is now caught by extraction_source, not button:Save string."""
        # If extraction_source is not "dom-capture" or "cv", it's fake
        assert extraction_is_real("stub") is False
        assert extraction_is_real("heuristic") is False
        # Real sources pass
        assert extraction_is_real("dom-capture") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
