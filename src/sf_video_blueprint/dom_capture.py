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
import re
from bisect import bisect_right
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

    selector_confidence and selector_fallback are added by the recorder's
    computeSelectorConfidence / computeSelectorFallback functions (recorder.js).
    They are optional so that captures made before these fields were added
    still parse without errors.
    """

    test_id: str | None = None
    aria: str | None = None
    role_name: RawRoleName | None = None
    label_for: str | None = None
    sf_field: str | None = None
    css_path: str | None = None
    text: str | None = None
    xpath: str | None = None
    # Selector quality scoring (added in recorder.js v1 patch).
    # 1.0 = named role+name, 0.5 = role only or data-id, 0.1 = null/null.
    # None means the capture pre-dates this field; treat as unknown quality.
    selector_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # Best non-null fallback: aria-label > data-id > innerText[:40] > null.
    selector_fallback: str | None = None


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
    # Enrichment fields added by inject.py --process-name
    process_name: str | None = None
    sf_cli_version: str | None = None
    playwright_mcp_version: str | None = None


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

    # ------------------------------------------------------------------
    # Loss accounting (DEFECT L4-7)
    #
    # Loss below the 50% fail-closed threshold was surfaced nowhere: a capture
    # that quietly discarded 40% of its events was stamped as real evidence with
    # no signal at all. These make the number reachable programmatically, so a
    # caller summarising a run cannot fail to render it.
    # ------------------------------------------------------------------

    @property
    def total_lines(self) -> int:
        """Lines that carried content: parsed events plus discarded lines."""
        return len(self.events) + len(self.skipped_lines)

    @property
    def loss_ratio(self) -> float:
        """Fraction of content lines the parser could not use, 0.0–1.0.

        0.0 for an empty trace — no lines means no lost lines. Emptiness is
        reported separately, as a CRITICAL finding.
        """
        total = self.total_lines
        if total == 0:
            return 0.0
        return len(self.skipped_lines) / total

    @property
    def manifest_gap(self) -> int | None:
        """Events the recorder claims it wrote that this parser never received.

        None when there is no manifest: without the recorder's own count the gap
        is UNKNOWABLE, and reporting unknown as 0 is the kind of quiet
        false-negative this module exists to prevent.

        This is a different loss channel from `skipped_lines`. A truncated file
        leaves no bad line behind, so `skipped_lines` is empty and only the
        manifest count reveals the gap. Negative values are clamped to 0 —
        parsing MORE events than the manifest claims is a mismatch, reported by
        `validate_trace`, not a negative loss.
        """
        if self.manifest is None:
            return None
        return max(0, self.manifest.event_count - len(self.events))

    @property
    def has_data_loss(self) -> bool:
        """True when any evidence was lost through either channel."""
        return bool(self.skipped_lines) or bool(self.manifest_gap)


# ============================================================================
# Parsing
# ============================================================================


SUPPORTED_VERSION = 1

#: Line-loss ratio at or above which `validate_trace` emits a `DATA LOSS:`
#: finding, which cli.py, pipeline.py and mcp_server.py all treat as fatal.
#:
#: DO NOT LOWER THIS to make a lossy capture pass, and do not raise it to make
#: one fail. Loss below this threshold is reported as `EVIDENCE INCOMPLETE:`
#: (DEFECT L4-7) — loud, greppable and non-fatal — which is the right place to
#: tune sensitivity. Named rather than inlined so a change to it is visible in a
#: diff.
_FAIL_CLOSED_LOSS_RATIO = 0.5


def find_manifest_path(capture_path: Path) -> Path | None:
    """Locate the manifest that belongs to a capture file.

    Two naming conventions are in use in this repo, so both are tried:

    1. `<capture-stem>.manifest.json` — the `.jsonl` suffix swapped out, e.g.
       `case_triage.dom_capture.jsonl` -> `case_triage.dom_capture.manifest.json`
       (the layout `examples/` ships and `docs/` documents).
    2. `dom_capture.manifest.json` in the same directory — the literal name
       `capture/inject.py` writes.

    Returns the first existing candidate, or None.
    """
    candidates = [
        capture_path.with_suffix(".manifest.json"),
        capture_path.parent / "dom_capture.manifest.json",
    ]
    for candidate in candidates:
        if candidate != capture_path and candidate.is_file():
            return candidate
    return None


def parse_capture_file(
    path: Path,
    *,
    manifest_path: Path | None = None,
    discover_manifest: bool = True,
) -> CaptureTrace:
    """Parse a dom_capture.jsonl file into a validated CaptureTrace.

    NEVER aborts on malformed lines — a truncated final line is EXPECTED when
    a recording is Ctrl-C'd. Records line number + reason in skipped_lines and
    continues.

    Version handling:
    - If v != SUPPORTED_VERSION and v > SUPPORTED_VERSION: loud warning + raise
      (forward-incompatible)
    - If v < SUPPORTED_VERSION: warning + best-effort parse (backward-compatible)

    Manifest handling (DEFECT L4-5): the sibling manifest is loaded and attached.
    It used to return `manifest=None  # loaded separately via load_manifest`, and
    no production caller ever loaded it — cli.py, pipeline.py and mcp_server.py
    all call parse_capture_file then validate_trace — so `validate_trace`'s
    manifest cross-check was structurally dead. That check is the ONLY thing that
    can detect events the recorder wrote but the parser never received: there is
    no bad line to land in skipped_lines, the events are simply absent from the
    file, so only the recorder's own count reveals them.

    Args:
        path: The capture `.jsonl` file.
        manifest_path: Explicit manifest location. Overrides discovery.
        discover_manifest: Set False to skip discovery entirely, for callers
            that manage the manifest themselves.
    """
    events = []
    warnings = []
    skipped_lines = []

    if not path.exists():
        raise FileNotFoundError(f"Capture file not found: {path}")

    # encoding="utf-8-sig", not "utf-8": a recorder running on Windows
    # (PowerShell redirection, .NET StreamWriter, Notepad) prefixes the file
    # with a UTF-8 BOM. Plain "utf-8" hands that BOM to json.loads, which
    # rejects line 1 with "Unexpected UTF-8 BOM (decode using utf-8-sig)" — so
    # the first event, the one that establishes where the recording started,
    # was silently discarded into skipped_lines. "utf-8-sig" strips a BOM when
    # present and is a no-op when absent.
    with path.open("r", encoding="utf-8-sig") as f:
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

    # DEFECT L4-5: actually wire the manifest in. `load_manifest` swallows every
    # exception and returns None, so distinguish "absent" from "present but
    # unloadable" here — silently treating a corrupt manifest as a missing one
    # would hide the loss of the only recorder-side cross-check there is.
    resolved_manifest_path = manifest_path
    if resolved_manifest_path is None and discover_manifest:
        resolved_manifest_path = find_manifest_path(path)

    manifest = None
    if resolved_manifest_path is None:
        if discover_manifest:
            warnings.append(
                "No manifest found beside the capture. The trace is usable but "
                "DEGRADED: without the recorder's own event_count there is no way "
                "to detect events the recorder wrote that this parser never "
                "received (a truncated capture leaves no bad line behind)."
            )
    elif not resolved_manifest_path.is_file():
        warnings.append(
            f"Manifest path '{resolved_manifest_path.name}' does not exist. "
            f"Proceeding without the recorder-side event-count cross-check."
        )
    else:
        manifest = load_manifest(resolved_manifest_path)
        if manifest is None:
            warnings.append(
                f"Manifest '{resolved_manifest_path.name}' exists but could not be "
                f"parsed or validated. Proceeding WITHOUT the recorder-side "
                f"event-count cross-check, so a truncated capture cannot be "
                f"detected. Treat this trace's completeness as unverified."
            )

    return CaptureTrace(
        events=events,
        warnings=warnings,
        skipped_lines=skipped_lines,
        manifest=manifest,
    )


def load_manifest(path: Path) -> CaptureManifest | None:
    """Load dom_capture.manifest.json if present.

    Returns None if the file does not exist — a trace without a manifest is
    degraded but usable.
    """
    if not path.exists():
        return None

    try:
        # utf-8-sig for the same reason as parse_capture_file: a recorder that
        # BOMs the capture BOMs the manifest beside it, and a manifest that
        # silently fails to load takes the event-count cross-check down with it.
        with path.open("r", encoding="utf-8-sig") as f:
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
    driver and cannot be faked by the page.

    A partially-stamped trace is MERGED, not partitioned. The previous
    implementation keyed stamped events `(0, ingest_seq, 0)` and unstamped ones
    `(1, t, seq)`. That leading 0/1 is a partition, not a tiebreak: every
    stamped event sorted before every unstamped event regardless of when either
    actually happened, so a trace where the driver missed a couple of events
    came back as two concatenated blocks. Measured on a five-event trace whose
    true order was A B C D E, it returned A C E B D.

    `capture/recorder.js:44` states the contract that broke: "`t` (Date.now())
    is the global ordering key. Python side uses `t` to merge/sort events from
    multiple frames and across navigations." Merge, not partition.

    How the merge preserves the driver's guarantee:

    - Stamped events keep their relative order EXACTLY as ingest_seq dictates.
      `t` is page-controlled and therefore untrusted; it never reorders two
      stamped events. This is enforced by
      `test_order_events_ingest_seq_still_absolutely_authoritative`.
    - An unstamped event is POSITIONED among the stamped ones using the only
      signal it has, its own `t`, by counting how many stamped events have a
      `t` at or before it. A running max makes that count non-decreasing, so
      clock skew between frames cannot drag an unstamped event backwards past a
      stamped event it was already placed after.
    - Exact ties preserve input order (`sorted` is stable).

    Honest residual: an unstamped event carries only page-controlled `t`, so a
    page with a skewed clock can misplace its own unstamped events. There is no
    trusted signal that would do better — the driver never saw those events.
    The partition misplaced them unconditionally, which is strictly worse.
    """
    # Anchor list: for each stamped event, in ingest_seq order, the highest `t`
    # seen so far. Monotonic by construction, so bisect can search it.
    stamped = sorted(
        (e for e in events if e.ingest_seq is not None),
        key=lambda e: e.ingest_seq,
    )
    anchors: list[int] = []
    running_max = None
    for event in stamped:
        running_max = event.t if running_max is None else max(running_max, event.t)
        anchors.append(running_max)

    # Rank of each stamped event within the stamped subsequence.
    stamped_rank = {id(e): i for i, e in enumerate(stamped)}

    def sort_key(event: RawDomEvent) -> tuple[int, int, int, int]:
        if event.ingest_seq is not None:
            # Slot = its own rank in the stamped subsequence.
            return (stamped_rank[id(event)], 1, 0, 0)
        # An unstamped event slots in after every stamped event whose t is at or
        # before its own; bisect_right on the monotonic anchor list is that
        # count. The 0 in field two places it BEFORE the stamped event holding
        # that same slot, i.e. between stamped[slot - 1] and stamped[slot].
        slot = bisect_right(anchors, event.t)
        return (slot, 0, event.t, event.seq)

    return sorted(events, key=sort_key)


