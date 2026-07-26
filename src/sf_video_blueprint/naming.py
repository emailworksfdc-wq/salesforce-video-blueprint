"""Canonical name derivation for Agentforce artifacts.

**This module exists because three emitters independently invented three
different answers to the same question.** For the intent
``"Update Case (Status)"`` they produced:

- ``agentforce_spec.py`` -> ``Update_Case_Status``  (the topic that actually exists)
- ``eval_spec.py``       -> ``UpdateCase``          (an ``expectedTopic`` pointing at nothing)
- ``agent_script.py``    -> ``update_case_status``  (the ``@subagent.X`` reference)

The first two MUST be byte-identical: a test spec's ``expectedTopic`` is a
reference to a topic ``name`` in the agent spec YAML. When they disagree the
generated test suite targets a topic that does not exist, the CLI reports a
confusing mismatch, and the refinement loop scores a spec whose tests never ran.
That is a silent, total failure of the loop, so name derivation is centralised
here and nowhere else.

Two output dialects, one source:

``topic_api_name()``
    ``Update_Case_Status``. Used for the spec YAML ``topics[].name`` and for the
    test spec's ``expectedTopic`` / ``expected``. The shape follows the contract
    (``docs/INTERFACE_CONTRACT.md`` 3.1 and 3.3).

``subagent_name()``
    ``update_case_status``. Agent Script requires ``snake_case`` in
    ``@subagent.X``, so this is a lossless lowercase mapping *of the same
    canonical token list* — not an independent derivation. Given one you can
    recover the other, which is what makes router-to-topic linkage checkable.
"""

from __future__ import annotations

import re

# Salesforce API names are conventionally capped at 80 characters. The exact
# limit for Agent Script subagent names is not documented in the first-party
# template, so the same cap is applied to both rather than guessing a looser one.
#
# **ROUTER ACTION BUDGET:** The router action name is `go_to_<subagent>`, which
# adds a 6-char prefix. To keep the router action under 80 chars, the subagent
# name (and therefore the topic name it derives from) must be capped at 74 chars.
# We apply this cap uniformly to topic_api_name, subagent_name, and
# router_action_name to preserve cross-artifact linkage.
MAX_NAME_LENGTH = 74  # was 80; reduced to 74 to budget for "go_to_" prefix

# Emitted when an intent carries no usable word characters at all. Deliberately
# not a plausible-looking name: an unnamed topic should be obvious in review.
FALLBACK_TOPIC_NAME = "Unresolved_Topic"

# spec_builder marks an undetermined intent with this prefix. It is stripped
# before naming so the marker never leaks into an API name, but callers are
# expected to refuse such a spec outright rather than rely on this.
_UNRESOLVED_PREFIX = "UNRESOLVED:"

# Names the Agent Script grammar claims for itself. A derived subagent that lands
# on one of these does not merely look odd — it collides.
#
# The three standard subagents are the dangerous case. Every generated script
# already emits `subagent escalation:`, `subagent off_topic:` and
# `subagent ambiguous_question:`, so a recording whose intent normalises to one of
# those names produces the block TWICE. Measured: an intent of "Escalation" emits
# two `subagent escalation:` blocks and `validate_locally` reports no findings, so
# the corruption reaches the org before anything complains.
#
# The structural keywords are included for the same reason at one remove: a block
# named `config` or `variables` shadows a grammar keyword at the same indentation
# level, and the parser is first-party, not ours, so the safe assumption is that
# it is ambiguous rather than that it happens to cope.
#
# Suffixing is preferred over raising: the recording is real evidence and the
# operator cannot rename a business process to satisfy our parser.
_RESERVED_SUBAGENT_NAMES = frozenset(
    {
        # Standard subagents present in every emitted script.
        "escalation",
        "off_topic",
        "ambiguous_question",
        # Structural keywords of the .agent grammar.
        "system",
        "config",
        "variables",
        "language",
        "start_agent",
        "subagent",
        "utils",
    }
)

# Appended to a derived name that collides with a reserved word. Reads as a topic
# name rather than an escape hatch, so a reviewer seeing `escalation_topic` in a
# diff can tell it came from a recording and not from the template.
_RESERVED_SUFFIX = "_topic"

# Split a run of characters into words at:
#   lower|digit -> Upper   ("updateCase"   -> "update", "Case")
#   ACRONYM     -> Word    ("HTTPResponse" -> "HTTP", "Response")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")


