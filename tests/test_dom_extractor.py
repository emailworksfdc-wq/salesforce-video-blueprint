"""
Adversarial test suite for DomCaptureExtractor — Step 5A7.

THE REGRESSION GUARD THAT MATTERS MOST:
The old HeuristicVideoExtractor returned ONE hardcoded button:Save action
for ANY input — a 16-byte .mp4 stub "succeeded". This test suite's primary
purpose is to ensure the new DOM extractor actually uses the input data
and produces different bundles for different traces.

Coverage:
1. Different recordings → different bundles (THE GUARD)
2. Noise reduction per contract 2.4 (exact counts)
3. Reduction auditability (bundle.warnings)
4. SUBMIT vs CLICK classification (contract 2.3)
5. Target prefix grammar compatibility with replay_browser.build_selector_candidates
6. No invented labels (warnings for weak targets)
7. Confidence from selector tier, NOT a constant
8. Redaction respect
9. Determinism (same trace → identical output)
10. timestamp_ms relative to first event
11. Evidence traceability (EvidenceArtifact links back to source)
12. Protocol compatibility (empty trace yields valid bundle)
13. End-to-end integration (actions feed spec_builder)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from sf_video_blueprint.correlation import correlate_all
from sf_video_blueprint.dom_capture import synthesize_trace
from sf_video_blueprint.dom_extractor import DomCaptureExtractor, ReductionReport
from sf_video_blueprint.models import ActionType, EvidenceType
from sf_video_blueprint.replay import ReplayEvent, ReplayStatus
from sf_video_blueprint.replay_browser import build_selector_candidates
from sf_video_blueprint.spec_builder import build_agent_spec
from sf_video_blueprint.telemetry import CorrelationKey, ObjectSnapshot, TelemetryEvent, TelemetryLayer

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


# ============================================================================
# 1. THE REGRESSION GUARD — different recordings MUST yield different bundles
# ============================================================================


def test_different_recordings_yield_different_bundles() -> None:
    """
    CRITICAL REGRESSION GUARD: The old HeuristicVideoExtractor returned
    one hardcoded button:Save action for ANY input. This test proves the
    new extractor actually reads the trace and produces different output
    for different input.

    Without this test, someone can reintroduce a stub and every other test
    still passes.
    """
    extractor = DomCaptureExtractor()

    # Trace A: Update Case status
    trace_a = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com/lightning/r/Case/500xx/view",
                "selectors": {"text": "Status", "css_path": "select.status-field"},
                "element": {"tag": "select", "text": "Status", "classes": ["status-field"]},
                "sf": {"object": "Case"},
            },
            {
                "seq": 2,
                "t": 1001000,
                "type": "change",
                "url": "https://test.salesforce.com/lightning/r/Case/500xx/view",
                "selectors": {"text": "Status", "css_path": "select.status-field"},
                "element": {"tag": "select", "text": "Status", "classes": ["status-field"]},
                "value": "Working",
                "sf": {"object": "Case"},
            },
            {
                "seq": 3,
                "t": 1002000,
                "type": "click",
                "url": "https://test.salesforce.com/lightning/r/Case/500xx/view",
                "selectors": {"text": "Save", "aria": "button[aria-label='Save']"},
                "element": {"tag": "button", "text": "Save", "aria_label": "Save"},
                "sf": {"object": "Case"},
            },
        ]
    )

    # Trace B: Update Opportunity amount
    trace_b = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 2000000,
                "type": "click",
                "url": "https://test.salesforce.com/lightning/r/Opportunity/006xx/view",
                "selectors": {"sf_field": "Amount", "css_path": "input.amount-field"},
                "element": {"tag": "input", "name": "Amount", "classes": ["amount-field"]},
                "sf": {"object": "Opportunity"},
            },
            {
                "seq": 2,
                "t": 2001000,
                "type": "input",
                "url": "https://test.salesforce.com/lightning/r/Opportunity/006xx/view",
                "selectors": {"sf_field": "Amount", "css_path": "input.amount-field"},
                "element": {"tag": "input", "name": "Amount", "classes": ["amount-field"]},
                "value": "100000",
                "sf": {"object": "Opportunity"},
            },
            {
                "seq": 3,
                "t": 2002000,
                "type": "click",
                "url": "https://test.salesforce.com/lightning/r/Opportunity/006xx/view",
                "selectors": {"text": "Submit", "aria": "button[aria-label='Submit']"},
                "element": {"tag": "button", "text": "Submit", "aria_label": "Submit"},
                "sf": {"object": "Opportunity"},
            },
        ]
    )

    bundle_a = extractor.extract_from_trace(trace_a)
    bundle_b = extractor.extract_from_trace(trace_b)

    # Different action counts
    assert len(bundle_a.actions) != len(bundle_b.actions), "Different traces MUST yield different action counts"

    # Different action content (at least one field must differ)
    # Check all serializable fields
    actions_a_serialized = [
        (a.action_type, a.target, a.value, a.ui_context.object_name) for a in bundle_a.actions
    ]
    actions_b_serialized = [
        (b.action_type, b.target, b.value, b.ui_context.object_name) for b in bundle_b.actions
    ]
    assert actions_a_serialized != actions_b_serialized, "Different traces MUST yield different action content"

    # Different objects touched
    objects_a = {a.ui_context.object_name for a in bundle_a.actions if a.ui_context.object_name}
    objects_b = {a.ui_context.object_name for a in bundle_b.actions if a.ui_context.object_name}
    assert objects_a != objects_b, f"Different traces MUST touch different objects: {objects_a} vs {objects_b}"

    # Different recording IDs (deterministic but different)
    assert bundle_a.recording_id != bundle_b.recording_id, "Different traces MUST have different recording IDs"


# ============================================================================
# 2. Noise reduction per contract 2.4 — EXACT COUNTS
# ============================================================================


def test_consecutive_input_events_coalesce_to_final_value() -> None:
    """
    Contract 2.4: 40 consecutive input events on ONE element coalesce to
    ONE action carrying the FINAL value (not the first, not concatenation).

    This is the difference between a 23-step spec and a 400-step unusable one.
    """
    extractor = DomCaptureExtractor()

    # 40 input events on same field
    events_data = []
    for i in range(40):
        events_data.append(
            {
                "seq": i + 1,
                "t": 1000000 + i * 100,
                "type": "input",
                "url": "https://test.salesforce.com/form",
                "selectors": {"sf_field": "Subject", "css_path": "input.subject"},
                "element": {"tag": "input", "name": "Subject", "id": "subject-field"},
                "value": f"Test subject {i}",  # Progressive values
            }
        )

    trace = synthesize_trace(events_data=events_data)
    bundle = extractor.extract_from_trace(trace)

    # Must produce exactly ONE action
    assert len(bundle.actions) == 1, f"40 consecutive inputs must coalesce to 1 action, got {len(bundle.actions)}"

    # Must carry the FINAL value
    assert bundle.actions[0].value == "Test subject 39", "Coalesced action must carry FINAL value, not first"

    # Reduction must be reported in warnings
    reduction_warning = next((w for w in bundle.warnings if "coalesced" in w.lower()), None)
    assert reduction_warning is not None, "Coalescing must be reported in warnings"
    assert "39" in reduction_warning, f"Reduction count must be accurate: {reduction_warning}"


def test_bubbling_duplicates_dropped() -> None:
    """
    Contract 2.4: A click on a container within 150ms of a click on its
    interactive descendant yields ONE action, and it is the INNERMOST element.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            # Click on non-interactive container
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"css_path": "div.container"},
                "element": {"tag": "div", "classes": ["container"]},  # Non-interactive
            },
            # Click on interactive button INSIDE container, within 150ms
            {
                "seq": 2,
                "t": 1000100,  # 100ms later
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"text": "Save", "aria": "button[aria-label='Save']"},
                "element": {"tag": "button", "text": "Save", "aria_label": "Save"},  # Interactive
            },
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Must produce exactly ONE action
    assert len(bundle.actions) == 1, f"Bubbling duplicates must be dropped, got {len(bundle.actions)} actions"

    # Must be the INNERMOST (interactive) element
    assert "Save" in bundle.actions[0].target, "Must keep the INNERMOST interactive element, not the container"

    # Reduction must be reported
    reduction_warning = next((w for w in bundle.warnings if "bubbling" in w.lower()), None)
    assert reduction_warning is not None, "Bubbling reduction must be reported in warnings"
    assert "1" in reduction_warning, f"Must report 1 dropped bubbling event: {reduction_warning}"


