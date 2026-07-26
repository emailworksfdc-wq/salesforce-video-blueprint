"""Tests for the shared in-process pipeline API.

The contract under test is not "does it produce a spec" but "does it refuse to
launder a mock run as a real one, and does it surface silent data loss". Those are
the properties every consumer (CLI, MCP server, library user) depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sf_video_blueprint.pipeline import CaptureRejected, PipelineResult, run_pipeline

EXAMPLE = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"
ORG = "https://example-dev.develop.my.salesforce.com"


def test_missing_capture_file_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_pipeline(tmp_path / "nope.jsonl", org_url=ORG)


def test_run_pipeline_on_example_capture_produces_scored_spec() -> None:
    result = run_pipeline(EXAMPLE, org_url=ORG)

    assert isinstance(result, PipelineResult)
    assert result.spec.intent
    assert result.events_parsed > 0
    assert result.actions_extracted > 0
    assert 0 <= result.score.total <= result.score.max_total


def test_mock_run_is_never_reported_as_real_evidence() -> None:
    """The central honesty property.

    Telemetry is mocked in-process, so no run through `run_pipeline` may claim
    real evidence. If this test ever fails, either provenance stopped saying
    "mock" or `markers.REAL_TELEMETRY_SOURCES` grew an entry it should not have —
    both would make a fabricated run indistinguishable from an observed one.
    """
    result = run_pipeline(EXAMPLE, org_url=ORG)

    assert result.provenance["telemetry_source"] == "mock"
    assert result.provenance["replay_source"] == "noop"
    assert result.evidence_is_real is False


def test_mock_run_cannot_pass_the_score_gate() -> None:
    """A mock run must be blocked regardless of how complete the spec looks."""
    result = run_pipeline(EXAMPLE, org_url=ORG)

    assert result.score.passed is False
    assert any("mock" in issue.lower() for issue in result.score.blocking_issues)


def test_capture_that_loses_all_data_is_rejected(tmp_path: Path) -> None:
    """Fail closed: a capture whose lines all fail to parse must not yield a spec.

    Deriving from an empty event list would produce a confident-looking but
    entirely unfounded spec.
    """
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n{unclosed\nalso not json\n", encoding="utf-8")

    with pytest.raises(CaptureRejected) as exc_info:
        run_pipeline(bad, org_url=ORG)

    assert exc_info.value.findings
    assert any("DATA LOSS" in f for f in exc_info.value.findings)


def test_summary_is_json_serializable() -> None:
    """MCP tools and log lines return this dict, so it must survive json.dumps.

    A dataclass or a set leaking into the digest would fail only at the wire,
    which is the worst place to find out.
    """
    summary = run_pipeline(EXAMPLE, org_url=ORG).summary()

    round_tripped = json.loads(json.dumps(summary))
    assert round_tripped["intent"]
    assert round_tripped["evidence_is_real"] is False


def test_summary_reports_skipped_lines_as_count_and_detail() -> None:
    """Partial data loss must be visible, not just total loss.

    The integrity gate only refuses at >=50% loss, so a capture that quietly
    drops a few lines still produces a spec. `skipped_lines` is the only place
    that loss surfaces to a caller.
    """
    result = run_pipeline(EXAMPLE, org_url=ORG)
    summary = result.summary()

    assert summary["skipped_line_count"] == len(result.skipped_lines)
    assert isinstance(summary["skipped_lines"], list)
    for entry in summary["skipped_lines"]:
        assert set(entry) == {"line", "reason"}


def test_partial_loss_proceeds_but_is_recorded(tmp_path: Path) -> None:
    """One bad line among many good ones: the run continues and says so."""
    good_lines = EXAMPLE.read_text(encoding="utf-8").strip().splitlines()
    partial = tmp_path / "partial.jsonl"
    # One unparseable line, well under the 50% loss threshold.
    partial.write_text("\n".join([*good_lines, "{ this is not json"]) + "\n", encoding="utf-8")

    result = run_pipeline(partial, org_url=ORG)

    assert result.events_parsed == len(good_lines)
    assert len(result.skipped_lines) == 1
    line_no, reason = result.skipped_lines[0]
    assert line_no == len(good_lines) + 1
    assert reason


def test_run_id_is_honoured_and_recorded() -> None:
    result = run_pipeline(EXAMPLE, org_url=ORG, run_id="run-fixed-1234")

    assert result.provenance["run_id"] == "run-fixed-1234"


def test_run_pipeline_writes_nothing_to_disk(tmp_path: Path) -> None:
    """The pipeline is side-effect free; persistence is the caller's decision."""
    capture = tmp_path / "capture.jsonl"
    capture.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    run_pipeline(capture, org_url=ORG)

    assert list(tmp_path.iterdir()) == [capture]


def test_pipeline_does_not_import_the_cli() -> None:
    """The pipeline must be usable without typer installed.

    `pipeline.py` used to import its adapters from `cli.py`, which dragged in
    typer and made the module unimportable in a minimal environment (an MCP
    server install, for instance). Guard against the regression.
    """
    import ast

    source = (
        Path(__file__).parent.parent / "src" / "sf_video_blueprint" / "pipeline.py"
    ).read_text(encoding="utf-8")

    imported_modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)

    assert "cli" not in imported_modules
    assert not any(m.endswith("cli") for m in imported_modules)