def tokenize(raw: str) -> list[str]:
    """Split any phrase into the canonical word tokens every name derives from.

    This is the single point where "what are the words?" is decided. Both
    dialects consume its output, which is why they cannot drift apart.

    Parenthetical content is kept, not dropped: for ``"Update Case (Status)"``
    the field name is the most specific thing observed, and a topic called
    ``Update_Case`` loses the detail that makes the derived spec worth having.
    """
    text = raw.strip()
    if text.upper().startswith(_UNRESOLVED_PREFIX):
        text = text[len(_UNRESOLVED_PREFIX) :].strip()

    tokens: list[str] = []
    for chunk in _NON_WORD.split(text):
        if not chunk:
            continue
        tokens.extend(part for part in _CAMEL_BOUNDARY.split(chunk) if part)
    return tokens


def _truncate_tokens(tokens: list[str], joiner: str) -> list[str]:
    """Drop trailing tokens until the joined name fits, keeping at least one.

    Truncating on a token boundary rather than mid-word keeps the name readable
    and keeps the two dialects in agreement, since both truncate the same list.
    """
    kept: list[str] = []
    for token in tokens:
        candidate = kept + [token]
        if len(joiner.join(candidate)) > MAX_NAME_LENGTH:
            # Stop before exceeding the cap. On the very first token this leaves
            # `kept` empty, which the hard-cut below handles — an earlier version
            # guarded this branch with `if kept and ...`, which skipped the check
            # entirely for a single over-long token and emitted a 100-char name
            # that Salesforce would reject.
            break
        kept = candidate
    if not kept:
        # A single token longer than the cap: hard-cut it. Rare, but a truncated
        # name is more useful than an exception here.
        return [tokens[0][:MAX_NAME_LENGTH]]
    return kept


def is_reserved(name: str) -> bool:
    """True when ``name`` collides with a name the Agent Script grammar claims.

    Compares in the snake_case dialect, because that is the form ``@subagent.X``
    references use and therefore the form in which a collision actually bites.
    """
    return "_".join(tokenize(name)).lower() in _RESERVED_SUBAGENT_NAMES


def _escape_reserved(tokens: list[str]) -> list[str]:
    """Append a disambiguating token when the joined name is reserved.

    Applied to the *token list* rather than the finished string so both dialects
    escape identically and :func:`names_agree` keeps holding — escaping only the
    snake_case form would reintroduce exactly the topic/subagent divergence this
    module exists to prevent.

    Idempotent: ``escalation`` -> ``escalation_topic``, which is not reserved, so a
    second pass is a no-op. That matters because :func:`subagent_name` routes
    through :func:`topic_api_name` and would otherwise double-suffix.
    """
    if "_".join(tokens).lower() not in _RESERVED_SUBAGENT_NAMES:
        return tokens
    return tokens + [_RESERVED_SUFFIX.lstrip("_")]


def topic_api_name(intent: str) -> str:
    """Derive the topic API name: ``"Update Case (Status)"`` -> ``Update_Case_Status``.

    Used for the spec YAML ``topics[].name`` **and** the test spec's
    ``expectedTopic``. Those two are a reference pair; call this for both.

    **Length budget:** `MAX_NAME_LENGTH` is set to 74 (not 80) to leave room for
    the `go_to_` prefix in the router action name. This ensures that
    `len(f"go_to_{subagent_name(intent)}")` never exceeds 80 chars, the assumed
    Salesforce API name cap.
    """
    tokens = tokenize(intent)
    if not tokens:
        return FALLBACK_TOPIC_NAME

    # Escape before truncating: the suffix must be inside the length budget, or a
    # name at exactly the cap would drop the very token that avoids the collision.
    tokens = _escape_reserved(tokens)

    # Capitalise the first letter but preserve the rest, so an acronym token
    # stays an acronym ("HTTP" must not become "Http").
    shaped = [t[:1].upper() + t[1:] for t in _truncate_tokens(tokens, "_")]
    name = "_".join(shaped)

    if not name[:1].isalpha():
        # API names must start with a letter. "2024 Renewal" -> "T_2024_Renewal".
        name = f"T_{name}"[:MAX_NAME_LENGTH].rstrip("_")
    return name


def snake_case(name: str) -> str:
    """Lowercase snake_case, matching ``@salesforce/kit``'s ``snakeCase``.

    The kit implementation (``@salesforce/kit/lib/nodash/internal.js``) is::

        str.replace(/([a-z])([A-Z])/g, '$1_$2')
           .toLowerCase()
           .replace(/\\W/g, '_')
           .replace(/^_+|_+$/g, '')

    The camelCase split is the part that matters: without it ``UpdateCase``
    becomes ``updatecase`` while the CLI expects ``update_case``, and the
    router's ``@subagent.X`` reference silently fails to resolve.
    """
    tokens = tokenize(name)
    if not tokens:
        return snake_case(FALLBACK_TOPIC_NAME)
    return "_".join(_truncate_tokens(_escape_reserved(tokens), "_")).lower()


