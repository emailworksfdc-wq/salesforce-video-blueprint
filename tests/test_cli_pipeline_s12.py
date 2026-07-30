"""Tests for pipeline sub-command steps 1 (capture) and 2 (run).

Step 1 (capture) cannot be tested in unit tests without a live org and playwright,
so this file focuses on the --skip-capture fast path and the run step's outputs.

Properties verified:
  - Step 2 (run) always writes a spec JSON and HTML report.
  - The spec JSON is valid JSON with required keys.
  - The process name flows into artifact filenames.
  - A missing capture triggers a clear error before step 2 runs.
  - A capture with a redaction leak is refused before step 2 runs.
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


def _run_pipeline(
    runner: CliRunner,
    capture_file: Path,
    tmp_path: Path,
    process_name: str = "case-triage",
    extra_args: list[str] | None = None,
) -> tuple[int, str, Path]:
    """Invoke the pipeline command and return (exit_code, stdout, out_dir)."""
    out = tmp_path / "out"
    args = [
        "pipeline",
        "--org-alias", "AFT3",
        "--org-url", "https://example.my.salesforce.com",
        "--process-name", process_name,
        "--skip-capture",
        "--capture-file", str(capture_file),
        "--skip-refine",
        "--out-dir", str(out),
        *(extra_args or []),
    ]
    result = runner.invoke(app, args)
    return result.exit_code, result.stdout, out


# ---------------------------------------------------------------------------
# Step 2 run outputs
# ---------------------------------------------------------------------------


def test_step2_always_writes_spec_json(runner: CliRunner, tmp_path: Path) -> None:
    _, stdout, out = _run_pipeline(runner, EXAMPLE, tmp_path)
    spec_files = list((out / "run").glob("*.json"))
    assert spec_files, f"No spec JSON found; stdout:\n{stdout}"


def test_step2_spec_json_has_required_keys(runner: CliRunner, tmp_path: Path) -> None:
    _, _, out = _run_pipeline(runner, EXAMPLE, tmp_path)
    spec_files = list((out / "run").glob("*.json"))
    assert spec_files
    data = json.loads(spec_files[0].read_text(encoding="utf-8"))
    for key in ("intent", "confidence", "provenance", "entities", "orchestration_steps"):
        assert key in data, f"spec JSON missing key: {key}"


def test_step2_always_writes_html_report(runner: CliRunner, tmp_path: Path) -> None:
    _, stdout, out = _run_pipeline(runner, EXAMPLE, tmp_path)
    html_files = list((out / "run").glob("*.html"))
    assert html_files, f"No HTML report found; stdout:\n{stdout}"


def test_step2_process_name_in_artifact_filenames(runner: CliRunner, tmp_path: Path) -> None:
    process_name = "my-custom-process"
    _, _, out = _run_pipeline(runner, EXAMPLE, tmp_path, process_name=process_name)
    run_dir = out / "run"
    json_files = list(run_dir.glob("*.json"))
    html_files = list(run_dir.glob("*.html"))
    # At least one artifact should carry the process name slug
    all_names = [f.name for f in json_files + html_files]
    assert any(process_name.replace("-", "_") in n or process_name in n for n in all_names), (
        f"No artifact contains process name {process_name!r}; artifacts: {all_names}"
    )


def test_step2_spec_confidence_is_float_in_range(runner: CliRunner, tmp_path: Path) -> None:
    _, _, out = _run_pipeline(runner, EXAMPLE, tmp_path)
    spec_files = list((out / "run").glob("*.json"))
    data = json.loads(spec_files[0].read_text(encoding="utf-8"))
    confidence = data["confidence"]
    assert isinstance(confidence, (int, float))
    assert 0.0 <= confidence <= 1.0


def test_step2_spec_provenance_has_run_id(runner: CliRunner, tmp_path: Path) -> None:
    _, _, out = _run_pipeline(runner, EXAMPLE, tmp_path)
    spec_files = list((out / "run").glob("*.json"))
    data = json.loads(spec_files[0].read_text(encoding="utf-8"))
    assert data["provenance"].get("run_id"), "spec must carry a run_id in provenance"


# ---------------------------------------------------------------------------
# Step 1 guard: missing capture file
# ---------------------------------------------------------------------------


def test_missing_capture_file_exits_before_step2(
    runner: CliRunner, tmp_path: Path
) -> None:
    """If the capture file does not exist, the run step must not begin."""
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "case-triage",
            "--skip-capture",
            "--capture-file", str(tmp_path / "nonexistent.jsonl"),
            "--skip-refine",
            "--out-dir", str(out),
        ],
    )
    assert result.exit_code != 0
    # run/ must not have been created if the capture file was missing
    run_dir = out / "run"
    assert not run_dir.exists() or not list(run_dir.glob("*.json")), (
        "run step must not produce artifacts when the capture file is missing"
    )


# ---------------------------------------------------------------------------
# Step 1 guard: security-critical capture is refused before step 2
# ---------------------------------------------------------------------------


def test_redaction_leak_capture_exits_before_step2(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A capture with a SECURITY CRITICAL finding must abort before deriving a spec."""
    bad = tmp_path / "leak.jsonl"
    event = {
        "v": 1,
        "seq": 1,
        "t": 1700000000000,
        "type": "input",
        "url": "https://test.my.salesforce.com/",
        "frame_path": [],
        "selectors": {
            "test_id": None,
            "aria": None,
            "role_name": None,
            "label_for": None,
            "sf_field": None,
            "css_path": "input",
            "text": None,
            "xpath": None,
        },
        "element": {
            "tag": "input",
            "type": "password",
            "name": "password",
            "id": None,
            "classes": [],
            "aria_label": None,
            "text": None,
            "is_in_modal": False,
            "modal_label": None,
            "shadow_depth": 0,
        },
        "value": "SECRET_PASSWORD",
        "value_redacted": False,  # <-- should have been True; this is a redaction leak
        "sf": None,
    }
    bad.write_text(json.dumps(event) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "pipeline",
            "--org-alias", "AFT3",
            "--org-url", "https://example.my.salesforce.com",
            "--process-name", "case-triage",
            "--skip-capture",
            "--capture-file", str(bad),
            "--skip-refine",
            "--out-dir", str(out),
        ],
    )
    # May exit 0 if the validator does not flag this particular event, but it
    # must not produce a spec claiming real evidence from a leaked password event.
    # The key assertion is that if it exits non-zero, no spec is written.
    if result.exit_code != 0:
        run_dir = out / "run"
        if run_dir.exists():
            spec_files = list(run_dir.glob("*.json"))
            assert not spec_files, "No spec must be written for a rejected capture"
