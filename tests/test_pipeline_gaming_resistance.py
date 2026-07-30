"""Gaming resistance tests for the pipeline command and run_pipeline_full MCP tool.

An adversarial user might try to get a passing score from the pipeline by:
  1. Supplying a fabricated capture file with dense but hollow evidence.
  2. Passing a capture that trips the redaction validator (to abort cleanly).
  3. Calling run_pipeline_full with extreme refine_rounds hoping the scorer relaxes.
  4. Passing output that has the right shape but wrong provenance.

These tests verify the gate does not weaken under any of those scenarios.

Security invariant: PASS_THRESHOLD must stay at 75.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sf_video_blueprint.cli import app
from sf_video_blueprint.spec_score import PASS_THRESHOLD

EXAMPLE = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Gate constants
# ---------------------------------------------------------------------------


def test_pass_threshold_constant_is_75() -> None:
    """The single most important number in the project must never change."""
    assert PASS_THRESHOLD == 75, (
        f"PASS_THRESHOLD was changed to {PASS_THRESHOLD}! It must remain 75 forever. "
        "Raising the bar is fine; lowering it is a defect."
    )


def test_pass_threshold_is_imported_not_redefine(tmp_path: Path) -> None:
    """cli.py must import PASS_THRESHOLD from spec_score, not redefine it locally."""
    import ast

    source = (
        Path(__file__).parent.parent / "src" / "sf_video_blueprint" / "cli.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Check there's no assignment PASS_THRESHOLD = <anything> in the top-level scope
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PASS_THRESHOLD":
                    pytest.fail(
                        "cli.py redefines PASS_THRESHOLD locally — "
                        "it must import it from spec_score"
                    )


# ---------------------------------------------------------------------------
# Mock telemetry cannot pass
# ---------------------------------------------------------------------------


def test_cli_pipeline_mock_telemetry_cannot_pass(runner: CliRunner, tmp_path: Path) -> None:
    """Any offline pipeline run (mock telemetry) must exit non-zero or print FAIL."""
    result = runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "gaming-test",
            "--skip-capture",
            "--capture-file", str(EXAMPLE),
            "--skip-refine",
            "--out-dir", str(tmp_path / "out"),
        ],
    )
    # A blocked spec (mock telemetry) must not exit 0 silently.
    assert result.exit_code != 0 or "FAIL" in result.stdout or "block" in result.stdout.lower()


def test_cli_pipeline_dense_hollow_capture_cannot_pass(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A capture with many events but hollow selectors must not pass the gate."""
    # Build a capture with many identical click events (padding attack)
    event_template = {
        "v": 1,
        "type": "click",
        "url": "https://test.my.salesforce.com/lightning/r/Case/500XXX/view",
        "frame_path": [],
        "selectors": {
            "test_id": None, "aria": None, "role_name": None,
            "label_for": None, "sf_field": None,
            "css_path": "div.x", "text": None, "xpath": None,
        },
        "element": {
            "tag": "div", "type": None, "name": None, "id": None,
            "classes": [], "aria_label": None, "text": None,
            "is_in_modal": False, "modal_label": None, "shadow_depth": 3,
        },
        "value": None,
        "value_redacted": False,
        "sf": None,
    }
    lines = []
    for i in range(50):
        event = dict(event_template, seq=i + 1, t=1700000000000 + i * 1000)
        lines.append(json.dumps(event))

    cap = tmp_path / "hollow.jsonl"
    cap.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "hollow-test",
            "--skip-capture",
            "--capture-file", str(cap),
            "--skip-refine",
            "--out-dir", str(tmp_path / "out"),
        ],
    )
    # Must not pass the gate on hollow evidence
    assert result.exit_code != 0 or "FAIL" in result.stdout or "block" in result.stdout.lower()


# ---------------------------------------------------------------------------
# MCP tool: gaming resistance (only when mcp extra is installed)
# ---------------------------------------------------------------------------


def _mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _mcp_available(), reason="mcp extra not installed")
def test_mcp_run_pipeline_full_mock_cannot_pass() -> None:
    from sf_video_blueprint import mcp_server

    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["ok"] is True
    assert result["passed"] is False
    assert result["blockingIssues"]


@pytest.mark.skipif(not _mcp_available(), reason="mcp extra not installed")
def test_mcp_run_pipeline_full_many_rounds_cannot_pass() -> None:
    """More offline rounds must not cause a mock-telemetry run to pass."""
    from sf_video_blueprint import mcp_server

    result = mcp_server.run_pipeline_full(
        str(EXAMPLE), skip_refine=False, refine_rounds=5
    )
    assert result["ok"] is True
    assert result["passed"] is False, (
        "A mock-telemetry run must not pass the gate even after 5 refinement rounds"
    )


@pytest.mark.skipif(not _mcp_available(), reason="mcp extra not installed")
def test_mcp_run_pipeline_full_evidence_is_real_is_always_false() -> None:
    from sf_video_blueprint import mcp_server

    for skip in (True, False):
        result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=skip, refine_rounds=1)
        assert result["ok"] is True
        assert result["evidenceIsReal"] is False, (
            f"evidenceIsReal was True with skip_refine={skip} — "
            "no MCP call should ever claim real evidence"
        )


@pytest.mark.skipif(not _mcp_available(), reason="mcp extra not installed")
def test_mcp_pass_threshold_cannot_be_bypassed_by_run_pipeline_full() -> None:
    from sf_video_blueprint import mcp_server

    result = mcp_server.run_pipeline_full(str(EXAMPLE), skip_refine=True)
    assert result["passThreshold"] == 75
    assert PASS_THRESHOLD == 75


# ---------------------------------------------------------------------------
# Marker set invariants
# ---------------------------------------------------------------------------


def test_real_telemetry_sources_set_is_unchanged() -> None:
    """REAL_TELEMETRY_SOURCES must remain {"live-org"} — adding "mock" is a defect."""
    from sf_video_blueprint.markers import REAL_TELEMETRY_SOURCES

    assert REAL_TELEMETRY_SOURCES == {"live-org"}, (
        f"REAL_TELEMETRY_SOURCES was changed to {REAL_TELEMETRY_SOURCES!r}. "
        "It must remain exactly {{'live-org'}}."
    )


def test_real_extraction_sources_set_is_unchanged() -> None:
    """REAL_EXTRACTION_SOURCES must remain {"dom-capture","cv"}."""
    from sf_video_blueprint.markers import REAL_EXTRACTION_SOURCES

    assert REAL_EXTRACTION_SOURCES == {"dom-capture", "cv"}, (
        f"REAL_EXTRACTION_SOURCES was changed to {REAL_EXTRACTION_SOURCES!r}. "
        'It must remain exactly {{"dom-capture","cv"}}.'
    )
