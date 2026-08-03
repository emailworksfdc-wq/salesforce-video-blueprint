"""Placeholder markers — the single list of strings that mean "this is fake".

Two scorers scan output for stub content: ``scripts/score_run.py`` (run-level
gate) and ``spec_score.py`` (offline spec score, which drives the refinement
loop). They had drifted to different lists, so a spec containing ``TODO`` failed
one and passed the other. A gate that disagrees with itself is not a gate, so
both now import from here.

**On removing ``button:Save``.** It used to be on this list as a proxy for the
stub extractor, which emitted exactly one hardcoded ``button:Save`` step for any
input. But a real DOM capture of an operator clicking Save also produces
``target="button:Save"`` — legitimately, as observed evidence. Leaving the
string on the list would have blocked every genuine run the moment Step 5 began
working, and the obvious "fix" under deadline pressure is to delete the marker,
which silently re-opens the hole it was guarding.

So the proxy is replaced with things only the stub can produce (see
``STUB_FINGERPRINTS``) plus a structural check on
``provenance.extraction_source``, which is what the marker was always standing in
for. That is strictly stronger: the stub is now caught by identity rather than by
a string it happens to share with real data.
"""

from __future__ import annotations

# Strings that indicate stub, sample, or fabricated content rather than observed
# org data. A hit on any of these is blocking, not advisory.
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    # Mock telemetry fixtures (cli.py MockTelemetryCollector)
    "Sample_Flow",
    "500xx0000012345AAA",
    # The stub extractor's hardcoded derived intent
    "Update case status from UI workflow",
    # spec_builder's explicit refusal to guess
    "UNRESOLVED:",
    # Unfinished authoring, in any artifact
    "TODO",
    "FIXME",
    "Lorem ipsum",
    # Emitted by the Step 6 builders under allow_incomplete=True. An incomplete
    # spec must not be able to score well.
    "[NEEDS EVIDENCE",
)

# Strings unique to HeuristicVideoExtractor. Unlike ``button:Save``, none of
# these can arise from a real recording, so they identify the stub itself rather
# than guessing from its output shape.
STUB_FINGERPRINTS: tuple[str, ...] = (
    "Heuristic extraction in use",
    "Baseline extraction. Replace with CV pipeline",
    "Commit the current form",
)

# ``DataProvenance.extraction_source`` values that mean real extraction ran.
# Kept in sync with ``html_report.DataProvenance._REAL_EXTRACTION_SOURCES``.
# Anything else — including an unrecognised new value — is treated as simulated,
# so a typo fails closed instead of silently passing.
REAL_EXTRACTION_SOURCES: frozenset[str] = frozenset({"dom-capture", "cv"})

REAL_TELEMETRY_SOURCES: frozenset[str] = frozenset({"live-org"})


def scan_text(text: str) -> list[str]:
    """Return every placeholder or stub marker present in ``text``.

    Fails closed: if text is not a string, returns empty list rather than crashing.
    A malformed input is not evidence of placeholder content.
    """
    if not isinstance(text, str):
        return []
    found = [m for m in PLACEHOLDER_MARKERS if m in text]
    found.extend(m for m in STUB_FINGERPRINTS if m in text)
    return found


def extraction_is_real(source: str | None) -> bool:
    """True only for a known-real extraction source. Unknown values fail closed.

    Fails closed: if source is not a string (e.g., list, dict), returns False rather
    than raising TypeError. A malformed provenance record is not evidence of real extraction.
    """
    if not isinstance(source, str):
        return False
    return source in REAL_EXTRACTION_SOURCES


def telemetry_is_real(source: str | None) -> bool:
    """True only for a known-real telemetry source. Unknown values fail closed.

    Fails closed: if source is not a string (e.g., list, dict), returns False rather
    than raising TypeError. A malformed provenance record is not evidence of real telemetry.
    """
    if not isinstance(source, str):
        return False
    return source in REAL_TELEMETRY_SOURCES


def scan_spec(spec_dict: dict) -> list[str]:
    """Scan a DerivedAgentSpec (as dict) for placeholder/stub markers.

    This is the single entry point for scanning specs, so scope cannot diverge
    between spec_score.py and scripts/score_run.py.

    D7 FIX: previously this walked a WHITELIST of keys (intent, orchestration_steps,
    guardrails, failure_handling, unknowns, entities[].name, entities[].evidence[].detail,
    evidence[].detail). Any placeholder in a field NOT on that list — for example
    evidence[].source, entities[].object_api_name, or any new field the builder adds
    later — was missed here while scripts/score_run.py's raw-text JSON scan caught it.
    The two gates then disagreed on the SAME artifact: in-process could score 100/100
    while CI blocked. The whitelist was the divergence, so it is gone.

    This function now walks the dict RECURSIVELY and collects every string it finds,
    at any depth, in any key. That matches the scope of scripts/score_run.py's raw-JSON
    scan by construction: if a marker is inside the serialized JSON, it will be inside
    some string value here. New fields the builder adds are covered automatically —
    the two gates cannot silently diverge again.

    Fails closed: if spec_dict is malformed (None, non-dict, or contains cycles),
    returns whatever markers were collected before the failure. A malformed spec is
    not evidence of placeholder content; it will fail scoring elsewhere.

    Args:
        spec_dict: A DerivedAgentSpec.to_dict() result, or the parsed JSON form.

    Returns:
        List of marker strings found (may contain duplicates if a marker appears
        multiple times).
    """
    if not isinstance(spec_dict, dict):
        return []

    text_parts: list[str] = []
    seen_ids: set[int] = set()

    def walk(value: object) -> None:
        # Guard against pathological recursion (cycles or excessive nesting).
        # We track ids of containers we've already visited; primitives don't need
        # tracking because they can't cycle.
        if isinstance(value, str):
            text_parts.append(value)
            return
        if isinstance(value, dict):
            if id(value) in seen_ids:
                return
            seen_ids.add(id(value))
            for k, v in value.items():
                # Keys can carry markers too (e.g. a dict keyed by "TODO: ...").
                if isinstance(k, str):
                    text_parts.append(k)
                walk(v)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            if id(value) in seen_ids:
                return
            seen_ids.add(id(value))
            for item in value:
                walk(item)
            return
        # Non-string primitives (int, float, bool, None) cannot contain markers.

    walk(spec_dict)

    combined = " ".join(text_parts)
    return scan_text(combined)