def test_scroll_dropped_unless_precedes_interaction() -> None:
    """
    Contract 2.4: Scroll dropped unless it precedes an interaction with
    a different element.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {"seq": 1, "t": 1000000, "type": "scroll", "url": "https://test.salesforce.com"},
            {"seq": 2, "t": 1001000, "type": "scroll", "url": "https://test.salesforce.com"},
            # Scroll before interaction — KEEP
            {"seq": 3, "t": 1002000, "type": "scroll", "url": "https://test.salesforce.com"},
            {
                "seq": 4,
                "t": 1003000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"text": "Next"},
                "element": {"tag": "button", "text": "Next"},
            },
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Must drop redundant scrolls
    scroll_actions = [a for a in bundle.actions if a.action_type == ActionType.SCROLL]
    assert len(scroll_actions) <= 1, f"Redundant scrolls must be dropped, got {len(scroll_actions)}"

    # Reduction must be reported
    reduction_warning = next((w for w in bundle.warnings if "scroll" in w.lower()), None)
    assert reduction_warning is not None, "Scroll reduction must be reported"


def test_navigate_synthesized_on_url_change() -> None:
    """
    Contract 2.4: NAVIGATE synthesized when url changes between consecutive events.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com/page1",
                "selectors": {"text": "Next"},
                "element": {"tag": "button", "text": "Next"},
            },
            # URL changes
            {
                "seq": 2,
                "t": 1001000,
                "type": "click",
                "url": "https://test.salesforce.com/page2",  # Different URL
                "selectors": {"text": "Save"},
                "element": {"tag": "button", "text": "Save"},
            },
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Must synthesize a NAVIGATE action
    navigate_actions = [a for a in bundle.actions if a.action_type == ActionType.NAVIGATE]
    assert len(navigate_actions) >= 1, "Must synthesize NAVIGATE when URL changes"

    # Reduction must be reported
    reduction_warning = next((w for w in bundle.warnings if "navigate" in w.lower()), None)
    assert reduction_warning is not None, "Navigate synthesis must be reported"


