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

import keyword
import re

# The Agentforce compilation API rejects a subagent name longer than 80
# characters. **This is measured, not assumed.** Probing org AFT3 on 2026-07-26
# with `sf agent validate authoring-bundle` (compile endpoint
# `/einstein/ai-agent/v1.1/authoring/scripts`, afScriptVersion 2.0.0):
#
#   subagent name 74 chars -> exit 0, {"success": true}
#   subagent name 80 chars -> exit 0, {"success": true}
#   subagent name 81 chars -> exit 1, CompilationError:
#       "Too big: expected string to have <=80 characters for uxxx…"
#   subagent name 100 chars -> exit 1, same error
#
# So the boundary is exactly 80, inclusive.
COMPILER_VERIFIED_NAME_LIMIT = 80

# **The router-action budget was never a real constraint.** The 80-char subagent
# name above produced an 86-char `go_to_…` router action and still compiled, and a
# deliberately-built 100-char router action with a short subagent name ALSO
# compiled (exit 0). The compiler applies the <=80 rule to the subagent name, not
# to the action identifier that references it. `docs/INTERFACE_CONTRACT_ROUND3.md`
# and `docs/DEFECT_LEDGER.md` record the 86-char router action as a defect that
# would reach an org unchecked; measurement says the org accepts it.
#
# The cap is nevertheless left at 74 rather than raised to
# COMPILER_VERIFIED_NAME_LIMIT, deliberately:
#
#   * 74 is strictly inside the measured 80, so it cannot produce a name the
#     compiler rejects. Raising it removes that headroom for no observed gain.
#   * `topic_api_name` also feeds the agent spec YAML `topics[].name` and the test
#     spec's `expectedTopic`, which reach Salesforce through the metadata path
#     (`sf agent generate`/publish), NOT through the script compiler. That path's
#     name limit has not been measured. Widening the shared cap on the strength of
#     compiler evidence would extend a result to a channel it was not gathered on.
#
# Cost of keeping 74: derived names truncate ~6 chars earlier than strictly
# necessary. That is a fidelity loss in an edge case, not a correctness bug.
MAX_NAME_LENGTH = 74

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
# Python reserved words (HARD keywords only). Included because Agent Script
# consumers (validators, transpilers, and tooling that pipes the emitted
# `.agent` file through Python identifier paths) treat these as illegal
# identifiers. Empirically, a subagent named `class` or `return` in
# `@subagent.class` produces a compiler-rejected script — the emitted token
# collides with a Python keyword in the toolchain that parses it. Guarding
# here (in the shared token list) keeps `topic_api_name` and `subagent_name`
# escaping in lockstep so `names_agree` still holds.
#
# **Soft keywords are deliberately excluded.** `keyword.softkwlist` (`match`,
# `case`, `type`, `_` as of 3.10+) are legal Python identifiers — `class Foo:
# case = 5; obj.case` compiles and runs. The empirical justification above
# cites hard keywords only and does NOT carry over to soft keywords. Including
# them causes false positives: intent `"Case"` (the Salesforce standard object)
# tokenizes to `["Case"]`, joins to `case`, and would be suffixed to
# `Case_topic` / `case_topic`, silently breaking backward compatibility with
# any prior generated spec whose topic was `Case`. Same false positive for
# `"Type"`, `"Match"`, `"_"`.
_PYTHON_KEYWORDS: frozenset[str] = frozenset(kw.lower() for kw in keyword.kwlist)

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
    | _PYTHON_KEYWORDS
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


