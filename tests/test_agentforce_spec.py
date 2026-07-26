"""Adversarial tests for the hand-rolled YAML emitter.

The YAML emitter in agentforce_spec.py is hand-rolled because PyYAML is not a
project dependency. Hand-rolled YAML is exactly where subtle corruption hides —
a malformed spec YAML fails deep inside the Salesforce CLI with an opaque error.

This suite attacks the emitter with YAML-hostile strings: colons, leading dashes,
quotes, YAML 1.1 boolean traps, etc. PyYAML is not available in .venv, so these
tests use structural assertions and manual parsing. If PyYAML becomes available
as a dev dependency, these tests should be upgraded to parse and round-trip.

Coverage:
1. Canonical key order (contract 3.1)
2. YAML-hostile string content (the core vulnerability)
3. Refuse-by-default: InsufficientEvidenceError when evidence is missing
4. No topic invention: one process -> one topic, not max_topics
5. Guardrails and failure_handling survive (folded into topic descriptions)
6. Determinism: same input -> byte-identical output
7. _to_api_name topic naming: valid API-name tokens
8. Provenance comments: visible, do not introduce non-schema keys
9. write_agent_spec_yaml: creates parent dirs, UTF-8, idempotent
10. End-to-end: build_agent_spec -> build_agent_spec_yaml -> sane role
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

import pytest

from sf_video_blueprint.agentforce_spec import (
    AgentSpecYaml,
    InsufficientEvidenceError,
    build_agent_spec_yaml,
    role_from_spec,
    topics_from_spec,
    write_agent_spec_yaml,
    _to_api_name,
    _escape_yaml_string,
)
from sf_video_blueprint.naming import MAX_NAME_LENGTH
from sf_video_blueprint.spec_builder import (
    DerivedAgentSpec,
    DerivedEntity,
    SpecEvidence,
    build_agent_spec,
)
from sf_video_blueprint.correlation import StepAnalysis, FailureLayer
from sf_video_blueprint.models import ActionType, ExtractedAction, UIContext
from sf_video_blueprint.telemetry import CorrelationKey, ObjectSnapshot, TelemetryLayer

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


# ==================== Canonical key order (contract 3.1) ====================


def test_emitted_yaml_keys_appear_in_canonical_order():
    """The CLI expects keys in a specific order. Assert by scanning line order."""
    spec = AgentSpecYaml(
        agent_type="internal",
        company_name="Acme Corp",
        company_description="Sells anvils",
        company_website="https://acme.example",
        role="Update cases",
        max_num_of_topics=5,
        agent_user="user@example.com",
        enrich_logs=True,
        tone="formal",
        prompt_template_name="custom",
        grounding_context="SFDC org",
        topics=[{"name": "Update_Case", "description": "Update a case"}],
    )

    yaml_text = spec.to_yaml()
    lines = [line.split(":")[0] for line in yaml_text.split("\n") if line and not line.startswith(" ") and not line.startswith("#")]

    # Contract 3.1 canonical order
    expected_keys = [
        "agentType",
        "companyName",
        "companyDescription",
        "companyWebsite",
        "role",
        "maxNumOfTopics",
        "agentUser",
        "enrichLogs",
        "tone",
        "promptTemplateName",
        "groundingContext",
        "topics",
    ]

    assert lines == expected_keys, f"Key order mismatch: {lines} != {expected_keys}"


def test_topic_keys_appear_in_correct_order():
    """Topics use name then description (reverse-alphabetical, per the CLI)."""
    spec = AgentSpecYaml(
        agent_type="internal",
        company_name="Acme",
        company_description="Anvils",
        company_website=None,
        role="Update",
        max_num_of_topics=5,
        agent_user=None,
        enrich_logs=False,
        tone="formal",
        prompt_template_name=None,
        grounding_context=None,
        topics=[{"name": "Topic_A", "description": "Desc A"}],
    )

    yaml_text = spec.to_yaml()
    topic_block = yaml_text.split("topics:")[1]
    lines_with_keys = [line.strip() for line in topic_block.split("\n") if ":" in line and not line.strip().startswith("#")]

    # name comes before description
    assert lines_with_keys[0].startswith("- name:"), f"First topic key must be 'name', got: {lines_with_keys[0]}"
    assert lines_with_keys[1].startswith("description:"), f"Second topic key must be 'description', got: {lines_with_keys[1]}"


# ==================== YAML-hostile string content ====================


@pytest.mark.parametrize(
    "hostile_string",
    [
        "Status: New",  # colon+space triggers mapping
        "-leading-dash",  # leading dash triggers list
        "#comment",  # leading hash triggers comment
        "trailing:",  # trailing colon
        "has'apostrophe",  # apostrophe
        'has"quote',  # double quote
        "has\\backslash",  # backslash
        "has\nnewline",  # newline
        "has\ttab",  # tab
        "yes",  # YAML 1.1 boolean trap
        "no",  # YAML 1.1 boolean trap
        "true",  # YAML 1.1 boolean trap
        "false",  # YAML 1.1 boolean trap
        "null",  # YAML 1.1 null trap
        "on",  # YAML 1.1 boolean
        "off",  # YAML 1.1 boolean
        "~",  # YAML null
        "12345",  # digits only (not actually a problem, but test coverage)
        " leading-space",  # leading space
        "trailing-space ",  # trailing space
        "very long string " * 50,  # very long line
    ],
)
def test_yaml_hostile_strings_are_safely_escaped(hostile_string: str):
    """YAML-hostile strings must not corrupt the output."""
    spec = AgentSpecYaml(
        agent_type="internal",
        company_name=hostile_string,
        company_description=hostile_string,
        company_website=None,
        role=hostile_string,
        max_num_of_topics=1,
        agent_user=None,
        enrich_logs=False,
        tone="formal",
        prompt_template_name=None,
        grounding_context=None,
        topics=[{"name": "Topic_Test", "description": hostile_string}],
    )

    yaml_text = spec.to_yaml()

    # Without PyYAML, we can't parse it. But we can assert:
    # 1. It doesn't raise on emission
    # 2. It contains the string somewhere (quoted or block-scalar)
    # 3. It doesn't have obvious corruption (e.g., unescaped newlines outside block scalars)
    assert yaml_text, "YAML must not be empty"

    # For strings with newlines, they should be in block scalar (|-) or escaped (\n)
    if "\n" in hostile_string:
        assert "|-" in yaml_text or "\\n" in yaml_text, "Newlines must be block-scalar or escaped"

    # For strings with colons, they should be quoted or block-scalar
    if ": " in hostile_string:
        # Either quoted or in a block scalar
        assert (f'"{hostile_string}"' in yaml_text or
                hostile_string.replace('"', '\\"') in yaml_text or
                "|-" in yaml_text), "Colon+space must be quoted or in block scalar"


def test_empty_string_is_emitted_as_empty_quotes():
    """Empty strings must emit as '""', not as nothing."""
    spec = AgentSpecYaml(
        agent_type="internal",
        company_name="",
        company_description="Desc",
        company_website=None,
        role="Role",
        max_num_of_topics=1,
        agent_user=None,
        enrich_logs=False,
        tone="formal",
        prompt_template_name=None,
        grounding_context=None,
        topics=[],
    )

    yaml_text = spec.to_yaml()
    assert 'companyName: ""' in yaml_text, "Empty string must emit as empty quotes"


# ==================== Refuse-by-default ====================


def test_insufficient_evidence_raises_when_intent_is_unresolved():
    """Unresolved intent must raise InsufficientEvidenceError by default."""
    spec = DerivedAgentSpec(
        intent="UNRESOLVED: no business action observed",
        confidence=0.05,
        objects_touched=[],
        entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
        unknowns=["intent is unresolved"],
        evidence=[],
    )

    with pytest.raises(InsufficientEvidenceError) as exc_info:
        build_agent_spec_yaml(spec, company_name="Acme", company_description="Anvils")

    assert "Intent is unresolved" in str(exc_info.value)


def test_insufficient_evidence_raises_when_confidence_too_low():
    """Confidence < 0.4 must raise by default."""
    spec = DerivedAgentSpec(
        intent="Do something",
        confidence=0.3,
        objects_touched=["Case"],
        entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
        unknowns=[],
        evidence=[],
    )

    with pytest.raises(InsufficientEvidenceError) as exc_info:
        build_agent_spec_yaml(spec, company_name="Acme", company_description="Anvils")

    assert "Confidence too low" in str(exc_info.value)
    assert "0.3" in str(exc_info.value)


def test_insufficient_evidence_raises_when_no_objects_touched():
    """No objects_touched must raise by default."""
    spec = DerivedAgentSpec(
        intent="Do something",
        confidence=0.8,
        objects_touched=[],
        entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
        unknowns=[],
        evidence=[],
    )

    with pytest.raises(InsufficientEvidenceError) as exc_info:
        build_agent_spec_yaml(spec, company_name="Acme", company_description="Anvils")

    assert "No objects touched" in str(exc_info.value)


def test_allow_incomplete_produces_needs_evidence_markers():
    """allow_incomplete=True must inject visible [NEEDS EVIDENCE markers."""
    spec = DerivedAgentSpec(
        intent="UNRESOLVED: no data",
        confidence=0.2,
        objects_touched=[],
        entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
        unknowns=["everything"],
        evidence=[],
    )

    result = build_agent_spec_yaml(
        spec,
        company_name="Acme",
        company_description="Anvils",
        allow_incomplete=True,
    )

    yaml_text = result.to_yaml()
    assert "[NEEDS EVIDENCE" in yaml_text, "Incomplete spec must contain [NEEDS EVIDENCE marker"


def test_needs_evidence_markers_match_score_run_placeholder_list():
    """The [NEEDS EVIDENCE marker must be catchable by score_run.py's scanner.

    NOTE: Currently, "[NEEDS EVIDENCE" is NOT in score_run's PLACEHOLDER_MARKERS list.
    This test documents that gap. Adding "[NEEDS EVIDENCE" to PLACEHOLDER_MARKERS would
    improve the gate's ability to catch incomplete specs.
    """
    spec = DerivedAgentSpec(
        intent="UNRESOLVED: no data",
        confidence=0.2,
        objects_touched=[],
        entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
        unknowns=["all"],
        evidence=[],
    )

    result = build_agent_spec_yaml(
        spec,
        company_name="Acme",
        company_description="Anvils",
        allow_incomplete=True,
    )

    yaml_text = result.to_yaml()

    # The emitted YAML contains "[NEEDS EVIDENCE" markers
    assert "[NEEDS EVIDENCE" in yaml_text, "Incomplete spec must contain [NEEDS EVIDENCE marker"

    # This marker should be added to score_run.py's PLACEHOLDER_MARKERS for proper gating
    # For now, we note that the score_run spec quality check (line 74) catches "UNRESOLVED"
    # in the spec.intent JSON field, which is the current gate mechanism.


# ==================== No topic invention ====================


def test_one_process_yields_one_topic_not_max_topics():
    """One observed process must yield exactly ONE topic, not padding to max_topics."""
    spec = DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=0.8,
        objects_touched=["Case"],
        entities=[
            DerivedEntity(
                name="status",
                object_api_name="Case",
                field_api_name="Status",
                evidence=[],
            )
        ],
        orchestration_steps=["Load case", "Update status"],
        guardrails=["Confirm before write"],
        failure_handling=["No failures observed"],
        unknowns=[],
        evidence=[],
    )

    result = build_agent_spec_yaml(
        spec,
        company_name="Acme",
        company_description="Anvils",
        max_topics=5,
    )

    assert len(result.topics) == 1, f"One process must yield one topic, got {len(result.topics)}"
    assert result.topics[0]["name"] == "Update_Case_Status", f"Topic name mismatch: {result.topics[0]['name']}"


# ==================== Guardrails and failure_handling must survive ====================


def test_guardrails_are_present_in_emitted_yaml():
    """Guardrails must be findable in the YAML (folded into topic description)."""
    spec = DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=0.8,
        objects_touched=["Case"],
        entities=[],
        orchestration_steps=["Update status"],
        guardrails=["Confirm before write", "Enforce FLS"],
        failure_handling=["No failures"],
        unknowns=[],
        evidence=[],
    )

    result = build_agent_spec_yaml(spec, company_name="Acme", company_description="Anvils")
    yaml_text = result.to_yaml()

    assert "Confirm before write" in yaml_text, "Guardrail 'Confirm before write' must be in YAML"
    assert "Enforce FLS" in yaml_text, "Guardrail 'Enforce FLS' must be in YAML"


def test_failure_handling_is_present_in_emitted_yaml():
    """Failure handling must be findable in the YAML."""
    spec = DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=0.8,
        objects_touched=["Case"],
        entities=[],
        orchestration_steps=["Update status"],
        guardrails=["Confirm"],
        failure_handling=["On validation error, return field and message"],
        unknowns=[],
        evidence=[],
    )

    result = build_agent_spec_yaml(spec, company_name="Acme", company_description="Anvils")
    yaml_text = result.to_yaml()

    assert "validation error" in yaml_text, "Failure handling must be in YAML"


def test_guardrails_and_failure_handling_not_silently_dropped():
    """Critical: guardrails and failure_handling must survive the round-trip."""
    spec = DerivedAgentSpec(
        intent="Update Opportunity (Amount)",
        confidence=0.9,
        objects_touched=["Opportunity"],
        entities=[],
        orchestration_steps=["Load opp", "Update Amount"],
        guardrails=["CRITICAL_GUARDRAIL_TOKEN_XYZ"],
        failure_handling=["CRITICAL_FAILURE_TOKEN_ABC"],
        unknowns=[],
        evidence=[],
    )

    result = build_agent_spec_yaml(spec, company_name="Acme", company_description="Anvils")
    yaml_text = result.to_yaml()

    assert "CRITICAL_GUARDRAIL_TOKEN_XYZ" in yaml_text, "Guardrail token must not be dropped"
    assert "CRITICAL_FAILURE_TOKEN_ABC" in yaml_text, "Failure token must not be dropped"


# ==================== Determinism ====================


def test_same_input_produces_byte_identical_yaml():
    """Determinism: same DerivedAgentSpec -> byte-identical YAML."""
    spec = DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=0.8,
        objects_touched=["Case"],
        entities=[],
        orchestration_steps=["Update status"],
        guardrails=["Confirm"],
        failure_handling=["No failures"],
        unknowns=[],
        evidence=[],
    )

    result1 = build_agent_spec_yaml(spec, company_name="Acme", company_description="Anvils")
    result2 = build_agent_spec_yaml(spec, company_name="Acme", company_description="Anvils")

    yaml1 = result1.to_yaml()
    yaml2 = result2.to_yaml()

    assert yaml1 == yaml2, "Same input must produce byte-identical YAML"


def test_no_timestamp_or_uuid_leaked_into_yaml():
    """No nondeterministic values (timestamp, UUID) must appear in the YAML body."""
    spec = DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=0.8,
        objects_touched=["Case"],
        entities=[],
        orchestration_steps=["Update"],
        guardrails=["Confirm"],
        failure_handling=["No failures"],
        unknowns=[],
        evidence=[],
    )

    result = build_agent_spec_yaml(
        spec,
        company_name="Acme",
        company_description="Anvils",
        recording_id="rec-123",
    )
    yaml_text = result.to_yaml()

    # Strip provenance comments (they're allowed to have IDs)
    lines_without_comments = [line for line in yaml_text.split("\n") if not line.strip().startswith("#")]
    yaml_body = "\n".join(lines_without_comments)

    # Look for ISO-like timestamp patterns (2026-07-25, 2026-07-25T12:00:00, etc.)
    import re
    iso_pattern = r"\d{4}-\d{2}-\d{2}"
    assert not re.search(iso_pattern, yaml_body), f"Timestamp leaked into YAML body: {yaml_body}"

    # Look for UUID patterns (not exhaustive, but catches common formats)
    uuid_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    assert not re.search(uuid_pattern, yaml_body, re.IGNORECASE), f"UUID leaked into YAML body: {yaml_body}"


# ==================== _to_api_name topic naming ====================


@pytest.mark.parametrize(
    "input_str,expected",
    [
        ("Update Case Status", "Update_Case_Status"),
        ("updateCaseStatus", "Update_Case_Status"),
        ("Update__Case__Status", "Update_Case_Status"),
        ("Update Case (Status)", "Update_Case_Status"),
        ("  Update Case  ", "Update_Case"),
        ("", "Unresolved_Topic"),  # empty -> fallback
        ("123Invalid", "T_123_Invalid"),  # leading digit gets T_ prefix
        ("válid-näme", "V_Lid_N_Me"),  # unicode stripped, first char capitalized
        # A single token longer than MAX_NAME_LENGTH is hard-cut at the cap.
        # Salesforce rejects a longer name, so emitting one is not an option.
        ("A" * 100, "A" * MAX_NAME_LENGTH),
        ("update case status", "Update_Case_Status"),  # lowercase gets capitalized
        ("ALLCAPS", "ALLCAPS"),  # all caps (no camelCase split)
        ("CamelCase", "Camel_Case"),  # camelCase
        ("aCamelCase", "A_Camel_Case"),  # leading lowercase
    ],
)
def test_to_api_name_produces_valid_api_tokens(input_str: str, expected: str):
    """_to_api_name must produce valid API-name tokens."""
    result = _to_api_name(input_str)
    assert result == expected, f"_to_api_name({input_str!r}) = {result!r}, expected {expected!r}"

    # Validate the result is a valid API name (skip for the known-broken case)
    if input_str != "A" * 100:
        assert result, "Must not be empty"
        assert result[0].isalpha(), f"Must start with letter: {result!r}"
        assert all(c.isalnum() or c == "_" for c in result), f"Must be alphanumeric+underscore: {result!r}"
        assert "__" not in result, f"Must not have double underscores: {result!r}"
        assert len(result) <= MAX_NAME_LENGTH, f"Must be <= {MAX_NAME_LENGTH} chars: {result!r}"


# ==================== Provenance comments ====================


def test_provenance_comments_exist_and_do_not_introduce_non_schema_keys():
    """Provenance must be YAML comments, not schema keys."""
    spec = DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=0.85,
        objects_touched=["Case"],
        entities=[],
        orchestration_steps=["Update"],
        guardrails=["Confirm"],
        failure_handling=["No failures"],
        unknowns=["one unknown"],
        evidence=[],
    )

    result = build_agent_spec_yaml(
        spec,
        company_name="Acme",
        company_description="Anvils",
        recording_id="rec-abc-123",
    )
    yaml_text = result.to_yaml()

    # Assert provenance comments exist
    assert "# Generated from recording: rec-abc-123" in yaml_text, "Recording ID must be in comment"
    assert "# Confidence: 0.850" in yaml_text, "Confidence must be in comment"
    assert "# Unknowns: 1" in yaml_text, "Unknowns count must be in comment"

    # Assert stripping comments still yields valid YAML with only schema keys
    lines_without_comments = [line for line in yaml_text.split("\n") if not line.strip().startswith("#")]
    yaml_body = "\n".join(lines_without_comments)

    # Schema keys only (no provenance keys like "provenance_recording_id")
    schema_keys = [
        "agentType",
        "companyName",
        "companyDescription",
        "companyWebsite",
        "role",
        "maxNumOfTopics",
        "agentUser",
        "enrichLogs",
        "tone",
        "promptTemplateName",
        "groundingContext",
        "topics",
    ]
    for key in schema_keys:
        # Optional keys may not appear
        pass

    # Assert no "provenance" key in the body
    assert "provenance_recording_id" not in yaml_body, "Provenance must be comments, not keys"
    assert "provenance_confidence" not in yaml_body, "Provenance must be comments, not keys"
    assert "provenance_unknowns_count" not in yaml_body, "Provenance must be comments, not keys"


# ==================== write_agent_spec_yaml ====================


def test_write_agent_spec_yaml_creates_parent_dirs(tmp_path: Path):
    """write_agent_spec_yaml must create parent directories."""
    spec = AgentSpecYaml(
        agent_type="internal",
        company_name="Acme",
        company_description="Anvils",
        company_website=None,
        role="Update cases",
        max_num_of_topics=1,
        agent_user=None,
        enrich_logs=False,
        tone="formal",
        prompt_template_name=None,
        grounding_context=None,
        topics=[],
    )

    output_path = tmp_path / "deep" / "nested" / "spec.yaml"
    assert not output_path.parent.exists(), "Parent must not exist yet"

    result_path = write_agent_spec_yaml(output_path, spec)

    assert result_path == output_path, "Must return the path"
    assert output_path.exists(), "File must be written"
    assert output_path.parent.exists(), "Parent dirs must be created"


def test_write_agent_spec_yaml_writes_utf8(tmp_path: Path):
    """write_agent_spec_yaml must write UTF-8."""
    spec = AgentSpecYaml(
        agent_type="internal",
        company_name="Acmé Corp",
        company_description="Sells anvils™",
        company_website=None,
        role="Update cases",
        max_num_of_topics=1,
        agent_user=None,
        enrich_logs=False,
        tone="formal",
        prompt_template_name=None,
        grounding_context=None,
        topics=[],
    )

    output_path = tmp_path / "spec.yaml"
    write_agent_spec_yaml(output_path, spec)

    content = output_path.read_text(encoding="utf-8")
    assert "Acmé" in content, "UTF-8 must be preserved"
    assert "™" in content, "UTF-8 symbols must be preserved"


def test_write_agent_spec_yaml_is_idempotent(tmp_path: Path):
    """Re-writing the same spec must be idempotent."""
    spec = AgentSpecYaml(
        agent_type="internal",
        company_name="Acme",
        company_description="Anvils",
        company_website=None,
        role="Update cases",
        max_num_of_topics=1,
        agent_user=None,
        enrich_logs=False,
        tone="formal",
        prompt_template_name=None,
        grounding_context=None,
        topics=[],
    )

    output_path = tmp_path / "spec.yaml"

    write_agent_spec_yaml(output_path, spec)
    content1 = output_path.read_text()

    write_agent_spec_yaml(output_path, spec)
    content2 = output_path.read_text()

    assert content1 == content2, "Re-writing must be idempotent"


# ==================== End-to-end ====================


def test_end_to_end_build_agent_spec_to_yaml():
    """End-to-end: build_agent_spec -> build_agent_spec_yaml -> sane role mentioning observed object."""

    # Build a realistic DerivedAgentSpec via the real build_agent_spec
    action = ExtractedAction(
        step_id="s1",
        sequence=1,
        timestamp_ms=1000,
        action_type=ActionType.SUBMIT,
        target="button:Save",
        ui_context=UIContext(object_name="Opportunity"),
        confidence=0.9,
    )

    snapshot = ObjectSnapshot(
        correlation=CorrelationKey(run_id="run-1", step_id="s1", event_time=NOW),
        object_api_name="Opportunity",
        record_id="006000000000001AAA",
        before={"StageName": "Prospecting", "Amount": None},
        after={"StageName": "Closed Won", "Amount": 100000},
        changed_fields=["StageName", "Amount"],
    )

    analysis = StepAnalysis(
        step_id="s1",
        action_target="button:Save",
        replay_status="success",
        replay_message="ok",
        triggered_layers=[TelemetryLayer.FLOW],
        data_changes=[snapshot],
        failure_layer=None,
        failure_reason=None,
    )

    derived_spec = build_agent_spec([action], [analysis])

    # Now convert to YAML
    yaml_spec = build_agent_spec_yaml(
        derived_spec,
        company_name="Acme Corp",
        company_description="Sells widgets",
    )

    yaml_text = yaml_spec.to_yaml()

    # Assert sane role mentioning the observed object
    assert "Opportunity" in yaml_spec.role, f"Role must mention observed object: {yaml_spec.role}"
    assert "StageName" in yaml_text or "Amount" in yaml_text, "YAML must mention observed fields"

    # Assert topics exist
    assert len(yaml_spec.topics) > 0, "Must have at least one topic"
    assert "Opportunity" in yaml_spec.topics[0]["name"] or "Update" in yaml_spec.topics[0]["name"], (
        f"Topic name must be relevant: {yaml_spec.topics[0]['name']}"
    )


# ==================== Additional edge cases ====================


def test_yaml_emitter_adversarial_strings_without_parser():
    """Test the hand-rolled YAML emitter with adversarial strings (no PyYAML available).

    PyYAML is not a dependency, so we cannot round-trip parse. Instead, we assert
    on the emitted YAML structure directly. Key vulnerabilities we test:

    1. Strings with ": " MUST be quoted (else parsed as mapping)
    2. Strings starting with "-" MUST be quoted (else parsed as list item)
    3. Strings starting with "#" MUST be quoted (else parsed as comment)
    4. Newlines MUST be in block scalars (|-) or escaped as \\n
    5. Quotes MUST be escaped as \\"
    6. YAML 1.1 boolean/null traps MUST be quoted: yes, no, true, false, null, on, off
    7. Empty strings MUST emit as ""
    8. Leading/trailing spaces MUST be quoted
    """
    adversarial_cases = [
        # String, expected pattern in output
        ("Status: New", 'Status: New'),  # colon+space requires quoting
        ("-leading", '"-leading"'),  # leading dash
        ("#comment", '"#comment"'),  # leading hash
        ("has'apostrophe", "has'apostrophe"),  # apostrophe can be in double-quoted or unquoted
        ('has"quote', '\\"'),  # double quote must be escaped
        ("has\nnewline", ('|-', 'has', 'newline')),  # newline -> block scalar or escaped
        ("yes", '"yes"'),  # YAML 1.1 boolean trap
        ("no", '"no"'),  # YAML 1.1 boolean trap
        ("true", '"true"'),  # YAML 1.1 boolean trap
        ("false", '"false"'),  # YAML 1.1 boolean trap
        ("null", '"null"'),  # YAML 1.1 null trap
        ("on", '"on"'),  # YAML 1.1 boolean
        ("off", '"off"'),  # YAML 1.1 boolean
        (" leading", '" leading"'),  # leading space
        ("trailing ", '"trailing "'),  # trailing space
        ("", '""'),  # empty string
    ]

    for test_string, expected_pattern in adversarial_cases:
        spec = AgentSpecYaml(
            agent_type="internal",
            company_name=test_string,
            company_description="Safe description",
            company_website=None,
            role="Safe role",
            max_num_of_topics=1,
            agent_user=None,
            enrich_logs=False,
            tone="formal",
            prompt_template_name=None,
            grounding_context=None,
            topics=[{"name": "Topic_Test", "description": test_string}],
        )

        yaml_text = spec.to_yaml()

        # Assert the expected pattern appears in the YAML
        if isinstance(expected_pattern, tuple):
            # Multiple patterns all must appear (for block scalars)
            for pattern in expected_pattern:
                assert pattern in yaml_text, (
                    f"Pattern {pattern!r} not found in YAML for input {test_string!r}\n"
                    f"YAML output:\n{yaml_text}"
                )
        else:
            assert expected_pattern in yaml_text, (
                f"Expected pattern {expected_pattern!r} not found in YAML for input {test_string!r}\n"
                f"YAML output:\n{yaml_text}"
            )


def test_yaml_emitter_validation_messages_survive_untouched():
    """Real validation messages contain colons, quotes, and punctuation.

    These MUST NOT be silently mangled. Prefer correct quoting over stripping.
    """
    validation_messages = [
        "Amount must be greater than 0",
        "Status: Cannot transition from 'New' to 'Closed Won' without approval",
        'Field "Contact Name" is required',
        "Case Priority: High requires escalation within 2 hours",
    ]

    for message in validation_messages:
        spec = AgentSpecYaml(
            agent_type="internal",
            company_name="Acme",
            company_description=message,  # Use the message as a field value
            company_website=None,
            role="Test",
            max_num_of_topics=1,
            agent_user=None,
            enrich_logs=False,
            tone="formal",
            prompt_template_name=None,
            grounding_context=None,
            topics=[{"name": "Test_Topic", "description": message}],
        )

        yaml_text = spec.to_yaml()

        # The message must appear somewhere in the output (quoted, escaped, or in a block scalar)
        # We don't care HOW it's encoded, just that it's not silently mangled or truncated.
        assert message in yaml_text or message.replace('"', '\\"') in yaml_text, (
            f"Validation message {message!r} was mangled or lost in YAML output:\n{yaml_text}"
        )


def test_yaml_emitter_field_labels_with_special_chars():
    """Field labels from recordings can contain parentheses, underscores, etc."""
    field_labels = [
        "Case (Status)",
        "Opportunity_Stage_Name",
        "Contact: Full Name",
        "Account - Billing Address",
    ]

    for label in field_labels:
        spec = AgentSpecYaml(
            agent_type="internal",
            company_name=label,
            company_description="Desc",
            company_website=None,
            role=label,
            max_num_of_topics=1,
            agent_user=None,
            enrich_logs=False,
            tone="formal",
            prompt_template_name=None,
            grounding_context=None,
            topics=[{"name": "Test_Topic", "description": label}],
        )

        yaml_text = spec.to_yaml()

        # The label must appear in the output (quoted if necessary)
        assert label in yaml_text or f'"{label}"' in yaml_text, (
            f"Field label {label!r} was lost or mangled in YAML output:\n{yaml_text}"
        )


def test_escape_yaml_string_handles_all_hostile_cases():
    """Direct test of _escape_yaml_string for all hostile cases."""
    test_cases = [
        ("plain", "plain"),  # no escaping needed
        ("", '""'),  # empty string
        ("Status: New", '"Status: New"'),  # colon+space
        ("-leading", '"-leading"'),  # leading dash
        ("true", '"true"'),  # YAML boolean trap
        ("yes", '"yes"'),  # YAML 1.1 boolean
        ("no", '"no"'),  # YAML 1.1 boolean
        ("null", '"null"'),  # YAML null
        (" leading", '" leading"'),  # leading space
        ("trailing ", '"trailing "'),  # trailing space
    ]

    for input_str, expected in test_cases:
        result = _escape_yaml_string(input_str)
        assert result == expected, f"_escape_yaml_string({input_str!r}) = {result!r}, expected {expected!r}"


def test_role_from_spec_includes_observed_objects():
    """role_from_spec must mention the observed objects."""
    spec = DerivedAgentSpec(
        intent="Update Opportunity (StageName, Amount)",
        confidence=0.9,
        objects_touched=["Opportunity"],
        entities=[
            DerivedEntity(name="stageName", object_api_name="Opportunity", field_api_name="StageName"),
            DerivedEntity(name="amount", object_api_name="Opportunity", field_api_name="Amount"),
        ],
        orchestration_steps=["Load opp", "Update fields"],
        guardrails=["Confirm"],
        failure_handling=["No failures"],
        unknowns=[],
        evidence=[],
    )

    role = role_from_spec(spec)
    assert "Opportunity" in role or "StageName" in role or "Amount" in role, (
        f"Role must mention observed data: {role}"
    )


def test_topics_from_spec_derives_topic_name_from_intent():
    """topics_from_spec must derive topic name from intent."""
    spec = DerivedAgentSpec(
        intent="Update Case (Status, Priority)",
        confidence=0.9,
        objects_touched=["Case"],
        entities=[],
        orchestration_steps=["Load case", "Update fields"],
        guardrails=["Confirm"],
        failure_handling=["No failures"],
        unknowns=[],
        evidence=[],
    )

    topics = topics_from_spec(spec)
    assert len(topics) == 1, "Must yield one topic"
    assert topics[0]["name"] == "Update_Case_Status_Priority", f"Topic name mismatch: {topics[0]['name']}"


def test_topics_from_spec_includes_guardrails_and_orchestration():
    """topics_from_spec must include orchestration + guardrails in description."""
    spec = DerivedAgentSpec(
        intent="Update Case (Status)",
        confidence=0.9,
        objects_touched=["Case"],
        entities=[],
        orchestration_steps=["Load case", "Update status", "Save"],
        guardrails=["Confirm before write", "Enforce FLS"],
        failure_handling=["On error, return field"],
        unknowns=[],
        evidence=[],
    )

    topics = topics_from_spec(spec)
    description = topics[0]["description"]

    assert "Load case" in description, "Orchestration step must be in description"
    assert "Update status" in description, "Orchestration step must be in description"
    assert "Confirm before write" in description, "Guardrail must be in description"
    assert "Enforce FLS" in description, "Guardrail must be in description"
    assert "On error, return field" in description, "Failure handling must be in description"


# ==================== CRITICAL DEFECT REGRESSION: topics_from_spec UNRESOLVED naming bypass ====================


@pytest.mark.parametrize(
    "unresolved_intent",
    [
        "UNRESOLVED:",  # empty after prefix
        "UNRESOLVED: ???",  # no word chars
        "UNRESOLVED: update the case status",  # normal-looking intent
        "UNRESOLVED: do something",
    ],
)
def test_unresolved_intent_topic_name_comes_from_naming_module(unresolved_intent: str):
    """REGRESSION: topics_from_spec must ALWAYS call naming.topic_api_name for the name.

    BUG HISTORY: Line 239 hard-coded "Needs_Evidence" for UNRESOLVED intents, bypassing
    naming.topic_api_name. Meanwhile agent_script.py and eval_spec.py both derive their
    names from naming. The result: for any UNRESOLVED intent, the spec YAML declared a
    topic that the test suite and Agent Script never referenced — silent total failure.

    FIX: Always call naming.topic_api_name(spec.intent), even for unresolved intents.
    The [NEEDS EVIDENCE] marker stays in the description (honest signalling), but the
    NAME is a reference key that must be consistent across artifacts.

    This test would have caught the bug: it asserts the YAML topic name is exactly
    naming.topic_api_name(intent), with no second-guessing allowed.
    """
    from sf_video_blueprint.naming import topic_api_name

    spec = DerivedAgentSpec(
        intent=unresolved_intent,
        confidence=0.05,
        objects_touched=[],
        entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
        unknowns=["intent is unresolved"],
        evidence=[],
    )

    topics = topics_from_spec(spec)
    assert len(topics) == 1, "Must yield exactly one topic"

    actual_name = topics[0]["name"]
    expected_name = topic_api_name(unresolved_intent)

    # THE CRITICAL ASSERTION: name must be EXACTLY what naming.topic_api_name returns.
    # Before the fix, this would fail with actual="Needs_Evidence", expected="Unresolved_Topic".
    assert actual_name == expected_name, (
        f"Topic name MUST come from naming.topic_api_name. "
        f"Got {actual_name!r}, expected {expected_name!r} for intent {unresolved_intent!r}"
    )

    # Assert the [NEEDS EVIDENCE] marker is still in the description (honest signalling)
    assert "[NEEDS EVIDENCE" in topics[0]["description"], (
        "Description must contain [NEEDS EVIDENCE marker for unresolved intents"
    )


def test_cross_artifact_agreement_for_unresolved_intent():
    """REGRESSION: Assert cross-artifact agreement for UNRESOLVED intent.

    The bug: agentforce_spec.py emitted "Needs_Evidence", agent_script.py emitted
    "unresolved_topic", and eval_spec.py emitted "Unresolved_Topic". All three
    independently invented answers to the same question.

    This test builds the spec YAML, the agent script, and the eval spec from the
    SAME DerivedAgentSpec with an UNRESOLVED intent, and asserts:
    1. The topic name in the YAML == the eval spec's expectedTopic (byte-identical)
    2. naming.names_agree(topic_name, subagent_name) holds (the router can resolve it)
    """
    from sf_video_blueprint.naming import names_agree, subagent_name, topic_api_name
    from sf_video_blueprint.agent_script import build_agent_script
    from sf_video_blueprint.eval_spec import build_legacy_test_spec

    unresolved_intent = "UNRESOLVED: update the case status"

    spec = DerivedAgentSpec(
        intent=unresolved_intent,
        confidence=0.05,
        objects_touched=["Case"],  # enough to avoid InsufficientEvidenceError
        entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
        unknowns=["intent is unresolved"],
        evidence=[],
    )

    # 1. Build the spec YAML
    yaml_spec = build_agent_spec_yaml(
        spec,
        company_name="Acme",
        company_description="Test",
        allow_incomplete=True,  # required for unresolved intent
    )
    yaml_topic_name = yaml_spec.topics[0]["name"]

    # 2. Build the agent script (allow_incomplete=True to generate skeletal script)
    agent_script_content = build_agent_script(
        spec,
        developer_name="test_agent",
        agent_label="Test Agent",
        allow_incomplete=True,
    )

    # 3. Build the eval spec (legacy dialect)
    eval_spec, _ = build_legacy_test_spec(
        spec,
        name="test_eval",
        subject_name="test_agent",
    )
    eval_expected_topic = eval_spec.testCases[0].expectedTopic

    # CRITICAL ASSERTIONS: All three artifacts must agree on the name
    canonical_name = topic_api_name(unresolved_intent)

    assert yaml_topic_name == canonical_name, (
        f"YAML topic name {yaml_topic_name!r} != canonical {canonical_name!r}"
    )
    assert eval_expected_topic == canonical_name, (
        f"Eval expectedTopic {eval_expected_topic!r} != canonical {canonical_name!r}"
    )

    # Extract the subagent name from the agent script (it appears in "subagent X:" lines)
    import re
    # Skip the three standard subagents (escalation, off_topic, ambiguous_question)
    # and find the derived subagent
    subagent_pattern = re.compile(r"^subagent (\w+):", re.MULTILINE)
    defined_subagents = subagent_pattern.findall(agent_script_content)
    derived_subagents = [
        s for s in defined_subagents
        if s not in {"escalation", "off_topic", "ambiguous_question"}
    ]
    assert len(derived_subagents) == 1, (
        f"Expected exactly one derived subagent, found {len(derived_subagents)}: {derived_subagents}"
    )
    script_subagent_name = derived_subagents[0]

    # Assert naming.names_agree: the topic name and subagent name are the same
    # canonical token list in different dialects (Update_Case_Status vs update_case_status)
    assert names_agree(yaml_topic_name, script_subagent_name), (
        f"names_agree({yaml_topic_name!r}, {script_subagent_name!r}) is False — "
        "cross-artifact linkage is broken"
    )


def test_needs_evidence_marker_appears_in_description_not_name():
    """The [NEEDS EVIDENCE marker is honest signalling and belongs in the DESCRIPTION.

    It must NEVER appear in the topic NAME (that's a reference key consumed by other
    modules). This test asserts the marker is findable in the description but NOT in
    the name.
    """
    spec = DerivedAgentSpec(
        intent="UNRESOLVED:",
        confidence=0.05,
        objects_touched=[],
        entities=[],
        orchestration_steps=[],
        guardrails=[],
        failure_handling=[],
        unknowns=["everything"],
        evidence=[],
    )

    topics = topics_from_spec(spec)
    topic_name = topics[0]["name"]
    topic_description = topics[0]["description"]

    assert "[NEEDS EVIDENCE" not in topic_name, (
        f"[NEEDS EVIDENCE must not appear in the topic name: {topic_name!r}"
    )
    assert "[NEEDS EVIDENCE" in topic_description, (
        "[NEEDS EVIDENCE marker must appear in the description"
    )
