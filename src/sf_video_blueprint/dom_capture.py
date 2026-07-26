"""Parser and validation layer for dom_capture.jsonl — the boundary between
untrusted browser output and the typed pipeline.

This module is responsible for:
1. Parsing raw JSONL capture files into validated Pydantic models
2. Handling malformed lines gracefully (never aborting the parse)
3. Preserving driver-stamped metadata fields (_ingest_seq, _ingest_t, etc.)
4. Version detection and forward-compatibility warnings
5. Canonical event ordering (respecting driver-stamped ingest_seq)
6. Integrity validation (redaction leaks, manifest mismatches, monotonicity)
7. Loading optional manifest files

The recorder is untrusted. This layer verifies its output rather than assuming
correctness — defense in depth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ============================================================================
# Raw capture models — mirror INTERFACE_CONTRACT.md section 2.1 EXACTLY
# ============================================================================


class RawEventType(str, Enum):
    """Known event types from the recorder.

    IMPORTANT: Do NOT reject unknown types. A future recorder version may add
    new event types. Unknown values must be preserved and reported as warnings,
    not exceptions.
    """

    CLICK = "click"
    INPUT = "input"
    CHANGE = "change"
    SUBMIT = "submit"
    NAVIGATE = "navigate"
    KEYDOWN = "keydown"
    SCROLL = "scroll"


class RawRoleName(BaseModel):
    """Accessibility role + name pair for get_by_role.

    Both fields are nullable, because the recorder genuinely emits nulls:

    - `capture/recorder.js:161` — `return { role, name: null }` is the terminal
      branch of `getRoleAndName`. An element with no aria-label, no resolvable
      aria-labelledby, no text, no title and no alt has no accessible name.
    - `capture/recorder.js:136` — `role` is `explicitRole || implicitRole`, and
      the implicit-role map covers only ~17 tags. A `div` or `span`, which is
      most of Lightning's clickable markup, has no role.

    Requiring both fields made the parser reject its own recorder's documented
    output: the whole event was discarded because ONE of eight selector
    strategies was empty. `strict` type checking is retained, so a role that
    is a number or a list is still malformed input and still lands in
    `skipped_lines` — this loosens nullability, not typing.

    The invariant that the required fields were really protecting — never
    emitting a `role=None[name="..."]` selector — is enforced where it belongs,
    in `selectors._build_role_selector`, which returns None when role is falsy.
    """

    model_config = ConfigDict(strict=True)

    role: str | None = None
    name: str | None = None


class RawSelectors(BaseModel):
    """All selector strategies computed by the recorder.

    All fields are nullable — the recorder emits null when it cannot compute
    a given selector type. The downstream selector ranking layer (selectors.py)
    will filter nulls and rank what's present.
    """

    test_id: str | None = None
    aria: str | None = None
    role_name: RawRoleName | None = None
    label_for: str | None = None
    sf_field: str | None = None
    css_path: str | None = None
    text: str | None = None
    xpath: str | None = None


class RawElement(BaseModel):
    """Element metadata captured at interaction time."""

    tag: str
    type: str | None = None
    name: str | None = None
    id: str | None = None
    classes: list[str] = Field(default_factory=list)
    aria_label: str | None = None
    text: str | None = None
    is_in_modal: bool = False
    modal_label: str | None = None
    shadow_depth: int = Field(ge=0, default=0)


class RawSalesforceContext(BaseModel):
    """Salesforce-specific context, best effort."""

    object: str | None = None
    record_id: str | None = None
    page_type: str | None = None
    app: str | None = None


class RawDomEvent(BaseModel):
    """A single DOM event from the recorder.

    CRITICAL: Pydantic treats leading-underscore field names as private and will
    NOT serialize them by default. The driver-stamped fields (_ingest_seq, etc.)
    must be handled explicitly via Field(alias="...") with populate_by_name=True
    in model_config.
    """

    model_config = ConfigDict(populate_by_name=True)

    # Schema version and sequence
    v: int = Field(ge=0)  # ge=0 to allow best-effort parsing of v=0 (older versions)
    seq: int = Field(ge=1)
    t: int = Field(ge=0)  # epoch ms

    # Event details
    type: str  # NOT constrained to RawEventType enum — unknown types must parse
    url: str
    frame_path: list[str] = Field(default_factory=list)

    # Selectors and element
    selectors: RawSelectors
    element: RawElement

    # Value (redacted if sensitive)
    value: str | None = None
    value_redacted: bool = False

    # Salesforce context
    sf: RawSalesforceContext

    # Driver-stamped metadata (added by agent A2, the Playwright driver)
    # These fields are AUTHORITATIVE for ordering — they cannot be faked by the page
    ingest_seq: int | None = Field(default=None, alias="_ingest_seq")
    ingest_t: int | None = Field(default=None, alias="_ingest_t")
    frame_url: str | None = Field(default=None, alias="_frame_url")
    page_index: int | None = Field(default=None, alias="_page_index")

    def is_known_type(self) -> bool:
        """Returns True if this event's type is in the known RawEventType enum."""
        try:
            RawEventType(self.type)
            return True
        except ValueError:
            return False


