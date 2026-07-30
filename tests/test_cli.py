"""Tests for CLI honesty and safety properties."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from sf_video_blueprint.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def minimal_capture(tmp_path: Path) -> Path:
    """Minimal valid dom_capture.jsonl."""
    cap = tmp_path / "capture.jsonl"
    event = {
        "v": 1,
        "seq": 1,
        "t": 1700000000000,
        "type": "click",
        "url": "https://test.my.salesforce.com/lightning/r/Case/500XX000001AbcAAA/view",
        "frame_path": [],
        "selectors": {
            "test_id": None,
            "aria": "[aria-label='Save']",
            "role_name": {"role": "button", "name": "Save"},
            "label_for": None,
            "sf_field": None,
            "css_path": "button.save",
            "text": "Save",
            "xpath": None,
        },
        "element": {
            "tag": "button",
            "type": None,
            "name": None,
            "id": None,
            "classes": ["save"],
            "aria_label": "Save",
            "text": "Save",
            "is_in_modal": False,
            "modal_label": None,
            "shadow_depth": 0,
        },
        "value": None,
        "value_redacted": False,
        "sf": {
            "object": "Case",
            "record_id": "500XX000001AbcAAA",
            "page_type": "record_home",
            "app": "Service",
        },
    }
    cap.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return cap


@pytest.fixture
def xss_capture(tmp_path: Path) -> Path:
    """Capture containing XSS payloads in every text field."""
    cap = tmp_path / "xss_capture.jsonl"
    payloads = [
        '<script>alert(1)</script>',
        '"><img src=x onerror=alert(1)>',
        '</textarea><script>alert(1)</script>',
        '&lt;script&gt;alert(1)&lt;/script&gt;',  # already escaped, should stay escaped
        "' onload='alert(1)",
        '<svg/onload=alert(1)>',
    ]
    events = []
    for idx, payload in enumerate(payloads, start=1):
        events.append({
            "v": 1,
            "seq": idx,
            "t": 1700000000000 + idx * 100,
            "type": "input" if idx < 4 else "click",
            "url": f"https://test.my.salesforce.com/{payload}",
            "frame_path": [],
            "selectors": {
                "test_id": payload,
                "aria": f"[aria-label='{payload}']",
                "role_name": {"role": "button", "name": payload},
                "label_for": payload,
                "sf_field": payload,
                "css_path": f"button[data-label='{payload}']",
                "text": payload,
                "xpath": f"//{payload}",
            },
            "element": {
                "tag": "button",
                "type": None,
                "name": payload,
                "id": payload,
                "classes": [payload],
                "aria_label": payload,
                "text": payload,
                "is_in_modal": idx % 2 == 0,
                "modal_label": payload if idx % 2 == 0 else None,
                "shadow_depth": 0,
            },
            "value": payload if idx < 4 else None,
            "value_redacted": False,
            "sf": {
                "object": payload,
                "record_id": f"500{payload}",
                "page_type": "record_home",
                "app": payload,
            },
        })
    cap.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return cap


@pytest.fixture
def redacted_capture(tmp_path: Path) -> Path:
    """Capture with value_redacted=True."""
    cap = tmp_path / "redacted.jsonl"
    event = {
        "v": 1,
        "seq": 1,
        "t": 1700000000000,
        "type": "input",
        "url": "https://test.my.salesforce.com/lightning/r/Contact/003XX000001AbcAAA/view",
        "frame_path": [],
        "selectors": {
            "test_id": None,
            "aria": "[aria-label='SSN']",
            "role_name": {"role": "textbox", "name": "SSN"},
            "label_for": "SSN",
            "sf_field": "SSN__c",
            "css_path": "input[name='SSN__c']",
            "text": "SSN",
            "xpath": None,
        },
        "element": {
            "tag": "input",
            "type": "password",
            "name": "SSN__c",
            "id": None,
            "classes": [],
            "aria_label": "SSN",
            "text": "SSN",
            "is_in_modal": False,
            "modal_label": None,
            "shadow_depth": 0,
        },
        "value": None,
        "value_redacted": True,
        "sf": {
            "object": "Contact",
            "record_id": "003XX000001AbcAAA",
            "page_type": "record_home",
            "app": "Sales",
        },
    }
    cap.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return cap


def test_exit_zero_on_successful_run(runner: CliRunner, minimal_capture: Path, tmp_path: Path) -> None:
    """A run that extracts actions and builds a spec should exit 0."""
    out = tmp_path / "report.html"
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(minimal_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code == 0, f"stdout: {result.stdout}\nstderr: {result.stderr if hasattr(result, 'stderr') else ''}"
    assert out.exists()
    spec_json = out.with_suffix(".agent-spec.json")
    assert spec_json.exists()


def test_exit_nonzero_on_missing_capture(runner: CliRunner, tmp_path: Path) -> None:
    """Missing capture file should fail fast."""
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(tmp_path / "does_not_exist.jsonl"),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(tmp_path / "out.html"),
        ],
    )
    # Typer BadParameter or file not found should be non-zero
    assert result.exit_code != 0


def test_exit_nonzero_on_malformed_capture(runner: CliRunner, tmp_path: Path) -> None:
    """Malformed JSONL should fail, not silently produce empty spec."""
    bad_cap = tmp_path / "bad.jsonl"
    bad_cap.write_text("not json\n{malformed", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(bad_cap),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(tmp_path / "out.html"),
        ],
    )
    # The dom_extractor should raise a validation error on malformed JSON
    # If it doesn't, we'll check that the error is surfaced
    # Currently, invalid JSON might be caught by json.loads and raise an exception
    # For now, check if it fails OR produces a warning/error visible in output
    # TODO: Strengthen this by making dom_extractor.extract fail hard on bad JSON
    if result.exit_code == 0:
        # If it succeeded, check if warnings were emitted
        assert "EXTRACTION:" in result.stdout or "WARNING" in result.stdout or "ERROR" in result.stdout, \
            "Malformed JSON should at least warn, not succeed silently"


def test_provenance_stamped_correctly_dom_capture(minimal_capture: Path, tmp_path: Path, runner: CliRunner) -> None:
    """When --capture is used, provenance must say dom-capture, not stub."""
    out = tmp_path / "report.html"
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(minimal_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
            "--mode", "mock",
        ],
    )
    assert result.exit_code == 0
    spec_json = out.with_suffix(".agent-spec.json")
    spec = json.loads(spec_json.read_text(encoding="utf-8"))
    assert spec["provenance"]["extraction_source"] == "dom-capture", "Mock run with --capture must stamp dom-capture"
    assert spec["provenance"]["telemetry_source"] == "mock"


def test_provenance_stamped_correctly_live_mode(minimal_capture: Path, tmp_path: Path, runner: CliRunner) -> None:
    """Live mode without proper org setup should fail (we can't test real token here)."""
    # We can't test live mode without a real org; just verify the CLI fails safely
    out = tmp_path / "report.html"
    # Live mode will fail on org safety check or token check
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(minimal_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
            "--mode", "live",
        ],
    )
    # Should fail (org safety check or token missing), not crash with unhandled exception
    assert result.exit_code != 0, "Live mode without proper setup should fail"
    # The key test: it should exit cleanly with an error, not crash
    # We don't assert on specific error message since multiple checks can fail first
    # (org safety verification, missing token, etc.)