def test_character_keydowns_dropped() -> None:
    """
    Contract 2.3: Plain character keydowns are dropped as noise.
    Modifier combos (Ctrl/Cmd/Alt/Shift) are retained as HOTKEY.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "keydown",
                "url": "https://test.salesforce.com",
                "selectors": {"css_path": "input"},
                "element": {"tag": "input"},
                "value": "a",  # Plain character keydown — DROP
            },
            {
                "seq": 2,
                "t": 1001000,
                "type": "input",
                "url": "https://test.salesforce.com",
                "selectors": {"css_path": "input"},
                "element": {"tag": "input"},
                "value": "test",
            },
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Character keydown must be dropped
    keydown_actions = [a for a in bundle.actions if a.action_type == ActionType.HOTKEY]
    assert len(keydown_actions) == 0, "Plain character keydowns must be dropped"

    # Reduction must be reported
    reduction_warning = next((w for w in bundle.warnings if "keydown" in w.lower()), None)
    assert reduction_warning is not None, "Keydown reduction must be reported"


def test_modifier_keydown_survives_as_hotkey() -> None:
    """
    Contract 2.3: keydown WITH a modifier (Ctrl/Cmd/Alt/Shift) → HOTKEY.
    A recording where the operator saved with Ctrl+S must not silently lose the save.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "input",
                "url": "https://test.salesforce.com",
                "selectors": {"sf_field": "Subject"},
                "element": {"tag": "input", "name": "Subject"},
                "value": "Test subject",
            },
            # Ctrl+S hotkey — KEEP
            {
                "seq": 2,
                "t": 1001000,
                "type": "keydown",
                "url": "https://test.salesforce.com",
                "selectors": {"css_path": "input"},
                "element": {"tag": "input"},
                "value": "Control+s",  # Modifier combo
            },
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Modifier keydown must survive as HOTKEY
    hotkey_actions = [a for a in bundle.actions if a.action_type == ActionType.HOTKEY]
    assert len(hotkey_actions) == 1, f"Modifier keydown must survive as HOTKEY, got {len(hotkey_actions)} hotkey actions"
    assert "s" in hotkey_actions[0].value.lower() or "save" in hotkey_actions[0].target.lower(), \
        f"Hotkey must capture the save intent: {hotkey_actions[0]}"


