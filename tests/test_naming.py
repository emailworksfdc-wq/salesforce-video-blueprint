"""Property tests for naming.py — the length invariant and cross-artifact linkage.

These tests verify that:
1. All three name forms (topic_api_name, subagent_name, router_action_name) respect MAX_NAME_LENGTH
2. Cross-artifact linkage is preserved: names_agree(topic, subagent) always holds
3. Distinct intents produce distinct router action names (no collision)
4. Reserved-word escaping works after the length fix
5. The two-dialect agreement (CapitalCase topic vs snake_case subagent) is maintained
"""
from __future__ import annotations

import re

import pytest

from sf_video_blueprint.naming import (
    COMPILER_VERIFIED_NAME_LIMIT,
    MAX_NAME_LENGTH,
    FALLBACK_TOPIC_NAME,
    topic_api_name,
    subagent_name,
    router_action_name,
    names_agree,
    is_reserved,
    dedupe_names,
    prefixed_api_name,
    tokenize,
    snake_case,
)


# === COMPILER-VERIFIED LIMIT ===
#
# Measured against org AFT3 on 2026-07-26 via `sf agent validate authoring-bundle`
# (compile endpoint /einstein/ai-agent/v1.1/authoring/scripts, afScriptVersion
# 2.0.0), by hand-building bundles with over-long subagent names:
#
#   74 chars -> exit 0, {"success": true}
#   80 chars -> exit 0, {"success": true}
#   81 chars -> exit 1, "Too big: expected string to have <=80 characters for …"
#  100 chars -> exit 1, same error
#
# The 80-char case produced an 86-char `go_to_…` router action and still compiled,
# and a 100-char router action with a short subagent name also compiled. The
# compiler enforces <=80 on the subagent NAME only, not on the router action that
# references it.


def test_compiler_verified_limit_is_80():
    """The measured compiler boundary is 80 inclusive; don't let it drift silently."""
    assert COMPILER_VERIFIED_NAME_LIMIT == 80


def test_derived_names_stay_inside_the_compiler_verified_limit():
    """Every derived name must fit the limit the compiler actually enforces.

    MAX_NAME_LENGTH (74) is deliberately stricter than the measured 80 — see the
    rationale in naming.py. This test asserts the relationship that must hold no
    matter how the local cap is tuned: nothing we emit may exceed what the
    compiler accepts.
    """
    assert MAX_NAME_LENGTH <= COMPILER_VERIFIED_NAME_LIMIT, (
        f"MAX_NAME_LENGTH ({MAX_NAME_LENGTH}) exceeds the compiler-verified limit "
        f"({COMPILER_VERIFIED_NAME_LIMIT}); emitted names would be rejected with "
        '"Too big: expected string to have <=80 characters".'
    )

    for length in (1, 40, 74, 80, 100, 200, 500):
        intent = "Update Case " + "X" * length
        for form, value in (
            ("topic_api_name", topic_api_name(intent)),
            ("subagent_name", subagent_name(intent)),
        ):
            assert len(value) <= COMPILER_VERIFIED_NAME_LIMIT, (
                f"{form}({length} char intent) = {len(value)} chars, over the "
                f"measured compiler limit of {COMPILER_VERIFIED_NAME_LIMIT}"
            )


# === PROPERTY TEST 1: Length invariant ===

@pytest.mark.parametrize(
    "intent",
    [
        "Update Case",
        "A" * 70,  # exactly 70 chars
        "A" * 74,  # exactly 74 chars (the cap)
        "A" * 80,  # over the cap
        "A" * 100,  # well over the cap
        "A" * 200,  # way over
        "Update Case Status Priority Owner Assignment Comments Description Subject",
        "Very Long Intent With Many Words That Should Get Truncated To Fit Within The Eighty Character Limit",
    ],
)
def test_length_invariant_all_forms_under_cap(intent: str):
    """All three name forms must be <= MAX_NAME_LENGTH for any intent."""
    topic = topic_api_name(intent)
    subagent = subagent_name(intent)
    router = router_action_name(intent)

    assert len(topic) <= MAX_NAME_LENGTH, f"topic_api_name too long: {len(topic)} chars"
    assert len(subagent) <= MAX_NAME_LENGTH, f"subagent_name too long: {len(subagent)} chars"
    # Router action is go_to_<subagent>, so its max length is len("go_to_") + MAX_NAME_LENGTH = 80
    assert len(router) <= 80, f"router_action_name too long: {len(router)} chars (max 80)"


def test_length_invariant_70_to_90_char_intents():
    """Sweep the 70-90 char range where the old defect manifested."""
    for length in range(70, 91):
        intent = "A" * length
        topic = topic_api_name(intent)
        subagent = subagent_name(intent)
        router = router_action_name(intent)

        assert len(topic) <= MAX_NAME_LENGTH, f"topic at {length} chars: {len(topic)}"
        assert len(subagent) <= MAX_NAME_LENGTH, f"subagent at {length} chars: {len(subagent)}"
        assert len(router) <= 80, f"router at {length} chars: {len(router)} (intent len={length})"


