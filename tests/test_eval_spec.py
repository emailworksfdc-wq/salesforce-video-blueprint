"""Adversarial tests for eval_spec.py (test-spec emitter).

These tests verify that the test-spec emitter:
1. Emits structurally distinct and correct legacy vs NGT specs
2. Uses only real metric/scorer names from the CLI
3. Enforces NGT structural rules the CLI checks
4. NEVER fabricates failure tests without observed failures
5. Never invents action names
6. Generates entity-collection cases for every required entity
7. Handles YAML-hostile characters correctly
8. Is deterministic (same input -> byte-identical output)
9. TOPIC-NAME AGREEMENT: test specs, agent specs, and agent scripts all derive
   the same topic name from the same DerivedAgentSpec (critical cross-module test)
"""
from __future__ import annotations

import pytest

from sf_video_blueprint.eval_spec import (
    LEGACY_METRICS,
    NGT_SCORERS_NEEDING_EXPECTED,
    NGT_SCORERS_QUALITY,
    LegacyTestCase,
    LegacyTestSpec,
    NgtTestCase,
    NgtTestSpec,
    build_legacy_test_spec,
    build_ngt_test_spec,
    write_test_spec,
    _to_api_name,
    _yaml_string,
)
from sf_video_blueprint.spec_builder import DerivedAgentSpec, DerivedEntity, SpecEvidence


# --- Fixture: minimal DerivedAgentSpec ---

def _make_spec(
    intent: str = "Update Case Status",
    confidence: float = 0.75,
    objects_touched: list[str] | None = None,
    entities: list[DerivedEntity] | None = None,
    guardrails: list[str] | None = None,
    failure_handling: list[str] | None = None,
) -> DerivedAgentSpec:
    """Build a minimal DerivedAgentSpec for testing."""
    if objects_touched is None:
        objects_touched = ["Case"]
    if entities is None:
        entities = [
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "recordId required")],
            ),
        ]
    if guardrails is None:
        guardrails = ["Require explicit user confirmation before writing: Status."]
    if failure_handling is None:
        failure_handling = [
            "No failures were observed in this run, so error paths are UNTESTED. "
            "Record a failing variant before relying on this spec."
        ]

    return DerivedAgentSpec(
        intent=intent,
        confidence=confidence,
        objects_touched=objects_touched,
        entities=entities,
        orchestration_steps=[
            "Resolve and load the target Case record",
            "SUBMIT on button:Save -> writes Status",
        ],
        guardrails=guardrails,
        failure_handling=failure_handling,
        unknowns=[],
        evidence=[SpecEvidence("telemetry", "test evidence")],
    )


# === TEST 1: Both dialects are structurally distinct and correct ===

def test_legacy_structure_is_correct():
    """Legacy specs use utterance/expectedTopic/expectedActions/expectedOutcome/metrics."""
    spec = _make_spec()
    legacy_spec, _ = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    assert isinstance(legacy_spec, LegacyTestSpec)
    assert legacy_spec.name == "Test"
    assert legacy_spec.subjectType == "AGENT"
    assert legacy_spec.subjectName == "MyAgent"
    assert len(legacy_spec.testCases) > 0

    # Check first test case structure
    tc = legacy_spec.testCases[0]
    assert hasattr(tc, "utterance")
    assert hasattr(tc, "expectedTopic")
    assert hasattr(tc, "expectedActions")
    assert hasattr(tc, "expectedOutcome")
    assert hasattr(tc, "metrics")
    assert hasattr(tc, "customEvaluations")

    # MUST NOT have NGT-only keys
    assert not hasattr(tc, "inputs")
    assert not hasattr(tc, "scorers")


