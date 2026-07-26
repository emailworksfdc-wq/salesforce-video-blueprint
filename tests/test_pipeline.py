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


# ---------------------------------------------------------------------------
# DEFECT L4-7: loss below the fail-closed threshold must reach the caller as a
# ratio, not only as a raw count of skipped lines.
#
# Before the fix, `PipelineResult` exposed `skipped_lines` and nothing else. A
# consumer had to know the denominator to judge whether 3 skipped lines mattered,
# and there was no channel at all for events the recorder wrote but the parser
# never saw (a truncated file has ZERO skipped lines). Both are tested here.
# ---------------------------------------------------------------------------


def _lossy_capture(tmp_path: Path, *, keep: int, corrupt: int) -> Path:
    """Write a capture with `keep` good lines and `corrupt` unparseable ones."""
    good = EXAMPLE.read_text(encoding="utf-8").strip().splitlines()[:keep]
    assert len(good) == keep, f"example capture has fewer than {keep} lines"
    bad = ["{ not json" for _ in range(corrupt)]
    target = tmp_path / f"lossy_{keep}_{corrupt}.jsonl"
    target.write_text("\n".join([*good, *bad]) + "\n", encoding="utf-8")
    return target


def test_result_exposes_loss_ratio_not_just_a_count(tmp_path: Path) -> None:
    """3 of 8 lines lost is 37.5% — the caller must not have to compute that.

    A bare `len(skipped_lines) == 3` reads as negligible. The ratio is the number
    that tells a consumer the spec is built on two thirds of a session.
    """
    result = run_pipeline(_lossy_capture(tmp_path, keep=5, corrupt=3), org_url=ORG)

    assert len(result.skipped_lines) == 3
    assert result.loss_ratio == pytest.approx(3 / 8)
    assert result.summary()["loss_ratio"] == pytest.approx(0.375)


def test_loss_ratio_is_zero_for_a_clean_capture() -> None:
    result = run_pipeline(EXAMPLE, org_url=ORG)

    assert result.loss_ratio == 0.0
    assert result.summary()["loss_ratio"] == 0.0


def test_evidence_is_complete_is_separate_from_evidence_is_real(tmp_path: Path) -> None:
    """Realness and completeness are different questions.

    A partially-discarded capture is still real dom-capture evidence — the events
    that survived were genuinely observed. It is not COMPLETE. Collapsing the two
    would either launder a partial capture as whole or refuse a real one.
    """
    lossy = run_pipeline(_lossy_capture(tmp_path, keep=6, corrupt=2), org_url=ORG)

    assert lossy.provenance["extraction_source"] == "dom-capture"
    assert lossy.evidence_is_complete is False
    assert lossy.summary()["evidence_is_complete"] is False

    clean = run_pipeline(EXAMPLE, org_url=ORG)
    assert clean.evidence_is_complete is True
    assert clean.summary()["evidence_is_complete"] is True


def test_manifest_gap_is_none_when_there_is_no_manifest(tmp_path: Path) -> None:
    """No manifest means the gap is UNKNOWABLE, and must not be reported as 0.

    Reporting 0 would assert that nothing was lost, on no evidence whatsoever.
    Copied to a bare directory so no sibling manifest is discoverable — the
    shipped example deliberately has one.
    """
    orphan = tmp_path / "orphan.jsonl"
    orphan.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")

    result = run_pipeline(orphan, org_url=ORG)

    assert result.manifest_gap is None
    assert result.summary()["manifest_gap"] is None
    # Unknown gap must not by itself mark the evidence incomplete...
    assert result.evidence_is_complete is True


def test_manifest_gap_is_zero_when_the_manifest_agrees() -> None:
    """The shipped example has a manifest claiming exactly the events present.

    0 and None are different answers: 0 is a witnessed "nothing lost", None is
    "no witness". This is also an end-to-end check that manifest discovery
    (defect L4-5) reaches the pipeline layer.
    """
    result = run_pipeline(EXAMPLE, org_url=ORG)

    assert result.manifest_gap == 0
    assert result.evidence_is_complete is True


def test_truncated_capture_surfaces_a_manifest_gap(tmp_path: Path) -> None:
    """The loss channel that leaves NO skipped lines behind.

    If the recorder wrote 8 events and the file only holds 5, every line in the
    file parses cleanly: `skipped_lines` is empty and `loss_ratio` is 0.0. Only
    the manifest witnesses this, which is why defect L4-5 (manifest wiring) had
    to be fixed before this one could work.
    """
    good = EXAMPLE.read_text(encoding="utf-8").strip().splitlines()
    capture = tmp_path / "truncated.jsonl"
    capture.write_text("\n".join(good[:5]) + "\n", encoding="utf-8")
    manifest = json.loads(
        (EXAMPLE.parent / "case_triage.dom_capture.manifest.json").read_text(
            encoding="utf-8"
        )
    )
    manifest["event_count"] = len(good)
    (tmp_path / "truncated.manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    result = run_pipeline(capture, org_url=ORG)

    assert result.skipped_lines == []
    assert result.loss_ratio == 0.0
    assert result.manifest_gap == len(good) - 5
    assert result.evidence_is_complete is False
    assert result.summary()["manifest_gap"] == len(good) - 5


def test_summary_always_carries_the_loss_keys() -> None:
    """Consumers must be able to read these unconditionally.

    A key that only appears when loss occurred forces every consumer to use
    `.get()` and makes "absent" indistinguishable from "no loss".
    """
    summary = run_pipeline(EXAMPLE, org_url=ORG).summary()

    for key in ("loss_ratio", "manifest_gap", "evidence_is_complete"):
        assert key in summary, f"summary() must always report {key}"
    json.dumps(summary)  # still wire-safe with the new keys