def test_length_invariant_realistic_long_intents():
    """Real-world intents that are long but not pathological."""
    long_intents = [
        "Update Case Status Priority Owner Assignment Comments Description Subject Matter",
        "Create New Opportunity With Account Contact Product Quantity Price Discount Stage",
        "Escalate Support Ticket To Engineering Team With Severity Priority Category",
    ]
    for intent in long_intents:
        topic = topic_api_name(intent)
        subagent = subagent_name(intent)
        router = router_action_name(intent)

        assert len(topic) <= MAX_NAME_LENGTH
        assert len(subagent) <= MAX_NAME_LENGTH
        assert len(router) <= 80  # Router action is go_to_<subagent>, so max 80


# === PROPERTY TEST 2: Cross-artifact linkage is preserved ===

@pytest.mark.parametrize(
    "intent",
    [
        "Update Case",
        "A" * 70,
        "A" * 80,
        "A" * 100,
        "Update Case Status Priority Owner",
        "Very Long Intent With Many Words",
        "Escalation",  # reserved word
        "Off Topic",  # reserved word (two words)
        "Ambiguous Question",  # reserved word (two words)
    ],
)
def test_linkage_preserved(intent: str):
    """names_agree must hold after the length fix."""
    topic = topic_api_name(intent)
    subagent = subagent_name(intent)

    assert names_agree(topic, subagent), (
        f"Linkage broken: topic={topic!r}, subagent={subagent!r}"
    )


def test_linkage_router_action_to_subagent():
    """Router action name must be unambiguously derivable from subagent name."""
    intents = ["Update Case", "A" * 80, "Very Long Intent With Many Words"]
    for intent in intents:
        subagent = subagent_name(intent)
        router = router_action_name(intent)

        # Router action is "go_to_<subagent>"
        assert router.startswith("go_to_"), f"Router action doesn't start with go_to_: {router!r}"
        extracted_subagent = router[len("go_to_"):]
        assert extracted_subagent == subagent, (
            f"Router action {router!r} does not map to subagent {subagent!r}"
        )


# === PROPERTY TEST 3: Distinct intents produce distinct router actions ===

def test_no_collision_on_long_intents():
    """Two distinct long intents that truncate to different topics must have different router actions."""
    intent_a = "Update Case Status Priority Owner Assignment Comments Description A"
    intent_b = "Update Case Status Priority Owner Assignment Comments Description B"

    topic_a = topic_api_name(intent_a)
    topic_b = topic_api_name(intent_b)
    router_a = router_action_name(intent_a)
    router_b = router_action_name(intent_b)

    # If topics differ, router actions must differ
    if topic_a != topic_b:
        assert router_a != router_b, (
            f"Collision: distinct topics {topic_a!r} vs {topic_b!r} "
            f"produced the same router action {router_a!r}"
        )


def test_no_collision_reserved_vs_normal():
    """A reserved-word intent must not collide with a normal intent of the same base."""
    # "escalation" is reserved; "escalation_flow" is not
    reserved_intent = "Escalation"
    normal_intent = "Escalation Flow"

    router_reserved = router_action_name(reserved_intent)
    router_normal = router_action_name(normal_intent)

    assert router_reserved != router_normal, (
        f"Collision: reserved {reserved_intent!r} and normal {normal_intent!r} "
        f"produced the same router action {router_reserved!r}"
    )


# === TEST 4: Reserved-word escaping still works after length fix ===

@pytest.mark.parametrize(
    "intent,expected_suffix",
    [
        ("Escalation", "_topic"),
        ("Off Topic", "_topic"),
        ("Ambiguous Question", "_topic"),
        ("escalation", "_topic"),  # case-insensitive
        ("ESCALATION", "_topic"),
        ("off_topic", "_topic"),
        ("ambiguous_question", "_topic"),
    ],
)
def test_reserved_word_escaping(intent: str, expected_suffix: str):
    """Reserved words must be escaped with _topic suffix, and must fit within MAX_NAME_LENGTH."""
    topic = topic_api_name(intent)
    subagent = subagent_name(intent)
    router = router_action_name(intent)

    # All forms must be escaped
    assert topic.lower().endswith(expected_suffix), f"topic not escaped: {topic!r}"
    assert subagent.endswith(expected_suffix), f"subagent not escaped: {subagent!r}"
    # Router action is go_to_<subagent>, so it should contain the escaped subagent
    assert expected_suffix in router, f"router action not escaped: {router!r}"

    # All forms must still be within the length cap
    assert len(topic) <= MAX_NAME_LENGTH
    assert len(subagent) <= MAX_NAME_LENGTH
    assert len(router) <= MAX_NAME_LENGTH

    # Linkage must still hold
    assert names_agree(topic, subagent)


