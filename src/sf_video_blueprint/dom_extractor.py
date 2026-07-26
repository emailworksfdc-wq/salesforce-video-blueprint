"""
DOM Capture Extractor — Step 5A4.

Converts raw DOM capture traces (JSONL format) into clean, replayable ActionExtractionBundle.
Primary responsibilities:
- Noise reduction: coalesce inputs, drop bubbling duplicates, filter scroll/keydown noise
- Action mapping: DOM events -> canonical ActionType with confidence based on selector tier
- Target synthesis: produce prefix grammar targets (button:, input:, link:, text:) for replay
- Evidence tracking: emit auditable provenance back to exact JSONL line/seq

This module is the centrepiece of Step 5 — it replaces the hardcoded single-step
HeuristicVideoExtractor with real capture-driven extraction.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    ActionExtractionBundle,
    ActionType,
    EvidenceArtifact,
    EvidenceType,
    ExtractedAction,
    UIContext,
)
from .redaction import pipeline_policy, redact_text, redact_url

# Defensive import for selectors module (agent A5)
try:
    from .selectors import best_selector, rank_selectors  # type: ignore[import]

    SELECTORS_AVAILABLE = True
except ImportError:
    SELECTORS_AVAILABLE = False

    # Fallback stubs for when A5's module isn't ready yet
    @dataclass
    class _FallbackRankedSelector:
        """Fallback RankedSelector when A5's module is not available."""

        selector: str
        tier: int
        confidence: float
        kind: str = "fallback"

    def rank_selectors(raw_selectors: Any, element: Any = None) -> list[_FallbackRankedSelector]:
        """Fallback selector ranker when A5's module is not available."""
        ranked = []
        tier_map = {
            "test_id": 1,
            "aria": 2,
            "role_name": 2,
            "label_for": 4,
            "sf_field": 5,
            "text": 6,
            "css_path": 7,
            "xpath": 8,
        }
        # Handle both dict and Pydantic model
        if hasattr(raw_selectors, "model_dump"):
            sel_dict = raw_selectors.model_dump()
        elif hasattr(raw_selectors, "__dict__"):
            sel_dict = raw_selectors.__dict__
        else:
            sel_dict = dict(raw_selectors)

        for key, selector in sel_dict.items():
            if selector is not None:
                tier = tier_map.get(key, 8)
                ranked.append(
                    _FallbackRankedSelector(
                        selector=str(selector), tier=tier, confidence=_tier_to_confidence(tier), kind=key
                    )
                )
        ranked.sort(key=lambda r: r.tier)
        return ranked

    def best_selector(raw_selectors: Any, element: Any = None) -> _FallbackRankedSelector | None:
        """Fallback best selector when A5's module is not available."""
        ranked = rank_selectors(raw_selectors, element)
        return ranked[0] if ranked else None


# Defensive import for dom_capture module (agent A3)
try:
    from .dom_capture import CaptureTrace, RawDomEvent, order_events, parse_capture_file  # type: ignore[import]

    DOM_CAPTURE_AVAILABLE = True