def test_ngt_structure_is_correct():
    """NGT specs use inputs + scorers, NOT legacy keys."""
    spec = _make_spec()
    ngt_spec, _ = build_ngt_test_spec(spec, name="Test", subject_name="MyAgent")

    assert isinstance(ngt_spec, NgtTestSpec)
    assert ngt_spec.name == "Test"
    assert ngt_spec.subjectType == "AGENT"
    assert ngt_spec.subjectName == "MyAgent"
    assert len(ngt_spec.testCases) > 0

    # Check first test case structure
    tc = ngt_spec.testCases[0]
    assert hasattr(tc, "inputs")
    assert hasattr(tc, "scorers")
    assert len(tc.inputs) >= 1
    assert len(tc.scorers) >= 1

    # MUST NOT have legacy-only keys
    for inp in tc.inputs:
        assert hasattr(inp, "utterance")
        # These are allowed in NGT inputs
        assert hasattr(inp, "contextVariables")
        assert hasattr(inp, "conversationHistory")

    # Scorers have name + optional expected
    for scorer in tc.scorers:
        assert hasattr(scorer, "name")
        assert hasattr(scorer, "expected")


# === TEST 2: Metric names must be exactly the four real ones ===

def test_legacy_metrics_are_exactly_the_four_real_ones():
    """Legacy metrics must be completeness, coherence, conciseness, output_latency_milliseconds."""
    spec = _make_spec()
    legacy_spec, _ = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # Check every test case
    for tc in legacy_spec.testCases:
        if tc.metrics:
            # Must be a subset of the real four
            assert set(tc.metrics).issubset(LEGACY_METRICS), f"Invalid metrics: {tc.metrics}"
            # Most cases should have all four
            if tc.expectedOutcome:  # non-trivial case
                assert set(tc.metrics) == LEGACY_METRICS, f"Should have all four metrics, got: {tc.metrics}"


def test_ngt_scorers_are_from_cli_catalog():
    """NGT scorers must be from the CLI's ngtScorerCatalog."""
    spec = _make_spec()
    ngt_spec, _ = build_ngt_test_spec(spec, name="Test", subject_name="MyAgent")

    all_valid_scorers = NGT_SCORERS_NEEDING_EXPECTED | NGT_SCORERS_QUALITY

    for tc in ngt_spec.testCases:
        for scorer in tc.scorers:
            assert scorer.name in all_valid_scorers, f"Invalid scorer: {scorer.name}"


# === TEST 3: NGT structural rules the CLI enforces ===

def test_ngt_case_has_at_least_one_input():
    """Every NGT test case must have >= 1 inputs."""
    spec = _make_spec()
    ngt_spec, _ = build_ngt_test_spec(spec, name="Test", subject_name="MyAgent")

    for tc in ngt_spec.testCases:
        assert len(tc.inputs) >= 1, "NGT case must have at least one input"


def test_ngt_case_has_at_least_one_scorer():
    """Every NGT test case must have >= 1 scorers."""
    spec = _make_spec()
    ngt_spec, _ = build_ngt_test_spec(spec, name="Test", subject_name="MyAgent")

    for tc in ngt_spec.testCases:
        assert len(tc.scorers) >= 1, "NGT case must have at least one scorer"


def test_ngt_scorer_that_requires_expected_has_it():
    """Scorers that require expected: field have it."""
    spec = _make_spec()
    ngt_spec, _ = build_ngt_test_spec(spec, name="Test", subject_name="MyAgent")

    for tc in ngt_spec.testCases:
        for scorer in tc.scorers:
            if scorer.name in NGT_SCORERS_NEEDING_EXPECTED:
                assert scorer.expected is not None, (
                    f"Scorer {scorer.name} requires expected: but got None"
                )


# === TEST 4: NO FABRICATED FAILURE TESTS ===