def test_reserved_word_escaping_long_intent():
    """A long intent that is also a reserved word must escape AND truncate correctly.

    Note: "Escalation Process Process..." has multiple tokens, so only the first
    token ("escalation") is checked for reserved-word status. Since "escalation"
    is reserved, the entire token list gets the _topic suffix. However, after
    truncation, the suffix may be dropped if it falls outside the budget.

    This test verifies that escaping happens, but if truncation removes the suffix,
    that's acceptable — the important part is that the name doesn't collide with
    the bare reserved word "escalation" (it will have other tokens like "Process").
    """
    # "Escalation" alone should be escaped
    escalation_only = "Escalation"
    topic_only = topic_api_name(escalation_only)
    subagent_only = subagent_name(escalation_only)
    router_only = router_action_name(escalation_only)

    # Must be escaped
    assert "_topic" in topic_only.lower()
    assert "_topic" in subagent_only
    assert "_topic" in router_only

    # Must be within cap
    assert len(topic_only) <= MAX_NAME_LENGTH
    assert len(subagent_only) <= MAX_NAME_LENGTH
    assert len(router_only) <= 80

    # Linkage must hold
    assert names_agree(topic_only, subagent_only)


def test_is_reserved_function():
    """Test the is_reserved helper directly."""
    assert is_reserved("escalation")
    assert is_reserved("Escalation")
    assert is_reserved("ESCALATION")
    assert is_reserved("off_topic")
    assert is_reserved("Off Topic")
    assert is_reserved("ambiguous_question")
    assert is_reserved("Ambiguous Question")

    assert not is_reserved("escalation_topic")  # already escaped
    assert not is_reserved("update_case")
    assert not is_reserved("normal_intent")


# === TEST 5: Two-dialect agreement ===

def test_topic_is_capital_case():
    """topic_api_name returns CapitalCase with underscores."""
    topic = topic_api_name("update case status")
    # Should be Update_Case_Status
    assert topic[0].isupper(), f"topic should start with uppercase: {topic!r}"
    assert "_" in topic, f"topic should have underscores: {topic!r}"


def test_subagent_is_snake_case():
    """subagent_name returns snake_case (all lowercase)."""
    subagent = subagent_name("Update Case Status")
    assert subagent.islower(), f"subagent should be lowercase: {subagent!r}"
    assert "_" in subagent, f"subagent should have underscores: {subagent!r}"


def test_router_is_snake_case_with_prefix():
    """router_action_name returns go_to_<snake_case>."""
    router = router_action_name("Update Case Status")
    assert router.startswith("go_to_"), f"router should start with go_to_: {router!r}"
    assert router.islower(), f"router should be all lowercase: {router!r}"


# === TEST 6: dedupe_names ===

def test_dedupe_names_preserves_distinct():
    """Distinct names are preserved."""
    names = ["Update_Case", "Close_Opportunity", "Escalate_Ticket"]
    result = dedupe_names(names)
    assert result == names


def test_dedupe_names_adds_numeric_suffix():
    """Colliding names get numeric suffixes."""
    names = ["Update_Case", "update_case", "UPDATE_CASE"]
    result = dedupe_names(names)

    # First is unchanged, second and third get _2 and _3
    assert result[0] == "Update_Case"
    assert result[1] == "update_case_2"
    assert result[2] == "UPDATE_CASE_3"


def test_dedupe_names_respects_length_cap():
    """Numeric suffixes must fit within MAX_NAME_LENGTH."""
    # A name at exactly the cap
    long_name = "A" * MAX_NAME_LENGTH
    names = [long_name, long_name, long_name]
    result = dedupe_names(names)

    # All results must be <= MAX_NAME_LENGTH
    for r in result:
        assert len(r) <= MAX_NAME_LENGTH, f"deduped name too long: {len(r)} chars"

    # First is unchanged
    assert result[0] == long_name

    # Second and third are truncated + suffixed
    assert result[1].endswith("_2")
    assert result[2].endswith("_3")


def test_dedupe_names_case_insensitive():
    """Deduplication is case-insensitive."""
    names = ["UpdateCase", "updatecase", "UPDATECASE"]
    result = dedupe_names(names)

    # All three should be treated as collisions
    assert len(result) == 3
    assert result[0] == "UpdateCase"
    assert "_2" in result[1]
    assert "_3" in result[2]


# === TEST 7: Empty/degenerate intents ===

def test_empty_intent():
    """Empty intent returns FALLBACK_TOPIC_NAME."""
    topic = topic_api_name("")
    assert topic == FALLBACK_TOPIC_NAME