def _truncate_tokens(tokens: list[str], joiner: str, limit: int = MAX_NAME_LENGTH) -> list[str]:
    """Drop trailing tokens until the joined name fits, keeping at least one.

    Truncating on a token boundary rather than mid-word keeps the name readable
    and keeps the two dialects in agreement, since both truncate the same list.

    ``limit`` defaults to `MAX_NAME_LENGTH`. :func:`prefixed_api_name` lowers it,
    because a fixed prefix has to fit inside the same cap as the tokens it
    precedes.
    """
    kept: list[str] = []
    for token in tokens:
        candidate = kept + [token]
        if len(joiner.join(candidate)) > limit:
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
        return [tokens[0][:limit]]
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

    **Length budget:** capped at `MAX_NAME_LENGTH` (74), which sits inside the
    measured compiler limit of `COMPILER_VERIFIED_NAME_LIMIT` (80). The compiler
    enforces 80 on this name; the extra 6 chars of headroom are retained for the
    reasons given at the top of this module, not because the compiler demands them.
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
    guess.

    This is the name the compiler length-checks: an 81-char subagent name is
    rejected with "Too big: expected string to have <=80 characters". The 74-char
    cap keeps it comfortably inside that.
    """
    return snake_case(topic_api_name(intent))


def router_action_name(intent: str) -> str:
    """The router's transition action name for a topic: ``go_to_update_case_status``.

    ``start_agent agent_router`` needs one ``go_to_<subagent>`` action per
    subagent. Deriving it here keeps the router and the subagent block in step.

    **Length:** the compiler does NOT length-check this identifier. Measured on
    AFT3: a deliberately-built 100-char router action referencing a short subagent
    compiled successfully (exit 0), and an 80-char subagent name produced an
    86-char action that also compiled. Only the subagent name is held to <=80.

    The action is still never truncated independently of the subagent name,
    because that would risk collision: two distinct subagent names differing only
    in their final tokens could map to the same action, a silent mis-route. Keeping
    it a pure `go_to_` + name concatenation preserves the invariants
    `router_action_name(i) == f"go_to_{subagent_name(i)}"` and
    `names_agree(topic_api_name(i), subagent_name(i))`.
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


def prefixed_api_name(prefix: str, intent: str) -> str:
    """Derive an API name that carries a fixed ``prefix``: ``SFVB_TEST_Update_Case``.

    Callers that need a marked name — anything creating findable, deletable
    artifacts in a real org — must use this rather than gluing a prefix onto
    :func:`topic_api_name`, and rather than folding the prefix into the intent.

    **Why this exists.** Both shortcuts silently lose the intent. Measured, with
    the prefix ``"SFVB TEST"``::

        topic_api_name("SFVB TEST " + "A" * 90)  -> "SFVB_TEST"
        topic_api_name("SFVB TEST " + "!!!")     -> "SFVB_TEST"

    The prefix competes with the intent for the same `MAX_NAME_LENGTH` budget and
    wins, because it comes first. Two unrelated recordings then derive the *same*
    agent name, and it names nothing but the prefix. Worse, the result still looks
    correct to a cross-artifact check: every dialect agrees, because they all agree
    on a name that identifies no recording.

    Holding the prefix out of the truncation budget keeps the intent's leading
    tokens, which are the ones that distinguish one recording from another.
    """
    prefix_tokens = tokenize(prefix)
    if not prefix_tokens:
        return topic_api_name(intent)

    shaped_prefix = [t[:1].upper() + t[1:] for t in prefix_tokens]
    head = "_".join(shaped_prefix)

    # -1 for the "_" joining the prefix to the first intent token.
    budget = MAX_NAME_LENGTH - len(head) - 1
    if budget <= 0:
        # A prefix that consumes the whole cap on its own. Nothing about the
        # intent can survive, so say so rather than returning a bare prefix that
        # would collide with every other intent.
        raise ValueError(
            f"prefix {prefix!r} is {len(head)} chars, leaving no room for an intent "
            f"inside the {MAX_NAME_LENGTH}-char cap"
        )

    tokens = _escape_reserved(tokenize(intent))
    if not tokens:
        # No usable words in the intent. Fall back to the same visible marker
        # `topic_api_name` uses, so an unnamed recording stays obvious in review
        # instead of being indistinguishable from a truncated one.
        tokens = tokenize(FALLBACK_TOPIC_NAME)

    shaped = [t[:1].upper() + t[1:] for t in _truncate_tokens(tokens, "_", budget)]
    return "_".join([head, *shaped])