# ============================================================================
# Manifest model
# ============================================================================


class CaptureManifest(BaseModel):
    """Metadata about the capture session (dom_capture.manifest.json).

    Written by the driver (agent A2) at the end of recording. A trace without
    a manifest is degraded but usable.
    """

    capture_id: str
    org_alias: str
    org_instance_url: str
    is_sandbox: bool
    is_scratch: bool
    started_at: str  # ISO 8601
    ended_at: str | None = None  # null if recording was aborted
    event_count: int = Field(ge=0)
    network_event_count: int = Field(ge=0)
    sink_errors: int = Field(ge=0)
    recorder_sha256: str | None = None
    playwright_version: str | None = None
    operator_note: str | None = None


# ============================================================================
# Trace container
# ============================================================================


@dataclass
class CaptureTrace:
    """Parsed capture trace with events, warnings, and optional manifest."""

    events: list[RawDomEvent]
    warnings: list[str] = field(default_factory=list)
    skipped_lines: list[tuple[int, str]] = field(default_factory=list)
    manifest: CaptureManifest | None = None


# ============================================================================
# Parsing
# ============================================================================


SUPPORTED_VERSION = 1


def parse_capture_file(path: Path) -> CaptureTrace:
    """Parse a dom_capture.jsonl file into a validated CaptureTrace.

    NEVER aborts on malformed lines — a truncated final line is EXPECTED when
    a recording is Ctrl-C'd. Records line number + reason in skipped_lines and
    continues.

    Version handling:
    - If v != SUPPORTED_VERSION and v > SUPPORTED_VERSION: loud warning + raise
      (forward-incompatible)
    - If v < SUPPORTED_VERSION: warning + best-effort parse (backward-compatible)
    """
    events = []
    warnings = []
    skipped_lines = []

    if not path.exists():
        raise FileNotFoundError(f"Capture file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                skipped_lines.append((line_num, f"JSON decode error: {e}"))
                continue

            # BUG FIX 1: Check if raw is a dict — valid JSON can parse to non-dict types
            if not isinstance(raw, dict):
                skipped_lines.append((line_num, f"Valid JSON but not an object (got {type(raw).__name__})"))
                continue

            # Version check
            version = raw.get("v")
            if version is None:
                warnings.append(f"Line {line_num}: missing 'v' field, assuming v={SUPPORTED_VERSION}")
                # BUG FIX 3: Actually default the version so validation doesn't reject it
                raw.setdefault("v", SUPPORTED_VERSION)
            elif version != SUPPORTED_VERSION:
                if version > SUPPORTED_VERSION:
                    msg = (
                        f"Line {line_num}: schema version {version} is NEWER than "
                        f"supported version {SUPPORTED_VERSION}. This parser is out "
                        f"of date and cannot safely parse this trace."
                    )
                    warnings.append(msg)
                    raise ValueError(msg)
                else:
                    warnings.append(
                        f"Line {line_num}: schema version {version} is older than "
                        f"supported version {SUPPORTED_VERSION}. Attempting best-effort parse."
                    )

            # Parse event
            try:
                event = RawDomEvent.model_validate(raw)
                events.append(event)

                # Warn on unknown event types
                if not event.is_known_type():
                    warnings.append(
                        f"Line {line_num}: unknown event type '{event.type}'. "
                        f"This is not an error — a future recorder version may have "
                        f"added it — but the type is preserved as-is for passthrough."
                    )

            except Exception as e:
                skipped_lines.append((line_num, f"Validation error: {e}"))
                continue

    return CaptureTrace(
        events=events,
        warnings=warnings,
        skipped_lines=skipped_lines,
        manifest=None,  # loaded separately via load_manifest
    )


def load_manifest(path: Path) -> CaptureManifest | None:
    """Load dom_capture.manifest.json if present.

    Returns None if the file does not exist — a trace without a manifest is
    degraded but usable.
    """
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return CaptureManifest.model_validate(raw)
    except Exception:
        # Silently return None — the caller will see manifest=None and can
        # decide whether to warn. We don't want load_manifest itself to raise
        # on a malformed manifest, because that would make the entire trace
        # unusable.
        return None


# ============================================================================
# Canonical event ordering
# ============================================================================


def order_events(events: list[RawDomEvent]) -> list[RawDomEvent]:
    """Canonical ordering function for events.

    SUBTLE: `seq` restarts per document/frame, so it is NOT globally sortable.
    The driver-stamped `ingest_seq` is AUTHORITATIVE — it is set by the trusted
    driver and cannot be faked by the page. When present, sort by `ingest_seq`.
    Otherwise fall back to `(t, seq)`, which is the best we can do for traces
    recorded by an older driver that doesn't stamp ingest_seq.

    If both ingest_seq and (t, seq) are identical, preserve input order.
    """

    def sort_key(event: RawDomEvent) -> tuple[int, int, int]:
        if event.ingest_seq is not None:
            # Driver-stamped ingest_seq is authoritative
            return (0, event.ingest_seq, 0)
        else:
            # Fallback: (t, seq) — seq is only locally monotonic per frame
            return (1, event.t, event.seq)

    return sorted(events, key=sort_key)


# ============================================================================
# Validation (integrity checks, NOT input validation)
# ============================================================================


def validate_trace(trace: CaptureTrace) -> list[str]:
    """Returns human-readable integrity findings.

    DOES NOT RAISE — returns a list of findings. An empty list means no issues.

    Checks:
    - Event count vs manifest.event_count mismatch
    - manifest.sink_errors > 0 (recorder reported write failures)
    - Redaction leaks (value_redacted=True but value still present, OR value present + field name looks sensitive)
    - Events with non-empty frame_path but missing _frame_url
    - Monotonicity violations in ingest_seq
    - All events have identical t (broken clock or synthetic data)
    - Zero events (empty trace)
    - Material data loss (zero events parsed while lines were skipped, or substantial fraction skipped)
    """
    findings = []

    # Zero events
    if not trace.events:
        findings.append("CRITICAL: Trace contains zero events.")
        # Continue to check for data-loss warnings even with zero events

    # Manifest checks
    if trace.manifest:
        if trace.manifest.event_count != len(trace.events):
            findings.append(
                f"Manifest reports {trace.manifest.event_count} events, "
                f"but parsed {len(trace.events)} events."
            )
        if trace.manifest.sink_errors > 0:
            findings.append(
                f"Manifest reports {trace.manifest.sink_errors} sink errors — "
                f"some events may not have been written to disk."
            )

    # Redaction leak detection
    sensitive_patterns = [
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "ssn",
        "social_security",
        "card",
        "credit",
        "cvv",
        "pin",
    ]

    for i, event in enumerate(trace.events):
        # DEFECT A1 FIX: Check for redaction flag set but value still present
        # This is the most serious leak: recorder claims it redacted but didn't.
        # NEVER include the leaked value in the finding text — that would defeat the purpose.
        #
        # DESIGN DECISION: We do NOT scrub event.value automatically here.
        # Rationale:
        # 1. Detection > silent fixes: The finding makes the breach visible to the operator.
        # 2. Callers may depend on `value` being present for non-redacted events. Scrubbing
        #    could break downstream code that filters on value_redacted=False.
        # 3. The audit trail must show what actually happened. If a value leaked, the trace
        #    should reflect that truth — not a post-hoc sanitization that hides the recorder bug.
        # 4. Scrubbing without visibility is dangerous: it would make the recorder's failure
        #    invisible in the raw data, defeating the entire purpose of this validation layer.
        #
        # The operator's correct response is to fix the recorder and re-record, not to rely on
        # the parser to silently clean up after a redaction failure.
        if event.value_redacted and event.value is not None:
            findings.append(
                f"SECURITY CRITICAL: Event {i} (seq={event.seq}): value_redacted=True "
                f"but value is still present. The recorder FAILED to redact a value it "
                f"identified as sensitive. This is a redaction leak."
                # NOTE: Do NOT interpolate event.value here — the finding will be printed
                # to terminals and written to reports. Echoing the secret defeats the purpose.
            )

        # Original check: value present without redaction flag, but field name looks sensitive
        if event.value is not None and not event.value_redacted:
            field_name_lower = (event.element.name or "").lower()
            if any(pattern in field_name_lower for pattern in sensitive_patterns):
                findings.append(
                    f"SECURITY: Event {i} (seq={event.seq}): value is present "
                    f"but field name '{event.element.name}' looks sensitive. "
                    f"Redaction may have FAILED."
                )

        # Frame path without frame URL
        if event.frame_path and not event.frame_url:
            findings.append(
                f"Event {i} (seq={event.seq}): has non-empty frame_path but "
                f"missing _frame_url (driver metadata)."
            )

    # Monotonicity check on ingest_seq
    ingest_seqs = [e.ingest_seq for e in trace.events if e.ingest_seq is not None]
    if ingest_seqs:
        for i in range(1, len(ingest_seqs)):
            if ingest_seqs[i] <= ingest_seqs[i - 1]:
                findings.append(
                    f"Monotonicity violation: ingest_seq={ingest_seqs[i]} at "
                    f"position {i} is not greater than previous {ingest_seqs[i-1]}."
                )
                break  # Only report the first violation

    # Broken clock detection
    timestamps = [e.t for e in trace.events]
    if len(set(timestamps)) == 1 and len(timestamps) > 1:
        findings.append(
            f"All {len(timestamps)} events have identical timestamp t={timestamps[0]}. "
            f"This suggests a broken clock or synthetic data."
        )

    # DEFECT A2 FIX: Data loss warnings for material parse failures
    # The audit trail IS the product for this project, so operators must be told when
    # evidence is discarded. Thresholds:
    # - Zero events parsed while lines were skipped: warn unconditionally (100% loss)
    # - Partial loss: warn when >=50% of lines were skipped (substantial data loss)
    #
    # Rationale for 50%: A recorder that emits one bad line among 500 good ones should
    # still produce a usable trace without alarming the operator. But if half or more
    # of the capture is unreadable, that signals a drift or misconfiguration that must
    # be visible in the summary.
    total_lines = len(trace.events) + len(trace.skipped_lines)
    if total_lines > 0:
        skip_ratio = len(trace.skipped_lines) / total_lines
        if len(trace.events) == 0 and len(trace.skipped_lines) > 0:
            # 100% data loss
            findings.append(
                f"DATA LOSS: Zero events parsed, but {len(trace.skipped_lines)} lines "
                f"were skipped. All capture data was discarded. Check for recorder/parser "
                f"version drift or schema mismatch."
            )
        elif skip_ratio >= 0.5:
            # Substantial partial loss
            findings.append(
                f"DATA LOSS: {len(trace.skipped_lines)} of {total_lines} lines were skipped "
                f"({skip_ratio:.0%}). More than half the capture was discarded. Check for "
                f"recorder/parser version drift or schema mismatch."
            )

    return findings


# ============================================================================
# Redaction audit
# ============================================================================


def redaction_audit(trace: CaptureTrace) -> tuple[int, list[str]]:
    """Audit redaction: count redacted values and list suspected leaks.

    Returns:
        (redacted_count, leak_findings)

    Where leak_findings is a list of strings describing suspected redaction
    failures — events where a value is present but the field name looks sensitive.

    Defense in depth: the recorder is supposed to redact, but we verify rather
    than trusting it.
    """
    redacted_count = sum(1 for e in trace.events if e.value_redacted)
    leak_findings = []

    sensitive_patterns = [
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "ssn",
        "social_security",
        "card",
        "credit",
        "cvv",
        "pin",
    ]

    for i, event in enumerate(trace.events):
        if event.value is not None and not event.value_redacted:
            field_name_lower = (event.element.name or "").lower()
            if any(pattern in field_name_lower for pattern in sensitive_patterns):
                leak_findings.append(
                    f"Event index {i} (seq={event.seq}, type={event.type}): "
                    f"value present but field name '{event.element.name}' matches "
                    f"sensitive pattern. POTENTIAL REDACTION LEAK."
                )

    return redacted_count, leak_findings


# ============================================================================
# Test helper
# ============================================================================


def synthesize_trace(
    events_data: list[dict[str, Any]] | None = None,
    manifest_data: dict[str, Any] | None = None,
) -> CaptureTrace:
    """Build a valid in-memory CaptureTrace from simple dicts.

    FOR TESTS ONLY. This is NOT for production use. It stamps a "synthetic: true"
    marker in warnings so it cannot be mistaken for real data.

    Args:
        events_data: List of dicts representing RawDomEvent fields. Minimal
            required fields will be filled with defaults if not provided.
        manifest_data: Dict representing CaptureManifest fields.

    Returns:
        A CaptureTrace with synthetic data.
    """
    warnings = ["SYNTHETIC TRACE — generated by synthesize_trace(), not from a real recording"]

    # Default minimal event if none provided
    if events_data is None:
        events_data = [
            {
                "v": 1,
                "seq": 1,
                "t": 1737830000123,
                "type": "click",
                "url": "https://test.my.salesforce.com",
                "frame_path": [],
                "selectors": {
                    "test_id": None,
                    "aria": None,
                    "role_name": None,
                    "label_for": None,
                    "sf_field": None,
                    "css_path": "button.test",
                    "text": "Click Me",
                    "xpath": None,
                },
                "element": {
                    "tag": "button",
                    "type": None,
                    "name": None,
                    "id": None,
                    "classes": ["test"],
                    "aria_label": None,
                    "text": "Click Me",
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
            }
        ]

    # Fill minimal defaults for each event
    events = []
    for i, data in enumerate(events_data):
        # Ensure required fields
        data.setdefault("v", 1)
        data.setdefault("seq", i + 1)
        data.setdefault("t", 1737830000000 + i * 1000)
        data.setdefault("type", "click")
        data.setdefault("url", "https://test.my.salesforce.com")
        data.setdefault("frame_path", [])
        data.setdefault(
            "selectors",
            {
                "test_id": None,
                "aria": None,
                "role_name": None,
                "label_for": None,
                "sf_field": None,
                "css_path": None,
                "text": None,
                "xpath": None,
            },
        )
        data.setdefault(
            "element",
            {
                "tag": "div",
                "type": None,
                "name": None,
                "id": None,
                "classes": [],
                "aria_label": None,
                "text": None,
                "is_in_modal": False,
                "modal_label": None,
                "shadow_depth": 0,
            },
        )
        data.setdefault("value", None)
        data.setdefault("value_redacted", False)
        data.setdefault(
            "sf",
            {
                "object": None,
                "record_id": None,
                "page_type": "unknown",
                "app": None,
            },
        )
        events.append(RawDomEvent.model_validate(data))

    manifest = None
    if manifest_data:
        manifest = CaptureManifest.model_validate(manifest_data)

    return CaptureTrace(events=events, warnings=warnings, manifest=manifest)
