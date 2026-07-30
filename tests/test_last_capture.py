"""Tests for the --last-capture flag on the 'sf-blueprint run' command.

Covers:
- ``find_last_capture`` helper: most-recent file is chosen when multiple exist.
- ``_read_event_count_from_manifest`` helper: reads from companion manifest.
- CLI: --last-capture flag selects the most recent capture.
- CLI: --capture overrides --last-capture (--last-capture is ignored with a warning).
- CLI: no files found prints a helpful error and exits non-zero.
- CLI: manifest event count is included in the informational message.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sf_video_blueprint.cli import _read_event_count_from_manifest, app, find_last_capture


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_event(seq: int = 1) -> dict:
    return {
        "v": 1,
        "seq": seq,
        "t": 1700000000000 + seq * 1000,
        "type": "click",
        "url": "https://test.my.salesforce.com/lightning/r/Case/SYNTHETIC_CASE_ID/view",
        "frame_path": [],
        "selectors": {
            "test_id": None,
            "aria": f"[aria-label='Btn{seq}']",
            "role_name": {"role": "button", "name": f"Btn{seq}"},
            "label_for": None,
            "sf_field": None,
            "css_path": f"button.b{seq}",
            "text": f"Btn{seq}",
            "xpath": None,
        },
        "element": {
            "tag": "button",
            "type": None,
            "name": None,
            "id": None,
            "classes": [],
            "aria_label": f"Btn{seq}",
            "text": f"Btn{seq}",
            "is_in_modal": False,
            "modal_label": None,
            "shadow_depth": 0,
        },
        "value": None,
        "value_redacted": False,
        "sf": {
            "object": "Case",
            "record_id": "SYNTHETIC_CASE_ID",
            "page_type": "record_home",
            "app": "Service",
        },
    }


def _write_capture(path: Path, n_events: int = 1) -> Path:
    """Write a minimal valid dom_capture.jsonl file with ``n_events`` events."""
    path.write_text(
        "\n".join(json.dumps(_make_minimal_event(i + 1)) for i in range(n_events)) + "\n",
        encoding="utf-8",
    )
    return path


def _write_manifest(capture_path: Path, event_count: int) -> Path:
    """Write a companion manifest next to *capture_path*."""
    stem = capture_path.name.removesuffix(".dom_capture.jsonl")
    manifest_path = capture_path.parent / f"{stem}.dom_capture.manifest.json"
    manifest_path.write_text(
        json.dumps({"event_count": event_count, "capture_id": "test-manifest"}),
        encoding="utf-8",
    )
    return manifest_path


# ---------------------------------------------------------------------------
# Unit tests: find_last_capture
# ---------------------------------------------------------------------------

def test_find_last_capture_returns_none_if_dir_missing(tmp_path: Path) -> None:
    """No directory -> None, not an exception."""
    result = find_last_capture(tmp_path / "does_not_exist")
    assert result is None


def test_find_last_capture_returns_none_if_no_matches(tmp_path: Path) -> None:
    """Directory exists but has no *.dom_capture.jsonl files."""
    (tmp_path / "other.jsonl").write_text("irrelevant\n", encoding="utf-8")
    result = find_last_capture(tmp_path)
    assert result is None


def test_find_last_capture_single_file(tmp_path: Path) -> None:
    """Single matching file is returned."""
    p = _write_capture(tmp_path / "proc_123.dom_capture.jsonl")
    result = find_last_capture(tmp_path)
    assert result == p


def test_find_last_capture_picks_most_recent(tmp_path: Path) -> None:
    """The most recently modified file is chosen, not the alphabetically last."""
    older = tmp_path / "proc_1000.dom_capture.jsonl"
    newer = tmp_path / "proc_9999.dom_capture.jsonl"
    _write_capture(older)
    # Give them distinct mtimes: newer must be strictly later.
    older.touch()
    time.sleep(0.05)  # ensure mtime difference
    _write_capture(newer)
    result = find_last_capture(tmp_path)
    assert result == newer


def test_find_last_capture_ignores_non_capture_files(tmp_path: Path) -> None:
    """Files that do not end in .dom_capture.jsonl are not returned."""
    _write_capture(tmp_path / "proc_1.dom_capture.jsonl")
    (tmp_path / "proc_2.network.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "proc_3.jsonl").write_text("{}\n", encoding="utf-8")
    result = find_last_capture(tmp_path)
    assert result is not None
    assert result.name == "proc_1.dom_capture.jsonl"


# ---------------------------------------------------------------------------
# Unit tests: _read_event_count_from_manifest
# ---------------------------------------------------------------------------

def test_read_event_count_returns_none_if_no_manifest(tmp_path: Path) -> None:
    """Missing manifest -> None."""
    cap = tmp_path / "proc_1.dom_capture.jsonl"
    cap.write_text("{}\n", encoding="utf-8")
    assert _read_event_count_from_manifest(cap) is None


def test_read_event_count_returns_count_from_manifest(tmp_path: Path) -> None:
    """Companion manifest present -> event_count is returned."""
    cap = tmp_path / "proc_1.dom_capture.jsonl"
    cap.write_text("{}\n", encoding="utf-8")
    _write_manifest(cap, event_count=42)
    assert _read_event_count_from_manifest(cap) == 42


def test_read_event_count_handles_malformed_manifest(tmp_path: Path) -> None:
    """Corrupt manifest -> None, not an exception."""
    cap = tmp_path / "proc_1.dom_capture.jsonl"
    cap.write_text("{}\n", encoding="utf-8")
    stem = cap.name.removesuffix(".dom_capture.jsonl")
    manifest = cap.parent / f"{stem}.dom_capture.manifest.json"
    manifest.write_text("not valid json{{", encoding="utf-8")
    assert _read_event_count_from_manifest(cap) is None


def test_read_event_count_handles_missing_field(tmp_path: Path) -> None:
    """Manifest without event_count field -> None."""
    cap = tmp_path / "proc_1.dom_capture.jsonl"
    cap.write_text("{}\n", encoding="utf-8")
    stem = cap.name.removesuffix(".dom_capture.jsonl")
    manifest = cap.parent / f"{stem}.dom_capture.manifest.json"
    manifest.write_text(json.dumps({"other_field": "value"}), encoding="utf-8")
    assert _read_event_count_from_manifest(cap) is None


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def capture_dir_with_files(tmp_path: Path):
    """Creates a capture dir with an older and a newer capture.

    Returns (capture_dir, older_path, newer_path).
    """
    cap_dir = tmp_path / "outputs" / "capture"
    cap_dir.mkdir(parents=True)

    older = cap_dir / "proc_alpha_1000.dom_capture.jsonl"
    newer = cap_dir / "proc_alpha_9999.dom_capture.jsonl"
    _write_capture(older)
    older.touch()
    time.sleep(0.05)
    _write_capture(newer)

    return cap_dir, older, newer


def test_last_capture_selects_most_recent_file(
    runner: CliRunner,
    capture_dir_with_files,
    tmp_path: Path,
) -> None:
    """--last-capture uses the most recently modified *.dom_capture.jsonl."""
    cap_dir, older, newer = capture_dir_with_files
    out = tmp_path / "report.html"

    result = runner.invoke(
        app,
        [
            "run",
            "--last-capture",
            "--capture-dir", str(cap_dir),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert str(newer) in result.stdout, (
        f"Expected the newer capture {newer} to appear in stdout; got:\n{result.stdout}"
    )
    assert "--last-capture: using" in result.stdout


def test_last_capture_override_by_explicit_capture(
    runner: CliRunner,
    capture_dir_with_files,
    tmp_path: Path,
) -> None:
    """When both --last-capture and --capture are given, --capture wins
    and a warning is emitted."""
    cap_dir, older, newer = capture_dir_with_files
    explicit = tmp_path / "explicit.dom_capture.jsonl"
    _write_capture(explicit)
    out = tmp_path / "report.html"

    result = runner.invoke(
        app,
        [
            "run",
            "--last-capture",
            "--capture", str(explicit),
            "--capture-dir", str(cap_dir),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "WARNING" in result.stdout
    assert "--last-capture is ignored" in result.stdout
    spec_json = out.with_suffix(".agent-spec.json")
    assert spec_json.exists()
    spec = json.loads(spec_json.read_text(encoding="utf-8"))
    assert spec["provenance"]["source_path"] == str(explicit)


def test_last_capture_no_files_prints_helpful_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--last-capture with an empty directory exits non-zero with a helpful message."""
    empty_dir = tmp_path / "empty_captures"
    empty_dir.mkdir()
    out = tmp_path / "report.html"

    result = runner.invoke(
        app,
        [
            "run",
            "--last-capture",
            "--capture-dir", str(empty_dir),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code != 0
    lower = result.stdout.lower()
    assert "dom_capture.jsonl" in lower or "capture" in lower
    assert not out.exists()


def test_last_capture_nonexistent_dir_prints_helpful_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--last-capture when --capture-dir does not exist exits non-zero."""
    missing_dir = tmp_path / "does_not_exist"
    out = tmp_path / "report.html"

    result = runner.invoke(
        app,
        [
            "run",
            "--last-capture",
            "--capture-dir", str(missing_dir),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code != 0
    assert not out.exists()


def test_last_capture_shows_event_count_from_manifest(
    runner: CliRunner, tmp_path: Path
) -> None:
    """When a companion manifest exists, the event count is shown in the message."""
    cap_dir = tmp_path / "captures"
    cap_dir.mkdir()
    cap = cap_dir / "proc_1_1000.dom_capture.jsonl"
    _write_capture(cap, n_events=3)
    _write_manifest(cap, event_count=3)
    out = tmp_path / "report.html"

    result = runner.invoke(
        app,
        [
            "run",
            "--last-capture",
            "--capture-dir", str(cap_dir),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "3 events" in result.stdout


def test_last_capture_without_manifest_still_works(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--last-capture works even when no companion manifest file exists."""
    cap_dir = tmp_path / "captures"
    cap_dir.mkdir()
    cap = cap_dir / "proc_1_1000.dom_capture.jsonl"
    _write_capture(cap, n_events=1)
    out = tmp_path / "report.html"

    result = runner.invoke(
        app,
        [
            "run",
            "--last-capture",
            "--capture-dir", str(cap_dir),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "--last-capture: using" in result.stdout
    assert "events)" not in result.stdout