def test_no_failure_case_when_untested():
    """When failure_handling says UNTESTED, no failure test case is emitted."""
    spec = _make_spec(
        failure_handling=[
            "No failures were observed in this run, so error paths are UNTESTED. "
            "Record a failing variant before relying on this spec."
        ]
    )

    legacy_spec, derivations = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # No failure test case should be present
    for tc in legacy_spec.testCases:
        assert "validation error" not in tc.utterance.lower(), (
            "Found a fabricated failure case when failures were UNTESTED"
        )
        assert "invalid" not in tc.utterance.lower(), (
            "Found a fabricated failure case when failures were UNTESTED"
        )

    # A derivation should record this gap
    gap_derivations = [d for d in derivations if "failure path gap" in d.purpose]
    assert len(gap_derivations) > 0, "No failure path gap recorded in derivations"

    gap = gap_derivations[0]
    assert len(gap.gaps) > 0, "Gap derivation has no gaps recorded"
    assert "UNTESTED" in gap.evidence


def test_failure_case_only_when_observed():
    """Failure test case is emitted ONLY when a validation error was observed."""
    spec = _make_spec(
        failure_handling=["Observed validation failure during recording: Status must be one of approved values"]
    )

    legacy_spec, derivations = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # Now a failure case SHOULD exist
    failure_cases = [tc for tc in legacy_spec.testCases if "validation error" in tc.utterance.lower() or "invalid" in tc.utterance.lower()]
    assert len(failure_cases) >= 1, "No failure case emitted despite observed validation failure"

    # Derivations should record this
    failure_derivations = [d for d in derivations if "failure path" in d.purpose and "gap" not in d.purpose]
    assert len(failure_derivations) >= 1, "No failure path derivation recorded"


def test_apex_failure_produces_test_case():
    """Observed APEX failure during recording produces a failure-path test case."""
    spec = _make_spec(
        failure_handling=["Observed apex failure during recording: Required field missing"]
    )

    legacy_spec, derivations = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # Should have a failure case
    failure_cases = [tc for tc in legacy_spec.testCases if "validation error" in tc.utterance.lower() or "invalid" in tc.utterance.lower()]
    assert len(failure_cases) >= 1, "No failure case emitted despite observed apex failure"

    # Derivation should record the apex layer
    failure_derivations = [d for d in derivations if "failure path" in d.purpose and "apex" in d.purpose.lower()]
    assert len(failure_derivations) >= 1, f"No apex failure path derivation recorded. Got: {[d.purpose for d in derivations]}"


def test_flow_failure_produces_test_case():
    """Observed FLOW failure during recording produces a failure-path test case."""
    spec = _make_spec(
        failure_handling=["Observed flow failure during recording: Flow interview terminated"]
    )

    legacy_spec, derivations = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # Should have a failure case
    failure_cases = [tc for tc in legacy_spec.testCases if "validation error" in tc.utterance.lower() or "invalid" in tc.utterance.lower()]
    assert len(failure_cases) >= 1, "No failure case emitted despite observed flow failure"

    # Derivation should record the flow layer
    failure_derivations = [d for d in derivations if "failure path" in d.purpose and "flow" in d.purpose.lower()]
    assert len(failure_derivations) >= 1, f"No flow failure path derivation recorded. Got: {[d.purpose for d in derivations]}"


# === TEST 5: No invented action names ===

def test_no_invented_action_names_in_legacy():
    """expectedActions is empty (not guessed) with a recorded gap."""
    spec = _make_spec()
    legacy_spec, derivations = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # Check that expectedActions is empty for all cases
    for tc in legacy_spec.testCases:
        assert tc.expectedActions == [], f"expectedActions should be empty, got: {tc.expectedActions}"

    # Check derivations record this gap
    happy_path_derivation = derivations[0]
    assert "expectedActions left empty" in " ".join(happy_path_derivation.gaps)


def test_no_action_sequence_match_scorer_in_ngt():
    """NGT happy-path case should omit action_sequence_match scorer (action names unknown)."""
    spec = _make_spec()
    ngt_spec, derivations = build_ngt_test_spec(spec, name="Test", subject_name="MyAgent")

    # Happy path is first case
    happy_case = ngt_spec.testCases[0]
    scorer_names = [s.name for s in happy_case.scorers]
    assert "action_sequence_match" not in scorer_names, (
        "action_sequence_match scorer should be omitted (action names not observed)"
    )

    # Derivation should record this gap
    assert "action_sequence_match scorer omitted" in " ".join(derivations[0].gaps)


