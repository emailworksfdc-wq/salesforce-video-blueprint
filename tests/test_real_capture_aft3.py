"""Regression tests pinned to the project's first REAL Salesforce capture.

`examples/case_creation_aft3.dom_capture.jsonl` is a real click-by-click recording
of a Case being created in a Developer Edition org (lane 02). Every other capture
in this repo is synthetic, and the synthetic one parses cleanly — which is exactly
why the ingest path looked healthy for so long. On real Lightning DOM it discards
98% of the recording.

These tests exist to make that fact non-regressible in both directions:

1. The fixture must stay REAL. If someone "fixes" the example by filling in the
   null selectors, the tests that assert nulls will fail and the honesty of the
   fixture is restored by reverting, not by editing the fixture.
2. The fixture must stay REDACTED. `test_real_capture_contains_no_secrets` is the
   gate that keeps a session id or org host from ever landing in git.
3. The 98% data-loss number is pinned as an XFAIL-style characterization, not as
   an endorsement. Lane 04 owns the ingest fix. When that fix lands, the loss
   assertions below are the ones that must be UPDATED (a comment marks each).

Do NOT make these tests pass by loosening the score gate or by adding
"mock"/"synthetic" to the real-source marker sets. The whole point of the
project's honesty contract is that a fabricated pass is worse than an honest fail.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sf_video_blueprint.dom_capture import parse_capture_file, validate_trace
from sf_video_blueprint.pipeline import CaptureRejected, run_pipeline

REAL_CAPTURE = Path(__file__).parent.parent / "examples" / "case_creation_aft3.dom_capture.jsonl"
SYNTHETIC_CAPTURE = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"

#: Total events the recorder wrote during the real session.
REAL_EVENT_COUNT = 175

#: Events that survive `RawDomEvent` validation today. Lane 04's ingest fix should
#: raise this toward REAL_EVENT_COUNT; update this constant when it does.
REAL_PARSEABLE_COUNT = 4


def _raw_events() -> list[dict]:
    return [
        json.loads(line)
        for line in REAL_CAPTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ============================================================================
# 1. THE FIXTURE MUST STAY REDACTED (security gate — never relax this)
# ============================================================================


@pytest.mark.parametrize(
    "label,pattern",
    [
        ("salesforce session token", r"00D[A-Za-z0-9]{12,15}![A-Za-z0-9._-]+"),
        ("sid query parameter", r"[?&]sid="),
        ("access_token", r"access_token"),
        ("frontdoor url", r"frontdoor\.jsp"),
        ("jwt", r"eyJ[A-Za-z0-9_-]{10,}\."),
        ("bearer token", r"[Bb]earer\s+[A-Za-z0-9._-]{20,}"),
        ("aura token", r"aura\.token"),
    ],
)
def test_real_capture_contains_no_secrets(label: str, pattern: str) -> None:
    """The committed real capture must never contain credentials or org identity.

    This is the test that makes committing a real recording defensible. If it ever
    fails, the correct response is to delete the fixture and re-redact — not to
    delete the assertion.
    """
    text = REAL_CAPTURE.read_text(encoding="utf-8")
    manifest = REAL_CAPTURE.with_name(
        REAL_CAPTURE.name.replace(".jsonl", ".manifest.json")
    )
    if manifest.exists():
        text += manifest.read_text(encoding="utf-8")

    matches = re.findall(pattern, text)
    assert not matches, f"{label} leaked into the committed capture ({len(matches)} hits)"


#: The only host the redacted capture is allowed to reference. Anything else means
#: a real org hostname survived redaction.
ALLOWED_HOST = "example-org-dev-ed.develop.lightning.force.com"

#: The only Salesforce-shaped record id the redacted capture is allowed to contain.
ALLOWED_RECORD_ID = "500XX000000EXAMPLE"


def test_real_capture_references_no_org_but_the_placeholder() -> None:
    """Org identity must be gone by SHAPE, not by matching known-bad strings.

    Deliberately expressed as an allowlist. An earlier version of this test listed
    the real org host, username, and alias as regex patterns to search for — which
    committed the very org identity it was meant to keep out of git. Never
    reintroduce a literal real-org value here; assert the placeholder instead.
    """
    events = _raw_events()

    hosts = {
        m.group(1)
        for e in events
        if (m := re.match(r"https?://([^/]+)", e.get("url") or ""))
    }
    assert hosts == {ALLOWED_HOST}, f"non-placeholder host survived redaction: {hosts}"


def test_real_capture_contains_no_unredacted_record_ids() -> None:
    """No 15/18-char Salesforce record id other than the documented placeholder.

    Real ids carry an org-specific 3-char instance segment, so leaving one in would
    identify the org even with the hostname stripped.
    """
    text = REAL_CAPTURE.read_text(encoding="utf-8")

    # Salesforce ids start with a 3-char keyprefix and are 15 or 18 chars total.
    # Restrict to prefixes that appear in this capture's object graph to avoid
    # matching the many same-length Lightning component/DOM identifiers.
    candidates = set(re.findall(r"\b(?:500|00D|005|001|00[Qq])[A-Za-z0-9]{12,15}\b", text))
    leaked = candidates - {ALLOWED_RECORD_ID}
    assert not leaked, f"unredacted Salesforce record id(s) in the capture: {sorted(leaked)}"


# ============================================================================
# 2. THE FIXTURE MUST STAY REAL (anti-sanitization gate)
# ============================================================================


def test_real_capture_is_structurally_real_lightning_dom() -> None:
    """Real Lightning DOM has properties the synthetic example does not.

    If this fails, someone has "cleaned up" the fixture and destroyed the only
    real evidence in the repo. Revert rather than adjust.
    """
    events = _raw_events()
    assert len(events) == REAL_EVENT_COUNT

    # Deep shadow nesting — the synthetic example is entirely shadow_depth 0.
    depths = [e["element"]["shadow_depth"] for e in events]
    assert max(depths) >= 8, "real capture must retain deep shadow nesting"

    # LWC custom-element tags reach the sink instead of native controls.
    tags = {e["element"]["tag"] for e in events}
    assert any("-" in t for t in tags), "real capture must retain LWC custom-element tags"

    # No Salesforce field API name was ever derivable on real DOM.
    assert all(
        e["selectors"].get("sf_field") is None for e in events
    ), "real capture had zero derivable sf_field; a non-null one means the fixture was edited"


def test_synthetic_example_is_the_optimistic_case() -> None:
    """Guards the comparison: the synthetic capture parses 100%, the real one does not.

    This is the contrast that matters. A test suite that only ever sees the
    synthetic fixture concludes the ingest path works.
    """
    synthetic = parse_capture_file(SYNTHETIC_CAPTURE)
    assert synthetic.skipped_lines == [], "synthetic example is expected to parse cleanly"

    real = parse_capture_file(REAL_CAPTURE)
    assert real.skipped_lines, "real capture is expected to lose events until lane 04's fix"


# ============================================================================
# 3. THE MEASURED FAILURE — root cause pinned to one field
# ============================================================================


def test_real_capture_loses_events_to_nullable_role_name() -> None:
    """ROOT CAUSE: recorder.js emits role_name={role: null, name: null} for LWC
    hosts (no implicit ARIA role), but RawRoleName declares role/name as
    non-nullable `str`. One type mismatch discards 98% of a real recording.

    LANE 04: when you make RawRoleName's fields nullable (or drop an all-null
    role_name to None at parse time), REAL_PARSEABLE_COUNT goes up and this test
    must be updated. That is the intended direction.
    """
    trace = parse_capture_file(REAL_CAPTURE)

    assert len(trace.events) == REAL_PARSEABLE_COUNT
    assert len(trace.skipped_lines) == REAL_EVENT_COUNT - REAL_PARSEABLE_COUNT

    # Every single skip is the same root cause — not a scattering of problems.
    reasons = [reason for _, reason in trace.skipped_lines]
    assert all("selectors.role_name" in r for r in reasons), (
        "expected every dropped line to fail on selectors.role_name; "
        f"got other reasons: {[r for r in reasons if 'selectors.role_name' not in r][:3]}"
    )

    # And the raw data really does carry null role/name — the recorder's output,
    # not a corruption introduced by redaction.
    raw = _raw_events()
    null_role = sum(1 for e in raw if (e["selectors"].get("role_name") or {}).get("role") is None)
    assert null_role >= 170


def test_real_capture_is_rejected_by_the_pipeline() -> None:
    """End-to-end honesty check: a real capture produces NO spec today.

    The pipeline fails closed at >=50% data loss, so the first real recording this
    project ever took yields a `CaptureRejected`, not a score. This is the correct
    behaviour for the current ingest path — a spec derived from 4 of 175 events
    would be a fabrication wearing a score.
    """
    with pytest.raises(CaptureRejected) as excinfo:
        run_pipeline(
            REAL_CAPTURE,
            org_url="https://example-org-dev-ed.develop.my.salesforce.com",
        )

    findings = excinfo.value.findings
    assert any(f.startswith("DATA LOSS:") for f in findings)
    assert any("98%" in f for f in findings), f"expected the measured 98% loss, got {findings}"


def test_validate_trace_reports_the_data_loss_rather_than_hiding_it() -> None:
    """The integrity layer must surface the loss, since the parser itself is silent."""
    trace = parse_capture_file(REAL_CAPTURE)
    findings = validate_trace(trace)
    assert any(f.startswith("DATA LOSS:") for f in findings), findings


# ============================================================================
# 4. SELECTOR QUALITY ON REAL DOM — what the extractor actually got
# ============================================================================


def test_no_stable_selector_tier_survives_real_lightning_dom() -> None:
    """Tiers 1/4/5 (test_id, label_for, sf_field) are all empty on real DOM.

    The recorder computes label_for with `document.querySelector`, which cannot
    see labels inside shadow roots, and no Lightning input carries
    data-field-api-name. So the tiers the contract calls stable never populate,
    and ranking falls through to text/css.
    """
    events = _raw_events()

    for tier_name in ("test_id", "label_for", "sf_field"):
        populated = [e for e in events if e["selectors"].get(tier_name)]
        assert not populated, (
            f"real capture unexpectedly has {len(populated)} events with {tier_name}; "
            "if lane 04 fixed the recorder, update this test"
        )

    # aria was derivable on only a handful of controls.
    with_aria = [e for e in events if e["selectors"].get("aria")]
    assert len(with_aria) <= 5


def test_change_events_fire_per_keystroke_and_are_not_debounced() -> None:
    """recorder.js debounces `input` (250ms) but NOT `change`.

    Lightning's LWC inputs re-dispatch `change` on every keystroke, so a 57-char
    Description produced 57 change events carrying every intermediate prefix.
    The debounce in handleInput never applies to them.
    """
    events = _raw_events()
    changes = [e for e in events if e["type"] == "change"]

    assert len(changes) > 100, "real capture is dominated by change events"
    assert len([e for e in events if e["type"] == "input"]) < 5, (
        "the debounced `input` path barely fired; `change` is what actually arrives"
    )

    # Prove the prefix explosion: values on one element grow one char at a time.
    textarea_values = [
        e["value"]
        for e in changes
        if e["element"]["tag"] == "lightning-textarea" and isinstance(e.get("value"), str)
    ]
    assert len(textarea_values) > 20
    growing = sum(
        1
        for a, b in zip(textarea_values, textarea_values[1:])
        if b.startswith(a) and len(b) == len(a) + 1
    )
    assert growing > 20, "expected monotonic one-char-at-a-time keystroke prefixes"
