"""Selector ranking and confidence scoring for DOM capture events.

This module is the bridge between raw capture output (dom_capture.py) and the
replay layer (replay_browser.py). It takes selector dictionaries (either plain
dicts from JSONL or Pydantic RawSelectors models) and emits ranked,
Playwright-ready selector strings with per-selector confidence scores.

Key decisions:
1. Playwright syntax correctness is load-bearing — these strings feed page.locator()
2. Shadow DOM ` >>> ` in CSS paths is translated to descendant space (Playwright
   pierces open shadow roots automatically; literal >>> is invalid syntax)
3. Auto-generated Lightning IDs (input-\d+, combobox-button-\d+) are heavily
   penalized — they change between page loads
4. Escaping is mandatory — unescaped quotes/apostrophes produce selectors that
   silently fail with "element not found" at replay time

INTERFACE CONTRACT COMPLIANCE:
- Section 2.2 tier order: test_id > role+name > aria > label_for > sf_field >
  text > css > xpath
- Section 2.2 confidence mapping: tiers 1-2 -> 0.95, 3-4 -> 0.85, 5 -> 0.8,
  6 -> 0.6, 7-8 -> 0.35
- Never empty — if nothing is derivable, return ONE tier-8 entry with confidence
  0.35 and a rationale explaining unreliability
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


# ============================================================================
# Public API
# ============================================================================


@dataclass(slots=True)
class RankedSelector:
    """A single selector candidate with ranking metadata."""

    selector: str
    tier: int
    confidence: float
    kind: str  # "test_id" | "role_name" | "aria" | "label_for" | "sf_field" | "text" | "css" | "xpath"
    rationale: str


def rank_selectors(
    raw_selectors: Mapping[str, Any] | Any,
    element: Mapping[str, Any] | Any = None,
) -> list[RankedSelector]:
    """Rank selector candidates from raw capture data, best-first.

    Args:
        raw_selectors: Either a plain dict (from JSONL) or a Pydantic RawSelectors
            model. Duck-typed via getattr + dict access fallback.
        element: Optional element metadata (plain dict or Pydantic RawElement).
            Used to detect brittleness patterns (auto-generated IDs, deep nesting).

    Returns:
        List of RankedSelector, best-first, deduplicated. Never empty — if no
        good selector exists, returns a single tier-8 entry with confidence 0.35.

    CONTRACT COMPLIANCE:
        - Tier order per contract 2.2: 1=test_id, 2=role+name, 3=aria, 4=label_for,
          5=sf_field, 6=text, 7=css, 8=xpath
        - Confidence per contract 2.2: tiers 1-2 -> 0.95, 3-4 -> 0.85, 5 -> 0.8,
          6 -> 0.6, 7-8 -> 0.35
        - Never returns an empty list
    """
    candidates: list[RankedSelector] = []

    # Extract values from either dict or model via duck typing
    def _get(obj: Any, key: str) -> Any:
        if obj is None:
            return None
        # Try attribute access (Pydantic model)
        val = getattr(obj, key, None)
        if val is not None:
            return val
        # Fall back to dict access
        if isinstance(obj, Mapping):
            return obj.get(key)
        return None

    test_id = _get(raw_selectors, "test_id")
    aria = _get(raw_selectors, "aria")
    role_name = _get(raw_selectors, "role_name")
    label_for = _get(raw_selectors, "label_for")
    sf_field = _get(raw_selectors, "sf_field")
    css_path = _get(raw_selectors, "css_path")
    text = _get(raw_selectors, "text")
    xpath = _get(raw_selectors, "xpath")

    # Tier 1: data-testid / data-qa (stable contract)
    if test_id:
        candidates.append(
            RankedSelector(
                selector=test_id,
                tier=1,
                confidence=confidence_for_tier(1),
                kind="test_id",
                rationale="Stable test ID — explicit contract between recorder and page",
            )
        )

    # Tier 2: role + accessible name (Playwright's get_by_role)
    if role_name:
        role_sel = _build_role_selector(role_name)
        if role_sel:
            candidates.append(
                RankedSelector(
                    selector=role_sel,
                    tier=2,
                    confidence=confidence_for_tier(2),
                    kind="role_name",
                    rationale="Accessibility role + name — stable, semantic",
                )
            )

    # Tier 3: aria-label exact
    if aria:
        candidates.append(
            RankedSelector(
                selector=aria,
                tier=3,
                confidence=confidence_for_tier(3),
                kind="aria",
                rationale="ARIA label — semantic, moderately stable",
            )
        )

    # Tier 4: <label for> association (form fields)
    if label_for:
        candidates.append(
            RankedSelector(
                selector=label_for,
                tier=4,
                confidence=confidence_for_tier(4),
                kind="label_for",
                rationale="Label-for association — semantic, form-field stable",
            )
        )

    # Tier 5: Salesforce field API name
    if sf_field:
        # Emit multiple fallback patterns — Lightning exposes field API names in
        # several ways depending on component type
        sf_candidates = _build_sf_field_selectors(sf_field)
        for sel in sf_candidates:
            candidates.append(
                RankedSelector(
                    selector=sel,
                    tier=5,
                    confidence=confidence_for_tier(5),
                    kind="sf_field",
                    rationale="Salesforce field API name — stable within Lightning components",
                )
            )

    # Tier 6: Visible text scoped to a stable container
    if text:
        text_sel = _build_text_selector(text)
        candidates.append(
            RankedSelector(
                selector=text_sel,
                tier=6,
                confidence=confidence_for_tier(6),
                kind="text",
                rationale="Visible text — moderately stable, context-dependent",
            )
        )

    # Tier 7: CSS path (brittle — penalize auto-generated patterns)
    if css_path:
        css_sel, css_conf, css_rat = _build_css_selector(css_path, element)
        candidates.append(
            RankedSelector(
                selector=css_sel,
                tier=7,
                confidence=css_conf,
                kind="css",
                rationale=css_rat,
            )
        )

    # Tier 8: XPath (diagnostic only)
    if xpath:
        candidates.append(
            RankedSelector(
                selector=xpath,
                tier=8,
                confidence=confidence_for_tier(8),
                kind="xpath",
                rationale="XPath — brittle, diagnostic only, not primary",
            )
        )

    # Deduplicate by selector string, keeping the best-ranked
    seen: dict[str, RankedSelector] = {}
    for cand in candidates:
        if cand.selector not in seen or cand.tier < seen[cand.selector].tier:
            seen[cand.selector] = cand

    result = sorted(seen.values(), key=lambda c: (c.tier, -c.confidence))

    # Contract: never return empty — if no selector was derivable, emit a
    # tier-8 fallback with a rationale explaining the problem
    if not result:
        result = [
            RankedSelector(
                selector="body",
                tier=8,
                confidence=0.35,
                kind="fallback",
                rationale="No selector could be derived from raw capture — using body as unreliable fallback",
            )
        ]

    return result


def best_selector(
    raw_selectors: Mapping[str, Any] | Any,
    element: Mapping[str, Any] | Any = None,
) -> RankedSelector | None:
    """Return the single best selector, or None if ranking failed."""
    ranked = rank_selectors(raw_selectors, element)
    return ranked[0] if ranked else None


def to_playwright_selectors(ranked: list[RankedSelector]) -> list[str]:
    """Extract plain selector strings in priority order for replay_browser."""
    return [r.selector for r in ranked]


def confidence_for_tier(tier: int) -> float:
    """Map tier to confidence score per contract 2.2.

    Tiers 1-2: 0.95
    Tiers 3-4: 0.85
    Tier 5: 0.8
    Tier 6: 0.6
    Tiers 7-8: 0.35
    """
    if tier <= 2:
        return 0.95
    if tier <= 4:
        return 0.85
    if tier == 5:
        return 0.80
    if tier == 6:
        return 0.60
    # tier 7-8
    return 0.35


def selector_health(ranked: list[RankedSelector]) -> str:
    """Overall health assessment: strong | moderate | weak."""
    if not ranked:
        return "weak"
    best = ranked[0]
    if best.tier <= 2:
        return "strong"
    if best.tier <= 5:
        return "moderate"
    return "weak"


def explain(ranked: list[RankedSelector]) -> str:
    """Human-readable audit output for debugging selector quality."""
    if not ranked:
        return "No selectors available."

    lines = []
    for i, r in enumerate(ranked, start=1):
        lines.append(
            f"{i}. [{r.kind}] tier={r.tier} conf={r.confidence:.2f} — {r.selector}\n"
            f"   Rationale: {r.rationale}"
        )
    return "\n".join(lines)


# ============================================================================
# Selector builders — Playwright-syntax-correct, escaped, shadow-aware
# ============================================================================


def _build_role_selector(role_name: Any) -> str | None:
    """Build Playwright role selector: role=button[name="Save"]

    Playwright's role engine syntax:
    - role=<role_name> for role alone
    - role=<role_name>[name="text"] for role + accessible name
    - name matching is substring + case-insensitive by default
    - Use [name="text"][exact=true] for exact match (we default to exact for stability)

    DECISION: Emit exact-match role selectors to avoid false positives. A "Save"
    button should not match "Save & Close".
    """
    if not role_name:
        return None

    # Duck-type: either a dict-like or a Pydantic model with .role and .name
    if isinstance(role_name, Mapping):
        role = role_name.get("role", "")
        name = role_name.get("name", "")
    else:
        role = getattr(role_name, "role", "")
        name = getattr(role_name, "name", "")

    if not role:
        return None

    # Escape the name value for attribute-value syntax
    name_escaped = _escape_attr_value(name) if name else ""

    if name_escaped:
        # Exact match to avoid substring false positives
        return f'role={role}[name="{name_escaped}"][exact=true]'
    else:
        return f"role={role}"


def _build_sf_field_selectors(sf_field: str) -> list[str]:
    """Build Salesforce field API name selectors.

    Lightning components expose field API names via several attributes:
    - [data-field-api-name='X']
    - lightning-input[data-name='X']
    - [data-name='X']

    Return all patterns — the replay layer will try them in order.
    """
    escaped = _escape_attr_value(sf_field)
    return [
        f"[data-field-api-name='{escaped}']",
        f"lightning-input[data-name='{escaped}']",
        f"[data-name='{escaped}']",
    ]


def _build_text_selector(text: str) -> str:
    """Build Playwright text selector: text="exact text"

    Playwright text engine:
    - text="exact" for exact match
    - text=substring for substring match

    DECISION: Use exact match by default for stability. Substring matching can
    produce false positives (e.g. "Save" matching "Save & Close").
    """
    escaped = _escape_text(text)
    return f'text="{escaped}"'


def _build_css_selector(
    css_path: str, element: Mapping[str, Any] | Any = None
) -> tuple[str, float, str]:
    """Build CSS selector with brittleness penalties.

    Shadow DOM handling:
    The raw css_path may contain ` >>> ` between shadow boundaries. Playwright
    pierces open shadow roots automatically with CSS selectors, so ` >>> ` must
    be TRANSLATED (drop the boundary marker, join with descendant space) rather
    than passed through literally (which would be invalid syntax).

    Brittleness penalties (lower confidence within tier 7):
    - >4 descendant levels: -0.05
    - Any nth-child: -0.05
    - Auto-generated class patterns (slds-/lwc-/uiBlock combined, hashed tokens): -0.05
    - Auto-generated Lightning ID patterns (input-\d+, combobox-button-\d+): -0.10

    Returns:
        (selector, confidence, rationale)
    """
    # Translate shadow DOM boundaries: " >>> " -> " "
    selector = css_path.replace(" >>> ", " ")

    base_conf = confidence_for_tier(7)
    penalties: list[str] = []

    # Descendant depth
    depth = selector.count(" ")
    if depth > 4:
        base_conf -= 0.05
        penalties.append(f"deep nesting (depth={depth})")

    # nth-child (positional, brittle)
    if "nth-child" in selector or "nth-of-type" in selector:
        base_conf -= 0.05
        penalties.append("positional selector (nth-child)")

    # Auto-generated classes (Lightning/LWC)
    if re.search(r"\b(slds-[a-z_-]+|lwc-[a-z0-9_-]+|uiBlock[A-Za-z]+)\b", selector):
        base_conf -= 0.05
        penalties.append("auto-generated class names")

    # Hashed-looking tokens (e.g. foo-abc123)
    if re.search(r"[a-z]+-[0-9a-f]{6,}", selector):
        base_conf -= 0.05
        penalties.append("hashed class/id pattern")

    # Auto-generated Lightning IDs (input-123, combobox-button-456)
    if element:
        elem_id = None
        if isinstance(element, Mapping):
            elem_id = element.get("id")
        else:
            elem_id = getattr(element, "id", None)

        if elem_id and re.match(r"(input|combobox-button|textarea|select)-\d+$", elem_id):
            base_conf -= 0.10
            penalties.append(f"auto-generated Lightning ID: {elem_id}")

    rationale = "CSS path — brittle"
    if penalties:
        rationale += f" (penalties: {', '.join(penalties)})"

    # Clamp confidence to [0.0, 1.0]
    base_conf = max(0.0, min(1.0, base_conf))

    return selector, base_conf, rationale


# ============================================================================
# Escaping utilities — CORRECTNESS CRITICAL
# ============================================================================


def _escape_attr_value(value: str) -> str:
    """Escape a value for use inside a Playwright attribute selector.

    Playwright attribute selectors use quotes, so we need to escape:
    - Double quotes -> \"
    - Backslashes -> \\

    CRITICAL: Unescaped quotes produce invalid selectors that silently fail at
    replay time with "element not found" — the worst kind of bug.

    Test cases:
    - "Save" -> Save (no escaping needed inside double quotes when using single quotes for the attribute)
    - "Don't Save" -> Don\'t Save (if using single quotes for attribute)
    - 'He said "hi"' -> He said \"hi\" (if using double quotes for attribute)

    DECISION: We use single quotes for attribute values, so escape single quotes
    and backslashes.
    """
    # Escape backslashes first (to avoid double-escaping)
    value = value.replace("\\", "\\\\")
    # Escape single quotes (our attribute delimiter)
    value = value.replace("'", "\\'")
    return value


def _escape_text(value: str) -> str:
    """Escape a value for use inside a Playwright text selector.

    Playwright text selectors use double quotes for exact match, so:
    - Double quotes -> \"
    - Backslashes -> \\

    Test cases:
    - "Save" -> Save
    - "Don't Save" -> Don't Save (apostrophe is fine inside double quotes)
    - 'He said "hi"' -> He said \"hi\"
    """
    # Escape backslashes first
    value = value.replace("\\", "\\\\")
    # Escape double quotes (our text delimiter)
    value = value.replace('"', '\\"')
    return value