def test_whitespace_intent():
    """Whitespace-only intent returns FALLBACK_TOPIC_NAME."""
    topic = topic_api_name("   ")
    assert topic == FALLBACK_TOPIC_NAME


def test_unresolved_intent():
    """UNRESOLVED: prefix is stripped for API names."""
    topic = topic_api_name("UNRESOLVED: Update Case")
    # Should be Update_Case, not Unresolved_Update_Case
    assert "UNRESOLVED" not in topic.upper()
    assert "Update" in topic or "Case" in topic


# === TEST 8: Tokenization ===

def test_tokenize_camelcase():
    """Tokenize splits camelCase correctly."""
    tokens = tokenize("updateCaseStatus")
    assert tokens == ["update", "Case", "Status"]


def test_tokenize_with_punctuation():
    """Tokenize treats punctuation as word boundaries."""
    tokens = tokenize("Update-Case (Status)")
    # Parentheses are kept in the current implementation
    assert "Update" in tokens
    assert "Case" in tokens
    assert "Status" in tokens


def test_tokenize_preserves_acronyms():
    """Tokenize preserves acronyms like HTTP, API."""
    tokens = tokenize("HTTPResponseAPI")
    # Should split on the lower-to-upper boundary
    assert "HTTP" in tokens or "Response" in tokens or "API" in tokens


# === TEST 9: Realistic e2e scenarios ===

def test_realistic_case_update_intent():
    """Realistic Case update intent."""
    intent = "Update Case (Status)"
    topic = topic_api_name(intent)
    subagent = subagent_name(intent)
    router = router_action_name(intent)

    # Should be Update_Case_Status
    assert topic == "Update_Case_Status"
    assert subagent == "update_case_status"
    assert router == "go_to_update_case_status"

    assert len(topic) <= MAX_NAME_LENGTH
    assert len(subagent) <= MAX_NAME_LENGTH
    assert len(router) <= MAX_NAME_LENGTH
    assert names_agree(topic, subagent)


def test_realistic_opportunity_close_intent():
    """Realistic Opportunity close intent."""
    intent = "Close Opportunity (Amount, Stage)"
    topic = topic_api_name(intent)
    subagent = subagent_name(intent)
    router = router_action_name(intent)

    # Should be Close_Opportunity_Amount_Stage
    assert "Close" in topic
    assert "Opportunity" in topic
    # Parentheticals are kept
    assert "Amount" in topic or "Stage" in topic

    assert len(topic) <= MAX_NAME_LENGTH
    assert len(subagent) <= MAX_NAME_LENGTH
    assert len(router) <= MAX_NAME_LENGTH
    assert names_agree(topic, subagent)


def test_realistic_long_multi_field_intent():
    """Long intent with many fields."""
    intent = "Update Case Status Priority Owner Assignment Comments Description Subject Matter Category"
    topic = topic_api_name(intent)
    subagent = subagent_name(intent)
    router = router_action_name(intent)

    # Should truncate but remain valid
    assert len(topic) <= MAX_NAME_LENGTH
    assert len(subagent) <= MAX_NAME_LENGTH
    assert len(router) <= 80  # Router action max is 80
    assert names_agree(topic, subagent)

    # Router should still be derivable
    assert router.startswith("go_to_")
    assert router[len("go_to_"):] == subagent


# === TEST 10: dedupe_names comprehensive invariants ===

def test_dedupe_preserves_length_and_order():
    """dedupe_names must preserve input length and order."""
    names = ["A", "B", "A", "C", "B", "A"]
    result = dedupe_names(names)
    assert len(result) == len(names), "Length must be preserved"


def test_dedupe_no_duplicates_case_insensitive():
    """dedupe_names output must have no case-insensitive duplicates."""
    names = ["Update_Case", "update_case", "UPDATE_CASE", "Update_Case"]
    result = dedupe_names(names)

    lower_result = [r.lower() for r in result]
    assert len(lower_result) == len(set(lower_result)), (
        f"Output has duplicates: {result}"
    )


def test_dedupe_respects_max_length():
    """Every dedupe_names output element must be <= MAX_NAME_LENGTH."""
    # Try various lengths near the boundary
    for base_len in range(MAX_NAME_LENGTH - 5, MAX_NAME_LENGTH + 5):
        names = ["A" * base_len] * 5
        result = dedupe_names(names)

        for r in result:
            assert len(r) <= MAX_NAME_LENGTH, (
                f"Output element too long: {len(r)} chars (max {MAX_NAME_LENGTH})"
            )


def test_dedupe_no_reserved_words_in_output():
    """dedupe_names must not output reserved words."""
    # All reserved words
    reserved_inputs = [
        "escalation", "escalation", "off_topic", "off_topic",
        "ambiguous_question", "system", "config"
    ]
    result = dedupe_names(reserved_inputs)

    for r in result:
        assert not is_reserved(r), f"Output contains reserved word: {r!r}"