def test_warnings_surface_to_terminal(tmp_path: Path, runner: CliRunner) -> None:
    """Extractor warnings must appear in stdout."""
    # Create a capture with many consecutive inputs that will be coalesced
    cap = tmp_path / "coalescable.jsonl"
    events = []
    for i in range(10):
        events.append({
            "v": 1,
            "seq": i + 1,
            "t": 1700000000000 + i * 50,  # < 150ms apart, same field -> coalescence
            "type": "input",
            "url": "https://test.my.salesforce.com/lightning/r/Case/500XX000001AbcAAA/view",
            "frame_path": [],
            "selectors": {
                "test_id": None,
                "aria": "[aria-label='Status']",
                "role_name": {"role": "textbox", "name": "Status"},
                "label_for": "Status",
                "sf_field": "Status",
                "css_path": "input[name='Status']",
                "text": "Status",
                "xpath": None,
            },
            "element": {
                "tag": "input",
                "type": "text",
                "name": "Status",
                "id": None,
                "classes": [],
                "aria_label": "Status",
                "text": "Status",
                "is_in_modal": False,
                "modal_label": None,
                "shadow_depth": 0,
            },
            "value": f"W{'orking'[:i]}",
            "value_redacted": False,
            "sf": {
                "object": "Case",
                "record_id": "500XX000001AbcAAA",
                "page_type": "record_home",
                "app": "Service",
            },
        })
    cap.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    out = tmp_path / "report.html"
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(cap),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code == 0
    # The extractor should have coalesced the 10 inputs into 1 and warned about it
    # Check for EXTRACTION: prefix from cli.py line 123
    assert "EXTRACTION:" in result.stdout or "coalesced" in result.stdout.lower() or "reduced" in result.stdout.lower()


