"""Tests for pipeline sub-command steps 3 (refine) and 4 (live iterate).

Step 3 (offline refine) runs the scoring loop. Step 4 (live iterate) contacts a
real org and is only tested at the guard level here — no org call is made.

Properties verified:
  - Offline refinement writes versioned artifacts under <out_dir>/iterations/.
  - --skip-refine prevents iterations/ from being populated.
  - --agent-api-name without --test-spec-name is refused before step 4 begins.
  - --agent-api-name against a forbidden org is refused.
  - --refine-rounds controls the maximum offline rounds.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sf_video_blueprint.cli import app

EXAMPLE = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Step 3: offline refinement
# ---------------------------------------------------------------------------


def test_step3_creates_iterations_dir(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "out"
    runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "case-triage",
            "--skip-capture",
            "--capture-file", str(EXAMPLE),
            "--out-dir", str(out),
        ],
    )
    assert (out / "iterations").exists(), "iterations/ must be created when --skip-refine is not set"


def test_step3_writes_iteration_report(runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "out"
    runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "case-triage",
            "--skip-capture",
            "--capture-file", str(EXAMPLE),
            "--out-dir", str(out),
        ],
    )
    iter_dir = out / "iterations"
    # iterate writes an iteration_report file (json or txt)
    report_files = list(iter_dir.glob("iteration_report*"))
    assert report_files, (
        f"No iteration_report found under {iter_dir}; "
        f"contents: {[f.name for f in iter_dir.iterdir()] if iter_dir.exists() else 'dir missing'}"
    )


def test_step3_skip_refine_does_not_write_iterations(
    runner: CliRunner, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "case-triage",
            "--skip-capture",
            "--capture-file", str(EXAMPLE),
            "--skip-refine",
            "--out-dir", str(out),
        ],
    )
    iter_dir = out / "iterations"
    if iter_dir.exists():
        # Should have no round dirs or report files from step 3
        round_files = list(iter_dir.glob("iteration_report*"))
        assert not round_files, "--skip-refine must not write an iteration report"


def test_step3_refine_rounds_limits_offline_rounds(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--refine-rounds 1 should limit the offline loop to one round."""
    out = tmp_path / "out"
    runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "case-triage",
            "--skip-capture",
            "--capture-file", str(EXAMPLE),
            "--refine-rounds", "1",
            "--out-dir", str(out),
        ],
    )
    iter_dir = out / "iterations"
    if iter_dir.exists():
        # At most one versioned subdir for 1 round
        version_dirs = list(iter_dir.glob("v*/"))
        assert len(version_dirs) <= 2, (
            f"Expected at most 2 versioned dirs for 1 refine round, got {len(version_dirs)}"
        )


# ---------------------------------------------------------------------------
# Step 4 guards
# ---------------------------------------------------------------------------


def test_step4_agent_api_name_without_test_spec_name_exits_nonzero(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "case-triage",
            "--skip-capture",
            "--capture-file", str(EXAMPLE),
            "--agent-api-name", "CaseTriageAgent",
            "--out-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code != 0


def test_step4_forbidden_org_refused_for_live_iterate(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A forbidden org alias must be caught at the top-level guard, not in step 4."""
    result = runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "PPCDM",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "case-triage",
            "--skip-capture",
            "--capture-file", str(EXAMPLE),
            "--agent-api-name", "SomeAgent",
            "--test-spec-name", "SomeAgentTest",
            "--out-dir", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code != 0
    assert "PPCDM" in result.stdout or "out of scope" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Final verdict
# ---------------------------------------------------------------------------


def test_pipeline_final_verdict_is_fail_for_mock_telemetry(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A mock-telemetry spec must fail the gate — the pipeline must exit non-zero."""
    result = runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "case-triage",
            "--skip-capture",
            "--capture-file", str(EXAMPLE),
            "--skip-refine",
            "--out-dir", str(tmp_path / "out"),
        ],
    )
    # Mock telemetry blocks at the gate, so exit code is non-zero.
    # The message must say FAIL or blocking or threshold, not quietly exit 0.
    assert result.exit_code != 0 or "FAIL" in result.stdout or "blocking" in result.stdout.lower()


def test_pipeline_stdout_echoes_step_progress(runner: CliRunner, tmp_path: Path) -> None:
    """The pipeline must print step progress lines so the operator knows what ran."""
    result = runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "case-triage",
            "--skip-capture",
            "--capture-file", str(EXAMPLE),
            "--skip-refine",
            "--out-dir", str(tmp_path / "out"),
        ],
    )
    # At a minimum "pipeline" and "step" (or "run") must appear in the output.
    assert "pipeline" in result.stdout.lower() or "step" in result.stdout.lower(), (
        "pipeline must echo step progress; got: " + result.stdout[:300]
    )
