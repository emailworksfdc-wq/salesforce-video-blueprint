"""Tests for run_capture_validation in capture/inject.py.

Spec: validate_trace runs automatically after inject finishes writing the JSONL.
The tests cover the three paths specified in the task:

1. Abort on SECURITY CRITICAL or DATA LOSS findings (non-zero exit).
2. Warn on EVIDENCE INCOMPLETE findings (return 0, print warning).
3. Pass on clean trace (return 0, print success message).
4. validate_trace is called with the exact path that was written.

These tests do not require a browser or a real Salesforce org.  They are pure
unit tests that write minimal JSONL files to tmp_path and call
run_capture_validation directly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# run_capture_validation lives in capture/inject.py, which is NOT a package.
# Import it the same way the real callers do: add the capture directory to sys.path
# so the module can be imported as "inject".
import importlib.util
import os

def _import_inject():
    """Import capture/inject.py as a module, patching playwright so it is not required."""
    # Patch playwright before importing inject so we don't need a real browser install.
    pw_mock = MagicMock()
    with patch.dict("sys.modules", {
        "playwright": pw_mock,
        "playwright.sync_api": pw_mock,
    }):
        spec = importlib.util.spec_from_file_location(
            "inject",
            Path(__file__).parent.parent / "capture" / "inject.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


# Import once at module level.
_inject_module = _import_inject()
run_capture_validation = _inject_module.run_capture_validation


# ---------------------------------------------------------------------------
# Minimal event fixture
# ---------------------------------------------------------------------------

_MINIMAL_EVENT = {
    "v": 1,
    "seq": 1,
    "t": 1737830000123,
    "type": "click",
    "url": "https://test.develop.my.salesforce.com",
    "frame_path": [],
    "selectors": {
        "test_id": None,
        "aria": None,
        "role_name": None,
        "label_for": None,
        "sf_field": None,
        "css_path": "button.submit",
        "text": "Submit",
        "xpath": None,
    },
    "element": {
        "tag": "button",
        "type": None,
        "name": None,
        "id": None,
        "classes": ["submit"],
        "aria_label": None,
        "text": "Submit",
        "is_in_modal": False,
        "modal_label": None,
        "shadow_depth": 0,
    },
    "value": None,
    "value_redacted": False,
    "sf": {
        "object": None,
        "record_id": None,
        "page_type": "unknown",
        "app": None,
    },
    "_ingest_seq": 1,
    "_ingest_t": 1737830000999,
    "_frame_url": "https://test.develop.my.salesforce.com",
    "_page_index": 0,
}


def _write_jsonl(path: Path, events: list[dict]) -> None:
    """Write a list of event dicts to a JSONL file."""
    with path.open("w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


# ============================================================================
# Test 1: clean trace -> return 0 and print success message
# ============================================================================


def test_clean_trace_returns_zero_and_prints_success(
    tmp_path: Path, capsys
) -> None:
    """A valid trace with no findings should return 0 and print the success line."""
    jsonl_path = tmp_path / "dom_capture.jsonl"
    _write_jsonl(jsonl_path, [_MINIMAL_EVENT])

    result = run_capture_validation(jsonl_path)

    assert result == 0, "Clean trace must return 0"
    captured = capsys.readouterr()
    assert "✓ capture validated" in captured.out
    assert "no issues found" in captured.out


# ============================================================================
# Test 2: EVIDENCE INCOMPLETE -> return 0 (warn but don't abort)
# ============================================================================


def test_evidence_incomplete_returns_zero_and_prints_warning(
    tmp_path: Path, capsys
) -> None:
    """EVIDENCE INCOMPLETE findings must print a warning but must NOT abort (return 0).

    Construct a trace with some skipped lines to trigger the EVIDENCE INCOMPLETE
    finding (below the 50% DATA LOSS threshold).
    """
    jsonl_path = tmp_path / "dom_capture.jsonl"
    # Write one good event and a few bad lines (just enough to get EVIDENCE INCOMPLETE
    # but below the 50% DATA LOSS cutoff: 2 good, 1 bad = 33% loss).
    with jsonl_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_MINIMAL_EVENT) + "\n")
        f.write(json.dumps({**_MINIMAL_EVENT, "seq": 2, "_ingest_seq": 2}) + "\n")
        f.write("NOT VALID JSON\n")  # 1 bad of 3 = 33% loss -> EVIDENCE INCOMPLETE

    result = run_capture_validation(jsonl_path)

    assert result == 0, "EVIDENCE INCOMPLETE must NOT abort (return 0)"
    captured = capsys.readouterr()
    assert "WARNING" in captured.out or "incomplete" in captured.out.lower()
    # Must NOT be empty output — the operator needs to see the warning.
    assert "EVIDENCE INCOMPLETE" in captured.out


def test_evidence_incomplete_does_not_raise_system_exit(
    tmp_path: Path,
) -> None:
    """EVIDENCE INCOMPLETE must not cause SystemExit even if run from main."""
    jsonl_path = tmp_path / "dom_capture.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_MINIMAL_EVENT) + "\n")
        f.write(json.dumps({**_MINIMAL_EVENT, "seq": 2, "_ingest_seq": 2}) + "\n")
        f.write("NOT VALID JSON\n")

    # Should not raise.
    try:
        result = run_capture_validation(jsonl_path)
    except SystemExit as exc:
        pytest.fail(
            f"run_capture_validation raised SystemExit({exc.code}) on "
            f"EVIDENCE INCOMPLETE finding — must not abort"
        )
    assert result == 0


# ============================================================================
# Test 3a: DATA LOSS -> return 1 (abort)
# ============================================================================


def test_data_loss_returns_one(tmp_path: Path, capsys) -> None:
    """DATA LOSS finding (>=50% loss) must return 1.

    Write more than half the lines as invalid JSON so the DATA LOSS threshold
    is crossed.
    """
    jsonl_path = tmp_path / "dom_capture.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_MINIMAL_EVENT) + "\n")
        # 3 bad lines out of 4 total = 75% loss -> DATA LOSS
        f.write("BAD JSON\n")
        f.write("MORE BAD JSON\n")
        f.write("ALSO BAD JSON\n")

    result = run_capture_validation(jsonl_path)

    assert result == 1, "DATA LOSS finding must return 1"
    captured = capsys.readouterr()
    assert "DATA LOSS" in captured.out or "FAILED" in captured.out


def test_data_loss_zero_events_returns_one(tmp_path: Path, capsys) -> None:
    """Zero events with skipped lines is the 100%-loss case -> DATA LOSS -> return 1."""
    jsonl_path = tmp_path / "dom_capture.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        # Write only unparseable lines.
        f.write("COMPLETELY INVALID\n")
        f.write("ALSO INVALID\n")

    result = run_capture_validation(jsonl_path)

    assert result == 1, "100% data loss (zero events, all lines skipped) must return 1"
    captured = capsys.readouterr()
    assert "DATA LOSS" in captured.out or "FAILED" in captured.out


# ============================================================================
# Test 3b: SECURITY CRITICAL -> return 1 (abort)
# ============================================================================


def test_security_critical_redaction_leak_returns_one(
    tmp_path: Path, capsys
) -> None:
    """SECURITY CRITICAL: value_redacted=True but value present -> return 1.

    This is the redaction-leak scenario: the recorder flagged the value as
    sensitive AND claimed it redacted it, but the value is still present.
    """
    jsonl_path = tmp_path / "dom_capture.jsonl"
    leak_event = {
        **_MINIMAL_EVENT,
        "value": "s3cr3t_password",   # present despite being flagged
        "value_redacted": True,        # recorder claimed it was redacted
    }
    _write_jsonl(jsonl_path, [leak_event])

    result = run_capture_validation(jsonl_path)

    assert result == 1, "SECURITY CRITICAL finding (redaction leak) must return 1"
    captured = capsys.readouterr()
    assert "SECURITY CRITICAL" in captured.out or "FAILED" in captured.out
    # Must NOT echo the leaked value -- that would defeat the purpose.
    assert "s3cr3t_password" not in captured.out


# ============================================================================
# Test 4: validate_trace is called with the exact path that was written
# ============================================================================


def test_validate_trace_called_with_exact_path(tmp_path: Path) -> None:
    """run_capture_validation must pass the exact jsonl_path to parse_capture_file.

    Verifies that the validation path matches the written path: a different file
    (e.g. a differently-named temp file) would silently validate the wrong data.
    """
    jsonl_path = tmp_path / "dom_capture.jsonl"
    _write_jsonl(jsonl_path, [_MINIMAL_EVENT])

    calls_to_parse = []

    original_parse = _inject_module.parse_capture_file

    def spy_parse(path, **kwargs):
        calls_to_parse.append(path)
        return original_parse(path, **kwargs)

    with patch.object(_inject_module, "parse_capture_file", side_effect=spy_parse):
        run_capture_validation(jsonl_path)

    assert len(calls_to_parse) == 1, "parse_capture_file must be called exactly once"
    assert calls_to_parse[0] == jsonl_path, (
        f"parse_capture_file was called with {calls_to_parse[0]!r}, "
        f"expected {jsonl_path!r}"
    )


def test_validate_trace_called_not_different_path(tmp_path: Path) -> None:
    """run_capture_validation must NOT silently validate a different path.

    This is the defect-catch test: if the implementation used a hardcoded path
    or a different variable, this test would fail because the wrong file would
    be passed to parse_capture_file.
    """
    real_path = tmp_path / "real_capture.jsonl"
    other_path = tmp_path / "other_capture.jsonl"
    _write_jsonl(real_path, [_MINIMAL_EVENT])
    _write_jsonl(other_path, [_MINIMAL_EVENT])

    calls_to_parse = []
    original_parse = _inject_module.parse_capture_file

    def spy_parse(path, **kwargs):
        calls_to_parse.append(path)
        return original_parse(path, **kwargs)

    with patch.object(_inject_module, "parse_capture_file", side_effect=spy_parse):
        run_capture_validation(real_path)

    assert calls_to_parse[0] == real_path, (
        "parse_capture_file must be called with exactly real_path, not a different path"
    )
    assert calls_to_parse[0] != other_path


# ============================================================================
# Test 5: output is printed to stdout (not silently swallowed)
# ============================================================================


def test_fatal_finding_output_reaches_stdout(tmp_path: Path, capsys) -> None:
    """Critical findings must be visible on stdout, not silently swallowed."""
    jsonl_path = tmp_path / "dom_capture.jsonl"
    # Zero events with skipped lines triggers DATA LOSS.
    jsonl_path.write_text("INVALID\n", encoding="utf-8")

    run_capture_validation(jsonl_path)

    captured = capsys.readouterr()
    # At minimum, the validation banner and at least one finding must be printed.
    assert "[inject]" in captured.out
    assert len(captured.out.strip()) > 0, "Fatal finding output must not be empty"


def test_clean_output_does_not_print_error_text(tmp_path: Path, capsys) -> None:
    """A clean trace must not print any error/warning text."""
    jsonl_path = tmp_path / "dom_capture.jsonl"
    _write_jsonl(jsonl_path, [_MINIMAL_EVENT])

    run_capture_validation(jsonl_path)

    captured = capsys.readouterr()
    assert "FAILED" not in captured.out
    assert "WARNING" not in captured.out
    assert "DATA LOSS" not in captured.out
    assert "SECURITY" not in captured.out
    assert "EVIDENCE INCOMPLETE" not in captured.out