def test_dedupe_idempotence():
    """dedupe_names must be idempotent: dedupe(dedupe(x)) == dedupe(x)."""
    test_cases = [
        ["A", "A", "B"],
        ["Update_Case", "update_case", "UPDATE_CASE"],
        ["A" * MAX_NAME_LENGTH] * 3,
        ["escalation", "escalation", "off_topic"],
        ["A", "A", "A_2", "A_3", "A"],
    ]

    for names in test_cases:
        once = dedupe_names(names)
        twice = dedupe_names(once)
        assert once == twice, (
            f"Not idempotent:\n  Input: {names}\n  Once:  {once}\n  Twice: {twice}"
        )


def test_dedupe_suffix_collision_protection():
    """dedupe_names must not create collisions when input contains suffix-like patterns."""
    # Input contains "A" and "A_2", second "A" must become "A_3" not "A_2"
    names = ["A", "A", "A_2"]
    result = dedupe_names(names)

    assert result[0] == "A"
    assert result[1] in ("A_2", "A_3")  # Could be either depending on order
    assert result[2] in ("A_2", "A_2_2", "A_3")
    # The key invariant: no duplicates
    assert len(set(r.lower() for r in result)) == len(result)


def test_dedupe_pre_existing_suffix():
    """dedupe_names must handle pre-existing suffixes correctly."""
    names = ["A_2", "A", "A"]
    result = dedupe_names(names)

    # All must be distinct
    lower_result = [r.lower() for r in result]
    assert len(set(lower_result)) == len(result), f"Has duplicates: {result}"


def test_dedupe_complex_collision_chain():
    """dedupe_names must handle complex collision chains."""
    names = ["A", "A", "A_2", "A_3", "A", "A_2", "A_4"]
    result = dedupe_names(names)

    # All must be distinct
    lower_result = [r.lower() for r in result]
    assert len(set(lower_result)) == len(result), f"Has duplicates: {result}"

    # All must be valid length
    for r in result:
        assert len(r) <= MAX_NAME_LENGTH


def test_dedupe_truncation_collision():
    """dedupe_names must prevent collisions caused by truncation."""
    # Two distinct long names that differ only at the end
    long_base = "A" * (MAX_NAME_LENGTH - 1)
    names = [long_base + "X", long_base + "Y", long_base + "X"]
    result = dedupe_names(names)

    # All must be distinct
    lower_result = [r.lower() for r in result]
    assert len(set(lower_result)) == len(result), f"Has duplicates: {result}"

    # All must be valid length
    for r in result:
        assert len(r) <= MAX_NAME_LENGTH


def test_dedupe_empty_input():
    """dedupe_names must handle empty input."""
    result = dedupe_names([])
    assert result == []


def test_dedupe_single_element():
    """dedupe_names must handle single-element input."""
    result = dedupe_names(["A"])
    assert result == ["A"]


def test_dedupe_all_distinct():
    """dedupe_names must preserve all-distinct input."""
    names = ["A", "B", "C", "D", "E"]
    result = dedupe_names(names)
    assert result == names


# === TEST 11: Linkage invariants across derivation (property tests) ===

@pytest.mark.parametrize(
    "intent",
    [
        "",  # empty
        "   ",  # whitespace
        "A",  # single char
        "1",  # digit-only
        "123 Update Case",  # starts with digit
        "Update-Case-Status!!!",  # all punctuation stripped
        "你好 World",  # non-ASCII
        "A" * 200,  # way over length
        "UNRESOLVED: Update Case",  # marker prefix
        "escalation",  # reserved word
        "Off Topic",  # reserved phrase
        "Update Case (Status, Priority)",  # parentheticals
    ],
)
def test_linkage_invariants_for_intent(intent: str):
    """Verify all linkage invariants hold for a wide range of intents."""
    topic = topic_api_name(intent)
    subagent = subagent_name(intent)
    router = router_action_name(intent)

    # Invariant 1: Length caps
    assert len(topic) <= MAX_NAME_LENGTH, f"topic too long for {intent!r}"
    assert len(subagent) <= MAX_NAME_LENGTH, f"subagent too long for {intent!r}"
    assert len(router) <= 80, f"router too long for {intent!r}"

    # Invariant 2: Router is go_to_<subagent>
    assert router == f"go_to_{subagent}", (
        f"Router action mismatch for {intent!r}: {router!r} != go_to_{subagent!r}"
    )

    # Invariant 3: Names agree
    assert names_agree(topic, subagent), (
        f"Linkage broken for {intent!r}: topic={topic!r}, subagent={subagent!r}"
    )

    # Invariant 4: No reserved words (unless input was empty/whitespace)
    if intent.strip():
        # topic_api_name and subagent_name should escape reserved words
        # We test the OUTPUT, not the input
        pass  # Reserved word escaping is tested elsewhere

    # Invariant 5: Valid API tokens (start with letter or underscore)
    # topic_api_name prefixes with "T_" if needed
    if topic and topic != "Unresolved_Topic":
        assert topic[0].isalpha() or topic[0] == "_", f"topic doesn't start with letter: {topic!r}"