def test_click_to_focus_on_input_dropped_when_followed_by_input_event() -> None:
    """
    Contract 2.4: Click on an input immediately followed by an input event on
    the same element is dropped — the click is mechanical, the intent is the typed value.
    Reduction count must be accurate.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            # Click to focus — DROP
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"sf_field": "Subject"},
                "element": {"tag": "input", "name": "Subject", "id": "subject-field"},
            },
            # Input event on same element — KEEP
            {
                "seq": 2,
                "t": 1000100,  # 100ms later
                "type": "input",
                "url": "https://test.salesforce.com",
                "selectors": {"sf_field": "Subject"},
                "element": {"tag": "input", "name": "Subject", "id": "subject-field"},
                "value": "Test subject",
            },
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Must produce exactly ONE action (input, not click)
    assert len(bundle.actions) == 1, f"Click-to-focus must be dropped, got {len(bundle.actions)} actions"
    assert bundle.actions[0].action_type in (ActionType.INPUT, ActionType.SELECT), \
        f"Must keep the input action, got {bundle.actions[0].action_type}"

    # Reduction must be reported
    reduction_warning = next((w for w in bundle.warnings if "noise reduction" in w.lower()), None)
    assert reduction_warning is not None, "Click-to-focus reduction must be reported"
    # Verify count accuracy — 1 event dropped
    assert "dropped 1" in reduction_warning or "1 click" in reduction_warning or "2 raw events" in reduction_warning


def test_reduction_report_counts_are_accurate() -> None:
    """
    Contract 2.4: ReductionReport counts must be ACCURATE, not just present.
    A wrong count is a silent audit failure.
    """
    extractor = DomCaptureExtractor()

    # Build a trace with known reduction targets
    events_data = [
        # 3 input events on same element → coalesce to 1
        {
            "seq": 1,
            "t": 1000000,
            "type": "input",
            "url": "https://test.salesforce.com",
            "selectors": {"css_path": "input.subject"},
            "element": {"tag": "input", "id": "subject"},
            "value": "a",
        },
        {
            "seq": 2,
            "t": 1000100,
            "type": "input",
            "url": "https://test.salesforce.com",
            "selectors": {"css_path": "input.subject"},
            "element": {"tag": "input", "id": "subject"},
            "value": "ab",
        },
        {
            "seq": 3,
            "t": 1000200,
            "type": "input",
            "url": "https://test.salesforce.com",
            "selectors": {"css_path": "input.subject"},
            "element": {"tag": "input", "id": "subject"},
            "value": "abc",
        },
        # 1 keydown → drop
        {
            "seq": 4,
            "t": 1000300,
            "type": "keydown",
            "url": "https://test.salesforce.com",
            "selectors": {"css_path": "input.subject"},
            "element": {"tag": "input"},
            "value": "Enter",
        },
        # 1 scroll → likely drop
        {"seq": 5, "t": 1000400, "type": "scroll", "url": "https://test.salesforce.com"},
        # 1 click → keep
        {
            "seq": 6,
            "t": 1000500,
            "type": "click",
            "url": "https://test.salesforce.com",
            "selectors": {"text": "Save"},
            "element": {"tag": "button", "text": "Save"},
        },
    ]

    trace = synthesize_trace(events_data=events_data)
    bundle = extractor.extract_from_trace(trace)

    # Parse reduction warning
    reduction_warning = next((w for w in bundle.warnings if "noise reduction:" in w), None)
    assert reduction_warning is not None, "Must emit reduction summary"

    # Extract counts
    assert "6 raw events" in reduction_warning, f"Must report raw count accurately: {reduction_warning}"
    assert "coalesced 2 input" in reduction_warning, f"Must report coalesced count (3→1 means 2 dropped): {reduction_warning}"
    assert "1 keydown" in reduction_warning or "dropped 1 keydown" in reduction_warning, f"Must report keydown drops: {reduction_warning}"


# ============================================================================
# 3. Reduction must be auditable
# ============================================================================


def test_reduction_is_auditable_via_warnings() -> None:
    """
    Contract 2.4: Every dropped event count MUST be in bundle.warnings.
    Silent reduction is forbidden.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {"seq": 1, "t": 1000000, "type": "input", "url": "https://test.salesforce.com", "element": {"tag": "input", "id": "a"}, "value": "1"},
            {"seq": 2, "t": 1000100, "type": "input", "url": "https://test.salesforce.com", "element": {"tag": "input", "id": "a"}, "value": "2"},
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Must have a reduction summary in warnings
    assert any("noise reduction" in w.lower() for w in bundle.warnings), "Reduction must be auditable via warnings"
    assert any("coalesced" in w.lower() for w in bundle.warnings), "Coalescing must be reported"


# ============================================================================
# 4. SUBMIT vs CLICK classification (contract 2.3)
# ============================================================================


def test_submit_classification_by_element_type() -> None:
    """
    Contract 2.3: button[type=submit] → SUBMIT
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"text": "Submit"},
                "element": {"tag": "button", "type": "submit", "text": "Submit"},
            }
        ]
    )

    bundle = extractor.extract_from_trace(trace)
    assert bundle.actions[0].action_type == ActionType.SUBMIT, "button[type=submit] must be classified as SUBMIT"


@pytest.mark.parametrize("label", ["Save", "Submit", "Next", "Finish", "create", " APPLY ", "Confirm"])
def test_submit_classification_by_label(label: str) -> None:
    """
    Contract 2.3: Button labeled Save/Submit/Next/Finish → SUBMIT.
    Test case-insensitivity and surrounding whitespace.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"text": label},
                "element": {"tag": "button", "text": label, "aria_label": label},
            }
        ]
    )

    bundle = extractor.extract_from_trace(trace)
    assert (
        bundle.actions[0].action_type == ActionType.SUBMIT
    ), f"Button labeled '{label}' must be classified as SUBMIT"