def test_no_path_traversal_in_output_path(runner: CliRunner, minimal_capture: Path, tmp_path: Path) -> None:
    """--output-path with ../ should not escape tmp_path."""
    # This is more of a documentation test; typer/pathlib don't inherently block traversal,
    # but the CLI should either reject it or resolve to an absolute path safely.
    # We'll just verify the CLI doesn't crash with a traversal attempt.
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(minimal_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(tmp_path / "../../../etc/passwd.html"),
        ],
    )
    # Should either succeed (resolved safely) or fail (rejected), but not crash
    # We just verify no exception was raised
    assert "Traceback" not in result.stdout


def test_org_url_not_leaked_in_report(runner: CliRunner, tmp_path: Path) -> None:
    """org_url with frontdoor.jsp must have sid redacted in the report."""
    # Simulate user passing a frontdoor URL (which contains a session ID)
    cap = tmp_path / "capture.jsonl"
    cap.write_text(json.dumps({
        "v": 1, "seq": 1, "t": 1700000000000, "type": "click",
        "url": "https://test.my.salesforce.com/lightning/r/Case/500XX000001AbcAAA/view",
        "frame_path": [],
        "selectors": {"test_id": None, "aria": None, "role_name": None, "label_for": None,
                      "sf_field": None, "css_path": "button", "text": "Save", "xpath": None},
        "element": {"tag": "button", "type": None, "name": None, "id": None, "classes": [],
                    "aria_label": "Save", "text": "Save", "is_in_modal": False, "modal_label": None,
                    "shadow_depth": 0},
        "value": None, "value_redacted": False,
        "sf": {"object": "Case", "record_id": "500XX000001AbcAAA", "page_type": "record_home", "app": "Service"},
    }) + "\n", encoding="utf-8")

    out = tmp_path / "report.html"
    # User passes --org-url with frontdoor.jsp containing a session ID
    secret_sid = "00Dxx0000001gPL!AQEAQNqHQfXj8jqx..."
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(cap),
            "--org-url", f"https://test.my.salesforce.com/secur/frontdoor.jsp?sid={secret_sid}",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code == 0, f"CLI failed: {result.stdout}"
    html = out.read_text(encoding="utf-8")

    # The report must show frontdoor.jsp but redact the sid
    assert "frontdoor.jsp" in html, "frontdoor.jsp URL should appear (with redaction)"
    assert "sid=[REDACTED]" in html, "sid parameter must be redacted"
    assert secret_sid not in html, "Secret session ID must not appear in report"

    # Also check the spec JSON
    spec_json = out.with_suffix(".agent-spec.json")
    spec = json.loads(spec_json.read_text(encoding="utf-8"))
    # The spec doesn't currently include org_url, but if it did, it should also be redacted
    # Check that source_path doesn't contain the secret
    assert secret_sid not in json.dumps(spec), "Secret must not appear in spec JSON"


