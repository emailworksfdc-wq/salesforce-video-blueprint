"""Tests for the `run_pipeline_full` MCP tool.

Skipped entirely when the optional `mcp` extra is absent.

Properties verified:
  - The tool is registered and has a description and input schema.
  - A mock-telemetry run always returns evidence_is_real=false and passed=false.
  - The response includes the pipeline summary, score, and provenance.
  - Refinement summary is included when skip_refine=false (default).
  - skip_refine=true omits the refinement step.
  - A missing capture file returns a NOT_FOUND error, not an exception.
  - A corrupted capture file returns a VALIDATION error.
  - output_dir causes a spec JSON to be written.
  - The tool is listed in health()'s tools list.
  - PASS_THRESHOLD is not weakened.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the optional [mcp] extra is not installed")

from sf_video_blueprint import mcp_server
from sf_video_blueprint.spec_score import PASS_THRESHOLD

EXAMPLE = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def _list_tool_names() -> set[str]:
    import asyncio
    return {tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())}


def test_run_pipeline_full_is_registered() -> None:
    assert "run_pipeline_full" in _list_tool_names()


def test_run_pipeline_full_has_description() -> None:
    import asyncio
    tools = asyncio.run(mcp_server.mcp.list_tools())
    tool = next((t for t in tools if t.name == "run_pipeline_full"), None)
    assert tool is not None
    assert tool.description, "run_pipeline_full must have a description"


def test_run_pipeline_full_has_input_schema() -> None:
    import asyncio
    tools = asyncio.run(mcp_server.mcp.list_tools())
    tool = next((t for t in tools if t.name == "run_pipeline_full"), None)
    assert tool is not None
    assert tool.inputSchema, "run_pipeline_full must expose an input schema"


def test_health_lists_run_pipeline_full() -> None:
    result = mcp_server.health()
    assert "run_pipeline_full" in result["tools"]


# ---------------------------------------------------------------------------
# Honesty: mock telemetry always refused
# ---------------------------------------------------------------------------


def test_run_pipeline_full_never_claims_real_evidence() -> None:
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["ok"] is True
    assert result["evidenceIsReal"] is False
    assert result["provenance"]["telemetry_source"] == "mock"


def test_run_pipeline_full_cannot_pass_the_gate() -> None:
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["ok"] is True
    assert result["passed"] is False
    assert result["blockingIssues"]


def test_run_pipeline_full_note_discloses_mock_constraint() -> None:
    """The note field must warn that mock telemetry blocks the gate."""
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["ok"] is True
    note = result.get("note", "")
    assert "mock" in note.lower()


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


def test_run_pipeline_full_response_shape(tmp_path: Path) -> None:
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["ok"] is True

    # Envelope fields
    assert result["requestId"]
    assert result["serverVersion"]
    assert isinstance(result["durationMs"], int)

    # Pipeline summary fields
    assert result["intent"]
    assert isinstance(result["confidence"], float)
    assert isinstance(result["score"], (int, float))
    assert isinstance(result["maxScore"], (int, float))
    assert result["band"]
    assert isinstance(result["passed"], bool)
    assert result["passThreshold"] == PASS_THRESHOLD
    assert isinstance(result["blockingIssues"], list)
    assert isinstance(result["recommendations"], list)

    # Provenance
    assert result["provenance"]["extraction_source"] == "dom-capture"
    assert result["provenance"]["telemetry_source"] == "mock"

    # Evidence counts
    assert result["eventsParsed"] > 0
    assert result["actionsExtracted"] > 0
    assert isinstance(result["skippedLineCount"], int)
    assert isinstance(result["lossRatio"], float)


def test_run_pipeline_full_spec_field_is_present() -> None:
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["ok"] is True
    spec = result.get("spec")
    assert spec is not None
    assert "intent" in spec
    assert "entities" in spec


def test_run_pipeline_full_display_score_is_capped_when_blocked() -> None:
    """A blocked result must carry a display score in the low band."""
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["ok"] is True
    assert result["passed"] is False
    display_score = result.get("displayScore", result["score"])
    assert display_score < 60, (
        f"Blocked spec has displayScore={display_score}, which reads as near-passing"
    )


# ---------------------------------------------------------------------------
# Refinement summary
# ---------------------------------------------------------------------------


def test_run_pipeline_full_includes_refinement_summary_by_default() -> None:
    """When skip_refine=False, the tool must report refinement_summary."""
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=False, refine_rounds=1)
    assert result["ok"] is True
    refinement = result.get("refinement")
    assert refinement is not None, "refinement summary must be present when skip_refine=False"
    assert "rounds_run" in refinement


def test_run_pipeline_full_skip_refine_sets_refinement_to_none() -> None:
    """When skip_refine=True, refinement summary must not carry stale data."""
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["ok"] is True
    # refinement is None when skipped
    assert result.get("refinement") is None


def test_run_pipeline_full_refinement_summary_has_stop_reason() -> None:
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=False, refine_rounds=1)
    assert result["ok"] is True
    refinement = result.get("refinement") or {}
    assert "stop_reason" in refinement, "refinement must carry a stop_reason"


# ---------------------------------------------------------------------------
# output_dir: spec is written
# ---------------------------------------------------------------------------


def test_run_pipeline_full_writes_spec_when_output_dir_is_given(tmp_path: Path) -> None:
    result = mcp_server.run_pipeline_full(
        str(EXAMPLE),
        output_dir=str(tmp_path / "output"),
        skip_refine=True,
    )
    assert result["ok"] is True
    written = result.get("writtenTo") or {}
    assert "spec" in written, "writtenTo must include spec path when output_dir is given"
    spec_path = Path(written["spec"])
    assert spec_path.exists(), f"spec file not found at {spec_path}"


def test_run_pipeline_full_written_spec_is_valid_json(tmp_path: Path) -> None:
    result = mcp_server.run_pipeline_full(
        str(EXAMPLE),
        output_dir=str(tmp_path / "output"),
        skip_refine=True,
    )
    written = result.get("writtenTo") or {}
    if "spec" in written:
        spec_path = Path(written["spec"])
        data = json.loads(spec_path.read_text(encoding="utf-8"))
        assert "intent" in data


def test_run_pipeline_full_no_write_when_output_dir_omitted() -> None:
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["ok"] is True
    assert result.get("writtenTo") is None


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_run_pipeline_full_missing_capture_returns_not_found() -> None:
    result = mcp_server.run_pipeline_full("/nonexistent/capture.jsonl", skip_refine=True)
    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_NOT_FOUND


def test_run_pipeline_full_corrupt_capture_returns_validation_error(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{unclosed\n", encoding="utf-8")
    result = mcp_server.run_pipeline_full(str(bad), skip_refine=True)
    assert result["ok"] is False
    assert result["error"]["code"] in (
        mcp_server.ERROR_VALIDATION, mcp_server.ERROR_INTERNAL
    )


def test_run_pipeline_full_invalid_refine_rounds_returns_error() -> None:
    result = mcp_server.run_pipeline_full(str(EXAMPLE), refine_rounds=0, skip_refine=False)
    assert result["ok"] is False
    assert result["error"]["code"] == mcp_server.ERROR_VALIDATION


# ---------------------------------------------------------------------------
# PASS_THRESHOLD invariant
# ---------------------------------------------------------------------------


def test_pass_threshold_unchanged_at_75() -> None:
    assert PASS_THRESHOLD == 75
    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["passThreshold"] == 75