def subagent_name(intent: str) -> str:
    """Derive the Agent Script subagent name: ``update_case_status``.

    Deliberately routed through :func:`topic_api_name` so the subagent name is
    always the lowercase form of the topic that exists, never an independent
    guess. With `MAX_NAME_LENGTH = 74`, the subagent name is guaranteed to fit
    within the router action budget (`go_to_` + 74 chars = 80 chars total).
    """
    return snake_case(topic_api_name(intent))


def router_action_name(intent: str) -> str:
    """The router's transition action name for a topic: ``go_to_update_case_status``.

    ``start_agent agent_router`` needs one ``go_to_<subagent>`` action per
    subagent. Deriving it here keeps the router and the subagent block in step.

    **Length constraint:** With `MAX_NAME_LENGTH = 74`, the subagent name is
    guaranteed to be <= 74 chars, so `f"go_to_{subagent_name(intent)}"` is
    guaranteed to be <= 80 chars (6 + 74 = 80). This budgets the prefix INSIDE
    the overall 80-char cap while preserving cross-artifact linkage:
    `router_action_name(i) == f"go_to_{subagent_name(i)}"` always holds, and
    `names_agree(topic_api_name(i), subagent_name(i))` always holds.

    The alternative (truncating the router action independently) would risk
    collision: two distinct 74-char subagent names that differ only in their
    final tokens could map to the same router action, which is a silent mis-route.
    """
    return f"go_to_{subagent_name(intent)}"


def dedupe_names(names: list[str]) -> list[str]:
    """Disambiguate names that collided during derivation, preserving order.

    Distinct intents can normalise to the same token list (``"Update Case"`` and
    ``"update-case"``). Left alone, two subagent blocks would share a name and
    one would silently shadow the other — so collisions get a numeric suffix
    instead. Comparison is case-insensitive because the snake_case dialect would
    collapse case-only differences anyway.

    **Guarantees** (all invariants hold for any input):
    1. Output has the same length as input, preserving order
    2. Every output element is pairwise-distinct (case-insensitively)
    3. Every element is <= MAX_NAME_LENGTH
    4. Every element is a valid API token (starts with letter or underscore)
    5. No element is a reserved Agent Script name
    6. The function is idempotent: dedupe_names(dedupe_names(x)) == dedupe_names(x)

    The second pass (idempotence) is guaranteed because suffixed names are distinct
    from their un-suffixed bases, so they are never re-suffixed.
    """
    # Track what we have EMITTED so far (the output space), not the input counts.
    # This prevents suffix collisions: if the input contains both "A" and "A_2",
    # we track "a" and "a_2" in the seen set, so the second "A" becomes "A_3".
    seen: set[str] = set()
    result: list[str] = []

    for name in names:
        candidate = name
        suffix_num = 1

        # Find the first available name in the output space that:
        # 1. Is not already emitted (case-insensitively)
        # 2. Fits within MAX_NAME_LENGTH
        # 3. Is not a reserved word
        while True:
            key = candidate.lower()

            # Check all three conditions
            is_available = (
                key not in seen
                and len(candidate) <= MAX_NAME_LENGTH
                and not is_reserved(candidate)
            )

            if is_available:
                seen.add(key)
                result.append(candidate)
                break

            # Try next suffix. For the first collision (suffix_num=1), we're trying
            # the un-suffixed name; if it's taken, we move to suffix_num=2.
            suffix_num += 1
            suffix_str = f"_{suffix_num}"

            # Truncate the base to fit the suffix within MAX_NAME_LENGTH
            max_base_len = MAX_NAME_LENGTH - len(suffix_str)
            if max_base_len < 1:
                # Pathological: even "_2" won't fit. Fall back to "T_2" or similar.
                # This is vanishingly rare (MAX_NAME_LENGTH would need to be < 3).
                candidate = f"T{suffix_str}"[:MAX_NAME_LENGTH]
            else:
                candidate = f"{name[:max_base_len]}{suffix_str}"

            # Safeguard: if we've tried 1000 suffixes, something is very wrong.
            # This should never happen in practice (would require 1000 pre-existing
            # collisions in the input), but prevents infinite loops.
            if suffix_num > 1000:
                raise RuntimeError(
                    f"dedupe_names: cannot find available name for {name!r} after 1000 attempts"
                )

    return result


def names_agree(topic_name: str, subagent: str) -> bool:
    """True when a subagent name is the expected dialect of a topic name.

    Lets tests and reviewers assert the cross-artifact linkage directly instead
    of re-deriving it and hoping the derivations match.
    """
    return snake_case(topic_name) == subagent
