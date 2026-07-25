from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer

from sf_video_blueprint.cli import _parse_tracked_records
from sf_video_blueprint.correlation import StepAnalysis
from sf_video_blueprint.html_report import (
    AgentBlueprintSection,
    DataProvenance,
    MasterBlueprintRenderer,
)
from sf_video_blueprint.models import (
    ActionExtractionBundle,
    ActionType,
    EvidenceArtifact,
    EvidenceType,
    ExtractedAction,
)
from sf_video_blueprint.replay import ReplayRunMetadata, ReplayStatus

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


# --- _parse_tracked_records ------------------------------------------------


def test_parse_tracked_records_valid() -> None:
    assert _parse_tracked_records(["Case:500xx0000012345AAA"]) == [("Case", "500xx0000012345AAA")]


def test_parse_tracked_records_strips_whitespace() -> None:
    assert _parse_tracked_records([" Case : 500x "]) == [("Case", "500x")]


def test_parse_tracked_records_multiple() -> None:
    parsed = _parse_tracked_records(["Case:500x", "Account:001y"])
    assert parsed == [("Case", "500x"), ("Account", "001y")]


def test_parse_tracked_records_rejects_missing_colon() -> None:
    """This is the exact case validate_dev_org.sh asserts via a slow live probe."""
    with pytest.raises(typer.BadParameter):
        _parse_tracked_records(["BadFormat"])


def test_parse_tracked_records_keeps_ids_containing_colon() -> None:
    assert _parse_tracked_records(["Case:a:b"]) == [("Case", "a:b")]


def test_parse_tracked_records_empty_list() -> None:
    assert _parse_tracked_records([]) == []


# --- provenance ------------------------------------------------------------


def test_mock_run_is_flagged_simulated() -> None:
    prov = DataProvenance(
        extraction_source="stub", telemetry_source="mock", replay_source="noop"
    )
    assert prov.is_simulated is True
    assert len(prov.simulated_parts) >= 3


def test_fully_live_run_is_not_flagged_simulated() -> None:
    prov = DataProvenance(
        extraction_source="dom-capture",
        telemetry_source="live-org",
        replay_source="browser",
        agent_spec_source="derived",
    )
    assert prov.is_simulated is False
    assert prov.simulated_parts == []


def test_live_telemetry_with_stub_extraction_still_simulated() -> None:
    """Partial realism must not clear the banner."""
    prov = DataProvenance(
        extraction_source="stub", telemetry_source="live-org", replay_source="browser"
    )
    assert prov.is_simulated is True


# --- report rendering ------------------------------------------------------


def _bundle() -> ActionExtractionBundle:
    return ActionExtractionBundle(
        recording_id="rec-test",
        source_video_path="/tmp/x.mp4",
        extracted_at=NOW,
        actions=[
            ExtractedAction(
                step_id="s1",
                sequence=1,
                timestamp_ms=0,
                action_type=ActionType.CLICK,
                target="button:Save",
                confidence=0.5,
            )
        ],
        evidence=[
            EvidenceArtifact(
                artifact_id="e1",
                evidence_type=EvidenceType.VIDEO_FRAME,
                path_or_uri="file:///tmp/x.mp4",
                captured_at=NOW,
                confidence=0.5,
            )
        ],
    )


def _render(provenance: DataProvenance) -> str:
    return MasterBlueprintRenderer().render(
        extraction=_bundle(),
        run=ReplayRunMetadata(
            run_id="run-1",
            org_url="https://example.my.salesforce.com",
            username="u@example.com",
            profile_name="System Administrator",
            role_name=None,
        ),
        analyses=[
            StepAnalysis(
                step_id="s1",
                action_target="button:Save",
                replay_status=ReplayStatus.SUCCESS,
                replay_message="ok",
            )
        ],
        agent_sections=[
            AgentBlueprintSection(
                intent="Update Case (Status)",
                required_entities=["status"],
                orchestration_steps=["step"],
                guardrails=["guard"],
                failure_handling=["handle"],
                derived=True,
            )
        ],
        provenance=provenance,
    )


def test_simulated_report_carries_loud_warning() -> None:
    html = _render(DataProvenance())
    assert "NOT AUDIT EVIDENCE" in html
    assert "SIMULATED DATA" in html


def test_live_report_has_no_simulation_warning() -> None:
    html = _render(
        DataProvenance(
            extraction_source="dom-capture",
            telemetry_source="live-org",
            replay_source="browser",
            agent_spec_source="derived",
        )
    )
    assert "NOT AUDIT EVIDENCE" not in html
    assert "Live org evidence" in html


def test_report_escapes_org_controlled_strings() -> None:
    """Record names and error text come from the org and must not inject markup."""
    bundle = _bundle()
    bundle.actions[0].target = "<script>alert(1)</script>"
    html = MasterBlueprintRenderer().render(
        extraction=bundle,
        run=ReplayRunMetadata(
            run_id="run-1",
            org_url="https://example.my.salesforce.com",
            username="u@example.com",
            profile_name="p",
            role_name=None,
        ),
        analyses=[
            StepAnalysis(
                step_id="s1",
                action_target="<script>alert(1)</script>",
                replay_status=ReplayStatus.SUCCESS,
                replay_message="ok",
            )
        ],
        agent_sections=[],
        provenance=DataProvenance(),
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_write_html_creates_parent_directories(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deeper" / "report.html"
    written = MasterBlueprintRenderer().write_html(
        out,
        _bundle(),
        ReplayRunMetadata(
            run_id="r",
            org_url="https://example.my.salesforce.com",
            username="u",
            profile_name="p",
            role_name=None,
        ),
        [],
        [],
        DataProvenance(),
    )
    assert written.exists()