# ============================================================================
# Sensitive-field detection (DEFECT L4-6)
# ============================================================================
#
# The leak detector used to inspect `element.name` alone — one of the eight-plus
# field-identity signals the recorder captures. Measured across twelve signals
# carrying a sensitive identity next to an unredacted value, it caught 1 and
# missed 11, including `element.type == "password"` (the strongest signal that
# exists) and `selectors.sf_field == "Credit_Card_Number__c"` (how a Salesforce
# field announces itself).
#
# RULE FOR EVERYTHING BELOW: report the FACT of a leak, never its content.
# Findings are printed to terminals and written into reports, so a detector that
# echoes the secret has leaked it a second time into a file that outlives the
# capture. Nothing here interpolates `event.value`.


#: Substrings that mark a field as sensitive. Matched against normalized
#: identity signals (see `_sensitive_signal_hits`).
#:
#: REVIEW FINDING R1 — why "card" and "auth" are NOT here as bare substrings.
#: They were, and they made the detector fire on ordinary Lightning markup:
#: `slds-card__body` is on a large fraction of record-detail DOM, so a Case
#: Subject field produced `element.classes~'card'` + `selectors.css_path~'card'`,
#: and `Author__c` / `Authorization_Status__c` / `Scorecard__c` all matched too.
#: Since these findings are non-blocking by design, the cost was not a broken
#: run but alarm fatigue — an operator who scrolls past the leak detector's
#: output scrolls past the real leak with it. For a control whose entire output
#: is an alarm, crying wolf IS the failure mode.
#:
#: The payment-card and credential cases are carried by more specific tokens
#: below. Verified in both directions by
#: `test_genuinely_sensitive_card_and_auth_fields_are_still_caught` (no true
#: positive lost, including `Auth_Token__c` and `cardNumber`) and
#: `test_benign_field_names_embedding_card_or_auth_do_not_false_positive`.
SENSITIVE_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",  # also carries Auth_Token__c / oauth_token / bearer-token fields
    "bearer",
    "api_key",
    "apikey",
    "ssn",
    "social_security",
    "socialsecurity",
    # Payment card: specific enough not to match `slds-card__body` or `Scorecard__c`.
    "card_number",
    "cardnumber",
    "cardnum",
    "cardholder",
    "credit",
    "cvv",
    "cvc",
    "iban",
    "routing_number",
    "sort_code",
    "passport",
    "tax_id",
    "taxid",
    "national_id",
    "credential",
)