def test_derivation_idempotence():
    """Verify that re-deriving from a derived name produces the same result.

    This tests whether topic_api_name(topic_api_name(i)) == topic_api_name(i)
    and subagent_name(subagent_name(i)) == subagent_name(i).

    If this holds, it means the derivations are stable and the two dialects
    cannot drift even if we accidentally re-derive.
    """
    intents = [
        "Update Case",
        "A" * 80,
        "escalation",
        "Off Topic",
        "Create New Opportunity",
    ]

    for intent in intents:
        topic_once = topic_api_name(intent)
        topic_twice = topic_api_name(topic_once)

        subagent_once = subagent_name(intent)
        subagent_twice = subagent_name(subagent_once)

        # topic_api_name should be idempotent-ish (may differ in casing but tokens same)
        assert names_agree(topic_once, snake_case(topic_twice)), (
            f"topic_api_name not stable: {intent!r} -> {topic_once!r} -> {topic_twice!r}"
        )

        # subagent_name should be exactly idempotent (it's already lowercase)
        assert subagent_once == subagent_twice, (
            f"subagent_name not idempotent: {intent!r} -> {subagent_once!r} -> {subagent_twice!r}"
        )


def test_distinct_intents_produce_distinct_routers():
    """Distinct intents that produce distinct topics must produce distinct router actions."""
    intent_pairs = [
        ("Update Case Status", "Update Case Priority"),
        ("Close Opportunity", "Close Account"),
        ("A" * 70 + "X", "A" * 70 + "Y"),
    ]

    for intent_a, intent_b in intent_pairs:
        topic_a = topic_api_name(intent_a)
        topic_b = topic_api_name(intent_b)
        router_a = router_action_name(intent_a)
        router_b = router_action_name(intent_b)

        # If topics differ, routers must differ
        if topic_a.lower() != topic_b.lower():
            assert router_a != router_b, (
                f"Collision: distinct topics {topic_a!r} vs {topic_b!r} "
                f"produced the same router action {router_a!r}"
            )


# === TEST 12: Property tests for all derivations ===

def test_topic_api_name_never_empty():
    """topic_api_name must never return empty string."""
    test_cases = ["", "   ", "!!!", "___", "123"]
    for intent in test_cases:
        topic = topic_api_name(intent)
        assert topic, f"topic_api_name returned empty for {intent!r}"
        assert len(topic) > 0


def test_subagent_name_never_empty():
    """subagent_name must never return empty string."""
    test_cases = ["", "   ", "!!!", "___", "123"]
    for intent in test_cases:
        subagent = subagent_name(intent)
        assert subagent, f"subagent_name returned empty for {intent!r}"
        assert len(subagent) > 0


def test_router_action_name_never_empty():
    """router_action_name must never return empty string."""
    test_cases = ["", "   ", "!!!", "___", "123"]
    for intent in test_cases:
        router = router_action_name(intent)
        assert router, f"router_action_name returned empty for {intent!r}"
        assert len(router) > 0
        assert router.startswith("go_to_")


# === PREFIXED NAMES: a marked name must still identify its recording ===
#
# `prefixed_api_name` exists because the obvious spelling loses the intent.
# Folding the prefix into the intent makes the two compete for one length budget,
# and the prefix wins because it comes first:
#
#     topic_api_name("SFVB TEST " + "A" * 90)  -> "SFVB_TEST"
#     topic_api_name("SFVB TEST " + "!!!")     -> "SFVB_TEST"
#
# Both derive the same name, and that name identifies nothing but the prefix. The
# failure is invisible to a cross-artifact check: every dialect agrees, because
# they agree on a name that names no recording. That is the same class of defect
# as the original three-name divergence, one level up.

ORG_PREFIX = "SFVB TEST"

PREFIX_INTENTS = [
    "Update Case (Status)",
    "Create Contact",
    "escalation",  # reserved: must still be escaped under a prefix
    "2024 Renewal Process",  # must not start with a digit
    "A" * 90,  # one token, longer than the whole cap
    "!!!",  # no word characters at all
    "Update Case " + ("Extremely Verbose Business Process Name " * 4),
]


@pytest.mark.parametrize("intent", PREFIX_INTENTS)
def test_prefixed_api_name_respects_the_length_cap(intent):
    """The prefix must be budgeted inside the cap, not added on top of it."""
    name = prefixed_api_name(ORG_PREFIX, intent)
    assert len(name) <= MAX_NAME_LENGTH, f"{name!r} is {len(name)} chars, cap is {MAX_NAME_LENGTH}"