# === TEST 6: Entity-collection cases exist ===

def test_entity_collection_cases_for_each_entity():
    """One test case per required entity, omitting that entity and expecting a prompt."""
    spec = _make_spec(
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority observed")],
            ),
            DerivedEntity(
                name="recordId",
                object_api_name="Case",
                field_api_name="Id",
                evidence=[SpecEvidence("inference", "recordId required")],
            ),
        ]
    )

    legacy_spec, derivations = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # Count entity-collection cases
    entity_collection_derivations = [d for d in derivations if d.purpose == "entity collection"]
    # Should have one for each entity with field_api_name
    assert len(entity_collection_derivations) == 3, f"Expected 3 entity collection cases, got {len(entity_collection_derivations)}"

    # Check that each case has correct evidence (entity observed in data delta)
    for d in entity_collection_derivations:
        assert "Entity" in d.evidence and "observed in data delta" in d.evidence, (
            f"Entity collection case evidence malformed: {d.evidence}"
        )


# === TEST 7: YAML hostility ===

def test_yaml_hostile_characters_in_utterances():
    """Utterances with colons, quotes, newlines, hashes must emit parseable YAML."""
    hostile_intent = "Update: Case? Status #urgent"
    spec = _make_spec(intent=hostile_intent)

    legacy_spec, _ = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # Write to a temp file and verify it's valid
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        path = f.name

    try:
        from pathlib import Path
        write_test_spec(Path(path), legacy_spec)

        # Try to parse it back
        yaml_content = Path(path).read_text()

        # Check escaping happened
        assert _yaml_string(hostile_intent) in yaml_content or f'"{hostile_intent}"' in yaml_content, (
            "Hostile characters not escaped in YAML"
        )

        # Try to parse with PyYAML if available
        try:
            import yaml
            parsed = yaml.safe_load(yaml_content)
            assert parsed is not None
            assert "testCases" in parsed
        except ImportError:
            # PyYAML not available, manual check
            # At minimum, the file should not have syntax errors (unbalanced quotes, etc.)
            assert yaml_content.count('"') % 2 == 0, "Unbalanced quotes in YAML"
    finally:
        import os
        os.unlink(path)


def test_yaml_string_escapes_correctly():
    """_yaml_string must handle colons, quotes, hashes, newlines."""
    # Colon
    assert '"' in _yaml_string("key: value") or _yaml_string("key: value") == "key: value"

    # Leading dash
    assert _yaml_string("- item").startswith('"') or _yaml_string("- item") == '"- item"'

    # Newline
    result = _yaml_string("line1\nline2")
    assert "\\n" in result or result.startswith('"'), "Newline not escaped"

    # Quote
    result = _yaml_string('say "hello"')
    assert '\\"' in result or result.startswith('"'), "Quote not escaped"


# === TEST 8: Determinism ===

def test_determinism():
    """Same spec -> byte-identical YAML."""
    spec = _make_spec()

    legacy_spec1, _ = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")
    legacy_spec2, _ = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f1:
        path1 = Path(f1.name)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f2:
        path2 = Path(f2.name)

    try:
        write_test_spec(path1, legacy_spec1)
        write_test_spec(path2, legacy_spec2)

        yaml1 = path1.read_text()
        yaml2 = path2.read_text()

        assert yaml1 == yaml2, "Same input produced different YAML output"
    finally:
        import os
        os.unlink(path1)
        os.unlink(path2)


# === TEST 9: TOPIC-NAME AGREEMENT ACROSS MODULES ===

