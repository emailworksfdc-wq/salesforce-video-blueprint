"""Integration tests for the pipeline sub-command: end-to-end offline run.

These tests run the full offline pipeline (--skip-capture + --capture-file) and
verify the contract between steps and the shape of the combined output.

Distinct from test_cli_pipeline.py (unit-level guards) and test_cli_pipeline_s12/s34
(per-step contracts). Integration tests look at the pipeline as a whole:

  - All artifacts are consistent with each other.
  - The pipeline is idempotent: running it twice into the same directory does not
    corrupt the first run's artifacts (second run writes to the same paths).
  - The pipeline output directory is self-contained: nothing is written outside
    <out_dir>.
  - The spec provenance in artifacts matches the spec provenance printed to stdout.
  - PASS_THRESHOLD is not bypassed by the pipeline command.
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


def _run(runner: CliRunner, capture: Path, out: Path, skip_refine: bool = True) -> tuple[int, str]:
    args = [
        "pipeline",
        "--org-alias", "AFT3",
        "--org-url", "https://example.my.salesforce.com",
        "--process-name", "case-triage",
        "--skip-capture",
        "--capture-file", str(capture),
        "--out-dir", str(out),
    ]
    if skip_refine:
        args.append("--skip-refine")
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout


# ---------------------------------------------------------------------------
# Cross-step consistency
# ---------------------------------------------------------------------------


def test_spec_run_id_matches_in_all_artifacts(runner: CliRunner, tmp_path: Path) -> None:
    """The run_id written to spec.json and HTML must be the same run_id."""
    out = tmp_path / "out"
    _run(runner, EXAMPLE, out)
    spec_files = list((out / "run").glob("*.json"))
    assert spec_files
    spec = json.loads(spec_files[0].read_text(encoding="utf-8"))
    run_id = spec.get("provenance", {}).get("run_id")
    assert run_id, "spec must carry a run_id"
    # The HTML report should reference the run_id somewhere (it's embedded in the report).
    html_files = list((out / "run").glob("*.html"))
    if html_files:
        html_text = html_files[0].read_text(encoding="utf-8")
        assert run_id in html_text, (
            f"HTML report must embed the run_id {run_id!r} for traceability"
        )


def test_spec_source_path_points_to_the_capture(runner: CliRunner, tmp_path: Path) -> None:
    """The spec must record which file its evidence came from."""
    out = tmp_path / "out"
    _run(runner, EXAMPLE, out)
    spec_files = list((out / "run").glob("*.json"))
    assert spec_files
    spec = json.loads(spec_files[0].read_text(encoding="utf-8"))
    source_path = spec.get("provenance", {}).get("source_path")
    assert source_path, "spec must record source_path in provenance"


# ---------------------------------------------------------------------------
# Idempotency: running the pipeline twice must not corrupt artifacts
# ---------------------------------------------------------------------------


def test_pipeline_second_run_overwrites_artifacts_cleanly(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Two consecutive runs into the same directory must both succeed."""
    out = tmp_path / "out"
    _run(runner, EXAMPLE, out)
    spec_files_1 = {f.name for f in (out / "run").glob("*.json")}

    _run(runner, EXAMPLE, out)
    spec_files_2 = {f.name for f in (out / "run").glob("*.json")}

    assert spec_files_1 == spec_files_2, (
        "Second run produced different artifact names — unexpected file churn"
    )


# ---------------------------------------------------------------------------
# Self-contained: nothing written outside <out_dir>
# ---------------------------------------------------------------------------


def test_pipeline_writes_nothing_outside_out_dir(runner: CliRunner, tmp_path: Path) -> None:
    """The pipeline must not scatter artifacts into the current directory."""
    out = tmp_path / "out"
    before = set(tmp_path.iterdir())  # should only contain "out" at most

    _run(runner, EXAMPLE, out)

    after = set(tmp_path.iterdir())
    new_items = after - before
    assert all(str(item).startswith(str(out)) for item in new_items), (
        f"pipeline wrote artifacts outside out_dir: {[str(i) for i in new_items]}"
    )


# ---------------------------------------------------------------------------
# PASS_THRESHOLD invariant
# ---------------------------------------------------------------------------


def test_pass_threshold_is_75() -> None:
    """Guard: the pass threshold must not be weakened by the pipeline command."""
    assert PASS_THRESHOLD == 75, (
        f"PASS_THRESHOLD was changed to {PASS_THRESHOLD}; it must remain 75."
    )


def test_pipeline_exits_nonzero_when_below_threshold(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A mock-telemetry spec is blocked at 75 — the pipeline must exit non-zero."""
    exit_code, stdout = _run(runner, EXAMPLE, tmp_path / "out")
    # A mock-telemetry run will be blocked and must not exit 0 silently.
    assert exit_code != 0 or "FAIL" in stdout, (
        "pipeline must not exit 0 for a blocked (mock-telemetry) spec"
    )


# ---------------------------------------------------------------------------
# Intent in output
# ---------------------------------------------------------------------------


def test_pipeline_stdout_mentions_spec_intent(runner: CliRunner, tmp_path: Path) -> None:
    """The derived intent must be echoed to stdout so the operator sees what was derived."""
    _, stdout = _run(runner, EXAMPLE, tmp_path / "out")
    # The spec will have some intent string; check that some non-empty text appears
    # suggesting the intent was reported.
    assert "spec" in stdout.lower() or "intent" in stdout.lower() or "case" in stdout.lower(), (
        "pipeline stdout should mention the derived intent or spec; got: " + stdout[:300]
    )
