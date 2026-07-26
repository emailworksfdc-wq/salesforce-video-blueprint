"""The org deny-list — one normalized matcher, used by every guard.

`PPCDM` and `PPCaccenture` are permanently out of scope for this project, even
read-only. This module is the single place that decides whether an org
identifier names one of them.

WHY THIS MODULE EXISTS (defect L4-4)
------------------------------------

The rule used to be enforced twice, differently, and both copies leaked:

    telemetry.py:197
        _FORBIDDEN_ORG_ALIASES = {"PPCDM", "PPCaccenture", "ppcdm", "ppaccenture"}
                                                                    ^^^^^^^^^^^^
    replay_browser.py:17
        BLOCKED_ORG_ALIASES = {"PPCDM", "PPCaccenture"}
        ... matched with a bare `alias in BLOCKED_ORG_ALIASES` at :126 and :346

The lowercase entry read `ppaccenture` — one `c` — where it meant
`ppcaccenture`. Hand-maintaining a case-folded deny-set is exactly the kind of
thing that rots this way. Measured consequence:

    _is_org_forbidden("ppcaccenture")      -> False   # the real lowercase form
    _is_org_forbidden("PPCACCENTURE")      -> False
    _is_org_forbidden("PpCaccenture")      -> False
    _is_org_forbidden(" PPCaccenture ")    -> False
    "ppcdm" in BLOCKED_ORG_ALIASES         -> False   # replay_browser, any case

So the spelling a shell user is most likely to type reached a hard-blocked org
through both guards. A deny-list that misses its target because of a typo is a
safety defect, not a style one.

DESIGN
------

1. Normalize both sides: casefold, then drop every non-alphanumeric character.
   `PPC-accenture`, `ppc_accenture`, `PPC.Accenture` and `  PPCaccenture  ` all
   become `ppcaccenture`. No hand-maintained case variants to keep in sync.
2. Match by CONTAINMENT of the normalized token, so a derived org
   (`PPCDM.uat`, `ppcdm-clone`), a username (`admin@ppcdm.com`) or an instance
   URL (`https://ppcaccenture.sandbox.my.salesforce.com`) is refused too. That
   closes the bypass surface the defect ledger flagged as "under audit":
   reaching a blocked org without ever naming its alias.
3. Bias toward refusing. Containment means an unrelated alias that happens to
   embed a blocked token (`ppcdmx`) is also refused. Accepted deliberately: a
   false positive costs one refused run and a clear message, a false negative
   touches an org that is permanently out of scope.
4. The `ppaccenture` typo spelling stays blocked. A near-miss of a hard-blocked
   org name is not somewhere this project should go either, and keeping it costs
   nothing.

This module has no imports from the rest of the package, so any guard can use
it without a cycle.
"""

from __future__ import annotations

import re

#: The canonical, human-readable names. Used in error messages and docs.
#: Matching is done on the normalized tokens below, NOT on this set — see
#: `is_org_blocked`.
BLOCKED_ORG_ALIASES: frozenset[str] = frozenset({"PPCDM", "PPCaccenture"})

#: Normalized tokens actually matched against. `ppaccenture` is the original
#: typo spelling, kept deliberately (see module docstring, point 4).
_BLOCKED_TOKENS: frozenset[str] = frozenset({"ppcdm", "ppcaccenture", "ppaccenture"})

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_org_identifier(value: str | None) -> str:
    """Fold an org alias, username or URL to a comparable token.

    Casefolds, then strips every non-alphanumeric character, so case,
    whitespace and punctuation cannot be used to slip past a deny-list.
    Digits are preserved — plenty of legitimate aliases carry them (`AFT3`).

    Returns "" for None or empty input.
    """
    if not value:
        return ""
    return _NON_ALNUM.sub("", str(value).casefold())


def is_org_blocked(value: str | None) -> bool:
    """True if `value` names a permanently out-of-scope org.

    Accepts an alias, a username or an instance URL. Matching is containment of
    a normalized blocked token, so derived orgs and URL/username forms are
    caught as well as bare aliases.

    An empty or absent identifier is NOT blocked — that is a different
    question, and callers fail closed on unknown orgs by a separate path
    (`telemetry._verify_org_is_sandbox`, `replay_browser.resolve_org_info_from_url`).
    Conflating "unknown" with "blocked" here would produce a misleading error.
    """
    normalized = normalize_org_identifier(value)
    if not normalized:
        return False
    return any(token in normalized for token in _BLOCKED_TOKENS)


def blocked_org_message(value: str | None) -> str:
    """A uniform refusal message for a blocked org.

    Keeps the phrase "permanently out of scope" that the existing guard tests
    and docs match on.
    """
    return (
        f"Org '{value}' is permanently out of scope per project rules "
        f"({' / '.join(sorted(BLOCKED_ORG_ALIASES))}). These are hard-blocked by "
        f"name, including case, punctuation, derived-sandbox, username and "
        f"instance-URL variants. No override available."
    )