#: Patterns short or ambiguous enough that a substring match produces false
#: positives ("pin" in "Shipping", "spinner", "Opt_In"). These require a WORD
#: boundary rather than a bare substring.
_WORD_BOUNDARY_PATTERNS: tuple[str, ...] = ("pin", "otp", "cvv", "cvc", "ssn", "iban")

#: Input types that are sensitive by definition, whatever the field is called.
_SENSITIVE_INPUT_TYPES: frozenset[str] = frozenset({"password"})

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def _normalize_identity_text(value: Any) -> str:
    """Lowercase a field-identity signal for pattern matching."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_normalize_identity_text(v) for v in value)
    return str(value).lower()


def _matches_sensitive_pattern(text: str) -> str | None:
    """Return the pattern that marks `text` sensitive, or None.

    Ambiguous short patterns are matched on word boundaries so that `Shipping`,
    `spinner` and `Opt_In_Preference__c` do not trip on "pin".
    """
    if not text:
        return None
    words = set(_WORD_SPLIT.split(text))
    # Collapse every separator run to a single "_" so a multi-word pattern such
    # as "social_security" matches "Social Security Number", "social-security"
    # and "socialSecurity" alike. Checked alongside the raw text, since patterns
    # like "api_key" must also match "apikey"-style spellings.
    collapsed = "_".join(w for w in _WORD_SPLIT.split(text) if w)
    for pattern in SENSITIVE_PATTERNS:
        if pattern in _WORD_BOUNDARY_PATTERNS:
            continue
        if pattern in text or pattern in collapsed:
            return pattern
    for pattern in _WORD_BOUNDARY_PATTERNS:
        if pattern in words:
            return pattern
    return None


def _identity_signals(event: RawDomEvent) -> list[tuple[str, str]]:
    """Every signal that can reveal what field an event's value belongs to.

    Named `(signal, text)` pairs so a finding can say WHICH signal matched
    without quoting the value.
    """
    element = event.element
    selectors = event.selectors
    role_name = selectors.role_name
    return [
        ("element.name", _normalize_identity_text(element.name)),
        ("element.id", _normalize_identity_text(element.id)),
        ("element.type", _normalize_identity_text(element.type)),
        ("element.aria_label", _normalize_identity_text(element.aria_label)),
        ("element.classes", _normalize_identity_text(element.classes)),
        ("element.text", _normalize_identity_text(element.text)),
        ("element.modal_label", _normalize_identity_text(element.modal_label)),
        ("selectors.sf_field", _normalize_identity_text(selectors.sf_field)),
        ("selectors.test_id", _normalize_identity_text(selectors.test_id)),
        ("selectors.aria", _normalize_identity_text(selectors.aria)),
        ("selectors.label_for", _normalize_identity_text(selectors.label_for)),
        ("selectors.css_path", _normalize_identity_text(selectors.css_path)),
        ("selectors.xpath", _normalize_identity_text(selectors.xpath)),
        (
            "selectors.role_name",
            _normalize_identity_text(role_name.name if role_name else None),
        ),
    ]


#: Signals safe to quote in a finding. A field's NAME is metadata, not secret,
#: and an operator needs it to fix the recorder. Deliberately excluded:
#: css_path, xpath, aria and label_for, which can embed record data or values.
_QUOTABLE_SIGNALS: frozenset[str] = frozenset(
    {"element.name", "element.id", "element.type", "selectors.sf_field", "selectors.test_id"}
)


def _sensitive_signal_hits(event: RawDomEvent) -> list[str]:
    """Names of the identity signals marking this event's field as sensitive.

    Each hit names the signal and the matched pattern. For the handful of
    signals that are pure field metadata (`_QUOTABLE_SIGNALS`) the text itself
    is quoted, because an operator needs to know WHICH field to fix. Free-text
    and path-like signals are never quoted — a css_path or xpath can embed
    record data. The event's `value` is never included by any path.
    """
    hits = []

    # An <input type="password"> is sensitive by definition, whatever it is
    # called. This is the single most reliable signal and was ignored entirely.
    if _normalize_identity_text(event.element.type) in _SENSITIVE_INPUT_TYPES:
        hits.append("element.type=password")

    for signal, text in _identity_signals(event):
        pattern = _matches_sensitive_pattern(text)
        if pattern is None:
            continue
        if signal in _QUOTABLE_SIGNALS:
            hits.append(f"{signal}='{text}'")
        else:
            hits.append(f"{signal}~'{pattern}'")

    return hits


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

    # Redaction leak detection. Patterns and signal extraction live in
    # SENSITIVE_PATTERNS / _sensitive_signal_hits above (DEFECT L4-6).
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

        # Value present without the redaction flag, on a field whose identity
        # looks sensitive. DEFECT L4-6: this used to read `element.name` alone
        # and missed 11 of 12 signals, `type="password"` among them.
        #
        # The finding names the matching SIGNALS and PATTERNS, never the
        # signal's full text and never the value — a css_path or xpath can embed
        # data, and findings are written to reports that outlive the capture.
        if event.value is not None and not event.value_redacted:
            hits = _sensitive_signal_hits(event)
            if hits:
                findings.append(
                    f"SECURITY: Event {i} (seq={event.seq}): value is present but "
                    f"{len(hits)} field-identity signal(s) look sensitive "
                    f"[{', '.join(hits)}]. Redaction may have FAILED."
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
        elif skip_ratio >= _FAIL_CLOSED_LOSS_RATIO:
            # Substantial partial loss
            findings.append(
                f"DATA LOSS: {len(trace.skipped_lines)} of {total_lines} lines were skipped "
                f"({skip_ratio:.0%}). More than half the capture was discarded. Check for "
                f"recorder/parser version drift or schema mismatch."
            )
        elif trace.skipped_lines:
            # DEFECT L4-7: ANY loss below the 50% threshold used to be surfaced
            # nowhere at all. Measured before the fix: 1/10, 2/10, 3/10 and 4/10
            # lines discarded each produced an empty finding list, so a capture
            # missing 40% of its events was stamped as real evidence in silence.
            #
            # A distinct prefix, deliberately NOT "DATA LOSS:": cli.py,
            # pipeline.py and mcp_server.py all abort on that prefix, and the
            # 50% fail-closed threshold is not being lowered here. This is loud
            # and non-fatal.
            findings.append(
                f"EVIDENCE INCOMPLETE: {len(trace.skipped_lines)} of {total_lines} "
                f"lines were discarded ({skip_ratio:.0%} loss). The capture parsed, "
                f"but it is NOT a complete record of the session — any spec derived "
                f"from it is missing evidence. Below the {_FAIL_CLOSED_LOSS_RATIO:.0%} "
                f"threshold that aborts a run, so this is a warning, not a refusal."
            )

    # The other loss channel: events the recorder wrote that never reached the
    # parser. A truncated file leaves no bad line behind, so skipped_lines is
    # empty and the line ratio above reports 0% on a capture missing 40% of its
    # events. Only the manifest count witnesses this.
    gap = trace.manifest_gap
    if gap:
        claimed = trace.manifest.event_count if trace.manifest else 0
        gap_ratio = gap / claimed if claimed else 0.0
        findings.append(
            f"EVIDENCE INCOMPLETE: the recorder reported {claimed} events but only "
            f"{len(trace.events)} reached the parser — {gap} missing "
            f"({gap_ratio:.0%} of the session). Every line present parsed cleanly, so "
            f"this loss is invisible in the skipped-line count: the events are absent "
            f"from the file, not malformed within it. Check for sink errors or a "
            f"truncated write."
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

    # DEFECT L4-6: shares _sensitive_signal_hits with validate_trace rather than
    # keeping a second copy of the pattern list. The two had already diverged in
    # the making — a duplicated security check is a check that will disagree with
    # itself eventually.
    for i, event in enumerate(trace.events):
        if event.value is not None and not event.value_redacted:
            hits = _sensitive_signal_hits(event)
            if hits:
                leak_findings.append(
                    f"Event index {i} (seq={event.seq}, type={event.type}): value "
                    f"present but {len(hits)} field-identity signal(s) match a "
                    f"sensitive pattern [{', '.join(hits)}]. POTENTIAL REDACTION "
                    f"LEAK. (Value deliberately not shown.)"
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