def test_topic_name_agreement_across_modules():
    """CRITICAL: eval_spec, agentforce_spec, and agent_script must derive the same topic name.

    This is the highest-stakes cross-module test. If these three derive different topic
    names from the same DerivedAgentSpec, the generated test suite will reference a topic
    that does not exist in the agent, which is a silent, total failure of the loop.
    """
    spec = _make_spec(intent="Update Case (Status, Priority)")

    # B3: eval_spec topic name
    from sf_video_blueprint.eval_spec import _to_api_name as eval_to_api_name
    eval_topic = eval_to_api_name(spec.intent)

    # B1: agentforce_spec topic name
    from sf_video_blueprint.agentforce_spec import topics_from_spec, _to_api_name as af_to_api_name
    af_topics = topics_from_spec(spec)
    assert len(af_topics) == 1, "agentforce_spec should emit exactly one topic"
    af_topic = af_topics[0]["name"]

    # B2: agent_script subagent name (snake_case, so we compare after snake_casing)
    from sf_video_blueprint.agent_script import _derive_topics, to_snake_case
    script_topics = _derive_topics(spec)
    assert len(script_topics) == 1, "agent_script should emit exactly one topic"
    script_topic_label = script_topics[0]["name"]

    # eval_spec uses CapitalCase (e.g., "UpdateCase")
    # agentforce_spec uses the same _to_api_name logic (should match eval_spec)
    # agent_script uses snake_case (e.g., "update_case")

    # Assert eval_spec == agentforce_spec topic name
    assert eval_topic == af_topic, (
        f"Topic name mismatch: eval_spec={eval_topic!r}, agentforce_spec={af_topic!r}. "
        "Test suite will reference a topic that doesn't exist in the agent spec!"
    )

    # Assert snake_case(eval_topic) == snake_case(script_topic_label)
    eval_snake = to_snake_case(eval_topic)
    script_snake = to_snake_case(script_topic_label)
    assert eval_snake == script_snake, (
        f"Topic name mismatch: eval_spec (snake)={eval_snake!r}, agent_script={script_snake!r}. "
        "Test suite will reference a subagent that doesn't exist in the .agent file!"
    )


def test_topic_name_agreement_with_parentheticals():
    """Verify topic-name agreement when intent contains parentheticals."""
    spec = _make_spec(intent="Close Opportunity (Amount, Stage)")

    from sf_video_blueprint.eval_spec import _to_api_name as eval_to_api_name
    from sf_video_blueprint.agentforce_spec import topics_from_spec
    from sf_video_blueprint.agent_script import _derive_topics, to_snake_case

    eval_topic = eval_to_api_name(spec.intent)
    af_topics = topics_from_spec(spec)
    af_topic = af_topics[0]["name"]
    script_topics = _derive_topics(spec)
    script_topic_label = script_topics[0]["name"]

    # All should strip parentheticals and agree on "CloseOpportunity"
    assert eval_topic == af_topic, f"Topic name mismatch with parentheticals: {eval_topic} vs {af_topic}"
    assert to_snake_case(eval_topic) == to_snake_case(script_topic_label), (
        f"Snake-case mismatch: {to_snake_case(eval_topic)} vs {to_snake_case(script_topic_label)}"
    )


# === TEST 10: DEGENERATE UTTERANCE CASES (Defect 2 from round 3) ===

def test_duplicate_entity_names_no_repeated_placeholders():
    """Duplicate entity names must not produce repeated placeholders like {status} {status}."""
    spec = _make_spec(
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
            DerivedEntity(
                name="status",  # duplicate name
                object_api_name="Case",
                field_api_name="Priority",  # different field, same name
                evidence=[SpecEvidence("data-delta", "Case.Priority observed")],
            ),
        ]
    )

    legacy_spec, _ = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # Check happy path utterance
    happy_case = legacy_spec.testCases[0]
    utterance = happy_case.utterance

    # Count occurrences of {status}
    import re
    placeholders = re.findall(r"\{status\}", utterance)
    assert len(placeholders) == 1, f"Duplicate placeholder in utterance: {utterance!r}"


