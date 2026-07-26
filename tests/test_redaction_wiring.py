"""Redaction must actually RUN in the pipeline, not merely exist.

`redaction.py` was fully implemented and fully tested but had zero production
callers, which makes it decoration rather than a control. These tests pin the
wiring: a capture carrying a planted secret must produce artifacts with that
secret absent, through every entry point that writes a file.

Two rules govern the fixtures here:

1. **Never write a plausible real secret into this repo.** Every planted value is
   obviously fake (`AKIAFAKEFAKEFAKE0000`, `example.invalid`) so a secret scanner
   pointed at this tree does not fire on the test suite itself.
2. **Never echo the planted value in an assertion message or a test name.** A
   failure report that prints the secret it was checking for defeats the control.
   Assertions below name the CATEGORY, never the bytes.

The false-positive tests matter as much as the leak tests: redaction that mangles
a legitimate Case Subject or a step identifier trades a real defect for a new one,
and this project's whole claim is that it does not corrupt its own evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sf_video_blueprint.dom_capture import parse_capture_file
from sf_video_blueprint.dom_extractor import DomCaptureExtractor, write_bundle
from sf_video_blueprint.redaction import pipeline_policy, redact_url

# --------------------------------------------------------------------------
# Planted markers. Obviously-fake by construction.
# --------------------------------------------------------------------------

# AWS-key-SHAPED but the body is the literal word FAKE repeated.
PLANTED_KEY_SHAPED = "AKIAFAKEFAKEFAKE0000"

# RFC 2606 reserves .invalid; this address cannot resolve or be delivered to.
PLANTED_EMAIL = "leaktest@example.invalid"

# Salesforce-session-token-SHAPED: 00D org prefix, '!', then a fake body.
PLANTED_SESSION_SHAPED = "00Dbm00000qTRQI!AQEAQFAKEfakefake0000"

# Legitimate business data that must SURVIVE untouched.
LEGIT_SUBJECT = "SFVB-TEST Broken solar panel needs replacement"
LEGIT_ID_LIKE_SUBJECT = "SFVB-TEST asset 5008d000004Xy9tAAC returned"
LEGIT_STEP_DIGITS = "SFVB-TEST reference 1234567890"


def _event(
    seq: int,
    *,
    name: str,
    value: str | None,
    aria_label: str | None = None,
    url: str = "https://example.my.salesforce.com/lightning/o/Case/new",
    event_type: str = "input",
    tag: str = "input",
) -> dict:
    """One recorder-shaped event.

    Field names here are deliberately ORDINARY (`Description`, `Subject`,
    `SuppliedEmail`). The recorder's name-based redaction and
    `validate_trace`'s leak detector both key off sensitive-LOOKING names, so a
    secret pasted into a normal field slips past both. That is the gap under test.
    """
    return {
        "v": 1,
        "seq": seq,
        "t": 1737830000000 + seq * 1000,
        "type": event_type,
        "url": url,
        "frame_path": [],
        "selectors": {
            "test_id": None,
            "aria": None,
            "role_name": None,
            "label_for": None,
            "sf_field": name,
            "css_path": f"{tag}[name={name}]",
            "text": None,
            "xpath": None,
        },
        "element": {
            "tag": tag,
            "type": "text" if tag == "input" else None,
            "name": name,
            "id": name,
            "classes": [],
            "aria_label": aria_label if aria_label is not None else name,
            "text": aria_label if aria_label is not None else name,
            "is_in_modal": False,
            "modal_label": None,
            "shadow_depth": 0,
        },
        "value": value,
        "value_redacted": False,
        "sf": {
            "object": "Case",
            "record_id": "5008d000004Xy9tAAC",
            "page_type": "record",
            "app": "Service",
        },
    }


def _write_capture(path: Path, events: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def leaky_capture(tmp_path: Path) -> Path:
    """A capture where secrets sit in ordinary fields, labels, and URLs."""
    return _write_capture(
        tmp_path / "dom_capture.jsonl",
        [
            # Secret in a free-text value on a non-sensitive field name.
            _event(1, name="Description", value=f"Deploy key {PLANTED_KEY_SHAPED} rotated"),
            # Email in a value.
            _event(2, name="SuppliedEmail", value=PLANTED_EMAIL),
            # Email in the ARIA LABEL, which becomes action.target.
            _event(3, name="Description", value="notes", aria_label=f"Notes for {PLANTED_EMAIL}"),
            # Live session token in a captured URL.
            _event(
                4,
                name="Subject",
                value=LEGIT_SUBJECT,
                url=(
                    "https://example.my.salesforce.com/secur/frontdoor.jsp"
                    f"?sid={PLANTED_SESSION_SHAPED}"
                ),
            ),
        ],
    )


# ==========================================================================
# redact_url — the URL-parameter gap
# ==========================================================================


def test_redact_url_strips_sid_parameter() -> None:
    """A captured frontdoor URL carries a live credential, not just a location."""
    url = f"https://example.my.salesforce.com/secur/frontdoor.jsp?sid={PLANTED_SESSION_SHAPED}"
    scrubbed, categories = redact_url(url)

    assert PLANTED_SESSION_SHAPED not in scrubbed, "sid value survived redact_url"
    assert "url_credential" in categories
    # The parameter NAME survives so the audit trail still shows what was used.
    assert "sid=" in scrubbed
    assert "frontdoor.jsp" in scrubbed


@pytest.mark.parametrize(
    "param",
    ["access_token", "refresh_token", "id_token", "code", "client_secret", "signature"],
)
def test_redact_url_strips_shapeless_credentials(param: str) -> None:
    """OAuth codes and opaque tokens have no detectable shape.

    Value-pattern matching cannot catch these, so the parameter NAME is the signal.
    Without this, `?code=<opaque>` reads as an ordinary query string.
    """
    opaque = "aBcD1234efGH5678ijKL"
    scrubbed, categories = redact_url(f"https://example.my.salesforce.com/x?{param}={opaque}")

    assert opaque not in scrubbed, f"{param} value survived redaction"
    assert "url_credential" in categories


def test_redact_url_preserves_ordinary_urls() -> None:
    """A plain record URL must come through byte-identical."""
    url = "https://example.my.salesforce.com/lightning/r/Case/5008d000004Xy9tAAC/view"
    scrubbed, categories = redact_url(url)

    assert scrubbed == url
    assert categories == []


def test_redact_url_preserves_benign_query_params() -> None:
    """Non-credential parameters are navigation state and must survive."""
    url = "https://example.my.salesforce.com/lightning/o/Case/list?filterName=Recent&page=2"
    scrubbed, _ = redact_url(url)

    assert scrubbed == url


# ==========================================================================
# The choke point: extract_from_trace
# ==========================================================================


def test_extraction_scrubs_secret_from_action_value(leaky_capture: Path) -> None:
    """A key-shaped token in a free-text field must not reach ExtractedAction."""
    bundle = DomCaptureExtractor().extract(leaky_capture)
    blob = bundle.model_dump_json()

    assert PLANTED_KEY_SHAPED not in blob, "a key-shaped secret survived extraction"
    assert "[REDACTED:aws_key]" in blob, "expected a redaction marker in its place"


def test_extraction_scrubs_email_from_action_target(leaky_capture: Path) -> None:
    """Targets are built from ARIA labels and ARE rendered into the HTML report."""
    bundle = DomCaptureExtractor().extract(leaky_capture)

    assert PLANTED_EMAIL not in bundle.model_dump_json(), "an email survived extraction"


def test_extraction_scrubs_session_token_from_captured_url(leaky_capture: Path) -> None:
    """The recorder captures whatever URL the operator was on.

    `cli.py::_redact_sensitive_url` only ever sees the operator-supplied
    `--org-url`; it never touches the URLs inside the capture. Those reach
    `ui_context.url` and the evidence appendix.
    """
    bundle = DomCaptureExtractor().extract(leaky_capture)

    assert PLANTED_SESSION_SHAPED not in bundle.model_dump_json(), (
        "a session-token-shaped value survived extraction"
    )


def test_extraction_reports_what_it_redacted(leaky_capture: Path) -> None:
    """A silent control cannot be audited — the run must say it fired."""
    bundle = DomCaptureExtractor().extract(leaky_capture)

    redaction_warnings = [w for w in bundle.warnings if "redact" in w.lower()]
    assert redaction_warnings, f"no redaction warning emitted: {bundle.warnings}"

    joined = " ".join(redaction_warnings)
    # Categories are named; the offending bytes never are.
    assert "aws_key" in joined or "email" in joined or "url_credential" in joined
    assert PLANTED_KEY_SHAPED not in joined, "warning echoed the value it redacted"
    assert PLANTED_EMAIL not in joined, "warning echoed the value it redacted"


def test_written_bundle_is_clean(leaky_capture: Path, tmp_path: Path) -> None:
    """write_bundle persists the extraction verbatim; it must have nothing to leak."""
    bundle = DomCaptureExtractor().extract(leaky_capture)
    out = tmp_path / "bundle.json"
    write_bundle(bundle, out)

    written = out.read_text(encoding="utf-8")
    assert PLANTED_KEY_SHAPED not in written
    assert PLANTED_EMAIL not in written
    assert PLANTED_SESSION_SHAPED not in written


# ==========================================================================
# False-positive safety — legitimate data must survive
# ==========================================================================


def test_legitimate_subject_survives(leaky_capture: Path) -> None:
    """Ordinary business prose must come through untouched."""
    bundle = DomCaptureExtractor().extract(leaky_capture)

    assert LEGIT_SUBJECT in bundle.model_dump_json(), (
        "a legitimate Case Subject was corrupted by redaction"
    )


def test_id_like_subject_survives(tmp_path: Path) -> None:
    """A Subject containing a real record id must NOT be mangled.

    Record ids are retained on purpose — they are the audit trail. This also
    guards the `redact_record_ids=False` decision in `pipeline_policy()`.
    """
    capture = _write_capture(
        tmp_path / "c.jsonl", [_event(1, name="Subject", value=LEGIT_ID_LIKE_SUBJECT)]
    )
    bundle = DomCaptureExtractor().extract(capture)

    assert LEGIT_ID_LIKE_SUBJECT in bundle.model_dump_json(), (
        "an id-bearing Subject was corrupted by redaction"
    )


def test_digit_run_survives(tmp_path: Path) -> None:
    """Ten consecutive digits are not automatically a phone number.

    `RedactionPolicy.strict()` rewrites `1234567890` to `[REDACTED:phone]`.
    `pipeline_policy()` disables phone redaction precisely so reference numbers,
    epoch timestamps, and step identifiers are not corrupted.
    """
    capture = _write_capture(
        tmp_path / "c.jsonl", [_event(1, name="Subject", value=LEGIT_STEP_DIGITS)]
    )
    bundle = DomCaptureExtractor().extract(capture)

    assert LEGIT_STEP_DIGITS in bundle.model_dump_json(), (
        "a digit reference number was corrupted by phone redaction"
    )


def test_record_id_in_url_is_retained(tmp_path: Path) -> None:
    """Record ids are deliberately NOT redacted — assert that intent explicitly.

    The id reaches artifacts through `ui_context.url` (a Lightning record URL), which
    is the surface `redact_url` also operates on — so this pins that stripping
    credential PARAMETERS does not also strip the record id from the PATH.

    If a future change flips `redact_record_ids` to True, this fails loudly rather
    than silently degrading the audit trail.
    """
    record_url = "https://example.my.salesforce.com/lightning/r/Case/5008d000004Xy9tAAC/view"
    capture = _write_capture(
        tmp_path / "c.jsonl", [_event(1, name="Subject", value="x", url=record_url)]
    )
    bundle = DomCaptureExtractor().extract(capture)

    assert record_url in bundle.model_dump_json(), (
        "record id was redacted; the blueprint can no longer name the record it touched"
    )


def test_clean_capture_is_byte_identical(tmp_path: Path) -> None:
    """Redaction must be a no-op on a capture with nothing to redact.

    This is the strongest false-positive check: same trace, extraction with the
    control active, and not one byte changed by it.
    """
    capture = _write_capture(
        tmp_path / "c.jsonl",
        [
            _event(1, name="Subject", value=LEGIT_SUBJECT),
            _event(2, name="Status", value="Escalated"),
            _event(3, name="Priority", value="High"),
            _event(4, name="Save", value=None, event_type="click", tag="button"),
        ],
    )
    bundle = DomCaptureExtractor().extract(capture)

    assert [a.value for a in bundle.actions[:3]] == [LEGIT_SUBJECT, "Escalated", "High"]
    assert not [w for w in bundle.warnings if "redact" in w.lower()], (
        "redaction fired on a capture with nothing sensitive in it"
    )


def test_pipeline_policy_keeps_ids_and_phones_but_redacts_secrets() -> None:
    """Pin the policy itself, so a weakening edit is visible in a diff."""
    policy = pipeline_policy()

    assert policy.redact_record_ids is False
    assert policy.redact_phones is False
    assert policy.redact_emails is True
    assert policy.mode == "mask"


# ==========================================================================
# The second choke point: org-controlled text that never passed extraction
# ==========================================================================


def _render(analysis) -> str:
    """Render a report around one StepAnalysis."""
    from datetime import UTC, datetime

    from sf_video_blueprint.html_report import (
        AgentBlueprintSection,
        DataProvenance,
        MasterBlueprintRenderer,
    )
    from sf_video_blueprint.models import ActionExtractionBundle
    from sf_video_blueprint.replay import ReplayRunMetadata

    bundle = ActionExtractionBundle(
        recording_id="rec-test",
        source_video_path="<none>",
        extracted_at=datetime.now(UTC),
        actions=[],
        evidence=[],
        warnings=[],
    )
    run = ReplayRunMetadata(
        run_id="run-test",
        org_url="https://example.my.salesforce.com",
        username="analyst@example.com",
        profile_name="System Administrator",
        role_name=None,
        environment="live",
    )
    section = AgentBlueprintSection(
        intent="test",
        required_entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
    )
    provenance = DataProvenance(
        extraction_source="dom-capture", telemetry_source="live-org", replay_source="browser"
    )
    return MasterBlueprintRenderer().render(bundle, run, [analysis], [section], provenance)


def test_report_scrubs_org_validation_message() -> None:
    """A real org validation error can quote customer data straight back.

    `FIELD_CUSTOM_VALIDATION_EXCEPTION` messages are authored by org admins and can
    embed an email or an account name. These reach the report through
    `failure_reason` / `replay_message`, which come from the REPLAY and TELEMETRY
    layers — they never pass through extraction, so the extractor choke point cannot
    see them. This is why the renderer needs its own pass.
    """
    from sf_video_blueprint.correlation import StepAnalysis

    analysis = StepAnalysis(
        step_id="step-001",
        action_target="input:Subject",
        replay_status=None,
        replay_message=f"Timed out submitting key {PLANTED_KEY_SHAPED}",
        failure_reason=f"FIELD_CUSTOM_VALIDATION_EXCEPTION: {PLANTED_EMAIL} is not permitted",
    )
    html = _render(analysis)

    assert PLANTED_KEY_SHAPED not in html, "a key-shaped secret reached the HTML report"
    assert PLANTED_EMAIL not in html, "an email reached the HTML report"


def test_report_does_not_mutate_caller_analyses() -> None:
    """Redacting for the report must not corrupt the caller's in-memory objects.

    The same StepAnalysis list is used to derive the spec. If rendering mutated it,
    the report and the spec would silently disagree about what was observed.
    """
    from sf_video_blueprint.correlation import StepAnalysis

    original = f"validation failed for {PLANTED_EMAIL}"
    analysis = StepAnalysis(
        step_id="step-001",
        action_target="input:Subject",
        replay_status=None,
        replay_message=None,
        failure_reason=original,
    )
    _render(analysis)

    assert analysis.failure_reason == original, "renderer mutated the caller's StepAnalysis"


def test_report_preserves_legitimate_validation_message() -> None:
    """A validation message with nothing sensitive must render verbatim.

    Operators depend on this text to debug a failed step; corrupting it would make
    the report less useful than no redaction at all.
    """
    from sf_video_blueprint.correlation import StepAnalysis

    message = "FIELD_CUSTOM_VALIDATION_EXCEPTION: Status cannot be Closed without a Reason"
    analysis = StepAnalysis(
        step_id="step-001",
        action_target="input:Status",
        replay_status=None,
        replay_message="element resolved in 240ms",
        failure_reason=message,
    )
    html = _render(analysis)

    assert message in html, "a legitimate validation message was corrupted"
    assert "element resolved in 240ms" in html


# ==========================================================================
# Detection and redaction are different things — both must survive
# ==========================================================================


def test_validate_trace_still_sees_raw_bytes(tmp_path: Path) -> None:
    """Redaction runs AFTER the integrity gate, on purpose.

    `validate_trace` exists to make a recorder redaction FAILURE visible. If
    redaction ran first, it would launder the recorder's bug and the operator would
    never learn their recorder is broken. Detection reads raw; redaction cleans the
    output. Order matters.
    """
    from sf_video_blueprint.dom_capture import validate_trace

    leak = _event(1, name="Password", value="anything")
    leak["value_redacted"] = True  # claims redacted, value still present
    capture = _write_capture(tmp_path / "c.jsonl", [leak])

    findings = validate_trace(parse_capture_file(capture))
    critical = [f for f in findings if f.startswith("SECURITY CRITICAL:")]

    assert critical, "the redaction-leak detector stopped firing"
    assert "anything" not in " ".join(critical), "finding echoed the leaked value"