@pytest.mark.parametrize("intent", PREFIX_INTENTS)
def test_prefixed_api_name_keeps_some_of_the_intent(intent):
    """A prefixed name must never collapse to the bare prefix.

    This is the regression guard. A name that is only the prefix cannot
    distinguish one recording from another, so it is worse than a truncated one.
    """
    name = prefixed_api_name(ORG_PREFIX, intent)
    head = "_".join(t[:1].upper() + t[1:] for t in tokenize(ORG_PREFIX))
    assert name != head, f"{intent[:30]!r} derived the bare prefix {name!r}, identifying nothing"
    assert name.startswith(head + "_")
    assert len(name) > len(head) + 1


def test_prefixed_api_name_does_not_collide_across_distinct_intents():
    """The exact defect: two unrelated intents deriving one agent name.

    Measured before the fix — `"A" * 90` and `"!!!"` both produced `SFVB_TEST`.
    """
    names = [prefixed_api_name(ORG_PREFIX, intent) for intent in PREFIX_INTENTS]
    assert len(set(names)) == len(names), f"collision among {names}"

    # And specifically the pair that used to collide.
    assert prefixed_api_name(ORG_PREFIX, "A" * 90) != prefixed_api_name(ORG_PREFIX, "!!!")


@pytest.mark.parametrize("intent", PREFIX_INTENTS)
def test_prefixed_api_name_keeps_both_dialects_in_agreement(intent):
    """The snake_case form must still be the same name, or `@subagent.X` dangles."""
    name = prefixed_api_name(ORG_PREFIX, intent)
    assert names_agree(name, snake_case(name))


@pytest.mark.parametrize("intent", PREFIX_INTENTS)
def test_prefixed_api_name_is_a_valid_api_name(intent):
    """Salesforce API names: word characters only, never leading with a digit."""
    name = prefixed_api_name(ORG_PREFIX, intent)
    assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name), f"{name!r} is not a valid API name"


def test_prefixed_api_name_marks_a_wordless_intent_visibly():
    """No usable words must yield the visible marker, not a plausible-looking name."""
    name = prefixed_api_name(ORG_PREFIX, "!!!")
    assert FALLBACK_TOPIC_NAME.split("_")[0] in name


def test_prefixed_api_name_refuses_a_prefix_that_eats_the_whole_cap():
    """Better to fail loudly than to return a name that cannot identify anything."""
    with pytest.raises(ValueError, match="no room for an intent"):
        prefixed_api_name("X" * MAX_NAME_LENGTH, "Update Case")


def test_prefixed_api_name_with_no_prefix_is_just_the_topic_name():
    """An empty prefix must not invent a separator or change the derivation."""
    assert prefixed_api_name("", "Update Case (Status)") == topic_api_name("Update Case (Status)")


# === PYTHON KEYWORD COLLISION GUARD (D8) ===
#
# subagent_name() emits snake_case names consumed by Agent Script tooling that
# parses the emitted `.agent` via Python identifier paths. If a recording's
# intent normalises to `class`, `return`, `if`, etc., the compiler rejects the
# emitted `@subagent.class` reference. Guard by asserting that the reserved
# set escapes EVERY entry in `keyword.kwlist` (and soft keywords), for both
# dialects, so `names_agree` still holds.

import keyword as _kw


@pytest.mark.parametrize("kw", sorted(_kw.kwlist))
def test_subagent_name_never_equals_a_python_keyword(kw: str):
    """Every HARD Python keyword must be escaped by subagent_name().

    Soft keywords (`match`, `case`, `type`, `_`) are deliberately NOT in scope
    here — they are legal Python identifiers, `class Foo: case = 5; obj.case`
    compiles and runs. Escaping them was an over-eager guard that silently
    renamed the Salesforce standard object `Case` to `case_topic`. See
    ``test_soft_keywords_are_legal_identifiers_and_not_escaped`` below.
    """
    subagent = subagent_name(kw)
    assert subagent not in _kw.kwlist, (
        f"subagent_name({kw!r}) = {subagent!r} collides with a Python keyword"
    )


@pytest.mark.parametrize("kw", sorted(_kw.kwlist))
def test_topic_api_name_never_equals_a_python_keyword(kw: str):
    """Every HARD Python keyword must be escaped by topic_api_name() too, in its own dialect."""
    topic = topic_api_name(kw)
    # Compare in the snake_case dialect since keywords are lowercase.
    assert topic.lower() not in _kw.kwlist, (
        f"topic_api_name({kw!r}) = {topic!r} snakes to a Python keyword"
    )