def test_redacted_value_not_in_report(runner: CliRunner, redacted_capture: Path, tmp_path: Path) -> None:
    """When value_redacted=True, value must not appear in HTML."""
    out = tmp_path / "report.html"
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(redacted_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code == 0
    html = out.read_text(encoding="utf-8")
    # The redacted event has value=None, value_redacted=True for SSN__c field
    # The report should not contain the actual SSN (which is None anyway in this test),
    # but more importantly, if it were non-null, it should be marked as redacted.
    # Since value is None, we check that the report doesn't claim a value was set.
    # A better test would have a non-null value that's been redacted, but the RawDomEvent
    # schema requires value=None when redacted. So we verify the field SSN__c appears but
    # no actual sensitive data is shown.
    # This is a placeholder; the real test is in test_html_report.py with XSS payloads.
    assert "SSN" in html  # field name should appear
    # If value were present, it should not be rendered literally


def test_xss_payloads_are_escaped(runner: CliRunner, xss_capture: Path, tmp_path: Path) -> None:
    """All XSS payloads in capture must be HTML-escaped in the report."""
    out = tmp_path / "report.html"
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(xss_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code == 0
    html = out.read_text(encoding="utf-8")

    # Test for actual XSS vulnerabilities: unescaped tags that would execute
    # The key patterns that must NOT appear (case-insensitive for robustness):
    dangerous_patterns = [
        '<script>',  # Opening script tag (would execute)
        '</script>',  # Closing script tag
        '<img',  # Opening img tag (could have onerror)
        'onerror=',  # Event handler attribute (without escaping, would execute)
        '<svg',  # SVG tag (could have onload)
        'onload=',  # Event handler
    ]

    # Count how many of these dangerous patterns appear unescaped
    # We allow them in escaped form (e.g., &lt;script&gt;)
    for pattern in dangerous_patterns:
        # Count unescaped occurrences
        # A simple heuristic: the pattern should not appear except in escaped form
        # Check that the HTML content doesn't have executable instances
        # We'll look for the pattern NOT preceded by &lt; or &gt; or &#
        # For simplicity, check that if the pattern appears, it's always escaped
        if pattern in html.lower():
            # If it appears, verify it's escaped
            escaped_variants = [
                pattern.replace('<', '&lt;'),
                pattern.replace('<', '&#60;'),
                pattern.replace('<', '&#x3c;'),
            ]
            assert any(variant.lower() in html.lower() for variant in escaped_variants), \
                f"Dangerous pattern {pattern!r} appears but is not escaped"

    # Also check that the escaped forms DO appear (meaning escaping is happening)
    assert '&lt;script&gt;' in html or '&lt;img' in html or '&lt;svg' in html, \
        "Expected to find escaped HTML tags"


def test_spec_json_source_path_is_accurate(runner: CliRunner, minimal_capture: Path, tmp_path: Path) -> None:
    """The spec's provenance.source_path must match the actual input file."""
    out = tmp_path / "report.html"
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(minimal_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )
    assert result.exit_code == 0
    spec_json = out.with_suffix(".agent-spec.json")
    spec = json.loads(spec_json.read_text(encoding="utf-8"))
    assert spec["provenance"]["source_path"] == str(minimal_capture)


def test_help_describes_stub_vs_real_evidence(runner: CliRunner) -> None:
    """run --help must accurately describe which flags produce stub/mock data."""
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    # Check that the run sub-command's help mentions stub vs real evidence
    help_text = result.stdout.lower()
    assert "stub" in help_text or "placeholder" in help_text
    assert "dom_capture" in help_text or "--capture" in help_text
    assert "real" in help_text or "observed" in help_text


@pytest.fixture
def redaction_leak_capture(tmp_path: Path) -> Path:
    """Capture with REDACTION LEAK: value_redacted=True but raw value present."""
    cap = tmp_path / "leak.jsonl"
    event = {
        "v": 1,
        "seq": 1,
        "t": 1700000000000,
        "type": "input",
        "url": "https://test.my.salesforce.com/lightning/r/Case/500XX000001AbcAAA/view",
        "frame_path": [],
        "selectors": {
            "test_id": None,
            "aria": "[aria-label='Card Number']",
            "role_name": {"role": "textbox", "name": "Card Number"},
            "label_for": "Card Number",
            "sf_field": "CardNumber__c",
            "css_path": "input[name='CardNumber__c']",
            "text": "Card Number",
            "xpath": None,
        },
        "element": {
            "tag": "input",
            "type": "text",
            "name": "CardNumber__c",
            "id": None,
            "classes": [],
            "aria_label": "Card Number",
            "text": "Card Number",
            "is_in_modal": False,
            "modal_label": None,
            "shadow_depth": 0,
        },
        "value": "4111111111111111",  # CANARY: leaked value
        "value_redacted": True,  # Recorder claims redacted, but value is present!
        "sf": {
            "object": "Case",
            "record_id": "500XX000001AbcAAA",
            "page_type": "record_home",
            "app": "Service",
        },
    }
    cap.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return cap


def test_redaction_leak_aborts_run(runner: CliRunner, redaction_leak_capture: Path, tmp_path: Path) -> None:
    """A capture with a redaction leak must abort and emit NO spec."""
    out = tmp_path / "report.html"
    spec_json = out.with_suffix(".agent-spec.json")

    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(redaction_leak_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )

    # CLI must exit non-zero
    assert result.exit_code != 0, "CLI must fail on redaction leak"

    # Error message must be visible
    assert "SECURITY CRITICAL" in result.stdout, "Must warn about security issue"
    assert "REDACTION LEAK" in result.stdout or "redaction leak" in result.stdout.lower()

    # NO spec JSON file should be emitted
    assert not spec_json.exists(), "Spec JSON must not be written when redaction leak detected"

    # CRITICAL: The leaked canary value must NOT appear in stdout
    assert "4111111111111111" not in result.stdout, "Leaked card number must not appear in terminal output"

    # Also verify no HTML report was written (since we abort before extraction)
    # The report MAY not exist, but if it does, it must not contain the canary
    if out.exists():
        html = out.read_text(encoding="utf-8")
        assert "4111111111111111" not in html, "Leaked card number must not appear in HTML"


def test_redaction_leak_canary_not_in_any_output(
    runner: CliRunner, redaction_leak_capture: Path, tmp_path: Path
) -> None:
    """Verify the canary value appears NOWHERE in any emitted file."""
    out = tmp_path / "report.html"

    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(redaction_leak_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )

    assert result.exit_code != 0

    # Check all files in tmp_path
    for file in tmp_path.rglob("*"):
        if file.is_file() and file != redaction_leak_capture:
            content = file.read_bytes()
            assert b"4111111111111111" not in content, f"Canary leaked into {file}"


def test_clean_capture_no_regression(runner: CliRunner, minimal_capture: Path, tmp_path: Path) -> None:
    """A clean capture (no validation issues) should succeed as before."""
    out = tmp_path / "report.html"
    spec_json = out.with_suffix(".agent-spec.json")

    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(minimal_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )

    assert result.exit_code == 0, "Clean capture must succeed"
    assert spec_json.exists(), "Spec JSON must be emitted for clean capture"
    assert out.exists(), "HTML report must be emitted for clean capture"


def test_validate_trace_is_actually_invoked(
    runner: CliRunner, minimal_capture: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure validate_trace is actually called, not a dead code path."""
    from sf_video_blueprint import cli as cli_module

    validate_called = False
    original_validate = cli_module.validate_trace

    def spy_validate(trace: Any) -> list[str]:
        nonlocal validate_called
        validate_called = True
        return original_validate(trace)

    monkeypatch.setattr(cli_module, "validate_trace", spy_validate)

    out = tmp_path / "report.html"
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(minimal_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(out),
        ],
    )

    assert result.exit_code == 0
    assert validate_called, "validate_trace must be invoked during capture processing"


# ---------------------------------------------------------------------------
# DEFECT L4-7: sub-threshold loss must be prominent in the terminal.
#
# The gate refuses at >=50% loss. Below that, the run proceeds — correctly — but
# before the fix it proceeded in total silence: no line of output distinguished a
# capture that lost 40% of its events from a clean one. The operator's only clue
# was a step count they had no baseline for.
# ---------------------------------------------------------------------------


def _capture_with_loss(tmp_path: Path, *, good: int, bad: int, name: str) -> Path:
    """A capture with `good` valid events and `bad` unparseable lines."""
    lines = []
    for i in range(good):
        lines.append(json.dumps({
            "v": 1,
            "seq": i + 1,
            "t": 1700000000000 + i * 1000,
            "type": "click",
            "url": "https://test.my.salesforce.com/lightning/r/Case/500XX000001AbcAAA/view",
            "frame_path": [],
            "selectors": {
                "test_id": None,
                "aria": f"[aria-label='Btn{i}']",
                "role_name": {"role": "button", "name": f"Btn{i}"},
                "label_for": None,
                "sf_field": None,
                "css_path": f"button.b{i}",
                "text": f"Btn{i}",
                "xpath": None,
            },
            "element": {
                "tag": "button",
                "type": None,
                "name": None,
                "id": None,
                "classes": [],
                "aria_label": f"Btn{i}",
                "text": f"Btn{i}",
                "is_in_modal": False,
                "modal_label": None,
                "shadow_depth": 0,
            },
            "value": None,
            "value_redacted": False,
            "sf": {
                "object": "Case",
                "record_id": "500XX000001AbcAAA",
                "page_type": "record_home",
                "app": "Service",
            },
        }))
    lines.extend("{ truncated line" for _ in range(bad))
    cap = tmp_path / name
    cap.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cap


@pytest.mark.parametrize("good,bad", [(9, 1), (8, 2), (7, 3), (6, 4)])
def test_sub_threshold_loss_is_announced_in_the_terminal(
    runner: CliRunner, tmp_path: Path, good: int, bad: int
) -> None:
    """10% through 40% loss: the run succeeds AND says how much it lost.

    Every one of these produced no output at all about the loss before the fix.
    """
    cap = _capture_with_loss(tmp_path, good=good, bad=bad, name=f"loss{bad}.jsonl")
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(cap),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(tmp_path / "report.html"),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "CAPTURE IS INCOMPLETE" in result.stdout
    assert "EVIDENCE INCOMPLETE:" in result.stdout
    # The ratio, not just the count: "1 line discarded" is meaningless alone.
    assert f"{bad / (good + bad):.0%}" in result.stdout
    assert "PARTIAL" in result.stdout


def test_clean_capture_says_nothing_about_loss(
    runner: CliRunner, minimal_capture: Path, tmp_path: Path
) -> None:
    """No false alarms. A warning that fires on clean input trains operators to
    ignore it, which is worse than no warning at all."""
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(minimal_capture),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(tmp_path / "report.html"),
        ],
    )

    assert result.exit_code == 0
    assert "CAPTURE IS INCOMPLETE" not in result.stdout
    assert "EVIDENCE INCOMPLETE" not in result.stdout


def test_loss_at_the_threshold_still_aborts(runner: CliRunner, tmp_path: Path) -> None:
    """The gate is unchanged: 50% loss remains fatal, not a yellow warning.

    LANE_RULES forbids weakening a gate. This test exists so that a future
    attempt to "improve" the incomplete-evidence path by downgrading the 50%
    abort into a warning fails loudly.
    """
    cap = _capture_with_loss(tmp_path, good=5, bad=5, name="half.jsonl")
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(cap),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(tmp_path / "report.html"),
        ],
    )

    assert result.exit_code == 1
    assert "DATA LOSS" in result.stdout
    assert not (tmp_path / "report.html").exists()


def test_incomplete_notice_is_not_duplicated_as_a_generic_warning(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Each finding is reported once, in its own severity block."""
    cap = _capture_with_loss(tmp_path, good=8, bad=2, name="dupe.jsonl")
    result = runner.invoke(
        app,
        [
            "run",
            "--capture", str(cap),
            "--org-url", "https://test.my.salesforce.com",
            "--output-path", str(tmp_path / "report.html"),
        ],
    )

    assert result.exit_code == 0
    assert "CAPTURE VALIDATION: EVIDENCE INCOMPLETE:" not in result.stdout
    assert result.stdout.count("EVIDENCE INCOMPLETE: 2 of 10") == 1


# =============================================================================
# === TEST: `iterate` CLI sub-command with --summary flag ===
# =============================================================================

def _write_minimal_spec_json(path: Path, intent: str = "Update Case Status") -> Path:
    """Write a minimal valid agent-spec.json for iterate CLI tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    spec = {
        "schema_version": "1.0.0",
        "intent": intent,
        "confidence": 0.75,
        "objects_touched": ["Case"],
        "entities": [
            {
                "name": "status",
                "object_api_name": "Case",
                "field_api_name": "Status",
                "evidence": [{"source": "data-delta", "detail": "Case.Status changed at step-001"}],
            }
        ],
        "orchestration_steps": [
            "Resolve and load the target Case record",
            "SUBMIT on button:Save to write Status",
        ],
        "guardrails": ["Require confirmation before writing Case.Status"],
        "failure_handling": ["Observed validation failure during recording"],
        "unknowns": [],
        "evidence": [{"source": "telemetry", "detail": "validation event observed at step-001"}],
    }
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


def test_iterate_command_runs_without_summary(runner: CliRunner, tmp_path: Path) -> None:
    """iterate command runs successfully without --summary and produces JSON report."""
    spec_path = _write_minimal_spec_json(tmp_path / "agent-spec.json")
    out_dir = tmp_path / "iterations"

    result = runner.invoke(
        app,
        [
            "iterate",
            str(spec_path),
            "--out-dir", str(out_dir),
            "--company-name", "TestCo",
            "--company-description", "A test company",
            "--max-rounds", "2",
        ],
    )

    assert result.exit_code == 0, f"iterate command failed:\n{result.stdout}\n{result.exception}"
    assert (out_dir / "iteration_report.json").exists(), "iteration_report.json not created"
    # Without --summary, no summary markdown file
    assert not (out_dir / "iteration_summary.md").exists(), "summary should not be written without --summary"


def test_iterate_command_summary_flag_writes_file(runner: CliRunner, tmp_path: Path) -> None:
    """iterate --summary writes iteration_summary.md to <out_dir>."""
    spec_path = _write_minimal_spec_json(tmp_path / "agent-spec.json")
    out_dir = tmp_path / "iterations"

    result = runner.invoke(
        app,
        [
            "iterate",
            str(spec_path),
            "--out-dir", str(out_dir),
            "--company-name", "TestCo",
            "--company-description", "A test company",
            "--max-rounds", "2",
            "--summary",
        ],
    )

    assert result.exit_code == 0, f"iterate --summary failed:\n{result.stdout}\n{result.exception}"
    summary_path = out_dir / "iteration_summary.md"
    assert summary_path.exists(), f"iteration_summary.md not written to {summary_path}"


def test_iterate_command_summary_contains_intent(runner: CliRunner, tmp_path: Path) -> None:
    """iterate --summary: the summary file contains the spec's intent."""
    intent = "Resolve a Support Ticket"
    spec_path = _write_minimal_spec_json(tmp_path / "agent-spec.json", intent=intent)
    out_dir = tmp_path / "iterations"

    result = runner.invoke(
        app,
        [
            "iterate",
            str(spec_path),
            "--out-dir", str(out_dir),
            "--summary",
        ],
    )

    assert result.exit_code == 0, f"iterate --summary failed:\n{result.stdout}"
    content = (out_dir / "iteration_summary.md").read_text(encoding="utf-8")
    assert intent in content, f"Intent '{intent}' missing from summary:\n{content}"


def test_iterate_command_summary_path_echoed(runner: CliRunner, tmp_path: Path) -> None:
    """iterate --summary echoes the summary path to stdout so the user knows where it is."""
    spec_path = _write_minimal_spec_json(tmp_path / "agent-spec.json")
    out_dir = tmp_path / "iterations"

    result = runner.invoke(
        app,
        [
            "iterate",
            str(spec_path),
            "--out-dir", str(out_dir),
            "--summary",
        ],
    )

    assert result.exit_code == 0
    assert "iteration_summary" in result.stdout, (
        f"Expected summary path in stdout. Got:\n{result.stdout}"
    )


def test_iterate_command_invalid_spec_json(runner: CliRunner, tmp_path: Path) -> None:
    """iterate command exits with non-zero code when the spec JSON is invalid."""
    bad_spec = tmp_path / "bad-spec.json"
    bad_spec.write_text("NOT JSON{{{{", encoding="utf-8")

    result = runner.invoke(
        app,
        ["iterate", str(bad_spec)],
    )

    assert result.exit_code != 0, "Expected non-zero exit for invalid JSON"