except ImportError:
    DOM_CAPTURE_AVAILABLE = False

    # Fallback stubs for when A3's module isn't ready yet
    # NOTE: These fallbacks won't be used in production since A3's module exists
    from pydantic import BaseModel

    class _FallbackElement(BaseModel):
        """Fallback element model."""

        tag: str = "unknown"
        type: str | None = None
        name: str | None = None
        id: str | None = None
        classes: list[str] = []
        aria_label: str | None = None
        text: str | None = None
        is_in_modal: bool = False
        modal_label: str | None = None
        shadow_depth: int = 0

    class _FallbackSelectors(BaseModel):
        """Fallback selectors model."""

        test_id: str | None = None
        aria: str | None = None
        role_name: dict[str, str] | None = None
        label_for: str | None = None
        sf_field: str | None = None
        css_path: str | None = None
        text: str | None = None
        xpath: str | None = None

    class _FallbackSF(BaseModel):
        """Fallback SF context model."""

        object: str | None = None
        record_id: str | None = None
        page_type: str | None = None
        app: str | None = None

    class RawDomEvent(BaseModel):
        """Fallback RawDomEvent when A3's module is not available."""

        v: int = 1
        seq: int = 1
        t: int = 0
        type: str = "unknown"
        url: str = ""
        frame_path: list[str] = []
        selectors: _FallbackSelectors
        element: _FallbackElement
        value: str | None = None
        value_redacted: bool = False
        sf: _FallbackSF

    @dataclass
    class CaptureTrace:
        """Fallback CaptureTrace when A3's module is not available."""

        events: list[RawDomEvent]
        warnings: list[str]
        skipped_lines: int
        manifest: dict[str, Any]

    def parse_capture_file(path: Path) -> CaptureTrace:
        """Fallback parser when A3's module is not available."""
        events = []
        warnings = []
        skipped = 0
        manifest = {}

        with path.open("r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    skipped += 1
                    continue
                try:
                    obj = json.loads(line)
                    # Convert to Pydantic models
                    evt = RawDomEvent(
                        v=obj.get("v", 1),
                        seq=obj.get("seq", line_num),
                        t=obj.get("t", 0),
                        type=obj.get("type", "unknown"),
                        url=obj.get("url", ""),
                        frame_path=obj.get("frame_path", []),
                        selectors=_FallbackSelectors(**(obj.get("selectors", {}))),
                        element=_FallbackElement(**(obj.get("element", {}))),
                        value=obj.get("value"),
                        value_redacted=obj.get("value_redacted", False),
                        sf=_FallbackSF(**(obj.get("sf", {}))),
                    )
                    events.append(evt)
                except Exception as e:
                    warnings.append(f"Line {line_num}: parse error: {e}")
                    skipped += 1

        return CaptureTrace(events=events, warnings=warnings, skipped_lines=skipped, manifest=manifest)

    def order_events(events: list[RawDomEvent]) -> list[RawDomEvent]:
        """Fallback event ordering when A3's module is not available."""
        return sorted(events, key=lambda e: (e.t, e.seq))


@dataclass
class ReductionReport:
    """Structured breakdown of noise reduction for testing and auditing."""

    raw_event_count: int
    output_action_count: int
    coalesced_input_count: int
    dropped_bubbling_count: int
    dropped_scroll_count: int
    dropped_keydown_count: int
    synthesized_navigate_count: int


def _tier_to_confidence(tier: int) -> float:
    """Map selector tier to confidence per contract 2.2."""
    if tier <= 2:
        return 0.95
    if tier <= 4:
        return 0.85
    if tier == 5:
        return 0.8
    if tier == 6:
        return 0.6
    return 0.35


def _is_submit_control(element: Any) -> bool:
    """Check if element is a submit control per contract 2.3."""
    # Handle both dict and Pydantic model
    tag = (getattr(element, "tag", None) or "").lower()
    elem_type = (getattr(element, "type", None) or "").lower()
    if tag == "button" and elem_type == "submit":
        return True
    if tag == "input" and elem_type == "submit":
        return True
    return False


def _is_submit_by_label(element: Any) -> bool:
    """Check if element's accessible name suggests submit intent per contract 2.3."""
    aria_label = (getattr(element, "aria_label", None) or "").strip().lower()
    text = (getattr(element, "text", None) or "").strip().lower()
    label = aria_label or text

    submit_patterns = ["save", "submit", "next", "finish", "create", "update", "apply", "confirm"]
    return label in submit_patterns


def _derive_target_prefix(element: Any) -> str:
    """Derive target prefix from element tag/role per contract 2.3."""
    tag = (getattr(element, "tag", None) or "").lower()
    elem_type = getattr(element, "type", None)

    # Check for button-like elements
    if tag in {"button", "input"} and elem_type in {"button", "submit"}:
        return "button:"
    if tag == "button":
        return "button:"

    # Check for input-like elements
    if tag in {"input", "textarea", "select"}:
        return "input:"

    # Check for links
    if tag == "a":
        return "link:"

    # Check ARIA roles if present
    classes = getattr(element, "classes", []) or []
    if any("combobox" in c.lower() for c in classes):
        return "input:"

    # Default fallback
    return "text:"


def _derive_target_label(element: Any, seq: int) -> str | None:
    """
    Derive accessible name for target label per contract 2.3.
    Returns None if no label can be derived (caller must handle).
    """
    # Priority: aria_label > text > name > id
    aria_label = (getattr(element, "aria_label", None) or "").strip()
    if aria_label:
        return aria_label

    text = (getattr(element, "text", None) or "").strip()
    if text:
        return text

    name = (getattr(element, "name", None) or "").strip()
    if name:
        return name

    elem_id = (getattr(element, "id", None) or "").strip()
    if elem_id:
        return elem_id

    # No label derivable
    return None


def _element_stable_key(event: RawDomEvent, best_sel: str | None) -> str:
    """
    Stable key for element identity across events (for coalescing).
    Uses best selector + frame_path + element id/name.
    """
    frame_key = "|".join(event.frame_path) if event.frame_path else "top"
    elem_id = getattr(event.element, "id", None) or ""
    elem_name = getattr(event.element, "name", None) or ""
    return f"{best_sel}|{frame_key}|{elem_id}|{elem_name}"


def _is_interactive_element(element: Any) -> bool:
    """Check if element is interactive (for bubbling duplicate detection)."""
    tag = (getattr(element, "tag", None) or "").lower()
    if tag in {"button", "a", "input", "select", "textarea"}:
        return True
    elem_type = getattr(element, "type", None)
    if elem_type in {"button", "submit", "reset"}:
        return True
    aria_label = getattr(element, "aria_label", None)
    if aria_label:
        return True
    return False


def _synthesize_navigate_actions(
    events: list[RawDomEvent], url_changes: list[tuple[int, str]]
) -> list[tuple[int, RawDomEvent]]:
    """
    Synthesize NAVIGATE actions when URL changes between consecutive events.
    Returns list of (insert_position, synthetic_event) tuples.
    """
    # Import here to avoid circular dependency issues
    try:
        from .dom_capture import RawElement, RawSalesforceContext, RawSelectors
    except ImportError:
        # Use fallback models if A3's module isn't available
        from pydantic import BaseModel

        class RawElement(BaseModel):  # type: ignore[no-redef]
            tag: str = "body"

        class RawSelectors(BaseModel):  # type: ignore[no-redef]
            pass

        class RawSalesforceContext(BaseModel):  # type: ignore[no-redef]
            pass

    synthesized = []
    for idx, new_url in url_changes:
        # Create a synthetic navigate event
        prev_event = events[idx - 1] if idx > 0 else events[0]
        nav_event = RawDomEvent(
            v=1,
            seq=prev_event.seq,  # Will be renumbered later
            t=prev_event.t + 1,  # Slight offset
            type="navigate",
            url=new_url,
            frame_path=[],
            selectors=RawSelectors(),
            element=RawElement(tag="body"),
            value=new_url,
            value_redacted=False,
            sf=RawSalesforceContext(),
        )
        synthesized.append((idx, nav_event))
    return synthesized


class DomCaptureExtractor:
    """
    Drop-in replacement for HeuristicVideoExtractor using DOM capture traces.

    Satisfies VideoActionExtractor protocol: extract(path: Path) -> ActionExtractionBundle.
    Accepts JSONL trace files (not videos). Also exposes extract_from_trace(trace)
    for testability without file IO.

    Primary value: noise reduction per contract 2.4 — a real 10-minute Salesforce
    recording produces 3-10x more raw events than meaningful steps. This extractor
    implements coalescing, deduplication, and synthetic navigation insertion.
    """

    def extract(self, video_path: Path) -> ActionExtractionBundle:
        """
        Extract actions from a DOM capture trace file.

        NOTE: Despite the parameter name `video_path` (legacy from VideoActionExtractor
        protocol), this accepts a JSONL trace file, not a video file.
        """
        if not video_path.exists():
            raise FileNotFoundError(f"Trace file not found: {video_path}")

        trace = parse_capture_file(video_path)
        bundle = self.extract_from_trace(trace)

        # Override source_video_path with actual file path (note: field name is legacy)
        bundle.source_video_path = str(video_path)
        return bundle

    def extract_from_trace(self, trace: CaptureTrace) -> ActionExtractionBundle:
        """
        Extract actions from a parsed CaptureTrace.

        Testability entry point — no file IO, operates on in-memory trace.
        """
        if not trace.events:
            return ActionExtractionBundle(
                recording_id=self._derive_recording_id(trace),
                source_video_path="<none>",
                extracted_at=datetime.now(timezone.utc),
                actions=[],
                evidence=[],
                warnings=["No events in trace"] + trace.warnings,
            )

        # Step 1: Order events
        ordered = order_events(trace.events)

        # Step 2: Noise reduction
        reduced, reduction_report = self._reduce_noise(ordered)

        # Step 3: Map to ExtractedAction
        actions = []
        evidence = []
        warnings = list(trace.warnings)

        base_time = reduced[0].t if reduced else 0

        for seq, event in enumerate(reduced, 1):
            action, evid, action_warnings = self._map_event_to_action(event, seq, base_time)
            actions.append(action)
            evidence.append(evid)
            warnings.extend(action_warnings)

        # Add reduction summary to warnings
        reduction_summary = (
            f"noise reduction: {reduction_report.raw_event_count} raw events -> "
            f"{reduction_report.output_action_count} actions "
            f"(coalesced {reduction_report.coalesced_input_count} input, "
            f"dropped {reduction_report.dropped_bubbling_count} bubbling, "
            f"{reduction_report.dropped_scroll_count} scroll, "
            f"{reduction_report.dropped_keydown_count} keydown, "
            f"synthesized {reduction_report.synthesized_navigate_count} navigate)"
        )
        warnings.insert(0, reduction_summary)

        # Step 4: Redact. THE choke point — see _redact_actions.
        redaction_note = self._redact_actions(actions)
        if redaction_note:
            warnings.insert(0, redaction_note)

        return ActionExtractionBundle(
            recording_id=self._derive_recording_id(trace),
            source_video_path="<none>",  # Will be overridden by extract()
            extracted_at=datetime.now(timezone.utc),
            actions=actions,
            evidence=evidence,
            warnings=warnings,
        )

    def _redact_actions(self, actions: list[ExtractedAction]) -> str | None:
        """Scrub secrets and PII out of extracted actions, in place.

        WHY HERE. This is the single point every consumer funnels through:
        `cli.py`, `pipeline.py` (and therefore `mcp_server.py`), and `write_bundle`
        all obtain their actions from `extract_from_trace`. Redacting here means the
        HTML report, the spec JSON, the extraction bundle, and every downstream
        emitter (`agent_script.py`, `agentforce_spec.py`, `eval_spec.py`,
        `iterate.py` version dirs) inherit clean data without each needing its own
        call. Sprinkling redaction across ~8 write sites would guarantee the next
        emitter added forgets it.

        WHY AFTER VALIDATION, NOT BEFORE. `dom_capture.validate_trace` exists to make
        a recorder redaction FAILURE visible, and it runs on the raw trace before this.
        If redaction ran first it would launder the recorder's bug: the operator would
        get a clean artifact and never learn their recorder is leaking. Detection reads
        raw bytes; redaction cleans what gets written. Both matter, in that order.

        WHAT IS COVERED. Free-text values, derived targets, inferred intents, and the
        URL/label fields of `ui_context` — the fields that carry org-controlled text
        into artifacts. `redact_url` handles URL query parameters, where the parameter
        NAME is the only signal (an OAuth `code` has no detectable shape).

        Returns:
            An audit note naming the categories redacted, or None if nothing fired.
            The note never contains the redacted bytes.
        """
        policy = pipeline_policy()
        categories: list[str] = []

        def _text(value: str | None) -> str | None:
            if not value:
                return value
            scrubbed, found = redact_text(value, policy)
            categories.extend(found)
            return scrubbed

        def _url(value: str | None) -> str | None:
            if not value:
                return value
            # Parameter-name pass first (catches shapeless credentials), then the
            # value-pattern pass (catches a token embedded in a path segment).
            scrubbed, found = redact_url(value)
            categories.extend(found)
            return _text(scrubbed)

        for action in actions:
            action.value = _text(action.value)
            action.target = _text(action.target) or action.target
            action.inferred_intent = _text(action.inferred_intent)
            action.expected_outcome = _text(action.expected_outcome)

            ctx = action.ui_context
            ctx.url = _url(ctx.url)
            ctx.page_title = _text(ctx.page_title)
            ctx.view_name = _text(ctx.view_name)
            ctx.modal_name = _text(ctx.modal_name)

        if not categories:
            return None

        # Categories only — never the values. A warning that echoes the secret it
        # removed would put it straight back into the report and the terminal.
        unique = sorted(set(categories))
        return (
            f"redaction: scrubbed {len(categories)} value(s) from extracted actions "
            f"(categories: {', '.join(unique)})"
        )

    def _derive_recording_id(self, trace: CaptureTrace) -> str:
        """
        Derive deterministic recording ID from trace content.

        Deterministic ID makes re-runs diffable. Uses manifest capture_id when
        available, else content hash.
        """
        if trace.manifest and "capture_id" in trace.manifest:
            return str(trace.manifest["capture_id"])

        # Fallback: hash trace content
        # Convert Pydantic models to dicts for serialization
        events_data = []
        for e in trace.events[:100]:
            if hasattr(e, "model_dump"):
                events_data.append(e.model_dump())
            elif hasattr(e, "__dict__"):
                events_data.append(e.__dict__)
            else:
                events_data.append(dict(e))

        content = json.dumps(events_data, sort_keys=True, default=str)
        digest = hashlib.sha256(content.encode()).hexdigest()
        return f"rec-{digest[:16]}"

    def _reduce_noise(self, events: list[RawDomEvent]) -> tuple[list[RawDomEvent], ReductionReport]:
        """
        Apply noise reduction per contract 2.4.

        Returns (reduced_events, reduction_report).
        """
        raw_count = len(events)

        # Phase 1: Coalesce consecutive input/change on same element
        coalesced, coalesced_count = self._coalesce_input_events(events)

        # Phase 2: Drop event-bubbling duplicates
        no_bubbling, bubbling_count = self._drop_bubbling_duplicates(coalesced)

        # Phase 3: Drop scroll (conservative heuristic)
        no_scroll, scroll_count = self._drop_scroll(no_bubbling)

        # Phase 4: Synthesize navigate actions
        with_navigate, navigate_count = self._synthesize_navigate(no_scroll)

        # Phase 5: Drop keydown noise
        final, keydown_count = self._drop_keydown_noise(with_navigate)

        report = ReductionReport(
            raw_event_count=raw_count,
            output_action_count=len(final),
            coalesced_input_count=coalesced_count,
            dropped_bubbling_count=bubbling_count,
            dropped_scroll_count=scroll_count,
            dropped_keydown_count=keydown_count,
            synthesized_navigate_count=navigate_count,
        )

        return final, report

    def _coalesce_input_events(self, events: list[RawDomEvent]) -> tuple[list[RawDomEvent], int]:
        """
        Coalesce consecutive input/change events on the same element.
        Also drops clicks on input fields immediately followed by input/change
        (focus click is noise).

        Returns (coalesced_events, count_coalesced).
        """
        if not events:
            return [], 0

        coalesced = []
        pending: dict[str, tuple[RawDomEvent, int]] = {}  # key -> (event, first_index)
        pending_focus_clicks: dict[str, tuple[RawDomEvent, int]] = {}  # click before input/change
        dropped_count = 0

        for idx, event in enumerate(events):
            if event.type not in {"input", "change", "click"}:
                # Flush all pending, add this event
                coalesced.extend(e for e, _ in pending.values())
                coalesced.extend(e for e, _ in pending_focus_clicks.values())
                pending.clear()
                pending_focus_clicks.clear()
                coalesced.append(event)
                continue

            best_ranked = best_selector(event.selectors, event.element)
            sel_str = best_ranked.selector if best_ranked else None
            key = _element_stable_key(event, sel_str)

            if event.type == "click":
                # Check if this is a click on an input/textarea (potential focus click)
                # Note: select clicks are NOT dropped because clicking a select opens
                # the dropdown, which is meaningful UI state change
                tag = (getattr(event.element, "tag", None) or "").lower()
                if tag in {"input", "textarea"}:
                    # Hold this click — might be followed by input
                    if key in pending_focus_clicks:
                        # Multiple clicks on same element, keep the last
                        dropped_count += 1
                    pending_focus_clicks[key] = (event, idx)
                else:
                    # Click on non-input element (including select), flush pending and add
                    coalesced.extend(e for e, _ in pending.values())
                    coalesced.extend(e for e, _ in pending_focus_clicks.values())
                    pending.clear()
                    pending_focus_clicks.clear()
                    coalesced.append(event)
                continue

            # Input/change event — check if we can coalesce
            if key in pending:
                # Replace with this one (keeps final value)
                pending[key] = (event, pending[key][1])
                dropped_count += 1
            else:
                # First occurrence of this key
                # Check if there's a pending focus click for this element
                if key in pending_focus_clicks:
                    # Drop the focus click, it's noise
                    pending_focus_clicks.pop(key)
                    dropped_count += 1

                # Flush all other pending
                for other_key in list(pending.keys()):
                    if other_key != key:
                        coalesced.append(pending.pop(other_key)[0])
                for other_key in list(pending_focus_clicks.keys()):
                    coalesced.append(pending_focus_clicks.pop(other_key)[0])

                pending[key] = (event, idx)

        # Flush remaining
        coalesced.extend(e for e, _ in pending.values())
        coalesced.extend(e for e, _ in pending_focus_clicks.values())

        return coalesced, dropped_count

    def _drop_bubbling_duplicates(self, events: list[RawDomEvent]) -> tuple[list[RawDomEvent], int]:
        """
        Drop click events on non-interactive containers followed within 150ms
        by click on descendant/ancestor interactive element.

        Returns (filtered_events, count_dropped).
        """
        if len(events) < 2:
            return events, 0

        filtered = []
        dropped_count = 0
        i = 0

        while i < len(events):
            event = events[i]

            if event.type != "click":
                filtered.append(event)
                i += 1
                continue

            # Look ahead for another click within 150ms
            is_bubbling = False
            if i + 1 < len(events):
                next_event = events[i + 1]
                if next_event.type == "click" and (next_event.t - event.t) <= 150:
                    # Check if current is non-interactive and next is interactive
                    if not _is_interactive_element(event.element) and _is_interactive_element(next_event.element):
                        is_bubbling = True

            if is_bubbling:
                dropped_count += 1
            else:
                filtered.append(event)

            i += 1

        return filtered, dropped_count

    def _drop_scroll(self, events: list[RawDomEvent]) -> tuple[list[RawDomEvent], int]:
        """
        Drop scroll events unless they immediately precede interaction with
        a different element (heuristic: scroll enabled reaching off-screen element).

        Returns (filtered_events, count_dropped).
        """
        if len(events) < 2:
            return events, 0

        filtered = []
        dropped_count = 0

        for i, event in enumerate(events):
            if event.type != "scroll":
                filtered.append(event)
                continue

            # Check if next event is on a different element
            keep_scroll = False
            if i + 1 < len(events):
                next_event = events[i + 1]
                # Conservative: keep scroll if next event is not also scroll
                if next_event.type != "scroll":
                    keep_scroll = True

            if keep_scroll:
                filtered.append(event)
            else:
                dropped_count += 1

        return filtered, dropped_count

    def _synthesize_navigate(self, events: list[RawDomEvent]) -> tuple[list[RawDomEvent], int]:
        """
        Synthesize NAVIGATE actions when URL changes between consecutive events.

        Returns (events_with_navigate, count_synthesized).
        """
        if len(events) < 2:
            return events, 0

        url_changes = []
        for i in range(1, len(events)):
            prev_url = events[i - 1].url
            curr_url = events[i].url
            if curr_url != prev_url:
                url_changes.append((i, curr_url))

        if not url_changes:
            return events, 0

        # Insert synthetic navigate events
        synthesized = _synthesize_navigate_actions(events, url_changes)
        result = list(events)

        for offset, (insert_idx, nav_event) in enumerate(synthesized):
            result.insert(insert_idx + offset, nav_event)

        return result, len(synthesized)

    def _drop_keydown_noise(self, events: list[RawDomEvent]) -> tuple[list[RawDomEvent], int]:
        """
        Drop keydown events unless they are modifier combos (hotkey).

        Per contract 2.3: "keydown with modifier → HOTKEY"
        Plain keydowns (including Enter/Tab) are dropped because input/change
        events already capture the final value.

        Returns (filtered_events, count_dropped).
        """
        filtered = []
        dropped_count = 0

        for event in events:
            if event.type != "keydown":
                filtered.append(event)
                continue

            # Check if it's a modifier combo
            value = (event.value or "").strip()

            # Modifier combos: Ctrl+X, Command+X, Alt+X, Shift+X
            # Common encoding patterns: "Control+S", "Meta+K", "Alt+Tab"
            has_modifier = any(
                mod in value
                for mod in ["Control+", "Ctrl+", "Meta+", "Command+", "Cmd+", "Alt+", "Shift+"]
            )

            if has_modifier:
                # Keep this keydown as a HOTKEY action
                filtered.append(event)
            else:
                # Plain keydown (including Enter, Tab, etc.) — drop as noise
                dropped_count += 1

        return filtered, dropped_count

    def _map_event_to_action(
        self, event: RawDomEvent, sequence: int, base_time: int
    ) -> tuple[ExtractedAction, EvidenceArtifact, list[str]]:
        """
        Map a single RawDomEvent to ExtractedAction + EvidenceArtifact.

        Returns (action, evidence, warnings).
        """
        warnings = []

        # Rank selectors - A5's API changed, best_selector is now a standalone function
        # that takes raw_selectors and element directly
        best_ranked = best_selector(event.selectors, event.element)
        if best_ranked:
            best_sel = best_ranked.selector
            tier = best_ranked.tier
            confidence = best_ranked.confidence
        else:
            best_sel = None
            tier = 8
            confidence = _tier_to_confidence(tier)

        # Derive action type per contract 2.3
        action_type = self._derive_action_type(event)

        # Derive target (prefix:label format)
        target_prefix = _derive_target_prefix(event.element)
        target_label = _derive_target_label(event.element, sequence)

        if target_label is None:
            # No label derivable — use positional form and warn
            tag = getattr(event.element, "tag", "unknown")
            target = f"{target_prefix}{tag}#{sequence}"
            warnings.append(f"Step {sequence}: weak target (no accessible name), using positional form")
        else:
            target = f"{target_prefix}{target_label}"

        # Handle redaction
        value = event.value
        inferred_intent = None
        expected_outcome = None

        if event.value_redacted:
            value = None
            warnings.append(f"Step {sequence}: value redacted, requires manual input at replay")
        else:
            # Derive intent conservatively
            if action_type == ActionType.INPUT and value:
                # Access Pydantic model fields properly
                field_name = getattr(event.selectors, "sf_field", None) or getattr(event.element, "name", None)
                if field_name:
                    # SECURITY: Never echo sensitive values in inferred_intent (defense in depth).
                    # dom_capture.validate_trace already flags leaks, but we must not launder them.
                    sensitive_patterns = [
                        "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
                        "ssn", "social_security", "card", "credit", "cvv", "pin"
                    ]
                    field_lower = field_name.lower()
                    if any(pattern in field_lower for pattern in sensitive_patterns):
                        inferred_intent = f"Set {field_name} (redacted)"
                    else:
                        inferred_intent = f"Set {field_name} to {value}"
            elif action_type == ActionType.SUBMIT:
                inferred_intent = "Submit form"

        # Build UIContext
        ui_context = UIContext(
            page_title=getattr(event.sf, "app", None),
            app_name=getattr(event.sf, "app", None),
            object_name=getattr(event.sf, "object", None),
            view_name=None,
            modal_name=getattr(event.element, "modal_label", None),
            selector_hint=best_sel,
            url=event.url,
        )

        # Build evidence artifact
        evidence_id = f"evid-seq-{sequence}"
        # Convert Pydantic models to dicts for metadata storage
        selectors_dict = event.selectors.model_dump() if hasattr(event.selectors, "model_dump") else dict(event.selectors)

        evidence = EvidenceArtifact(
            artifact_id=evidence_id,
            evidence_type=EvidenceType.DOM_SNAPSHOT,
            path_or_uri=f"<trace>#seq={event.seq}",  # Will be overridden with real path
            captured_at=datetime.fromtimestamp(event.t / 1000, tz=timezone.utc),
            confidence=confidence,
            metadata={
                "selectors": selectors_dict,
                "tier": tier,
                "frame_path": event.frame_path,
                "shadow_depth": getattr(event.element, "shadow_depth", 0),
                "raw_type": event.type,
            },
        )

        action = ExtractedAction(
            step_id=f"step-{sequence:03d}",
            sequence=sequence,
            timestamp_ms=event.t - base_time,
            action_type=action_type,
            target=target,
            value=value,
            ui_context=ui_context,
            confidence=confidence,
            inferred_intent=inferred_intent,
            expected_outcome=expected_outcome,
            evidence_ids=[evidence_id],
        )

        return action, evidence, warnings

    def _derive_action_type(self, event: RawDomEvent) -> ActionType:
        """Derive ActionType from raw event type per contract 2.3."""
        raw_type = event.type

        if raw_type == "click":
            # Check if it should be SUBMIT
            if _is_submit_control(event.element) or _is_submit_by_label(event.element):
                return ActionType.SUBMIT
            return ActionType.CLICK

        if raw_type in {"input", "change"}:
            # Check if it's a select
            tag = (getattr(event.element, "tag", None) or "").lower()
            classes = getattr(event.element, "classes", None) or []
            if tag == "select" or any("combobox" in c.lower() for c in classes):
                return ActionType.SELECT
            return ActionType.INPUT

        if raw_type == "navigate":
            return ActionType.NAVIGATE

        if raw_type == "scroll":
            return ActionType.SCROLL

        if raw_type == "keydown":
            return ActionType.HOTKEY

        # Fallback
        return ActionType.CLICK


def write_bundle(bundle: ActionExtractionBundle, path: Path) -> None:
    """
    Write ActionExtractionBundle to JSON file via Pydantic model_dump_json.

    Allows converting a DOM capture once and reusing the bundle without re-parsing.
    """
    json_str = bundle.model_dump_json(indent=2)
    path.write_text(json_str, encoding="utf-8")