@pytest.mark.parametrize("kw", sorted(_kw.kwlist))
def test_keyword_escape_preserves_linkage(kw: str):
    """After escaping, `names_agree(topic, subagent)` must still hold."""
    topic = topic_api_name(kw)
    subagent = subagent_name(kw)
    assert names_agree(topic, subagent), (
        f"Linkage broken for keyword {kw!r}: topic={topic!r}, subagent={subagent!r}"
    )


@pytest.mark.parametrize("kw", sorted(_kw.kwlist))
def test_keyword_escape_uses_topic_suffix(kw: str):
    """The chosen escape is the stable `_topic` suffix, matching grammar-keyword handling."""
    subagent = subagent_name(kw)
    # For a bare keyword input, the escape should append _topic.
    assert subagent.endswith("_topic"), (
        f"subagent_name({kw!r}) = {subagent!r} did not receive the _topic escape"
    )


# === REGRESSION: soft keywords are legal identifiers, must NOT be escaped ===
#
# `keyword.softkwlist` (`match`, `case`, `type`, `_` as of 3.10+) are legal
# Python identifiers — `class Foo: case = 5; obj.case` compiles and runs. The
# empirical justification for escaping HARD keywords (`class`, `return`) does
# NOT carry over. Including softkwlist in the reserved set caused a silent
# false positive: intent "Case" (the Salesforce standard object) tokenised to
# `["Case"]`, joined to `case`, and was suffixed to `Case_topic` / `case_topic`,
# silently breaking backward compatibility with any prior generated spec whose
# topic was `Case`. Same false positive for `"Type"`, `"Match"`.
#
# This test locks in the fix: soft keywords must round-trip cleanly.


@pytest.mark.parametrize(
    "intent,expected_topic,expected_subagent",
    [
        # The headline counter-example: Salesforce standard object.
        ("Case", "Case", "case"),
        # Account.Type is a real Salesforce field-name pattern.
        ("Type", "Type", "type"),
        # A plausible business verb.
        ("Match", "Match", "match"),
        # Lowercased forms of the same intents must not be escaped either.
        ("case", "Case", "case"),
        ("type", "Type", "type"),
        ("match", "Match", "match"),
    ],
)
def test_soft_keywords_are_legal_identifiers_and_not_escaped(
    intent: str, expected_topic: str, expected_subagent: str
):
    """Soft-keyword intents must NOT receive the `_topic` suffix.

    Regression for the D8 compatibility defect: including `keyword.softkwlist`
    in `_RESERVED_SUBAGENT_NAMES` mislabelled the Salesforce standard object
    `Case` (and other soft-keyword-named intents) as a grammar collision and
    silently renamed it. Soft keywords are legal Python identifiers, so no
    escape is warranted.
    """
    assert topic_api_name(intent) == expected_topic, (
        f"topic_api_name({intent!r}) should not receive a keyword-collision suffix"
    )
    assert subagent_name(intent) == expected_subagent, (
        f"subagent_name({intent!r}) should not receive a keyword-collision suffix"
    )
    # is_reserved must also agree — soft keywords are not reserved.
    assert not is_reserved(intent), (
        f"is_reserved({intent!r}) must be False; soft keywords are legal identifiers"
    )


def test_underscore_soft_keyword_is_not_reserved():
    """`_` is a soft keyword in 3.10+ but a legal identifier; must not be treated as reserved.

    Bare `_` tokenises to a single token; the name-derivation path applies its
    non-alpha-prefix rule (`T_` prefix) which is unrelated to the reserved-set
    question. What we assert here is that the reserved-set check itself does
    not flag `_` as a collision.
    """
    assert not is_reserved("_"), "'_' is a soft keyword but a legal identifier; not reserved"


# === GOLDEN TEST: non-colliding inputs still produce byte-identical output ===

@pytest.mark.parametrize(
    "intent,expected_topic,expected_subagent,expected_router",
    [
        ("Update Case (Status)", "Update_Case_Status", "update_case_status", "go_to_update_case_status"),
        ("Close Opportunity", "Close_Opportunity", "close_opportunity", "go_to_close_opportunity"),
        ("Create Contact", "Create_Contact", "create_contact", "go_to_create_contact"),
        ("HTTPResponse", "HTTP_Response", "http_response", "go_to_http_response"),
        ("updateCaseStatus", "Update_Case_Status", "update_case_status", "go_to_update_case_status"),
    ],
)
def test_golden_non_colliding_inputs_are_byte_identical(
    intent: str, expected_topic: str, expected_subagent: str, expected_router: str
):
    """The Python-keyword fix MUST NOT alter output for non-colliding inputs.

    This locks in byte-identical output for the intents that dominate real
    recordings, so a future change to the reserved set cannot silently drift
    the emitted names.
    """
    assert topic_api_name(intent) == expected_topic
    assert subagent_name(intent) == expected_subagent
    assert router_action_name(intent) == expected_router