@pytest.mark.parametrize("label", ["Cancel", "Filter", "Search", "Reset"])
def test_click_classification_for_non_submit_labels(label: str) -> None:
    """
    Contract 2.3: Button labeled Cancel/Filter → CLICK (not SUBMIT).
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"text": label},
                "element": {"tag": "button", "text": label, "aria_label": label},
            }
        ]
    )

    bundle = extractor.extract_from_trace(trace)
    assert bundle.actions[0].action_type == ActionType.CLICK, f"Button labeled '{label}' must be classified as CLICK"


# ============================================================================
# 5. Target prefix grammar compatibility with replay_browser
# ============================================================================


def test_target_prefix_grammar_produces_valid_selectors() -> None:
    """
    Contract 2.3: Emitted targets must use button:/input:/link:/text: prefixes
    and be consumable by replay_browser.build_selector_candidates.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"text": "Save", "test_id": "[data-testid='save-btn']"},
                "element": {"tag": "button", "text": "Save"},
            },
            {
                "seq": 2,
                "t": 1001000,
                "type": "input",
                "url": "https://test.salesforce.com",
                "selectors": {"sf_field": "Subject"},
                "element": {"tag": "input", "name": "Subject"},
                "value": "Test",
            },
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Check prefix grammar
    assert bundle.actions[0].target.startswith("button:"), f"Button target must use button: prefix: {bundle.actions[0].target}"
    assert bundle.actions[1].target.startswith("input:"), f"Input target must use input: prefix: {bundle.actions[1].target}"

    # Check replay integration
    for action in bundle.actions:
        candidates = build_selector_candidates(action)
        assert len(candidates) > 0, f"Target {action.target} must produce non-empty selector candidates for replay"


# ============================================================================
# 6. No invented labels
# ============================================================================


def test_no_label_derivable_produces_honest_target() -> None:
    """
    Contract: An element with no derivable label must NOT get a plausible
    fabricated one. Assert a warning is recorded and the target is honest
    (id-based or positional).
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"xpath": "/html/body/div[3]/button"},  # Only xpath, no label
                "element": {
                    "tag": "button",
                    "text": None,  # No text
                    "aria_label": None,  # No aria
                    "name": None,  # No name
                    "id": None,  # No id
                },
            }
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Must NOT invent a plausible label
    target = bundle.actions[0].target
    assert (
        "Save" not in target and "Submit" not in target and "Click" not in target
    ), f"Must not invent plausible labels: {target}"

    # Must use positional form (e.g., button:#1)
    assert "#" in target or "button:" in target, f"Must use positional or id-based target: {target}"

    # Must record a warning
    assert any(
        "weak target" in w.lower() or "no accessible name" in w.lower() for w in bundle.warnings
    ), f"Must warn about weak target: {bundle.warnings}"


# ============================================================================
# 7. Confidence comes from selector tier, NOT a constant
# ============================================================================


def test_confidence_varies_by_selector_tier() -> None:
    """
    Contract 2.2: Confidence mapping for ExtractedAction.confidence:
    tier 1-2 → 0.95, tier 3-4 → 0.85, tier 5 → 0.8, tier 6 → 0.6, tier 7-8 → 0.35.

    Build a trace with a data-testid element (tier 1) and one with only xpath (tier 8);
    assert the first action's confidence is materially higher.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            # Tier 1: data-testid
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"test_id": "[data-testid='save-btn']", "text": "Save"},
                "element": {"tag": "button", "text": "Save"},
            },
            # Tier 8: xpath only
            {
                "seq": 2,
                "t": 1001000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"xpath": "/html/body/div[1]/button"},
                "element": {"tag": "button"},
            },
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    tier1_action = bundle.actions[0]
    tier8_action = bundle.actions[1]

    # Tier 1 must have higher confidence than tier 8
    assert (
        tier1_action.confidence > tier8_action.confidence
    ), f"Tier 1 confidence ({tier1_action.confidence}) must be > tier 8 ({tier8_action.confidence})"

    # Tier 1 should be ~0.95
    assert tier1_action.confidence >= 0.9, f"Tier 1 confidence should be ~0.95, got {tier1_action.confidence}"

    # Tier 8 should be ~0.35
    assert tier8_action.confidence <= 0.5, f"Tier 8 confidence should be ~0.35, got {tier8_action.confidence}"


