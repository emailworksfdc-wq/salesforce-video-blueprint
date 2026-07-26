"""Regression tests pinned to the project's first REAL Salesforce capture.

`examples/case_creation_aft3.dom_capture.jsonl` is a real click-by-click recording
of a Case being created in a Developer Edition org (lane 02). Every other capture
in this repo is synthetic, and the synthetic one parses cleanly — which is exactly
why the ingest path looked healthy for so long. On real Lightning DOM the parser
discarded 98% of the recording: 171 of 175 events failed `RawDomEvent` validation
because `selectors.role_name.role` is null for LWC hosts with no implicit ARIA role.

**That defect is fixed.** `RawRoleName` now declares both halves nullable while
keeping `strict=True`, and this capture parses 175/175 with zero skipped lines and
zero `validate_trace` findings. The assertions below were inverted when the fix
landed; the history of this file is the record of the defect.

These tests exist to make that fact non-regressible in both directions:

1. The fixture must stay REAL. If someone "fixes" the example by filling in the
   null selectors, the tests that assert nulls will fail and the honesty of the
   fixture is restored by reverting, not by editing the fixture. The nulls are
   still asserted — they are the evidence that the parser handles real DOM rather
   than DOM that was cleaned up to suit it.
2. The fixture must stay REDACTED. `test_real_capture_contains_no_secrets` is the
   gate that keeps a session id or org host from ever landing in git.
3. Full parse is now pinned, so a regression that reintroduces the loss fails
   loudly instead of quietly returning to 4 events.

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
from sf_video_blueprint.pipeline import run_pipeline

REAL_CAPTURE = Path(__file__).parent.parent / "examples" / "case_creation_aft3.dom_capture.jsonl"
SYNTHETIC_CAPTURE = Path(__file__).parent.parent / "examples" / "case_triage.dom_capture.jsonl"

#: Total events the recorder wrote during the real session.
REAL_EVENT_COUNT = 175

#: Events that survive `RawDomEvent` validation. Every one of them does, now that
#: `RawRoleName` accepts the nulls its own recorder emits. This was 4 before that
#: fix — a regression would show up here as a drop, which is the point of pinning
#: the full count rather than a floor.
REAL_PARSEABLE_COUNT = REAL_EVENT_COUNT


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


def test_both_captures_now_parse_but_only_one_proves_anything() -> None:
    """Both parse cleanly — and that is precisely why the synthetic one is not enough.

    A suite that sees only the synthetic fixture concluded the ingest path worked
    while it was discarding 98% of real recordings, because the synthetic fixture
    was written to match the parser rather than the recorder. Equal cleanliness here
    is not evidence of equal value: the real capture is the one carrying null
    role/name pairs, shadow-DOM-defeated labels and Lightning's actual markup.
    """
    synthetic = parse_capture_file(SYNTHETIC_CAPTURE)
    assert synthetic.skipped_lines == [], "synthetic example is expected to parse cleanly"

    real = parse_capture_file(REAL_CAPTURE)
    assert real.skipped_lines == [], (
        "the real capture must parse without loss; a non-empty skipped_lines here "
        f"means the nullable role/name regression is back: {real.skipped_lines[:3]}"
    )

    # The distinguishing property, asserted so the two fixtures cannot converge:
    # the real one still contains the nulls that used to break the parser.
    raw = _raw_events()
    null_role = sum(
        1 for e in raw if (e["selectors"].get("role_name") or {}).get("role") is None
    )
    assert null_role >= 170, (
        "the real capture no longer carries null role/name pairs, so it has been "
        "sanitized into a second synthetic fixture and proves nothing about real DOM"
    )


# ============================================================================
# 3. THE MEASURED FAILURE — root cause pinned to one field
# ============================================================================


def test_nullable_role_name_no_longer_costs_the_recording() -> None:
    """ROOT CAUSE, now fixed: `recorder.js` emits `role_name={role: null, name: null}`
    for LWC hosts with no implicit ARIA role, and `RawRoleName` used to declare both
    halves as required `str`. One type mismatch discarded 171 of 175 real events —
    the parser was stricter than the format it consumes.

    Both halves are nullable now, so all 175 parse. `strict=True` is retained, which
    is what keeps this from being a blanket loosening: a role that is a *number* or a
    list is still malformed and still lands in `skipped_lines`.
    """
    trace = parse_capture_file(REAL_CAPTURE)

    assert len(trace.events) == REAL_PARSEABLE_COUNT == REAL_EVENT_COUNT
    assert trace.skipped_lines == [], (
        f"expected zero loss on the real capture, lost {len(trace.skipped_lines)}: "
        f"{[r for _, r in trace.skipped_lines][:3]}"
    )

    # The nulls are still there — this passes because the parser accepts real DOM,
    # not because the fixture was cleaned up to suit the parser.
    raw = _raw_events()
    null_role = sum(1 for e in raw if (e["selectors"].get("role_name") or {}).get("role") is None)
    assert null_role >= 170, (
        "the fixture no longer carries the null role/name pairs that exposed the "
        "defect; full parse is then meaningless"
    )


def test_a_wrongly_typed_role_is_still_malformed() -> None:
    """Nullable is not untyped. The fix must not have turned the schema off.

    If a role of `123` were accepted, the parser would have stopped validating this
    field at all rather than allowing the one value the recorder really emits.
    """
    from sf_video_blueprint.dom_capture import RawRoleName

    RawRoleName(role=None, name=None)  # the recorder's real output
    RawRoleName(role="button", name=None)  # a partial pair still narrows a selector

    for bad in (123, ["button"], {"role": "button"}, 1.5, True):
        with pytest.raises(Exception):  # noqa: B017 - pydantic's own error type
            RawRoleName(role=bad, name=None)


def test_real_capture_now_yields_a_spec_and_still_does_not_pass_the_gate() -> None:
    """End-to-end: the first real recording produces a spec, and it honestly fails.

    Before the ingest fix this raised `CaptureRejected` — the pipeline fails closed
    at >=50% loss, and a spec built from 4 of 175 events would have been a
    fabrication wearing a score. Now all 175 parse and a real spec comes out.

    It still does not pass, and that is the correct result: telemetry here is
    `mock`, which is not in `markers.REAL_TELEMETRY_SOURCES`, so the run is blocked
    on provenance no matter how good the DOM evidence is. Fixing ingest bought real
    extraction evidence; it did not and must not buy a pass.
    """
    result = run_pipeline(
        REAL_CAPTURE,
        org_url="https://example-org-dev-ed.develop.my.salesforce.com",
    )

    assert result.spec is not None
    assert result.spec.intent, "a real capture must yield a non-empty intent"
    assert "Case" in (result.spec.objects_touched or [])

    assert result.score.passed is False, "mock telemetry must never pass the gate"
    assert any(
        "mock" in issue or "live org" in issue for issue in result.score.blocking_issues
    ), f"expected a telemetry-provenance block, got {result.score.blocking_issues}"


def test_the_gate_does_not_accuse_the_builder_of_padding_on_real_dom() -> None:
    """128 of 129 observed entities are unresolvable Lightning controls, not an attack.

    Real Lightning markup produces many inputs whose object/field cannot be resolved.
    An earlier scorer collapsed all of them into one `None.None` bucket that looked
    like the same field repeated, cut `evidence_grounding` to 5/30 and fired a
    threshold-surfing block — penalising exactly the honest runs this project wants.
    Pinned here on the real artifact, which is the only place the bug was visible.
    """
    result = run_pipeline(
        REAL_CAPTURE,
        org_url="https://example-org-dev-ed.develop.my.salesforce.com",
    )
    grounding = result.score.dimensions["evidence_grounding"]

    accusations = [f for f in grounding.findings if "PADDING" in f.upper()]
    assert not accusations, f"gate accused its own builder of padding: {accusations}"
    assert grounding.score >= 20, (
        f"evidence_grounding collapsed to {grounding.score}/30 on a real capture, "
        "which is the None.None-bucket regression"
    )


def test_validate_trace_finds_nothing_to_report_on_the_real_capture() -> None:
    """The integrity layer is silent because there is genuinely no loss left.

    It used to report `DATA LOSS: 171 of 175 lines were skipped (98%)`. That the
    finding is gone is the strongest single statement about the ingest fix — the
    check that caught the defect no longer has anything to say about this file.
    """
    trace = parse_capture_file(REAL_CAPTURE)
    findings = validate_trace(trace)
    assert findings == [], f"expected a clean real capture, got findings: {findings}"


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