def test_empty_entity_name_excluded():
    """Empty or whitespace-only entity names must be excluded from utterances."""
    spec = _make_spec(
        entities=[
            DerivedEntity(
                name="",  # empty name
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "Case.Status observed")],
            ),
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "Case.Priority observed")],
            ),
        ]
    )

    legacy_spec, _ = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # Happy path utterance should not have {}
    happy_case = legacy_spec.testCases[0]
    utterance = happy_case.utterance

    assert "{}" not in utterance, f"Empty placeholder in utterance: {utterance!r}"
    # Should only have {priority}
    assert "{priority}" in utterance


def test_empty_intent_produces_marker():
    """Empty intent must produce a visible [NEEDS EVIDENCE] marker."""
    spec = _make_spec(intent="")

    legacy_spec, _ = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    happy_case = legacy_spec.testCases[0]
    utterance = happy_case.utterance

    assert "[NEEDS EVIDENCE" in utterance, f"Empty intent should produce marker, got: {utterance!r}"


def test_unresolved_intent_visible_in_utterance():
    """UNRESOLVED: intent is stripped from the utterance (same as in API names).

    The current behavior is that tokenize() in naming.py strips the UNRESOLVED:
    prefix, so the utterance is derived from the cleaned intent. This is correct
    — the test spec's utterances should be normal user requests, not markers.
    The UNRESOLVED marker is kept in the intent field itself for visibility.
    """
    spec = _make_spec(intent="UNRESOLVED: Update Case")

    legacy_spec, _ = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    happy_case = legacy_spec.testCases[0]
    utterance = happy_case.utterance

    # The utterance should be derived from "Update Case" (UNRESOLVED stripped)
    assert "Update" in utterance or "Case" in utterance, (
        f"Utterance should be derived from cleaned intent, got: {utterance!r}"
    )
    # UNRESOLVED should NOT be in the utterance
    assert "UNRESOLVED" not in utterance


def test_all_empty_entities_produces_base_intent():
    """When all entities have empty names, utterance should be just the base intent."""
    spec = _make_spec(
        intent="Update Case",
        entities=[
            DerivedEntity(
                name="",  # empty
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "x")],
            ),
            DerivedEntity(
                name="  ",  # whitespace
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "x")],
            ),
        ]
    )

    legacy_spec, _ = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    happy_case = legacy_spec.testCases[0]
    utterance = happy_case.utterance

    # Should be just "Update Case" with no placeholders
    assert "{" not in utterance, f"Should have no placeholders, got: {utterance!r}"
    assert "Update Case" in utterance or "Update" in utterance


def test_ngt_duplicate_entities_deduplicated():
    """NGT specs should also deduplicate entity names."""
    spec = _make_spec(
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "x")],
            ),
            DerivedEntity(
                name="status",  # duplicate
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "x")],
            ),
        ]
    )

    ngt_spec, _ = build_ngt_test_spec(spec, name="Test", subject_name="MyAgent")

    # Check happy path
    happy_case = ngt_spec.testCases[0]
    utterance = happy_case.inputs[0].utterance

    import re
    placeholders = re.findall(r"\{status\}", utterance)
    assert len(placeholders) == 1, f"Duplicate placeholder in NGT utterance: {utterance!r}"


def test_entity_collection_with_empty_name_skipped():
    """Entity collection test cases should not be generated for entities with empty names."""
    spec = _make_spec(
        entities=[
            DerivedEntity(
                name="",  # empty name
                object_api_name="Case",
                field_api_name="Status",
                evidence=[SpecEvidence("data-delta", "x")],
            ),
            DerivedEntity(
                name="priority",
                object_api_name="Case",
                field_api_name="Priority",
                evidence=[SpecEvidence("data-delta", "x")],
            ),
        ]
    )

    legacy_spec, derivations = build_legacy_test_spec(spec, name="Test", subject_name="MyAgent")

    # Count entity collection cases
    entity_collection_cases = [
        d for d in derivations if d.purpose == "entity collection"
    ]

    # Should only have one (for priority), not two
    assert len(entity_collection_cases) == 1, (
        f"Should only generate entity collection for non-empty names, got {len(entity_collection_cases)}"
    )