def test_confidence_not_hardcoded_across_all_actions() -> None:
    """
    Assert NOT all actions share one hardcoded confidence.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {"seq": 1, "t": 1000000, "type": "click", "selectors": {"test_id": "[data-testid='a']"}, "element": {"tag": "button"}},
            {"seq": 2, "t": 1001000, "type": "click", "selectors": {"xpath": "/html/body/div"}, "element": {"tag": "button"}},
            {"seq": 3, "t": 1002000, "type": "click", "selectors": {"aria": "button[aria-label='B']"}, "element": {"tag": "button"}},
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    confidences = {a.confidence for a in bundle.actions}
    assert len(confidences) > 1, f"Confidence must vary by selector tier, got single value: {confidences}"


# ============================================================================
# 8. Redaction respect
# ============================================================================


def test_redacted_value_produces_none_and_warning() -> None:
    """
    Contract: An event with value_redacted=True must produce an action whose
    value is None, plus a warning that a redacted value is needed at replay time.
    Assert the value is never reconstructed.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "input",
                "url": "https://test.salesforce.com",
                "selectors": {"sf_field": "Password"},
                "element": {"tag": "input", "type": "password", "name": "Password"},
                "value": None,  # Redacted
                "value_redacted": True,
            }
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Value must be None
    assert bundle.actions[0].value is None, "Redacted value must be None in action"

    # Must record a warning
    assert any(
        "redacted" in w.lower() and "manual input" in w.lower() for w in bundle.warnings
    ), f"Must warn about redacted value: {bundle.warnings}"


def test_leaked_sensitive_value_not_echoed_in_inferred_intent() -> None:
    """
    CRITICAL SECURITY: When the recorder FAILS to set value_redacted=True but the
    field name is sensitive (password, token, etc.), the extractor must NOT echo
    the leaked value in inferred_intent, warnings, or any other string that could
    be logged or printed to terminals.

    This is defense in depth: dom_capture.validate_trace already flags such leaks,
    but the extractor must also not launder them downstream.
    """
    from sf_video_blueprint.dom_capture import RawDomEvent, CaptureTrace

    extractor = DomCaptureExtractor()

    # Recorder bug: password field with value_redacted=False
    events_json = [
        {
            "v": 1,
            "seq": 1,
            "t": 1000000,
            "type": "input",
            "url": "https://test.salesforce.com",
            "frame_path": [],
            "selectors": {"sf_field": "Password"},
            "element": {"tag": "input", "type": "password", "name": "Password"},
            "value": "LEAKED_SECRET",
            "value_redacted": False,  # Recorder failed to redact
            "sf": {},
        }
    ]
    events = [RawDomEvent.model_validate(e) for e in events_json]
    trace = CaptureTrace(events=events, warnings=[], skipped_lines=[], manifest=None)

    bundle = extractor.extract_from_trace(trace)

    action = bundle.actions[0]

    # The leaked value WILL be in action.value (because value_redacted=False),
    # but it must NOT be in inferred_intent or any warnings
    assert action.value == "LEAKED_SECRET", "Recorder bug: value is present"

    # CRITICAL: inferred_intent must NOT echo the leaked value
    if action.inferred_intent:
        assert (
            "LEAKED_SECRET" not in action.inferred_intent
        ), f"SECURITY LEAK: inferred_intent echoes sensitive value: {action.inferred_intent}"

    # CRITICAL: warnings must NOT echo the leaked value
    for w in bundle.warnings:
        assert "LEAKED_SECRET" not in w, f"SECURITY LEAK: warning echoes sensitive value: {w}"


# ============================================================================
# 9. Determinism
# ============================================================================


def test_same_trace_yields_identical_bundle() -> None:
    """
    Contract: Same trace → identical recording_id and identical action list.
    The contract requires a deterministic ID, NOT a random UUID, so specs
    diff cleanly.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"text": "Save"},
                "element": {"tag": "button", "text": "Save"},
            }
        ]
    )

    bundle1 = extractor.extract_from_trace(trace)
    bundle2 = extractor.extract_from_trace(trace)

    # recording_id must be identical
    assert bundle1.recording_id == bundle2.recording_id, "Same trace must yield identical recording_id"

    # Actions must be identical
    assert len(bundle1.actions) == len(bundle2.actions), "Same trace must yield same action count"

    for a1, a2 in zip(bundle1.actions, bundle2.actions):
        assert a1.action_type == a2.action_type
        assert a1.target == a2.target
        assert a1.value == a2.value
        assert a1.confidence == a2.confidence


# ============================================================================
# 10. timestamp_ms is relative to first event
# ============================================================================


def test_timestamp_ms_relative_to_first_event() -> None:
    """
    Contract: timestamp_ms is relative to the first event (starts at 0)
    while absolute epoch survives in evidence metadata.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {"seq": 1, "t": 1737830000123, "type": "click", "url": "https://test.salesforce.com", "element": {"tag": "button"}},
            {"seq": 2, "t": 1737830001123, "type": "click", "url": "https://test.salesforce.com", "element": {"tag": "button"}},
            {"seq": 3, "t": 1737830002123, "type": "click", "url": "https://test.salesforce.com", "element": {"tag": "button"}},
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # First action must have timestamp_ms = 0
    assert bundle.actions[0].timestamp_ms == 0, f"First action timestamp_ms must be 0, got {bundle.actions[0].timestamp_ms}"

    # Second action must be 1000ms later
    assert bundle.actions[1].timestamp_ms == 1000, f"Second action timestamp_ms must be 1000, got {bundle.actions[1].timestamp_ms}"

    # Third action must be 2000ms later
    assert bundle.actions[2].timestamp_ms == 2000, f"Third action timestamp_ms must be 2000, got {bundle.actions[2].timestamp_ms}"


# ============================================================================
# 11. Evidence traceability
# ============================================================================


def test_evidence_artifact_links_to_source_event() -> None:
    """
    Contract: Every action has an EvidenceArtifact with DOM_SNAPSHOT type
    whose path_or_uri references the source seq — assert the link back to
    the exact recorded event exists. This is what makes the spec auditable.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 42,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com",
                "selectors": {"text": "Save"},
                "element": {"tag": "button", "text": "Save"},
            }
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    action = bundle.actions[0]
    assert len(action.evidence_ids) > 0, "Action must have evidence_ids"

    # Find the evidence artifact
    evidence_id = action.evidence_ids[0]
    evidence = next((e for e in bundle.evidence if e.artifact_id == evidence_id), None)
    assert evidence is not None, f"Evidence artifact {evidence_id} must exist in bundle"

    # Must be DOM_SNAPSHOT type
    assert evidence.evidence_type == EvidenceType.DOM_SNAPSHOT, f"Evidence type must be DOM_SNAPSHOT, got {evidence.evidence_type}"

    # Must reference the source seq
    assert "seq=42" in evidence.path_or_uri, f"Evidence must link to source seq: {evidence.path_or_uri}"


# ============================================================================
# 12. Protocol compatibility
# ============================================================================


def test_extractor_satisfies_extract_protocol(tmp_path: Path) -> None:
    """
    Contract: DomCaptureExtractor is usable where HeuristicVideoExtractor was
    (satisfies extract(path)), and an empty trace yields an empty-but-valid
    bundle rather than a crash.
    """
    extractor = DomCaptureExtractor()

    # Write an empty trace file
    trace_file = tmp_path / "empty.jsonl"
    trace_file.write_text("", encoding="utf-8")

    # Must not crash
    bundle = extractor.extract(trace_file)

    # Must be a valid bundle
    assert bundle.recording_id is not None
    assert isinstance(bundle.actions, list)
    assert len(bundle.actions) == 0
    assert "No events" in str(bundle.warnings)


def test_extractor_accepts_jsonl_not_mp4(tmp_path: Path) -> None:
    """
    Extractor accepts JSONL trace files despite the legacy parameter name
    `video_path`.
    """
    extractor = DomCaptureExtractor()

    trace_file = tmp_path / "trace.jsonl"
    # Write a minimal valid trace
    trace_file.write_text(
        '{"v":1,"seq":1,"t":1000000,"type":"click","url":"https://test.salesforce.com","frame_path":[],'
        '"selectors":{"text":"Save"},"element":{"tag":"button","text":"Save","classes":[]},'
        '"sf":{}}\n',
        encoding="utf-8",
    )

    bundle = extractor.extract(trace_file)
    assert len(bundle.actions) >= 1


# ============================================================================
# 13. End-to-end integration
# ============================================================================


def test_produced_actions_feed_correlation_and_spec_builder() -> None:
    """
    CRITICAL INTEGRATION TEST: Feed the produced actions into the REAL
    correlate_all and build_agent_spec and assert a sensible intent is derived.

    This proves Step 5 actually feeds the spec builder — the whole point
    of the pipeline. If it fails, that is a CRITICAL finding.
    """
    extractor = DomCaptureExtractor()

    trace = synthesize_trace(
        events_data=[
            {
                "seq": 1,
                "t": 1000000,
                "type": "click",
                "url": "https://test.salesforce.com/lightning/r/Case/500xx/view",
                "selectors": {"sf_field": "Status", "text": "Status"},
                "element": {"tag": "select", "name": "Status", "text": "Status"},
                "sf": {"object": "Case", "record_id": "500xx0000012345AAA"},
            },
            {
                "seq": 2,
                "t": 1001000,
                "type": "change",
                "url": "https://test.salesforce.com/lightning/r/Case/500xx/view",
                "selectors": {"sf_field": "Status", "text": "Status"},
                "element": {"tag": "select", "name": "Status", "text": "Status"},
                "value": "Working",
                "sf": {"object": "Case", "record_id": "500xx0000012345AAA"},
            },
            {
                "seq": 3,
                "t": 1002000,
                "type": "click",
                "url": "https://test.salesforce.com/lightning/r/Case/500xx/view",
                "selectors": {"text": "Save", "aria": "button[aria-label='Save']"},
                "element": {"tag": "button", "text": "Save", "aria_label": "Save"},
                "sf": {"object": "Case", "record_id": "500xx0000012345AAA"},
            },
        ]
    )

    bundle = extractor.extract_from_trace(trace)

    # Build minimal correlation inputs
    replay_events = [
        ReplayEvent(
            run_id="run-1",
            step_id=action.step_id,
            attempted_at=NOW,
            status=ReplayStatus.SUCCESS,
            attempt_no=1,
            duration_ms=10,
            message="ok",
        )
        for action in bundle.actions
    ]

    telemetry_events = [
        TelemetryEvent(
            correlation=CorrelationKey(run_id="run-1", step_id=bundle.actions[-1].step_id, event_time=NOW),
            layer=TelemetryLayer.FLOW,
            event_name="UpdateRecord",
            status="success",
        )
    ]

    snapshots = [
        ObjectSnapshot(
            correlation=CorrelationKey(run_id="run-1", step_id=bundle.actions[-1].step_id, event_time=NOW),
            object_api_name="Case",
            record_id="500xx0000012345AAA",
            before={"Status": "New"},
            after={"Status": "Working"},
            changed_fields=["Status"],
        )
    ]

    # Run the real correlation pipeline
    analyses = correlate_all(bundle.actions, replay_events, telemetry_events, snapshots)

    # Build the spec
    spec = build_agent_spec(bundle.actions, analyses)

    # ASSERT: Spec must derive a sensible intent
    assert spec.objects_touched == ["Case"], f"Spec must touch Case, got {spec.objects_touched}"
    assert "Case" in spec.intent, f"Spec intent must mention Case: {spec.intent}"
    assert "Status" in spec.intent or "status" in spec.intent, f"Spec intent must mention Status field: {spec.intent}"
    assert not spec.intent.startswith("UNRESOLVED"), f"Spec intent must be resolved, got: {spec.intent}"
    assert spec.confidence >= 0.5, f"Spec confidence must be reasonable, got {spec.confidence}"

    # Entities must include the changed field
    entity_fields = {e.field_api_name for e in spec.entities}
    assert "Status" in entity_fields, f"Spec entities must include Status field: {entity_fields}"


# ============================================================================
# Run the full test suite
# ============================================================================


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_extractor_uses_the_hardened_pattern_list_not_a_stale_copy() -> None:
    """REVIEW FINDING R2: the extractor kept its own copy of the sensitive-field
    patterns, and DEFECT L4-6 hardened only the one in `dom_capture`.

    The two lists diverged by eleven patterns. `dom_capture.validate_trace`
    correctly flagged an unredacted `IBAN__c` / `Passport_No__c` /
    `National_ID__c` / `Credential__c` as a leak, while the extractor — not
    recognising the field as sensitive — wrote the value straight into
    `inferred_intent`, which lands in the emitted spec, the HTML report and the
    terminal. Detecting a leak in one module and laundering it in the next is
    worse than not detecting it, because the finding says the control worked.

    Asserts on the SHARED constant rather than a hardcoded list, so the two
    cannot drift apart again.
    """
    from sf_video_blueprint.dom_capture import RawDomEvent, CaptureTrace, SENSITIVE_PATTERNS

    canary = "CANARY-MUST-NOT-APPEAR"
    extractor = DomCaptureExtractor()

    # One field name per pattern in the shared list, so adding a pattern to
    # dom_capture without teaching the extractor about it fails here.
    for pattern in SENSITIVE_PATTERNS:
        field_name = f"{pattern}__c"
        events = [
            RawDomEvent.model_validate(
                {
                    "v": 1,
                    "seq": 1,
                    "t": 1000000,
                    "type": "input",
                    "url": "https://test.salesforce.com",
                    "frame_path": [],
                    "selectors": {"sf_field": field_name},
                    "element": {"tag": "input", "type": "text", "name": field_name},
                    "value": canary,
                    "value_redacted": False,  # recorder failed to redact
                    "sf": {},
                }
            )
        ]
        trace = CaptureTrace(events=events, warnings=[], skipped_lines=[], manifest=None)

        bundle = extractor.extract_from_trace(trace)
        action = bundle.actions[0]

        assert canary not in (action.inferred_intent or ""), (
            f"SECURITY LEAK: field {field_name!r} matches the shared sensitive "
            f"pattern {pattern!r}, but the extractor echoed its value into "
            f"inferred_intent"
        )
        for warning in bundle.warnings:
            assert canary not in warning, (
                f"SECURITY LEAK: field {field_name!r} value echoed into a warning"
            )
